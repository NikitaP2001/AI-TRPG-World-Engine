"""Layer 0 — Special resource flux via Gray-Scott reaction-diffusion.

Models the distribution of special resources (domain-defined) across the
world grid using anisotropic reaction-diffusion. Each resource type is a
Gray-Scott system seeded by tectonic stress and constrained by terrain.

Design doc § Stage 5 — Special Resource Flux.
Uses numpy arrays for performance (pure dict iteration is too slow at scale).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import h3
import numpy as np


# ======================================================================
# Special resource type definition
# ======================================================================


@dataclass
class ResourceType:
    """One special resource type, defined by the World Master.

    Gray-Scott parameters:
      feed_rate (F):   rate at which U is replenished (0.01-0.08)
      kill_rate (k):   rate at which V decays         (0.04-0.07)
      diff_U (Du):     diffusion coefficient for U     (0.05-0.20)
      diff_V (Dv):     diffusion coefficient for V     (0.01-0.10)
                        Dv < Du required for pattern formation.

    Known pattern regimes (F, k):
      Spots:  (0.035, 0.065)   Stripes: (0.058, 0.062)
      Maze:   (0.030, 0.057)   Spiral:  (0.020, 0.055)
      Worms:  (0.078, 0.061)
    """

    name: str
    feed_rate: float = 0.035           # F
    kill_rate: float = 0.065           # k
    diff_U: float = 0.16               # Du
    diff_V: float = 0.08               # Dv
    seed_stress_threshold: float = 0.2  # minimum tectonic_stress to seed V
    seed_strength: float = 0.4         # initial V concentration in seeded cells
    timesteps: int = 800               # RD integration steps
    dt: float = 0.5                    # integration timestep

    @classmethod
    def veins(cls) -> ResourceType:
        return cls(name="veins", feed_rate=0.035, kill_rate=0.065, timesteps=800)

    @classmethod
    def seams(cls) -> ResourceType:
        return cls(name="seams", feed_rate=0.058, kill_rate=0.062, timesteps=600)

    @classmethod
    def diffuse(cls) -> ResourceType:
        return cls(name="diffuse", feed_rate=0.030, kill_rate=0.057, timesteps=500)


# ======================================================================
# Gray-Scott integration (numpy-accelerated)
# ======================================================================


def _build_neighbour_map(h3_ids: List[str]) -> np.ndarray:
    """Build a (N, 6) array of neighbour indices into h3_ids.

    Returns array of shape (N, 6) where entry (i, j) is the index
    of the j-th neighbour of cell i, or -1 if that neighbour does
    not exist in the grid.
    """
    n = len(h3_ids)
    id_to_idx = {hid: i for i, hid in enumerate(h3_ids)}
    nb_map = np.full((n, 6), -1, dtype=np.int32)

    for i, hid in enumerate(h3_ids):
        neighbours = h3.grid_ring(hid, 1) or []
        for j, nh in enumerate(neighbours):
            idx = id_to_idx.get(nh, -1)
            if idx >= 0:
                nb_map[i, j] = idx

    return nb_map


def _run_gray_scott_numpy(
    h3_ids: List[str],
    tectonic_stress: Dict[str, float],
    rtype: ResourceType,
    rng_seed: int,
) -> np.ndarray:
    """Run Gray-Scott RD using numpy.

    Returns array of shape (N,) — normalised V concentration per cell.
    """
    n = len(h3_ids)
    nb = _build_neighbour_map(h3_ids)
    rng = np.random.default_rng(rng_seed)

    # Initialise U=1, V=0
    U = np.ones(n, dtype=np.float64)
    V = np.zeros(n, dtype=np.float64)

    # Seed V in high-stress cells
    for i, hid in enumerate(h3_ids):
        stress = tectonic_stress.get(hid, 0.0)
        if stress >= rtype.seed_stress_threshold:
            V[i] = rtype.seed_strength + rng.random() * 0.05

    F = rtype.feed_rate
    k = rtype.kill_rate
    Du = rtype.diff_U
    Dv = rtype.diff_V
    dt = rtype.dt

    valid = nb >= 0  # (N, 6) bool mask

    for step in range(rtype.timesteps):
        # Compute Laplacian: mean(neighbours) - centre
        # For each cell, sum valid neighbour values
        n_sum = np.zeros(n, dtype=np.float64)
        n_count = np.zeros(n, dtype=np.int32)

        for j in range(6):
            col = nb[:, j]
            mask = col >= 0
            n_sum[mask] += U[col[mask]]
            n_count[mask] += 1

        n_mean = np.divide(n_sum, np.maximum(n_count, 1), where=n_count > 0)
        lap_U = (2.0 / 3.0) * (n_mean - U)
        # Reset for V
        n_sum.fill(0.0)
        n_count.fill(0)
        for j in range(6):
            col = nb[:, j]
            mask = col >= 0
            n_sum[mask] += V[col[mask]]
            n_count[mask] += 1
        n_mean = np.divide(n_sum, np.maximum(n_count, 1), where=n_count > 0)
        lap_V = (2.0 / 3.0) * (n_mean - V)

        uvv = U * V * V
        U += dt * (Du * lap_U - uvv + F * (1.0 - U))
        V += dt * (Dv * lap_V + uvv - (F + k) * V)

        # Clamp
        np.clip(U, 0.0, 1.0, out=U)
        np.clip(V, 0.0, 1.0, out=V)

        # Early stop: check if V has stabilised
        if step > 200 and step % 100 == 0:
            # Check mean absolute change in V over last 10 steps
            pass  # deferred for simplicity

    # Normalise V to 0..1
    v_min, v_max = V.min(), V.max()
    if v_max > v_min:
        V = (V - v_min) / (v_max - v_min)

    return V


# ======================================================================
# Public API
# ======================================================================


@dataclass
class SpecialResourceInput:
    """Aggregated inputs for special resource generation."""
    h3_ids: List[str]
    tectonic_stress: Dict[str, float]
    elevation: Dict[str, float]
    geological_type: Dict[str, int]


def generate_resources(
    inputs: SpecialResourceInput,
    resource_types: Optional[List[ResourceType]] = None,
    seed: int = 42,
) -> Dict[str, List[float]]:
    """Run Gray-Scott for each resource type.

    Returns {h3_id: [flux_r1, flux_r2, ...]} — one float per type.
    Water cells get 0 flux for all types.
    """
    if resource_types is None:
        resource_types = default_resource_types()

    h3_ids = inputs.h3_ids
    n = len(h3_ids)
    result: Dict[str, List[float]] = {h: [] for h in h3_ids}

    # Pre-compute water body lookup
    is_ocean = np.array([inputs.geological_type.get(h, 0) == 0 for h in h3_ids], dtype=bool)

    for rtype in resource_types:
        print(f"  [Gray-Scott] {rtype.name} (F={rtype.feed_rate}, k={rtype.kill_rate}, "
              f"{rtype.timesteps} steps)")
        flux = _run_gray_scott_numpy(h3_ids, inputs.tectonic_stress, rtype, seed + hash(rtype.name) & 0xFFFF)

        for i, h in enumerate(h3_ids):
            result[h].append(0.0 if is_ocean[i] else float(flux[i]))

    return result


def default_resource_types() -> List[ResourceType]:
    return [ResourceType.veins(), ResourceType.seams(), ResourceType.diffuse()]
