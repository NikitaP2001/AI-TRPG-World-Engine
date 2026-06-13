"""Initial fauna and flora seeding after world generation.

Seeds initial population_density fields and registers WM-authored
species in FAUNA_REGISTRY / PFT_REGISTRY after the world is generated
but before the first TimeEngine tick runs.

Usage:
    from simulation.layer1.initial_seeding import seed_initial_fauna
    seed_initial_fauna(db, cells, wm_fauna_species)
"""
from __future__ import annotations

import h3
import numpy as np
from typing import Any, Dict, List

from .fauna_registry import FAUNA_REGISTRY, register_fauna_species, FaunaSpeciesDef


def seed_initial_fauna(
    db: Any,
    cells: List[Any],
    wm_fauna_species: List[dict],
) -> int:
    """Seed initial fauna populations after world generation.

    For each registered species (both WM-authored and defaults), evaluates
    habitat suitability across all land/water cells and seeds 10% of
    carrying capacity where suitability > 0.3.

    Args:
        db: WorldDB instance for saving fauna populations.
        cells: List of CellData from generator.
        wm_fauna_species: List of WM concept dicts with concept_type="fauna_species".

    Returns:
        Number of (species, cell) pairs seeded.
    """
    # 1. Register WM species (if not already registered by generator)
    for sp in wm_fauna_species:
        p = sp.get("parameters") or {}
        sp_id = sp["concept_id"]
        if sp_id not in FAUNA_REGISTRY:
            register_fauna_species(
                species_id=sp_id,
                species=FaunaSpeciesDef(
                    name=p.get("name", sp_id),
                    habitat_type=p.get("habitat_type", "terrestrial"),
                    habitat_biomes=p.get("habitat_biomes", []),
                    diet=p.get("diet", "herbivore"),
                    diet_sources=p.get("diet_sources", {}),
                    population_density_max=float(p.get("population_density_max", 10.0)),
                    base_birth=float(p.get("base_birth", 0.01)),
                    base_death=float(p.get("base_death", 0.01)),
                    migration_rate=float(p.get("migration_rate", 0.05)),
                    huntable=bool(p.get("huntable", True)),
                    emergence_population_threshold=float(p.get("emergence_population_threshold", 0.0)),
                    hazard_weight=float(p.get("hazard_weight", -1.0)),
                    size_class=p.get("size_class", "medium"),
                    plankton_consumption_rate=float(p.get("plankton_consumption_rate", 0.0)),
                ),
            )

    if not FAUNA_REGISTRY:
        return 0

    # 2. Build field arrays for fast suitability estimation
    # Collect per-cell data
    cell_data = []
    import math
    for cell in cells:
        latlng = h3.cell_to_latlng(cell.h3_id)
        lat, lon = float(latlng[0]), float(latlng[1])
        is_ocean = cell.elevation_mean <= 0.0 and cell.geological_type == 0
        cell_data.append({
            "h3_id": cell.h3_id,
            "lat": lat,
            "lon": lon,
            "elevation": cell.elevation_mean,
            "is_ocean": is_ocean,
            "temperature": cell.temperature,
            "precipitation": cell.precipitation,
            "soil_fertility": cell.soil_fertility,
        })

    # 3. Seed each species
    total_seeded = 0
    fauna_rows = []

    for species_id, sp_def in FAUNA_REGISTRY.items():
        seed_count = 0
        for cd in cell_data:
            suit = _estimate_seed_suitability(sp_def, cd)
            if suit > 0.3:
                density = suit * 0.1 * sp_def.population_density_max
                if density > 1e-8:
                    fauna_rows.append({
                        "h3_id": cd["h3_id"],
                        "species_id": species_id,
                        "density": density,
                        "updated_at_tick": 0,
                    })
                    seed_count += 1

        if seed_count > 0:
            total_seeded += seed_count

    # 4. Save to database
    if fauna_rows:
        db.save_fauna_populations(fauna_rows)

    return total_seeded


def _estimate_seed_suitability(
    sp_def: FaunaSpeciesDef,
    cd: dict,
) -> float:
    """Estimate initial habitat suitability for seeding.

    Simple biome-based estimation using cell climate fields.
    Returns 0.0-1.0.
    """
    habitat_type = sp_def.habitat_type

    # Habitat type filtering
    if habitat_type == "aquatic" and not cd["is_ocean"]:
        return 0.0
    if habitat_type == "terrestrial" and cd["is_ocean"]:
        return 0.0
    if habitat_type == "amphibious":
        # Moist areas near water
        if cd["precipitation"] < 0.2:
            return 0.0

    # Temperature suitability (gaussian centered on 0.5)
    temp = cd["temperature"]
    temp_suit = math.exp(-((temp - 0.5) ** 2) / 0.15)

    # Precipitation suitability
    precip = cd["precipitation"]
    if habitat_type == "aquatic":
        precip_suit = 1.0  # ocean cells don't depend on precip
    else:
        precip_suit = math.exp(-((precip - 0.4) ** 2) / 0.25)

    # Combine
    suit = temp_suit * precip_suit

    # Biome list filter (if specified) — use continuous fields instead of
    # ad-hoc classification. habitat_biomes is a soft preference, not hard rule.
    if sp_def.habitat_biomes and False:  # disabled — use continuous suitability below
        pass

    return min(1.0, max(0.0, suit))
