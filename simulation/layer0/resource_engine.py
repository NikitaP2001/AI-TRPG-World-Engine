"""ResourceEngine — continuous time-evolving ore/resource system.

Stores resource concentrations as ContinuousFields (one per resource type).
Runs Gray-Scott reaction-diffusion periodically to evolve ore bodies.

Usage:
    engine = ResourceEngine(ws)
    engine.advance(dt_myr=0.1)  # evolve resources
    concentration = engine.field("veins")(35.0, 120.0)  # at any point
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np

from .resources import (
    ResourceType,
    _build_neighbour_map,
    _run_gray_scott_numpy,
    default_resource_types,
)
from ..world_state import WorldState


class ResourceEngine:
    """Time-evolving resource (ore) system with continuous fields."""

    def __init__(
        self,
        ws: WorldState,
        h3_ids: List[str],
        resource_types: Optional[List[ResourceType]] = None,
        seed: int = 42,
    ):
        self._ws = ws
        self._h3_ids = list(h3_ids)
        self._rtypes = resource_types or default_resource_types()
        self._seed = seed
        self._neighbour_map: Optional[np.ndarray] = None

    def _get_nb_map(self) -> np.ndarray:
        """Build and cache neighbour map."""
        if self._neighbour_map is None:
            self._neighbour_map = _build_neighbour_map(self._h3_ids)
        return self._neighbour_map

    # ── Run Gray-Scott and register fields ───────────────────────

    def advance(self, dt_myr: float, rng: Optional[random.Random] = None) -> None:
        """Evolve resource concentrations for dt_myr million years.

        Runs Gray-Scott with current tectonic stress as seed.
        Registers results as ContinuousFields in WorldState.
        """
        if rng is None:
            rng = random.Random(42)

        # Get current tectonic stress from WS
        stress_map = self._ws.get_discrete("tectonic_stress")
        stress = {hid: stress_map.get(hid, 0.0) for hid in self._h3_ids}

        # Get geological type for ocean masking
        geo_map = self._ws.get_discrete("geological_type")

        for rtype in self._rtypes:
            # Scale timesteps by dt_myr (baseline = 1 Myr ≈ full generation run)
            n_steps = max(10, int(rtype.timesteps * min(dt_myr, 5.0)))
            rtype.timesteps = n_steps

            # Run Gray-Scott
            seed = self._seed + hash(rtype.name) & 0xFFFF
            flux = _run_gray_scott_numpy(self._h3_ids, stress, rtype, seed)

            # Mask oceans
            result_data: Dict[str, float] = {}
            for i, hid in enumerate(self._h3_ids):
                is_ocean = geo_map.get(hid, 2) == 0
                result_data[hid] = 0.0 if is_ocean else float(flux[i])

            # Register as continuous field + discrete data
            fname = f"resource_{rtype.name}"
            self._ws.set_field(fname, result_data)
            self._ws.get_discrete(fname).update(result_data)

    def field_names(self) -> List[str]:
        """Get registered resource field names."""
        return [f"resource_{rt.name}" for rt in self._rtypes]

    def has_field(self, name: str) -> bool:
        """Check if a resource field exists."""
        return self._ws.has_field(f"resource_{name}")
