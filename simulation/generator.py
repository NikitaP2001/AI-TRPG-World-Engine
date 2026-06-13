"""World generator — orchestrates L0 + L1 world building pipeline.

Delegates each stage to focused sub-functions so that generate_world()
stays under ~100 lines.  Every public symbol formerly in
simulation.layer0.generator is re-exported for backward compatibility.

Pipeline:
  Tectonics → Noise → WM Constraints → Continent detection → Climate →
  Ocean currents → CellData → Lithology → Soil → Vegetation → Resources →
  Hydrology → Feature extraction → L1 (biomes, lakes, wetlands, water
  balance) → Mineralogy → WM registrations
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h3
import numpy as np
import opensimplex

from .layer0.cell_model import CellData, GenerationParams
from .layer0.feature_store import FeatureStore, Feature
from .layer0.plate_tectonics import PlateTectonicsModel
from .layer0.resources import generate_resources, default_resource_types, SpecialResourceInput, ResourceType
from .layer0.soil import assign_soil_profiles
from .layer0.vegetation import assign_vegetation, vegetation_potential
from .layer0.climate import compute_climate
from .layer0.hydrology import compute_runoff_for_cells, compute_flow_accum_weighted
from .layer0.river_tracer import extract_rivers_continuous, build_elevation_kdtree
from .layer0.geology import bedrock_from_geology, assign_bedrock_classes, geology_name
from .layer0.lithology import generate_all_lithology
from .layer0.mineralogy import generate_all_ores
from .layer0.contouring import (
    ContinuousField,
    sample_grid,
    threshold_polygons,
    vegetation_masks_to_polygons,
    classify_vegetation_grid,
    contour_bands,
)
from .layer0.ocean_currents import (
    compute_ocean_currents, advect_sst, apply_coastal_climate, OceanCurrentParams,
)
from .layer1.engine import SimEngine
from .layer1.fields import FieldRegistry
from .layer1.features.groundwater import Groundwater
from .layer1.features.biomes import BiomeRegion, sample_biomes
from .grid_utils import flood_fill_grid
from .layer0.tectonics import TectonicEngine
from .layer0.cryosphere import CryosphereEngine

# ======================================================================
# Stage helpers  (each ≤ ~100 lines)
# ======================================================================


def _compute_all_ids(params: GenerationParams) -> List[str]:
    """Generate + shuffle H3 cell IDs for the full planet."""
    _rng = random.Random(params.seed)
    all_ids: List[str] = []
    for r0 in list(h3.get_res0_cells()):
        all_ids.extend(h3.cell_to_children(r0, params.h3_resolution))
    _rng.shuffle(all_ids)
    return all_ids


class _DictModelWrapper:
    """Wraps a tectonic state dict to quack like PlateTectonicsModel."""
    def __init__(self, state: dict):
        self.assignment = state["assignment"]
        self.boundary_type = state["boundary_type"]
        self.distance_to_boundary = state["distance_to_boundary"]
        self.crustal_age_myr = state["crustal_age_myr"]
        self.crustal_thickness_km = state["crustal_thickness_km"]
        self.thermal_gradient = state["thermal_gradient"]


def _stage_tectonics(
    all_ids: List[str], params: GenerationParams,
) -> Tuple[PlateTectonicsModel, Dict[str, float], Dict[str, int], set, set]:
    """Plates → elevation + geology + ocean/land sets.

    Uses the new two-phase TectonicEngine with planet_age_myr.
    Falls back to the legacy PlateTectonicsModel for age≈4500.
    """
    if abs(params.planet_age_myr - 4500.0) < 0.1:
        # Legacy fast path for Earth-like default
        tectonics = PlateTectonicsModel(
            all_ids, num_plates=params.num_plates,
            seed=params.seed, tectonic_activity=params.tectonic_activity,
        )
        elevation = tectonics.compute_elevation()
        geo_type = tectonics.compute_geology()
    else:
        # Age-aware generation via TectonicEngine
        engine = TectonicEngine(
            all_ids,
            tectonic_activity=params.tectonic_activity,
            seed=params.seed,
        )
        state = engine.generate(age_myr=params.planet_age_myr)
        tectonics = _DictModelWrapper(state)
        elevation = state["elevation"]
        geo_type = state["geological_type"]

    ocean_set = {h for h in all_ids if geo_type.get(h, 0) == 0}
    land_set = {h for h in all_ids if geo_type.get(h, 0) != 0}
    return tectonics, elevation, geo_type, ocean_set, land_set


def _add_noise(
    all_ids: List[str], elevation: Dict[str, float], seed: int,
) -> None:
    """Multi-octave simplex noise refinement (in-place).

    Creates 4 preseeded OpenSimplex generators ONCE and reuses them
    for all cells, avoiding 40 000 opensimplex.seed() calls.
    """
    gens = [opensimplex.OpenSimplex(seed + 1000 + o) for o in range(4)]
    for h in all_ids:
        latlng = h3.cell_to_latlng(h)
        noise = _multi_octave_noise(latlng[0], latlng[1], gens,
                                    persistence=0.5) * 0.2
        elevation[h] = max(-0.5, min(1.5, elevation[h] + noise))


def _apply_wm_elevation(
    elevation: Dict[str, float], all_ids: List[str],
    wm_constraints: Optional[Dict[str, Any]],
) -> None:
    """Inject WM continent/mountain/lake elevation constraints."""
    if not wm_constraints:
        return
    for key, label in [("continent_elevation", "continent"),
                       ("mountain_elevation", "mountain")]:
        mods = wm_constraints.get(key, {})
        if mods:
            n = 0
            for h, fe in mods.items():
                if h in all_ids and fe > elevation.get(h, 0.0):
                    elevation[h] = fe; n += 1
            print(f"  [Constraints] {label} elevation: {n} cells")
    le = wm_constraints.get("lake_elevation", {})
    if le:
        n = 0
        for h, fe in le.items():
            if h in all_ids and fe < elevation.get(h, 0.0):
                elevation[h] = fe; n += 1
        print(f"  [Constraints] lake depression: {n} cells")


def _detect_continents(
    all_ids: List[str], elevation: Dict[str, float],
    geo_type: Dict[str, int],
) -> Tuple[set, set, set]:
    """Detect continent/island/shelf via contouring + clustering fallback."""
    from scipy.spatial import cKDTree
    from shapely.geometry import Point as _Pt
    from shapely.prepared import prep as _prep
    from .grid_utils import build_kdtree_from_cells

    tree, elev_vals, _ = build_kdtree_from_cells(all_ids, values=elevation)
    tmp_elev = ContinuousField(tree, elev_vals)
    lats, lons, vals = sample_grid(tmp_elev, lat_step=0.5, lon_step=0.5)

    land_polys = threshold_polygons(lats, lons, vals, threshold=0.0,
                                     use_above=True, min_area=0.1, simplify_tol=0.05)
    land_polys.sort(key=lambda p: p.area, reverse=True)

    continent_cells: set = set()
    island_cells: set = set()

    # Precompute cell points once (avoids recreating Points in each loop iteration)
    cell_pts = {h: _Pt(h3.cell_to_latlng(h)[1], h3.cell_to_latlng(h)[0])
                for h in all_ids if elevation.get(h, 0.0) > 0.0}

    for i, poly in enumerate(land_polys):
        if poly.area < 0.01:
            continue
        minx, miny, maxx, maxy = poly.bounds
        prepped = _prep(poly)  # prepared geometry — caches spatial index
        pc = set()
        for h, pt in cell_pts.items():
            if (minx <= pt.x <= maxx and miny <= pt.y <= maxy
                    and elevation.get(h, 0.0) > 0.0):
                if prepped.contains(pt):
                    pc.add(h)
        if i == 0:
            continent_cells = pc
        else:
            island_cells |= pc

    # Fallback: clustering
    if not continent_cells:
        all_land = [h for h in all_ids if elevation.get(h, 0.0) > 0.0]
        clusters = _cluster_cells(all_land, lambda h: True)
        clusters.sort(key=len, reverse=True)
        if clusters and len(clusters[0]) > max(3, len(all_land) * 0.1):
            continent_cells = set(clusters[0])
        else:
            continent_cells = set(all_land)
        island_cells = set()
        for cl in clusters[1:]:
            island_cells.update(cl)

    # Shelf: shallow ocean adjacent to land
    ocean_set = {h for h in all_ids if geo_type.get(h, 0) == 0}
    shelf: set = set()
    for h in ocean_set:
        if elevation.get(h, 0.0) > -0.1:
            for nh in (h3.grid_ring(h, 1) or []):
                if nh in continent_cells or nh in island_cells:
                    shelf.add(h)
                    break
    return continent_cells, island_cells, shelf


def _stage_climate(
    all_ids: List[str], elevation: Dict[str, float],
    ocean_set: set, params: GenerationParams,
) -> Tuple:
    """Compute climate fields: temp, precip, wind, etc."""
    return compute_climate(all_ids, elevation, ocean_set,
                           seed=params.seed + 200, axial_tilt=params.axial_tilt)


def _apply_ocean_currents(
    all_ids: List[str], ocean_set: set, wind: dict,
    temp: dict, precip: dict, params: GenerationParams,
) -> Tuple[dict, dict]:
    """Modify coastal climate via ocean currents."""
    oc = OceanCurrentParams(
        enabled=True,
        wind_drag_coefficient=params.ocean_wind_drag,
        ekman_turn_angle_deg=params.ocean_ekman_angle,
        coastal_influence_radius_deg=params.ocean_coastal_radius,
    )
    currents = compute_ocean_currents(all_ids, ocean_set, wind, oc)
    base_sst = {h: temp.get(h, 0.5) for h in all_ids if h in ocean_set}
    sst_anom = advect_sst(all_ids, ocean_set, currents, base_sst, oc)
    land_ids = [h for h in all_ids if h not in ocean_set]
    temp, precip = apply_coastal_climate(land_ids, ocean_set, sst_anom, temp, precip, oc)
    n = sum(1 for v in sst_anom.values() if abs(v) > 0.01)
    print(f"  [Ocean] applied currents ({n} cells with SST anomaly)")
    return temp, precip


def _build_cells(
    all_ids: List[str], elevation: dict, geo_type: dict,
    tectonics: PlateTectonicsModel, slope: dict,
    temp: dict, temp_range: dict, precip: dict, precip_seas: dict,
    climate_class: dict, wind: dict, ocean_set: set, shelf_cells: set,
    params: GenerationParams,
) -> List[CellData]:
    """Construct CellData objects from all computed fields."""
    cells: List[CellData] = []
    res = params.h3_resolution
    for h in all_ids:
        nbs = h3.grid_ring(h, 1) or []
        n_els = [elevation.get(n, elevation.get(h, 0)) for n in nbs]
        ev = float(np.var([elevation.get(h, 0)] + n_els)) if n_els else 0.0
        is_ocean = h in ocean_set
        el = elevation.get(h, 0.0)
        gtype = geo_type.get(h, 0)
        near_water = any(nh in ocean_set for nh in nbs)
        wt = 0.0 if is_ocean else 0.5 * (1.0 - precip.get(h, 0.5)) * (0.3 if near_water else 1.0)
        cells.append(CellData(
            h3_id=h, resolution=res,
            elevation_mean=el, elevation_variance=ev,
            slope=slope.get(h, (0.0, 0.0)),
            geological_type=gtype,
            plate_id=tectonics.assignment.get(h, -1),
            boundary_type=tectonics.boundary_type.get(h, "intraplate"),
            distance_to_boundary=tectonics.distance_to_boundary.get(h, -1.0),
            bedrock_class=bedrock_from_geology(gtype),
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
            crustal_age_myr=tectonics.crustal_age_myr.get(h, 100.0),
            crustal_thickness_km=tectonics.crustal_thickness_km.get(h, 35.0),
            thermal_gradient=tectonics.thermal_gradient.get(h, 25.0),
            soil_fertility=0.02 if is_ocean else 0.1,
            hazard_level=0.0 if is_ocean else params.tectonic_activity * (1.0 - max(0.0, el)) * 0.3,
            special_resource_flux=[], tectonic_stress=abs(el) * params.tectonic_activity,
            anchor_feature_ids=[], feature_ids=[],
        ))
    return cells


def _stage_resources(
    all_ids: List[str], elevation: dict, geo_type: dict,
    params: GenerationParams, wm_constraints: Optional[Dict[str, Any]],
) -> Tuple[dict, list]:
    """Gray-Scott resource generation.  Injects WM ore types."""
    rtypes = default_resource_types()
    if wm_constraints and wm_constraints.get("ore_types"):
        n = 0
        for oc in wm_constraints["ore_types"]:
            p = oc.get("parameters") or {}
            rtypes.append(ResourceType(
                name=oc.get("name", oc["concept_id"]),
                feed_rate=float(p.get("gray_scott_feed", 0.035)),
                kill_rate=float(p.get("gray_scott_kill", 0.065)),
                diff_U=float(p.get("diff_U", 0.16)),
                diff_V=float(p.get("diff_V", 0.08)),
                seed_stress_threshold=float(p.get("seed_stress", 0.2)),
                seed_strength=float(p.get("seed_strength", 0.4)),
                timesteps=int(p.get("timesteps", 800)),
            ))
            n += 1
        if n:
            print(f"  [Constraints] {n} ore type(s) injected")
    stress = {h: abs(elevation.get(h, 0.0)) * params.tectonic_activity for h in all_ids}
    inp = SpecialResourceInput(h3_ids=all_ids, tectonic_stress=stress,
                                elevation=elevation, geological_type=geo_type)
    flux = generate_resources(inp, rtypes, params.seed + 555)
    return flux, rtypes


def _apply_hydrology(
    all_ids: List[str], elevation: dict, ocean_set: set, cells: List[CellData],
    wm_constraints: Optional[Dict[str, Any]],
) -> Tuple[dict, dict, dict, dict]:
    """Full hydrology pipeline with WM river/ridge constraints."""
    # WM river valley / ridge constraints
    if wm_constraints:
        rb = wm_constraints.get("river_bed_elevation", {})
        if rb:
            n = 0
            for h, fe in rb.items():
                if h in all_ids and fe < elevation.get(h, 0.0):
                    elevation[h] = fe; n += 1
            print(f"  [Constraints] river valley: {n} cells lowered")
        rg = wm_constraints.get("ridge_elevation", {})
        if rg:
            n = 0
            for h, fe in rg.items():
                if h in all_ids and fe > elevation.get(h, 0.0):
                    elevation[h] = fe; n += 1
            print(f"  [Constraints] river ridge: {n} cells raised")

    river_flag, flow_dir, flow_acc, basin = _compute_hydrology(
        all_ids, elevation, ocean_set,
    )
    weights = {c.h3_id: c.effective_precip for c in cells}
    flow_acc = compute_flow_accum_weighted(all_ids, flow_dir, weights)
    threshold = max(0.5, len(all_ids) / 4000 * 0.5)
    for h in all_ids:
        river_flag[h] = flow_acc.get(h, 0.0) >= threshold and h not in ocean_set
    return river_flag, flow_dir, flow_acc, basin


def _build_shared_fields(cells):
    """Build one shared KDTree from all cells + ContinuousField per attribute.

    Returns (sf, tf, pf, ef, kdt, kdv) where kdt/kdv are for river tracing.
    This eliminates 5+ separate KDTree builds from the same cell data.
    """
    import h3
    from scipy.spatial import cKDTree
    n = len(cells)
    pts = np.zeros((n, 3), dtype=np.float64)
    sf_vals = np.zeros(n, dtype=np.float64)
    tf_vals = np.zeros(n, dtype=np.float64)
    pf_vals = np.zeros(n, dtype=np.float64)
    ef_vals = np.zeros(n, dtype=np.float64)
    for i, c in enumerate(cells):
        latlng = h3.cell_to_latlng(c.h3_id)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        pts[i] = [math.cos(lat_r) * math.cos(lon_r),
                   math.sin(lat_r),
                   math.cos(lat_r) * math.sin(lon_r)]
        sf_vals[i] = c.soil_fertility
        tf_vals[i] = c.temperature
        pf_vals[i] = c.precipitation
        ef_vals[i] = c.elevation_mean

    tree = cKDTree(pts)
    sf = ContinuousField(tree, sf_vals)
    tf = ContinuousField(tree, tf_vals)
    pf = ContinuousField(tree, pf_vals)
    ef = ContinuousField(tree, ef_vals)

    # Also build the elevation KDTree for river tracing (shared pts array)
    kdt = tree
    kdv = ef_vals
    return sf, tf, pf, ef, kdt, kdv


def _extract_features(
    cells: List[CellData], island_cells: set, ocean_set: set,
    elev: dict, flow_acc: dict, params: GenerationParams,
) -> FeatureStore:
    """Extract terrain/river/climate features — islands, rivers, terrain, mountains, continent, bands, geology, resources."""
    from shapely.geometry import Point as _Pt

    fs = FeatureStore()

    # Build shared fields (one KDTree for all attributes)
    sf, tf, pf, ef, kdt, kdv = _build_shared_fields(cells)

    # Sample elevation at 0.25° ONCE and reuse for islands + continent
    ef_lats, ef_lons, ef_vals = sample_grid(ef, lat_step=0.25, lon_step=0.25)

    # Islands (reuse ef instead of building a separate isle_f)
    if island_cells:
        polys = threshold_polygons(ef_lats, ef_lons, ef_vals, threshold=0.0, use_above=True,
                                    min_area=0.01, simplify_tol=0.05)
        polys.sort(key=lambda p: p.area, reverse=True)
        for p in polys[1:]:
            if p.area < 0.01:
                continue
            fs.add_feature(Feature(type="island", name=f"Island", geometry=p,
                                    properties={"area_deg2": p.area},
                                    layer_effects={"geological_type_override": 1}))

    # Rivers via continuous gradient descent
    opensimplex.seed(params.seed + 1000)

    def _cont_elev(lat, lon):
        latr, lonr = math.radians(lat), math.radians(lon)
        px = math.cos(latr)*math.cos(lonr); py = math.sin(latr); pz = math.cos(latr)*math.sin(lonr)
        d, ix = kdt.query([px, py, pz], k=3)
        if np.any(d < 1e-15):
            e = float(kdv[ix[0]])
        else:
            e = float(np.average(kdv[ix], weights=1.0/(d+1e-15)))
        return e + opensimplex.noise3(px, py, pz) * 0.1

    min_a = max(0.5, len([c for c in cells if c.h3_id not in ocean_set]) / 4000 * 0.3)
    headwaters = []
    for c in cells:
        h = c.h3_id
        if h in ocean_set or flow_acc.get(h, 0) < min_a:
            continue
        nb = max((flow_acc.get(n, 0) for n in (h3.grid_ring(h, 1) or []) if n not in ocean_set), default=0)
        if flow_acc.get(h, 0) >= nb:
            ll = h3.cell_to_latlng(h); headwaters.append((ll[0], ll[1]))

    for r in extract_rivers_continuous(headwaters, _cont_elev, lambda lat, lon: _cont_elev(lat, lon) < 0,
                                        flow_accum_map=flow_acc, cell_area=params.cell_area):
        fs.add_feature(r)

    # Terrain cover (sf, tf, pf, ef already built from shared tree)
    vl = np.arange(-89.5, 90, 1); vo = np.arange(-179.5, 180, 1)
    vm = classify_vegetation_grid(vl, vo, sf, tf, pf,
        lambda s, t, p: vegetation_potential(s, t, p, False),
        ocean_threshold=0, ocean_field=ef)
    for ct, poly in vegetation_masks_to_polygons(vm, vl, vo, 4):
        fs.add_feature(Feature(type="terrain_cover", name=ct, geometry=poly,
                                properties={"cover_type": ct},
                                layer_effects={"soil_fertility_modifier": 1.0}))

    # Mountains (top 10 % of land elevation)
    ml, mo, mv = sample_grid(ef, lat_step=0.5, lon_step=0.5)
    lv = mv[mv > 0]
    mt = float(np.percentile(lv, 90)) if len(lv) > 100 else 0.4
    for p in threshold_polygons(ml, mo, mv, threshold=mt, use_above=True, min_area=0.5, simplify_tol=0.1):
        fs.add_feature(Feature(type="mountain_range", name="Mountain", geometry=p,
                                properties={"elevation_min": 0.6},
                                layer_effects={"hazard_modifier": 1.5, "soil_fertility_modifier": 0.5}))

    # Continent polygon (reuses ef_lats/ef_vals sampled at 0.25° above)
    cp = threshold_polygons(ef_lats, ef_lons, ef_vals, threshold=0, use_above=True, min_area=10, simplify_tol=0.1)
    cp.sort(key=lambda p: p.area, reverse=True)
    if cp:
        fs.add_feature(Feature(type="continent", name="Main Continent", geometry=cp[0],
                                properties={"area_deg2": cp[0].area, "is_continent": True}))

    # Temperature bands
    cbl, cbo, tg = sample_grid(tf, lat_step=1, lon_step=1)
    _TBN = ["Polar", "Subpolar", "Temperate", "Subtropical", "Tropical"]
    for bi, polys in contour_bands(cbl, cbo, tg, num_bands=5, min_area=1).items():
        nm = _TBN[bi] if bi < len(_TBN) else f"Zone {bi}"
        for p in polys:
            fs.add_feature(Feature(type="temperature_band", name=f"{nm} Zone", geometry=p,
                                    properties={"band": f"temp_{bi}"}))

    # Soil bands
    _, _, sg = sample_grid(sf, lat_step=1, lon_step=1)
    _SBN = ["Barren", "Poor", "Moderate", "Fertile", "Rich"]
    for bi, polys in contour_bands(cbl, cbo, sg, num_bands=5, min_area=1).items():
        nm = _SBN[bi] if bi < len(_SBN) else f"Soil {bi}"
        for p in polys:
            fs.add_feature(Feature(type="soil_region", name=f"{nm} Soil", geometry=p,
                                    properties={"band": f"soil_{bi}"}))

    # Geology + resource zones (pass shared KDTree to avoid rebuild)
    _register_geology_features(fs, cells, ocean_set, shared_tree=ef._tree)
    for f in _extract_resource_zone_polygons(cells, ef, 0.5, shared_tree=ef._tree):
        fs.add_feature(f)

    for c in cells:
        fs.sync_cell(c)
    return fs


def _add_layer1(
    cells: List[CellData], feature_store: FeatureStore,
) -> None:
    """Build L1 simulation: biomes, lakes, wetlands, water balance, mineralogy."""
    from .layer1.features.lake import _detect_depressions as _dd, Lake as _Lake
    from .layer1.features.wetland import detect_wetlands as _dw, Wetland
    from shapely.geometry import Point as _Pt
    from shapely import concave_hull as _ch
    from shapely.geometry import MultiPoint as _MP

    l1f = FieldRegistry.from_cells(cells)
    l1e = SimEngine(l1f)
    l1e.add_feature(Groundwater())

    # Biomes — flood fill on 2° grid
    bp = sample_biomes(l1f, lat_step=2, lon_step=2)
    cbl = np.arange(-89, 90, 2); cbo = np.arange(-179, 180, 2)
    bg = {}
    for bk, pts in bp.items():
        for lon, lat in pts:
            i = int((lat - cbl[0]) / 2 + 0.5); j = int((lon - cbo[0]) / 2 + 0.5)
            if 0 <= i < len(cbl) and 0 <= j < len(cbo):
                bg[(i, j)] = bk
    for bk in sorted(set(bg.values())):
        if bk == "ice_desert":
            continue
        cells_bk = [(i, j) for (i, j), b in bg.items() if b == bk]
        if len(cells_bk) < 4:
            continue
        mask = np.zeros((len(cbl), len(cbo)), dtype=bool)
        for i, j in cells_bk:
            mask[i, j] = True
        for comp in flood_fill_grid(mask, 4):
            pts = [(float(cbo[cj]), float(cbl[ci])) for ci, cj in comp]
            try:
                hull = _ch(_MP(pts), ratio=0.05)
                if hull and hull.geom_type == "Polygon" and hull.area > 4:
                    r = BiomeRegion(polygon=hull, biome_key=bk)
                    l1e.add_feature(r)
                    feature_store.add_feature(Feature(type="biome", name=r.name, geometry=hull,
                        properties={"biome_key": bk, "canopy": r.props.get("canopy_density", 0)}))
            except Exception:
                pass

    # Lakes
    deps = _dd(l1f, lat_step=0.5, lon_step=0.5, max_lakes=30, verbose=False)
    for poly, spill in deps:
        lake = _Lake(polygon=poly, spill_elevation=spill)
        l1e.add_feature(lake)
        feature_store.add_feature(Feature(type="lake", name=f"Lake", geometry=poly,
            properties={"spill_elevation": spill, "area_deg2": poly.area}))

    # Connect rivers → lakes
    for lf in l1e.get_features_by_type("lake"):
        if lf.geometry is None:
            continue
        for rf in feature_store.get_features_by_type("river"):
            if rf.geometry is None:
                continue
            coords = list(rf.geometry.coords)
            if coords and lf.geometry.buffer(0.5).contains(_Pt(coords[-1][0], coords[-1][1])):
                lf.props["river_inflow_m3s"] = lf.props.get("river_inflow_m3s", 0) + rf.properties.get("flow_accumulation", 10) * 0.05
        clat, clon = lf.centroid() or (0, 0)
        p = l1f.get("precipitation").base_only(clat, clon)
        lf.props["river_inflow_m3s"] += p * lf.props.get("max_area_deg2", 1) * 5 * 0.3

    l1e.run(num_ticks=50, dt=10)

    # Update / dissolve dry lakes
    for sf in l1e.get_features_by_type("lake"):
        vol, vmax = sf.props.get("volume_m3", 0), sf.props.get("max_volume_m3", 1)
        fill = vol / max(1, vmax) if vmax > 0 else 0
        for fs_feat in feature_store.get_features_by_type("lake"):
            if fs_feat.geometry and sf.geometry and fs_feat.geometry.centroid.distance(sf.geometry.centroid) < 1:
                if fill < 0.05:
                    feature_store.dissolve_feature(fs_feat.feature_id, 0)
                else:
                    fs_feat.properties["fill_fraction"] = fill
                    fs_feat.properties["volume_m3"] = vol

    # Wetlands
    for poly, wtype, _ in _dw(l1f, lat_step=2, lon_step=2):
        if poly.area > 2:
            l1e.add_feature(Wetland(polygon=poly, wetland_type=wtype))
            feature_store.add_feature(Feature(type="wetland", name=wtype, geometry=poly,
                properties={"wetland_type": wtype}))

    # Mineralogy
    try:
        lith = generate_all_lithology(cells, seed=42)
        generate_all_ores(cells, lith, None, feature_store, seed=42)
    except Exception as e:
        print(f"  [Mineralogy] skipped: {e}")


def _register_wm_concepts(wm_constraints: Optional[Dict[str, Any]]) -> None:
    """Register WM fauna species and flora PFTs from constraints."""
    if not wm_constraints:
        return
    wm_fauna = wm_constraints.get("fauna_species", [])
    wm_flora = wm_constraints.get("flora_pft", [])
    if wm_fauna:
        from .layer1.fauna_registry import register_fauna_species, FaunaSpeciesDef
        for sp in wm_fauna:
            p = sp.get("parameters", {})
            register_fauna_species(sp["concept_id"], FaunaSpeciesDef(
                name=p.get("name", sp["concept_id"]), habitat_type=p.get("habitat_type", "terrestrial"),
                habitat_biomes=p.get("habitat_biomes", []), diet=p.get("diet", "herbivore"),
                diet_sources=p.get("diet_sources", {}),
                population_density_max=float(p.get("population_density_max", 10)),
                base_birth=float(p.get("base_birth", 0.01)),
                base_death=float(p.get("base_death", 0.01)),
                migration_rate=float(p.get("migration_rate", 0.05)),
                huntable=bool(p.get("huntable", True)),
                emergence_population_threshold=float(p.get("emergence_population_threshold", 0)),
                hazard_weight=float(p.get("hazard_weight", -1)),
                size_class=p.get("size_class", "medium"),
                plankton_consumption_rate=float(p.get("plankton_consumption_rate", 0)),
            ))
        print(f"  [Constraints] {len(wm_fauna)} fauna species registered")
    if wm_flora:
        from .layer0.plant_registry import register_pft, PlantDef
        for pft in wm_flora:
            p = pft.get("parameters", {})
            register_pft(pft["concept_id"], PlantDef(
                name=p.get("name", pft["concept_id"]), family=p.get("family", "unknown"),
                growth_form=p.get("growth_form", "tree"),
                temp_min=float(p.get("temp_min", 0)), temp_opt_min=float(p.get("temp_opt_min", 0.3)),
                temp_opt_max=float(p.get("temp_opt_max", 0.7)), temp_max=float(p.get("temp_max", 1)),
                precip_min=float(p.get("precip_min", 0)), precip_opt_min=float(p.get("precip_opt_min", 0.2)),
                precip_opt_max=float(p.get("precip_opt_max", 0.8)), precip_max=float(p.get("precip_max", 1)),
                max_biomass_kgm2=float(p.get("max_biomass_kgm2", 10)),
                max_canopy_density=float(p.get("max_canopy_density", 0.7)),
                growth_rate=float(p.get("growth_rate", 0.1)), mortality_rate=float(p.get("mortality_rate", 0.02)),
                shade_tolerance=float(p.get("shade_tolerance", 0.3)),
            ))
        print(f"  [Constraints] {len(wm_flora)} flora PFT(s) registered")


# ======================================================================
# ── Initial fauna distribution ──────────────────────────────────────

def _init_fauna_distribution(
    cells: List[CellData],
    elevation: Dict[str, float],
    temperature: Dict[str, float],
    precipitation: Dict[str, float],
    ocean_set: set,
) -> None:
    """Compute initial simple fauna distribution per cell.

    For each registered species, evaluates habitat suitability using
    basic environmental variables and stores initial density as a
    CellData attribute (list of (species_id, density) tuples).

    Later saved to fauna_populations table by save_generated_world().
    """
    from .layer1.fauna_registry import FAUNA_REGISTRY, get_species_ids
    species_ids = list(get_species_ids())
    if not species_ids:
        print("  [Fauna] No species registered, skipping")
        return

    import math, random
    random.seed(42)

    n_cells_with_fauna = 0
    for c in cells:
        hid = c.h3_id
        el = elevation.get(hid, 0.0)
        tn = temperature.get(hid, 0.5)
        pn = precipitation.get(hid, 0.5)
        is_ocean = hid in ocean_set

        fauna_list = []
        for sp_id in species_ids:
            sp = FAUNA_REGISTRY.get(sp_id)
            if sp is None:
                continue

            # Simple habitat suitability (same logic as Fauna._suitability)
            suit = 1.0

            if sp.habitat_type == "aquatic":
                if not is_ocean and el >= 0:
                    suit = 0.0
            elif sp.habitat_type in ("terrestrial",):
                if is_ocean or el < 0:
                    suit = 0.0
            elif sp.habitat_type == "amphibious":
                pass  # can live anywhere
            elif sp.habitat_type == "aerial":
                pass  # can live anywhere

            if suit > 0:
                # Temperature penalties
                if tn < 0.05:
                    suit *= max(0.0, tn / 0.05)
                if tn > 0.95:
                    suit *= max(0.0, (1.0 - tn) / 0.05)
                # Precipitation penalty for terrestrial
                if sp.habitat_type != "aquatic" and pn < 0.02:
                    suit *= max(0.0, pn / 0.02)

            if suit > 0.01:
                # Initial density = suitability * max_density * random jitter
                jitter = 0.7 + random.random() * 0.6  # 0.7–1.3
                density = suit * sp.population_density_max * jitter * 0.3
                if density > 0.01:
                    fauna_list.append((sp_id, density))

        c.fauna = fauna_list
        if fauna_list:
            n_cells_with_fauna += 1

    n_total_species = len(species_ids)
    print(f"  [Fauna] {n_total_species} species, {n_cells_with_fauna}/{len(cells)} cells populated")


# Main entry point  (≤ ~100 lines)
# ======================================================================

def generate_world(
    params: Optional[GenerationParams] = None,
    feature_store: Optional[FeatureStore] = None,
    wm_constraints: Optional[Dict[str, Any]] = None,
) -> Tuple[List[CellData], FeatureStore, Dict[str, float]]:
    """Run full L0 + L1 world generation pipeline.

    Pipeline: Tectonics (age-aware) → Noise → WM Constraints → Continents →
    Climate → Ocean Currents → CellData → Lithology → Soil → Vegetation →
    Resources → Hydrology → Cryosphere (age-aware glaciers) → Feature Extraction →
    L1 (Biomes/Lakes/Wetlands/Water Balance) → Mineralogy → WM registrations.
    """
    if params is None:
        params = GenerationParams()
    params.derive()

    # Register default Earth-like fauna (WM can supplement later)
    from .layer1.default_fauna import register_default_fauna
    register_default_fauna()

    print(f"[Layer 0] resolution={params.h3_resolution}, top_level_cells={params.top_level_cell_count}", flush=True)

    # Phase 1 — Terrain
    all_ids = _compute_all_ids(params)
    print(f"[Layer 0] cell_count={len(all_ids)}")
    print("  [Tectonics] generating plates...")
    tectonics, elevation, geo_type, ocean_set, land_set = _stage_tectonics(all_ids, params)
    print("  [Refinement] simplex noise detail...")
    _add_noise(all_ids, elevation, params.seed)
    _apply_wm_elevation(elevation, all_ids, wm_constraints)

    # Phase 2 — Continents + climate
    continent_cells, island_cells, shelf_cells = _detect_continents(all_ids, elevation, geo_type)
    slope = _compute_slope(all_ids, elevation)
    print("  [Climate] computing wind, temperature, precipitation...")
    temp, temp_range, precip, precip_seas, climate_class, wind = _stage_climate(all_ids, elevation, ocean_set, params)
    if params.ocean_currents_enabled:
        temp, precip = _apply_ocean_currents(all_ids, ocean_set, wind, temp, precip, params)

    # Phase 3 — CellData + sub-surface + surface
    print("  Generated cells...")
    cells = _build_cells(all_ids, elevation, geo_type, tectonics, slope,
                          temp, temp_range, precip, precip_seas,
                          climate_class, wind, ocean_set, shelf_cells, params)
    assign_bedrock_classes(cells)
    print(f"  Generated {len(cells)} cells")

    print("  [Lithology] generating subsurface rock columns...")
    lithology_map = generate_all_lithology(cells, seed=params.seed)
    print(f"  [Lithology] {len(lithology_map)} columns generated")

    print("  [Soil] computing weathering and soil profiles...")
    assign_soil_profiles(cells, temperature=temp, precipitation=precip,
                          ocean_set=ocean_set, shelf_set=shelf_cells, time_factor=1)
    print("  [Vegetation] computing vegetation potential...")
    assign_vegetation(cells, ocean_set, iterations=3)

    # Phase 4 — Resources + hydrology
    print("  [Resources] generating special resources...")
    resource_flux, _ = _stage_resources(all_ids, elevation, geo_type, params, wm_constraints)
    for c in cells:
        c.special_resource_flux = resource_flux.get(c.h3_id, [])
    print(f"  {len(resource_flux)} cells with resource flux")

    print("  [Hydrology] computing runoff ratios...")
    compute_runoff_for_cells(cells, precip, ocean_set, day_of_year=172)
    print("  [Hydrology] computing flow direction...", flush=True)
    river_flag, flow_dir, flow_acc, basin = _apply_hydrology(all_ids, elevation, ocean_set, cells, wm_constraints)

    # Phase 4b — Cryosphere (age-aware glacier generation)
    print("  [Cryosphere] computing glaciers...")
    try:
        cryo = CryosphereEngine(
            all_ids, elevation, temp, precip, ocean_set,
        )
        ice_thickness = cryo.generate(age_myr=params.planet_age_myr)
        n_glaciers = sum(1 for v in ice_thickness.values() if v > 1.0)
        print(f"  [Cryosphere] {n_glaciers} cells with glacier ice")
        # Store ice thickness on cells (for feature extraction + viewer)
        for c in cells:
            c.ice_thickness_m = ice_thickness.get(c.h3_id, 0.0)
    except Exception as e:
        print(f"  [Cryosphere] skipped: {e}")
        for c in cells:
            c.ice_thickness_m = 0.0

    # Phase 5 — Feature extraction
    print("  [Features] extracting terrain features...")
    feature_store = _extract_features(cells, island_cells, ocean_set, elevation, flow_acc, params)

    # Phase 6 — Layer 1 simulation
    print("  [Layer 1] building simulation fields...")
    _add_layer1(cells, feature_store)
    _register_wm_concepts(wm_constraints)

    # Phase 7 — Initial fauna distribution
    _init_fauna_distribution(cells, elevation, temp, precip, ocean_set)

    print(f"  Feature store: {feature_store.count} total features")
    return cells, feature_store, flow_acc


# ======================================================================
# Re-exports — all public symbols previously in simulation.layer0.generator
# ======================================================================

def _bedrock_from_geology(gtype: int) -> str:
    return bedrock_from_geology(gtype)


# Backward-compatible re-exports from the original module
from .layer0.generator import (
    _noise_at,
    _multi_octave_noise,
    _diamond_square_refine,
    _compute_slope,
    _fill_depressions,
    _cluster_cells,
    _cluster_to_polygon,
    _compute_flow_dir,
    _compute_flow_accum,
    _assign_basins,
    _compute_hydrology,
    _register_geology_features,
    _extract_resource_zone_polygons,
    save_cells_parquet,
    load_cells_parquet,
)
