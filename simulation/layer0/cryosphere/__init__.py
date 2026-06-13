"""Cryosphere package — two-phase ice/glacier system.

Phase 1 — Initial state generation (generate.py):
    Computes glacier cover from climate (temp, precip) + topography.
    Determines accumulation zones, equilibrium line, ice thickness.
    Age-aware: young worlds (< water oceans) → no ice.

Phase 2 — Forward simulation (advance.py):
    Annual mass balance: snow accumulation vs ablation.
    Ice flow: Shallow Ice Approximation (SIA) down elevation gradient.
    Calving at ocean boundaries.

Design:
    CryosphereEngine orchestrates both phases:
        engine = CryosphereEngine(cells, temp, precip, elev)
        engine.generate(age_myr=4500)
        engine.advance(dt_years=100)

    Ice fields are stored on CellData as:
        cell.snowpack_mm       — seasonal snow water equivalent
        cell.ice_thickness_m   — perennial glacier ice (0 if no glacier)
        cell.ice_flow_dir      — flow direction (for visualisation)
"""

from __future__ import annotations

from .engine import CryosphereEngine

__all__ = ["CryosphereEngine"]
