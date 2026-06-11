# WM / GM Tool Reference

*Authoritative interface contract for World Master and Game Master agent tools*

---

## Overview

This document defines all tools available to the World Master (WM) and Game Master (GM) agents. Each tool maps directly to one LangChain `@tool` function. Parameters marked `optional` may be omitted; the system applies documented defaults.

Tools are grouped by domain. Each entry covers: signature, parameter descriptions, return value, agent authorization, and side effects on other systems.

### Authorization Levels

| Role | Access |
|---|---|
| **WM** | All tools |
| **GM** | Read tools + limited write tools explicitly marked `GM` |
| **Scene Manager** | Read tools only |
| **Character Agent** | None — uses `ask_manager` escalation instead |

Authorization is enforced at the tool layer. Calling a WM-only tool from a GM-level agent returns an authorization error, not a result.

### Parameter Conventions

- All `feature_id`, `entity_id`, `faction_id` parameters accept either the UUID or the `display_name` string. If a name is ambiguous (multiple features share it), the tool returns a disambiguation error listing the matches.
- All distance values are in the same unit as `planet_radius` set in `set_world_orientation()`.
- All tick values are absolute simulation ticks (integers).
- `location` blocks always accept exactly one of: `absolute`, `relative`, `inside`, `near`, `region_hint`. If multiple are provided, `absolute` > `relative` > `inside` > `near` > `region_hint` in precedence.

---

## World Initialization

---

### `set_world_orientation`

Establishes the coordinate reference frame, grid parameters, and global climate baseline. Must be called once before any other tool. Cannot be called again after `place_continent()` or the generation pipeline has run.

**Authorization:** WM only

```python
@tool
def set_world_orientation(
    planet_radius: float,
    # Radius of the world body in any consistent unit.
    # All subsequent distances use this unit.
    # Determines base cell size: cell_side_length = planet_radius / 60.
    # Approximately 17,400 top-level hex cells regardless of value.

    reference_meridian: float = 0.0,
    # Longitude of the prime meridian (0° line).
    # Arbitrary — pick a meaningful location.

    axial_tilt: float = 23.5,
    # Planet axial tilt in degrees.
    # 0 = no seasons. 23.5 = Earth-like. Higher = extreme seasonality.

    global_temperature_offset: float = 0.0,
    # Degrees added to every cell's derived temperature.
    # Positive = warmer world. Negative = colder.

    global_precipitation_modifier: float = 1.0,
    # Multiplier on derived precipitation for every cell.
    # > 1.0 = wetter world. < 1.0 = drier.

    solar_intensity: float = 1.0,
    # Multiplier on total solar energy.
    # < 1.0 = dim star, cold world. > 1.0 = bright star, hot world.

    atmospheric_density: float = 1.0,
    # Temperature buffering factor.
    # Low = thin atmosphere, wild temperature swings.
    # High = thick atmosphere, greenhouse buffering.

    ocean_temperature: float = 15.0,
    # Base ocean surface temperature in world temperature units.
    # Affects maritime moderation of coastal climates.

    tectonic_activity: float = 0.5,
    # 0.0–1.0. Controls geological complexity during generation.
    # Low = stable cratons, few mountains. High = active orogeny, many ranges.

    long_cycle_tick_interval: int = 1000,
    # How many simulation ticks between long-cycle Layer 0 updates.
    # Climate drift, resource flux evolution, geological stress accumulation.

    climate_drift_rate: float = 0.0,
    # How much global_temperature_offset shifts per long-cycle tick.
    # Positive = gradual warming. Negative = gradual cooling. 0 = stable.

    special_resource_definitions: list[dict] = [],
    # World-defined special resource types.
    # Each entry: { "resource_id": str, "name": str,
    #               "gray_scott_feed": float, "gray_scott_kill": float,
    #               "diffusion_rate": float, "geological_affinity": float }
    # geological_affinity: how strongly tectonic_stress seeds this resource.

    direction_names: dict = {},
    # Optional remapping of cardinal direction labels.
    # { "north": "spinward", "south": "rimward", ... }
    # Affects natural language resolution only. Math unchanged.
) -> dict:
    # Returns:
    # {
    #   "status": "ok",
    #   "cell_side_length": float,
    #   "top_level_cell_count": int,    # always ~17,400
    #   "grid_initialized": bool
    # }
```

**Side effects:** Initializes the H3 cell grid. Creates ~17,400 empty cell condition vector records. No features created yet.

---

## Geography

---

### `place_continent`

Defines the land/ocean boundary for a landmass. Must be called before `place_feature()` terrain anchors and before the generation pipeline runs. The continent polygon is a `fixed` anchor — generation cannot place ocean inside it or land outside it.

**Authorization:** WM only

```python
@tool
def place_continent(
    name: str,
    # Display name. Used as reference in subsequent tool calls.

    outline_vertices: list[dict] = [],
    # Explicit coastline polygon as lat/lon vertex sequence.
    # Each vertex: { "lat": float, "lon": float }
    # Vertices should trace the coastline in order (CW or CCW).
    # Use this for precise known geography (adapted from source maps).

    outline_template: str = "",
    # Named template for known world shapes.
    # System-provided templates available; WM can register custom ones.
    # If provided, outline_vertices is ignored.

    outline_description: str = "",
    # Natural language shape description.
    # Compiler produces approximate polygon.
    # Used when neither vertices nor template are available.
    # Example: "large irregular landmass, widest in the north around
    #           60N, tapering to a southern peninsula around 20N,
    #           western coast running roughly north-south"

    location_absolute: dict = {},
    # { "lat": float, "lon": float }
    # Center point of the continent if using outline_description.
    # Ignored when vertices or template provided.
) -> dict:
    # Returns FeatureResult:
    # {
    #   "feature_id": str,
    #   "name": str,
    #   "geometry_type": "Polygon",
    #   "center": { "lat": float, "lon": float },
    #   "bounding_box": { "lat_min": float, "lat_max": float,
    #                     "lon_min": float, "lon_max": float },
    #   "cells_affected": int,        # cells classified as continental
    #   "coastline_length": float,    # in world units
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts Polygon into PostGIS feature store. All cells inside polygon: `geological_type = continental`. All cells outside: `geological_type = oceanic`. Boundary cells receive a `terrain_cover` coastal feature. Updates `feature_ids[]` cache for all affected cells.

---

### `place_feature`

Places a geographic feature — terrain cover, water body, elevation feature, climate zone, special resource zone, or any world-defined type. Used both before generation (as anchors) and during live play (to add features to an existing world).

**Authorization:** WM only (before generation) / WM and GM (during live play)

```python
@tool
def place_feature(
    type: str,
    # Feature type. Open string, world-defined.
    # Generation pipeline recognizes:
    #   "elevation_feature"     — mountain range, hills, plateau, valley
    #   "water_body"            — lake, inland sea, bay, fjord
    #   "river"                 — flowing water, LineString geometry
    #   "terrain_cover"         — forest, grassland, desert, tundra, wetland, etc.
    #   "climate_zone"          — forces climate_override on contained cells
    #   "geological_zone"       — modifies geological_type and tectonic_stress
    #   "special_resource_zone" — overrides special_resource_flux values
    #   "void_zone"             — suppresses generation (deep ocean, impassable waste)
    #   "mountain_pass"         — traversable gap through elevation feature
    #   "fault_line"            — geological boundary, affects tectonic_stress
    # Unknown types stored without Layer 0 effects unless layer_effects provided.

    name: str = "",
    # Display name. Empty = unnamed background feature.
    # Named features queryable by name in subsequent calls.

    # ── Location (provide exactly one) ──────────────────────────────────────

    location_absolute: dict = {},
    # { "lat": float, "lon": float, "alt": float (optional) }

    location_relative: dict = {},
    # { "from": str,        # feature_id, entity_id, or reserved:
    #                       # "player_start" | "world_center" |
    #                       # "north_pole"   | "south_pole"
    #   "bearing": float,   # degrees clockwise from north
    #   "distance": float,  # world units
    #   "alt_offset": float (optional) }

    location_inside: str = "",
    # feature_id or name — places geometrically inside parent

    location_near: list[str] = [],
    # feature_id(s) or name(s) — places adjacent to

    location_region_hint: str = "",
    # Natural language: "northern coast", "center of the continent", etc.
    # Resolved via world orientation reference frame.

    # ── Size (provide at most one) ───────────────────────────────────────────

    size_preset: str = "",
    # "tiny" | "small" | "medium" | "large" | "massive"
    # Interpreted relative to feature type and planet scale.

    size_width: float = 0.0,
    size_length: float = 0.0,
    size_radius: float = 0.0,
    size_height: float = 0.0,       # vertical extent above surroundings
    size_depth: float = 0.0,        # depth below surface
    # All in world units. Use whichever dimensions apply to the feature type.

    size_shape_description: str = "",
    # Natural language shape: "elongated north-south", "crescent shaped",
    # "follows the valley floor", "irregular with a narrow inlet"

    # ── Physical properties ──────────────────────────────────────────────────

    properties: dict = {},
    # Open key-value. Type-specific.
    # Recognized keys:
    #   navigable: bool           — supports water or road transit
    #   passable: bool            — can be traversed on foot/vehicle
    #   elevation_override: float — forces elevation at feature center
    #   feeds_into: str           — hydrological target (feature_id or "ocean")
    #   resource_richness: str    — "scarce"|"normal"|"rich"|"exceptional"
    #   climate_override: str     — Koppen-Geiger class code, forces climate
    #   hazard_modifier: float    — multiplies base hazard_level in cells
    #   depth: float              — for water bodies and caves

    # ── Layer 0 physics effects ──────────────────────────────────────────────

    layer_effects: dict = {},
    # Explicit overrides for what this feature writes to intersecting cells.
    # System applies type-default effects if omitted.
    # {
    #   "soil_fertility_modifier":     float,  (multiplier)
    #   "hazard_modifier":             float,  (multiplier)
    #   "elevation_offset":            float,  (added to cell mean elevation)
    #   "special_resource_overrides":  dict,   (resource_id → float)
    #   "water_table_modifier":        float,  (multiplier)
    #   "climate_override":            str     (Koppen-Geiger class code)
    # }

    # ── Generation behavior ──────────────────────────────────────────────────

    anchor_strength: str = "preferred",
    # "suggestion" | "preferred" | "fixed"
    # suggestion: may shift significantly for coherence
    # preferred:  minor adjustments only
    # fixed:      exact placement guaranteed, seeds generation

    relationships_feeds_into: str = "",
    # Hydrological downstream target. feature_id or "ocean".

    relationships_part_of: str = "",
    # This feature is a sub-feature of. feature_id or name.

    relationships_connected_to: list[str] = [],
    # feature_ids with navigable connections (roads, passes, straits).
) -> dict:
    # Returns FeatureResult:
    # {
    #   "feature_id": str,
    #   "name": str,
    #   "geometry_type": str,           # "Point" | "LineString" | "Polygon"
    #   "center": { "lat": float, "lon": float },
    #   "bounding_box": dict,
    #   "cells_affected": int,
    #   "layer_effects_applied": {
    #     "cells_modified": int,
    #     "fields_written": list[str]
    #   },
    #   "relationships_detected": {
    #     "contained_by": str | None,
    #     "contains": list[str],
    #     "adjacent_to": list[str],
    #     "hydrological_connections": list[str]
    #   },
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts geometry into PostGIS. Applies `layer_effects` to all intersecting cell condition vectors. Updates `feature_ids[]` cache for all affected cells. If called before generation, registers as an anchor constraint.

---

### `update_feature`

Modifies an existing feature — geometry, properties, name, or layer effects. Used during live play for world evolution: forest retreats, coastline changes, resource zone depletion.

**Authorization:** WM only

```python
@tool
def update_feature(
    feature_id: str,
    # UUID or display_name of the feature to update.

    new_name: str = "",
    # Rename the feature. Empty = no change.

    geometry_clip_region: dict = {},
    # Clips (shrinks) the feature polygon to the intersection with this region.
    # { "inside": str }  — clip to intersection with named feature
    # { "exclude_near": str, "radius": float }  — remove area near reference
    # Used for: forest retreat, lake shrinkage, erosion.

    geometry_expand_region: dict = {},
    # Expands the feature polygon.
    # { "direction": str, "amount": float }
    # Used for: desert advance, glacier retreat (lake grows), urbanization.

    properties_update: dict = {},
    # Partial update of properties. Only specified keys are changed.
    # Unspecified keys retain current values.

    layer_effects_update: dict = {},
    # Partial update of layer_effects. Only specified keys changed.
    # Triggers recalculation for all intersecting cells.

    cause: str = "",
    # Narrative reason for the change. Written to event log.
    # "centuries of farming", "volcanic activity", "magical corruption"
) -> dict:
    # Returns:
    # {
    #   "feature_id": str,
    #   "cells_affected": int,
    #   "layer_effects_recalculated": bool,
    #   "event_log_id": str,
    #   "warnings": list[str]
    # }
```

**Side effects:** Updates PostGIS geometry and/or properties. Recalculates `layer_effects` for cells that gained or lost feature intersection. Updates `feature_ids[]` cache for all affected cells. Inserts event record into R*-tree event log.

---

### `dissolve_feature`

Removes a feature from the world. Can be immediate or scheduled for gradual dissolution over time.

**Authorization:** WM only

```python
@tool
def dissolve_feature(
    feature_id: str,
    # UUID or display_name of the feature to dissolve.

    cause: str = "",
    # Narrative cause. Written to event log.

    scheduled_tick: int = 0,
    # If > current tick: schedules dissolution for that tick.
    # If 0 or <= current tick: immediate dissolution.

    gradual_over_ticks: int = 0,
    # If > 0: feature shrinks incrementally over this many ticks
    # before final dissolution. Creates a sequence of update events.
    # Used for: forest clearing, lake drying, glacier retreat.
) -> dict:
    # Returns:
    # {
    #   "feature_id": str,
    #   "dissolved_immediately": bool,
    #   "scheduled_tick": int | None,
    #   "cells_released": int,
    #   "layer_effects_reversed": bool,
    #   "event_log_id": str
    # }
```

**Side effects:** Sets `dissolved_tick` on PostGIS record. Reverts `layer_effects` on all previously affected cells. Updates `feature_ids[]` cache. Inserts dissolution event into R*-tree event log.

---

### `name_feature`

Assigns a name and optional properties to an existing unnamed feature that was auto-generated by the pipeline. Used after generation to name rivers, forests, mountain ranges that the system created but did not name.

**Authorization:** WM and GM

```python
@tool
def name_feature(
    name: str,
    # The name to assign.

    description: str = "",
    # Natural language description of the target feature.
    # Used to find it if feature_id not provided.
    # "the large river flowing west from the central mountains"

    feature_id: str = "",
    # Direct reference if already known. If provided, description ignored.

    location_near: list[str] = [],
    # Helps disambiguate if description matches multiple features.

    feature_type: str = "",
    # Filters candidates by type if description is ambiguous.

    properties_update: dict = {},
    # Optional: update properties at the same time as naming.
) -> dict:
    # Returns:
    # {
    #   "feature_id": str,
    #   "name": str,
    #   "geometry_type": str,
    #   "center": { "lat": float, "lon": float },
    #   "was_unnamed": bool,
    #   "warnings": list[str]
    # }
```

**Side effects:** Updates `name` field in PostGIS. The feature becomes referenceable by name in all subsequent tool calls.

---

## Entity Placement

---

### `place_entity`

Places a new entity or relocates an existing one. The first call with `is_player: true` registers `"player_start"` as a reserved coordinate reference.

**Authorization:** WM and GM

```python
@tool
def place_entity(
    archetype_id: str = "",
    # Archetype for new entity. Ignored if entity_id provided.

    entity_id: str = "",
    # Existing entity UUID to relocate. If provided, archetype_id ignored.

    is_player: bool = False,
    # If true and this is the first player placed: registers "player_start".
    # Subsequent player placements do not update "player_start".

    # ── Location (provide exactly one) ──────────────────────────────────────

    location_absolute: dict = {},
    # { "lat": float, "lon": float, "alt": float (optional) }
    # Required for first player placement.

    location_relative: dict = {},
    # { "from": str, "bearing": float, "distance": float,
    #   "alt_offset": float (optional) }

    location_inside: str = "",
    # feature_id or location_id — spawn inside this feature or location

    location_near: str = "",
    # feature_id, location_id, or entity_id

    facing: float = 0.0,
    # Initial facing direction, degrees clockwise from north.
    # Affects first behavior tick pathfinding.
) -> dict:
    # Returns:
    # {
    #   "entity_id": str,
    #   "resolved_location": { "lat": float, "lon": float, "alt": float },
    #   "player_start_registered": bool,
    #   "containing_features": list[str],   # feature_ids at spawn point
    #   "containing_locations": list[str],  # location_ids at spawn point
    #   "warnings": list[str]
    # }
```

**Side effects:** Registers entity in the EPL. If `is_player: true` and first player: stores world coordinate as `"player_start"` reserved reference. Updates EPL entity position index.

---

## Geological Events

---

### `trigger_geological_event`

Fires a large-scale geological or environmental event that modifies the world over time. Unlike `update_feature()` (which modifies specific known features), this triggers systemic changes with cascading simulation effects.

**Authorization:** WM only

```python
@tool
def trigger_geological_event(
    event_type: str,
    # "tectonic_subduction" | "volcanic_eruption" | "earthquake" |
    # "landslide" | "flood" | "drought" | "wildfire" |
    # "meteor_impact" | "magical_catastrophe" | (world-defined)

    affected_region: dict,
    # Specifies what area is affected.
    # { "feature_id": str }          — event centered on named feature
    # { "location_absolute": dict }  — event centered on lat/lon
    # { "radius": float }            — radius around center in world units

    magnitude: float = 0.5,
    # 0.0–1.0. Controls scale of effects.

    onset_tick: int = 0,
    # When the event begins. 0 = immediately.

    duration_ticks: int = 1,
    # How many ticks the event unfolds over.
    # 1 = instantaneous. Higher = gradual change.

    elevation_change: float = 0.0,
    # Elevation delta in world units applied to affected cells.
    # Positive = uplift. Negative = subsidence.

    hazard_spike: float = 0.0,
    # Temporary hazard_level increase. 0.0–1.0.
    # Applied immediately, decays over duration_ticks.

    volcanic_activity: bool = False,
    # If true: spawns volcanic_vent point features along fault lines
    # in affected region.

    hydrological_recompute: bool = True,
    # If true: river network and lake polygons recomputed for affected
    # region after elevation changes. Default true for elevation events.

    climate_recompute: bool = False,
    # If true: climate fields recomputed for affected region.
    # Typically only needed for very large events.

    narrative_flag: bool = True,
    # If true: sends high-priority notification to WM task queue.

    cause: str = "",
    # Narrative cause. Written to event log.
) -> dict:
    # Returns:
    # {
    #   "event_id": str,
    #   "scheduled_ticks": list[int],  # ticks at which incremental changes fire
    #   "cells_affected": int,
    #   "features_affected": list[str],
    #   "features_spawned": list[str],
    #   "event_log_id": str,
    #   "warnings": list[str]
    # }
```

**Side effects:** Schedules a sequence of cell mutations across `duration_ticks`. Spawns new features if configured. Triggers hydrological and/or climate recomputation if configured. All changes propagate through the PostGIS trigger to update `feature_ids[]` caches. All mutations written to R*-tree event log with `causal_parent` pointing to this event.

---

## Factions and Institutions

---

### `create_faction`

Creates a new faction or institution in Layer 2.5. Can represent political entities, religious organizations, guilds, military orders, or any collective social structure.

**Authorization:** WM and GM

```python
@tool
def create_faction(
    name: str,
    faction_type: str,
    # Open string. Examples: "kingdom", "guild", "religion",
    # "tribe", "corporation", "military_order", "criminal_network"

    home_region: str = "",
    # feature_id or location_id of primary territory.

    initial_strength: float = 0.5,
    # 0.0–1.0. Relative institutional strength at founding.

    ideology_vector: dict = {},
    # Domain-defined ideological attributes.
    # Used as initial position in Deffuant opinion space.
    # { "attribute_name": float (0.0–1.0), ... }

    relationships: dict = {},
    # Initial relationship states with other factions.
    # { "faction_id": float (-1.0 hostile to +1.0 allied), ... }

    norms: dict = {},
    # Initial norm vector overrides. Partial — unspecified norms
    # inherit from region Layer 2.5 state.
    # { "norm_id": float, ... }

    founding_tick: int = 0,
    # When this faction was founded. 0 = current tick.
    # Used for historical placement of pre-existing factions.
) -> dict:
    # Returns:
    # {
    #   "faction_id": str,
    #   "name": str,
    #   "founding_tick": int,
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts faction record into Layer 2.5 state. Initializes trust matrix entries for all existing factions based on `relationships`. Registers territory claim if `home_region` provided.

---

### `update_faction`

Modifies an existing faction's properties, relationships, or territory.

**Authorization:** WM and GM

```python
@tool
def update_faction(
    faction_id: str,

    new_name: str = "",
    strength_delta: float = 0.0,
    # Added to current strength. Positive = growth. Negative = decline.

    ideology_update: dict = {},
    # Partial update of ideology_vector.

    relationship_updates: dict = {},
    # { "faction_id": float, ... } — updates specific relationships.

    norm_updates: dict = {},
    # Partial update of norm vector.

    territory_add: list[str] = [],
    # feature_id(s) to add to faction territory.

    territory_remove: list[str] = [],
    # feature_id(s) to remove from faction territory.

    cause: str = "",
) -> dict:
    # Returns:
    # {
    #   "faction_id": str,
    #   "fields_updated": list[str],
    #   "event_log_id": str
    # }
```

**Side effects:** Updates Layer 2.5 faction state. Trust matrix recalculated for affected faction pairs. Territory changes propagate to Layer 2.5 norm vectors for affected regions.

---

### `dissolve_faction`

Removes a faction from the simulation. Optionally absorbs its members and territory into another faction.

**Authorization:** WM only

```python
@tool
def dissolve_faction(
    faction_id: str,
    cause: str = "",

    absorb_into: str = "",
    # faction_id to receive territory and members.
    # Empty = faction dissolves without successor.

    scheduled_tick: int = 0,
    # If > current tick: scheduled dissolution.
) -> dict:
    # Returns:
    # {
    #   "faction_id": str,
    #   "dissolved_tick": int,
    #   "territory_transferred": bool,
    #   "event_log_id": str
    # }
```

---

## World Narrative State

---

### `set_world_narrative_state`

Sets the WM's high-level narrative parameters — era, active prophecies, age transitions. Feeds into Layer 2.5 norm initialization and GM context for all scenes.

**Authorization:** WM only

```python
@tool
def set_world_narrative_state(
    era_name: str = "",
    # Current era or age name. Surfaced in GM context.

    era_parameters: dict = {},
    # Domain-defined parameters for this era.
    # Injected into Layer 2.5 norm propagation as baseline modifiers.
    # { "parameter_name": value, ... }

    active_prophecies: list[dict] = [],
    # Declared prophecy conditions tracked by the WM.
    # Each: { "id": str, "description": str,
    #         "trigger_condition": str,   # event_type or expression
    #         "fulfilled": bool }

    world_age_transitions: list[dict] = [],
    # Scheduled era transitions.
    # Each: { "from_era": str, "to_era": str,
    #         "trigger_tick": int,
    #         "trigger_condition": str }
) -> dict:
    # Returns:
    # {
    #   "status": "ok",
    #   "era_name": str,
    #   "active_prophecy_count": int
    # }
```

---

## Event System Rules

---

### `create_event_rule`

Adds a production rule to the event system. Rules fire when their condition is true and apply effects to world state. Hot-reloadable — no simulation restart required.

**Authorization:** WM only

```python
@tool
def create_event_rule(
    name: str,
    # Unique identifier for this rule.

    condition: str,
    # Boolean expression over world state variables.
    # Uses the condition expression grammar defined in the
    # Entity Definition Schema.
    # Examples:
    #   "L2.settlement.population < 0.1 * L2.settlement.founding_population
    #    AND world.time_since[settlement_founded] > 500"
    #   "L0.cell.tectonic_stress > 0.95"
    #   "L2_5.faction.strength < 0.05 AND faction.age > 200"

    effects: list[dict],
    # List of world_delta objects. Same types as entity action rule effects:
    # modify_field, spawn_entity, dissolve_entity, trigger_event, notify_wm.
    # [ { "delta_type": str, ...parameters... }, ... ]

    priority: int = 5,
    # 1–10. Higher priority rules evaluated first.

    cooldown_ticks: int = 0,
    # Minimum ticks between firings.

    scope: str = "global",
    # "global"  — evaluated once per tick against world state
    # "per_cell" — evaluated once per cell per tick
    # "per_entity" — evaluated once per active entity per tick
    # "per_faction" — evaluated once per faction per tick

    narrative_flag: bool = False,
    # If true: rule firings added to GM notification queue.

    enabled: bool = True,
) -> dict:
    # Returns:
    # {
    #   "rule_id": str,
    #   "name": str,
    #   "status": "active" | "disabled"
    # }
```

---

### `update_event_rule`

Modifies an existing event rule. Changes take effect on the next simulation tick.

**Authorization:** WM only

```python
@tool
def update_event_rule(
    rule_id: str,
    # UUID or name of the rule to update.

    condition: str = "",
    effects: list[dict] = [],
    priority: int = 0,
    cooldown_ticks: int = -1,
    enabled: bool = None,
    # Partial update — only non-default values are applied.
) -> dict:
    # Returns:
    # { "rule_id": str, "fields_updated": list[str] }
```

---

### `delete_event_rule`

Permanently removes an event rule.

**Authorization:** WM only

```python
@tool
def delete_event_rule(
    rule_id: str,
) -> dict:
    # Returns:
    # { "rule_id": str, "deleted": bool }
```

---

## World Debt System

---

### `commit_fact`

Declares a world fact that does not yet have full simulation infrastructure. Immediately commits the fact as a stub to world state with a reservation lock, then optionally schedules async build-out tasks. Used when the WM invents a fact to resolve a narrative escalation.

**Authorization:** WM only

```python
@tool
def commit_fact(
    fact_type: str,
    # Type of fact being declared.
    # "faction_exists", "location_exists", "entity_exists",
    # "historical_event", "relationship_state", (world-defined)

    payload: dict,
    # The declared fact. Type-specific content.
    # faction_exists: { "name": str, "type": str, "region": str, ... }
    # location_exists: { "name": str, "type": str, "feature_id": str, ... }
    # historical_event: { "description": str, "tick": int, "location": str }

    stub_lock: bool = True,
    # If true: places reservation lock on this fact.
    # Queries touching locked facts return "information unavailable"
    # until build-out tasks complete.

    auto_schedule_tasks: bool = True,
    # If true: system automatically schedules appropriate build-out
    # tasks based on fact_type (create_faction, place_feature, etc.)
    # with deadlines set to current_tick + estimated_query_window.

    world_time_deadline: int = 0,
    # Latest tick by which build-out must complete.
    # 0 = system estimates based on narrative proximity.

    narrative_context: str = "",
    # Why this fact was declared. Stored for WM reference.
) -> dict:
    # Returns:
    # {
    #   "fact_id": str,
    #   "stub_locked": bool,
    #   "tasks_scheduled": list[str],   # task_ids created
    #   "estimated_completion_tick": int
    # }
```

---

### `schedule_task`

Adds a world-building task to the async task queue. Tasks execute as simulation time advances.

**Authorization:** WM and GM

```python
@tool
def schedule_task(
    task_type: str,
    # "create_faction", "place_feature", "place_entity",
    # "generate_history", "establish_territory",
    # "assign_relationships", "populate_settlement", (world-defined)

    parameters: dict,
    # Task-specific parameters. Same schema as the corresponding
    # direct tool call.

    world_time_deadline: int,
    # Tick by which this task must complete.
    # If deadline passes without completion: WM notified.

    dependencies: list[str] = [],
    # task_ids that must complete before this task runs.

    priority: str = "normal",
    # "low" | "normal" | "high" | "immediate"
    # immediate: runs before next simulation tick advances.

    fact_id: str = "",
    # If this task is part of a commit_fact build-out:
    # links task to the stub lock. Lock released when all
    # linked tasks complete.
) -> dict:
    # Returns:
    # {
    #   "task_id": str,
    #   "scheduled_tick": int,
    #   "deadline_tick": int,
    #   "status": "queued"
    # }
```

---

### `cancel_task`

Removes a pending task from the queue. Has no effect if the task has already started.

**Authorization:** WM only

```python
@tool
def cancel_task(
    task_id: str,
    reason: str = "",
) -> dict:
    # Returns:
    # { "task_id": str, "cancelled": bool, "reason": str }
```

---

## Query Tools

All query tools are read-only. Available to WM, GM, and Scene Manager. Results are filtered and formatted — raw cell arrays and H3 IDs are never returned.

---

### `get_region`

Returns geographic features, aggregate simulation state, and social context for a spatial region.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_region(
    # ── Center (provide one) ─────────────────────────────────────────────────
    center_feature: str = "",
    # feature_id or name — uses feature centroid as center

    center_location: str = "",
    # location_id or name

    center_absolute: dict = {},
    # { "lat": float, "lon": float }

    center_entity: str = "",
    # entity_id — uses current entity position

    radius: float = 0.0,
    # Query radius in world units. 0 = auto-select based on center type.

    # ── Filters ──────────────────────────────────────────────────────────────
    include_features: bool = True,
    include_climate: bool = True,
    include_resources: bool = False,
    include_faction_control: bool = True,
    include_settlements: bool = True,
    include_hazards: bool = True,
    feature_types: list[str] = [],
    # If non-empty: return only features of these types.
) -> dict:
    # Returns:
    # {
    #   "center": { "lat": float, "lon": float },
    #   "radius": float,
    #   "features": [ { "feature_id", "name", "type", "properties",
    #                   "distance_from_center" } ],
    #   "climate": { "temperature", "precipitation", "climate_class",
    #                "hazard_level" },
    #   "resources": { resource_id: flux_value },   # if requested
    #   "faction_control": [ { "faction_id", "name", "strength" } ],
    #   "settlements": [ { "location_id", "name", "population" } ],
    #   "social_context": { "dominant_norms": dict, "trust_baseline": float }
    # }
```

---

### `get_events`

Queries the 4D R*-tree event log. Returns causally attributed events for a spatial-temporal region.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_events(
    # ── Spatial center (provide one) ─────────────────────────────────────────
    center_feature: str = "",
    center_location: str = "",
    center_absolute: dict = {},
    center_entity: str = "",

    radius: float = 0.0,
    z_min: float = 0.0,
    z_max: float = 0.0,
    # Vertical range. Both 0 = surface level only.

    # ── Temporal range ───────────────────────────────────────────────────────
    t_start: int = 0,
    t_end: int = 0,
    # Both required. Use current_tick for t_end to query up to now.

    # ── Filters ──────────────────────────────────────────────────────────────
    filter_entity: str = "",
    # Return only events from this entity.

    filter_effect_types: list[str] = [],
    # Return only events of these types.
    # "resource_extraction", "population_change", "infrastructure_damage",
    # "resource_transfer", "state_change", "spawn", "dissolve",
    # "special_resource_flux_change", "social_norm_shift"

    filter_severity_min: float = 0.0,
    # Return only events where abs(delta_magnitude) >= this value.

    filter_narrative_flagged: bool = False,
    # If true: return only events flagged for narrative attention.

    include_causal_chain: bool = False,
    # If true: for each event, include its causal_parent chain to root.
    # Expensive for large result sets.

    max_results: int = 50,
) -> dict:
    # Returns:
    # {
    #   "events": [
    #     {
    #       "event_id": str,
    #       "entity_id": str | None,
    #       "entity_name": str | None,
    #       "source_id": str,            # rule_id or aura_id
    #       "effect_type": str,
    #       "delta_magnitude": float,
    #       "delta_field": str,
    #       "location": { "lat", "lon", "alt" },
    #       "tick": int,
    #       "narrative_flag": bool,
    #       "causal_chain": list | None  # if requested
    #     }
    #   ],
    #   "total_matching": int,
    #   "truncated": bool
    # }
```

---

### `get_entity_state`

Returns current simulation state for a named entity. Blocked if the entity has an active stub lock pending contradiction resolution.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_entity_state(
    entity_id: str,
    # UUID or display_name.

    fields: list[str] = [],
    # Subset of fields to return. Empty = all fields.
    # Available: "location", "needs", "behavior_mode", "active_goal",
    #            "scale", "faction", "relationships",
    #            "continuity_snapshot", "recent_events", "hfsm_state"
) -> dict:
    # Returns:
    # {
    #   "entity_id": str,
    #   "display_name": str,
    #   "tier": int,
    #   "location": { "lat", "lon", "alt",
    #                 "containing_features": list[str],
    #                 "containing_locations": list[str] },
    #   "needs": { need_id: float },
    #   "behavior_mode": str,
    #   "active_goal": str | None,
    #   "scale": float,
    #   "faction_id": str | None,
    #   "continuity_snapshot": dict | None,
    #   "stub_locked": bool,
    #   "summary": str    # formatted by query_summary_template
    # }
```

---

### `get_faction_state`

Returns Layer 2.5 aggregate state for a faction or institution.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_faction_state(
    faction_id: str,

    fields: list[str] = [],
    # Empty = all fields.
    # Available: "territory", "strength", "norms", "relationships",
    #            "institutional_health", "ideology", "membership_estimate"
) -> dict:
    # Returns:
    # {
    #   "faction_id": str,
    #   "name": str,
    #   "type": str,
    #   "strength": float,
    #   "territory": list[str],          # feature_ids
    #   "norms": dict,
    #   "relationships": dict,           # faction_id → trust float
    #   "institutional_health": float,   # coalition stability 0.0–1.0
    #   "ideology": dict,
    #   "membership_estimate": int | None
    # }
```

---

### `get_social_context`

Returns norm vectors and trust baselines for a social stratum in a region. Used by the GM to understand behavioral context before generating NPC behavior or scene descriptions.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_social_context(
    location: str = "",
    # location_id, feature_id, or name.

    location_absolute: dict = {},
    # { "lat": float, "lon": float } if location not known by name.

    stratum: str = "general",
    # Social stratum to query.
    # "general" | domain-defined strata (e.g. "peasant", "merchant",
    # "noble", "military", "clergy", "outcast")
) -> dict:
    # Returns:
    # {
    #   "region_name": str,
    #   "stratum": str,
    #   "dominant_factions": list[str],
    #   "norms": {
    #     norm_id: { "value": float, "description": str }
    #   },
    #   "trust_baseline": float,          # 0.0–1.0 stranger trust
    #   "authority_structure": str,       # "feudal" | "flat" | etc.
    #   "social_mobility": float,         # 0.0–1.0
    #   "notable_tensions": list[str]     # active faction conflicts nearby
    # }
```

---

### `get_feature`

Returns a feature record from the PostGIS store.

**Authorization:** WM, GM, Scene Manager

```python
@tool
def get_feature(
    feature_id: str = "",
    # UUID or name.

    fields: list[str] = [],
    # Empty = all fields.
    # Available: "geometry", "properties", "layer_effects",
    #            "parent_feature", "children", "adjacent",
    #            "cells_intersected", "created_tick", "dissolved_tick"
) -> dict:
    # Returns:
    # {
    #   "feature_id": str,
    #   "name": str,
    #   "type": str,
    #   "geometry_type": str,
    #   "center": { "lat": float, "lon": float },
    #   "properties": dict,
    #   "layer_effects": dict,
    #   "parent_feature_id": str | None,
    #   "children_feature_ids": list[str],
    #   "adjacent_feature_ids": list[str],
    #   "cells_intersected": int,
    #   "active": bool
    # }
```

---

### `get_world_debt`

Returns the current state of the WM async task queue — how much declared-but-unbuilt world infrastructure exists.

**Authorization:** WM only

```python
@tool
def get_world_debt() -> dict:
    # Returns:
    # {
    #   "total_pending_tasks": int,
    #   "overdue_tasks": int,          # past deadline_tick
    #   "stub_locked_facts": int,      # facts blocking queries
    #   "tasks_by_priority": {
    #     "immediate": int,
    #     "high": int,
    #     "normal": int,
    #     "low": int
    #   },
    #   "tasks_by_type": dict,         # task_type → count
    #   "next_deadline_tick": int | None,
    #   "oldest_overdue": dict | None  # { task_id, task_type, deadline_tick }
    # }
```

---

## Summary

| Function | Auth | Domain | Touches |
|---|---|---|---|
| `set_world_orientation` | WM | Initialization | H3 grid, climate params |
| `place_continent` | WM | Geography | PostGIS, cell vectors |
| `place_feature` | WM / GM | Geography | PostGIS, cell vectors, event log |
| `update_feature` | WM | Geography | PostGIS, cell vectors, event log |
| `dissolve_feature` | WM | Geography | PostGIS, cell vectors, event log |
| `name_feature` | WM / GM | Geography | PostGIS |
| `place_entity` | WM / GM | Entity | EPL, coordinate registry |
| `trigger_geological_event` | WM | Geological | Cell vectors, PostGIS, event log |
| `create_faction` | WM / GM | Social | Layer 2.5 state |
| `update_faction` | WM / GM | Social | Layer 2.5 state, event log |
| `dissolve_faction` | WM | Social | Layer 2.5 state, event log |
| `set_world_narrative_state` | WM | Narrative | WM context, Layer 2.5 norms |
| `create_event_rule` | WM | Event system | Production rule engine |
| `update_event_rule` | WM | Event system | Production rule engine |
| `delete_event_rule` | WM | Event system | Production rule engine |
| `commit_fact` | WM | World debt | Stub lock registry, task queue |
| `schedule_task` | WM / GM | World debt | Task queue |
| `cancel_task` | WM | World debt | Task queue |
| `get_region` | WM / GM / SM | Query | PostGIS, Layer 2-2.5 |
| `get_events` | WM / GM / SM | Query | R*-tree event log |
| `get_entity_state` | WM / GM / SM | Query | EPL |
| `get_faction_state` | WM / GM / SM | Query | Layer 2.5 |
| `get_social_context` | WM / GM / SM | Query | Layer 2.5 |
| `get_feature` | WM / GM / SM | Query | PostGIS |
| `get_world_debt` | WM | Query | Task queue |

**Total: 25 functions.** 7 WM-only. 6 WM and GM. 5 WM, GM, and Scene Manager (queries). 7 query-only (read-only, no side effects on simulation state).




Extra tools arised while building simulation

Для добавления фэнтези-руды:
register_ore_type('mithril', OreFormation(
    'Mithril', 'mithril', ['silver'], ['granite', 'gneiss'],
    formation_type='magmatic', rarity=0.0005,
    depth_range=(1000, 4000), ...
))
