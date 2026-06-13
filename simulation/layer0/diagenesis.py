"""Diagenesis — lithosphere column evolution over geological time.

Processes:
  1. Compaction: porosity decreases exponentially with depth/time
  2. Cementation: pore space fills with cement over time
  3. Organic maturation: kerogen → hydrocarbons with burial

Cell attributes updated:
  - porosity (new field): 0.0–0.5 fraction
  - bulk_density (new field): g/cm³
  - cementation (new field): 0.0–1.0 fraction
  - crustal_thickness_km: increases slowly with sediment
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional

from .cell_model import CellData


# ── Compaction parameters ──────────────────────────────────────────
_COMPACTION_LENGTH = 2.0       # km — e-folding depth for porosity
_INITIAL_POROSITY = 0.5       # surface porosity (shallow sediment)
_MIN_POROSITY = 0.01          # minimum porosity (fully compacted)
_CEMENTATION_TIMESCALE = 50.0  # Myr — e-folding time for cementation


def diagenesis_step(
    cells: List[CellData],
    dt_myr: float,
    sediment_thickness_change: Optional[Dict[str, float]] = None,
    rng: Optional[random.Random] = None,
) -> None:
    """Evolve lithosphere columns for dt_myr million years.

    Updates in-place on CellData objects. Adds attributes:
        c.porosity, c.bulk_density, c.cementation

    Args:
        cells: List of CellData objects (mutated).
        dt_myr: Time step in Myr.
        sediment_thickness_change: Dict of h3_id -> thickness delta [km].
        rng: Random state.
    """
    if rng is None:
        rng = random.Random(42)

    for c in cells:
        # Get or initialise diagenesis fields
        porosity = getattr(c, 'porosity', _INITIAL_POROSITY)
        cementation = getattr(c, 'cementation', 0.0)
        thickness = c.crustal_thickness_km

        # ── 1. Add sediment (from erosion) ──────────────────────
        if sediment_thickness_change:
            delta_km = sediment_thickness_change.get(c.h3_id, 0.0) * 0.001  # world units → km
            if delta_km > 0:
                thickness += delta_km
                # New sediment has high porosity
                # Blend: new porosity = weighted avg of old and fresh sediment
                old_mass = thickness - delta_km
                if old_mass > 0:
                    porosity = (porosity * old_mass + _INITIAL_POROSITY * delta_km) / thickness
                else:
                    porosity = _INITIAL_POROSITY

        # ── 2. Compaction ───────────────────────────────────────
        # Porosity decreases exponentially with depth
        # Depth approximated as thickness (crust)
        depth_km = max(0.0, thickness - 1.0)  # surface layer ~1km
        equilibrium_porosity = _INITIAL_POROSITY * math.exp(-depth_km / _COMPACTION_LENGTH)
        equilibrium_porosity = max(_MIN_POROSITY, min(_INITIAL_POROSITY, equilibrium_porosity))

        # Relax toward equilibrium (exponential decay in time)
        if dt_myr > 0:
            compaction_rate = 1.0 / (_COMPACTION_LENGTH * 10.0)  # per Myr
            porosity += (equilibrium_porosity - porosity) * (1.0 - math.exp(-compaction_rate * dt_myr))

        # ── 3. Cementation ──────────────────────────────────────
        # Cement fills pores over time
        if dt_myr > 0:
            cement_growth = (1.0 - cementation) * (1.0 - math.exp(-dt_myr / _CEMENTATION_TIMESCALE))
            cementation += cement_growth

        # ── 4. Bulk density from porosity ───────────────────────
        # grain density ~2.65 g/cm³, fluid density ~1.0 g/cm³
        grain_density = 2.65
        fluid_density = 1.0
        bulk_density = grain_density * (1.0 - porosity) + fluid_density * porosity

        # ── 5. Update thickness: compaction reduces thickness ───
        # Compaction strain = (1 - porosity) / (1 - initial_porosity)
        compaction_strain = (1.0 - porosity) / (1.0 - _INITIAL_POROSITY)
        # Only compact the sediment-added portion
        if sediment_thickness_change:
            delta_km = sediment_thickness_change.get(c.h3_id, 0.0) * 0.001
            if delta_km > 0 and thickness > 0:
                # The sediment layer compacts
                sediment_volume = delta_km  # pre-compaction
                compacted_volume = sediment_volume * compaction_strain
                thickness += compacted_volume - sediment_volume

        # Store back
        c.crustal_thickness_km = max(1.0, thickness)
        c.porosity = max(_MIN_POROSITY, min(_INITIAL_POROSITY, porosity))
        c.cementation = max(0.0, min(1.0, cementation))
        c.bulk_density = bulk_density
