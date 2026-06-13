"""Phase 1 — Initial glacier/ice sheet state generation.

Analytical snapshot: given climate (temperature, precipitation) and
topography, compute where glaciers would form after reaching equilibrium
(typically 10⁴ years under stable climate).

Logic:
  1. Annual mass balance = snowfall - ablation
  2. Accumulation zone: balance > 0
  3. Equilibrium Line Altitude (ELA): where balance = 0
  4. Ice thickness: scales with positive balance × time_to_equilibrium
  5. Ice flow: steepest descent from accumulation zones to ablation zones
  6. Calving: outlet glaciers reaching ocean

Age-aware: if planet_age < time_for_water_oceans → no glaciers.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..climate import norm_to_c, TEMP_C_MIN, _TEMP_C_RANGE


# Thresholds (calibrated for Earth-like planets)
_SNOW_TEMP_C = -1.0               # precip falls as snow below this
_MELT_TEMP_C = 0.0                # ice starts melting above this
_DEGREE_DAY_FACTOR = 3.0          # mm/day/degC melt rate
_ICE_EQUILIBRIUM_YEARS = 10000.0  # years to reach full冰川 thickness
_MIN_ACCUMULATION_M_WE = 0.2      # m water equiv / yr for glacier formation
_MAX_ICE_THICKNESS_M = 3000.0     # physical max
_AGE_FOR_WATER_OCEANS_MYR = 10.0  # Myr needed for liquid water


def generate_glaciers(
    h3_ids: List[str],
    elevation: Dict[str, float],
    temperature: Dict[str, float],
    precipitation: Dict[str, float],
    ocean_set: set,
    age_myr: float = 4500.0,
    elev_scale_m: float = 5000.0,  # elevation unit → metres
) -> Dict[str, float]:
    """Compute initial glacier ice thickness for every cell.

    Fast analytical pass — no time stepping.

    Args:
        h3_ids: All cell IDs.
        elevation: Dict[h3_id] → normalized elevation.
        temperature: Dict[h3_id] → normalized temperature (0-1).
        precipitation: Dict[h3_id] → normalized precipitation (0-1).
        ocean_set: Set of ocean cell IDs.
        age_myr: Planet age in Myr.
        elev_scale_m: Conversion from elevation units to metres.

    Returns:
        Dict[h3_id] → ice thickness in metres (0 = no glacier).
    """
    ice: Dict[str, float] = {}

    # Too young for liquid water → no glaciers
    if age_myr < _AGE_FOR_WATER_OCEANS_MYR:
        return {h: 0.0 for h in h3_ids}

    # 1. Compute annual mass balance for each cell
    #    snowfall = precip at T < -1°C, sum over year
    #    ablation = degree-day melt at T > 0°C, sum over year
    #    balance = snowfall_mwe - melt_mwe
    balance: Dict[str, float] = {}
    for h in h3_ids:
        if h in ocean_set:
            continue  # no glaciers on open ocean (sea ice is different)

        t_norm = temperature.get(h, 0.5)
        t_c = norm_to_c(t_norm)
        p_norm = precipitation.get(h, 0.3)
        # Convert precip to m water equivalent / yr (0-1 → 0-2 m/yr)
        precip_m_yr = p_norm * 2.0

        # Snowfall fraction (days below SNOW_TEMP_C per year)
        # Approximate: fraction of year when T < SNOW_TEMP_C
        # Using seasonal amplitude ~15°C as typical
        snow_fraction = max(0.0, min(1.0,
            (_SNOW_TEMP_C - t_c + 7.5) / 15.0 if t_c < _SNOW_TEMP_C + 7.5 else 0.0
        ))
        snowfall_mwe = precip_m_yr * snow_fraction

        # Ablation: degree-day melt
        if t_c > _MELT_TEMP_C:
            # Approximate annual positive degree-days
            # For T_mean > 0, ~365 * T_mean degree-days
            pdd = max(0, t_c) * 365.0
            melt_mwe = pdd * _DEGREE_DAY_FACTOR / 1000.0  # mm → m
        else:
            melt_mwe = 0.0

        balance[h] = snowfall_mwe - melt_mwe

    # 2. Accumulation zones: cells with positive annual balance
    #    plus upslope neighbours feeding them
    acc_cells = {h for h in balance if balance.get(h, 0) > _MIN_ACCUMULATION_M_WE}

    # 3. Compute ice sheet thickness from balance × time_to_equilibrium
    #    In equilibrium, thickness ≈ balance * equilibrium_years / compaction_factor
    compaction = 2.0  # snow compacts to ice at ~2:1 depth ratio
    for h in h3_ids:
        if h in ocean_set:
            ice[h] = 0.0
            continue

        bal = balance.get(h, 0.0)
        if bal > 0 and h in acc_cells:
            thick = bal * _ICE_EQUILIBRIUM_YEARS / compaction
            ice[h] = min(_MAX_ICE_THICKNESS_M, thick)
        else:
            ice[h] = 0.0

    # 4. Simple ice flow: fill downhill from accumulation zones
    #    Down to elevation where balance becomes negative (ELA)
    ice = _flow_downhill(ice, elevation, balance, h3_ids, ocean_set)

    return ice


def _flow_downhill(
    ice: Dict[str, float],
    elevation: Dict[str, float],
    balance: Dict[str, float],
    h3_ids: List[str],
    ocean_set: set,
) -> Dict[str, float]:
    """Extend ice downhill from accumulation zones via steepest descent.

    Ice flows from cells with positive balance downhill until:
      - Balance becomes sufficiently negative (ablation zone)
      - Ocean is reached (calving)
      - Elevation rises again (can't flow uphill)
    """
    import h3

    result = dict(ice)
    # Sort accumulation cells by elevation (highest first)
    acc_cells = sorted(
        [h for h in h3_ids if balance.get(h, 0) > 0 and h not in ocean_set],
        key=lambda h: elevation.get(h, 0), reverse=True,
    )

    for seed_h in acc_cells:
        # Follow steepest descent from each accumulation cell
        cur = seed_h
        visited = {cur}
        for _ in range(50):  # max 50 steps (~500 km)
            el = elevation.get(cur, 0.0)
            if el < -0.1:  # reached ocean
                break

            # Find steepest downhill neighbour
            nbs = h3.grid_ring(cur, 1) or []
            best_nb = None
            best_drop = 0.0
            for nb in nbs:
                if nb in ocean_set:
                    best_nb = nb
                    break
                if nb in visited:
                    continue
                nb_el = elevation.get(nb, 0.0)
                drop = el - nb_el
                if drop > best_drop:
                    best_drop = drop
                    best_nb = nb

            if best_nb is None or best_nb in visited:
                break

            visited.add(best_nb)
            # Carry ice downhill, thinning as it goes
            carry = result.get(cur, 0.0) * 0.7
            if carry > 1.0 and balance.get(best_nb, 0) < 0:
                # Ablation zone: ice melts, reduce thickness
                ablation = balance.get(best_nb, 0) * _ICE_EQUILIBRIUM_YEARS * 0.5
                carry = max(0, carry + ablation)
            result[best_nb] = max(result.get(best_nb, 0.0), carry)
            if carry < 0.5:
                break
            cur = best_nb

    return result
