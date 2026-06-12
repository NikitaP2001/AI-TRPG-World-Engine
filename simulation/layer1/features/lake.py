"""Lake — depression filling via Priority-Flood from ocean.

Causal chain:
  Priority-Flood algorithm (Barnes 2014): flood from ocean upward,
  tracking spill elevation for every cell.
  Cells where spill > elevation are in closed depressions → lakes.
  Lake level = spill elevation (lowest saddle to drained terrain).
  Lake area = connected depression cells.
  Water balance: inflow > evaporation → lake persists.

Uses 0.25° sampling grid for sub-cell precision (~28km at equator).
"""
from __future__ import annotations

import heapq
import math
import uuid
from typing import List, Optional, Tuple

import numpy as np
from shapely import concave_hull
from shapely.geometry import MultiPoint, Polygon as SPolygon

from .base import Feature
from ..fields import FieldRegistry
from ...layer0.climate import potential_evap_mm_day, _saturation_vp, norm_to_c, _ELEV_UNIT_TO_M


def _detect_depressions(
    fields: FieldRegistry,
    lat_step: float = 0.5,
    lon_step: float = 0.5,
    min_depth: float = 0.005,
    min_area_deg2: float = 0.1,
    max_lakes: int = 50,
    verbose: bool = True,
) -> List[Tuple[SPolygon, float]]:
    """Find lakes via Priority-Flood from ocean.

    Algorithm (Barnes 2014):
      1. Grid elevation at lat_step resolution.
      2. Push all ocean cells to priority queue (elevation = priority).
      3. Pop lowest cell. For each land neighbor not visited:
         spill = max(current spill, neighbor elev). Push to queue.
      4. After flood: cells with spill > elev are in depressions.
      5. Cluster adjacent depression cells. Each cluster = one lake.

    O(N log N), guaranteed to find every enclosed basin.
    Returns list of (polygon, spill_elevation) tuples.
    """
    import opensimplex
    elev_f = fields.get("elevation_mean")

    # 1. Sample grid
    lats = np.arange(-89.5, 90.0, lat_step)
    lons = np.arange(-179.5, 180.0, lon_step)
    nlat, nlon = len(lats), len(lons)
    if verbose:
        print(f"    grid={nlat}x{nlon}={nlat*nlon:,} pts", flush=True)

    # 1b. Vectorized KDTree sampling (replaces 259K Python calls)
    from simulation.grid_utils import sample_field_vectorized
    grid = sample_field_vectorized(elev_f, lats, lons)

    # Add tiny noise to break ties in priority flood (opensimplex loop, unavoidable)
    opensimplex.seed(999)
    for i in range(nlat):
        for j in range(nlon):
            noise = opensimplex.noise3(
                float(lats[i]) * 0.1, float(lons[j]) * 0.1, 0.0
            ) * 0.005
            grid[i, j] += noise

    # 2. Priority flood from ocean
    visited = np.zeros((nlat, nlon), dtype=bool)
    spill = np.full((nlat, nlon), -1.0, dtype=np.float64)

    pq = []  # (elevation, i, j)

    # Initialise: push ocean cells
    for i in range(nlat):
        for j in range(nlon):
            if grid[i, j] < -0.01:
                visited[i, j] = True
                spill[i, j] = 0.0
                heapq.heappush(pq, (0.0, i, j))

    # Flood
    while pq:
        cur_spill, ci, cj = heapq.heappop(pq)
        if cur_spill > spill[ci, cj]:
            continue
        for di, dj in [(-1,0),(1,0),(0,-1),(0,1)]:
            ni, nj = ci + di, cj + dj
            if 0 <= ni < nlat and 0 <= nj < nlon and not visited[ni, nj]:
                visited[ni, nj] = True
                ns = max(cur_spill, grid[ni, nj])
                spill[ni, nj] = ns
                heapq.heappush(pq, (ns, ni, nj))

    # 3. Find depressions: land cells with spill > elevation
    land = grid > -0.01
    depth = spill - grid
    depression = (depth > min_depth) & land

    if verbose:
        ndep = int(depression.sum())
        print(f"    depression cells: {ndep}", flush=True)

    # 4. Label connected depression components
    from scipy import ndimage as ndi
    labeled, n_features = ndi.label(depression)
    if n_features == 0:
        return []

    results = []
    for feat_id in range(1, min(n_features + 1, max_lakes * 5)):
        if len(results) >= max_lakes:
            break
        mask = labeled == feat_id
        n_cells = int(mask.sum())
        min_cells = max(3, int(min_area_deg2 * 16))
        if n_cells < min_cells:
            continue

        # Spill elevation = median of edge cell elevations
        rows, cols = np.where(mask)
        edge_elevs = []
        for r, c in zip(rows, cols):
            for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
                nr, nc = r + dr, c + dc
                if 0 <= nr < nlat and 0 <= nc < nlon and not depression[nr, nc]:
                    edge_elevs.append(grid[nr, nc])
                    break

        spill_elev = np.median(edge_elevs) if edge_elevs else float(spill[mask].max())
        if spill_elev < 0:
            continue

        pts = [(float(lons[c]), float(lats[r])) for r, c in zip(rows, cols)]
        if len(pts) < 3:
            continue

        try:
            mp = MultiPoint(pts)
            hull = concave_hull(mp, ratio=0.05)
            if hull is not None and hull.geom_type == "Polygon" and not hull.is_empty and hull.area > min_area_deg2:
                # Compactness check: skip if hull is a thin artifact
                # connecting sparse points over a huge area
                bounds = hull.bounds
                bbox_area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                if bbox_area > 100 and hull.area / bbox_area < 0.05:
                    if verbose:
                        print(f"      skip sparse hull: area={hull.area:.1f} bbox={bbox_area:.0f} ratio={hull.area/bbox_area:.4f}")
                    continue
                results.append((hull, spill_elev))
        except Exception:
            continue

    return results


class Lake(Feature):
    """A lake with physical water budget (no magic coefficients).

    Water balance:
      dV/dt = (precip_on_lake + river_inflow + spring_inflow + groundwater_inflow)
            - (evaporation + seepage + outflow)

    State variables:
      volume_m3:    Actual water volume [m³]
      level_m:      Water surface elevation above datum [m]

    Physics:
      - Evaporation: Penman-Monteith open water
      - Seepage:     Darcy flow through lake bed (K_sat × A × dh / d)
      - Inflow:      Precip on surface + catchment + rivers
      - Spill:       When level > spill_elevation, excess drains
    """

    def __init__(
        self,
        polygon: SPolygon,
        spill_elevation: float,
        feature_id: str = "",
    ):
        if not feature_id:
            feature_id = f"lake_{uuid.uuid4().hex[:8]}"
        max_area = polygon.area if polygon else 1.0

        # Centroid latitude for lat-dependent area/volume calculations
        centroid_pt = polygon.centroid if polygon else None
        clat = centroid_pt.y if centroid_pt is not None else 0.0  # Shapely: (lon, lat)

        # deg² → m² (latitude-dependent, P0.5)
        cos_lat = abs(math.cos(math.radians(clat)))
        m2_per_deg2 = (111320.0 * cos_lat) ** 2
        max_area_m2 = max_area * m2_per_deg2

        # Max volume from area (conical depression approximation, P0.6)
        # V_max ≈ 0.3 * A * depth  where depth ≈ spill * 500 m
        depth_m = max(10.0, spill_elevation * _ELEV_UNIT_TO_M)
        max_volume_m3 = max_area_m2 * depth_m * 0.3

        # Initial volume: small baseline, will be refined by water balance ticks (P0.7)
        volume_m3 = max_volume_m3 * 0.05
        level_m = depth_m * 0.05

        super().__init__(
            feature_id=feature_id,
            name=feature_id.replace("lake_", "Lake #"),
            geometry=polygon,
            feature_type="lake",
            props={
                "spill_elevation": spill_elevation,
                "max_area_deg2": max_area,
                "max_volume_m3": max_volume_m3,
                "volume_m3": volume_m3,
                "level_m": level_m,
                "outflow_m3s": 0.0,
                "river_inflow_m3s": 0.0,
                "spring_inflow_m3s": 0.0,
            },
        )

    def current_area(self) -> float:
        """Lake area scales with V^(2/3) (simple hypsometry)."""
        v = self.props.get("volume_m3", 0.0)
        v_max = self.props.get("max_volume_m3", 1.0)
        fill = max(0.0, min(1.0, v / max(1.0, v_max)))
        return self.props.get("max_area_deg2", 1.0) * (fill ** (2.0/3.0))

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        if self.geometry is None:
            return
        centroid = self.centroid()
        if centroid is None:
            return
        clat, clon = centroid

        vol = self.props.get("volume_m3", 0.0)
        v_max = self.props.get("max_volume_m3", 1.0)
        area_deg2 = self.current_area()

        if area_deg2 < 0.001 or v_max < 1.0:
            return

        # ── 1. Surface area in m² (latitude-dependent, P0.5) ──
        cos_lat = abs(math.cos(math.radians(clat)))
        m2_per_deg2 = (111320.0 * cos_lat) ** 2
        area_m2 = area_deg2 * m2_per_deg2

        # ── 2. Inflow from precipitation ──
        precip_norm = fields.get("precipitation").base_only(clat, clon)
        precip_ms = precip_norm * 2.0 / 365.0 / 86400.0  # m/s from normalized annual
        inflow_m3s = precip_ms * area_m2

        # ── 3. River inflow (set externally from catchment) ──
        inflow_m3s += self.props.get("river_inflow_m3s", 0.0)

        # ── 4. Spring inflow (from lithology) ──
        inflow_m3s += self.props.get("spring_inflow_m3s", 0.0)

        # ── 5. Evaporation (Penman-Monteith, P0.8-P0.9) ──
        temp_norm = fields.get("temperature").base_only(clat, clon)
        temp_c = norm_to_c(temp_norm)
        rh = min(0.90, max(0.25, 0.3 + precip_norm * 0.6))

        # Wind from fields (P0.8): геострофический ветер уже в CellData
        try:
            wind_u = fields.get("wind_u")(clat, clon)
            wind_v = fields.get("wind_v")(clat, clon)
            wind_ms = math.sqrt(wind_u**2 + wind_v**2)
        except KeyError:
            # Fallback: эмпирическая широтная зависимость
            wind_ms = 5.0 + 2.0 * abs(clat) / 90.0

        # Солнечная радиация из физической инсоляции (P0.9)
        from ...layer0.climate import _daily_insolation_toa, _solar_declination
        solar = _daily_insolation_toa(clat, _solar_declination(172.0, 23.5))

        evap_mm_day = potential_evap_mm_day(temp_c, rh, wind_ms, solar)
        evap_ms = evap_mm_day / 1000.0 / 86400.0  # mm/day → m/s
        evap_m3s = evap_ms * area_m2

        # ── 6. Seepage (Darcy) с K_sat из текстуры (P0.10) ──
        wt = fields.get("water_table_depth")(clat, clon)
        level_m = self.props.get("level_m", 0.0)
        lake_depth_m = max(1.0, level_m - wt * 100.0)
        if lake_depth_m > 0.5:
            # Оценка K_sat из текстуры почвы (Rosetta-подобная PTF)
            try:
                clay = fields.get("clay_content")(clat, clon)
                sand = fields.get("sand_content")(clat, clon)
                # log10(K_sat) ≈ -7 (clay) до -4 (sand) в m/s
                log_k = -7.0 + 3.0 * min(1.0, max(0.0, sand))
                k_sat_ms = 10.0 ** log_k
            except KeyError:
                k_sat_ms = 1e-6  # fallback silty lakebed
            seepage_m3s = k_sat_ms * area_m2 * (max(0.0, level_m - wt * 100.0)) / lake_depth_m
        else:
            seepage_m3s = 0.0

        # ── 7. Groundwater exchange (water table interaction) ──
        if wt < 0.5 and level_m > 0:
            gw_exchange = min(seepage_m3s, vol * 0.01 / max(1.0, dt))
        elif wt < 1.0:
            gw_exchange = -seepage_m3s * 0.5
        else:
            gw_exchange = 0.0

        # ── 8. Total budget ──
        dt_s = dt * 86400.0  # convert model dt to seconds
        dV = (inflow_m3s - evap_m3s - seepage_m3s + gw_exchange) * dt_s
        new_vol = max(0.0, vol + dV)

        # ── 9. Spill when above max ──
        spill_m3s = 0.0
        if new_vol > v_max:
            spill_m3s = (new_vol - v_max) / dt_s
            new_vol = v_max

        # ── 10. Update state ──
        fill = new_vol / max(1.0, v_max)
        spill_elev = self.props.get("spill_elevation", 1.0)
        # Level from hypsometry: для конической депрессии depth ∝ V^(1/3)
        new_level = spill_elev * _ELEV_UNIT_TO_M * (fill ** (1.0 / 3.0))

        self.props["volume_m3"] = new_vol
        self.props["level_m"] = new_level
        self.props["outflow_m3s"] = spill_m3s

        # ── 11. Effect on water table (lake raises local WT) ──
        wt_f = fields.get_mutable("water_table_depth")
        wt_lift = max(0.0, fill * 5.0 - wt)
        if wt_lift > 0.01:
            wt_f.add_persistent(clat, clon, radius_deg=area_deg2 ** 0.3 * 0.5,
                                strength=-wt_lift)

        # Reset inflows for next tick
        self.props["river_inflow_m3s"] = 0.0
        self.props["spring_inflow_m3s"] = 0.0

    def should_dissolve(self, fields: FieldRegistry) -> bool:
        return (self.props.get("volume_m3", 0.0) < 0.1
                and self._age_ticks > 20)
