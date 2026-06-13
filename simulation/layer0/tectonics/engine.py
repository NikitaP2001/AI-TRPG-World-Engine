"""TectonicEngine — two-phase plate geodynamics orchestrator.

Phase 1 generate(age_myr):
    Produces a snapshot of plate configuration at planet_age.
    Delegates to generate.py which picks the right strategy
    (magma ocean, proto-plates, full system) based on age.

Phase 2 advance(dt_myr):
    Forward-simulates plate motion, subduction, orogeny.
    Respects pinned cells (WM-constrained features).

Usage:
    engine = TectonicEngine(h3_ids, seed=42)
    state = engine.generate(age_myr=4500)
    state = engine.advance(dt_myr=1.0, state=state)
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Set

from .generate import generate_initial_state
from .advance import advance_plates


class TectonicEngine:
    """Two-phase plate geodynamics engine."""

    def __init__(
        self,
        h3_ids: List[str],
        tectonic_activity: float = 0.5,
        seed: int = 42,
    ):
        self.h3_ids = h3_ids
        self.tectonic_activity = tectonic_activity
        self.seed = seed

    def generate(
        self,
        age_myr: float = 4500.0,
    ) -> dict:
        """Phase 1: generate initial tectonic state at given planet age.

        Args:
            age_myr: Planet age in Myr.

        Returns:
            Tectonic state dict with plates, elevation, geology, etc.
        """
        return generate_initial_state(
            self.h3_ids,
            age_myr=age_myr,
            tectonic_activity=self.tectonic_activity,
            seed=self.seed,
        )

    def advance(
        self,
        state: dict,
        dt_myr: float,
        pinned_cells: Optional[Set[str]] = None,
    ) -> dict:
        """Phase 2: forward-simulate tectonic evolution.

        Args:
            state: Current tectonic state (from generate() or previous advance()).
            dt_myr: Time step in millions of years.
            pinned_cells: Cells that should not change (WM feature cells).

        Returns:
            Updated tectonic state dict.
        """
        return advance_plates(
            self.h3_ids,
            state,
            dt_myr=dt_myr,
            tectonic_activity=self.tectonic_activity,
            pinned_cells=pinned_cells,
        )
