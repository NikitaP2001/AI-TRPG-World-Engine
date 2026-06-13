"""Layer 0 — Long-cycle tick updates for planetary processes (continuous).

Runs at configurable intervals to simulate slow planetary changes using
WorldState continuous fields instead of CellData lists.

  1. Climate drift       — temperature / precipitation noise field
  2. Resource evolution  — Gray-Scott flux drift (via fields)
  3. Geological events   — stress accumulation → terrain events
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .resources import ResourceType


# ======================================================================
# Geological event types
# ======================================================================


@dataclass
class GeologicalEvent:
    """An event fired when tectonic stress exceeds threshold."""
    type: str                         # "earthquake" | "volcanic_eruption" | "landslide" | "rift"
    h3_id: str                        # cell where event originates
    affected_cells: List[str]         # cells impacted by this event
    magnitude: float                  # 0.0–1.0 severity
    elevation_delta: float            # change to cell elevation
    hazard_spike: float               # temporary hazard increase
    description: str                  # human-readable summary


# ======================================================================
# Climate drift
# ======================================================================


def _drift_climate_continuous(
    h3_ids: List[str],
    temp_map: Dict[str, float],
    precip_map: Dict[str, float],
    temp_range_map: Dict[str, float],
    climate_class_map: Dict[str, str],
    drift_rate: float,
    rng: random.Random,
) -> int:
    """Apply random walk to temperature/precipitation continuous fields.

    Operates on dicts (h3_id → value), NOT CellData.
    Updates temp_map, precip_map, climate_class_map in-place.

    Returns:
        Number of cells whose Köppen class changed.
    """
    from .climate import koppen_classify as _koppen_class
    class_changes = 0

    for hid in h3_ids:
        dt = rng.gauss(0.0, drift_rate)
        dp = rng.gauss(0.0, drift_rate)

        old_temp = temp_map.get(hid, 0.5)
        old_precip = precip_map.get(hid, 0.5)
        temp_range = temp_range_map.get(hid, 0.2)

        new_temp = max(0.0, min(1.0, old_temp + dt))
        new_precip = max(0.0, min(1.0, old_precip + dp))

        temp_map[hid] = new_temp
        precip_map[hid] = new_precip

        if abs(dt) > drift_rate * 0.5 or abs(dp) > drift_rate * 0.5:
            new_class = _koppen_class(new_temp, new_precip, temp_range)
            if new_class != climate_class_map.get(hid, ""):
                climate_class_map[hid] = new_class
                class_changes += 1

    return class_changes


# ======================================================================
# Resource evolution — continue Gray-Scott integration
# ======================================================================


def _evolve_resources_continuous(
    flux_map: Dict[str, List[float]],
    resource_types: List[ResourceType],
    h3_ids: List[str],
    steps: int,
    rng: random.Random,
) -> None:
    """Drift resource fluxes via continuous field (dict-based).

    Args:
        flux_map: h3_id → [flux_r1, flux_r2, ...]
        resource_types: Resource type definitions.
        h3_ids: All cell IDs.
        steps: Number of perturbation steps.
        rng: Seeded RNG.
    """
    if not h3_ids or not resource_types or not flux_map:
        return

    n_resources = len(resource_types)
    sample = next(iter(flux_map.values()))
    if len(sample) < n_resources:
        return

    for ri, rtype in enumerate(resource_types):
        for _ in range(steps):
            for hid in h3_ids:
                fluxes = flux_map.get(hid)
                if fluxes is None or ri >= len(fluxes):
                    continue
                perturb = rng.gauss(0.0, 0.01 * rtype.feed_rate)
                fluxes[ri] = max(0.0, min(1.0, fluxes[ri] + perturb))


# ======================================================================
# Geological event detection
# ======================================================================


def _check_geological_events_continuous(
    h3_ids: List[str],
    geo_type_map: Dict[str, int],
    stress_map: Dict[str, float],
    elevation_map: Dict[str, float],
    hazard_map: Dict[str, float],
    stress_accumulation_rate: float,
    event_threshold: float,
    rng: random.Random,
) -> List[GeologicalEvent]:
    """Accumulate stress and fire events via continuous dicts.

    Args:
        h3_ids: All cell IDs.
        geo_type_map: h3_id → geological_type (0=ocean).
        stress_map: h3_id → current stress (mutated in-place).
        elevation_map: h3_id → elevation (mutated on event).
        hazard_map: h3_id → hazard_level (mutated on event).
        stress_accumulation_rate: Stress added per tick.
        event_threshold: Stress level that triggers event.

    Returns:
        List of GeologicalEvent objects fired this tick.
    """
    events: List[GeologicalEvent] = []

    for hid in h3_ids:
        if geo_type_map.get(hid, 2) == 0:
            continue

        old_stress = stress_map.get(hid, 0.0)
        new_stress = old_stress + rng.random() * stress_accumulation_rate
        stress_map[hid] = new_stress

        if new_stress >= event_threshold:
            mag = min(1.0, new_stress / (event_threshold * 2.0))
            el_delta = rng.gauss(0.0, mag * 0.05)

            event_types = ["earthquake", "volcanic_eruption", "landslide", "rift"]
            etype = rng.choice(event_types)

            stress_map[hid] = 0.0
            elevation_map[hid] = max(0.0, min(1.0, elevation_map.get(hid, 0.5) + el_delta))
            hazard_map[hid] = min(1.0, hazard_map.get(hid, 0.0) + mag * 0.3)

            events.append(GeologicalEvent(
                type=etype,
                h3_id=hid,
                affected_cells=[hid],
                magnitude=mag,
                elevation_delta=el_delta,
                hazard_spike=mag * 0.3,
                description=f"{etype} (mag={mag:.2f}) at {hid[:8]}...",
            ))

    return events


# ======================================================================
# LongCycleScheduler — orchestrator
# ======================================================================


@dataclass
class LongCycleConfig:
    """Configuration for long-cycle tick behaviour."""
    interval_ticks: int = 100            # run every N entity ticks
    climate_drift_rate: float = 0.005    # max temp/precip change per tick
    resource_evolution_steps: int = 50   # Gray-Scott sub-steps per tick
    stress_accumulation_rate: float = 0.02  # stress added per tick
    event_threshold: float = 0.8         # stress level that triggers event


class LongCycleScheduler:
    """Orchestrates slow planetary processes.

    Usage:
      scheduler = LongCycleScheduler(config)
      events = scheduler.tick(cells, resource_types, turn_count)
    """

    def __init__(
        self,
        config: Optional[LongCycleConfig] = None,
        seed: int = 42,
    ) -> None:
        self.config = config or LongCycleConfig()
        self._rng = random.Random(seed)
        self._last_tick_turn: int = 0

    def tick(
        self,
        cells: List[CellData],
        resource_types: Optional[List[ResourceType]] = None,
        current_turn: int = 0,
    ) -> List[GeologicalEvent]:
        """Run one long-cycle tick if enough turns have passed.

        Args:
            cells: Current world cells (modified in-place).
            resource_types: Resource definitions for evolution step.
            current_turn: Current entity turn counter.

        Returns:
            List of geological events that fired this tick.
        """
        if current_turn - self._last_tick_turn < self.config.interval_ticks:
            return []

        self._last_tick_turn = current_turn
        events: List[GeologicalEvent] = []

        # 1. Climate drift
        n_class_changes = _drift_climate(cells, self.config.climate_drift_rate, self._rng)
        if n_class_changes:
            print(f"[LongCycle] climate drifted — {n_class_changes} cells changed class")

        # 2. Resource evolution
        if resource_types:
            _evolve_resources(cells, resource_types, self.config.resource_evolution_steps, self._rng)

        # 3. Geological events
        gevents = _check_geological_events(
            cells, self.config.stress_accumulation_rate,
            self.config.event_threshold, self._rng,
        )
        if gevents:
            print(f"[LongCycle] {len(gevents)} geological event(s)")
            events.extend(gevents)

        return events
