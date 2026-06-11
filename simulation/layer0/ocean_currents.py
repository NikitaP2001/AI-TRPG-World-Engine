"""Layer 0 — Ocean Surface Currents (Ekman wind-drift model).

Computes wind-driven surface currents from the geostrophic wind field,
then advects sea surface temperature and modifies coastal climate.

Design (P2.6):
  - Ekman transport: U_current = alpha * W_wind, turned by Ekman angle
  - SST advection: simplified tracer transport along current vectors
  - Coastal climate modification: warm/cold currents affect coastal T and P

All parameters are tunable for non-Earth worlds (axial_tilt, rotation_rate,
atmospheric density, etc.) via OceanCurrentParams.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

import numpy as np


# ======================================================================
# Tunable parameters for arbitrary worlds
# ======================================================================


@dataclass
class OceanCurrentParams:
    """Parameters for ocean current model.

    All values tunable for non-Earth worlds.
    """
    enabled: bool = True                     # master switch
    wind_drag_coefficient: float = 0.03       # fraction of wind speed → surface current
    ekman_turn_angle_deg: float = 45.0        # Ekman turning angle (Earth: ~45°)
    max_current_speed_ms: float = 1.5         # cap on surface current [m/s]
    sst_advection_rate: float = 0.01          # per-tick SST advection magnitude
    sst_diffusion_coeff: float = 0.001        # horizontal SST diffusion
    sst_relaxation_rate: float = 0.001        # restoring to baseline SST
    coastal_influence_radius_deg: float = 5.0 # how far inland currents affect climate
    coastal_temp_sensitivity: float = 0.3     # dT_air / dT_SST
    coastal_precip_sensitivity: float = 0.2   # d(log P) / dT_SST [1/°C]


# ======================================================================
# Ekman surface currents
# ======================================================================


def compute_ocean_currents(
    h3_ids: List[str],
    ocean_set: Set[str],
    wind_field: Dict[str, Tuple[float, float]],
    params: OceanCurrentParams,
) -> Dict[str, Tuple[float, float]]:
    """Compute surface ocean current vectors from wind stress (Ekman model).

    For each ocean cell:
      U_surface = alpha * W_10m, turned by Ekman angle
      Ekman angle: max at poles, 0 at equator (Coriolis vanishes)

    Args:
        h3_ids: All cell IDs.
        ocean_set: Set of ocean cell IDs.
        wind_field: {h3_id: (u_wind, v_wind)} — prevailing wind components.
        params: OceanCurrentParams.

    Returns:
        {h3_id: (u_current, v_current)} for ocean cells only.
    """
    import h3

    currents: Dict[str, Tuple[float, float]] = {}

    for h in h3_ids:
        if h not in ocean_set:
            continue

        wind = wind_field.get(h, (0.0, 0.0))
        wspd = math.sqrt(wind[0]**2 + wind[1]**2)
        if wspd < 0.01:
            currents[h] = (0.0, 0.0)
            continue

        # Latitude-dependent Ekman turning
        latlng = h3.cell_to_latlng(h)
        abs_lat = abs(latlng[0])
        # Turning angle decays toward equator (Coriolis → 0)
        lat_factor = min(1.0, abs_lat / 30.0)  # full angle above 30°
        ekman_turn = math.radians(params.ekman_turn_angle_deg * lat_factor)

        # Sign: NH → right (+), SH → left (-)
        sign = 1.0 if latlng[0] >= 0 else -1.0

        # Rotate wind vector by Ekman angle
        cos_a = math.cos(sign * ekman_turn)
        sin_a = math.sin(sign * ekman_turn)
        u_curr = params.wind_drag_coefficient * (wind[0] * cos_a - wind[1] * sin_a)
        v_curr = params.wind_drag_coefficient * (wind[0] * sin_a + wind[1] * cos_a)

        # Cap speed
        speed = math.sqrt(u_curr**2 + v_curr**2)
        if speed > params.max_current_speed_ms:
            scale = params.max_current_speed_ms / speed
            u_curr *= scale
            v_curr *= scale

        currents[h] = (u_curr, v_curr)

    return currents


# ======================================================================
# SST advection (simplified tracer transport)
# ======================================================================


def advect_sst(
    h3_ids: List[str],
    ocean_set: Set[str],
    ocean_currents: Dict[str, Tuple[float, float]],
    base_sst: Dict[str, float],
    params: OceanCurrentParams,
) -> Dict[str, float]:
    """Advect sea surface temperature anomaly by ocean currents.

    Simplified Eulerian step:
      dSST/dt = -u·∇SST + κ∇²SST - λ(SST - SST₀)
    
    For each ocean cell, estimate gradient from neighbours in current
    direction and step SST accordingly.

    Args:
        h3_ids: All cell IDs.
        ocean_set: Set of ocean cell IDs.
        ocean_currents: {h3_id: (u, v)} current vectors.
        base_sst: Baseline SST {h3_id: temperature_norm} (0-1).
        params: OceanCurrentParams.

    Returns:
        {h3_id: sst_anomaly} — deviation from baseline normalised 0-1.
    """
    import h3

    sst = dict(base_sst)  # start from baseline
    anomaly: Dict[str, float] = {}

    # Build neighbour cache for speed
    neighbours: Dict[str, List[str]] = {}
    for h in ocean_set:
        nhs = h3.grid_ring(h, 1) or []
        neighbours[h] = [nh for nh in nhs if nh in ocean_set]

    # One iteration of advection + diffusion + relaxation
    sst_new = dict(sst)
    for h in ocean_set:
        nhs = neighbours.get(h, [])
        if not nhs:
            anomaly[h] = 0.0
            continue

        current = ocean_currents.get(h, (0.0, 0.0))
        cspd = math.sqrt(current[0]**2 + current[1]**2)
        if cspd < 0.001:
            # No current: just relaxation + diffusion
            diff = 0.0
            if nhs:
                diff = sum(sst.get(nh, sst[h]) for nh in nhs) / len(nhs) - sst[h]
            sst_new[h] += params.sst_diffusion_coeff * diff
            sst_new[h] -= params.sst_relaxation_rate * (sst[h] - base_sst[h])
            continue

        # Find the neighbour most down-current
        latlng = h3.cell_to_latlng(h)
        clat, clon = math.radians(latlng[0]), math.radians(latlng[1])

        best_dot = -999.0
        best_nh = nhs[0]
        for nh in nhs:
            nll = h3.cell_to_latlng(nh)
            # Direction from h to nh
            dx = math.radians(nll[1] - latlng[1]) * math.cos(clat)
            dy = math.radians(nll[0] - latlng[0])
            d_norm = math.sqrt(dx**2 + dy**2) or 1e-10
            # Dot product with current direction
            dot = (current[0] * dx + current[1] * dy) / d_norm
            if dot > best_dot:
                best_dot = dot
                best_nh = nh

        # Advection: SST difference along current direction
        sst_diff = sst.get(best_nh, sst[h]) - sst[h]
        sst_new[h] += params.sst_advection_rate * sst_diff

        # Diffusion (average of all neighbours)
        diff = sum(sst.get(nh, sst[h]) for nh in nhs) / len(nhs) - sst[h]
        sst_new[h] += params.sst_diffusion_coeff * diff

        # Relaxation toward baseline
        sst_new[h] -= params.sst_relaxation_rate * (sst[h] - base_sst.get(h, sst[h]))

    # Compute anomaly from baseline
    for h in ocean_set:
        anomaly[h] = sst_new[h] - base_sst.get(h, sst_new[h])
        anomaly[h] = max(-0.3, min(0.3, anomaly[h]))  # clamp

    return anomaly


# ======================================================================
# Coastal climate modification
# ======================================================================


def apply_coastal_climate(
    land_ids: List[str],
    ocean_set: Set[str],
    sst_anomaly: Dict[str, float],
    temperature: Dict[str, float],
    precipitation: Dict[str, float],
    params: OceanCurrentParams,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Modify coastal temperature and precipitation based on SST anomalies.

    Warm currents (positive anomaly) → warmer, wetter coastal climate.
    Cold currents (negative anomaly) → cooler, drier coastal climate.

    Args:
        land_ids: List of land cell H3 IDs to potentially modify.
        ocean_set: Set of ocean cell IDs.
        sst_anomaly: {h3_id: anomaly} from advect_sst().
        temperature: {h3_id: temp_norm} — will be modified in-place.
        precipitation: {h3_id: precip_norm} — will be modified in-place.
        params: OceanCurrentParams.

    Returns:
        Modified (temperature, precipitation) dicts.
    """
    import h3
    from scipy.spatial import cKDTree
    import numpy as np

    temp = dict(temperature)
    precip = dict(precipitation)

    # Build KDTree of all land cells for fast distance queries
    land_coords = {}
    for h in land_ids:
        latlng = h3.cell_to_latlng(h)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        land_coords[h] = (
            math.cos(lat_r) * math.cos(lon_r),
            math.sin(lat_r),
            math.cos(lat_r) * math.sin(lon_r),
        )
    if not land_coords:
        return temp, precip

    land_pts = np.array(list(land_coords.values()), dtype=np.float64)
    land_keys = list(land_coords.keys())
    tree = cKDTree(land_pts)
    # Convert radius from degrees to 3D chord distance
    radius_rad = math.radians(params.coastal_influence_radius_deg)
    radius_chord = 2.0 * math.sin(radius_rad / 2.0)

    # For each ocean cell with non-negligible anomaly, find nearby land
    for h in ocean_set:
        anom = sst_anomaly.get(h, 0.0)
        if abs(anom) < 0.005:
            continue

        latlng = h3.cell_to_latlng(h)
        lat_r = math.radians(latlng[0])
        lon_r = math.radians(latlng[1])
        qpt = np.array([[
            math.cos(lat_r) * math.cos(lon_r),
            math.sin(lat_r),
            math.cos(lat_r) * math.sin(lon_r),
        ]], dtype=np.float64)

        idxs = tree.query_ball_point(qpt[0], r=radius_chord)
        for idx in idxs:
            nh = land_keys[idx]
            # Compute actual distance for weighting
            nll = h3.cell_to_latlng(nh)
            dlat = math.radians(nll[0] - latlng[0])
            dlon = math.radians(nll[1] - latlng[1])
            a = (math.sin(dlat/2)**2 +
                 math.cos(lat_r) * math.cos(math.radians(nll[0])) *
                 math.sin(dlon/2)**2)
            dist = math.degrees(2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

            if dist > params.coastal_influence_radius_deg:
                continue

            weight = math.exp(-dist / params.coastal_influence_radius_deg)

            # Temperature modification
            temp_mod = anom * params.coastal_temp_sensitivity * weight
            temp[nh] = max(0.0, min(1.0, temp.get(nh, 0.5) + temp_mod))

            # Precipitation modification
            precip_mod = math.exp(anom * params.coastal_precip_sensitivity * weight)
            precip[nh] = max(0.0, min(1.0, precip.get(nh, 0.5) * precip_mod))

    return temp, precip
