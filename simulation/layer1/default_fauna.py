"""Default Earth-like fauna species (~50) — optional, callable on demand.

Call register_default_fauna() after the simulation starts to populate
FAUNA_REGISTRY with a balanced set of terrestrial, aquatic, and aerial
species forming a plausible food web.

This is a convenience for quick world setup. The WM can instead define
each species individually via define_world_concept(concept_type="fauna_species").
"""
from __future__ import annotations

from .fauna_registry import register_fauna_species, FaunaSpeciesDef


def register_default_fauna() -> None:
    """Register ~50 default Earth-like species into FAUNA_REGISTRY.

    Call once at world start if default fauna is desired.
    Safe to call multiple times — updates existing registrations.
    """
    _register_mammals()
    _register_birds()
    _register_fish()
    _register_reptiles_amphibians()
    _register_insects()


# ──── HELPERS ────────────────────────────────────────────────────────

def _h(habitat: str) -> str:
    return habitat

def _d(diet: str) -> str:
    return diet

H = _h
D = _d


# ──── MAMMALS (17) ───────────────────────────────────────────────────

def _register_mammals() -> None:
    # --- Herbivores (8) ---
    register_fauna_species("deer", FaunaSpeciesDef(
        name="Deer", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.05},
        base_birth=0.015, base_death=0.01, population_density_max=8.0,
        migration_rate=0.04, hazard_weight=0.0, huntable=True,
        size_class="large",
    ))
    register_fauna_species("sheep", FaunaSpeciesDef(
        name="Sheep", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.06},
        base_birth=0.02, base_death=0.012, population_density_max=15.0,
        migration_rate=0.03, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("moose", FaunaSpeciesDef(
        name="Moose", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.04},
        base_birth=0.008, base_death=0.008, population_density_max=3.0,
        migration_rate=0.02, hazard_weight=0.3, huntable=True,
        size_class="huge", habitat_suitability_modifiers=["L0.cell[elevation_mean] > 0.003 -> suit *= 0.5"],
    ))
    register_fauna_species("elk", FaunaSpeciesDef(
        name="Elk", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.045},
        base_birth=0.012, base_death=0.009, population_density_max=6.0,
        migration_rate=0.03, hazard_weight=0.0, huntable=True,
        size_class="large",
    ))
    register_fauna_species("bison", FaunaSpeciesDef(
        name="Bison", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.04},
        base_birth=0.01, base_death=0.008, population_density_max=5.0,
        migration_rate=0.02, hazard_weight=0.4, huntable=True,
        size_class="huge",
    ))
    register_fauna_species("rabbit", FaunaSpeciesDef(
        name="Rabbit", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.08},
        base_birth=0.06, base_death=0.03, population_density_max=40.0,
        migration_rate=0.06, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("hare", FaunaSpeciesDef(
        name="Hare", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.07},
        base_birth=0.05, base_death=0.025, population_density_max=30.0,
        migration_rate=0.07, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("beaver", FaunaSpeciesDef(
        name="Beaver", habitat_type="amphibious", diet="herbivore",
        diet_sources={"biomass": 0.06},
        base_birth=0.015, base_death=0.012, population_density_max=10.0,
        migration_rate=0.02, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))

    # --- Omnivores (5) ---
    register_fauna_species("bear", FaunaSpeciesDef(
        name="Bear", habitat_type="terrestrial", diet="omnivore",
        diet_sources={"biomass": 0.03, "deer": 0.08, "rabbit": 0.10, "fish": 0.15, "scavenge": 0.05},
        base_birth=0.006, base_death=0.006, population_density_max=1.5,
        migration_rate=0.02, hazard_weight=0.9, huntable=True,
        size_class="huge",
    ))
    register_fauna_species("boar", FaunaSpeciesDef(
        name="Boar", habitat_type="terrestrial", diet="omnivore",
        diet_sources={"biomass": 0.06, "scavenge": 0.03},
        base_birth=0.02, base_death=0.015, population_density_max=12.0,
        migration_rate=0.03, hazard_weight=0.5, huntable=True,
        size_class="large",
    ))
    register_fauna_species("fox", FaunaSpeciesDef(
        name="Fox", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"rabbit": 0.20, "hare": 0.18, "scavenge": 0.04},
        base_birth=0.015, base_death=0.015, population_density_max=4.0,
        migration_rate=0.05, hazard_weight=0.3, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("badger", FaunaSpeciesDef(
        name="Badger", habitat_type="terrestrial", diet="omnivore",
        diet_sources={"biomass": 0.04, "scavenge": 0.03},
        base_birth=0.012, base_death=0.015, population_density_max=5.0,
        migration_rate=0.02, hazard_weight=0.3, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("raccoon", FaunaSpeciesDef(
        name="Raccoon", habitat_type="terrestrial", diet="omnivore",
        diet_sources={"biomass": 0.05, "scavenge": 0.05},
        base_birth=0.025, base_death=0.02, population_density_max=15.0,
        migration_rate=0.04, hazard_weight=0.1, huntable=True,
        size_class="small",
    ))

    # --- Predators (4) ---
    register_fauna_species("wolf", FaunaSpeciesDef(
        name="Wolf", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"deer": 0.15, "elk": 0.10, "rabbit": 0.20, "sheep": 0.18},
        base_birth=0.01, base_death=0.01, population_density_max=3.0,
        migration_rate=0.04, hazard_weight=1.0, huntable=True,
        size_class="large",
        emergence_population_threshold=50.0,
        social_complexity_template="proto_wolf_pack",
        emergence_leader_archetype="wolf_alpha",
    ))
    register_fauna_species("lynx", FaunaSpeciesDef(
        name="Lynx", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"rabbit": 0.25, "hare": 0.22, "deer": 0.05},
        base_birth=0.008, base_death=0.01, population_density_max=2.0,
        migration_rate=0.03, hazard_weight=0.8, huntable=True,
        size_class="medium",
        habitat_suitability_modifiers=["L0.cell[elevation_mean] > 0.002 -> suit *= 1.5"],
    ))
    register_fauna_species("otter", FaunaSpeciesDef(
        name="Otter", habitat_type="amphibious", diet="carnivore",
        diet_sources={"fish": 0.25, "plankton": 0.05},
        base_birth=0.012, base_death=0.015, population_density_max=5.0,
        migration_rate=0.03, hazard_weight=0.1, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("mink", FaunaSpeciesDef(
        name="Mink", habitat_type="amphibious", diet="carnivore",
        diet_sources={"fish": 0.20, "rabbit": 0.10},
        base_birth=0.015, base_death=0.018, population_density_max=4.0,
        migration_rate=0.04, hazard_weight=0.3, huntable=True,
        size_class="small",
    ))


# ──── BIRDS (15) ─────────────────────────────────────────────────────

def _register_birds() -> None:
    # --- Herbivores / Granivores (4) ---
    register_fauna_species("sparrow", FaunaSpeciesDef(
        name="Sparrow", habitat_type="aerial", diet="herbivore",
        diet_sources={"biomass": 0.10},
        base_birth=0.08, base_death=0.04, population_density_max=80.0,
        migration_rate=0.15, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))
    register_fauna_species("pigeon", FaunaSpeciesDef(
        name="Pigeon", habitat_type="aerial", diet="herbivore",
        diet_sources={"biomass": 0.09},
        base_birth=0.06, base_death=0.03, population_density_max=60.0,
        migration_rate=0.10, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))
    register_fauna_species("goose", FaunaSpeciesDef(
        name="Goose", habitat_type="amphibious", diet="herbivore",
        diet_sources={"biomass": 0.07, "plankton": 0.03},
        base_birth=0.02, base_death=0.015, population_density_max=20.0,
        migration_rate=0.12, hazard_weight=0.2, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("swan", FaunaSpeciesDef(
        name="Swan", habitat_type="amphibious", diet="herbivore",
        diet_sources={"plankton": 0.06, "biomass": 0.04},
        base_birth=0.01, base_death=0.01, population_density_max=5.0,
        migration_rate=0.08, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))

    # --- Insectivores (3) ---
    register_fauna_species("swallow", FaunaSpeciesDef(
        name="Swallow", habitat_type="aerial", diet="carnivore",
        diet_sources={"plankton": 0.15},  # aerial insects as simplified plankton
        base_birth=0.07, base_death=0.04, population_density_max=50.0,
        migration_rate=0.20, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))
    register_fauna_species("woodpecker", FaunaSpeciesDef(
        name="Woodpecker", habitat_type="aerial", diet="carnivore",
        diet_sources={"biomass": 0.08},  # insects in wood
        base_birth=0.03, base_death=0.025, population_density_max=10.0,
        migration_rate=0.04, hazard_weight=0.0, huntable=False,
        size_class="small",
        habitat_suitability_modifiers=["L0.cell[elevation_mean] < 0.001 -> suit *= 0.3"],
    ))
    register_fauna_species("nightingale", FaunaSpeciesDef(
        name="Nightingale", habitat_type="aerial", diet="carnivore",
        diet_sources={"plankton": 0.12},
        base_birth=0.04, base_death=0.03, population_density_max=20.0,
        migration_rate=0.15, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))

    # --- Predatory birds (5) ---
    register_fauna_species("seagull", FaunaSpeciesDef(
        name="Seagull", habitat_type="aerial", diet="omnivore",
        diet_sources={"fish": 0.20, "plankton": 0.08, "scavenge": 0.10},
        base_birth=0.025, base_death=0.02, population_density_max=25.0,
        migration_rate=0.15, hazard_weight=0.1, huntable=False,
        size_class="medium",
        habitat_suitability_modifiers=["L0.cell[elevation_mean] < 0.0 -> suit *= 2.0"],
    ))
    register_fauna_species("hawk", FaunaSpeciesDef(
        name="Hawk", habitat_type="aerial", diet="carnivore",
        diet_sources={"rabbit": 0.15, "hare": 0.12, "sparrow": 0.18, "pigeon": 0.15},
        base_birth=0.012, base_death=0.012, population_density_max=2.0,
        migration_rate=0.12, hazard_weight=0.6, huntable=False,
        size_class="medium",
    ))
    register_fauna_species("eagle", FaunaSpeciesDef(
        name="Eagle", habitat_type="aerial", diet="carnivore",
        diet_sources={"fish": 0.20, "rabbit": 0.12, "deer": 0.05, "scavenge": 0.08},
        base_birth=0.006, base_death=0.008, population_density_max=0.8,
        migration_rate=0.10, hazard_weight=0.7, huntable=False,
        size_class="large",
        habitat_suitability_modifiers=["L0.cell[elevation_mean] < 0.002 -> suit *= 0.6"],
    ))
    register_fauna_species("owl", FaunaSpeciesDef(
        name="Owl", habitat_type="aerial", diet="carnivore",
        diet_sources={"rabbit": 0.18, "hare": 0.15, "sparrow": 0.15},
        base_birth=0.01, base_death=0.012, population_density_max=2.5,
        migration_rate=0.05, hazard_weight=0.4, huntable=False,
        size_class="medium",
    ))
    register_fauna_species("raven", FaunaSpeciesDef(
        name="Raven", habitat_type="aerial", diet="omnivore",
        diet_sources={"scavenge": 0.12, "sparrow": 0.08, "biomass": 0.04},
        base_birth=0.02, base_death=0.018, population_density_max=10.0,
        migration_rate=0.08, hazard_weight=0.1, huntable=False,
        size_class="medium",
    ))

    # --- Domestic birds (3) ---
    register_fauna_species("chicken", FaunaSpeciesDef(
        name="Chicken", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.10},
        base_birth=0.10, base_death=0.04, population_density_max=50.0,
        migration_rate=0.01, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("duck", FaunaSpeciesDef(
        name="Duck", habitat_type="amphibious", diet="omnivore",
        diet_sources={"plankton": 0.08, "biomass": 0.05},
        base_birth=0.05, base_death=0.03, population_density_max=30.0,
        migration_rate=0.10, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("turkey", FaunaSpeciesDef(
        name="Turkey", habitat_type="terrestrial", diet="herbivore",
        diet_sources={"biomass": 0.07},
        base_birth=0.025, base_death=0.02, population_density_max=15.0,
        migration_rate=0.03, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))


# ──── FISH (11) ──────────────────────────────────────────────────────

def _register_fish() -> None:
    # --- Plankton-feeders (4) ---
    register_fauna_species("carp", FaunaSpeciesDef(
        name="Carp", habitat_type="aquatic", diet="herbivore",
        diet_sources={"plankton": 0.15, "biomass": 0.03},
        base_birth=0.03, base_death=0.02, population_density_max=30.0,
        migration_rate=0.02, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("crucian", FaunaSpeciesDef(
        name="Crucian Carp", habitat_type="aquatic", diet="herbivore",
        diet_sources={"plankton": 0.18},
        base_birth=0.04, base_death=0.025, population_density_max=50.0,
        migration_rate=0.02, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("roach", FaunaSpeciesDef(
        name="Roach", habitat_type="aquatic", diet="herbivore",
        diet_sources={"plankton": 0.16},
        base_birth=0.035, base_death=0.022, population_density_max=40.0,
        migration_rate=0.03, hazard_weight=0.0, huntable=True,
        size_class="small",
    ))
    register_fauna_species("bream", FaunaSpeciesDef(
        name="Bream", habitat_type="aquatic", diet="herbivore",
        diet_sources={"plankton": 0.14},
        base_birth=0.025, base_death=0.018, population_density_max=25.0,
        migration_rate=0.02, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))

    # --- Predatory fish (4) ---
    register_fauna_species("pike", FaunaSpeciesDef(
        name="Pike", habitat_type="aquatic", diet="carnivore",
        diet_sources={"roach": 0.20, "crucian": 0.18, "carp": 0.12},
        base_birth=0.015, base_death=0.015, population_density_max=4.0,
        migration_rate=0.02, hazard_weight=0.5, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("perch", FaunaSpeciesDef(
        name="Perch", habitat_type="aquatic", diet="carnivore",
        diet_sources={"roach": 0.15, "bream": 0.10, "plankton": 0.05},
        base_birth=0.02, base_death=0.018, population_density_max=15.0,
        migration_rate=0.03, hazard_weight=0.3, huntable=True,
        size_class="small",
    ))
    register_fauna_species("zander", FaunaSpeciesDef(
        name="Zander", habitat_type="aquatic", diet="carnivore",
        diet_sources={"roach": 0.18, "perch": 0.12, "bream": 0.10},
        base_birth=0.012, base_death=0.014, population_density_max=3.0,
        migration_rate=0.02, hazard_weight=0.6, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("catfish", FaunaSpeciesDef(
        name="Catfish", habitat_type="aquatic", diet="omnivore",
        diet_sources={"roach": 0.12, "carp": 0.08, "plankton": 0.06, "scavenge": 0.05},
        base_birth=0.01, base_death=0.012, population_density_max=2.0,
        migration_rate=0.01, hazard_weight=0.4, huntable=True,
        size_class="large",
    ))

    # --- Bottom-feeders (3) ---
    register_fauna_species("sturgeon", FaunaSpeciesDef(
        name="Sturgeon", habitat_type="aquatic", diet="carnivore",
        diet_sources={"plankton": 0.10, "roach": 0.08},
        base_birth=0.006, base_death=0.008, population_density_max=1.5,
        migration_rate=0.01, hazard_weight=0.1, huntable=True,
        size_class="large",
    ))
    register_fauna_species("eel", FaunaSpeciesDef(
        name="Eel", habitat_type="aquatic", diet="carnivore",
        diet_sources={"roach": 0.10, "crucian": 0.08, "plankton": 0.04},
        base_birth=0.012, base_death=0.015, population_density_max=6.0,
        migration_rate=0.03, hazard_weight=0.2, huntable=True,
        size_class="medium",
    ))
    register_fauna_species("salmon", FaunaSpeciesDef(
        name="Salmon", habitat_type="aquatic", diet="carnivore",
        diet_sources={"plankton": 0.12, "roach": 0.08},
        base_birth=0.008, base_death=0.01, population_density_max=8.0,
        migration_rate=0.20, hazard_weight=0.3, huntable=True,
        size_class="medium",
    ))


# ──── REPTILES & AMPHIBIANS (5) ──────────────────────────────────────

def _register_reptiles_amphibians() -> None:
    register_fauna_species("frog", FaunaSpeciesDef(
        name="Frog", habitat_type="amphibious", diet="carnivore",
        diet_sources={"plankton": 0.12},
        base_birth=0.06, base_death=0.04, population_density_max=60.0,
        migration_rate=0.05, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))
    register_fauna_species("toad", FaunaSpeciesDef(
        name="Toad", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"plankton": 0.10},
        base_birth=0.04, base_death=0.03, population_density_max=30.0,
        migration_rate=0.03, hazard_weight=0.0, huntable=False,
        size_class="small",
    ))
    register_fauna_species("lizard", FaunaSpeciesDef(
        name="Lizard", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"plankton": 0.10},
        base_birth=0.04, base_death=0.03, population_density_max=25.0,
        migration_rate=0.04, hazard_weight=0.0, huntable=False,
        size_class="small",
        habitat_suitability_modifiers=["L0.cell[elevation_mean] > 0.001 -> suit *= 1.5"],
    ))
    register_fauna_species("snake", FaunaSpeciesDef(
        name="Snake", habitat_type="terrestrial", diet="carnivore",
        diet_sources={"frog": 0.15, "lizard": 0.12, "rabbit": 0.08, "sparrow": 0.10},
        base_birth=0.015, base_death=0.018, population_density_max=6.0,
        migration_rate=0.03, hazard_weight=0.6, huntable=False,
        size_class="medium",
    ))
    register_fauna_species("turtle", FaunaSpeciesDef(
        name="Turtle", habitat_type="amphibious", diet="omnivore",
        diet_sources={"plankton": 0.08, "biomass": 0.04},
        base_birth=0.005, base_death=0.006, population_density_max=4.0,
        migration_rate=0.01, hazard_weight=0.0, huntable=True,
        size_class="medium",
    ))


# ──── INSECTS / MICRO (3) ────────────────────────────────────────────

def _register_insects() -> None:
    register_fauna_species("bee", FaunaSpeciesDef(
        name="Bee", habitat_type="aerial", diet="herbivore",
        diet_sources={"biomass": 0.10},
        base_birth=0.15, base_death=0.08, population_density_max=200.0,
        migration_rate=0.10, hazard_weight=0.0, huntable=False,
        size_class="micro",
        emergence_population_threshold=1000.0,
        social_complexity_template="apiary",
        emergence_leader_archetype="queen_bee",
    ))
    register_fauna_species("butterfly", FaunaSpeciesDef(
        name="Butterfly", habitat_type="aerial", diet="herbivore",
        diet_sources={"biomass": 0.08},
        base_birth=0.12, base_death=0.10, population_density_max=100.0,
        migration_rate=0.20, hazard_weight=0.0, huntable=False,
        size_class="micro",
    ))
    register_fauna_species("ant", FaunaSpeciesDef(
        name="Ant", habitat_type="terrestrial", diet="omnivore",
        diet_sources={"biomass": 0.10, "scavenge": 0.08},
        base_birth=0.20, base_death=0.10, population_density_max=500.0,
        migration_rate=0.05, hazard_weight=0.0, huntable=False,
        size_class="micro",
        emergence_population_threshold=5000.0,
        social_complexity_template="anthill",
        emergence_leader_archetype="queen_ant",
    ))
