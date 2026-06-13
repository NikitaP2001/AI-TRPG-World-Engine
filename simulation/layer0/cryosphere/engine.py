"""CryosphereEngine — two-phase ice/glacier system orchestrator.

Usage:
    engine = CryosphereEngine(h3_ids, elevation, temperature, precipitation, ocean_set)
    ice = engine.generate(age_myr=4500)
    engine.advance(dt_years=100, snowpack, ice_thickness)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .generate import generate_glaciers
from .advance import advance_year


class CryosphereEngine:
    """Two-phase cryosphere engine.

    Phase 1: generate() — analytical snapshot of equilibrium glaciers.
    Phase 2: advance()  — annual mass balance + ice flow.
    """

    def __init__(
        self,
        h3_ids: List[str],
        elevation: Dict[str, float],
        temperature: Dict[str, float],
        precipitation: Dict[str, float],
        ocean_set: set,
        elev_scale_m: float = 5000.0,
    ):
        self.h3_ids = h3_ids
        self.elevation = elevation
        self.temperature = temperature
        self.precipitation = precipitation
        self.ocean_set = ocean_set
        self.elev_scale_m = elev_scale_m

    def generate(
        self,
        age_myr: float = 4500.0,
    ) -> Dict[str, float]:
        """Phase 1: generate initial glacier ice thickness.

        Returns:
            Dict[h3_id] → ice thickness in metres (0 = no glacier).
        """
        return generate_glaciers(
            self.h3_ids,
            self.elevation,
            self.temperature,
            self.precipitation,
            self.ocean_set,
            age_myr=age_myr,
            elev_scale_m=self.elev_scale_m,
        )

    def advance(
        self,
        dt_years: float,
        snowpack: Dict[str, float],
        ice_thickness: Dict[str, float],
        day_of_year: float = 172.0,
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Phase 2: advance cryosphere by dt_years.

        Uses sub-sampling for long time spans — 1 year steps up to
        max 100 iterations, then scales linearly. This avoids 10 000+
        loop iterations for geological advances while preserving
        the annual mass balance dynamics.

        Args:
            dt_years: Number of years to advance.
            snowpack: Current snow water equivalent [mm].
            ice_thickness: Current glacier ice [m].

        Returns:
            (new_snowpack, new_ice_thickness) after dt_years.
        """
        new_snow = dict(snowpack)
        new_ice = dict(ice_thickness)

        # Cap iterations at 100, scale the effect linearly beyond that
        n_iter = min(max(1, int(dt_years)), 100)
        scale = dt_years / n_iter if n_iter > 0 else 1.0

        for _ in range(n_iter):
            new_snow, new_ice = advance_year(
                self.h3_ids,
                self.temperature,
                self.precipitation,
                self.elevation,
                self.ocean_set,
                new_snow,
                new_ice,
                day_of_year=day_of_year,
            )

        # Scale ice thickness for un-iterated years
        if scale > 1.0 and n_iter >= 1:
            for k in new_ice:
                if new_ice[k] > 0:
                    new_ice[k] = min(3000.0, new_ice[k] * scale)

        return new_snow, new_ice
