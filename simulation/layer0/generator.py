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
from .resources import generate_resources, default_resource_types, SpecialResourceInput
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
# Layer 1 — causal simulation engine
from ..layer1.engine import SimEngine
from ..layer1.fields import FieldRegistry
from ..layer1.features.groundwater import Groundwater
from ..layer1.features.biomes import BiomeRegion, sample_biomes, classify_biome, BIOME_REGISTRY


# ======================================================================
# Spherical noise — terrain refinement on top of tectonic base
# ======================================================================


def _noise_at(lat_deg: float, lon_deg: float, seed: int) -> float:
    """3D simplex noise at a lat/lon point."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    x = math.cos(lat_r) * math.cos(lon_r)
    y = math.sin(lat_r)
    z = math.cos(lat_r) * math.sin(lon_r)
    opensimplex.seed(seed)
    return opensimplex.noise3(x, y, z)


def _multi_octave_noise(lat_deg: float, lon_deg: float, seed: int = 42,
                        octaves: int = 4, persistence: float = 0.5,
                        lacunarity: float = 2.5) -> float:
    """Multi-octave noise → [-1, 1] for terrain refinement.
    
    Used as additive detail on top of tectonic baseline, not as
    the primary elevation source.
    """
    value = 0.0
    max_amp = 0.0
    amp = 1.0
    freq = 1.0
    for o in range(octaves):
        v = _noise_at(lat_deg * freq, lon_deg * freq, seed + o)
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
) -> List[Feature]:
    """Extract special resource zone Polygons via continuous contouring (P0.3).

    Builds a ContinuousField for mean special_resource_flux, samples at
    a grid, and extracts polygons where flux >= threshold using
    threshold_polygons().
    """
    if not cells or not cells[0].special_resource_flux:
        return []

    n_resources = len(cells[0].special_resource_flux)

    # Build flux field
    import h3
    from scipy.spatial import cKDTree
    from .contouring import ContinuousField, sample_grid, threshold_polygons

    pts = []
    flux_vals = []
    for c in cells:
        latlng = h3.cell_to_latlng(c.h3_id)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        pts.append([math.cos(lat_r)*math.cos(lon_r),
                     math.sin(lat_r),
                     math.cos(lat_r)*math.sin(lon_r)])
        f = sum(c.special_resource_flux) / max(n_resources, 1)
        flux_vals.append(f)

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


def generate_world(
    params: Optional[GenerationParams] = None,
    feature_store: Optional[FeatureStore] = None,
) -> Tuple[List[CellData], FeatureStore, Dict[str, float]]:
    """Run full Layer 0 generation pipeline.

    Pipeline order follows causal dependencies:
      1. Plate tectonics → elevation + geology
      2. Noise refinement (sub-tectonic detail)
      3. Slope computation
      4. Climate (temp, precip with orographic)
      5. Weathering + soil formation
      6. Vegetation potential
      7. Runoff hydrology
      8. Special resources
      9. CellData construction
      10. Feature extraction (rivers, terrain cover, resources)

    Args:
        params: WM-authored parameters.
        feature_store: Existing store to populate, or None to create one.

    Returns:
        (cells, feature_store, flow_accumulation)
    """
    if params is None:
        params = GenerationParams()
    params.derive()
    resolution = params.h3_resolution
    print(f"[Layer 0] resolution={resolution}, top_level_cells={params.top_level_cell_count}")
    _rng = random.Random(params.seed)

    all_ids: List[str] = []
    res0_cells = list(h3.get_res0_cells())
    for r0 in res0_cells:
        all_ids.extend(h3.cell_to_children(r0, resolution))
    _rng.shuffle(all_ids)
    if params.world_extent < 1.0:
        all_ids = all_ids[:int(len(all_ids) * params.world_extent)]
    print(f"[Layer 0] cell_count={len(all_ids)}")

    # ══════════════════════════════════════════════════════════════
    # Stage 1 — Plate Tectonics
    # ══════════════════════════════════════════════════════════════
    print("  [Tectonics] generating plates...")
    tectonics = PlateTectonicsModel(
        all_ids,
        num_plates=params.num_plates,
        seed=params.seed,
        tectonic_activity=params.tectonic_activity,
    )
    # Compute tectonic elevation
    elevation = tectonics.compute_elevation()
    # Compute geology from plate context
    geo_type = tectonics.compute_geology()
    # Sea level is 0.0 (oceanic crust < 0, continental crust > 0)
    sea_level = 0.0

    # Ocean/land from geology (not elevation-threshold)
    ocean_cells = {h for h in all_ids if geo_type.get(h, 0) == 0}
    land_cells = {h for h in all_ids if geo_type.get(h, 0) != 0}

    # Add noise refinement on top of tectonic base (20% amplitude)
    print("  [Refinement] simplex noise detail...")
    for h in all_ids:
        latlng = h3.cell_to_latlng(h)
        noise = _multi_octave_noise(latlng[0], latlng[1],
                                    seed=params.seed + 1000,
                                    octaves=4, persistence=0.5) * 0.2
        elevation[h] = max(-0.5, min(1.5, elevation[h] + noise))

    # Land/ocean from tectonic geology
    ocean_set = ocean_cells
    land_set = land_cells

    # Continent detection via elevation isoline (P0.4)
    # Build temporary ContinuousField, extract elevation=0 isoline
    from .contouring import ContinuousField, sample_grid, threshold_polygons
    from scipy.spatial import cKDTree
    import numpy as np
    elev_pts = []
    elev_vals = []
    for h in all_ids:
        latlng = h3.cell_to_latlng(h)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        elev_pts.append([math.cos(lat_r)*math.cos(lon_r),
                          math.sin(lat_r),
                          math.cos(lat_r)*math.sin(lon_r)])
        elev_vals.append(elevation.get(h, 0.0))
    tmp_tree = cKDTree(np.array(elev_pts, dtype=np.float64))
    tmp_elev = ContinuousField(tmp_tree, np.array(elev_vals, dtype=np.float64))
    cont_lats, cont_lons, cont_vals = sample_grid(tmp_elev, lat_step=0.25, lon_step=0.25)
    land_polys = threshold_polygons(cont_lats, cont_lons, cont_vals,
                                     threshold=0.0, use_above=True,
                                     min_area=0.1, simplify_tol=0.05)
    land_polys.sort(key=lambda p: p.area, reverse=True)

    res = params.h3_resolution
    continent_cells = set()
    island_cells = set()
    # Use KDTree to efficiently find H3 cells inside each polygon
    for i, poly in enumerate(land_polys):
        if poly.area < 0.01:
            continue
        minx, miny, maxx, maxy = poly.bounds
        # Find candidate H3 cells within polygon bounding box
        poly_cells = set()
        for h in all_ids:
            latlng = h3.cell_to_latlng(h)
            if (minx <= latlng[1] <= maxx and miny <= latlng[0] <= maxy
                    and elevation.get(h, 0.0) > 0.0):
                pt = __import__('shapely').geometry.Point(latlng[1], latlng[0])
                if poly.contains(pt):
                    poly_cells.add(h)
        if i == 0:
            continent_cells = poly_cells
        else:
            island_cells |= poly_cells

    # Fallback: if contouring produced no cells, use original clustering
    if not continent_cells:
        all_land_h3 = [h for h in all_ids if elevation.get(h, 0.0) > 0.0]
        land_clusters = _cluster_cells(all_land_h3, lambda h: True)
        land_clusters.sort(key=len, reverse=True)
        if land_clusters and len(land_clusters[0]) > max(3, len(all_land_h3) * 0.1):
            continent_cells = set(land_clusters[0])
        else:
            continent_cells = set(all_land_h3)
        island_cells = set()
        for cl in land_clusters[1:]:
            island_cells.update(cl)

    # Continental shelf: oceanic cells adjacent to land
    shelf_cells: set = set()
    for h in ocean_set:
        if elevation.get(h, 0.0) > -0.1:  # Shallow enough
            for nh in (h3.grid_ring(h, 1) or []):
                if nh in continent_cells or nh in island_cells:
                    shelf_cells.add(h)
                    break

    slope = _compute_slope(all_ids, elevation)

    # ══════════════════════════════════════════════════════════════
    # Stage 2 — Climate (using dedicated climate module)
    # ══════════════════════════════════════════════════════════════
    print("  [Climate] computing wind, temperature, precipitation...")
    temp, temp_range, precip, precip_seas, climate_class, wind = compute_climate(
        all_ids, elevation, ocean_set,
        seed=params.seed + 200,
        axial_tilt=params.axial_tilt,
    )

    # ── Ocean currents — modify coastal climate (P2.6) ─────────
    if params.ocean_currents_enabled:
        from .ocean_currents import (
            compute_ocean_currents, advect_sst, apply_coastal_climate,
            OceanCurrentParams,
        )
        oc_params = OceanCurrentParams(
            enabled=True,
            wind_drag_coefficient=params.ocean_wind_drag,
            ekman_turn_angle_deg=params.ocean_ekman_angle,
            coastal_influence_radius_deg=params.ocean_coastal_radius,
        )
        ocean_currents = compute_ocean_currents(
            all_ids, ocean_set, wind, oc_params,
        )
        base_sst = {h: temp.get(h, 0.5) for h in all_ids if h in ocean_set}
        sst_anomaly = advect_sst(
            all_ids, ocean_set, ocean_currents, base_sst, oc_params,
        )
        # Apply coastal climate on ocean_set ± neighbours (cells not yet built)
        land_ids = [h for h in all_ids if h not in ocean_set]
        temp, precip = apply_coastal_climate(
            land_ids, ocean_set, sst_anomaly, temp, precip, oc_params,
        )
        n_anom = sum(1 for v in sst_anomaly.values() if abs(v) > 0.01)
        print(f"  [Ocean] applied currents ({n_anom} cells with SST anomaly)")

    # ══════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════
    # Stage 4 — Build CellData (basic fields: tectonics, climate)
    # ══════════════════════════════════════════════════════════════
    cells: List[CellData] = []
    elev_var: Dict[str, float] = {}
    for h in all_ids:
        neighbours = h3.grid_ring(h, 1) or []
        n_els = [elevation.get(n, elevation.get(h, 0.0)) for n in neighbours]
        if n_els:
            elev_var[h] = float(np.var([elevation.get(h, 0.0)] + n_els))
        else:
            elev_var[h] = 0.0

    for h in all_ids:
        is_ocean = h in ocean_set
        el = elevation.get(h, 0.0)
        gtype = geo_type.get(h, 0)
        is_shelf = h in shelf_cells
        near_water = any(nh in ocean_set for nh in (h3.grid_ring(h, 1) or []))

        wt = 0.0 if is_ocean else (
            0.5 * (1.0 - precip.get(h, 0.5)) * (0.3 if near_water else 1.0)
        )

        cells.append(CellData(
            h3_id=h, resolution=resolution,
            elevation_mean=el, elevation_variance=elev_var.get(h, 0.0),
            slope=slope.get(h, (0.0, 0.0)),
            geological_type=gtype,
            plate_id=tectonics.assignment.get(h, -1),
            boundary_type=tectonics.boundary_type.get(h, "intraplate"),
            distance_to_boundary=tectonics.distance_to_boundary.get(h, -1.0),
            bedrock_class=_bedrock_from_geology(gtype),
            # Soil/vegetation — will be populated by later stages
            soil_depth=0.0, organic_matter=0.0,
            clay_content=0.0, sand_content=0.0, silt_content=0.0,
            soil_ph=7.0, cation_exchange=5.0,
            vegetation_cover="barren",
            water_table_depth=wt,
            runoff_ratio=0.5, effective_precip=0.0,
            temperature=temp.get(h, 0.5),
            temp_seasonal_range=temp_range.get(h, 0.2),
            precipitation=precip.get(h, 0.5),
            precip_seasonality=precip_seas.get(h, 0.3),
            climate_class=climate_class.get(h, ""),
            prevailing_wind=wind.get(h, (0.0, 0.0)),
            # Deep geology
            crustal_age_myr=tectonics.crustal_age_myr.get(h, 100.0),
            crustal_thickness_km=tectonics.crustal_thickness_km.get(h, 35.0),
            thermal_gradient=tectonics.thermal_gradient.get(h, 25.0),
            soil_fertility=0.02 if is_ocean else 0.1,  # placeholder, overwritten by soil model
            hazard_level=0.0 if is_ocean else params.tectonic_activity * (1.0 - max(0.0, el)) * 0.3,
            special_resource_flux=[],  # placeholder, populated below
            tectonic_stress=abs(el) * params.tectonic_activity,
            anchor_feature_ids=[],
            feature_ids=[],
        ))
    print(f"  Generated {len(cells)} cells")

    # Assign bedrock classes from geology
    assign_bedrock_classes(cells)

    # ══════════════════════════════════════════════════════════════
    # Stage 2b — Lithology (subsurface rock columns)
    # ══════════════════════════════════════════════════════════════
    print("  [Lithology] generating subsurface rock columns...")
    lithology_map = generate_all_lithology(cells, seed=params.seed)
    print(f"  [Lithology] {len(lithology_map)} columns generated")

    # ══════════════════════════════════════════════════════════════
    # Stage 3 — Weathering & Soil Formation
    # ══════════════════════════════════════════════════════════════
    print("  [Soil] computing weathering and soil profiles...")
    assign_soil_profiles(
        cells,
        temperature=temp,
        precipitation=precip,
        ocean_set=ocean_set,
        shelf_set=shelf_cells,
        time_factor=1.0,
    )

    # ══════════════════════════════════════════════════════════════
    # Stage 4 — Vegetation (with soil feedback iterations)
    # ══════════════════════════════════════════════════════════════
    print("  [Vegetation] computing vegetation potential...")
    assign_vegetation(cells, ocean_set, iterations=3)

    # ══════════════════════════════════════════════════════════════
    # Stage 5 — Special resources via Gray-Scott
    # ══════════════════════════════════════════════════════════════
    resource_types = default_resource_types()
    stress_map = {h: abs(elevation.get(h, 0.0)) * params.tectonic_activity
                  for h in all_ids}
    gtype_map = {h: geo_type.get(h, 0) for h in all_ids}
    res_input = SpecialResourceInput(
        h3_ids=all_ids, tectonic_stress=stress_map,
        elevation=elevation, geological_type=gtype_map,
    )
    resource_flux = generate_resources(res_input, resource_types, params.seed + 555)

    # Populate resource flux and stress on cells
    for cell in cells:
        h = cell.h3_id
        cell.special_resource_flux = resource_flux.get(h, [])
        cell.tectonic_stress = stress_map.get(h, 0.0)
    print(f"  {len(resource_flux)} cells with resource flux")

    # ══════════════════════════════════════════════════════════════
    # Stage 6a — Runoff computation
    # ══════════════════════════════════════════════════════════════
    print("  [Hydrology] computing runoff ratios...")
    compute_runoff_for_cells(cells, precip, ocean_set, day_of_year=172.0)

    # ══════════════════════════════════════════════════════════════
    # Stage 6b — Weighted Hydrology (D8 with effective_precip)
    # ══════════════════════════════════════════════════════════════
    # Compute flow direction from elevation (same as before)
    river_flag, flow_dir, flow_acc, basin = _compute_hydrology(
        all_ids, elevation, ocean_set
    )
    # Recompute flow accumulation using effective_precip weights
    effective_weights = {c.h3_id: c.effective_precip for c in cells}
    flow_acc = compute_flow_accum_weighted(all_ids, flow_dir, effective_weights)
    # Flag rivers from weighted accumulation
    threshold = max(0.5, len(all_ids) / 4000 * 0.5)
    for h in all_ids:
        river_flag[h] = flow_acc.get(h, 0.0) >= threshold and h not in ocean_set

    # ══════════════════════════════════════════════════════════════
    # Stage 7 — Create feature store and extract features
    # ══════════════════════════════════════════════════════════════
    if feature_store is None:
        feature_store = FeatureStore()

    def _apply_layer_effects(feat: Feature, cells: List[CellData]) -> None:
        """Apply feature's layer_effects to all intersecting cells."""
        if not feat.layer_effects or feat.geometry is None:
            return
        # For LineString features (rivers), use distance threshold
        # since cell centroids rarely fall exactly on the line.
        is_line = feat.geometry.geom_type == "LineString"
        threshold_deg = 0.5  # ~55km, half a cell at H3 res 2
        for cell in cells:
            latlng = h3.cell_to_latlng(cell.h3_id)
            from shapely.geometry import Point
            pt = Point(latlng[1], latlng[0])
            if is_line:
                hit = feat.geometry.distance(pt) < threshold_deg
            else:
                hit = feat.geometry.contains(pt)
            if not hit:
                continue
                le = feat.layer_effects
                if "elevation_offset" in le:
                    cell.elevation_mean += le["elevation_offset"]
                if "hazard_modifier" in le:
                    cell.hazard_level *= le["hazard_modifier"]
                if "soil_fertility_modifier" in le:
                    cell.soil_fertility *= le["soil_fertility_modifier"]
                if "water_table_modifier" in le:
                    cell.water_table_depth *= le["water_table_modifier"]
                cell.elevation_mean = max(0.0, min(1.0, cell.elevation_mean))
                cell.hazard_level = max(0.0, min(1.0, cell.hazard_level))
                cell.soil_fertility = max(0.0, min(1.0, cell.soil_fertility))

    # Island features — continuous contouring (P0.1)
    if island_cells:
        # Build ContinuousField from all cells for elevation
        isle_field = ContinuousField.from_cells(cells, "elevation_mean")
        isle_lats, isle_lons, isle_vals = sample_grid(isle_field, lat_step=0.25, lon_step=0.25)
        # Extract all land polygons (elevation > 0)
        land_polys = threshold_polygons(isle_lats, isle_lons, isle_vals,
                                         threshold=0.0, use_above=True,
                                         min_area=0.01, simplify_tol=0.05)
        # Largest polygon is the continent; skip it (registered elsewhere)
        land_polys.sort(key=lambda p: p.area, reverse=True)
        land_polys = land_polys[1:]  # remove continent
        island_count = 0
        for poly in land_polys:
            if poly.area < 0.01:
                continue
            island_count += 1
            feat = Feature(
                type="island",
                name=f"Island #{island_count}",
                geometry=poly,
                properties={"area_deg2": poly.area},
                layer_effects={"geological_type_override": 1},
            )
            feature_store.add_feature(feat)
            _apply_layer_effects(feat, cells)
        if island_count > 0:
            print(f"  Extracted {island_count} island(s) via contouring")

    # Mountain ranges — continuous contouring (will be done after elev_field is ready)
    # (actual extraction happens after the continuous elevation field is set up)

    # ── Continuous rivers via gradient descent ────────────────
    print("  [Rivers] tracing continuous gradient-descent paths...")

    # Build KDTree for fast elevation lookup
    elev_map = {cell.h3_id: cell.elevation_mean for cell in cells}
    kdtree, kd_elevs = build_elevation_kdtree(all_ids, elev_map)
    noise_seed = params.seed + 1000
    opensimplex.seed(noise_seed)
    
    def _continuous_elevation(lat: float, lon: float) -> float:
        """Continuous elevation at ANY (lat, lon) via KDTree-based IDW.
        Fast (O(log N)) compared to brute-force numpy."""
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        px = math.cos(lat_r) * math.cos(lon_r)
        py = math.sin(lat_r)
        pz = math.cos(lat_r) * math.sin(lon_r)
        dists, idxs = kdtree.query([px, py, pz], k=3)
        if np.any(dists < 1e-15):
            elev = float(kd_elevs[idxs[0]])
        else:
            w = 1.0 / (dists + 1e-15)
            elev = float(np.average(kd_elevs[idxs], weights=w))
        n = opensimplex.noise3(px, py, pz) * 0.1  # 10% noise amplitude
        return elev + n

    _ocean_func = lambda lat, lon: _continuous_elevation(lat, lon) < 0.0

    # Headwater seeds: cells with local max flow accumulation
    min_accum = max(0.5, len(all_ids) / 4000 * 0.3)
    headwaters: List[Tuple[float, float]] = []
    for cell in cells:
        h = cell.h3_id
        if h in ocean_set:
            continue
        if flow_acc.get(h, 0.0) < min_accum:
            continue
        neighbours = h3.grid_ring(h, 1) or []
        max_nb = 0.0
        for nh in neighbours:
            if nh not in ocean_set:
                max_nb = max(max_nb, flow_acc.get(nh, 0.0))
        if flow_acc.get(h, 0.0) >= max_nb:
            latlng = h3.cell_to_latlng(h)
            headwaters.append((latlng[0], latlng[1]))

    # cell_area for discharge normalisation (P2.3)
    ca = params.cell_area
    rivers = extract_rivers_continuous(
        headwaters, _continuous_elevation, _ocean_func,
        flow_accum_map=flow_acc, cell_area=ca,
    )
    for feat in rivers:
        feature_store.add_feature(feat)
        _apply_layer_effects(feat, cells)
    print(f"  Extracted {len(rivers)} river(s)")

    # ── Continuous terrain cover via vegetation potential grid ──
    print("  [Terrain] sampling vegetation potential grid...")
    soil_field = ContinuousField.from_cells(cells, "soil_fertility")
    temp_field = ContinuousField.from_cells(cells, "temperature")
    precip_field = ContinuousField.from_cells(cells, "precipitation")
    elev_field = ContinuousField.from_cells(cells, "elevation_mean")

    from .vegetation import vegetation_potential
    def _veg_classify(soil, temp, precip):
        return vegetation_potential(soil, temp, precip, is_ocean=False)

    # Sample at ~1° grid (180×360 = 64,800 points, fast with KDTree)
    vg_lats = np.arange(-89.5, 90.0, 1.0)
    vg_lons = np.arange(-179.5, 180.0, 1.0)
    veg_masks = classify_vegetation_grid(
        vg_lats, vg_lons, soil_field, temp_field, precip_field,
        _veg_classify, ocean_field=elev_field, ocean_threshold=0.0,
    )
    veg_polys = vegetation_masks_to_polygons(veg_masks, vg_lats, vg_lons, min_cells=4)
    for cover_type, poly in veg_polys:
        feat = Feature(
            type="terrain_cover",
            name=f"{cover_type.capitalize()} #{len([f for f in feature_store.get_features_by_type('terrain_cover')]) + 1}",
            geometry=poly,
            properties={"cover_type": cover_type},
            layer_effects={"soil_fertility_modifier": 1.0},
        )
        feature_store.add_feature(feat)
        _apply_layer_effects(feat, cells)
    print(f"  Extracted {len(veg_polys)} terrain cover polygon(s)")

    # ── Continuous mountain contours via elevation isoline ──
    print("  [Terrain] extracting mountain contours...")
    mt_lats, mt_lons, mt_values = sample_grid(elev_field, lat_step=0.5, lon_step=0.5)
    # Dynamic threshold: top 5% of land elevation
    land_vals = mt_values[mt_values > 0.0]
    mt_threshold = float(np.percentile(land_vals, 90)) if len(land_vals) > 100 else 0.4
    print(f"    (threshold={mt_threshold:.3f}, max_elev={float(mt_values.max()):.3f})")
    mt_polys = threshold_polygons(mt_lats, mt_lons, mt_values,
                                   threshold=mt_threshold, use_above=True,
                                   min_area=0.5, simplify_tol=0.1)
    for poly in mt_polys:
        mt_num = len([f for f in feature_store.get_features_by_type('mountain_range')]) + 1
        feat = Feature(
            type="mountain_range",
            name=f"Mountain Range #{mt_num}",
            geometry=poly,
            properties={"elevation_min": 0.6, "elevation_max": 1.0},
            layer_effects={"hazard_modifier": 1.5, "soil_fertility_modifier": 0.5},
        )
        feature_store.add_feature(feat)
        _apply_layer_effects(feat, cells)
    print(f"  Extracted {len(mt_polys)} mountain range(s)")

    # ── Continent polygon via elevation isoline (P0.4) ──
    print("  [Terrain] extracting continent outline...")
    cont_lats, cont_lons, cont_vals = sample_grid(elev_field, lat_step=0.25, lon_step=0.25)
    cont_polys = threshold_polygons(cont_lats, cont_lons, cont_vals,
                                     threshold=0.0, use_above=True,
                                     min_area=10.0, simplify_tol=0.1)
    cont_polys.sort(key=lambda p: p.area, reverse=True)
    if cont_polys:
        continent_poly = cont_polys[0]
        feature_store.add_feature(Feature(
            type="continent",
            name="Main Continent",
            geometry=continent_poly,
            properties={"area_deg2": continent_poly.area, "is_continent": True},
        ))
        print(f"  Continent outline: {continent_poly.area:.0f} deg²")
    else:
        print("  (no continent polygon extracted)")

    # ── Continuous climate/soil contour bands ──
    print("  [Fields] contouring climate and soil bands...")
    # Temperature bands (sampled at 1° grid)
    cb_lats, cb_lons, temp_grid = sample_grid(temp_field, lat_step=1.0, lon_step=1.0)
    temp_bands = contour_bands(cb_lats, cb_lons, temp_grid, num_bands=5, min_area=1.0)
    _TEMP_BAND_NAMES = ["Polar", "Subpolar", "Temperate", "Subtropical", "Tropical"]
    for band_idx, polys in temp_bands.items():
        band_name = _TEMP_BAND_NAMES[band_idx] if band_idx < len(_TEMP_BAND_NAMES) else f"Zone {band_idx}"
        for poly in polys:
            feature_store.add_feature(Feature(
                type="temperature_band",
                name=f"{band_name} Zone",
                geometry=poly,
                properties={"band": f"temp_{band_idx}", "temperature": (band_idx + 0.5) / 5.0},
            ))
    print(f"  Registered {sum(len(v) for v in temp_bands.values())} temperature band(s)")

    # Soil fertility bands
    _, _, soil_grid = sample_grid(soil_field, lat_step=1.0, lon_step=1.0)
    soil_bands = contour_bands(cb_lats, cb_lons, soil_grid, num_bands=5, min_area=1.0)
    _SOIL_BAND_NAMES = ["Barren", "Poor", "Moderate", "Fertile", "Rich"]
    for band_idx, polys in soil_bands.items():
        soil_name = _SOIL_BAND_NAMES[band_idx] if band_idx < len(_SOIL_BAND_NAMES) else f"Soil {band_idx}"
        for poly in polys:
            feature_store.add_feature(Feature(
                type="soil_region",
                name=f"{soil_name} Soil",
                geometry=poly,
                properties={"band": f"soil_{band_idx}", "soil_fertility": (band_idx + 0.5) / 5.0},
            ))
    print(f"  Registered {sum(len(v) for v in soil_bands.values())} soil region(s)")

    # Geology regions — continuous contouring (P0.2)
    _register_geology_features(feature_store, cells, ocean_set)

    # Resource zones — continuous contouring (P0.3)
    res_zones = _extract_resource_zone_polygons(cells, elev_field, flux_threshold=0.5)
    for feat in res_zones:
        feature_store.add_feature(feat)
    if res_zones:
        print(f"  Extracted {len(res_zones)} resource zone(s)")

    for cell in cells:
        feature_store.sync_cell(cell)

    # ══════════════════════════════════════════════════════════════
    # Layer 1 — Causal simulation (continuous fields + biomes)
    # ══════════════════════════════════════════════════════════════
    print("  [Layer 1] building simulation fields...")
    l1_fields = FieldRegistry.from_cells(cells)
    l1_engine = SimEngine(l1_fields)

    # Groundwater
    gw = Groundwater()
    l1_engine.add_feature(gw)

    # Biomes — replace old vegetation with continuous classification
    print("  [Layer 1] classifying biomes...")
    biome_points = sample_biomes(l1_fields, lat_step=2.0, lon_step=2.0)

    # Build a classification grid to prevent overlapping biome polygons
    # (Each grid point belongs to exactly one biome → connected components
    #  → non-overlapping hulls)
    # np is already imported at module level
    cb_lats = np.arange(-89.0, 90.0, 2.0)
    cb_lons = np.arange(-179.0, 180.0, 2.0)
    # Map each grid cell to its biome key
    biome_grid = {}
    for bk, pts in biome_points.items():
        for lon, lat in pts:
            # Find nearest grid index
            i = int((lat - cb_lats[0]) / 2.0 + 0.5)
            j = int((lon - cb_lons[0]) / 2.0 + 0.5)
            if 0 <= i < len(cb_lats) and 0 <= j < len(cb_lons):
                biome_grid[(i, j)] = bk

    # For each biome, find connected components via flood fill on grid
    from shapely import concave_hull as _ch
    from shapely.geometry import MultiPoint as _MP
    biome_count = 0
    assigned = set()

    for biome_key in sorted(set(biome_grid.values())):
        if biome_key in ("ice_desert",):
            continue
        # Get grid cells for this biome type and find connected components
        cells_bk = [(i, j) for (i, j), bk in biome_grid.items() if bk == biome_key]
        if len(cells_bk) < 4:
            continue

        # Flood fill to find connected components
        cell_set = set(cells_bk)
        while cell_set:
            seed = next(iter(cell_set))
            stack = [seed]
            component = []
            while stack:
                ci, cj = stack.pop()
                if (ci, cj) not in cell_set:
                    continue
                cell_set.discard((ci, cj))
                component.append((ci, cj))
                for di, dj in [(-1,0),(1,0),(0,-1),(0,1)]:
                    if (ci+di, cj+dj) in cell_set:
                        stack.append((ci+di, cj+dj))
            if len(component) < 4:
                continue
            pts = [(float(cb_lons[cj]), float(cb_lats[ci])) for ci, cj in component]
            try:
                mp = _MP(pts)
                hull = _ch(mp, ratio=0.05)
                if hull is not None and hull.geom_type == "Polygon" and hull.area > 4.0:
                    region = BiomeRegion(polygon=hull, biome_key=biome_key)
                    l1_engine.add_feature(region)
                    feature_store.add_feature(Feature(
                        type="biome",
                        name=region.name,
                        geometry=hull,
                        properties={"biome_key": biome_key,
                                    "canopy": region.props.get("canopy_density", 0),
                                    "biomass": region.props.get("biomass_kgm2", 0)},
                    ))
                    biome_count += 1
            except Exception:
                pass
    print(f"  [Layer 1] {biome_count} biome region(s)")

    # ── Water balance: connect rivers to lakes, then run ticks ──
    # (lakes are detected after this initial run, then further ticks run)

    # Detect lakes
    print("  [Layer 1] detecting lakes...")
    try:
        from ..layer1.features.lake import _detect_depressions as _dd, Lake as _Lake
        from shapely.geometry import Point as _Pt
        depressions = _dd(l1_fields, lat_step=0.5, lon_step=0.5, max_lakes=30, verbose=False)
        for poly, spill_elev in depressions:
            lake = _Lake(polygon=poly, spill_elevation=spill_elev)
            l1_engine.add_feature(lake)
            feature_store.add_feature(Feature(
                type="lake",
                name=f"Lake #{len([f for f in feature_store.get_features_by_type('lake')]) + 1}",
                geometry=poly,
                properties={"spill_elevation": spill_elev, "area_deg2": poly.area},
            ))
        print(f"  [Layer 1] {len(depressions)} lake(s)")

        # Connect rivers to lakes (river endpoint in lake polygon)
        from shapely.geometry import Point as _Pt
        river_features = feature_store.get_features_by_type("river")
        for lake_feat in l1_engine.get_features_by_type("lake"):
            if lake_feat.geometry is None:
                continue
            for riv_feat in river_features:
                if riv_feat.geometry is None:
                    continue
                coords = list(riv_feat.geometry.coords)
                if coords:
                    end_pt = _Pt(coords[-1][0], coords[-1][1])
                    if lake_feat.geometry.buffer(0.5).contains(end_pt):
                        flow = riv_feat.properties.get("flow_accumulation", 10.0) * 0.05
                        lake_feat.props["river_inflow_m3s"] = lake_feat.props.get("river_inflow_m3s", 0.0) + flow

            # Approximate catchment runoff (precip on 5x lake area)
            clat, clon = lake_feat.centroid() or (0, 0)
            p = l1_fields.get("precipitation").base_only(clat, clon)
            lake_area = lake_feat.props.get("max_area_deg2", 1.0)
            lake_feat.props["river_inflow_m3s"] += p * lake_area * 5.0 * 0.3

    except Exception as e:
        import traceback
        print(f"  [Layer 1] lake detection: {e}")
        traceback.print_exc()

    # Run water balance ticks (50 ticks × dt=10 = 500 "days")
    print("  [Layer 1] running water balance...")
    l1_engine.run(num_ticks=50, dt=10.0)

    # Report surviving lakes
    survivors = l1_engine.get_features_by_type("lake")
    stable = [f for f in survivors if f.props.get("volume_m3", 0) > f.props.get("max_volume_m3", 1) * 0.05]
    # Update lake areas in feature store and remove dry lakes
    fs_lakes = feature_store.get_features_by_type("lake")
    to_remove = []
    for i, sf in enumerate(survivors):
        vol = sf.props.get("volume_m3", 0)
        v_max = sf.props.get("max_volume_m3", 1)
        fill = vol / max(1.0, v_max) if v_max > 0 else 0
        if fs_lakes and i < len(fs_lakes):
            fs_feat = fs_lakes[i]
            if fill < 0.05:
                to_remove.append(fs_feat)
            else:
                area = sf.current_area() if vol > 0.01 else 0.0
                fs_feat.properties["fill_fraction"] = fill
                fs_feat.properties["volume_m3"] = vol
                fs_feat.properties["area_deg2"] = area
                fs_feat.name = f"{sf.name} ({fill*100:.0f}% full)"
    for fs_feat in to_remove:
        feature_store.dissolve_feature(fs_feat.feature_id, tick=0)
    print(f"  [Layer 1] {len(stable)} stable lake(s) after water balance")

    # Detect wetlands
    print("  [Layer 1] detecting wetlands...")
    try:
        from ..layer1.features.wetland import detect_wetlands as _dw, Wetland
        wetland_data = _dw(l1_fields, lat_step=2.0, lon_step=2.0)
        wl_count = 0
        for poly, wtype, size in wetland_data:
            if poly.area > 2.0:
                wetland = Wetland(polygon=poly, wetland_type=wtype)
                l1_engine.add_feature(wetland)
                feature_store.add_feature(Feature(
                    type="wetland",
                    name=wetland.name,
                    geometry=poly,
                    properties={"wetland_type": wtype},
                ))
                wl_count += 1
        print(f"  [Layer 1] {wl_count} wetland(s)")
    except Exception as e:
        print(f"  [Layer 1] wetland detection: {e}")

    # ══════════════════════════════════════════════════════════════
    # Stage — Mineralogy (ore deposit generation)
    # ══════════════════════════════════════════════════════════════
    print("  [Mineralogy] generating ore deposits...")
    try:
        from .mineralogy import generate_all_ores as _go
        ore_count = _go(cells, lithology_map, tectonics, feature_store, seed=params.seed)
        print(f"  [Mineralogy] {ore_count} ore deposits placed")
    except Exception as e:
        print(f"  [Mineralogy] skipped: {e}")

    print(f"  Feature store: {feature_store.count} total features")
    return cells, feature_store, flow_acc


def _register_geology_features(
    feature_store, cells: List[CellData], ocean: set,
) -> None:
    """Register geology regions using continuous contouring (P0.2).

    For each geological_type, builds a binary ContinuousField
    (1.0 for this type, 0.0 for others) and extracts polygons
    via threshold_polygons().
    """
    import h3
    from scipy.spatial import cKDTree
    from .contouring import ContinuousField, sample_grid, threshold_polygons

    # Build shared KDTree from all cell centroids
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
