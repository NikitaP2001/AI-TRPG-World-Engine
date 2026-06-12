"""Fauna Species Registry — species definitions for population dynamics.

Each fauna_species registered via define_world_concept(concept_type="fauna_species")
populates this registry. Mirrors layer0/plant_registry.py pattern.

No default species are pre-registered (R1: universal setting principle).
Use simulation.layer1.default_fauna.register_default_fauna() for ~50 earth-like species.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class FaunaSpeciesDef:
    """One fauna species — maps to fauna_species concept in define_world_concept.

    Registered via define_world_concept(concept_type="fauna_species").
    """

    # ── Identity ─────────────────────────────────────────────────────
    name: str
    existence_type: str = "mortal"          # registered existence_type concept_id

    # ── Habitat type ─────────────────────────────────────────────────
    habitat_type: str = "terrestrial"
    # "terrestrial" — land cells, suitability from vegetation+climate
    # "aquatic"     — water cells only, suitability from temp+plankton
    # "aerial"      — any cells, wide suitability, higher migration
    # "amphibious"  — land+water, suitability = min(land_suit, water_suit)

    # ── Habitat suitability ──────────────────────────────────────────
    habitat_biomes: List[str] = field(default_factory=list)
    # Suitability modifier expressions, same grammar as entity action rules.
    # e.g. "L0.cell[elevation_mean] > 2000 -> suit *= 0.3"
    habitat_suitability_modifiers: List[str] = field(default_factory=list)

    # ── Demographics ─────────────────────────────────────────────────
    base_birth: float = 0.01       # per-tick birth rate at full suitability
    base_death: float = 0.01       # per-tick background death rate
    population_density_max: float = 10.0  # individuals/cell at suitability=1.0

    # ── Diet / trophic role ──────────────────────────────────────────
    diet: str = "herbivore"         # "herbivore" | "carnivore" | "omnivore" | (world-defined)

    # diet_sources maps prey/flora/plankton ID -> efficiency per tick.
    # For herbivore/omnivore: flora_pft ids or "biomass[group]" -> efficiency
    # For carnivore/omnivore: fauna_species ids -> efficiency
    # Special key "plankton" -> plankton_consumption (for aquatic species)
    # Special key "biomass"  -> generic grazing (for generalist herbivores)
    # Special key "scavenge" -> generic carrion (for scavengers)
    # Example: {"deer": 0.15, "rabbit": 0.20, "scavenge": 0.02}
    diet_sources: Dict[str, float] = field(default_factory=dict)

    # For aquatic species: direct plankton consumption rate
    # (alternative to "plankton" key in diet_sources for filter-feeders)
    plankton_consumption_rate: float = 0.0

    # ── Drops (L3 entity kill events) ────────────────────────────────
    drops: List[dict] = field(default_factory=list)

    # ── Hunting ──────────────────────────────────────────────────────
    huntable: bool = True

    hazard_weight: float = -1.0
    # -1 = auto (1.0 for carnivore, 0.0 for herbivore)
    # 0-1 = explicit

    # ── Migration ────────────────────────────────────────────────────
    migration_rate: float = 0.05

    # ── Size class (affects carrying capacity ratios) ────────────────
    size_class: str = "medium"
    # "micro" (<0.01), "small" (0.01-0.1), "medium" (0.1-10),
    # "large" (10-100), "huge" (100-1000), "legendary" (>1000 kg)

    # ── Proto-faction emergence (R4) ─────────────────────────────────
    social_complexity_template: str = ""
    emergence_population_threshold: float = 0.0
    emergence_leader_archetype: str = ""


# ── Registry ───────────────────────────────────────────────────────

FAUNA_REGISTRY: Dict[str, FaunaSpeciesDef] = {}


def register_fauna_species(species_id: str, species: FaunaSpeciesDef) -> None:
    """Register a species, updating if exists."""
    FAUNA_REGISTRY[species_id] = species


def get_species_ids() -> List[str]:
    return list(FAUNA_REGISTRY.keys())


def get_hazard_weight(species_id: str) -> float:
    """Return hazard_weight for encounter_probability computation."""
    sp = FAUNA_REGISTRY.get(species_id)
    if sp is None:
        return 0.0
    if sp.hazard_weight >= 0:
        return sp.hazard_weight
    if sp.diet == "carnivore":
        return 1.0
    if sp.diet == "omnivore":
        return 0.5
    return 0.0
