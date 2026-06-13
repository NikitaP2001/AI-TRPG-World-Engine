"""Wetland — bogs, fens, swamps, marshes.

Causal chain:
  water_table < 0.5m + slope < 2% + saturation → wetland
  wetland type: bog (low pH, sphagnum), fen (neutral, sedge),
                swamp (woody), marsh (herbaceous)
  peat accumulation: slow elevation increase (0.1-1mm/yr)
  wetland → higher albedo, lower temperature, higher soil_moisture

Fields read:
  elevation (for slope), water_table_depth, temperature,
  precipitation, soil_fertility

Fields written:
  soil_moisture = 1.0
  water_table_depth → raised (water retention)
  temperature → slight cooling
  elevation → very slow increase (peat)
"""
from __future__ import annotations

import uuid
from typing import Optional

from shapely.geometry import Polygon as SPolygon

from .base import Feature
from ..fields import FieldRegistry
from ...layer0.climate import norm_to_c


def _wetland_suitability(
    wt: float, temp: float, precip: float,
) -> dict:
    """Непрерывная гидропериодная классификация (P1.1).

    Возвращает {wetland_type: score (0-1)} — плавную пригодность
    для каждого типа без жёстких порогов.
    """
    scores = {}
    if wt > 0.5:
        return scores  # too dry for any wetland

    # Bog: холодный климат, высокие осадки, торф
    bog_temp = max(0.0, 1.0 - temp / 0.4)       # пик при temp < 0.2
    bog_precip = min(1.0, precip * 1.5)
    scores['bog'] = bog_temp * bog_precip * max(0.0, 1.0 - wt * 3.0)

    # Fen: умеренно-холодный, грунтовое питание
    fen_temp = max(0.0, 1.0 - abs(temp - 0.35) / 0.35)  # пик при temp ≈ 0.35
    scores['fen'] = fen_temp * max(0.0, 1.0 - wt * 2.0)

    # Swamp: тёплый, много осадков, древесная растительность
    swamp_temp = max(0.0, (temp - 0.4) / 0.4)   # пик при temp > 0.6
    swamp_precip = min(1.0, precip * 1.2)
    scores['swamp'] = swamp_temp * swamp_precip * max(0.0, 1.0 - wt * 3.0)

    # Marsh: тёплый, травянистый
    marsh_temp = max(0.0, (temp - 0.3) / 0.5)   # пик при temp > 0.5
    scores['marsh'] = marsh_temp * max(0.0, 1.0 - wt * 2.0)

    return scores


def detect_wetlands(
    fields: FieldRegistry,
    lat_step: float = 2.0,
    lon_step: float = 2.0,
) -> list:
    """Find potential wetland areas from field conditions.

    Uses continuous suitability scoring (P1.1) instead of threshold branching.
    Extracts polygons via marching squares on water_table_depth (P1.3).

    Returns list of (polygon, wetland_type, saturation) tuples.
    """
    import numpy as np
    from ..fields import ContinuousField
    from ...layer0.contouring import sample_grid, threshold_polygons

    elev_f = fields.get("elevation_mean")
    wt_f = fields.get("water_table_depth")
    precip_f = fields.get("precipitation")
    temp_f = fields.get("temperature")

    lats = np.arange(-88.0, 90.0, lat_step)
    lons = np.arange(-178.0, 180.0, lon_step)

    # 1b. Vectorized KDTree sampling for all fields at once
    from simulation.grid_utils import sample_fields_vectorized, make_latlon_grid
    lats, lons = make_latlon_grid(lat_step, lon_step, (-88.0, 90.0), (-178.0, 180.0))
    grids = sample_fields_vectorized({
        "elevation_mean": elev_f,
        "water_table_depth": wt_f,
        "precipitation": precip_f,
        "temperature": temp_f,
    }, lats, lons)
    elev_grid = grids["elevation_mean"]
    wt_grid = grids["water_table_depth"]
    precip_grid = grids["precipitation"]
    temp_grid = grids["temperature"]

    # 2. Vectorized suitability computation
    from collections import defaultdict
    nlat, nlon = len(lats), len(lons)

    # Ocean mask
    land = elev_grid >= -0.01
    dry = wt_grid <= 0.5
    valid = land & dry

    # Slope from neighbours using numpy slicing
    el_pad = np.pad(elev_grid, 1, mode='edge')
    slope_e = np.abs(elev_grid - el_pad[1:-1, 2:])
    slope_w = np.abs(elev_grid - el_pad[1:-1, :-2])
    slope_n = np.abs(elev_grid - el_pad[:-2, 1:-1])
    slope_s = np.abs(elev_grid - el_pad[2:, 1:-1])
    slope = np.maximum.reduce([slope_e, slope_w, slope_n, slope_s])
    valid &= slope <= 0.02

    # Wetland suitability for each valid cell
    by_type: dict = defaultdict(list)

    for i in range(nlat):
        for j in range(nlon):
            if not valid[i, j]:
                continue
            temp = temp_grid[i, j]
            precip = precip_grid[i, j]
            wt = wt_grid[i, j]

            scores = _wetland_suitability(wt, temp, precip)
            if not scores:
                continue

            wtype = max(scores, key=scores.get)
            if scores[wtype] > 0.3:
                by_type[wtype].append((float(lats[i]), float(lons[j])))

    # For each type, extract polygons via threshold_polygons on water_table_depth
    results = []
    for wtype, pts in by_type.items():
        if len(pts) < 4:
            continue
        # Build a binary field for this wetland type
        wt_lat_vals = np.array([p[0] for p in pts])
        wt_lon_vals = np.array([p[1] for p in pts])
        # Use marching squares on water_table_depth to define wetland boundary
        wt_min = min(wt_grid[wt_grid >= 0]) if np.any(wt_grid >= 0) else 0.0
        polys = threshold_polygons(lats, lons, wt_grid, threshold=wt_min + 0.1,
                                    use_above=False, min_area=2.0, simplify_tol=0.1)
        for poly in polys:
            if poly.area > 2.0:
                results.append((poly, wtype, len(pts)))

    # Fallback: if no polys from water table, use concave hull on points
    if not results:
        from shapely import concave_hull
        from shapely.geometry import MultiPoint
        for wtype, pts in by_type.items():
            if len(pts) < 4:
                continue
            lon_lat = [(p[1], p[0]) for p in pts]
            try:
                mp = MultiPoint(lon_lat)
                hull = concave_hull(mp, ratio=0.05)
                if hull is not None and hull.geom_type == "Polygon" and hull.area > 2.0:
                    results.append((hull, wtype, len(pts)))
            except Exception:
                pass

    return results


class Wetland(Feature):
    """A wetland area (bog/fen/swamp/marsh).

    Wetlands retain water, cool the local area, accumulate peat,
    and provide unique habitats.
    """

    def __init__(
        self,
        polygon: SPolygon,
        wetland_type: str = "marsh",
        feature_id: str = "",
    ):
        if not feature_id:
            feature_id = f"wetland_{uuid.uuid4().hex[:8]}"
        type_names = {"bog": "Bog", "fen": "Fen", "swamp": "Swamp", "marsh": "Marsh"}
        super().__init__(
            feature_id=feature_id,
            name=f"{type_names.get(wetland_type, 'Wetland')} #{feature_id[-4:]}",
            geometry=polygon,
            feature_type="wetland",
            props={"wetland_type": wetland_type, "peat_depth": 0.0},
        )

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        if self.geometry is None:
            return

        centroid = self.centroid()
        if centroid is None:
            return
        clat, clon = centroid

        wt_f = fields.get_mutable("water_table_depth")
        sm_f = fields.get_mutable("soil_moisture")
        temp_f = fields.get("temperature")

        # Keep water table at surface
        wt = wt_f(clat, clon)
        if wt > 0.0:
            wt_f.add_persistent(clat, clon, radius_deg=2.0,
                                strength=-wt * 0.3)

        # Saturate soil
        sm_f.add_persistent(clat, clon, radius_deg=2.0,
                            strength=0.5)

        # Cool local temperature (placeholder — needs MutableField for temperature)
        _ = temp_f  # ensure field reference exists

        # Peat accumulation with Q10 temperature dependence (P1.2)
        # Q10 = 2.5 for peat formation (2-3 range)
        temp_norm = temp_f(clat, clon)
        temp_c = max(0.0, norm_to_c(temp_norm))
        q10 = 2.5
        base_rate = 0.0001  # 0.1mm/tick at 10°C
        temp_factor = q10 ** ((temp_c - 10.0) / 10.0)
        peat = self.props.get("peat_depth", 0.0)
        peat += base_rate * temp_factor * dt
        self.props["peat_depth"] = peat

    def should_dissolve(self, fields: FieldRegistry) -> bool:
        """Dissolve if water table drops below 2m for extended period."""
        centroid = self.centroid()
        if centroid is None:
            return False
        wt = fields.get("water_table_depth")(centroid[0], centroid[1])
        return wt > 2.0 and self._age_ticks > 20
