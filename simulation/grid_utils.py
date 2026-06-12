"""Shared grid utilities — KDTree sampling, flood-fill, spherical geometry.

Common operations needed by L0, L1, and L2 layers. Avoids duplicating
the KDTree-IDW sampling and flood-fill patterns across the codebase.

Main functions:
    sample_field_vectorized(field, lats, lons) — batch KDTree query
    build_field_kdtree(cells_or_ids, values) — build KDTree from CellData
    sample_field_scalar(field, lats, lons) — per-point fallback
    flood_fill_grid(mask) — connected components on lat/lon grid
    spherical_xyz(lat, lon) — lat/lon → 3D cartesian
    spherical_xyz_vec(lats, lons) — vectorized version
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree


# ======================================================================
# Spherical coordinate helpers
# ======================================================================


def spherical_xyz(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    """Convert lat/lon degrees to 3D unit vector."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    return (
        math.cos(lat_r) * math.cos(lon_r),
        math.sin(lat_r),
        math.cos(lat_r) * math.sin(lon_r),
    )


def spherical_xyz_vec(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Vectorized lat/lon → 3D. Input: 1D or 2D arrays. Returns (N, 3)."""
    lat_r = np.radians(lats)
    lon_r = np.radians(lons)
    cos_lat = np.cos(lat_r)
    x = cos_lat * np.cos(lon_r)
    y = np.sin(lat_r)
    z = cos_lat * np.sin(lon_r)
    if lats.ndim == 2:
        return np.column_stack([x.ravel(), y.ravel(), z.ravel()])
    return np.column_stack([x, y, z])


# ======================================================================
# KDTree construction from H3 cell data
# ======================================================================


def build_kdtree_from_cells(
    h3_ids: List[str],
    values: Optional[Dict[str, float]] = None,
    cells: Optional[List[Any]] = None,
    attribute: Optional[str] = None,
) -> Tuple[cKDTree, np.ndarray, List[str]]:
    """Build a spherical KDTree from H3 cell IDs and values.

    Args:
        h3_ids: List of H3 cell IDs (all at same resolution).
        values: Optional {h3_id: value} dict. If None, use cells+attribute.
        cells: Optional list of CellData objects (used if values is None).
        attribute: Attribute name on CellData (used if values is None).

    Returns:
        (kdtree, values_array, h3_ids) — tree, values in same order as h3_ids.
    """
    import h3 as _h3

    pts = np.zeros((len(h3_ids), 3), dtype=np.float64)
    vals = np.zeros(len(h3_ids), dtype=np.float64)

    if values is not None:
        for i, h in enumerate(h3_ids):
            latlng = _h3.cell_to_latlng(h)
            pts[i] = spherical_xyz(latlng[0], latlng[1])
            vals[i] = values.get(h, 0.0)
    elif cells is not None and attribute is not None:
        cell_map = {c.h3_id: c for c in cells}
        for i, h in enumerate(h3_ids):
            latlng = _h3.cell_to_latlng(h)
            pts[i] = spherical_xyz(latlng[0], latlng[1])
            c = cell_map.get(h)
            vals[i] = getattr(c, attribute, 0.0) if c else 0.0
    else:
        raise ValueError("Either values or (cells + attribute) must be provided")

    tree = cKDTree(pts)
    return tree, vals, h3_ids


# ======================================================================
# Vectorized field sampling (single KDTree query for all grid points)
# ======================================================================


def sample_field_vectorized(
    field,
    lats: np.ndarray,
    lons: np.ndarray,
) -> np.ndarray:
    """Sample a KDTree-backed field at ALL grid points in one query.

    Accepts either a ContinuousField or a FieldAccessor (unwrapes _base).
    Returns 2D array of shape (len(lats), len(lons)).

    Replaces 259K Python for-loop calls with a single numpy+KDTree query.
    """
    # Unwrap FieldAccessor if needed
    if hasattr(field, '_base') and hasattr(field, '_mutable'):
        # It's a FieldAccessor — prefer _base for the stable field
        inner = field._base if field._base is not None else getattr(field, '_mutable', None)
        if inner is None:
            return np.zeros((len(lats), len(lons)), dtype=np.float64)
        cf = inner
    else:
        cf = field

    # Access KDTree internals (both ContinuousField and MutableField have these)
    tree = cf._tree
    vals_arr = cf._values

    # Build meshgrid and convert to 3D
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    query_pts = spherical_xyz_vec(lat_grid, lon_grid)

    # Single KDTree query
    dists, idxs = tree.query(query_pts, k=3)

    # Vectorized IDW
    eps = 1e-15
    nearby = dists < eps
    w = 1.0 / (dists + eps)
    w_sum = w.sum(axis=1, keepdims=True)
    vals_3d = vals_arr[idxs]
    flat = np.sum(vals_3d * w, axis=1) / w_sum[:, 0]

    # Exact hits
    exact_mask = nearby.any(axis=1)
    if exact_mask.any():
        first_exact = nearby.argmax(axis=1)
        flat[exact_mask] = vals_3d[exact_mask, first_exact[exact_mask]]

    return flat.reshape(lat_grid.shape)


def sample_fields_vectorized(
    fields: Dict[str, Any],
    lats: np.ndarray,
    lons: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Sample MULTIPLE fields at once, sharing the meshgrid computation.

    Args:
        fields: {name: field_accessor} dict.
        lats, lons: 1D coordinate arrays.

    Returns:
        {name: 2D grid array} for each field.
    """
    result = {}
    for name, f in fields.items():
        result[name] = sample_field_vectorized(f, lats, lons)
    return result


# ======================================================================
# Scalar fallback (for non-KDTree fields)
# ======================================================================


def sample_field_scalar(
    field: Callable[[float, float], float],
    lats: np.ndarray,
    lons: np.ndarray,
) -> np.ndarray:
    """Sample a generic callable field point-by-point.

    Only used when field is NOT a KDTree-backed object.
    """
    grid = np.zeros((len(lats), len(lons)), dtype=np.float64)
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            grid[i, j] = field(float(lat), float(lon))
    return grid


# ======================================================================
# Flood-fill connected components on a lat/lon grid
# ======================================================================


def flood_fill_grid(
    mask: np.ndarray,
    min_cells: int = 1,
) -> List[List[Tuple[int, int]]]:
    """Find connected components in a 2D boolean mask (4-connectivity).

    Uses stack-based flood fill. Returns list of components, where each
    component is a list of (row, col) tuples.

    Args:
        mask: 2D boolean array. True = part of a component.
        min_cells: Minimum component size to include.

    Returns:
        List of components, sorted by size descending.
    """
    if mask.ndim != 2:
        raise ValueError(f"mask must be 2D, got {mask.ndim}D")
    if not mask.any():
        return []

    nrows, ncols = mask.shape
    cell_set = set(zip(*np.where(mask)))
    components: List[List[Tuple[int, int]]] = []

    while cell_set:
        seed = next(iter(cell_set))
        stack = [seed]
        comp: List[Tuple[int, int]] = []
        while stack:
            ci, cj = stack.pop()
            if (ci, cj) not in cell_set:
                continue
            cell_set.discard((ci, cj))
            comp.append((ci, cj))
            for di, dj in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                ni, nj = ci + di, cj + dj
                if 0 <= ni < nrows and 0 <= nj < ncols and (ni, nj) in cell_set:
                    stack.append((ni, nj))
        if len(comp) >= min_cells:
            components.append(comp)

    components.sort(key=len, reverse=True)
    return components


# ======================================================================
# Grid creation helpers
# ======================================================================


def make_latlon_grid(
    lat_step: float = 0.5,
    lon_step: float = 0.5,
    lat_range: Tuple[float, float] = (-90.0, 90.0),
    lon_range: Tuple[float, float] = (-180.0, 180.0),
) -> Tuple[np.ndarray, np.ndarray]:
    """Create regular lat/lon grid arrays."""
    lats = np.arange(lat_range[0], lat_range[1] + lat_step * 0.5, lat_step)
    lons = np.arange(lon_range[0], lon_range[1] + lon_step * 0.5, lon_step)
    return lats, lons
