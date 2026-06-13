"""Phase 1 — Initial tectonic state generation by planet age.

Determines how many plates, crust properties, and elevation patterns
to produce given the planet's age in Myr.

Age phases:
  0–1 Myr:    Magma ocean — single uniform crust, no plates
  1–100 Myr:  Proto-plates — 2-4 thin plates, no subduction yet
  100–500 Myr: Juvenile — 4-8 plates, active spreading, small continents
  500–2000 Myr: Mature — 6-10 plates, subduction, orogeny, full system
  2000–4500+ Myr: Old — 8-15 plates, thick crust, cratons, erosion
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import h3
import numpy as np

from .plates import (
    Plate, BOUNDARY_INTRAPLATE,
    random_sphere_vector, voronoi_assign, detect_boundaries,
)
from .polygon_plates import build_plate_polygons
from .crust import (
    compute_crustal_age, compute_crustal_thickness,
    compute_thermal_gradient, compute_elevation, compute_geology,
)


def generate_initial_state(
    h3_ids: List[str],
    age_myr: float = 4500.0,
    tectonic_activity: float = 0.5,
    seed: int = 42,
) -> dict:
    """Generate initial tectonic state for a planet of given age.

    Args:
        h3_ids: All H3 cell IDs at generation resolution.
        age_myr: Planet age in millions of years.
        tectonic_activity: 0-1 scaling for boundary effects.
        seed: Random seed.

    Returns:
        dict with keys:
            plates: List[Plate]
            assignment: Dict[h3_id] → plate_id
            boundary_type: Dict[h3_id] → str
            distance_to_boundary: Dict[h3_id] → float
            boundary_plate_ids: Dict[h3_id] → (a, b)
            convergence_velocity: Dict[h3_id] → float
            crustal_age_myr: Dict[h3_id] → float
            crustal_thickness_km: Dict[h3_id] → float
            thermal_gradient: Dict[h3_id] → float
            elevation: Dict[h3_id] → float
            geological_type: Dict[h3_id] → int
    """
    rng = random.Random(seed)
    age = max(0.1, age_myr)

    # ── Select generation strategy by age ──────────────────────────
    if age < 1.0:
        return _magma_ocean(h3_ids, age, seed)
    elif age < 100.0:
        n_plates = max(2, min(4, 2 + int(age / 25)))
        cont_ratio = 0.15  # little continental crust yet
    elif age < 500.0:
        n_plates = max(3, min(8, 3 + int(age / 80)))
        cont_ratio = 0.25
    elif age < 2000.0:
        n_plates = max(4, min(10, 4 + int(age / 300)))
        cont_ratio = 0.35
    else:
        n_plates = max(5, min(15, 5 + int(age / 500)))
        cont_ratio = 0.40

    # ── Generate plates ───────────────────────────────────────────
    plates: List[Plate] = []
    for i in range(n_plates):
        theta = rng.random() * 2.0 * math.pi
        phi = math.acos(2.0 * rng.random() - 1.0)
        mx, my, mz = random_sphere_vector(rng)
        is_continental = rng.random() < cont_ratio or i == 0
        plates.append(Plate(
            id=i, centre_phi=phi, centre_theta=theta,
            motion_x=mx, motion_y=my, motion_z=mz,
            is_continental=is_continental,
            plate_type=1 if is_continental else 0,
        ))

    # ── Assign cells (Voronoi) ────────────────────────────────────
    assignment: Dict[str, int] = {}
    voronoi_assign(h3_ids, plates, assignment)

    # ── Detect boundaries ─────────────────────────────────────────
    (boundary_type, distance_to_boundary,
     boundary_plate_ids, convergence_velocity) = detect_boundaries(
        h3_ids, assignment, plates
    )

    # ── Crustal properties ────────────────────────────────────────
    crustal_age = compute_crustal_age(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, rng, age_myr=age,
    )

    crustal_thickness = compute_crustal_thickness(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, crustal_age, rng,
    )

    thermal_gradient = compute_thermal_gradient(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, crustal_age,
    )

    # ── Elevation ─────────────────────────────────────────────────
    elevation = compute_elevation(
        h3_ids, plates, assignment, boundary_type,
        distance_to_boundary, convergence_velocity,
        crustal_age, crustal_thickness, tectonic_activity, rng,
    )

    # ── Geology ───────────────────────────────────────────────────
    geological_type = compute_geology(
        h3_ids, plates, assignment, boundary_type, crustal_age,
    )

    # Build plate polygons from Voronoi assignments
    plate_polygons = build_plate_polygons(h3_ids, assignment)

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


# ======================================================================
# Magma ocean (age < 1 Myr)
# ======================================================================


def _magma_ocean(
    h3_ids: List[str],
    age_myr: float,
    seed: int,
) -> dict:
    """Generate a young magma ocean world — no plates, uniform basalt.

    Thin, hot crust just beginning to solidify.
    """
    rng = random.Random(seed + 1)
    n = len(h3_ids)

    # Single "plate" — the whole planet
    plates = [Plate(
        id=0, centre_phi=0.0, centre_theta=0.0,
        motion_x=0, motion_y=0, motion_z=1,
        is_continental=False, plate_type=0,
    )]
    assignment = {h: 0 for h in h3_ids}
    boundary_type = {h: BOUNDARY_INTRAPLATE for h in h3_ids}
    distance_to_boundary = {h: 999.0 for h in h3_ids}
    boundary_plate_ids: Dict[str, Tuple[int, int]] = {}
    convergence_velocity = {h: 0.0 for h in h3_ids}

    # Extremely young, thin crust
    crustal_age = {h: max(0.1, age_myr + rng.gauss(0, 0.1)) for h in h3_ids}
    crustal_thickness = {h: 3.0 + max(0, age_myr * 5) + rng.gauss(0, 1) for h in h3_ids}
    thermal_gradient = {h: 50.0 + rng.gauss(0, 5) for h in h3_ids}

    # Very flat, slightly below "sea level" (all magma ocean)
    elevation = {h: -0.2 + rng.gauss(0, 0.05) for h in h3_ids}
    geological_type = {h: 0 for h in h3_ids}  # all oceanic

    import h3 as _h3_m
    single_cell = h3_ids[0]
    ll = _h3_m.cell_to_latlng(single_cell)
    from shapely.geometry import Polygon as _SPoly
    # Whole planet polygon: a band around the equator covering all longitudes
    plate_polygons = {0: _SPoly([
        (-180, -90), (180, -90), (180, 90), (-180, 90), (-180, -90)
    ])}

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
