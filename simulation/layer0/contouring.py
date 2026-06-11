"""Layer 0 — Continuous Contouring Utilities.

Replaces cell-cluster-based polygon extraction with marching squares
on continuous fields sampled at fine regular grids.

Architecture:
  CellData fields (elevation, temperature, precipitation, soil_fertility)
  are interpolated to ANY (lat, lon) via KDTree-IDW. A regular lat/lon
  grid is then classified or thresholded, and marching squares extracts
  isolines → polygons.

  Cells remain ONLY as display cache and click-detection (via h3).
  All geometry extraction is done at continuous resolution.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree
from shapely import concave_hull
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import polygonize, unary_union


# ======================================================================
# Continuous field from cell data (KDTree-IDW interpolation)
# ======================================================================


class ContinuousField:
    """Evaluate a cell field at ANY (lat, lon) via KDTree-IDW.

    Usage:
        elev_field = ContinuousField.from_cells(cells, "elevation_mean")
        val = elev_field(35.0, 120.0)  # elevation at Tokyo
    """

    def __init__(
        self,
        tree: cKDTree,
        values: np.ndarray,
        noise_func: Optional[Callable[[float, float], float]] = None,
    ):
        self._tree = tree
        self._values = values
        self._noise = noise_func

    @classmethod
    def from_cells(
        cls,
        cells: List,
        attribute: str,
        all_ids: Optional[List[str]] = None,
        noise_func: Optional[Callable[[float, float], float]] = None,
    ) -> "ContinuousField":
        """Build a ContinuousField from a list of CellData objects.

        Args:
            cells: List of CellData (or dict-like) objects.
            attribute: Attribute name to interpolate (e.g. "elevation_mean").
            all_ids: Optional H3 ID list (if different from cells).
            noise_func: Optional (lat, lon) → noise additive function.
        """
        import h3
        ids = all_ids or [c.h3_id for c in cells]
        cell_map = {c.h3_id: c for c in cells}
        points = []
        vals = []
        for h in ids:
            latlng = h3.cell_to_latlng(h)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            points.append([
                math.cos(lat_r) * math.cos(lon_r),
                math.sin(lat_r),
                math.cos(lat_r) * math.sin(lon_r),
            ])
            obj = cell_map.get(h)
            vals.append(getattr(obj, attribute, 0.0) if obj else 0.0)
        tree = cKDTree(np.array(points, dtype=np.float64))
        return cls(tree, np.array(vals, dtype=np.float64), noise_func)

    def __call__(self, lat: float, lon: float) -> float:
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        px = math.cos(lat_r) * math.cos(lon_r)
        py = math.sin(lat_r)
        pz = math.cos(lat_r) * math.sin(lon_r)

        dists, idxs = self._tree.query([px, py, pz], k=3)
        if np.any(dists < 1e-15):
            val = float(self._values[idxs[0]])
        else:
            w = 1.0 / (dists + 1e-15)
            val = float(np.average(self._values[idxs], weights=w))

        if self._noise is not None:
            val += self._noise(lat, lon)
        return val


# ======================================================================
# Regular lat/lon grid sampling
# ======================================================================


def sample_grid(
    field: Callable[[float, float], float],
    lat_step: float = 0.5,
    lon_step: float = 0.5,
    lat_range: Tuple[float, float] = (-90.0, 90.0),
    lon_range: Tuple[float, float] = (-180.0, 180.0),
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample a continuous field on a regular lat/lon grid.

    Returns:
        (lats_1d, lons_1d, values_2d) where values_2d[i, j] is the
        field value at (lats[i], lons[j]).
    """
    lats = np.arange(lat_range[0], lat_range[1] + lat_step * 0.5, lat_step)
    lons = np.arange(lon_range[0], lon_range[1] + lon_step * 0.5, lon_step)
    values = np.zeros((len(lats), len(lons)), dtype=np.float64)

    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            values[i, j] = field(float(lat), float(lon))

    return lats, lons, values


# ======================================================================
# Marching squares — extract isoline at threshold
# ======================================================================


def _march_cell(
    values: np.ndarray,
    i: int,
    j: int,
    threshold: float,
    lats: np.ndarray,
    lons: np.ndarray,
) -> List[Tuple[float, float]]:
    """March one grid cell, return interpolated edge crossings.

    Grid cell is at (i, j) top-left, (i+1, j+1) bottom-right.
    Uses linear interpolation along edges.
    """
    # Four corner values
    tl = values[i, j]
    tr = values[i, j + 1]
    bl = values[i + 1, j]
    br = values[i + 1, j + 1]

    # Corner case assignments (0 = below threshold, 1 = above/equal)
    case = 0
    if tl >= threshold:
        case |= 1
    if tr >= threshold:
        case |= 2
    if br >= threshold:
        case |= 4
    if bl >= threshold:
        case |= 8

    if case == 0 or case == 15:
        return []  # All below or all above

    lat0, lat1 = lats[i], lats[i + 1]
    lon0, lon1 = lons[j], lons[j + 1]

    # Edge interpolation functions
    def _interp_top(t):
        return (lat0, lon0 + (lon1 - lon0) * t)
    def _interp_bottom(t):
        return (lat1, lon0 + (lon1 - lon0) * t)
    def _interp_left(t):
        return (lat0 + (lat1 - lat0) * t, lon0)
    def _interp_right(t):
        return (lat0 + (lat1 - lat0) * t, lon1)

    def _t(v1, v2):
        if abs(v2 - v1) < 1e-15:
            return 0.5
        return (threshold - v1) / (v2 - v1)

    pts = []
    # Top edge: TL→TR, interp_top t=0 at TL, t=1 at TR
    if (case & 1) != (case & 2):
        pts.append(_interp_top(_t(tl, tr)))
    # Right edge: TR→BR, interp_right t=0 at TR, t=1 at BR
    if (case & 2) != (case & 4):
        pts.append(_interp_right(_t(tr, br)))
    # Bottom edge: BL→BR, interp_bottom t=0 at BL, t=1 at BR
    if (case & 4) != (case & 8):
        pts.append(_interp_bottom(_t(bl, br)))
    # Left edge: TL→BL, interp_left t=0 at TL, t=1 at BL
    if (case & 8) != (case & 1):
        pts.append(_interp_left(_t(tl, bl)))

    return pts


def marching_squares_isolines(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    threshold: float,
    min_length: float = 0.0,
) -> List[np.ndarray]:
    """Extract isolines at a given threshold via marching squares.

    Returns list of coordinate arrays, each shape (N, 2) in (lat, lon).
    """
    segments = []
    for i in range(len(lats) - 1):
        for j in range(len(lons) - 1):
            pts = _march_cell(values, i, j, threshold, lats, lons)
            if len(pts) == 2:
                segments.append([np.array(pts[0]), np.array(pts[1])])

    if not segments:
        return []

    # Chain segments into polylines by matching endpoints
    used = [False] * len(segments)
    lines = []

    while True:
        # Find first unused segment
        start_idx = -1
        for k in range(len(segments)):
            if not used[k]:
                start_idx = k
                break
        if start_idx == -1:
            break

        used[start_idx] = True
        # chain as list of (lat, lon) tuples
        chain = [segments[start_idx][0], segments[start_idx][1]]

        # Extend forward
        changed = True
        while changed:
            changed = False
            tail = chain[-1]
            for k in range(len(segments)):
                if used[k]:
                    continue
                s0, s1 = segments[k][0], segments[k][1]
                if np.allclose(tail, s0, atol=1e-8):
                    chain.append(s1)
                    used[k] = True
                    changed = True
                elif np.allclose(tail, s1, atol=1e-8):
                    chain.append(s0)
                    used[k] = True
                    changed = True

        # Extend backward
        changed = True
        while changed:
            changed = False
            head = chain[0]
            for k in range(len(segments)):
                if used[k]:
                    continue
                s0, s1 = segments[k][0], segments[k][1]
                if np.allclose(head, s0, atol=1e-8):
                    chain.insert(0, s1)
                    used[k] = True
                    changed = True
                elif np.allclose(head, s1, atol=1e-8):
                    chain.insert(0, s0)
                    used[k] = True
                    changed = True

        lines.append(np.array(chain))

    # Filter by min_length
    if min_length > 0:
        lines = [ln for ln in lines if np.sum(np.diff(ln, axis=0)**2, axis=1).sum() > min_length]

    return lines


# ======================================================================
# Threshold → polygon regions
# ======================================================================


def threshold_polygons(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    threshold: float,
    use_above: bool = True,
    min_area: float = 0.0,
    simplify_tol: float = 0.1,
) -> List[Polygon]:
    """Extract polygons where values >= or < threshold.

    Uses grid point clustering + concave hull for tight polygon fit.
    Multiple disjoint regions produce multiple polygons.

    Args:
        lats, lons: Grid axes.
        values: 2D array of field values.
        threshold: Isoline value.
        use_above: If True, regions where value >= threshold.
        min_area: Minimum polygon area (degrees²) to keep.
        simplify_tol: Shapely simplify tolerance in degrees.

    Returns:
        List of Shapely Polygons in (lon, lat) order.
    """
    from shapely import concave_hull
    from shapely.geometry import MultiPoint, Polygon as SPolygon

    # Build mask of points meeting threshold
    if use_above:
        mask = values >= threshold
    else:
        mask = values < threshold

    # Find connected components in mask using flood fill
    nlat, nlon = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: List[List[Tuple[int, int]]] = []

    for i in range(nlat):
        for j in range(nlon):
            if mask[i, j] and not visited[i, j]:
                stack = [(i, j)]
                comp: List[Tuple[int, int]] = []
                while stack:
                    ci, cj = stack.pop()
                    if ci < 0 or ci >= nlat or cj < 0 or cj >= nlon:
                        continue
                    if not mask[ci, cj] or visited[ci, cj]:
                        continue
                    visited[ci, cj] = True
                    comp.append((ci, cj))
                    # 8-direction connectivity
                    for di, dj in [(-1, 0), (1, 0), (0, -1), (0, 1),
                                   (-1, -1), (-1, 1), (1, -1), (1, 1)]:
                        stack.append((ci + di, cj + dj))
                if comp:
                    components.append(comp)

    polys: List[SPolygon] = []
    for comp in components:
        # Convert grid indices to (lon, lat) coordinates
        pts = []
        for ci, cj in comp:
            pts.append((float(lons[cj]), float(lats[ci])))
        if len(pts) < 3:
            continue

        try:
            mp = MultiPoint(pts)
            hull = concave_hull(mp, ratio=0.05)
            if hull is not None and hull.geom_type == "Polygon" and not hull.is_empty:
                if simplify_tol > 0:
                    hull = hull.simplify(simplify_tol, preserve_topology=True)
                if min_area <= 0 or hull.area > min_area:
                    polys.append(hull)
        except Exception:
            pass

    return polys


# ======================================================================
# Vegetation cover from continuous fields
# ======================================================================


def classify_vegetation_grid(
    lats: np.ndarray,
    lons: np.ndarray,
    soil_field: ContinuousField,
    temp_field: ContinuousField,
    precip_field: ContinuousField,
    classify_fn: Callable[[float, float, float], str],
    ocean_threshold: float = 0.0,
    ocean_field: Optional[ContinuousField] = None,
) -> Dict[str, np.ndarray]:
    """Classify each grid point by vegetation type.

    Returns dict mapping vegetation_type → boolean 2D mask.
    """
    masks: Dict[str, np.ndarray] = {}
    nlat, nlon = len(lats), len(lons)

    for i in range(nlat):
        lat = float(lats[i])
        for j in range(nlon):
            lon = float(lons[j])

            # Determine ocean
            is_ocean = False
            if ocean_field is not None:
                is_ocean = ocean_field(lat, lon) < ocean_threshold

            if is_ocean:
                veg = "barren"
            else:
                soil = soil_field(lat, lon)
                temp = temp_field(lat, lon)
                precip = precip_field(lat, lon)
                veg = classify_fn(soil, temp, precip)

            if veg not in masks:
                masks[veg] = np.zeros((nlat, nlon), dtype=bool)
            masks[veg][i, j] = True

    return masks


def vegetation_masks_to_polygons(
    masks: Dict[str, np.ndarray],
    lats: np.ndarray,
    lons: np.ndarray,
    min_cells: int = 4,
) -> List[Tuple[str, Polygon]]:
    """Convert classified vegetation masks to Shapely polygons.

    For each vegetation type, extracts connected components and
    converts to concave hull polygons.

    Returns list of (cover_type, polygon) tuples.
    """
    import h3
    results: List[Tuple[str, Polygon]] = []

    for veg_type, mask in masks.items():
        if veg_type == "barren":
            continue

        # Find connected components in the mask using flood fill
        nlat, nlon = mask.shape
        visited = np.zeros_like(mask, dtype=bool)
        components: List[List[Tuple[int, int]]] = []

        for i in range(nlat):
            for j in range(nlon):
                if mask[i, j] and not visited[i, j]:
                    # Flood fill
                    stack = [(i, j)]
                    comp: List[Tuple[int, int]] = []
                    while stack:
                        ci, cj = stack.pop()
                        if ci < 0 or ci >= nlat or cj < 0 or cj >= nlon:
                            continue
                        if not mask[ci, cj] or visited[ci, cj]:
                            continue
                        visited[ci, cj] = True
                        comp.append((ci, cj))
                        for di, dj in [(-1, 0), (1, 0), (0, -1), (0, 1),
                                       (-1, -1), (-1, 1), (1, -1), (1, 1)]:
                            stack.append((ci + di, cj + dj))
                    if len(comp) >= min_cells:
                        components.append(comp)

        for comp in components:
            # Convert grid coordinates to (lon, lat)
            pts = []
            for ci, cj in comp:
                pts.append((float(lons[cj]), float(lats[ci])))
            if len(pts) < 3:
                continue

            try:
                mp = MultiPoint(pts)
                hull = concave_hull(mp, ratio=0.05)
                if hull is not None and hull.geom_type == "Polygon" and not hull.is_empty:
                    results.append((veg_type, hull))
            except Exception:
                pass

    return results


# ======================================================================
# Contour band regions (for climate, soil, etc.)
# ======================================================================


def _range_mask_polygons(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    lower: float,
    upper: float,
    min_area: float = 0.0,
    simplify_tol: float = 0.1,
) -> List[Polygon]:
    """Extract polygons where lower <= values < upper (non-overlapping bands).

    Uses grid point clustering + concave hull for tight polygon fit.
    Multiple disjoint regions produce multiple polygons.
    """
    from shapely import concave_hull
    from shapely.geometry import MultiPoint, Polygon as SPolygon

    mask = (values >= lower) & (values < upper)

    nlat, nlon = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: List[List[Tuple[int, int]]] = []

    for i in range(nlat):
        for j in range(nlon):
            if mask[i, j] and not visited[i, j]:
                stack = [(i, j)]
                comp: List[Tuple[int, int]] = []
                while stack:
                    ci, cj = stack.pop()
                    if ci < 0 or ci >= nlat or cj < 0 or cj >= nlon:
                        continue
                    if not mask[ci, cj] or visited[ci, cj]:
                        continue
                    visited[ci, cj] = True
                    comp.append((ci, cj))
                    for di, dj in [(-1, 0), (1, 0), (0, -1), (0, 1),
                                   (-1, -1), (-1, 1), (1, -1), (1, 1)]:
                        stack.append((ci + di, cj + dj))
                if comp:
                    components.append(comp)

    polys: List[SPolygon] = []
    for comp in components:
        pts = [(float(lons[cj]), float(lats[ci])) for ci, cj in comp]
        if len(pts) < 3:
            continue
        try:
            mp = MultiPoint(pts)
            hull = concave_hull(mp, ratio=0.05)
            if hull is not None and hull.geom_type == "Polygon" and not hull.is_empty:
                if simplify_tol > 0:
                    hull = hull.simplify(simplify_tol, preserve_topology=True)
                if min_area <= 0 or hull.area > min_area:
                    polys.append(hull)
        except Exception:
            pass
    return polys


def contour_bands(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    num_bands: int = 5,
    min_area: float = 0.0,
) -> Dict[int, List[Polygon]]:
    """Extract contour band polygons at equally-spaced thresholds.

    Each band is a non-overlapping range [lower, upper), so bands
    NEVER overlap — unlike the old threshold-based approach where
    band 0 contained all higher bands.

    Returns dict mapping band_index (0..num_bands-1) → list of Polygons.
    """
    vmin, vmax = float(values.min()), float(values.max())
    band_size = (vmax - vmin) / max(num_bands, 1)
    bands: Dict[int, List[Polygon]] = {}

    for b in range(num_bands):
        lower = vmin + b * band_size
        upper = lower + band_size
        polys = _range_mask_polygons(lats, lons, values, lower, upper,
                                     min_area=min_area, simplify_tol=0.1)
        if polys:
            bands[b] = polys

    return bands
