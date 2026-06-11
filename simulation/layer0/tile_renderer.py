"""Layer 0 — Tile pyramid renderer (topographic map style).

Generates tiles at multiple zoom levels for the LOD planet viewer.
Uses the same feature-driven topographic style as the main renderer:

  - Elevation contour bands (green/tan/brown/snow)
  - Rivers: blue dots at cell centroids, sized by flow accumulation
  - Forests: semi-transparent green tint overlay
  - Ocean: depth shading from temperature
  - Hex borders: thin navigation lines

Level 0:   4 tiles x 2 tiles  (full equirectangular map)
Level 1:   8 x 4
Level 2:  16 x 8
Level 3:  32 x 16
Level 4:  64 x 32
Level 5: 128 x 64
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import h3
import numpy as np
from PIL import Image, ImageDraw

from .cell_model import CellData

TILE_SIZE = 1024
MAX_LEVEL = 5

# ── Biome base colours (muted, topographic-map style) ────────────
_BIOME = {
    "Af": (60, 125, 45), "Am": (80, 140, 55), "Aw": (105, 150, 65),
    "BWh": (220, 190, 125), "BWk": (210, 195, 155), "BSh": (200, 180, 135),
    "BSk": (190, 175, 145), "Cfa": (80, 135, 68), "Cwa": (95, 142, 75),
    "Cs": (138, 158, 98), "Dfb": (50, 105, 52), "Dwc": (62, 95, 108),
    "EF": (212, 212, 222), "ET": (168, 188, 198),
}

# ── Elevation contour colours (classic topographic map) ──────────
_CONTOUR_FLAT = (70, 135, 60)
_CONTOUR_LOW = (120, 155, 85)
_CONTOUR_MID = (185, 165, 110)
_CONTOUR_HIGH = (165, 130, 80)
_CONTOUR_PEAK = (135, 95, 55)
_CONTOUR_SNOW = (205, 200, 200)

_OCEAN_DEEP = (18, 32, 95)
_OCEAN_MID = (30, 55, 120)
_OCEAN_SHALLOW = (45, 80, 150)

_RIVER_COL = (60, 125, 205)
_FOREST_TINT = (35, 75, 35)
_HEX_BORDER = (0, 0, 0, 15)
_HEX_BORDER_ZOOM = (0, 0, 0, 25)


# ======================================================================
# Geometry helpers
# ======================================================================


def _tile_bounds(level: int, tx: int, ty: int) -> Tuple[float, float, float, float]:
    """Tile bounds in lat/lon. Exact division — no overlap."""
    cols = 4 * (2 ** level)
    rows = 2 * (2 ** level)
    lon_min = (tx / cols) * 360.0 - 180.0
    lon_max = ((tx + 1) / cols) * 360.0 - 180.0
    lat_max = 90.0 - (ty / rows) * 180.0
    lat_min = 90.0 - ((ty + 1) / rows) * 180.0
    return (lon_min, lat_min, lon_max, lat_max)


def _cell_centroid_px(cell: CellData, lon_min: float, lat_max: float,
                       dlon: float, dlat: float) -> Tuple[int, int]:
    """Return (px, py) pixel coordinates of a cell's centroid within a tile."""
    latlng = h3.cell_to_latlng(cell.h3_id)
    px = int((latlng[1] - lon_min) / dlon * TILE_SIZE)
    py = int((lat_max - latlng[0]) / dlat * TILE_SIZE)
    return (px, py)


def _hex_vertices_px(cell: CellData, lon_min: float, lat_max: float,
                      dlon: float, dlat: float) -> List[Tuple[int, int]]:
    """Return hex polygon vertices in tile pixel coords."""
    verts = []
    for b in h3.cell_to_boundary(cell.h3_id):
        px = int((b[1] - lon_min) / dlon * TILE_SIZE)
        py = int((lat_max - b[0]) / dlat * TILE_SIZE)
        verts.append((px, py))
    return verts


def _build_index(cells: List[CellData]) -> Dict[int, Dict[int, List[CellData]]]:
    """Build spatial index: level -> flat_tile_key -> [cells]."""
    index: Dict[int, Dict[int, List[CellData]]] = {}
    for cell in cells:
        latlng = h3.cell_to_latlng(cell.h3_id)
        for level in range(MAX_LEVEL + 1):
            cols = 4 * (2 ** level)
            rows = 2 * (2 ** level)
            tx = int((latlng[1] + 180.0) / 360.0 * cols)
            ty = int((90.0 - latlng[0]) / 180.0 * rows)
            tx = max(0, min(cols - 1, tx))
            ty = max(0, min(rows - 1, ty))
            key = ty * cols + tx
            index.setdefault(level, {}).setdefault(key, []).append(cell)
    return index


# ======================================================================
# Drawing helpers
# ======================================================================


def _terrain_color(el: float, biome: str, is_ocean: bool, temp: float) -> Tuple[int, int, int]:
    """Topographic colour: elevation is primary color driver."""
    if is_ocean:
        d = int(min(1.0, max(0.0, 1.0 - temp)) * 200)
        if d < 80:
            c = _OCEAN_SHALLOW
        elif d < 160:
            c = _OCEAN_MID
        else:
            c = _OCEAN_DEEP
        return (max(0, c[0] - d // 8), max(0, c[1] - d // 6), min(255, c[2] + d // 4))

    if el > 0.85:
        return _CONTOUR_SNOW
    elif el > 0.70:
        return _CONTOUR_PEAK
    elif el > 0.55:
        return _CONTOUR_HIGH
    elif el > 0.40:
        return _CONTOUR_MID
    elif el > 0.20:
        return _CONTOUR_LOW
    else:
        return _CONTOUR_FLAT


def _draw_rivers(draw: ImageDraw.ImageDraw, cells: List[CellData],
                  lon_min: float, lat_max: float, dlon: float, dlat: float,
                  flow_accum: Dict[str, float], min_flow: float = 5.0) -> None:
    """Draw rivers as blue circles at cell centroids, sized by flow accumulation."""
    if not flow_accum:
        return
    max_acc = max(flow_accum.values()) if flow_accum else 1.0
    for cell in cells:
        acc = flow_accum.get(cell.h3_id, 0.0)
        if acc < min_flow:
            continue
        cx, cy = _cell_centroid_px(cell, lon_min, lat_max, dlon, dlat)
        r = max(1.0, math.log(acc + 1) * 0.5)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_RIVER_COL)


def _draw_forest_tint(draw: ImageDraw.ImageDraw, cells: List[CellData],
                       lon_min: float, lat_max: float, dlon: float, dlat: float) -> None:
    """Draw green tint on forested cells."""
    forest_classes = {"Af", "Am", "Cfa", "Cwa", "Dfb"}
    for cell in cells:
        if cell.climate_class not in forest_classes or cell.geological_type == 0:
            continue
        pv = _hex_vertices_px(cell, lon_min, lat_max, dlon, dlat)
        if len(pv) < 3:
            continue
        draw.polygon(pv, fill=_FOREST_TINT + (80,))


# ======================================================================
# Main tile rendering
# ======================================================================


def render_tile(
    cells: List[CellData],
    level: int,
    tx: int,
    ty: int,
    cell_index: Optional[Dict[int, Dict[int, List[CellData]]]] = None,
    flow_accum: Optional[Dict[str, float]] = None,
) -> Image.Image:
    """Render one 512x512 fantasy-map style tile."""
    lon_min, lat_min, lon_max, lat_max = _tile_bounds(level, tx, ty)
    dlon = lon_max - lon_min
    dlat = lat_max - lat_min

    # Get cells for this tile
    if cell_index:
        cols = 4 * (2 ** level)
        key = ty * cols + tx
        tile_cells = cell_index.get(level, {}).get(key, [])
    else:
        tile_cells = []
        for cell in cells:
            latlng = h3.cell_to_latlng(cell.h3_id)
            if lon_min <= latlng[1] <= lon_max and lat_min <= latlng[0] <= lat_max:
                tile_cells.append(cell)

    if not tile_cells:
        return Image.new("RGB", (TILE_SIZE, TILE_SIZE), _OCEAN_DEEP)

    tile_cells.sort(key=lambda c: c.resolution, reverse=True)

    # ── 1. Fill background with ocean ──
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), _OCEAN_DEEP)
    draw = ImageDraw.Draw(img)

    # ── 2. Draw land cells with topographic contour colours ──
    for cell in tile_cells:
        if cell.geological_type == 0:
            continue  # ocean — already background
        pv = _hex_vertices_px(cell, lon_min, lat_max, dlon, dlat)
        if len(pv) < 3:
            continue

        # Topographic colour from elevation contour bands
        is_ocean = cell.geological_type == 0
        col = _terrain_color(cell.elevation_mean, cell.climate_class, is_ocean, cell.temperature)

        # Land cells
        if not is_ocean:
            draw.polygon(pv, fill=col)

    # ── 3. Draw rivers ──
    if flow_accum:
        _draw_rivers(draw, tile_cells, lon_min, lat_max, dlon, dlat, flow_accum)

    # ── 4. Draw forest tint overlay (RGBA composite) ──
    fi = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fi)
    _draw_forest_tint(fd, tile_cells, lon_min, lat_max, dlon, dlat)
    img = Image.alpha_composite(img.convert("RGBA"), fi).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── 5. Draw hex borders (subtle, only at high zoom) ──
    if level >= 3:
        border_col = _HEX_BORDER_ZOOM
        for cell in tile_cells:
            pv = _hex_vertices_px(cell, lon_min, lat_max, dlon, dlat)
            if len(pv) < 3:
                continue
            for i in range(len(pv)):
                draw.line([pv[i], pv[(i + 1) % len(pv)]], fill=border_col, width=1)

    # ── 6. Redraw ocean cells on top (so coast is clean) ──
    for cell in tile_cells:
        if cell.geological_type != 0:
            continue
        pv = _hex_vertices_px(cell, lon_min, lat_max, dlon, dlat)
        if len(pv) < 3:
            continue
        col = _terrain_color(0.0, "", True, cell.temperature)
        draw.polygon(pv, fill=col)

    return img


# ======================================================================
# Batch tile generation
# ======================================================================


def generate_all_tiles(
    cells: List[CellData],
    output_dir: Path,
    max_level: int = MAX_LEVEL,
    flow_accum: Optional[Dict[str, float]] = None,
) -> int:
    """Generate all tiles for all zoom levels."""
    base_dir = output_dir / "tiles"
    cell_index = _build_index(cells)
    total = 0

    for level in range(max_level + 1):
        cols = 4 * (2 ** level)
        rows = 2 * (2 ** level)
        level_dir = base_dir / str(level)
        level_dir.mkdir(parents=True, exist_ok=True)

        print(f"[Tiles] Level {level}: {cols}x{rows} tiles")
        for ty in range(rows):
            row_dir = level_dir / str(ty)
            row_dir.mkdir(exist_ok=True)
            for tx in range(cols):
                tile_path = row_dir / f"{tx}.png"
                if tile_path.exists():
                    total += 1
                    continue
                img = render_tile(cells, level, tx, ty, cell_index, flow_accum)
                img.save(tile_path)
                total += 1
            print(f"  Row {ty + 1}/{rows} done")

    print(f"[Tiles] Generated {total} tiles across {level + 1} levels")
    return total
