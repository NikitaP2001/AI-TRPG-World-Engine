"""Layer 0 — Texture atlas renderer.

Feature-driven topographic mapping:
  - Mountains: elevation contour bands (brown/tan above thresholds)
  - Rivers: polylines from flow accumulation, width proportional to flow volume
  - Forests: green tint on vegetated biomes
  - Ocean: depth shading from temperature
  - Hex borders: thin navigation lines

Features are scale-gated by their pixel extent at the texture resolution.
"""

from __future__ import annotations
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import h3
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from shapely.geometry import LineString as SLineString, Polygon as SPolygon
from .cell_model import CellData
from .feature_store import FeatureStore

# ── Biome base colours (muted, topographic-map style) ────────────
_BIOME = {
    "Af": (60, 125, 45), "Am": (80, 140, 55), "Aw": (105, 150, 65),
    "BWh": (220, 190, 125), "BWk": (210, 195, 155), "BSh": (200, 180, 135),
    "BSk": (190, 175, 145), "Cfa": (80, 135, 68), "Cwa": (95, 142, 75),
    "Cs": (138, 158, 98), "Dfb": (50, 105, 52), "Dwc": (62, 95, 108),
    "EF": (212, 212, 222), "ET": (168, 188, 198),
}

# ── Elevation contour colours (classic topographic map) ──────────
# Bands at lower elevations for more visible terrain features
_CONTOUR_FLAT = (70, 135, 60)       # el 0.0-0.2: lowland green
_CONTOUR_LOW = (120, 155, 85)       # el 0.2-0.4: light olive-green
_CONTOUR_MID = (185, 165, 110)      # el 0.4-0.55: tan
_CONTOUR_HIGH = (165, 130, 80)      # el 0.55-0.70: brown
_CONTOUR_PEAK = (135, 95, 55)       # el 0.70-0.85: dark brown
_CONTOUR_SNOW = (205, 200, 200)     # el > 0.85: snow

_OCEAN_DEEP = (18, 32, 95)
_OCEAN_MID = (30, 55, 120)
_OCEAN_SHALLOW = (45, 80, 150)

_RIVER_COL = (40, 140, 230)   # bright blue for contrast
_FOREST_TINT = (35, 75, 35)
_HEX_BORDER = (0, 0, 0, 20)

# ── Palette for vector feature overlays ─────────────────────────
_MOUNTAIN_FILL = (175, 115, 55)       # warm brown, distinct from base contour bands
_MOUNTAIN_OUTLINE = (80, 45, 15)       # dark brown outline
_DESERT_TINT = (235, 210, 160, 70)
_GRASSLAND_TINT = (150, 175, 115, 60)
_SHRUBLAND_TINT = (180, 170, 120, 60)
_SAVANNA_TINT = (195, 180, 120, 60)
_TAIGA_TINT = (70, 120, 90, 70)
_TUNDRA_TINT = (170, 185, 195, 60)
_ICE_TINT = (230, 230, 240, 70)


def _equirect(lat: float, lng: float, w: int, h: int) -> Tuple[int, int]:
    """Standard equirectangular projection lat -90..+90, lng -180..+180."""
    return (int((lng + 180.0) / 360.0 * w) % w,
            max(0, min(h - 1, int((90.0 - lat) / 180.0 * h))))


def _terrain_color(el: float, biome: str, is_ocean: bool, temp: float) -> Tuple[int, int, int]:
    """Topographic colour: elevation is primary, biome adds subtle tint."""
    if is_ocean:
        d = int(min(1.0, max(0.0, 1.0 - temp)) * 200)
        if d < 80:
            c = _OCEAN_SHALLOW
        elif d < 160:
            c = _OCEAN_MID
        else:
            c = _OCEAN_DEEP
        return (max(0, c[0] - d // 8), max(0, c[1] - d // 6), min(255, c[2] + d // 4))

    # Elevation contour bands — matched to tectonic range [-0.35, 1.5]
    if el > 0.80:
        return _CONTOUR_SNOW
    elif el > 0.60:
        return _CONTOUR_PEAK
    elif el > 0.45:
        return _CONTOUR_HIGH
    elif el > 0.30:
        return _CONTOUR_MID
    elif el > 0.15:
        return _CONTOUR_LOW
    else:
        return _CONTOUR_FLAT


def _geom_to_pixels(geom: Any, width: int, height: int) -> Any:
    """Convert a Shapely geometry (lon/lat) to equirectangular pixel coords."""
    try:
        if geom is None:
            return None
        if geom.geom_type == "Polygon":
            exterior = [_equirect(lat, lon, width, height) for lon, lat in geom.exterior.coords]
            interiors = []
            for ring in geom.interiors:
                interiors.append([_equirect(lat, lon, width, height) for lon, lat in ring.coords])
            return SPolygon(exterior, interiors)
        elif geom.geom_type == "LineString":
            coords = [_equirect(lat, lon, width, height) for lon, lat in geom.coords]
            return SLineString(coords)
        elif geom.geom_type == "MultiPolygon":
            from shapely.geometry import MultiPolygon
            polys = [p for p in (_geom_to_pixels(g, width, height) for g in geom.geoms) if p is not None]
            return MultiPolygon(polys) if polys else None
        elif geom.geom_type == "MultiLineString":
            from shapely.geometry import MultiLineString
            lines = [l for l in (_geom_to_pixels(g, width, height) for g in geom.geoms) if l is not None]
            return MultiLineString(lines) if lines else None
    except Exception:
        return None
    return None


def _smooth_line_coords(coords: List[Tuple[float, float]], n: int = 100) -> List[Tuple[float, float]]:
    """Smooth a polyline by linear interpolation between vertices.
    
    Converts chunky zigzag (H3 cell-centroid paths) into a smooth curve
    with n evenly-spaced points. Falls back to original if < 3 coords.
    """
    if len(coords) < 3:
        return coords
    arr = np.array(coords, dtype=np.float64)
    # Cumulative distance along path
    seg_dists = np.sqrt(np.sum(np.diff(arr, axis=0) ** 2, axis=1))
    total = seg_dists.sum()
    if total < 1.0:
        return coords
    t = np.zeros(len(arr))
    t[1:] = np.cumsum(seg_dists) / total
    ti = np.linspace(0, 1, n)
    # Linear interpolation (avoids scipy dependency)
    xi = np.interp(ti, t, arr[:, 0])
    yi = np.interp(ti, t, arr[:, 1])
    return [(float(xi[i]), float(yi[i])) for i in range(n)]


def _draw_polygon_px(draw: ImageDraw.ImageDraw, geom_px: Any, fill,
                      outline=None, img_width: int = 4096) -> None:
    """Draw a Shapely Polygon (in pixel coords), handling antimeridian wrap."""
    if geom_px is None:
        return
    try:
        ext = list(geom_px.exterior.coords)
        if len(ext) < 3:
            return
        xs = [p[0] for p in ext]
        if max(xs) - min(xs) > img_width * 0.6:
            h1 = [(x - img_width, y) if x > img_width // 2 else (x, y) for (x, y) in ext]
            h2 = [(x + img_width, y) if x < img_width // 2 else (x, y) for (x, y) in ext]
            for hl in [h1, h2]:
                if len(hl) >= 3:
                    draw.polygon(hl, fill=fill)
                    if outline:
                        for i in range(len(hl)):
                            draw.line([hl[i], hl[(i + 1) % len(hl)]], fill=outline, width=1)
        else:
            draw.polygon(ext, fill=fill)
            if outline:
                for i in range(len(ext)):
                    draw.line([ext[i], ext[(i + 1) % len(ext)]], fill=outline, width=1)
    except Exception:
        pass


def _draw_line_px(draw: ImageDraw.ImageDraw, geom_px: Any, color,
                   line_width: int = 2, img_width: int = 4096) -> None:
    """Draw a Shapely LineString (in pixel coords), handling antimeridian wrap."""
    if geom_px is None:
        return
    try:
        coords = list(geom_px.coords)
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            d = x2 - x1
            if abs(d) > img_width * 0.5:
                if d > 0:
                    draw.line([(x1, y1), (x2 - img_width, y2)], fill=color, width=line_width)
                    draw.line([(x1 + img_width, y1), (x2, y2)], fill=color, width=line_width)
                else:
                    draw.line([(x1, y1), (x2 + img_width, y2)], fill=color, width=line_width)
                    draw.line([(x1 - img_width, y1), (x2, y2)], fill=color, width=line_width)
            else:
                draw.line([(x1, y1), (x2, y2)], fill=color, width=line_width)
    except Exception:
        pass


def _render_features(ci: Image.Image, cells: List[CellData],
                      feature_store: FeatureStore,
                      width: int, height: int) -> Image.Image:
    """No-op: feature store polygons are cluster convex-hulls at resolution 2,
    creating unrealistically large triangular artifacts. The base elevation
    colours produce cleaner results. Features remain in store for game logic."""
    return ci


def _render_flow_accum_raster(cells: List[CellData],
                                flow_accum: Dict[str, float],
                                width: int, height: int) -> Image.Image:
    """Render flow accumulation as a smooth blue river overlay."""
    flow_img = Image.new("L", (width, height), 0)
    fd = ImageDraw.Draw(flow_img)
    for cell in cells:
        acc = flow_accum.get(cell.h3_id, 0.0)
        if acc < 2.0:
            continue
        latlng = h3.cell_to_latlng(cell.h3_id)
        px, py = _equirect(latlng[0], latlng[1], width, height)
        intensity = int(min(255, math.log(acc + 1) * 80))
        if intensity < 5:
            continue
        fd.ellipse([px - 3, py - 3, px + 3, py + 3], fill=intensity)
    flow_blur = flow_img.filter(ImageFilter.GaussianBlur(radius=12))
    fa = np.array(flow_blur, dtype=np.float32) / 255.0
    fa = np.clip(fa, 0.0, 1.0)
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    overlay[:, :, 0] = (fa * 40).astype(np.uint8)
    overlay[:, :, 1] = (fa * 140).astype(np.uint8)
    overlay[:, :, 2] = (fa * 230).astype(np.uint8)
    overlay[:, :, 3] = (fa * 200).astype(np.uint8)
    return Image.fromarray(overlay, "RGBA")


def render_textures(
    cells: List[CellData],
    out: Path,
    width: int = 4096,
    height: int = 2048,
    flow_accum: Optional[Dict[str, float]] = None,
    feature_store: Optional[FeatureStore] = None,
) -> None:
    """Render planet textures using polygon rendering + smooth blur.

    Each cell is drawn as a polygon (handles equirectangular projection
    correctly — poles stretch, antimeridian wraps). Gaussian blur then
    smooths cell boundaries. Vector features and river raster composite
    on top for geographic detail.
    """
    import numpy as np
    out.mkdir(parents=True, exist_ok=True)
    print(f"[Renderer] {len(cells)} cells -> {width}x{height}")

    # ── 1. Base layer: each cell as polygon with contour colour ──
    ci = Image.new("RGB", (width, height), _OCEAN_DEEP)
    cd = ImageDraw.Draw(ci)
    hi = Image.new("L", (width, height), 0)
    hd = ImageDraw.Draw(hi)

    for idx, cell in enumerate(cells):
        if idx % 5000 == 0 and idx > 0:
            print(f"  ... {idx}/{len(cells)}")
        bnd = h3.cell_to_boundary(cell.h3_id)
        pv = [_equirect(b[0], b[1], width, height) for b in bnd]
        if len(pv) < 3:
            continue

        is_ocean = cell.geological_type == 0
        el = cell.elevation_mean
        temp = cell.temperature

        # Choose colour
        if is_ocean:
            d = int(min(1.0, max(0.0, 1.0 - temp)) * 200)
            if d < 80:
                c = _OCEAN_SHALLOW
            elif d < 160:
                c = _OCEAN_MID
            else:
                c = _OCEAN_DEEP
            col = (max(0, c[0] - d // 8), max(0, c[1] - d // 6), min(255, c[2] + d // 4))
        else:
            if el > 0.85:
                col = _CONTOUR_SNOW
            elif el > 0.70:
                col = _CONTOUR_PEAK
            elif el > 0.55:
                col = _CONTOUR_HIGH
            elif el > 0.40:
                col = _CONTOUR_MID
            elif el > 0.20:
                col = _CONTOUR_LOW
            else:
                col = _CONTOUR_FLAT

        h_val = int(el * 65535)

        xs = [p[0] for p in pv]
        if max(xs) - min(xs) > width * 0.6:
            h1 = [(x - width, y) if x > width // 2 else (x, y) for (x, y) in pv]
            h2 = [(x + width, y) if x < width // 2 else (x, y) for (x, y) in pv]
            for hl in [h1, h2]:
                if len(hl) >= 3:
                    cd.polygon(hl, fill=col)
                    hd.polygon(hl, fill=h_val)
        else:
            cd.polygon(pv, fill=col)
            hd.polygon(pv, fill=h_val)

    # ── 2. Smooth cell boundaries ──
    ci = ci.filter(ImageFilter.BoxBlur(1))
    hi = hi.filter(ImageFilter.BoxBlur(1))

    # ── 3. Rivers — flow accumulation raster ──
    if flow_accum:
        ri = _render_flow_accum_raster(cells, flow_accum, width, height)
        ci = Image.alpha_composite(ci.convert("RGBA"), ri).convert("RGB")

    # ── 4. No hex borders on main texture (creates "parallels" at global zoom) ──
    # Borders remain available in the tile viewer for high-zoom detail.

    # ── 5. Height map ──
    ha = np.array(hi, dtype=np.float32) / 65535.0

    # ── 6. Normal map (vectorised) ──
    print("[Renderer] normal map...")
    dx = np.zeros_like(ha)
    dy = np.zeros_like(ha)
    dx[:, 1:-1] = (ha[:, 2:] - ha[:, :-2]) * 2.0
    dy[1:-1, :] = (ha[2:, :] - ha[:-2, :]) * 2.0
    L = np.sqrt(dx * dx + dy * dy + 1.0)
    nr = np.clip(128 + 127 * (-dx / L), 0, 255).astype(np.uint8)
    ng = np.clip(128 + 127 * (-dy / L), 0, 255).astype(np.uint8)
    nb = np.clip(128 + 127 * (1.0 / L), 0, 255).astype(np.uint8)
    na = np.stack([nr, ng, nb], axis=-1)
    ni = Image.fromarray(na, "RGB")

    # ── Save ──
    ci.save(out / "planet_color.png")
    hi.save(out / "planet_height.png")
    ni.save(out / "planet_normal.png")
    print(f"[Renderer] saved textures to {out}")
