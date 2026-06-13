"""Phase 2 — Forward cryosphere simulation.

Annual time steps:
  1. Snow accumulation: precip at T < -1°C → snowpack
  2. Snowmelt: degree-day model for T > 0°C
  3. Glacier mass balance: accumulation vs ablation
  4. Ice flow: shallow ice approximation along elevation gradient
  5. Calving: outlet glacier termini in ocean

Called from TimeEngine once per year (or per decade).
Stores state on CellData: snowpack_mm, ice_thickness_m.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from ..climate import norm_to_c


# Physical constants
_DEGREE_DAY_FACTOR = 3.0          # mm / °C / day
_SNOW_TEMP_C = -1.0
_MELT_TEMP_C = 0.0
_ICE_DENSITY = 917.0              # kg/m³
_WATER_DENSITY = 1000.0
_SNOW_COMPACTION_RATIO = 0.4      # fresh snow → ice depth ratio
_ICE_FLOW_CONSTANT = 1e-4         # SIA flow rate factor
_MAX_ICE_THICKNESS_M = 3000.0
_DAYS_PER_YEAR = 365.0


def advance_year(
    h3_ids: List[str],
    temperature: Dict[str, float],
    precipitation: Dict[str, float],
    elevation: Dict[str, float],
    ocean_set: set,
    snowpack: Dict[str, float],
    ice_thickness: Dict[str, float],
    day_of_year: float = 172.0,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Advance cryosphere by one year.

    Args:
        h3_ids: All cell IDs.
        temperature: Dict[h3_id] → normalized temperature.
        precipitation: Dict[h3_id] → normalized precipitation.
        elevation: Dict[h3_id] → normalized elevation.
        ocean_set: Set of ocean cell IDs.
        snowpack: Dict[h3_id] → snow water equivalent [mm].
        ice_thickness: Dict[h3_id] → glacier ice [m].

    Returns:
        (new_snowpack, new_ice_thickness)
    """
    new_snow = dict(snowpack)
    new_ice = dict(ice_thickness)

    for h in h3_ids:
        if h in ocean_set:
            new_snow[h] = 0.0
            new_ice[h] = 0.0
            continue

        t_norm = temperature.get(h, 0.5)
        t_c = norm_to_c(t_norm)
        p_norm = precipitation.get(h, 0.3)
        precip_mm = p_norm * 2000.0  # 0-1 → 0-2000 mm/yr
        el = elevation.get(h, 0.0)

        # ── 1. Snow accumulation ──────────────────────────────────────
        snowfall_mm = 0.0
        if t_c < _SNOW_TEMP_C:
            snowfall_mm = precip_mm  # all snow
        elif t_c < _MELT_TEMP_C + 2.0:
            # Mixed rain/snow transition
            snow_frac = (_MELT_TEMP_C + 2.0 - t_c) / 2.0
            snowfall_mm = precip_mm * max(0.0, min(1.0, snow_frac))

        new_snow[h] += snowfall_mm

        # ── 2. Snowmelt (degree-day) ──────────────────────────────────
        if t_c > _MELT_TEMP_C:
            pdd = t_c * _DAYS_PER_YEAR  # annual positive degree-days
            melt_mm = pdd * _DEGREE_DAY_FACTOR
            melt_mm = min(melt_mm, new_snow[h])  # can't melt more than available
            new_snow[h] -= melt_mm

        # ── 3. Snow → ice compaction ──────────────────────────────────
        # If snowpack persists through melt season, it compacts to ice
        if new_snow[h] > 100.0:  # > 10 cm snow water equivalent
            compaction = new_snow[h] * _SNOW_COMPACTION_RATIO / 1000.0  # mm → m
            new_ice[h] += compaction
            new_snow[h] -= new_snow[h] * 0.3  # partial compaction

        # ── 4. Ice ablation (surface melt) ────────────────────────────
        if t_c > _MELT_TEMP_C and new_ice[h] > 0:
            pdd = t_c * _DAYS_PER_YEAR
            ice_melt_m = pdd * _DEGREE_DAY_FACTOR / 1000.0  # mm → m
            new_ice[h] = max(0.0, new_ice[h] - ice_melt_m)

    # ── 5. Ice flow (simple downhill creep) ───────────────────────────
    new_ice = _ice_flow(h3_ids, new_ice, elevation, ocean_set)

    # ── 6. Calving at ocean boundary ──────────────────────────────────
    new_ice = _calving(h3_ids, new_ice, ocean_set)

    return new_snow, new_ice


def _ice_flow(
    h3_ids: List[str],
    ice: Dict[str, float],
    elevation: Dict[str, float],
    ocean_set: set,
) -> Dict[str, float]:
    """Simple ice flow: downhill creep proportional to thickness × slope.

    Shallow Ice Approximation (SIA) simplified:
      flux ∝ H^(n+1) * |∇(h)|^(n-1) * ∇(h)   where n ≈ 3 (Glen's law)

    Here: neighbour cells share ice proportional to thickness gradient.
    """
    import h3
    result = dict(ice)

    for h in list(ice.keys()):
        if h in ocean_set or ice.get(h, 0) < 1.0:
            continue
        nbs = h3.grid_ring(h, 1) or []
        h_el = elevation.get(h, 0.0)
        h_ice = ice.get(h, 0.0)

        total_out = 0.0
        for nb in nbs:
            if nb in ocean_set:
                continue
            nb_el = elevation.get(nb, 0.0)
            drop = h_el - nb_el
            if drop > 0 and h_ice > 5.0:
                # Flow downhill: ice thickness * slope * constant
                flow = h_ice * drop * _ICE_FLOW_CONSTANT
                total_out += flow

        if total_out > 0 and h_ice > total_out:
            result[h] = h_ice - total_out
            # Distribute outflow to downhill neighbours
            for nb in nbs:
                if nb in ocean_set:
                    continue
                nb_el = elevation.get(nb, 0.0)
                drop = h_el - nb_el
                if drop > 0:
                    flow = h_ice * drop * _ICE_FLOW_CONSTANT
                    result[nb] = result.get(nb, 0.0) + flow

    return result


def _calving(
    h3_ids: List[str],
    ice: Dict[str, float],
    ocean_set: set,
) -> Dict[str, float]:
    """Remove ice from cells adjacent to ocean (calving)."""
    import h3
    result = dict(ice)

    for h in list(ice.keys()):
        if h in ocean_set or ice.get(h, 0) < 1.0:
            continue
        nbs = h3.grid_ring(h, 1) or []
        for nb in nbs:
            if nb in ocean_set:
                # Calve 50% of ice adjacent to ocean
                result[h] *= 0.5
                break

    return result
