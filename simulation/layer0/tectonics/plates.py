"""Plate data types and spherical geometry helpers.

Extracted from the monolithic plate_tectonics.py for reuse across
generate.py and advance.py.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

import h3
import numpy as np


# ======================================================================
# Constants
# ======================================================================

BOUNDARY_CONVERGENT = "convergent"
BOUNDARY_DIVERGENT = "divergent"
BOUNDARY_TRANSFORM = "transform"
BOUNDARY_INTRAPLATE = "intraplate"

_OCEANIC_BASELINE = -0.35
_CONTINENTAL_BASELINE = 0.15
_UPLIFT_PER_UNIT_CONVERGENCE = 0.4


# ======================================================================
# Plate data type
# ======================================================================


@dataclass
class Plate:
    """One tectonic plate on the sphere."""

    id: int
    centre_phi: float        # colatitude in radians (0 = north pole)
    centre_theta: float      # longitude in radians
    motion_x: float = 0.0    # unit vector component X
    motion_y: float = 0.0    # unit vector component Y
    motion_z: float = 0.0    # unit vector component Z
    plate_type: int = 0      # 0 = oceanic, 1 = continental
    is_continental: bool = False

    @property
    def motion(self) -> Tuple[float, float, float]:
        return (self.motion_x, self.motion_y, self.motion_z)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "centre_phi": self.centre_phi,
            "centre_theta": self.centre_theta,
            "motion_x": self.motion_x,
            "motion_y": self.motion_y,
            "motion_z": self.motion_z,
            "plate_type": self.plate_type,
            "is_continental": self.is_continental,
        }

    @staticmethod
    def from_dict(d: dict) -> "Plate":
        return Plate(
            id=d["id"],
            centre_phi=d["centre_phi"],
            centre_theta=d["centre_theta"],
            motion_x=d.get("motion_x", 0.0),
            motion_y=d.get("motion_y", 0.0),
            motion_z=d.get("motion_z", 0.0),
            plate_type=d.get("plate_type", 0),
            is_continental=d.get("is_continental", False),
        )


# ======================================================================
# Spherical geometry
# ======================================================================


def latlon_to_cartesian(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    """Convert lat/lon degrees to 3D unit vector."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    x = math.cos(lat_r) * math.cos(lon_r)
    y = math.sin(lat_r)
    z = math.cos(lat_r) * math.sin(lon_r)
    return (x, y, z)


def cartesian_to_latlon(x: float, y: float, z: float) -> Tuple[float, float]:
    """Convert 3D unit vector to lat/lon degrees."""
    lat = math.degrees(math.asin(max(-1.0, min(1.0, y))))
    lon = math.degrees(math.atan2(z, x))
    return (lat, lon)


def great_circle_distance(
    phi1: float, theta1: float, phi2: float, theta2: float
) -> float:
    """Haversine distance between two spherical points (radians)."""
    dphi = phi2 - phi1
    dtheta = theta2 - theta1
    sin_dphi = math.sin(dphi / 2.0)
    sin_dtheta = math.sin(dtheta / 2.0)
    a = sin_dphi ** 2 + math.sin(phi1) * math.sin(phi2) * sin_dtheta ** 2
    return 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def random_sphere_vector(rng: random.Random) -> Tuple[float, float, float]:
    """Generate a random unit vector on the sphere."""
    theta = rng.random() * 2.0 * math.pi
    phi = math.acos(2.0 * rng.random() - 1.0)
    x = math.sin(phi) * math.cos(theta)
    y = math.sin(phi) * math.sin(theta)
    z = math.cos(phi)
    return (x, y, z)


def rotate_vector(v: Tuple[float, float, float],
                   axis: Tuple[float, float, float],
                   angle_rad: float) -> Tuple[float, float, float]:
    """Rotate vector v around axis by angle_rad (Rodrigues formula)."""
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    dot = v[0] * axis[0] + v[1] * axis[1] + v[2] * axis[2]
    cross_x = axis[1] * v[2] - axis[2] * v[1]
    cross_y = axis[2] * v[0] - axis[0] * v[2]
    cross_z = axis[0] * v[1] - axis[1] * v[0]
    rx = v[0] * cos_a + cross_x * sin_a + axis[0] * dot * (1.0 - cos_a)
    ry = v[1] * cos_a + cross_y * sin_a + axis[1] * dot * (1.0 - cos_a)
    rz = v[2] * cos_a + cross_z * sin_a + axis[2] * dot * (1.0 - cos_a)
    return (rx, ry, rz)


# ======================================================================
# Voronoi assignment
# ======================================================================


def voronoi_assign(h3_ids: List[str], plates: List[Plate],
                   assignment: Dict[str, int]) -> None:
    """Assign each H3 cell to the nearest plate centre (Voronoi on sphere).

    Updates assignment dict in-place.
    """
    for h in h3_ids:
        latlng = h3.cell_to_latlng(h)
        lat_r = math.radians(latlng[0])
        lng_r = math.radians(latlng[1])

        best_dist = float("inf")
        best_idx = 0
        for idx, plate in enumerate(plates):
            d = great_circle_distance(
                lat_r, lng_r, plate.centre_phi, plate.centre_theta
            )
            if d < best_dist:
                best_dist = d
                best_idx = idx
        assignment[h] = best_idx


def detect_boundaries(h3_ids: List[str], assignment: Dict[str, int],
                      plates: List[Plate]) -> Tuple[Dict[str, str], Dict[str, float],
                                                     Dict[str, Tuple[int, int]],
                                                     Dict[str, float]]:
    """Detect plate boundaries from neighbour differences + relative motion.

    Returns:
        boundary_type: h3_id → convergent|divergent|transform|intraplate
        distance_to_boundary: h3_id → cells to nearest boundary
        boundary_plate_ids: h3_id → (plate_a, plate_b)
        convergence_velocity: h3_id → relative speed (0-1)
    """
    boundary_type: Dict[str, str] = {}
    distance_to_boundary: Dict[str, float] = {}
    boundary_plate_ids: Dict[str, Tuple[int, int]] = {}
    convergence_velocity: Dict[str, float] = {}

    # Pass 1: find cells at boundaries
    boundary_cells: Dict[str, Tuple[int, int]] = {}
    for h in h3_ids:
        my_plate = assignment.get(h, 0)
        neighbours = h3.grid_ring(h, 1) or []
        for nh in neighbours:
            if nh not in assignment:
                continue
            if assignment[nh] != my_plate:
                boundary_cells[h] = (my_plate, assignment[nh])
                break

    # Pass 2: classify boundary from plate motion vectors
    for h in h3_ids:
        if h in boundary_cells:
            pid_a, pid_b = boundary_cells[h]
            ma = plates[pid_a].motion
            mb = plates[pid_b].motion

            dot = ma[0] * mb[0] + ma[1] * mb[1] + ma[2] * mb[2]
            # Relative motion vector
            rx, ry, rz = ma[0] - mb[0], ma[1] - mb[1], ma[2] - mb[2]
            rel_speed = math.sqrt(rx*rx + ry*ry + rz*rz)

            if dot < -0.3:
                # Moving toward each other → convergent
                btype = BOUNDARY_CONVERGENT
            elif dot > 0.3:
                # Moving apart → divergent
                btype = BOUNDARY_DIVERGENT
            else:
                # Moving sideways → transform
                btype = BOUNDARY_TRANSFORM

            boundary_type[h] = btype
            boundary_plate_ids[h] = (pid_a, pid_b)
            convergence_velocity[h] = min(1.0, rel_speed * 1.5)
        else:
            boundary_type[h] = BOUNDARY_INTRAPLATE

    # Pass 3: distance to nearest boundary
    _compute_distance_to_boundary(h3_ids, boundary_type, distance_to_boundary)

    return boundary_type, distance_to_boundary, boundary_plate_ids, convergence_velocity


def _compute_distance_to_boundary(
    h3_ids: List[str],
    boundary_type: Dict[str, str],
    distance_to_boundary: Dict[str, float],
) -> None:
    """Compute shortest distance (in cells) to a plate boundary."""
    from collections import deque
    INF = 999.0

    # BFS from all boundary cells
    queue = deque()
    for h in h3_ids:
        if boundary_type.get(h) != BOUNDARY_INTRAPLATE:
            distance_to_boundary[h] = 0.0
            queue.append(h)
        else:
            distance_to_boundary[h] = INF

    while queue:
        cur = queue.popleft()
        nd = distance_to_boundary[cur] + 1.0
        for nh in (h3.grid_ring(cur, 1) or []):
            if nh in h3_ids and nd < distance_to_boundary.get(nh, INF):
                distance_to_boundary[nh] = nd
                queue.append(nh)
