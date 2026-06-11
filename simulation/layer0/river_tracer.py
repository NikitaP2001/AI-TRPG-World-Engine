"""Layer 0 — Continuous River Tracer.

Traces rivers by following steepest descent of a continuous elevation
function at sub-cell resolution (~100m precision). Not cell-to-cell paths.
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

import numpy as np
from shapely.geometry import LineString

from .feature_store import Feature


# ======================================================================
# Fast elevation interpolation using KDTree
# ======================================================================


def build_elevation_kdtree(
    h3_ids: List[str],
    elevation_map: dict,
) -> Tuple:
    """Build a KDTree from cell centroids for fast elevation lookup.

    Returns (kdtree, elevations_array) where kdtree can query
    elevation at any (lat, lon) via inverse-distance weighting.
    """
    from scipy.spatial import cKDTree

    points = []
    elevations = []
    import h3
    for h in h3_ids:
        latlng = h3.cell_to_latlng(h)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        x = math.cos(lat_r) * math.cos(lon_r)
        y = math.sin(lat_r)
        z = math.cos(lat_r) * math.sin(lon_r)
        points.append((x, y, z))
        elevations.append(elevation_map.get(h, 0.0))
    
    tree = cKDTree(np.array(points, dtype=np.float64))
    return tree, np.array(elevations, dtype=np.float64)


class ContinuousElevation:
    """Continuous elevation function evaluable at any (lat, lon).

    Uses KDTree-based IDW interpolation from cell centroids,
    plus optional noise refinement.
    """

    def __init__(
        self,
        tree,
        elevations: np.ndarray,
        noise_func: Optional[Callable[[float, float], float]] = None,
    ):
        self._tree = tree
        self._elevations = elevations
        self._noise = noise_func

    def __call__(self, lat: float, lon: float) -> float:
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        px = math.cos(lat_r) * math.cos(lon_r)
        py = math.sin(lat_r)
        pz = math.cos(lat_r) * math.sin(lon_r)

        # Query 3 nearest neighbors via KDTree (O(log N))
        dists, idxs = self._tree.query([px, py, pz], k=3)
        if np.any(dists < 1e-15):
            exact = self._elevations[idxs[0]]
        else:
            w = 1.0 / (dists + 1e-15)
            exact = float(np.average(self._elevations[idxs], weights=w))

        if self._noise is not None:
            exact += self._noise(lat, lon)
        return exact


def _central_diff(
    f: Callable[[float, float], float],
    lat: float,
    lon: float,
    eps: float = 0.0005,  # ≈ 55m at equator
) -> Tuple[float, float]:
    """Numerical gradient at (lat, lon) via central differences.

    Returns (df_dlat, df_dlon) — partial derivatives in metres⁻¹ scale.
    """
    lat_c = max(-89.9, min(89.9, lat))
    df_dlat = (f(lat_c + eps, lon) - f(lat_c - eps, lon)) / (2.0 * eps)
    df_dlon = (f(lat_c, lon + eps) - f(lat_c, lon - eps)) / (2.0 * eps)
    return df_dlat, df_dlon


def _step_descent(
    lat: float,
    lon: float,
    grad_lat: float,
    grad_lon: float,
    step_deg: float = 0.001,  # ≈ 111m
) -> Tuple[float, float]:
    """Step along steepest descent direction.

    Steps ~step_deg in great-circle distance along negative gradient.
    """
    g = np.array([grad_lat, grad_lon])
    norm = np.linalg.norm(g)
    if norm < 1e-15:
        return lat, lon

    dlat = -grad_lat / norm * step_deg
    dlon = -grad_lon / norm * step_deg

    cos_lat = max(0.01, math.cos(math.radians(lat)))
    dlon_adj = dlon / cos_lat

    new_lat = max(-90.0, min(90.0, lat + dlat))
    new_lon = lon + dlon_adj

    while new_lon > 180.0:
        new_lon -= 360.0
    while new_lon < -180.0:
        new_lon += 360.0

    return new_lat, new_lon


# ======================================================================
# River tracing
# ======================================================================


def trace_river(
    start_lat: float,
    start_lon: float,
    elevation: Callable[[float, float], float],
    is_ocean: Callable[[float, float], bool],
    step_deg: float = 0.001,   # ~111m
    max_steps: int = 5000,
) -> Optional[LineString]:
    """Trace a river from headwater to coast via gradient descent.

    The river follows the steepest descent path of the continuous
    elevation function. Each vertex is ~step_deg apart (~111m).

    Args:
        start_lat, start_lon: Seed point (degrees).
        elevation: (lat, lon) → elevation (continuous).
        is_ocean: (lat, lon) → True if ocean.
        step_deg: Step size (~111m at equator, adjusts for latitude).
        max_steps: Max iterations (~5000 steps ≈ 550km max river).

    Returns:
        Shapely LineString (lon, lat order) or None if too short.
    """
    lats, lons = [start_lat], [start_lon]
    lat, lon = start_lat, start_lon
    prev_el = elevation(lat, lon)

    for _ in range(max_steps):
        g_lat, g_lon = _central_diff(elevation, lat, lon)
        new_lat, new_lon = _step_descent(lat, lon, g_lat, g_lon, step_deg)

        # Stop BEFORE entering ocean
        if is_ocean(new_lat, new_lon):
            break

        new_el = elevation(new_lat, new_lon)
        if new_el >= prev_el:
            break  # Local minimum or flat area

        # Loop detection: check last 20 points for near-duplicates (actual spirals)
        looped = False
        if len(lats) > 10:
            for j in range(max(0, len(lats) - 20), len(lats) - 1):
                d = math.sqrt(
                    (new_lat - lats[j]) ** 2 +
                    ((new_lon - lons[j]) * math.cos(math.radians(lat))) ** 2
                )
                if d < step_deg * 0.5:  # Within half a step = stuck in place
                    looped = True
                    break
        if looped:
            break

        lats.append(new_lat)
        lons.append(new_lon)
        lat, lon = new_lat, new_lon
        prev_el = new_el

    if len(lats) < 2:
        return None

    # Return LineString in (lon, lat) GeoJSON order
    coords = [(lons[i], lats[i]) for i in range(len(lats))]
    return LineString(coords)


# ======================================================================
# Batch extraction
# ======================================================================


def extract_rivers_continuous(
    headwaters: List[Tuple[float, float]],  # (lat, lon) seeds
    elevation: Callable[[float, float], float],
    is_ocean: Callable[[float, float], bool],
    flow_accum_map: Optional[dict] = None,
    cell_area: float = 1.0,
) -> List[Feature]:
    """Extract rivers from headwater seeds via continuous gradient descent.

    River width scales with discharge Q = flow_accum * effective_precip.
    width = k * Q^0.5 (Leopold & Maddock, 1953).

    Args:
        headwaters: List of (lat, lon) seed points.
        elevation: Continuous elevation function.
        is_ocean: Continuous ocean test function.
        flow_accum_map: Optional dict[h3_id] → weighted flow accumulation.
                        Used for physically-based width and discharge (P2.2, P2.3).
        cell_area: Area of one H3 cell in world units, for discharge normalization.

    Returns:
        List of river Feature objects.
    """
    import h3
    features: List[Feature] = []
    for i, (hlat, hlon) in enumerate(headwaters):
        try:
            line = trace_river(hlat, hlon, elevation, is_ocean)
        except Exception:
            continue
        if line is None or len(line.coords) < 2:
            continue

        n = len(line.coords)

        # Physical flow accumulation (P2.3)
        if flow_accum_map:
            # Look up the cell at the river midpoint
            mid_idx = n // 2
            mid_lat, mid_lon = line.coords[mid_idx][1], line.coords[mid_idx][0]
            mid_cell = h3.latlng_to_cell(mid_lat, mid_lon, 2)
            flow_acc_val = flow_accum_map.get(mid_cell, float(n * 0.5))
        else:
            flow_acc_val = float(n * 0.5)

        # Discharge proxy: flow_accum * effective_precip / cell_area (P2.3)
        discharge = flow_acc_val / max(1.0, cell_area)

        # River width from discharge (P2.2): width = k * Q^0.5
        # k ≈ 0.02 calibrated for typical world scale (Q in world units)
        k_width = 0.02
        width_km = k_width * math.sqrt(max(0.01, discharge))
        river_type = "Major" if width_km > 0.5 else "Stream"
        features.append(Feature(
            type="river",
            name=f"{river_type} River #{len(features) + 1}",
            geometry=line,
            properties={
                "width_km": float(width_km),
                "navigable": bool(width_km > 0.5),
                "flow_accumulation": float(flow_acc_val),
                "discharge": float(discharge),
                "segments": int(n),
                "river_type": river_type,
            },
        ))
    return features
