"""Phase 2 — Forward tectonic simulation (polygon-based Euler rotation).

Moves plates by Euler-rotating polygon vertices, assigns cells via
point-in-polygon (no Voronoi). Each advance(dt_myr) step:
  1. Rotate every vertex of each plate polygon (continuous motion)
  2. Reassign cells via point-in-polygon (STRtree)
  3. Evolve crustal age (ocean ages, subduction resets)
  4. Recompute thickness from tectonic events
  5. Recompute elevation

Pinned features (from WM constraints) are skipped.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Set, Tuple

import h3
import numpy as np
from shapely.geometry import Polygon as ShapelyPolygon

from .plates import (
    Plate, BOUNDARY_CONVERGENT, BOUNDARY_DIVERGENT, BOUNDARY_INTRAPLATE,
)
from .polygon_plates import (
    euler_rotate_polygon,
    assign_cells_via_polygons,
    detect_boundaries_from_polygons,
)
from .crust import (
    compute_crustal_age, compute_crustal_thickness,
    compute_thermal_gradient, compute_elevation, compute_geology,
)


def advance_plates(
    h3_ids: List[str],
    state: dict,
    dt_myr: float,
    tectonic_activity: float = 0.5,
    pinned_cells: Optional[Set[str]] = None,
    rng: Optional[random.Random] = None,
) -> dict:
    """Advance tectonic state by dt_myr million years (polygon-based).

    Pins cells are kept on their original plates. Non-pinned cells
    are reassigned via point-in-polygon after polygon rotation.

    Args:
        h3_ids: All H3 cell IDs.
        state: Current tectonic state dict (from generate_initial_state).
        dt_myr: Time step in millions of years.
        pinned_cells: Cells with WM-constrained features (not moved).
        rng: Random state.

    Returns:
        Updated state dict with new plate positions, crust, elevation.
    """
    if rng is None:
        rng = random.Random(42)

    plates: List[Plate] = state["plates"]
    plate_polygons: Dict[int, ShapelyPolygon] = dict(state.get("plate_polygons", {}))
    assignment: Dict[str, int] = dict(state["assignment"])
    pinned = pinned_cells or set()

    # ── 1. Euler-rotate all plate polygons ─────────────────────────
    for plate in plates:
        pid = plate.id
        if pid not in plate_polygons:
            continue
        omega = (plate.motion_x, plate.motion_y, plate.motion_z)
        plate_polygons[pid] = euler_rotate_polygon(
            plate_polygons[pid], omega[0], omega[1], omega[2], dt_myr,
        )

    # ── 2. Reassign cells via point-in-polygon (non-pinned only) ──
    reassign_ids = [h for h in h3_ids if h not in pinned]
    new_assignment = assign_cells_via_polygons(reassign_ids, plate_polygons)
    # Keep pinned cells on their original plates
    for h in h3_ids:
        if h in pinned:
            pass  # keep original assignment
        else:
            assignment[h] = new_assignment.get(h, assignment.get(h, 0))

    # ── 3. Detect boundaries from polygon neighbours ───────────────
    omega_vectors = {p.id: (p.motion_x, p.motion_y, p.motion_z) for p in plates}
    (boundary_type, distance_to_boundary,
     boundary_plate_ids, convergence_velocity) = detect_boundaries_from_polygons(
        h3_ids, assignment, plate_polygons, omega_vectors
    )

    # ── 4. Evolve crustal age ──────────────────────────────────────
    crustal_age = dict(state["crustal_age_myr"])
    _evolve_crustal_age(h3_ids, assignment, plates, boundary_type,
                        distance_to_boundary, crustal_age, dt_myr, rng)

    # ── 5. Recompute thickness ─────────────────────────────────────
    crustal_thickness = compute_crustal_thickness(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, crustal_age, rng,
    )

    # ── 6. Thermal gradient ────────────────────────────────────────
    thermal_gradient = compute_thermal_gradient(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, crustal_age,
    )

    # ── 7. Recompute elevation ─────────────────────────────────────
    elevation = compute_elevation(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, convergence_velocity,
        crustal_age, crustal_thickness, tectonic_activity, rng,
    )

    # ── 8. Geology ─────────────────────────────────────────────────
    geological_type = compute_geology(
        h3_ids, plates, assignment, boundary_type, crustal_age,
    )

    return {
        "plates": plates,
        "plate_polygons": plate_polygons,
        "assignment": assignment,
        "boundary_type": boundary_type,
        "distance_to_boundary": distance_to_boundary,
        "boundary_plate_ids": boundary_plate_ids,
        "convergence_velocity": convergence_velocity,
        "crustal_age_myr": crustal_age,
        "crustal_thickness_km": crustal_thickness,
        "thermal_gradient": thermal_gradient,
        "elevation": elevation,
        "geological_type": geological_type,
    }


def _evolve_crustal_age(
    h3_ids: List[str],
    assignment: Dict[str, int],
    plates: List[Plate],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
    crustal_age: Dict[str, float],
    dt_myr: float,
    rng: random.Random,
) -> None:
    """Age the crust by dt_myr, reset at ridges and subduction zones."""
    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)

        if not plate.is_continental:
            # Ocean crust ages by dt
            crustal_age[h] += dt_myr

            # Reset at spreading ridge
            if btype == BOUNDARY_DIVERGENT:
                crustal_age[h] = max(0.1, crustal_age[h] * 0.3)

            # Reset at subduction zone (convergent ocean-ocean)
            if btype == BOUNDARY_CONVERGENT:
                crustal_age[h] = max(0.1, crustal_age[h] * 0.5)
        else:
            # Continental crust ages slowly
            crustal_age[h] += dt_myr * 0.1
