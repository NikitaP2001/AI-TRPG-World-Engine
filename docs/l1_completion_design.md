# L1 Completion Design

*Fauna Population Dynamics and Remaining Layer 1 Work*

---

## Current State

L1 has a working causal engine (`SimEngine`, two-phase compute/commit, `FieldRegistry`
with continuous KDTree-IDW fields and persistent/temporary effect stacks) and four
working features: `Vegetation` (continuous PFT-based canopy/biomass), `Lake`
(Priority-Flood depression filling with water balance), `Wetland` (hydroperiod
classification with peat accumulation), and `Groundwater` (water table dynamics).
`BiomeRegion` is a read-only classification label derived from the above, not an
independent feature.

**What is missing**, against the design documents already produced:

1. **Fauna populations entirely.** No `fauna_species` registry, no
   `current_population[species_id]` field anywhere in `CellData`, `WorldDB`, or
   `FieldRegistry`. L2/L2.5 §1.1 and §2 reference `L1:current_population[species]` as
   a flow source — this field does not exist.
2. **`harvest_yield`/`drops` connection.** `flora_pft.harvest_yield` (vegetation →
   L2 stocks) and `fauna_species.drops` (fauna → items/materials) have no
   implementation path — `Vegetation` writes `canopy_density`/`biomass`/soil feedback
   only, with no yield extraction.
3. **Predator-prey dynamics.** `fauna_species.diet`/`diet_sources` imply
   Lotka-Volterra-style interaction between species populations — nothing computes
   this.
4. **Proto-faction emergence threshold (R4, L2/L2.5 §13).** The 0.0→0.3 transition
   from "pure L1 ecology" to "proto-faction with named leaders" has no mechanism —
   there's no point at which a `fauna_species` population becomes eligible to spawn
   an L3 entity (a goblin warchief) or register as a `define_faction` with
   `social_complexity > 0`.
5. **Encounter probability and L1→L2 hazard feedback.** L2/L2.5 §14 references
   `encounter_probability` from L1 as a hazard modifier for frontier territories —
   not computed anywhere.
6. **Migration as L1 dynamics.** Species ranges should shift with `habitat_suitability`
   changes (climate drift, vegetation succession) — currently nothing models
   population redistribution.

This document designs all six, following the same architectural patterns as the
existing four features (continuous fields, persistent effects, two-phase tick,
sampling-grid computation) and consistent with the WM tools / L2-L2.5 / L3
specifications already written.

---

## Part I — Data Model

### §1 The `population_density` Field

Per-species population is stored as one `MutableField` per registered
`fauna_species`, named `population_density[species_id]`. This mirrors how
`canopy_density`/`biomass` work for vegetation — a continuous field over the sphere,
queried at any (lat, lon), with persistent effects representing the current state and
temporary effects representing this-tick changes.

**Why a field per species, not a column per species in `cells`:** the `cells` table
is a fixed wide schema. `fauna_species` is open-ended and world-defined — a fantasy
world might register 40 species, a sci-fi world 5. A flat column-per-species approach
either pre-allocates columns for species that don't exist in this world (waste) or
requires schema migration every time the WM registers a new species (fragile). A
separate table keyed by `(h3_id, species_id)` — analogous to how `features` is a
separate table from `cells` — is the right structure.

**New table: `fauna_populations`**

```sql
CREATE TABLE IF NOT EXISTS fauna_populations (
    h3_id        TEXT NOT NULL,
    species_id   TEXT NOT NULL,
    density      REAL DEFAULT 0.0,   -- individuals per cell, current value
    updated_at_tick INTEGER DEFAULT 0,
    PRIMARY KEY (h3_id, species_id)
);

CREATE INDEX IF NOT EXISTS idx_fauna_species ON fauna_populations(species_id);
```

Only cells with `density > epsilon` need rows — most (species, cell) pairs are zero
and are simply absent, keeping the table sparse. `WorldDB` gains
`load_fauna_populations(species_id=None) -> list[dict]` and
`save_fauna_populations(rows: list[dict]) -> None`, following the same
load/save-as-CellData pattern as `load_cells_as_celldata`/`save_cells`.

### §2 The `fauna_species` Registry

A new module `simulation/layer1/fauna_registry.py`, structurally parallel to
`simulation/layer0/plant_registry.py`:

```python
@dataclass
class FaunaSpeciesDef:
    """One fauna species — maps to the fauna_species concept in define_world_concept."""

    name: str
    existence_type: str = "mortal"          # registered existence_type concept_id

    # ── Habitat suitability ──────────────────────────────────────────
    habitat_biomes: List[str] = field(default_factory=list)
    # Suitability modifier expressions, evaluated against L0/L1 fields.
    # Stored as raw strings; evaluated via the shared condition expression
    # grammar (same evaluator used for entity action rule conditions).
    habitat_suitability_modifiers: List[str] = field(default_factory=list)

    # ── Demographics ──────────────────────────────────────────────────
    base_birth: float = 0.01      # per-tick birth rate at full suitability
    base_death: float = 0.01      # per-tick background death rate
    population_density_max: float = 10.0   # individuals/cell at suitability=1.0

    # ── Diet / trophic role ────────────────────────────────────────────
    diet: str = "herbivore"       # "herbivore" | "carnivore" | "omnivore" | (world-defined)
    diet_sources: List[str] = field(default_factory=list)
    # herbivore/omnivore: flora_pft ids grazed
    # carnivore/omnivore: fauna_species ids preyed upon

    # ── Drops ────────────────────────────────────────────────────────
    drops: List[dict] = field(default_factory=list)
    # [{ "stock_id": str, "quantity": float, "probability": float,
    #    "condition": str }]

    # ── Migration ────────────────────────────────────────────────────
    migration_rate: float = 0.05  # max fraction of local population that can
                                   # redistribute toward higher-suitability
                                   # neighbors per tick

    # ── Proto-faction emergence (R4) ───────────────────────────────────
    social_complexity_template: str = ""  # faction_template concept_id used
                                   # if/when this population crosses
                                   # emergence_population_threshold
    emergence_population_threshold: float = 0.0  # 0 = never emerges as a
                                   # faction; >0 = total population across
                                   # contiguous cells above which a
                                   # proto-faction may spawn (R4, see §6)
    emergence_leader_archetype: str = ""  # archetype_id for the L3 entity
                                   # spawned as the proto-faction's first
                                   # named leader, if emergence fires


FAUNA_REGISTRY: Dict[str, FaunaSpeciesDef] = {}

def register_fauna_species(species_id: str, species: FaunaSpeciesDef) -> None:
    FAUNA_REGISTRY[species_id] = species
```

`define_world_concept(concept_type="fauna_species", ...)` populates this registry —
the same registration pattern as `register_pft`/`register_ore_type`/`register_mineral`
in L0. No default fauna are pre-registered (unlike `PFT_REGISTRY`'s 24 defaults) —
every world's fauna is WM-defined, consistent with R1's universal setting principle.
A thin set of Earth-default species (deer, wolf, rabbit, etc.) can be provided as an
optional pre-built registration script the WM may call, but the engine itself ships
empty.

---

## Part II — The `Fauna` Feature

### §3 Structure

A new `simulation/layer1/features/fauna.py`, one `Fauna` feature instance **per
registered species** (analogous to one `Vegetation` instance globally — but fauna
needs per-species instances because each species has independent demographics, diet,
and migration parameters).

```python
class Fauna(Feature):
    """One species' population dynamics as a Layer 1 feature.

    Reads habitat suitability (climate, biome, vegetation fields) and
    diet-source population/biomass fields. Writes population_density[species_id]
    as a persistent field effect. One instance per registered fauna_species.
    """

    def __init__(self, species_id: str, feature_id: str = ""):
        if not feature_id:
            feature_id = f"fauna_{species_id}"
        self.species_id = species_id
        self.species_def = FAUNA_REGISTRY[species_id]
        super().__init__(
            feature_id=feature_id,
            name=self.species_def.name,
            geometry=None,
            feature_type="fauna",
            props={"species_id": species_id},
        )
```

### §4 Habitat Suitability

Suitability is computed per sampled grid point (same `_LAT_STEP`/`_LON_STEP = 2.0`
pattern as `Vegetation`), combining:

```
suitability = biome_match × PRODUCT(habitat_suitability_modifiers)

biome_match = 1.0 if classify_biome(...) in habitat_biomes else 0.0
              (or a soft gradient — see note below)
```

**Soft biome matching.** A hard 0/1 `biome_match` would create discontinuous
population fields at biome boundaries — population would jump rather than gradient
smoothly, which is both ecologically wrong and produces visual artifacts in the
OpenGL viewer (sharp population edges). Instead, `biome_match` is computed from the
*continuous* PFT suitability values already produced by `compute_vegetation_cell`
(the same continuous suitability used for canopy/biomass) for the PFTs associated
with `habitat_biomes` — giving a smooth 0.0–1.0 gradient that naturally tapers at
ecotones, exactly mirroring how `Vegetation` itself produces smooth canopy gradients
rather than hard biome edges.

`habitat_suitability_modifiers` expressions are evaluated using the same condition
expression grammar as L3 action rules (R9's variable registry applies equally here —
`L0.cell[elevation_mean] > 2000 → suit *= 0.3` is the same grammar as an entity rule
condition, just used as a multiplier rather than a boolean gate). This reuses the
existing expression evaluator rather than introducing a second one.

### §5 Population Update

Each tick, for each sampled grid point:

```
carrying_capacity = population_density_max × suitability

food_availability = f(diet, diet_sources, local field values)
  herbivore: SUM over diet_sources: biomass_field(diet_source) × grazing_efficiency
  carnivore: SUM over diet_sources: population_density_field(diet_source) × predation_efficiency
  omnivore:  weighted combination of both

food_adequacy = min(1.0, food_availability / (current_density × subsistence_per_individual))

birth_rate = base_birth × food_adequacy × (1 - current_density / carrying_capacity)
           # logistic term: growth slows as density approaches carrying_capacity
death_rate = base_death × (1 + max(0, 1 - food_adequacy))
           # death rate rises when food is scarce

Δdensity = current_density × (birth_rate - death_rate) × dt
```

This is the same demographic transition model already specified in L2/L2.5 §2
(`food_adequacy_factor`, logistic growth), applied at the per-species, per-cell level
rather than the faction level — L2/L2.5 §2's model is the aggregate that this feeds
into once a population crosses into proto-faction territory (§6 below). Using the
same formula shape at both layers means the transition at the emergence threshold
doesn't produce a discontinuity in dynamics, only a change in which subsystem owns
the numbers.

**Predation feedback (Lotka-Volterra coupling).** For carnivore/omnivore species,
`food_availability` reads `population_density[prey_species]` — and the carnivore's
consumption is written back as a *negative* temporary effect on the prey's
`population_density` field this same tick:

```
predation_loss = predator_density × predation_efficiency × prey_density
population_density_field(prey_species).add_effect(lat, lon, radius, -predation_loss)
```

Because `SimEngine.step()` runs phase 1 (all `compute_effects`) for all features
before phase 2 (commit), and `Fauna` instances for different species are all features
in the same engine, a predator's consumption this tick is visible to the prey
species' `compute_effects` only on the *next* tick (one-step lag, same as the
L2/L2.5 §12 one-step lag between L2/L2.5/knowledge). This is the standard
discrete-time Lotka-Volterra formulation and avoids order-dependence between
species (which species' `Fauna` instance is added to the engine first should not
matter).

### §6 Drops, Yields, and Item Production

`fauna_species.drops` fires on **entity-level kill events**, not on the L1 population
field directly. When an L3 entity with `existence_type` referencing this
`fauna_species` is dissolved via a `termination_condition` that matches a kill (e.g.
`event.type == "slain_by_hunter"`), the L3 spec's termination check (Part V §15)
evaluates `drops` and applies `spawn_entity`/`transfer_resource`/item-creation deltas
per the `probability`/`condition` fields.

This means: **most individuals in a `population_density` field never produce drops** —
they are aggregate population, not individually-tracked entities. Drops are only
relevant for individually-tracked L3 instances (a named "ancient dragon," or a hunted
individual the GM has elevated to L3 for a scene). Population-level harvesting (a
faction's hunting institution extracting meat from a deer population) is instead
modeled as an L2 flow:

```
flow: { "source": "L1:population_density[deer]", "sink": "L2:meat_stores",
        "rate": "0.001 * faction.hunting_institution_capacity",
        "rate_modifiers": ["L1.population_density[deer] < sustainable_threshold → rate * 0"] }
```

This flow *reduces* `population_density[deer]` via a `transfer_resource` delta — the
extraction is a negative temporary effect on the field, applied during L2's update
(L2/L2.5 §12 step 2a), symmetric with how predation losses are applied within L1.
`flora_pft.harvest_yield` works identically: an L2 flow reads `L1:biomass[pft_id]`
and the extraction reduces the `biomass` field.

This gives one consistent rule across the whole stack: **population/biomass fields
are reduced by flows that read them, whether the consumer is another species (L1
predation), a faction's economy (L2 extraction), or an individual entity's kill (L3
drops on a tracked individual)**. No special-casing per consumer type.

---

## Part III — Emergence and Cross-Layer Integration

### §7 Proto-Faction Emergence (R4)

`emergence_population_threshold` and `social_complexity_template` close the gap
identified in L2/L2.5 §13's complexity-scaling description, which states the
transition happens but doesn't specify the trigger.

**Emergence check**, run once per L1 step after population update:

```
For each fauna_species with emergence_population_threshold > 0:
    total_population = SUM over contiguous cells: population_density[species_id]
    # "contiguous" = connected component of cells with density > epsilon,
    # using the same adjacency the feature store uses for spatial features

    if total_population >= emergence_population_threshold
       AND no existing faction already covers this contiguous region
       AND no existing emergence for this (species_id, region) pair:

        1. Register a new faction via define_faction-equivalent internal call:
           - faction_id: auto-generated (e.g. "{species_id}_tribe_{region_hash}")
           - social_complexity: 0.3 (proto-faction tier, L2/L2.5 §13)
           - template_id: social_complexity_template
           - territory_cells: the contiguous region's cell set
           - founding_population: total_population

        2. Spawn one L3 entity as the faction's first council_member:
           - archetype_id: emergence_leader_archetype
           - faction_id: the new faction
           - authority_overrides: [{ faction_id: new_faction, authority_weight: 1.0,
                                      domain: "all" }]

        3. Fire a narrative_flag event: "A goblin tribe in [region] has organized
           under a warchief" — WM notification per subscribe_to_events default
           "after"-timing for narratively-significant emergence events
```

**Why this lives in L1, not L2.** The trigger condition (`total_population` crossing
a threshold) is fundamentally an L1 quantity — it would be circular for L2 to watch
its own faction's `population` stock for a faction that doesn't exist yet. L1 is the
natural owner of "is there now enough of this species in one place for social
structure to make sense," and the *result* of emergence (a new `define_faction` +
`define_entity` registration) is what L2/L2.5 then takes over running.

**After emergence**, the contiguous cells' `population_density[species_id]` field
becomes the new faction's `population` stock (L2/L2.5 §2) going forward — L1's
`Fauna` feature for this species should exclude cells now under faction territory
from its own demographic update (the faction's L2 demographic model, §2, now owns
those cells' population dynamics), while continuing to model `population_density` for
the same species in *other* cells not yet part of any faction (wild populations of
the same species existing alongside an organized tribe — e.g. feral goblins outside
the tribe's territory).

### §8 Encounter Probability

L2/L2.5 §14 references `encounter_probability` as an L1→L2 hazard input. This is
computed per cell as a derived field (not stored — computed on query, like
`biome_key`):

```
encounter_probability(cell) =
    SUM over fauna_species with diet == "carnivore" or hazard-tagged existence_types:
        population_density[species_id](cell) / population_density_max[species_id]
        × hazard_weight[species_id]
```

`hazard_weight` is an optional field on `fauna_species` (default 1.0 for carnivores,
0.0 for herbivores, world-defined for special cases — a `mana_grazer` herbivore that
is nonetheless dangerous would set this explicitly). `encounter_probability` feeds
`L0.cell[hazard_level]` as a *read-only addition* for L2's `safety_factor`
(L2/L2.5 §2) — frontier cells with high wild predator density are less safe for
faction expansion, without the WM needing to manually tag hazard zones.

### §9 Migration / Range Shift

Each tick, after the local population update (§5), a redistribution pass moves
population toward higher-suitability neighboring cells:

```
For each cell with population_density[species_id] > 0:
    Δsuitability_to_neighbor = suitability(neighbor) - suitability(this_cell)
    if Δsuitability_to_neighbor > 0:
        migrating_fraction = min(migration_rate,
                                  migration_rate × Δsuitability_to_neighbor)
        flow_amount = population_density(this_cell) × migrating_fraction
        # temporary effects: -flow_amount here, +flow_amount at neighbor
```

This is what causes species ranges to track climate drift (L0 long-cycle updates,
`set_world_orientation.climate_drift_rate`) over long timescales — as a region's
climate shifts outside a species' `habitat_suitability_modifiers` envelope,
population gradually redistributes toward regions that remain suitable, rather than
simply dying off in place. Combined with §7, a sustained climate shift could cause a
proto-faction's territory to become unsuitable for its founding species — a
narratively rich slow-motion event (the orcs' homeland is drying out) that emerges
from physics rather than being scripted.

---

## Part IV — TimeEngine Integration

### §10 `_run_l1_step` Changes

```python
# Existing imports plus:
from .layer1.features.fauna import Fauna
from .layer1.fauna_registry import FAUNA_REGISTRY

# After Vegetation is added:
for species_id in FAUNA_REGISTRY:
    engine.add_feature(Fauna(species_id))

# After engine.step(dt=...):
# Propagate population_density fields back to fauna_populations table
fauna_rows = []
for species_id in FAUNA_REGISTRY:
    pop_f = fields.get_mutable(f"population_density[{species_id}]")
    for cell in cells:
        lat, lon = ... # from h3
        density = max(0.0, pop_f(lat, lon))
        if density > 1e-6:
            fauna_rows.append({"h3_id": cell.h3_id, "species_id": species_id,
                               "density": density, "updated_at_tick": new_tick})
self.db.save_fauna_populations(fauna_rows)

# Run emergence check (§7) after population save
emergence_events = check_fauna_emergence(self.db, FAUNA_REGISTRY)
# emergence_events feed the WM notification queue per subscribe_to_events
```

`FieldRegistry.from_cells()` needs a corresponding addition:
`register_mutable(f"population_density[{species_id}]", MutableField(base=None,
default=0.0))` for each registered species, populated from
`load_fauna_populations(species_id)` rather than `from_cells` (since population isn't
a `CellData` attribute).

### §11 Performance Note

§9's migration pass is O(cells × neighbors × species) — at `_LAT_STEP/_LON_STEP = 2.0`
sampling (~16,000 sample points globally) with, say, 20 registered species, this is
~320,000 neighbor evaluations per L1 step. This is comparable in cost to
`Vegetation`'s existing per-PFT sampling loop (24 PFTs × same grid) and should run at
similar speed. If a world registers a very large number of species,
`emergence_population_threshold == 0` species (those that will never become
factions — most wildlife) could run at a coarser sampling resolution than
threshold-bearing species, since their only consumers are L2 extraction flows
(which read interpolated field values, not the raw sample grid) and predation
coupling (§5), neither of which requires fine resolution for ecological plausibility.
This is an optimization to apply if profiling shows it necessary, not a requirement
for first implementation.

---

## Part V — Summary of New Components

| Component | Location | Purpose |
|---|---|---|
| `fauna_populations` table | `world_db.py` schema | Sparse per-(cell, species) population storage |
| `FaunaSpeciesDef` / `FAUNA_REGISTRY` | `layer1/fauna_registry.py` (new) | WM-registered species, parallel to `PFT_REGISTRY` |
| `Fauna` feature | `layer1/features/fauna.py` (new) | One instance per species; habitat suitability, demographics, predation, migration |
| `population_density[species_id]` fields | `FieldRegistry` | One `MutableField` per registered species |
| `check_fauna_emergence()` | `layer1/emergence.py` (new) | R4 proto-faction spawn trigger, §7 |
| `encounter_probability` | derived query, not stored | L1→L2 hazard feedback, §8 |
| L2 extraction flows | `define_faction.flows` (WM-authored) | `harvest_yield`/population extraction, §6 |

This closes all six gaps identified in "Current State" using only patterns already
established by the existing four L1 features — no new architectural concepts beyond
what `Vegetation`/`Lake`/`Wetland`/`Groundwater` already demonstrate, and full
consistency with the WM tools (`fauna_species` registration), L2/L2.5
(`current_population`, `encounter_probability`, complexity scaling §13), and L3
(`drops`, termination-driven item creation) specifications already written.
