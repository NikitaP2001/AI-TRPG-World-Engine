"""Layer 0 — Planet generator.

Tectonic plate elevation + multi-octave noise refinement + feature extraction.
Elevation emerges from plate motion (convergent uplift, divergent rifting),
with simplex noise refinement for sub-tectonic terrain texture.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h3
import numpy as np
import opensimplex

from .cell_model import CellData, GenerationParams
from .resources import generate_resources, default_resource_types, SpecialResourceInput, ResourceType
from .feature_store import FeatureStore, Feature
from .plate_tectonics import PlateTectonicsModel
from .geology import bedrock_from_geology, assign_bedrock_classes, geology_name
from .soil import assign_soil_profiles, update_organic_matter
from .climate import koppen_name
from .vegetation import assign_vegetation
from .climate import compute_climate
from .hydrology import compute_runoff_for_cells, compute_flow_accum_weighted
from .river_tracer import extract_rivers_continuous, ContinuousElevation, build_elevation_kdtree
from .lithology import generate_all_lithology
from .mineralogy import generate_all_ores, register_ore_type, OreFormation
from .contouring import (
    ContinuousField,
    sample_grid,
    threshold_polygons,
    vegetation_masks_to_polygons,
    classify_vegetation_grid,
    contour_bands,
)
# NOTE: generate_world() moved to simulation/generator.py


# ======================================================================
# Spherical noise — terrain refinement on top of tectonic base
# ======================================================================


def _noise_at(lat_deg: float, lon_deg: float, gen) -> float:
    """3D simplex noise at a lat/lon point using a preseeded generator."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    x = math.cos(lat_r) * math.cos(lon_r)
    y = math.sin(lat_r)
    z = math.cos(lat_r) * math.sin(lon_r)
    return gen.noise3(x, y, z)


def _multi_octave_noise(lat_deg: float, lon_deg: float, gens,
                        persistence: float = 0.5,
                        lacunarity: float = 2.5) -> float:
    """Multi-octave noise → [-1, 1] using preseeded generators (one per octave).
    
    gens: list of opensimplex.OpenSimplex objects, one per octave.
    Used as additive detail on top of tectonic baseline, not as
    the primary elevation source.
    """
    value = 0.0
    max_amp = 0.0
    amp = 1.0
    freq = 1.0
    for gen in gens:
        v = _noise_at(lat_deg * freq, lon_deg * freq, gen)
        value += v * amp
        max_amp += amp
        amp *= persistence
        freq *= lacunarity
    return value / max_amp


def _diamond_square_refine(
    h3_ids: List[str],
    coarse_elev: Dict[str, float],
    roughness: float,
    seed: int,
) -> Dict[str, float]:
    """Diamond-square fractal refinement on the H3 hierarchy.

    Algorithm:
      1. Group target cells by their parent at resolution-1 (coarse level).
      2. For each group, the *diamond* step interpolates base elevation
         from the parent cell and its six neighbours at the coarse level.
      3. The *square* step adds fractal displacement scaled by roughness.

    The roughness parameter controls the Hurst exponent H:
      H = 1 - roughness * 0.8   →  H=1 (smooth) when roughness=0
                                  →  H=0.2 (jagged) when roughness=1

    Displacement amplitude at each cell:  gauss(0, amp) where
      amp = roughness * 0.3 * H^0.5
    """
    if not h3_ids:
        return {}

    resolution = h3.get_resolution(h3_ids[0])
    H = 1.0 - roughness * 0.8          # Hurst exponent
    base_amp = max(0.01, roughness * 0.3 * (H ** 0.5))
    rng = random.Random(seed)
    result: Dict[str, float] = {}

    # Group target cells by parent at resolution-1
    groups: Dict[str, List[str]] = {}
    for h in h3_ids:
        parent = h3.cell_to_parent(h, resolution - 1) if resolution > 0 else h
        groups.setdefault(parent, []).append(h)

    for parent, children in groups.items():
        p_el = coarse_elev.get(parent, 0.0)

        # Get neighbour elevations at coarse resolution
        neighbours = h3.grid_ring(parent, 1) or []
        n_els = [coarse_elev.get(n, p_el) for n in neighbours]

        # Diamond step: blend parent with neighbour average
        avg_n = sum(n_els) / len(n_els) if n_els else p_el
        interp = p_el * 0.5 + avg_n * 0.5

        for child in children:
            rng.seed(str(child) + str(seed))
            displacement = rng.gauss(0.0, base_amp)
            result[child] = interp + displacement

    return result


def _compute_slope(
    h3_ids: List[str], elevation: Dict[str, float],
) -> Dict[str, Tuple[float, float]]:
    slope: Dict[str, Tuple[float, float]] = {}
    for h in h3_ids:
        el = elevation.get(h, 0.0)
        neighbours = h3.grid_ring(h, 1)
        if not neighbours:
            slope[h] = (0.0, 0.0)
            continue
        max_diff = 0.0
        steepest_dir = 0.0
        for i, nh in enumerate(neighbours):
            n_el = elevation.get(nh, el)
            diff = el - n_el
            if diff > max_diff:
                max_diff = diff
                steepest_dir = math.radians(i * 60.0)
        slope[h] = (min(1.0, max_diff * 2.0), steepest_dir)
    return slope


def _fill_depressions(
    h3_ids: List[str],
    elevation: Dict[str, float],
    ocean: set,
    max_iter: int = 10,
) -> Dict[str, float]:
    """Fill topographic sinks iteratively.

    A sink is a cell whose all 6 neighbours have higher elevation.
    Raise it to the minimum neighbour elevation + epsilon.
    Repeat until no sinks remain or max_iter reached.
    """
    filled = dict(elevation)
    eps = 1e-6

    for iteration in range(max_iter):
        changed = 0
        for h in h3_ids:
            if h in ocean:
                continue
            neighbours = h3.grid_ring(h, 1) or []
            if not neighbours:
                continue
            n_els = [filled.get(n, filled[h]) for n in neighbours]
            min_n = min(n_els)
            if filled[h] >= min_n:
                continue
            # Sink — raise to spill level
            filled[h] = min_n + eps
            changed += 1
        if changed == 0:
            break

    return filled


# ======================================================================
# Vector feature extraction helpers
# ======================================================================


def _cluster_cells(
    h3_ids: List[str],
    predicate,  # callable(h3_id) -> bool
) -> List[List[str]]:
    """Flood-fill cluster adjacent cells satisfying a predicate.

    Returns list of clusters, each a list of H3 cell IDs.
    """
    pool = set(h for h in h3_ids if predicate(h))
    clusters: List[List[str]] = []

    while pool:
        seed = next(iter(pool))
        queue = [seed]
        cluster: List[str] = []
        while queue:
            cur = queue.pop()
            if cur not in pool:
                continue
            pool.remove(cur)
            cluster.append(cur)
            for nh in h3.grid_ring(cur, 1) or []:
                if nh in pool:
                    queue.append(nh)
        if cluster:
            clusters.append(cluster)

    return clusters


def _cluster_to_polygon(cluster: List[str]) -> Optional[Any]:
    """Convert a cluster of H3 cell IDs to a Shapely Polygon.

    Uses concave hull for a tight boundary that follows the cell cluster
    without extending into empty space. Falls back to convex hull on error.
    Returns None if the cluster has fewer than 3 cells.
    """
    from shapely import concave_hull
    from shapely.geometry import MultiPoint
    if len(cluster) < 3:
        return None
    centroids = []
    for cid in cluster:
        latlng = h3.cell_to_latlng(cid)
        centroids.append((latlng[1], latlng[0]))  # (lon, lat)
    if len(centroids) < 3:
        return None
    multipoint = MultiPoint(centroids)
    try:
        hull = concave_hull(multipoint, ratio=0.05)
    except Exception:
        hull = multipoint.convex_hull
    return hull if hull.geom_type == "Polygon" else None


# River extraction moved to river_tracer.py — continuous gradient descent
# Old _extract_river_features and _smooth_polyline removed.


def _extract_resource_zone_polygons(
    cells: List[CellData],
    elev_field,  # ContinuousField for elevation, used for grid context
    flux_threshold: float = 0.5,
    shared_tree=None,
) -> List[Feature]:
    """Extract special resource zone Polygons via continuous contouring (P0.3).

    Builds a ContinuousField for mean special_resource_flux, samples at
    a grid, and extracts polygons where flux >= threshold using
    threshold_polygons().

    Args:
        shared_tree: Optional pre-built cKDTree from cell centroids.
                     If provided, skips rebuilding the tree.
    """
    if not cells or not cells[0].special_resource_flux:
        return []

    n_resources = len(cells[0].special_resource_flux)

    # Build flux field
    import h3
    from scipy.spatial import cKDTree
    from .contouring import ContinuousField, sample_grid, threshold_polygons

    flux_vals = []
    for c in cells:
        f = sum(c.special_resource_flux) / max(n_resources, 1)
        flux_vals.append(f)

    if shared_tree is not None:
        tree = shared_tree
    else:
        pts = []
        for c in cells:
            latlng = h3.cell_to_latlng(c.h3_id)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            pts.append([math.cos(lat_r)*math.cos(lon_r),
                         math.sin(lat_r),
                         math.cos(lat_r)*math.sin(lon_r)])
        tree = cKDTree(np.array(pts, dtype=np.float64))
    flux_field = ContinuousField(tree, np.array(flux_vals, dtype=np.float64))

    lats, lons, vgrid = sample_grid(flux_field, lat_step=0.25, lon_step=0.25)
    polys = threshold_polygons(lats, lons, vgrid, threshold=flux_threshold,
                                use_above=True, min_area=0.1, simplify_tol=0.05)

    features: List[Feature] = []
    for poly in polys:
        # Sample mean flux inside polygon
        centroid = poly.centroid
        mean_flux = flux_field(centroid.y, centroid.x)
        flux_pct = mean_flux * 100
        features.append(Feature(
            type="special_resource_zone",
            name=f"Resource Zone ({flux_pct:.0f}% flux)",
            geometry=poly,
            properties={"mean_flux": mean_flux, "resource_pct": flux_pct, "area_deg2": poly.area},
        ))
    return features


def _compute_flow_dir(
    h3_ids: List[str],
    elevation: Dict[str, float],
    ocean: set,
) -> Dict[str, int]:
    """Steepest descent D8 flow direction on hex grid.

    Returns {h3_id: neighbour_index} where 0-5 = hex neighbour,
    -1 = sink (no downhill neighbour), -2 = ocean terminus.
    """
    flow: Dict[str, int] = {}
    for h in h3_ids:
        if h in ocean:
            flow[h] = -2
            continue
        el = elevation.get(h, 0.0)
        neighbours = h3.grid_ring(h, 1) or []
        max_drop = 0.0
        best_idx = -1
        for i, nh in enumerate(neighbours):
            n_el = elevation.get(nh, el)
            drop = el - n_el
            if drop > max_drop:
                max_drop = drop
                best_idx = i
        flow[h] = best_idx
    return flow


def _compute_flow_accum(
    h3_ids: List[str],
    flow_dir: Dict[str, int],
) -> Dict[str, float]:
    """Iterative flow accumulation.

    For each cell, trace its flow path to terminus and count
    upstream cells. Uses a stack-based approach to handle cycles.
    """
    # Convert neighbour indices to actual cell IDs
    flow_to: Dict[str, Optional[str]] = {}
    for h in h3_ids:
        d = flow_dir.get(h, -1)
        if d >= 0:
            nh = (h3.grid_ring(h, 1) or [])[d] if d < 6 else None
            flow_to[h] = nh if nh and nh in h3_ids else None
        else:
            flow_to[h] = None

    # Topological order: process cells with no upstream first
    upstream: Dict[str, List[str]] = {h: [] for h in h3_ids}
    for h in h3_ids:
        target = flow_to.get(h)
        if target and target in upstream:
            upstream[target].append(h)

    # Stack-based accumulation
    acc: Dict[str, float] = {h: 1.0 for h in h3_ids}
    visited: set = set()
    for h in h3_ids:
        if h in visited:
            continue
        # Trace path
        stack = [h]
        path = []
        while stack:
            cur = stack[-1]
            if cur in visited:
                stack.pop()
                continue
            target = flow_to.get(cur)
            if target is None or target in visited:
                # Terminus reached
                visited.add(cur)
                stack.pop()
                for up in upstream.get(cur, []):
                    if up not in visited:
                        acc[cur] += acc.get(up, 0.0)
            elif target in path:
                # Cycle detected — break it
                visited.add(cur)
                stack.pop()
            else:
                if target not in visited:
                    stack.append(target)
                    path.append(cur)
                else:
                    visited.add(cur)
                    stack.pop()
                    for up in upstream.get(cur, []):
                        if up not in visited:
                            acc[cur] += acc.get(up, 0.0)

    return acc


def _assign_basins(
    h3_ids: List[str],
    flow_dir: Dict[str, int],
    ocean: set,
) -> Dict[str, int]:
    """Assign each cell to a drainage basin.

    Basin IDs are integers. Sinks and ocean cells each get
    unique negative IDs.
    """
    flow_to: Dict[str, Optional[str]] = {}
    for h in h3_ids:
        d = flow_dir.get(h, -1)
        if d >= 0:
            nh = (h3.grid_ring(h, 1) or [])[d] if d < 6 else None
            flow_to[h] = nh if nh and nh in h3_ids else None
        else:
            flow_to[h] = None

    basin: Dict[str, int] = {}
    basin_id = 1

    for h in h3_ids:
        if h in basin:
            continue
        if h in ocean:
            basin[h] = -1
            continue

        # Trace to outlet
        path = [h]
        cur = h
        while True:
            nxt = flow_to.get(cur)
            if nxt is None or nxt in ocean:
                # Terminus — assign new basin
                bid = basin_id
                basin_id += 1
                for p in path:
                    basin[p] = bid
                break
            if nxt in basin:
                # Already assigned
                bid = basin[nxt]
                for p in path:
                    basin[p] = bid
                break
            if nxt in path:
                # Cycle — assign unique basin
                bid = basin_id
                basin_id += 1
                for p in path:
                    basin[p] = bid
                break
            path.append(nxt)
            cur = nxt

    return basin


def _compute_hydrology(
    h3_ids: List[str], elevation: Dict[str, float],
    ocean: set,
) -> Tuple[Dict[str, bool], Dict[str, int], Dict[str, float], Dict[str, int]]:
    """Full D8 hydrology pipeline: depression fill → flow dir → accumulation → basins."""
    # 1. Fill depressions (only on land)
    filled = _fill_depressions(h3_ids, elevation, ocean)

    # 2. Compute flow direction on filled elevation
    flow_dir = _compute_flow_dir(h3_ids, filled, ocean)

    # 3. Flow accumulation
    flow_acc = _compute_flow_accum(h3_ids, flow_dir)

    # 4. River flagging
    threshold = max(2, len(h3_ids) // 4000)
    river = {h: flow_acc.get(h, 0.0) >= threshold and h not in ocean for h in h3_ids}

    # 5. Drainage basins
    basin = _assign_basins(h3_ids, flow_dir, ocean)

    return river, flow_dir, flow_acc, basin


# NOTE: generate_world() moved to simulation/generator.py
# Use:  from simulation.generator import generate_world


def _register_geology_features(
    feature_store, cells: List[CellData], ocean: set,
    shared_tree=None,
) -> None:
    """Register geology regions using continuous contouring (P0.2).

    For each geological_type, builds a binary ContinuousField
    (1.0 for this type, 0.0 for others) and extracts polygons
    via threshold_polygons().

    Args:
        shared_tree: Optional pre-built cKDTree from cell centroids.
                     If provided, skips rebuilding the tree.
    """
    import h3
    from scipy.spatial import cKDTree
    from .contouring import ContinuousField, sample_grid, threshold_polygons

    # Build shared KDTree from all cell centroids (or reuse pre-built)
    if shared_tree is not None:
        tree = shared_tree
    else:
        pts = []
        for c in cells:
            latlng = h3.cell_to_latlng(c.h3_id)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            pts.append([math.cos(lat_r)*math.cos(lon_r),
                         math.sin(lat_r),
                         math.cos(lat_r)*math.sin(lon_r)])
        tree = cKDTree(np.array(pts, dtype=np.float64))

    geo_types = sorted(set(c.geological_type for c in cells))
    for gtype in geo_types:
        # Build one-hot field for this geological type
        vals = np.array([1.0 if c.geological_type == gtype else 0.0 for c in cells],
                        dtype=np.float64)
        field = ContinuousField(tree, vals)
        lats, lons, vgrid = sample_grid(field, lat_step=0.5, lon_step=0.5)
        polys = threshold_polygons(lats, lons, vgrid, threshold=0.5,
                                    use_above=True, min_area=0.5, simplify_tol=0.1)
        gname = geology_name(gtype)
        for poly in polys:
            feature_store.add_feature(Feature(
                type="geology_region",
                name=gname,
                geometry=poly,
                properties={"geological_type": gtype, "geology_name": gname},
            ))


def _bedrock_from_geology(gtype: int) -> str:
    """Map geological_type to bedrock mineral profile class."""
    return bedrock_from_geology(gtype)


def save_cells_parquet(cells: List[CellData], path: Path) -> None:
    import pyarrow as pa, pyarrow.parquet as pq
    t = pa.table({k: [getattr(c, k) for c in cells] for k in [
        "h3_id", "resolution", "elevation_mean", "geological_type",
        "temperature", "precipitation", "soil_fertility",
        "hazard_level", "tectonic_stress", "climate_class",
        "plate_id", "boundary_type", "soil_depth",
        "vegetation_cover", "runoff_ratio"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, path)
    print(f"[Layer 0] saved {len(cells)} cells to {path}")


def load_cells_parquet(path: Path) -> List[CellData]:
    import pyarrow.parquet as pq
    t = pq.read_table(path)
    cols = set(t.column_names)
    default_int = -1
    default_str = ""
    return [CellData(
        h3_id=str(t["h3_id"][i].as_py()),
        resolution=t["resolution"][i].as_py(),
        elevation_mean=float(t["elevation_mean"][i].as_py()),
        geological_type=t["geological_type"][i].as_py(),
        temperature=float(t["temperature"][i].as_py()),
        precipitation=float(t["precipitation"][i].as_py()),
        soil_fertility=float(t["soil_fertility"][i].as_py()),
        hazard_level=float(t["hazard_level"][i].as_py()),
        tectonic_stress=float(t["tectonic_stress"][i].as_py()),
        climate_class=str(t["climate_class"][i].as_py()),
        plate_id=int(t["plate_id"][i].as_py()) if "plate_id" in cols else default_int,
        boundary_type=str(t["boundary_type"][i].as_py()) if "boundary_type" in cols else default_str,
    ) for i in range(len(t))]
