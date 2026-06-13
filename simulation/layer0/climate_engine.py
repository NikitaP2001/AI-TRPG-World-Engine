"""ClimateEngine — orbital (Milankovitch) + CO2 + albedo climate evolution.

Drives long-term climate change by modulating insolation, greenhouse
gases, and surface albedo. Writes updated temperature and precipitation
to WorldState continuous fields.

Architecture:
  Inputs:  time, ice/snow, elevation, existing temperature/precip
  Physics: orbital mechanics → insolation → temperature → precipitation
  Outputs: temperature, precipitation (ContinuousFields)

All other modules (glaciers, vegetation, soil, erosion) automatically
respond to the new temperature/precipitation fields.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..world_state import WorldState

# Orbital parameters (Earth-like)
_OBLIQUITY_MEAN = 23.44         # degrees
_OBLIQUITY_AMPLITUDE = 1.2      # ±1.2° over 41 kyr
_OBLIQUITY_PERIOD = 41000.0     # years
_PRECESSION_PERIOD = 26000.0    # years
_ECCENTRICITY_PERIOD_1 = 100000.0  # years
_ECCENTRICITY_PERIOD_2 = 413000.0  # years
_ECCENTRICITY_MEAN = 0.028
_ECCENTRICITY_AMPLITUDE = 0.012

# Climate sensitivity
_TEMP_SENSITIVITY_PER_WM2 = 0.05   # °C per W/m² insolation change
_CO2_DOUBLING_TEMP = 3.0           # °C per CO₂ doubling (ECS)
_CO2_REFERENCE = 280.0             # pre-industrial ppm

# CO2 cycle
_VOLCANIC_CO2_RATE = 0.01          # ppm per year (background outgassing)
_WEATHERING_CO2_TIMESCALE = 200000.0  # years for silicate weathering to halve CO2 anomaly


class ClimateEngine:
    """Drives long-term climate evolution via Milankovitch + CO2 + albedo."""

    def __init__(self, seed: int = 42):
        self._co2_ppm = _CO2_REFERENCE
        self._axial_tilt = _OBLIQUITY_MEAN
        self._rng = random.Random(seed)

    # ── Orbital parameters ────────────────────────────────────────

    def _obliquity(self, year: float) -> float:
        """Axial tilt at given year (41 kyr cycle)."""
        phase = 2.0 * math.pi * year / _OBLIQUITY_PERIOD
        return _OBLIQUITY_MEAN + _OBLIQUITY_AMPLITUDE * math.sin(phase)

    def _precession_index(self, year: float) -> float:
        """Precession index (0-1): 0 = perihelion in NH summer."""
        phase = 2.0 * math.pi * year / _PRECESSION_PERIOD
        return (math.sin(phase) + 1.0) / 2.0

    def _eccentricity(self, year: float) -> float:
        """Orbital eccentricity at given year."""
        phase1 = 2.0 * math.pi * year / _ECCENTRICITY_PERIOD_1
        phase2 = 2.0 * math.pi * year / _ECCENTRICITY_PERIOD_2
        return _ECCENTRICITY_MEAN + _ECCENTRICITY_AMPLITUDE * (
            math.sin(phase1) * 0.6 + math.sin(phase2) * 0.4
        )

    def _insolation_anomaly(self, lat_deg: float, year: float) -> float:
        """Insolation anomaly [W/m²] from orbital variations at latitude.

        Combines obliquity + precession effects.
        Positive = more insolation = warming.
        """
        obl = self._obliquity(year)
        ecc = self._eccentricity(year)
        prec = self._precession_index(year)

        # Obliquity effect: higher tilt → more polar insolation
        obl_anom = (obl - _OBLIQUITY_MEAN) * 0.3 * abs(lat_deg) / 45.0

        # Precession + eccentricity: modulates seasonal contrast
        # Max effect at mid-latitudes
        lat_factor = math.sin(math.radians(lat_deg * 2.0))
        prec_anom = (prec - 0.5) * 10.0 * lat_factor * (1.0 + ecc * 2.0)

        return obl_anom + prec_anom

    # ── CO2 cycle ────────────────────────────────────────────────

    def _update_co2(self, dt_years: float, global_temp_c: float) -> None:
        """Simple CO2 model: volcanic source - weathering sink.

        Weathering rate increases with temperature (negative feedback).
        """
        # Volcanic outgassing
        source = _VOLCANIC_CO2_RATE * dt_years

        # Silicate weathering: faster when warmer (CO2 sink)
        # Reference: 15°C global mean
        temp_anomaly = global_temp_c - 15.0
        weathering_rate = 1.0 / _WEATHERING_CO2_TIMESCALE * (1.0 + temp_anomaly * 0.05)
        sink = (self._co2_ppm - _CO2_REFERENCE) * weathering_rate * dt_years

        self._co2_ppm += source - sink
        self._co2_ppm = max(_CO2_REFERENCE * 0.5, min(2000.0, self._co2_ppm))

    # ── Albedo ───────────────────────────────────────────────────

    def _compute_albedo(self, lat_deg: float, elev: float, ice_m: float) -> float:
        """Surface albedo at a point.

        Args:
            lat_deg: Latitude in degrees.
            elev: Elevation (world units).
            ice_m: Ice thickness (metres).

        Returns:
            Albedo (0-1).
        """
        if ice_m > 1.0:
            return 0.6  # ice/snow
        if ice_m > 0.1:
            return 0.4  # thin ice
        if elev < -0.01:
            return 0.06  # ocean
        if abs(lat_deg) > 70:
            return 0.5  # polar desert
        if abs(lat_deg) > 50:
            return 0.25  # boreal
        return 0.15  # temperate/tropical

    # ── Main advance ─────────────────────────────────────────────

    def advance(self, ws: WorldState, dt_years: float) -> None:
        """Evolve climate forward by dt_years.

        Args:
            ws: WorldState (reads/writes temperature, precipitation).
            dt_years: Time step in years.
        """
        if dt_years <= 0:
            return

        # Get current time from WS
        current_year = ws.time.get("year", 0)

        # Get ice thickness data
        ice_map = ws.get_discrete("ice_thickness_m")
        if not ice_map:
            return

        # Update orbital parameters for midpoint of step
        mid_year = current_year + dt_years / 2
        self._axial_tilt = self._obliquity(mid_year)

        # Compute global mean temperature for CO2 model
        h3_ids = list(ice_map.keys())
        import h3 as _h3

        # Estimate current global mean temperature from WS
        global_temp_norm = 0.5
        if ws.has_field("temperature"):
            t_disc = ws.get_discrete("temperature")
            if t_disc:
                from .climate import norm_to_c as _n2c
                temps_c = [_n2c(v) for v in t_disc.values()]
                global_temp_c = sum(temps_c) / len(temps_c) if temps_c else 15.0
            else:
                global_temp_c = 15.0
        else:
            global_temp_c = 15.0

        # Update CO2
        self._update_co2(dt_years, global_temp_c)

        # CO2 greenhouse anomaly
        co2_ratio = self._co2_ppm / _CO2_REFERENCE
        co2_anomaly = _CO2_DOUBLING_TEMP * math.log2(max(0.5, co2_ratio))

        # Compute new temperature and precipitation for each H3 ID
        temp_data: Dict[str, float] = {}
        precip_data: Dict[str, float] = {}
        elev_map = ws.get_discrete("elevation")
        geo_map = ws.get_discrete("geological_type")

        for hid in h3_ids:
            latlng = _h3.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]
            elev = elev_map.get(hid, 0.0)
            ice_m = ice_map.get(hid, 0.0)
            gtype = geo_map.get(hid, 2)

            # Insolation anomaly from Milankovitch
            insol_anom = self._insolation_anomaly(lat, mid_year)

            # Albedo
            albedo = self._compute_albedo(lat, elev, ice_m)

            # Temperature anomaly = insolation + CO2 + albedo feedback
            insol_temp = insol_anom * _TEMP_SENSITIVITY_PER_WM2
            albedo_feedback = (0.15 - albedo) * 5.0  # lower albedo = warming
            total_anomaly = insol_temp + co2_anomaly + albedo_feedback

            # Existing temperature
            if ws.has_field("temperature"):
                old_temp = ws.field("temperature")(lat, lon)
            else:
                from .climate import _earth_ref_temp
                old_temp = _earth_ref_temp(lat)

            from .climate import TEMP_C_MIN as _TCM, _TEMP_C_RANGE as _TCR
            new_temp_c = old_temp + total_anomaly * 0.01 * dt_years / 1000.0
            new_temp_norm = max(0.0, min(1.0, (new_temp_c - _TCM) / _TCR))

            temp_data[hid] = new_temp_norm

            # Precipitation: simple temperature-driven model
            # Warmer = more evaporation = more precipitation
            if ws.has_field("precipitation"):
                old_precip = ws.field("precipitation")(lat, lon)
            else:
                old_precip = 0.5

            # Clausius-Clapeyron: ~7% more water vapor per °C
            precip_change = 0.07 * total_anomaly * dt_years / 1000.0
            new_precip = max(0.01, min(1.0, old_precip + precip_change))
            precip_data[hid] = new_precip

        # Register results as continuous fields
        if temp_data:
            ws.set_field("temperature", temp_data)
            ws.get_discrete("temperature").update(temp_data)
        if precip_data:
            ws.set_field("precipitation", precip_data)
            ws.get_discrete("precipitation").update(precip_data)

        # Save CO2 and tilt as WS parameters
        ws.params["climate_co2_ppm"] = str(self._co2_ppm)
        ws.params["climate_axial_tilt"] = str(self._axial_tilt)
