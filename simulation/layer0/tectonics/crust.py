"""Crustal models — age, thickness, isostasy, thermal gradient.

All functions work on per-cell basis and are shared between
generate.py (initial state) and advance.py (forward sim).
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple

import h3

from .plates import (
    Plate,
    BOUNDARY_CONVERGENT, BOUNDARY_DIVERGENT, BOUNDARY_TRANSFORM,
    BOUNDARY_INTRAPLATE,
)


# ======================================================================
# Crustal age
# ======================================================================


def compute_crustal_age(
    h3_ids: List[str],
    plates: List[Plate],
    assignment: Dict[str, int],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
    rng: random.Random,
    age_myr: float = 4500.0,
) -> Dict[str, float]:
    """Compute crustal age for every cell.

    Oceanic: young near ridges, ages away (spreading rate ~5 cm/yr).
    Continental: old cratons, younger in orogens/rifta.

    Args:
        age_myr: Planet age in Myr — caps maximum crustal age.
    """
    crustal_age: Dict[str, float] = {}

    # Find ridge cells (oceanic divergent boundaries)
    ridge_cells = [
        h for h in h3_ids
        if boundary_type.get(h) == BOUNDARY_DIVERGENT
        and not plates[assignment.get(h, 0)].is_continental
    ]

    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)
        dist = distance_to_boundary.get(h, 999.0)

        if not plate.is_continental:
            # Oceanic: age from distance to nearest ridge
            if ridge_cells:
                min_dist = min(
                    distance_to_boundary.get(rh, 999.0)
                    for rh in ridge_cells
                ) or dist
                # ~2.2 Myr per degree (5 cm/yr spreading)
                age_myr_val = min_dist * 2.2 + rng.gauss(0, 5)
                age_myr_val = max(0.1, age_myr_val)
            else:
                age_myr_val = 50 + rng.random() * 100

            # Convergent subduction resets age (arc crust)
            if btype == BOUNDARY_CONVERGENT:
                age_myr_val = min(age_myr_val, 20 + dist * 5)
        else:
            # Continental: old in cratons, young in orogens/rifta
            if btype == BOUNDARY_CONVERGENT:
                age_myr_val = 50 + dist * 20 + rng.random() * 100
            elif btype == BOUNDARY_DIVERGENT:
                age_myr_val = 10 + dist * 10 + rng.random() * 50
            else:
                continent_edge = min(dist, 10.0) / 10.0
                age_myr_val = 500 + (min(age_myr, 3000.0)) * (1 - continent_edge * 0.5)

        crustal_age[h] = min(age_myr, max(0.1, age_myr_val))

    return crustal_age


# ======================================================================
# Crustal thickness
# ======================================================================


def compute_crustal_thickness(
    h3_ids: List[str],
    plates: List[Plate],
    assignment: Dict[str, int],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
    crustal_age: Dict[str, float],
    rng: random.Random,
) -> Dict[str, float]:
    """Compute crustal thickness in km.

    Continental: 30-70 km (thicker in orogens, thinner in rifts)
    Oceanic: 5-10 km (thicker near plateaus)
    """
    thickness: Dict[str, float] = {}

    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)
        dist = distance_to_boundary.get(h, 999.0)
        age = crustal_age.get(h, 100.0)

        if plate.is_continental:
            if btype == BOUNDARY_CONVERGENT:
                thick = 45 + min(25.0, dist * 8) + rng.gauss(0, 3)
            elif btype == BOUNDARY_DIVERGENT:
                thick = 30 + max(0, 5 - dist) + rng.gauss(0, 2)
            else:
                thick = 35 + math.log(max(1.0, age)) * 3 + rng.gauss(0, 2)
        else:
            if btype == BOUNDARY_CONVERGENT:
                thick = 8 + min(5.0, dist) + rng.gauss(0, 1)
            elif btype == BOUNDARY_DIVERGENT:
                thick = 5 + min(5.0, dist * 0.5) + rng.gauss(0, 0.5)
            else:
                thick = 7 + math.sqrt(max(0.1, age)) * 0.3 + rng.gauss(0, 0.5)

        thickness[h] = max(3.0, min(80.0, thick))

    return thickness


# ======================================================================
# Thermal gradient
# ======================================================================


def compute_thermal_gradient(
    h3_ids: List[str],
    plates: List[Plate],
    assignment: Dict[str, int],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
    crustal_age: Dict[str, float],
) -> Dict[str, float]:
    """Compute geothermal gradient in degC/km.

    Young crust / active margins: 30-50 degC/km
    Old cratons: 10-20 degC/km
    """
    gradient: Dict[str, float] = {}

    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)
        age = crustal_age.get(h, 100.0)

        if btype == BOUNDARY_CONVERGENT:
            grad = 30 + 20 * math.exp(-distance_to_boundary.get(h, 5.0) / 3.0)
        elif btype == BOUNDARY_DIVERGENT:
            grad = 40 + 15 * math.exp(-distance_to_boundary.get(h, 5.0) / 2.0)
        elif btype == BOUNDARY_TRANSFORM:
            grad = 25 + 10 * math.exp(-distance_to_boundary.get(h, 5.0) / 2.0)
        elif plate.is_continental:
            grad = 15 + 15 * math.exp(-age / 500.0)
        else:
            grad = 20 + 20 * math.exp(-age / 100.0)

        gradient[h] = max(8.0, min(60.0, grad))

    return gradient


# ======================================================================
# Elevation (isostasy + boundary effects)
# ======================================================================


def compute_elevation(
    h3_ids: List[str],
    plates: List[Plate],
    assignment: Dict[str, int],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
    convergence_velocity: Dict[str, float],
    crustal_age: Dict[str, float],
    crustal_thickness: Dict[str, float],
    tectonic_activity: float = 0.5,
    rng: random.Random = None,
) -> Dict[str, float]:
    """Compute tectonic baseline elevation.

    Physics:
      - Continental: isostatic balance from crustal thickness
      - Oceanic: thermal subsidence (Parsons-Sclater)
      - Convergent: orogenic uplift
      - Divergent: rift/ridge
      - Transform: slight roughness
    """
    if rng is None:
        rng = random.Random(42)

    elevation: Dict[str, float] = {}
    rho_mantle = 3.3

    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)
        dist = distance_to_boundary.get(h, 999.0)
        age = crustal_age.get(h, 100.0)
        thick = crustal_thickness.get(h, 35.0)
        rho_crust = 2.8 if plate.is_continental else 2.9
        ref_thick = 35.0 if plate.is_continental else 7.0

        if plate.is_continental:
            el = (thick - ref_thick) * (rho_mantle - rho_crust) / rho_crust * 0.1
        else:
            depth_km = 2.5 + 0.35 * math.sqrt(max(0.1, age))
            depth_norm = depth_km / 12.0 * 0.35
            el = -0.35 + 0.35 - depth_norm
            if age < 10:
                el += (10 - age) / 10 * 0.15

        # Boundary effects
        if btype == BOUNDARY_CONVERGENT:
            range_w = 3.0 + tectonic_activity * 3.0
            conv = convergence_velocity.get(h, tectonic_activity)
            uplift = conv * 0.4 * math.exp(-dist / range_w)
            el += uplift
            if not plate.is_continental:
                el += uplift * 0.4  # island arc
        elif btype == BOUNDARY_DIVERGENT:
            range_w = 2.0
            conv = convergence_velocity.get(h, tectonic_activity)
            if plate.is_continental:
                el -= conv * 0.4 * 0.3 * math.exp(-dist / range_w)
            else:
                el += conv * 0.4 * 0.15 * math.exp(-dist / range_w)
        elif btype == BOUNDARY_TRANSFORM:
            el += (rng.random() - 0.5) * 0.1 * math.exp(-dist / 2.0)

        elevation[h] = max(-0.5, min(1.5, el))

    return elevation


# ======================================================================
# Geology type
# ======================================================================


def compute_geology(
    h3_ids: List[str],
    plates: List[Plate],
    assignment: Dict[str, int],
    boundary_type: Dict[str, str],
    crustal_age: Dict[str, float],
) -> Dict[str, int]:
    """Assign geological_type from plate context.

    0 = oceanic       1 = shelf       2 = continental
    3 = mountain      4 = rift        5 = craton      6 = fault
    """
    geo: Dict[str, int] = {}
    for h in h3_ids:
        pid = assignment.get(h, 0)
        plate = plates[pid]
        btype = boundary_type.get(h, BOUNDARY_INTRAPLATE)
        age = crustal_age.get(h, 100.0)

        if btype == BOUNDARY_CONVERGENT:
            geo[h] = 3
        elif btype == BOUNDARY_DIVERGENT:
            geo[h] = 4 if plate.is_continental else 0
        elif btype == BOUNDARY_TRANSFORM:
            geo[h] = 6
        elif plate.is_continental:
            geo[h] = 5 if age > 1500 else 2
        else:
            geo[h] = 0

    return geo
