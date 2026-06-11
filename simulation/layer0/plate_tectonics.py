"""Layer 0 — Plate Tectonics Model.

Voronoi plate simulation on sphere with motion vectors.
Drives elevation: convergent boundaries → uplift, divergent → rifting.

Design doc § Stage 1 — Tectonic Structure.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

import h3
import numpy as np


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
# Spherical geometry helpers
# ======================================================================


def _latlon_to_cartesian(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    """Convert lat/lon degrees to 3D unit vector."""
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    x = math.cos(lat_r) * math.cos(lon_r)
    y = math.sin(lat_r)
    z = math.cos(lat_r) * math.sin(lon_r)
    return (x, y, z)


def _great_circle_distance(
    phi1: float, theta1: float, phi2: float, theta2: float
) -> float:
    """Haversine distance between two spherical coordinate points."""
    dphi = phi2 - phi1
    dtheta = theta2 - theta1
    sin_dphi = math.sin(dphi / 2.0)
    sin_dtheta = math.sin(dtheta / 2.0)
    a = (
        sin_dphi ** 2
        + math.sin(phi1) * math.sin(phi2) * sin_dtheta ** 2
    )
    return 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _random_sphere_vector(rng: random.Random) -> Tuple[float, float, float]:
    """Generate a random unit vector on the sphere."""
    theta = rng.random() * 2.0 * math.pi
    phi = math.acos(2.0 * rng.random() - 1.0)
    x = math.sin(phi) * math.cos(theta)
    y = math.sin(phi) * math.sin(theta)
    z = math.cos(phi)
    return (x, y, z)


# ======================================================================
# Boundary type constants
# ======================================================================

BOUNDARY_CONVERGENT = "convergent"
BOUNDARY_DIVERGENT = "divergent"
BOUNDARY_TRANSFORM = "transform"
BOUNDARY_INTRAPLATE = "intraplate"

# Crust baseline elevations
_OCEANIC_BASELINE = -0.35
_CONTINENTAL_BASELINE = 0.15

# Uplift scaling: convergence=1.0 (unit vectors) → uplift ≈ 0.4 (P2.4)
_UPLIFT_PER_UNIT_CONVERGENCE = 0.4


# ======================================================================
# Plate Tectonics Model
# ======================================================================


class PlateTectonicsModel:
    """Tectonic plate simulation on an H3 grid.

    Generates plates, assigns cells to nearest plate (Voronoi on sphere),
    detects convergent/divergent/transform boundaries from relative plate
    motion, and computes tectonic baseline elevation.

    Extended with:
      - Crustal age model (ocean cooling → thermal subsidence)
      - Crustal thickness model (isostasy-driven elevation)
      - Thermal gradient model (controls metamorphism, ore formation)
      - Wider deformation zones (exp decay with configurable range)

    Elevation ranges:
      < -0.2   deep ocean
      -0.2–0.0 shallow / continental shelf
       0.0–0.3 plains
       0.3–0.7 hills / foothills
      > 0.7    mountains
    """

    def __init__(
        self,
        h3_ids: List[str],
        num_plates: int = 8,
        seed: int = 42,
        tectonic_activity: float = 0.5,
    ):
        self.h3_ids = h3_ids
        self.num_plates = max(3, num_plates)
        self.seed = seed
        self.tectonic_activity = tectonic_activity
        self._rng = random.Random(seed)

        # Populated by generate()
        self.plates: List[Plate] = []
        self.assignment: Dict[str, int] = {}          # h3_id → plate_id
        self.boundary_type: Dict[str, str] = {}        # h3_id → boundary type
        self.distance_to_boundary: Dict[str, float] = {}  # cells to nearest boundary
        self.boundary_plate_ids: Dict[str, Tuple[int, int]] = {}
        self.convergence_velocity: Dict[str, float] = {}  # P2.4

        # New fields
        self.crustal_age_myr: Dict[str, float] = {}        # age in Myr
        self.crustal_thickness_km: Dict[str, float] = {}   # thickness in km
        self.thermal_gradient: Dict[str, float] = {}       # degC/km

        self.generate()

    # ── Public API ────────────────────────────────────────────────

    def compute_elevation(self) -> Dict[str, float]:
        """Compute tectonic baseline elevation with age, isostasy, thermal subsidence.

        Returns Dict[h3_id] → elevation in [-0.5, 1.5].

        Physics:
          - Oceanic crust: thermal subsidence Parsons-Sclater law
            depth(km) = 2.5 + 0.35*sqrt(age_Myr)
          - Continental crust: isostatic balance
            elev = H0 + (thickness - 35km) * (rho_m - rho_c)/rho_c
          - Convergent: orogenic uplift + thickened crust
          - Divergent: rifting / ridge uplift
        """
        elevation: Dict[str, float] = {}
        for h in self.h3_ids:
            pid = self.assignment.get(h, 0)
            plate = self.plates[pid]
            btype = self.boundary_type.get(h, BOUNDARY_INTRAPLATE)
            dist = self.distance_to_boundary.get(h, 999.0)
            age = self.crustal_age_myr.get(h, 100.0)
            thick = self.crustal_thickness_km.get(h, 35.0)

            # ── Baseline from isostatic balance ──
            rho_mantle = 3.3  # g/cm3
            rho_crust = 2.8 if plate.is_continental else 2.9
            ref_thick = 35.0 if plate.is_continental else 7.0

            if plate.is_continental:
                # Isostasy: thicker crust → higher elevation
                el = 0.0 + (thick - ref_thick) * (rho_mantle - rho_crust) / rho_crust * 0.1
            else:
                # Oceanic: thermal subsidence (Parsons-Sclater)
                # depth_km = 2.5 + 0.35 * sqrt(age)
                # Convert: elev = -depth_norm, ridge crest at ~0.2
                depth_km = 2.5 + 0.35 * math.sqrt(max(0.1, age))
                depth_norm = depth_km / 12.0 * 0.35  # scale to -0.35 max depth
                el = _OCEANIC_BASELINE + 0.35 - depth_norm
                # Young crust near ridge = shallower
                if age < 10:
                    el += (10 - age) / 10 * 0.15

            # ── Convergent: uplift + crustal thickening (P2.4) ──
            if btype == BOUNDARY_CONVERGENT:
                range_w = 3.0 + self.tectonic_activity * 3.0  # 3-5 cells
                # Convergence velocity from relative plate motion
                conv = self.convergence_velocity.get(h, self.tectonic_activity)
                uplift = conv * _UPLIFT_PER_UNIT_CONVERGENCE * math.exp(-dist / range_w)
                el += uplift
                if not plate.is_continental:
                    el += uplift * 0.4  # island arc

            # ── Divergent: rift or ridge (P2.4) ──
            elif btype == BOUNDARY_DIVERGENT:
                range_w = 2.0
                conv = self.convergence_velocity.get(h, self.tectonic_activity)
                if plate.is_continental:
                    rift = -conv * _UPLIFT_PER_UNIT_CONVERGENCE * 0.3 * math.exp(-dist / range_w)
                    el += rift
                else:
                    ridge = conv * _UPLIFT_PER_UNIT_CONVERGENCE * 0.15 * math.exp(-dist / range_w)
                    el += ridge

            # ── Transform: slight roughness ──
            elif btype == BOUNDARY_TRANSFORM:
                noise = (self._rng.random() - 0.5) * 0.1 * math.exp(-dist / 2.0)
                el += noise

            elevation[h] = max(-0.5, min(1.5, el))

        return elevation

    def compute_geology(self) -> Dict[str, int]:
        """Assign geological_type from plate context.

        Uses crustal age + thickness rather than random classification.

        0 = oceanic crust
        1 = continental shelf
        2 = continental
        3 = mountain belt (convergent)
        4 = rift valley (continental divergent)
        5 = craton (old continental core, age > 1.5 Gyr)
        6 = fault zone (transform)
        """
        geo: Dict[str, int] = {}
        for h in self.h3_ids:
            pid = self.assignment.get(h, 0)
            plate = self.plates[pid]
            btype = self.boundary_type.get(h, BOUNDARY_INTRAPLATE)
            age = self.crustal_age_myr.get(h, 100.0)

            if btype == BOUNDARY_CONVERGENT:
                geo[h] = 3  # mountain belt
            elif btype == BOUNDARY_DIVERGENT:
                geo[h] = 4 if plate.is_continental else 0
            elif btype == BOUNDARY_TRANSFORM:
                geo[h] = 6  # fault zone
            elif plate.is_continental:
                # Old crust (> 1.5 Gyr) = craton
                if age > 1500:
                    geo[h] = 5
                else:
                    geo[h] = 2
            else:
                geo[h] = 0  # oceanic

        return geo

    # ── Crustal age model ──────────────────────────────────────────

    def _compute_crustal_age(self) -> None:
        """Assign crustal age based on plate type and distance to ridge.

        Oceanic crust: young near ridges (divergent boundaries), ages away.
        Continental crust: old cratons (2-3 Gyr), younger in orogens.
        """
        # Find divergent boundaries for oceanic spreading centres
        ridge_cells = [
            h for h in self.h3_ids
            if self.boundary_type.get(h) == BOUNDARY_DIVERGENT
            and not self.plates[self.assignment.get(h, 0)].is_continental
        ]

        for h in self.h3_ids:
            pid = self.assignment.get(h, 0)
            plate = self.plates[pid]
            btype = self.boundary_type.get(h, BOUNDARY_INTRAPLATE)
            dist = self.distance_to_boundary.get(h, 999.0)

            if not plate.is_continental:
                # Oceanic: age from distance to nearest ridge
                # Spreading rate ~2-8 cm/yr, assume 5 cm/yr average
                if ridge_cells:
                    # Find min distance to any ridge
                    min_dist = min(
                        self.distance_to_boundary.get(rh, 999.0)
                        for rh in ridge_cells
                    ) or dist
                    # age_myr = distance_deg * 111km/deg / (5cm/yr * 1e-5 km/cm * 1e6 yr/Myr)
                    # ≈ distance_deg * 111 / 50 ≈ distance_deg * 2.2
                    age_myr = min_dist * 2.2 + self._rng.gauss(0, 5)
                    age_myr = max(0.1, age_myr)
                else:
                    age_myr = 50 + self._rng.random() * 100

                # Convergent subduction zones reset age (young arc crust)
                if btype == BOUNDARY_CONVERGENT:
                    age_myr = min(age_myr, 20 + dist * 5)

            else:
                # Continental: old in cratons, young in orogens/rifta
                if btype == BOUNDARY_CONVERGENT:
                    # Orogen: young crust (recycled)
                    age_myr = 50 + dist * 20 + self._rng.random() * 100
                elif btype == BOUNDARY_DIVERGENT:
                    age_myr = 10 + dist * 10 + self._rng.random() * 50
                else:
                    # Intraplate: old craton interior = 2-3 Gyr
                    # Edge of continent = younger
                    continent_edge = min(dist, 10.0) / 10.0
                    age_myr = 500 + (2000 + self._rng.random() * 1000) * (1 - continent_edge * 0.5)

            self.crustal_age_myr[h] = max(0.1, age_myr)

    # ── Crustal thickness model ────────────────────────────────────

    def _compute_crustal_thickness(self) -> None:
        """Assign crustal thickness based on tectonic setting.

        Continental: 30-70 km (thicker in orogens, thinner in rifts)
        Oceanic: 5-10 km (thicker near plateaus)
        """
        for h in self.h3_ids:
            pid = self.assignment.get(h, 0)
            plate = self.plates[pid]
            btype = self.boundary_type.get(h, BOUNDARY_INTRAPLATE)
            dist = self.distance_to_boundary.get(h, 999.0)
            age = self.crustal_age_myr.get(h, 100.0)

            if plate.is_continental:
                # Base: 35 km
                thick = 35.0
                # Orogen: thickened crust (up to 70 km in collision)
                if btype == BOUNDARY_CONVERGENT:
                    thick += 25.0 * math.exp(-dist / 3.0)
                # Rift: thinned crust
                elif btype == BOUNDARY_DIVERGENT:
                    thick -= 10.0 * math.exp(-dist / 2.0)
                # Cratons slightly thicker
                elif age > 1500:
                    thick += 5.0
                thick += self._rng.gauss(0, 3)
            else:
                # Oceanic: 5-10 km, thinner near ridge
                thick = 7.0
                if btype == BOUNDARY_DIVERGENT:
                    thick = 5.0 + 2.0 * (1 - math.exp(-dist / 2.0))
                # Old crust thicker (sediment accumulation)
                thick += min(2.0, age / 200.0)
                thick += self._rng.gauss(0, 0.5)

            self.crustal_thickness_km[h] = max(3.0, min(80.0, thick))

    # ── Thermal gradient ───────────────────────────────────────────

    def _compute_thermal_gradient(self) -> None:
        """Assign geothermal gradient (degC/km) from tectonic setting.

        Controls: metamorphism, ore formation, geothermal energy.
        Normal: 25-30 C/km
        Rift/ridge: 40-60 C/km (hot)
        Craton: 10-15 C/km (cold)
        Subduction: 15-25 C/km (variable)
        """
        for h in self.h3_ids:
            pid = self.assignment.get(h, 0)
            plate = self.plates[pid]
            btype = self.boundary_type.get(h, BOUNDARY_INTRAPLATE)
            dist = self.distance_to_boundary.get(h, 999.0)

            if btype == BOUNDARY_DIVERGENT:
                # Ridge/rift: high heat flow
                grad = 45.0 + 15.0 * math.exp(-dist / 1.5)
            elif btype == BOUNDARY_CONVERGENT:
                # Subduction: variable
                grad = 20.0 + self._rng.random() * 15.0
            elif btype == BOUNDARY_TRANSFORM:
                # Friction heating
                grad = 35.0 + 10.0 * math.exp(-dist / 1.0)
            elif not plate.is_continental:
                # Oceanic: hot near ridge, cools with age
                age = self.crustal_age_myr.get(h, 100.0)
                grad = 60.0 / math.sqrt(max(1.0, age))
                grad = max(15.0, min(60.0, grad))
            else:
                # Continental: cold craton, warm elsewhere
                age = self.crustal_age_myr.get(h, 100.0)
                if age > 1500:
                    grad = 15.0  # craton
                else:
                    grad = 25.0 + self._rng.random() * 10.0

            self.thermal_gradient[h] = max(8.0, min(70.0, grad))

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "num_plates": self.num_plates,
            "seed": self.seed,
            "tectonic_activity": self.tectonic_activity,
            "plates": [p.to_dict() for p in self.plates],
            "assignment": dict(self.assignment),
            "boundary_type": dict(self.boundary_type),
            "distance_to_boundary": dict(self.distance_to_boundary),
        }

    @staticmethod
    def from_dict(d: dict, h3_ids: List[str]) -> "PlateTectonicsModel":
        """Restore from dict (produced by to_dict)."""
        model = object.__new__(PlateTectonicsModel)
        model.h3_ids = h3_ids
        model.num_plates = d["num_plates"]
        model.seed = d["seed"]
        model.tectonic_activity = d["tectonic_activity"]
        model._rng = random.Random(d["seed"])
        model.plates = [Plate.from_dict(pd) for pd in d["plates"]]
        model.assignment = {k: int(v) for k, v in d["assignment"].items()}
        model.boundary_type = dict(d["boundary_type"])
        model.distance_to_boundary = {
            k: float(v) for k, v in d["distance_to_boundary"].items()
        }
        model.boundary_plate_ids = {}
        return model

    # ── Internal generation ───────────────────────────────────────

    def generate(self) -> None:
        """Run the full plate generation pipeline."""
        self._generate_plates()
        self._assign_cells()
        self._detect_boundaries()
        self._compute_crustal_age()
        self._compute_crustal_thickness()
        self._compute_thermal_gradient()

    def _generate_plates(self) -> None:
        """Create N plates with random centres and motion vectors."""
        self.plates = []
        for i in range(self.num_plates):
            theta = self._rng.random() * 2.0 * math.pi
            phi = math.acos(2.0 * self._rng.random() - 1.0)
            mx, my, mz = _random_sphere_vector(self._rng)

            # First plate is always continental; others have 40% chance
            is_continental = (i == 0) or (self._rng.random() < 0.4)
            ptype = 1 if is_continental else 0

            self.plates.append(Plate(
                id=i,
                centre_phi=phi,
                centre_theta=theta,
                motion_x=mx,
                motion_y=my,
                motion_z=mz,
                plate_type=ptype,
                is_continental=is_continental,
            ))

    def _assign_cells(self) -> None:
        """Assign each cell to the nearest plate centre (Voronoi on sphere)."""
        self.assignment = {}
        for h in self.h3_ids:
            latlng = h3.cell_to_latlng(h)
            lat_r = math.radians(latlng[0])
            lng_r = math.radians(latlng[1])

            best_dist = float("inf")
            best_idx = 0
            for idx, plate in enumerate(self.plates):
                d = _great_circle_distance(
                    lat_r, lng_r, plate.centre_phi, plate.centre_theta
                )
                if d < best_dist:
                    best_dist = d
                    best_idx = idx
            self.assignment[h] = best_idx

    def _detect_boundaries(self) -> None:
        """Detect plate boundaries by checking neighbour cells.

        For each cell that has a neighbour on a different plate, classify
        the boundary type from relative plate motion.
        """
        boundary_cells: Dict[str, Tuple[int, int]] = {}

        for h in self.h3_ids:
            my_plate = self.assignment.get(h, 0)
            neighbours = h3.grid_ring(h, 1) or []
            for nh in neighbours:
                if nh not in self.assignment:
                    continue
                nh_plate = self.assignment[nh]
                if nh_plate != my_plate:
                    boundary_cells[h] = (my_plate, nh_plate)
                    break

        # Classify boundary types from relative motion
        for h, (pa, pb) in boundary_cells.items():
            plate_a = self.plates[pa]
            plate_b = self.plates[pb]

            ma = np.array([plate_a.motion_x, plate_a.motion_y, plate_a.motion_z])
            mb = np.array([plate_b.motion_x, plate_b.motion_y, plate_b.motion_z])

            # Cell position on sphere (normal direction)
            latlng = h3.cell_to_latlng(h)
            pos = np.array(_latlon_to_cartesian(latlng[0], latlng[1]))
            pos = pos / np.linalg.norm(pos)

            # Relative velocity
            rel_v = ma - mb

            # Convergence = dot product of relative v with position
            convergence = np.dot(rel_v, pos)

            threshold = 0.1 * self.tectonic_activity

            if convergence > threshold:
                self.boundary_type[h] = BOUNDARY_CONVERGENT
            elif convergence < -threshold:
                self.boundary_type[h] = BOUNDARY_DIVERGENT
            else:
                self.boundary_type[h] = BOUNDARY_TRANSFORM

            self.boundary_plate_ids[h] = (pa, pb)
            self.convergence_velocity[h] = abs(convergence)  # P2.4

        # BFS distance to nearest boundary for all cells
        dist_map: Dict[str, float] = {}
        queue: List[str] = []

        for h in self.h3_ids:
            if h in boundary_cells:
                dist_map[h] = 0.0
                queue.append(h)

        # Wavefront propagation
        while queue:
            cur = queue.pop(0)
            cur_dist = dist_map.get(cur, 0.0)
            new_dist = cur_dist + 1.0
            for nh in (h3.grid_ring(cur, 1) or []):
                if nh not in dist_map and nh in self.assignment:
                    dist_map[nh] = new_dist
                    queue.append(nh)

        for h in self.h3_ids:
            self.distance_to_boundary[h] = dist_map.get(h, 100.0)
