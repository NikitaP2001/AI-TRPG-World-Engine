"""Continuous Soil — soil properties as continuous functions of (lat, lon).

Same physics as assign_soil_profiles() but takes field accessors instead
of CellData lists. Produces ContinuousFields directly.

Usage:
    cs = ContinuousSoil(temp_f, precip_f, elev_f, ...)
    fertility = cs.fertility(35.0, 120.0)   # soil fertility at any point
    cs.build_fields(h3_ids) → register in WS
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .soil import (
    compute_weathering,
    compute_erosion,
    form_soil,
    get_mineral_profile,
    norm_to_c,
)
from ..world_state import WorldState


class ContinuousSoil:
    """Soil properties evaluable at any (lat, lon) via continuous inputs."""

    def __init__(
        self,
        temperature_f: Callable[[float, float], float],
        precipitation_f: Callable[[float, float], float],
        elevation_f: Callable[[float, float], float],
        canopy_f: Callable[[float, float], float],
        bedrock_f: Callable[[float, float], str],
        geo_type_f: Callable[[float, float], int],
        slope_f: Callable[[float, float], float],
        precip_seas_f: Optional[Callable[[float, float], float]] = None,
        time_factor: float = 1.0,
    ):
        self._temp_f = temperature_f
        self._precip_f = precipitation_f
        self._elev_f = elevation_f
        self._canopy_f = canopy_f
        self._bedrock_f = bedrock_f
        self._geo_f = geo_type_f
        self._slope_f = slope_f
        self._psec_f = precip_seas_f or (lambda lat, lon: 0.3)
        self._time_factor = time_factor

    # ── Evaluate at a single point ────────────────────────────────

    def compute(self, lat: float, lon: float) -> dict:
        """Compute all soil properties at (lat, lon). Returns dict."""

        temp = self._temp_f(lat, lon)
        precip = self._precip_f(lat, lon)
        elev = self._elev_f(lat, lon)
        canopy = self._canopy_f(lat, lon)
        bedrock = self._bedrock_f(lat, lon)
        gtype = self._geo_f(lat, lon)
        slope = self._slope_f(lat, lon)
        psec = self._psec_f(lat, lon)

        is_ocean = gtype == 0
        mineral = get_mineral_profile(bedrock)

        weather = compute_weathering(
            temperature=temp, precipitation=precip, mineral=mineral,
            slope=slope, time_factor=self._time_factor, is_ocean=is_ocean,
        )

        # For erosion we need current clay/sand/om — use defaults from
        # the mineral profile since we're forming soil from scratch here
        clay0 = mineral.clay_potential * 0.3 if mineral else 0.2
        sand0 = 0.5 - clay0 * 0.5
        om0 = 0.02

        erosion = compute_erosion(
            slope=slope, precipitation=precip, canopy=canopy,
            clay=clay0, sand=sand0, organic_matter=om0,
            precip_seasonality=psec,
        )

        soil = form_soil(
            weathering=weather, erosion_rate=erosion,
            is_ocean=is_ocean, is_shelf=False,
            time_factor=self._time_factor, mineral=mineral, canopy=canopy,
        )

        return soil

    # ── Build fields into WorldState ──────────────────────────────

    def build_fields(self, ws: WorldState, h3_ids: List[str]) -> None:
        """Compute soil at all H3 centroids and register as fields."""
        import h3 as _h3

        # Collect results per field
        results: Dict[str, Dict[str, float]] = {
            "soil_fertility": {},
            "soil_depth": {},
            "organic_matter": {},
            "clay_content": {},
            "sand_content": {},
            "silt_content": {},
            "soil_ph": {},
            "cation_exchange": {},
        }

        for hid in h3_ids:
            latlng = _h3.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]
            try:
                soil = self.compute(lat, lon)
                for key in results:
                    results[key][hid] = float(soil.get(key, 0.0))
            except Exception:
                continue

        # Register as continuous fields + discrete data
        for fname, data in results.items():
            if data:
                ws.set_field(fname, data)
                ws.get_discrete(fname).update(data)
