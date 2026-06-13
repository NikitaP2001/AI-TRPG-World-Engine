"""Tectonics package — two-phase plate geodynamics.

Phase 1 — Initial state generation (generate.py):
    Produces a snapshot of plate configuration at any planet_age_myr.
    For young worlds (< 1 Myr) → magma ocean.
    For juvenile worlds (1–500 Myr) → proto-plates, active spreading.
    For mature worlds (500–4500+ Myr) → full plate system + subduction.

Phase 2 — Forward simulation (advance.py):
    Advances plate positions, subduction, spreading, orogeny in
    configurable time steps (e.g. 0.1 Myr per call).

Design:
    TectonicEngine orchestrates both phases:
        engine = TectonicEngine(h3_ids, seed=42)
        engine.generate(age_myr=4500)   # Phase 1 snapshot
        engine.advance(dt_myr=1.0)      # Phase 2 forward step

    WM constraints pin features so forward sim doesn't move them.
"""

from __future__ import annotations

from .engine import TectonicEngine

__all__ = ["TectonicEngine"]
