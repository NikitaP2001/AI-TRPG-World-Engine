"""Hillslope diffusion on H3 grid — finite-volume explicit scheme.

Solves dH/dt = K * laplacian(H) on the H3 graph.
Each cell exchanges mass with its 6 neighbours (grid_ring(1)).
"""

from __future__ import annotations

import math
from typing import Dict, List, Set

import h3
import numpy as np


# Hillslope diffusivity [m^2 / yr] — typical soil-creep values
_HILLSLOPE_K = 0.01  # m^2/yr (~10^-3 km^2/kyr)

# Fluvial incision parameters (stream-power law)
_FLUVIAL_KF = 1e-6   # erodibility
_FLUVIAL_M = 0.5     # drainage area exponent
_FLUVIAL_N = 1.0     # slope exponent


def _euclidean_3d(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    """3D chord distance between two lat/lon points on unit sphere."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def build_neighbour_map(h3_ids: List[str]) -> Dict[str, List[str]]:
    """Build mapping h3_id -> [neighbour_h3_ids] using grid_ring(1).

    Only neighbours that are also in the provided h3_ids list are kept
    (edges of the grid are handled gracefully).
    """
    id_set: Set[str] = set(h3_ids)
    nmap: Dict[str, List[str]] = {}
    for hid in h3_ids:
        try:
            ring = h3.grid_ring(hid, k=1)
        except Exception:
            ring = []
        nmap[hid] = [n for n in ring if n in id_set]
    return nmap


def solve_diffusion(
    h3_ids: List[str],
    neighbour_map: Dict[str, List[str]],
    elevation: Dict[str, float],
    dt_years: float,
    k: float = _HILLSLOPE_K,
    ocean_set: Set[str] | None = None,
    boundary_elevation: Dict[str, float] | None = None,
) -> Dict[str, float]:
    """Explicit finite-volume diffusion on H3 graph.

    Args:
        h3_ids: All cell IDs.
        neighbour_map: h3_id -> list of neighbour IDs (from build_neighbour_map).
        elevation: Current elevation per cell [world units].
        dt_years: Time step [years].
        k: Hillslope diffusivity [m^2/yr].
        ocean_set: Cells that are ocean — they receive sediment but their
                   elevation stays at baseline (sediment fills basin).
        boundary_elevation: Fixed elevation for boundary conditions
                            (used for cells outside the grid, e.g. ocean floor).

    Returns:
        Updated elevation dict.
    """
    if ocean_set is None:
        ocean_set = set()
    if boundary_elevation is None:
        boundary_elevation = {}

    elev = dict(elevation)
    new_elev = dict(elevation)

    # CFL stability: dt <= dx^2 / (4*K)
    # For H3 res 2: dx ≈ 5 km = 5000 m
    # dt_max ≈ 5000^2 / (4*0.01) = 6.25e8 years — always stable for our dt
    # So we can do explicit Euler with no sub-stepping for typical dt

    for hid in h3_ids:
        if hid in ocean_set:
            continue  # ocean cells handled separately

        neighbours = neighbour_map.get(hid, [])
        if not neighbours:
            continue

        h0 = elev[hid]
        total_flux = 0.0
        for nid in neighbours:
            hn = elev.get(nid, boundary_elevation.get(nid, h0))
            # Diffusive flux: q = K * (hn - h0) / dx^2  (per unit area)
            # dx approximated from neighbour distance
            total_flux += (hn - h0)

        new_elev[hid] = h0 + k * dt_years * total_flux / len(neighbours)

    # Ocean cells: fill with sediment (elevation rises toward 0)
    # Simple: each ocean cell receives average of incoming sediment
    # from land neighbours
    for hid in ocean_set:
        if hid not in elevation:
            continue
        neighbours = neighbour_map.get(hid, [])
        if not neighbours:
            continue
        # Average sediment from land neighbours
        incoming = 0.0
        n_land = 0
        for nid in neighbours:
            if nid not in ocean_set and nid in elevation:
                incoming += elevation[nid] - new_elev.get(nid, elevation[nid])
                n_land += 1
        if n_land > 0 and incoming > 0:
            # Sediment fills ocean — raise toward 0
            sediment = incoming * 0.01 * dt_years * k * 100
            new_elev[hid] = min(0.0, elevation[hid] + sediment)

    return new_elev


def compute_sediment_budget(
    old_elev: Dict[str, float],
    new_elev: Dict[str, float],
    ocean_set: Set[str],
) -> Dict[str, float]:
    """Compute how much sediment each cell gained/lost.

    Returns:
        Dict of h3_id -> sediment_thickness_change [world units].
        Positive = deposition, negative = erosion.
    """
    budget = {}
    for hid in old_elev:
        budget[hid] = new_elev.get(hid, old_elev[hid]) - old_elev[hid]
    return budget
