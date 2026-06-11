"""Layer 0 — Long-cycle tick updates for planetary processes.

Runs at a configurable interval (typically much slower than entity ticks)
to simulate slow planetary changes:

  1. Climate drift       — temperature / precipitation random walk
  2. Resource evolution  — continued Gray-Scott integration
  3. Geological events   — stress accumulation → terrain events

Design doc § Long-Cycle Tick Updates.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .cell_model import CellData, GenerationParams
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


def _drift_climate(
    cells: List[CellData],
    drift_rate: float,
    rng: random.Random,
) -> int:
    """Apply slow random walk to temperature and precipitation.

    Args:
        cells: Current cell list (modified in-place).
        drift_rate: Max change per tick (e.g. 0.01 = 1% drift).
        rng: Seeded RNG.

    Returns:
        Number of cells whose Köppen class changed.
    """
    class_changes = 0

    for cell in cells:
        # Small random walk
        dt = rng.gauss(0.0, drift_rate)
        dp = rng.gauss(0.0, drift_rate)

        new_temp = max(0.0, min(1.0, cell.temperature + dt))
        new_precip = max(0.0, min(1.0, cell.precipitation + dp))

        cell.temperature = new_temp
        cell.precipitation = new_precip

        # Recompute Köppen class if temperature moved significantly
        if abs(dt) > drift_rate * 0.5 or abs(dp) > drift_rate * 0.5:
            # Import locally to avoid circular dependency at module level
            from .generator import _koppen_class
            new_class = _koppen_class(new_temp, new_precip, cell.temp_seasonal_range)
            if new_class != cell.climate_class:
                cell.climate_class = new_class
                class_changes += 1

    return class_changes


# ======================================================================
# Resource evolution — continue Gray-Scott integration
# ======================================================================


def _evolve_resources(
    cells: List[CellData],
    resource_types: List[ResourceType],
    steps: int,
    rng: random.Random,
) -> None:
    """Run additional Gray-Scott steps on existing resource fields.

    Requires that special_resource_flux[] was populated during generation.
    This is a simplified single-step approximation — for proper long-term
    evolution the full RD would need to be re-run.
    """
    if not cells or not resource_types:
        return

    n_resources = len(resource_types)
    if not cells[0].special_resource_flux:
        return

    # For each resource type, do approximate decay/regeneration
    for ri, rtype in enumerate(resource_types):
        for _ in range(steps):
            # Simplified: each cell's flux drifts slightly toward a
            # local equilibrium determined by neighbours and tectonic stress
            for cell in cells:
                if ri >= len(cell.special_resource_flux):
                    continue
                # Mean neighbour flux
                flux = cell.special_resource_flux[ri]
                # Small perturbation
                perturb = rng.gauss(0.0, 0.01 * rtype.feed_rate)
                new_flux = max(0.0, min(1.0, flux + perturb))
                cell.special_resource_flux[ri] = new_flux


# ======================================================================
# Geological event detection
# ======================================================================


def _check_geological_events(
    cells: List[CellData],
    stress_accumulation_rate: float,
    event_threshold: float,
    rng: random.Random,
) -> List[GeologicalEvent]:
    """Accumulate tectonic stress and fire events when threshold exceeded.

    Args:
        cells: Current cell list (modified in-place for stress).
        stress_accumulation_rate: How much stress increases per tick.
        event_threshold: Stress level that triggers an event.
        rng: Seeded RNG.

    Returns:
        List of GeologicalEvent objects that fired this tick.
    """
    events: List[GeologicalEvent] = []

    for cell in cells:
        if cell.geological_type == 0:  # ocean
            continue

        # Accumulate stress
        cell.tectonic_stress += rng.random() * stress_accumulation_rate

        if cell.tectonic_stress >= event_threshold:
            # Fire event
            mag = min(1.0, cell.tectonic_stress / (event_threshold * 2.0))
            el_delta = rng.gauss(0.0, mag * 0.05)

            event_types = ["earthquake", "volcanic_eruption", "landslide", "rift"]
            etype = rng.choice(event_types)

            # Reset stress after event
            cell.tectonic_stress = 0.0

            # Modify elevation
            cell.elevation = max(0.0, min(1.0, cell.elevation + el_delta))
            cell.hazard_level = min(1.0, cell.hazard_level + mag * 0.3)

            events.append(GeologicalEvent(
                type=etype,
                h3_id=cell.h3_id,
                affected_cells=[cell.h3_id],
                magnitude=mag,
                elevation_delta=el_delta,
                hazard_spike=mag * 0.3,
                description=f"{etype} (mag={mag:.2f}) at cell {cell.h3_id[:8]}...",
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
