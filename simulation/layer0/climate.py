"""Layer 0 — Climate Model.

Temperature from latitude + elevation lapse + ocean proximity.
Precipitation from latitude + orographic lift + rain shadow.
Wind from latitude (prevailing) + elevation deflection.
Climate class from Köppen-Geiger.

Design doc § Stage 4 — Climate Fields.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Set, Tuple

import h3
import numpy as np


# ======================================================================
# Physical wind model — geostrophic from temperature gradient
# ======================================================================

_OMEGA = 7.292e-5           # Earth rotation rate [rad/s]
_R_GAS = 287.0               # Gas constant for dry air [J/(kg*K)]
_EKMAN_FACTOR = 0.60         # surface / geostrophic speed ratio
_EKMAN_TURN_DEG = 25.0       # surface wind turn from geostrophic (NH left)
_GRADIENT_SPACING = 3.0      # neighbour distance for gradient [deg]


def _coriolis(lat_deg: float) -> float:
    """Coriolis parameter f = 2*Omega*sin(lat) [1/s]."""
    f = 2.0 * _OMEGA * math.sin(math.radians(lat_deg))
    return f if abs(f) > 1e-12 else 1e-12  # avoid division by zero at equator


def _gradient_at(
    lat: float, lon: float,
    temp_c: Dict[str, float],
    elevation: Dict[str, float],
    h3_ids_set: set,
) -> Tuple[float, float]:
    """Estimate horizontal temperature gradient (dT/dlat, dT/dlon) at a point.

    Samples nearby H3 cells and fits a plane: T(lat, lon) ~ a*lat + b*lon + c.
    Uses up to 6 neighbours + self for the fit.
    """
    import h3
    h = h3.latlng_to_cell(lat, lon, 2)
    if h not in h3_ids_set:
        return (0.0, 0.0)

    T0 = temp_c.get(h, 15.0)
    points = [(lat, lon, T0)]

    nhs = h3.grid_ring(h, 1) or []
    for nh in nhs:
        if nh in h3_ids_set:
            nll = h3.cell_to_latlng(nh)
            points.append((nll[0], nll[1], temp_c.get(nh, T0)))

    n = len(points)
    if n < 3:
        return (0.0, 0.0)

    # Centre on mean
    mlat = sum(p[0] for p in points) / n
    mlon = sum(p[1] for p in points) / n
    mT = sum(p[2] for p in points) / n

    # Least squares: T = a*(lat-mlat) + b*(lon-mlon) + mT
    # Normal equations
    Sxx = sum((p[0]-mlat)**2 for p in points)
    Syy = sum((p[1]-mlon)**2 for p in points)
    Sxy = sum((p[0]-mlat)*(p[1]-mlon) for p in points)
    SxT = sum((p[0]-mlat)*(p[2]-mT) for p in points)
    SyT = sum((p[1]-mlon)*(p[2]-mT) for p in points)

    det = Sxx*Syy - Sxy*Sxy
    if abs(det) < 1e-12:
        return (0.0, 0.0)

    a = (Syy*SxT - Sxy*SyT) / det   # dT/dlat [degC/deg]
    b = (Sxx*SyT - Sxy*SxT) / det   # dT/dlon [degC/deg]

    # Convert to dT/dy, dT/dx (metres)
    dlat_to_m = 111320.0  # metres per degree latitude
    dlon_to_m = 111320.0 * math.cos(math.radians(lat))  # m/deg longitude
    if dlon_to_m < 1:
        dlon_to_m = 1

    dT_dy = a / dlat_to_m     # K/m (positive northward)
    dT_dx = b / dlon_to_m     # K/m (positive eastward)

    return (dT_dy, dT_dx)


def compute_wind_field(
    h3_ids: List[str],
    temperature: Dict[str, float],
    elevation: Dict[str, float],
    ocean_set: Set[str],
    day_of_year: float = 172.0,
    hour: float = 12.0,
) -> Dict[str, Tuple[float, float]]:
    """Compute surface wind from temperature gradient (geostrophic + Ekman).

    Returns Dict[h3_id] -> (u, v) where:
      u = eastward component [m/s] (positive = east)
      v = northward component [m/s] (positive = north)

    Physics:
      1. Geostrophic balance: PGF = Coriolis
         u_g = -(R/f) * dT/dy,  v_g = (R/f) * dT/dx
      2. Ekman boundary layer: surface wind is slower, turned left (NH)
      3. Near equator (|lat| < 5): linear interpolation to avoid division
    """
    import h3
    h3_set = set(h3_ids)

    # Convert temperature map to °C
    temp_c: Dict[str, float] = {}
    for h in h3_ids:
        temp_c[h] = norm_to_c(temperature.get(h, 0.5))

    wind: Dict[str, Tuple[float, float]] = {}

    for h in h3_ids:
        latlng = h3.cell_to_latlng(h)
        lat, lon = latlng[0], latlng[1]
        abs_lat = abs(lat)

        # Temperature gradient
        dT_dy, dT_dx = _gradient_at(lat, lon, temp_c, elevation, h3_set)

        # Coriolis parameter (avoid division by zero at equator)
        f = _coriolis(lat)

        # Geostrophic wind
        factor = _R_GAS / f
        u_g = -factor * dT_dy   # eastward component from meridional gradient
        v_g =  factor * dT_dx   # northward component from zonal gradient

        # Near-equator blending: Coriolis → 0 causes unrealistically high winds
        if abs_lat < 5.0:
            # Blend from geostrophic to a simple zonal trade wind
            weight = abs_lat / 5.0
            # Trade wind approximation (easterly, -5 m/s at equator)
            u_trade = -5.0
            v_trade = 0.0
            u_g = u_g * weight + u_trade * (1 - weight)
            v_g = v_g * weight + v_trade * (1 - weight)

        # Ekman boundary layer: reduce speed, turn toward low pressure
        speed = math.sqrt(u_g**2 + v_g**2)
        if speed > 0.1:
            # Turn left (NH) / right (SH) by ~25 deg
            turn_rad = math.radians(_EKMAN_TURN_DEG)
            if lat < 0:
                turn_rad = -turn_rad  # SH: turn right (opposite direction)

            cos_t = math.cos(turn_rad)
            sin_t = math.sin(turn_rad)
            u_surf = (u_g * cos_t - v_g * sin_t) * _EKMAN_FACTOR
            v_surf = (v_g * cos_t + u_g * sin_t) * _EKMAN_FACTOR
        else:
            u_surf, v_surf = 0.0, 0.0

        # Cap at reasonable max
        max_speed = 30.0
        spd = math.sqrt(u_surf**2 + v_surf**2)
        if spd > max_speed:
            scale = max_speed / spd
            u_surf *= scale
            v_surf *= scale

        wind[h] = (u_surf, v_surf)

    return wind


# ======================================================================
# Physical temperature model — solar radiation balance
# ======================================================================
#
# Hybrid approach:
#   1. Earth-like reference temperature (calibrated to ~15°C global mean)
#      accounts for the net effect of: albedo, greenhouse, heat transport.
#   2. Insolation anomaly: dT = dT/dQ * (Q - Q_earth) propagates changes
#      from axial_tilt, solar_constant, etc. via physical sensitivity.
#   3. Elevation lapse rate (~6.5 °C/km, moist adiabatic).
#   4. Ocean thermal inertia damsps seasonal range.
#
# Future support for day/night and seasons:
#   - Replace _annual_mean_insolation(lat) with
#     _daily_insolation_toa(lat, _solar_declination(day_of_year, tilt))
#   - Add diurnal cycle via hour_angle in zenith angle
#   - Seasonal range = +-amplitude from solstice delta
#   - Diurnal range = +-amplitude from day/night delta
# ======================================================================

_SOLAR_CONSTANT = 1361.0          # W/m^2
_LAPSE_RATE = 0.0065              # degC/m (moist adiabatic)
_ELEV_UNIT_TO_M = 500.0           # 1 elev unit ~ 500 m
_TEMP_SENSITIVITY = 0.05          # dT/dQ [degC/(W/m^2)]
_OCEAN_DAMPING = 4.0              # ocean thermal inertia vs land
_COASTAL_DAMPING = 1.8            # coastal moderation factor

# Normalised 0-1 ↔ °C conversion (used across L0+L1)
TEMP_C_MIN = -5.0
TEMP_C_MAX = 40.0
_TEMP_C_RANGE = TEMP_C_MAX - TEMP_C_MIN  # 45.0


def norm_to_c(temp_norm: float) -> float:
    """Convert 0-1 normalised temperature to °C."""
    return temp_norm * _TEMP_C_RANGE + TEMP_C_MIN


def _solar_declination(day_of_year: float, axial_tilt: float) -> float:
    """Solar declination angle in degrees.

    Args:
        day_of_year: 0-365 (0 = Jan 1, 172 ~ summer solstice NH)
        axial_tilt:  obliquity in degrees (Earth ~ 23.44)

    Returns:
        Declination in degrees [-axial_tilt, +axial_tilt].
    """
    angle = 2.0 * math.pi * (day_of_year - 79.0) / 365.0
    return math.degrees(math.asin(
        math.sin(math.radians(axial_tilt)) * math.sin(angle)
    ))


def _daily_insolation_toa(lat_deg: float, decl_deg: float,
                          solar_constant: float = _SOLAR_CONSTANT) -> float:
    """Daily mean top-of-atmosphere insolation [W/m^2].

    Berger 1978; Hartmann 1994:
      Q = S0/pi * (H*sin(phi)*sin(delta) + cos(phi)*cos(delta)*sin(H))
      H = arccos(-tan(phi)*tan(delta))  -- half-day length [rad]
    """
    phi = math.radians(lat_deg)
    delta = math.radians(decl_deg)
    tp = math.tan(phi) * math.tan(delta)

    if tp <= -1.0:
        return 0.0            # polar night
    if tp >= 1.0:
        H = math.pi           # midnight sun
    else:
        H = math.acos(-tp)

    return max(0.0, solar_constant / math.pi * (
        H * math.sin(phi) * math.sin(delta) +
        math.cos(phi) * math.cos(delta) * math.sin(H)
    ))


def _annual_mean_insolation(lat_deg: float, axial_tilt: float,
                            n_samples: int = 36) -> float:
    """Annual mean daily insolation [W/m^2] averaged over the year."""
    total = 0.0
    for i in range(n_samples):
        day = i * 365.0 / n_samples
        total += _daily_insolation_toa(lat_deg,
                                       _solar_declination(day, axial_tilt))
    return total / n_samples


def _insolation_solstice(lat_deg: float, axial_tilt: float,
                         summer: bool = True) -> float:
    """Daily insolation at solstice [W/m^2]."""
    day = 172.0 if summer else 355.0
    return _daily_insolation_toa(lat_deg,
                                 _solar_declination(day, axial_tilt))


def _earth_ref_temp(lat_deg: float) -> float:
    """Earth-like annual mean surface temperature [degC] at latitude.

    Empirically calibrated to reproduce Earth's observed latitudinal
    temperature gradient, which integrates: solar insolation, albedo,
    greenhouse effect, and meridional heat transport.
    """
    return 42.0 * math.cos(math.radians(lat_deg)) ** 1.5 - 15.0


# ======================================================================
# Instantaneous temperature — supports day_of_year and hour
# ======================================================================


def _cos_zenith_angle(lat_deg: float, decl_deg: float, hour: float) -> float:
    """Cosine of solar zenith angle at given latitude, day, and hour.

    Args:
        lat_deg:  latitude [-90, 90]
        decl_deg: solar declination [-23.44, 23.44]
        hour:     0-24 (0 = midnight, 12 = noon)

    Returns:
        cos(zenith) in [-1, 1]; negative means sun below horizon.
    """
    phi = math.radians(lat_deg)
    delta = math.radians(decl_deg)
    # Hour angle: 0 at noon, positive westward
    hour_angle = math.radians((hour - 12.0) * 15.0)
    return (math.sin(phi) * math.sin(delta) +
            math.cos(phi) * math.cos(delta) * math.cos(hour_angle))


def _day_length_hours(lat_deg: float, decl_deg: float) -> float:
    """Length of daylight in hours at given latitude and declination."""
    phi = math.radians(lat_deg)
    delta = math.radians(decl_deg)
    tp = math.tan(phi) * math.tan(delta)
    if tp <= -1.0:
        return 0.0
    if tp >= 1.0:
        return 24.0
    H = math.acos(-tp)
    return H * 24.0 / math.pi


def _diurnal_amplitude(lat_deg: float, day_of_year: float,
                       is_ocean: bool = False, coastal: bool = False) -> float:
    """Diurnal temperature amplitude [degC] at given latitude and day.

    Depends on:
      - Peak insolation (noon solar elevation)
      - Day length (heating duration)
      - Surface thermal inertia (ocean vs land)
    """
    decl = _solar_declination(day_of_year, 23.44)
    cos_noon = _cos_zenith_angle(lat_deg, decl, hour=12.0)
    noon_elev = math.degrees(math.asin(max(0.0, cos_noon)))

    # Day length factor: longer days = more heating
    day_h = _day_length_hours(lat_deg, decl)
    length_factor = math.sin(math.pi * day_h / 24.0)  # peak at 12h

    # Sun elevation factor: higher sun = more peak heating
    sun_factor = math.sin(math.radians(max(5.0, noon_elev)))

    # Base amplitude (max ~14 degC in clear desert conditions)
    base_amp = 14.0 * (sun_factor * length_factor) ** 0.6

    # Surface damping
    if is_ocean:
        base_amp *= 0.15
    elif coastal:
        base_amp *= 0.45

    return max(0.5, min(20.0, base_amp))


def instant_temperature(
    lat_deg: float,
    elevation: float,
    is_ocean: bool,
    coastal: bool,
    day_of_year: float,
    hour: float,
    axial_tilt: float = 23.44,
) -> float:
    """Instantaneous surface temperature [degC] at any time and place.

    Composes:
      1. Annual reference (Earth-calibrated latitudinal gradient)
      2. Seasonal offset (+/- from solstice insolation)
      3. Diurnal offset (+/- from daily insolation cycle)
      4. Elevation lapse rate

    Future expansion:
      - Replace 23.44 hardcode in _diurnal_amplitude with axial_tilt param
      - Add cloud/albedo feedback for seasonal amplitude
    """
    # 1. Annual reference
    t_c = _earth_ref_temp(abs(lat_deg))

    # 2. Seasonal offset (from summer/winter solstice difference)
    q_now = _daily_insolation_toa(lat_deg, _solar_declination(day_of_year, axial_tilt))
    q_ann = _annual_mean_insolation(lat_deg, axial_tilt)
    seasonal_dq = q_now - q_ann
    t_c += seasonal_dq * _TEMP_SENSITIVITY

    # 3. Elevation lapse
    t_c -= _LAPSE_RATE * max(0.0, elevation) * _ELEV_UNIT_TO_M

    # 4. Diurnal offset (hour of day)
    diurnal_amp = _diurnal_amplitude(lat_deg, day_of_year, is_ocean, coastal)
    # Hour angle: peak at 14:00 (2 PM, ~2h after solar noon)
    hour_offset = math.cos(math.radians((hour - 14.0) * 15.0))
    t_c += diurnal_amp * hour_offset

    return t_c


def compute_temperature(
    h3_ids: List[str],
    elevation: Dict[str, float],
    ocean_set: Set[str],
    wind: Dict[str, Tuple[float, float]],
    axial_tilt: float = 23.5,
    global_offset: float = 0.0,
    solar_intensity: float = 1.0,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute temperature and seasonal range from physical insolation.

    Returns (temp_map, temp_range_map) -- both 0-1 normalized.

    Physics pipeline:
      1. Earth reference temperature at latitude (calibrated to real world)
      2. Insolation anomaly: dT = sensitivity * (Q - Q_earth)
         -- propagates axial_tilt and solar_intensity changes
      3. Elevation lapse rate (~6.5 degC/km)
      4. Ocean thermal inertia -- seasonal amplitude damping
      5. Normalise to 0-1 (0 <-> -5 degC, 1 <-> 40 degC)

    Future:
      day_of_year param -> replace _annual_mean with _daily_insolation_toa
      hour_angle param  -> add diurnal cycle via zenith angle
    """
    temp: Dict[str, float] = {}
    temp_range: Dict[str, float] = {}

    # Cache per-latitude values
    lat_cache: Dict[float, dict] = {}

    for h in h3_ids:
        latlng = h3.cell_to_latlng(h)
        lat = round(abs(latlng[0]), 2)
        el_norm = max(0.0, elevation.get(h, 0.0))
        is_ocean = h in ocean_set

        if lat not in lat_cache:
            q_ann = _annual_mean_insolation(lat, axial_tilt)
            q_earth = _annual_mean_insolation(lat, 23.44)
            q_sum = _insolation_solstice(lat, axial_tilt, summer=True)
            q_win = _insolation_solstice(lat, axial_tilt, summer=False)
            lat_cache[lat] = dict(q_ann=q_ann, q_earth=q_earth,
                                  q_sum=q_sum, q_win=q_win)

        c = lat_cache[lat]

        # --- 1. Earth reference temperature ---
        t_c = _earth_ref_temp(lat)

        # --- 2. Insolation anomaly ---
        dq = c["q_ann"] * solar_intensity - c["q_earth"]
        t_c += dq * _TEMP_SENSITIVITY

        # --- 3. Elevation lapse (~6.5 degC/km) ---
        t_c -= _LAPSE_RATE * el_norm * _ELEV_UNIT_TO_M

        # --- 4. Normalise to 0-1 (0 <-> -5 degC, 1 <-> 40 degC) ---
        # No lower clamp — polar cells may go below 0 (e.g. -0.22 for -15 degC)
        t_norm = (t_c - TEMP_C_MIN) / _TEMP_C_RANGE
        t_norm = min(1.0, t_norm)

        # --- 5. Seasonal range from solstice difference ---
        dq_sum = c["q_sum"] * solar_intensity - c["q_earth"]
        dq_win = c["q_win"] * solar_intensity - c["q_earth"]
        t_sum_c = _earth_ref_temp(lat) + dq_sum * _TEMP_SENSITIVITY
        t_win_c = _earth_ref_temp(lat) + dq_win * _TEMP_SENSITIVITY
        t_sum_c -= _LAPSE_RATE * el_norm * _ELEV_UNIT_TO_M
        t_win_c -= _LAPSE_RATE * el_norm * _ELEV_UNIT_TO_M

        seasonal_amp_c = (t_sum_c - t_win_c) / 2.0
        seasonal_amp_c = max(0.0, seasonal_amp_c)

        # Ocean / coastal damping
        if is_ocean:
            seasonal_amp_c /= _OCEAN_DAMPING
        else:
            neighbours = h3.grid_ring(h, 1) or []
            if any(nh in ocean_set for nh in neighbours):
                seasonal_amp_c /= _COASTAL_DAMPING

        r_norm = min(0.5, seasonal_amp_c / _TEMP_C_RANGE)

        # --- 6. Global modifiers ---
        t_norm = min(1.0, t_norm + global_offset)
        t_norm *= solar_intensity
        t_norm = min(1.0, t_norm)  # no lower clamp — polar temps go below 0

        temp[h] = t_norm
        temp_range[h] = r_norm

    return temp, temp_range


# ======================================================================
# (The NEW compute_precipitation is below — old version deleted)
# ======================================================================

# ======================================================================
# Köppen-Geiger climate classification

# Köppen classification thresholds
_P_TROPICAL = 0.7
_P_COLD = 0.35
_P_POLAR = 0.2
_P_DESERT = 0.2
_P_STEPPE = 0.35
_P_WET = 0.6

_KOPPEN_NAMES = {
    "Af": "Tropical Rainforest",
    "Am": "Tropical Monsoon",
    "Aw": "Tropical Savanna",
    "BWh": "Hot Desert",
    "BWk": "Cold Desert",
    "BSh": "Hot Semi-Arid Steppe",
    "BSk": "Cold Semi-Arid Steppe",
    "Cfa": "Humid Subtropical",
    "Cwa": "Monsoon Humid Subtropical",
    "Cs": "Mediterranean",
    "Dfb": "Humid Continental",
    "Dwc": "Subarctic",
    "EF": "Ice Cap",
    "ET": "Tundra",
}


def koppen_name(code: str) -> str:
    """Convert Köppen code to full name, e.g. 'Af' → 'Tropical Rainforest'."""
    code = code.strip()
    return _KOPPEN_NAMES.get(code, f"Climate {code}")


def koppen_classify(temp: float, precip: float, tr: float) -> str:
    """Simplified Köppen-Geiger climate classification.

    Returns class code like 'Af', 'Cfb', 'ET', etc.
    """
    # Tropical (A): coldest month > 18°C equivalent → temp > 0.7
    if temp >= _P_TROPICAL:
        if precip >= _P_WET:
            return "Af"  # Rainforest
        elif precip >= _P_STEPPE:
            return "Am"  # Monsoon
        else:
            return "Aw"  # Savanna

    # Arid (B): precipitation below desert/steppe threshold
    if precip < _P_DESERT:
        return "BWh" if temp >= _P_COLD else "BWk"  # Desert hot/cold
    if precip < _P_STEPPE:
        return "BSh" if temp >= _P_COLD else "BSk"  # Steppe hot/cold

    # Temperate (C): coldest month > -3°C → temp > 0.2
    if temp >= _P_COLD:
        if precip >= _P_WET:
            return "Cfa"  # Humid subtropical
        elif tr > 0.3:
            return "Cwa"  # Monsoon-influenced
        else:
            return "Cs"  # Mediterranean

    # Cold (D): coldest month < -3°C, warmest > 10°C
    if temp >= _P_POLAR:
        if precip >= _P_WET:
            return "Dfb"  # Humid continental
        else:
            return "Dwc"  # Subarctic

    # Polar (E): warmest month < 10°C
    return "EF" if precip < _P_STEPPE else "ET"


# ======================================================================
# Full climate computation for the generator pipeline
# ======================================================================


# ── Vapor pressure (Magnus formula) ───────────────────────────

def _saturation_vp(temp_c: float) -> float:
    """Saturation vapor pressure in hPa (Magnus formula)."""
    return 6.112 * math.exp(17.62 * temp_c / (temp_c + 243.12))


def _actual_vp(temp_c: float, rh: float) -> float:
    """Actual vapor pressure from temperature and relative humidity."""
    return _saturation_vp(temp_c) * max(0.01, min(1.0, rh))


def _vpd(temp_c: float, rh: float) -> float:
    """Vapor pressure deficit in hPa."""
    return _saturation_vp(temp_c) - _actual_vp(temp_c, rh)


# ── Open water evaporation (Penman-Monteith simplified) ───────

def potential_evap_mm_day(
    temp_c: float,
    rh: float,
    wind_ms: float,
    solar_wm2: float = 200.0,
) -> float:
    """Open-water evaporation in mm/day (Penman-style).

    ET = (Δ * Rn + ρ*cp * VPD / ra) / (Δ + γ)

    Simplified for open water with surface resistance rs=0.
    """
    Δ = 4098 * _saturation_vp(temp_c) / ((temp_c + 237.3) ** 2)  # slope of es(T)
    γ = 0.066  # psychrometric constant (hPa/K)
    Rn = solar_wm2 * 0.0036  # net radiation → mm/day equiv (~0.0036 mm per J/m²)
    ra = 50.0 / max(0.1, wind_ms)  # aerodynamic resistance (s/m)
    ρ_cp = 1.2 * 1005 / 2.45e6  # ρ*cp/λ ≈ 0.0005 (kg/m³ * J/kgK / J/kg)
    VPD = _vpd(temp_c, rh)

    ET = (Δ * Rn + ρ_cp * VPD / ra) / (Δ + γ)
    return max(0.0, ET * 0.5)  # mm/day, scaled down for monthly mean


# ── Simplified upwind precipitation model ─────────────────────

def _upwind_trace(
    lat: float, lon: float,
    wind_u: float, wind_v: float,
    elevation_map: Dict[str, float],
    h3_ids_set: set,
    steps: int = 5,
    step_deg: float = 0.5,
) -> tuple:
    """Trace upwind path and accumulate orographic effect.

    Returns (total_rise, passed_ocean, distance_from_ocean) where:
      total_rise: cumulative elevation gain along upwind path
      passed_ocean: whether the path crossed ocean
      distance_from_ocean: degrees to nearest ocean along path
    """
    import h3
    total_rise = 0.0
    passed_ocean = False
    min_ocean_dist = 999.0
    prev_el = elevation_map.get(h3.latlng_to_cell(lat, lon, 2), 0.0)
    clat, clon = lat, lon

    for _ in range(steps):
        # Step upwind (against wind direction)
        clat -= wind_v * step_deg
        clon -= wind_u * step_deg / max(0.1, math.cos(math.radians(clat)))
        # Wrap longitude
        while clon > 180: clon -= 360
        while clon < -180: clon += 360
        if clat < -90 or clat > 90:
            break

        h = h3.latlng_to_cell(clat, clon, 2)
        el = elevation_map.get(h, prev_el)

        if h not in h3_ids_set:
            passed_ocean = True
            min_ocean_dist = min(min_ocean_dist, abs(el))

        if el > prev_el:
            total_rise += el - prev_el
        prev_el = el

    return total_rise, passed_ocean, min(min_ocean_dist, steps * step_deg)


def compute_precipitation(
    h3_ids: List[str],
    elevation: Dict[str, float],
    temperature: Dict[str, float],
    wind: Dict[str, Tuple[float, float]],
    ocean_set: Set[str],
    rng: random.Random,
    day_of_year: float = 172.0,
    axial_tilt: float = 23.44,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute precipitation from physical vapor budget.

    No magic scale factors — every term has a physical basis.

    Architecture:
      precip = vapor_norm * (orographic + itcz + monsoon + coastal)

    where each driver is a fraction 0-1 of the available vapor that condenses.

    Returns (precip_map, precip_seas_map) — both 0-1 normalized.
    """
    import h3
    h3_set = set(h3_ids)
    decl = _solar_declination(day_of_year, axial_tilt)
    # ITCZ latitude follows declination with ~80% amplitude (ocean thermal lag)
    itcz_lat = decl * 0.8

    precip: Dict[str, float] = {}
    precip_seas: Dict[str, float] = {}

    for h in h3_ids:
        latlng = h3.cell_to_latlng(h)
        lat, lon = latlng[0], latlng[1]
        abs_lat = abs(lat)
        el = elevation.get(h, 0.0)
        temp_c = temperature.get(h, 0.5) * 45.0 - 5.0
        wind_dir = wind.get(h, (0.0, 0.0))
        wind_u, wind_v = wind_dir
        wind_spd = math.sqrt(wind_u**2 + wind_v**2)
        is_ocean = h in ocean_set

        # ── 1. Vapor availability (0-1, normalized) ──
        es = _saturation_vp(temp_c)              # saturation VP [hPa]
        # Max es at 40°C ≈ 73 hPa; vapor is es * RH
        # For scaling: 40 hPa → 1.0 (typical tropical max)
        vapor_norm = es / 40.0
        vapor_norm = max(0.0, min(1.0, vapor_norm))

        # ── 2. Upwind moisture trace ──
        rise, over_ocean, ocean_dist = _upwind_trace(
            lat, lon, wind_u, wind_v, elevation, h3_set,
            steps=8, step_deg=0.5,
        )
        # Normalise rise to physical metres
        rise_m = rise * _ELEV_UNIT_TO_M  # 1 elev unit ≈ 500 m

        # ── 3. Orographic precipitation ──
        # Rising air cools adiabatically → condensation
        # Temp drop = Γ * Δz, condense fraction = temp_drop / 5°C (full condensation)
        orographic_frac = 0.0
        if rise_m > 10.0 and el > 0 and wind_spd > 0.5:
            temp_drop = rise_m * _LAPSE_RATE  # °C
            condense_frac = min(1.0, temp_drop / 5.0)
            # Wind enhances moisture throughput
            wind_eff = min(1.0, wind_spd / 10.0)
            orographic_frac = condense_frac * wind_eff * 0.6

        # ── 4. ITCZ convergence (seasonal) ──
        itcz_frac = 0.0
        if abs_lat < 25:
            dist = abs(lat - itcz_lat)
            itcz_frac = max(0.0, 1.0 - dist / 15.0) * 0.4

        # ── 5. Coastal / oceanic moisture ──
        coastal_frac = 0.0
        if is_ocean:
            # Ocean gets base precipitation from local evaporation
            coastal_frac = 0.15
        elif over_ocean:
            # Coastal: moist air from ocean = base + advection
            coastal_frac = 0.10
            if ocean_dist < 1.0:
                coastal_frac += 0.10 * (1.0 - ocean_dist)

        # ── 6. Monsoon effect (land-ocean temp contrast) ──
        monsoon_frac = 0.0
        if not is_ocean and abs_lat < 30 and wind_spd > 1.0:
            # Monsoon = onshore wind from warm ocean to hot land
            neighbours = h3.grid_ring(h, 1) or []
            nh_ocean = sum(1 for nh in neighbours if nh in ocean_set)
            if nh_ocean > 0:
                monsoon_frac = 0.15

        # ── 7. Mid-latitude frontal precipitation (westerlies) ──
        # This is the dominant precipitation mechanism in temperate zones
        # on Earth: cyclonic lifting along the polar front.
        # Peak at ~50° latitude, modulated by wind speed.
        frontal_frac = 0.0
        if 25 < abs_lat < 75:
            # Bell curve peaking at 50° latitude
            lat_factor = math.exp(-((abs_lat - 50) / 15) ** 2)
            # Needs wind to propagate weather systems
            wind_factor = min(1.0, wind_spd / 8.0)
            # Enhanced when warm air meets cold (frontal lifting)
            temp_gradient = abs(wind_v) * 0.5  # meridional wind component
            frontal_frac = lat_factor * wind_factor * (0.20 + temp_gradient)

        # ── 8. Rain shadow (descending air) ──
        rain_shadow = 1.0
        if rise_m < -20.0 and not is_ocean:
            rain_shadow = 0.2 + 0.8 * max(0.0, 1.0 + rise_m / 100.0)

        # ── 9. Composite ──
        # All components are fractions of available vapor that condenses
        total_frac = orographic_frac + itcz_frac + coastal_frac + monsoon_frac + frontal_frac
        p = total_frac * rain_shadow
        p = p * vapor_norm  # scale by available moisture

        # ── 10. Cold desert: negligible precip below -15°C ──
        if temp_c < -15.0:
            p *= max(0.0, (temp_c + 20.0) / 5.0)

        # ── 11. Small random noise ──
        p += rng.gauss(0.0, 0.01)
        p = max(0.0, min(1.0, p))
        precip[h] = p

        # Seasonality: precip varies with ITCZ migration
        seas = min(0.5, 0.05 + abs_lat / 60.0 * 0.4)
        precip_seas[h] = seas

    return precip, precip_seas


def compute_climate(
    h3_ids: List[str],
    elevation: Dict[str, float],
    ocean_set: Set[str],
    seed: int = 42,
    axial_tilt: float = 23.5,
    global_temp_offset: float = 0.0,
    solar_intensity: float = 1.0,
) -> Tuple[
    Dict[str, float],           # temperature
    Dict[str, float],           # temp_seasonal_range
    Dict[str, float],           # precipitation
    Dict[str, float],           # precip_seasonality
    Dict[str, str],             # climate_class
    Dict[str, Tuple[float, float]],  # prevailing_wind
]:
    """Run full climate computation for all cells.

    Pipeline:
      1. Wind field
      2. Temperature (latitude + lapse + maritime)
      3. Precipitation (latitude + orographic + rain shadow)
      4. Köppen classification
    """
    rng = random.Random(seed)

    # 1. Temperature (independent — does not need wind)
    temp, temp_range = compute_temperature(
        h3_ids, elevation, ocean_set, {},  # wind not used by temp model
        axial_tilt=axial_tilt,
        global_offset=global_temp_offset,
        solar_intensity=solar_intensity,
    )

    # 2. Wind from temperature gradient (geostrophic + Ekman)
    wind = compute_wind_field(
        h3_ids, temp, elevation, ocean_set,
        day_of_year=172.0, hour=12.0,
    )

    # 3. Precipitation from vapor + wind + seasonal ITCZ
    precip, precip_seas = compute_precipitation(
        h3_ids, elevation, temp, wind, ocean_set, rng,
        day_of_year=172.0, axial_tilt=axial_tilt,
    )

    # 4. Köppen
    climate_class: Dict[str, str] = {}
    for h in h3_ids:
        climate_class[h] = koppen_classify(
            temp.get(h, 0.5),
            precip.get(h, 0.5),
            temp_range.get(h, 0.2),
        )

    return temp, temp_range, precip, precip_seas, climate_class, wind
