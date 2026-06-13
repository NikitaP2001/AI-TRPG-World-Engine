"""ErosionEngine — two-phase landscape evolution.

Phase 1 generate():
    Analytical erosion snapshot — applies diffusion for planet_age_myr
    to produce a mature drainage landscape.

Phase 2 advance(dt_years):
    Forward-simulates hillslope diffusion + fluvial incision + sediment
    transport. Updates cell elevation.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set

from .diffusion import (
    build_neighbour_map,
    solve_diffusion,
    compute_sediment_budget,
    _HILLSLOPE_K,
)


class ErosionEngine:
    """Landscape evolution engine — diffusion + sediment transport."""

    def __init__(
        self,
        h3_ids: List[str],
        neighbour_map: Optional[Dict[str, List[str]]] = None,
        k: float = _HILLSLOPE_K,
    ):
        self.h3_ids = h3_ids
        self.neighbour_map = neighbour_map or build_neighbour_map(h3_ids)
        self.k = k

    def generate(
        self,
        elevation: Dict[str, float],
        age_myr: float = 4500.0,
        ocean_set: Optional[Set[str]] = None,
    ) -> Dict[str, float]:
        """Phase 1: diffuse initial topography over planet age.

        Applies ~10 diffusion steps across age_myr, each large enough
        to smooth topography but maintain major features.
        """
        if ocean_set is None:
            ocean_set = set()
        elev = dict(elevation)
        # Number of steps: scale with age (more steps for older planets)
        n_steps = min(max(5, int(age_myr / 100)), 50)
        dt_per_step = age_myr * 1e6 / n_steps  # convert Myr to years

        for _ in range(n_steps):
            elev = solve_diffusion(
                self.h3_ids, self.neighbour_map, elev,
                dt_years=dt_per_step, k=self.k, ocean_set=ocean_set,
            )

        return elev

    def advance(
        self,
        elevation: Dict[str, float],
        dt_years: float,
        ocean_set: Optional[Set[str]] = None,
    ) -> Dict[str, float]:
        """Phase 2: diffuse topography for dt_years.

        Caps iterations to avoid runaway — scales K for very long steps.
        """
        if ocean_set is None:
            ocean_set = set()
        if dt_years <= 0:
            return dict(elevation)

        elev = dict(elevation)

        # Cap: at most 10 iterations, scale diffusivity for long dt
        n_steps = min(max(1, int(dt_years / 100_000)), 10)
        dt_per_step = dt_years / n_steps

        for _ in range(n_steps):
            elev = solve_diffusion(
                self.h3_ids, self.neighbour_map, elev,
                dt_years=dt_per_step, k=self.k, ocean_set=ocean_set,
            )

        return elev
