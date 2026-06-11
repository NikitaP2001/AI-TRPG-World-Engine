# Entity Definition Schema

*Input/output contract for the Layer 3 behavior tick engine*

---

## Overview

An entity definition is the complete specification registered by the GM or WM when introducing a named entity into the simulation. It compiles to a rule set that the behavior tick engine executes each simulation tick. There is no training step and no inference at runtime — only compiled rule evaluation against live world-state variables.

The schema feeds two systems:

- **Layer 3 behavior tick engine** — executes movement, need decay, aura effects, and action rules each tick; writes world-state mutations and R*-tree event records
- **Query API** — shapes what `get_entity_state()` returns about this entity and how the GM notification queue handles its events

The schema has six sections:

1. [Identity](#1-identity)
2. [Need Vector](#2-need-vector)
3. [Behavior Mode](#3-behavior-mode)
4. [Passive Aura Effects](#4-passive-aura-effects)
5. [Action Rules](#5-action-rules)
6. [Narrative Persistence Flags](#6-narrative-persistence-flags)

> **Authoring note.** The GM and WM fill in this schema using natural language descriptions. The LLM compiler translates those descriptions into the typed formal representation shown here before handing off to the tick engine. The GM never writes raw condition expressions directly — they describe intent, the compiler produces the formal output, and any case the compiler cannot resolve cleanly escalates back to the GM for clarification.

---

## 1. Identity

| Field | Type | Description |
|---|---|---|
| `entity_id` | `string (UUID)` | System-assigned on registration. Stable across the simulation lifetime. |
| `display_name` | `string` | Name used in event logs, GM queries, and narrative references. |
| `archetype_id` | `string` | Identifies the behavior profile this entity is based on. Used for rule set lookup and policy registry reuse across similar entities. |
| `tier` | `enum: 1 \| 2 \| 3` | Determines behavior mode transition model. 1 = need-driven, 2 = goal-driven, 3 = state-machine (HFSM). |
| `existence_type` | `string (domain-defined)` | Controls which need vector template applies and which termination model runs. Defined per world — examples: `mortal`, `construct`, `energy_being`, `distributed_system`. |
| `scale` | `enum: individual \| party \| unit \| swarm` | Determines whether behavior ticks execute per-entity or per-cluster, and how action rule magnitudes scale. |
| `faction_id` | `string \| null` | Optional affiliation. Affects norm vector selection from Layer 2.5 and trust matrix lookups in condition expressions. |
| `narrative_importance` | `enum: background \| notable \| named \| critical` | Controls continuity record depth and contradiction-check strictness. Only `named` and `critical` entities receive full continuity tracking. |

---

## 2. Need Vector

The need vector defines what this entity consumes per tick and what happens when needs are depleted. Each need has a current value, a decay rate, and a depletion consequence.

The need vector is fully domain-defined. The engine imposes no assumptions about what needs exist — a biological entity might have `food` and `water`; a mechanical entity might have `fuel` and `structural_integrity`; a distributed computational system might have `processing_capacity` and `network_coherence`. The schema is identical in all cases.

### 2A — Need Entry Structure

| Field | Type | Description |
|---|---|---|
| `need_id` | `string` | Identifier for this need. Domain-defined: `food`, `water`, `fuel`, `morale`, `cohesion`, `energy_reserve`, `signal_integrity`, etc. |
| `current_value` | `float 0.0–1.0` | Normalized. 1.0 = fully satisfied. 0.0 = critically depleted. |
| `decay_rate` | `float per tick` | Depletion rate per tick under baseline conditions. `0.0` = does not decay autonomously (modified only by events). |
| `decay_modifiers` | `list[condition_expression]` | Conditions that multiply the base decay rate. Evaluated each tick. Example: `entity.behavior_mode == PATH_TO_GOAL → decay_rate * 1.5`. |
| `depletion_threshold` | `float` | Value below which the entity enters distress behavior. Default `0.2`. |
| `depletion_consequence` | `enum: seek_resource \| distress_action \| termination \| cascade` | What fires at depletion. `cascade` triggers another named need's depletion immediately. `termination` dissolves the entity. |
| `replenishment_source` | `enum: L0 \| L1 \| L2 \| self_carried \| raid` | Where this need is replenished from. `self_carried` = entity carries its own supply stock. `raid` = entity takes from world-state fields via action rules. |

### 2B — Existence Type Templates

The existence type determines which need template is applied at registration. Templates are world-defined — the engine ships with no built-in existence types. The WM registers templates at world initialization.

A template specifies:
- Which needs are present
- Default decay rates per need
- Which needs trigger termination at depletion
- Whether termination is need-driven (depletion) or event-driven (a specific event condition)

**Event-driven termination** is used for entities whose existence depends on an external condition rather than biological survival. The termination condition is specified as an event expression rather than a need depletion threshold. The need vector may still exist for other behavioral purposes — the termination logic is simply decoupled from it.

**Scale-specific templates** exist for group entities. A `unit` scale entity has a bulk supply stock (e.g. `food_stockpile` rather than `food`) whose decay rate is proportional to `entity.scale`. Attrition rules reduce `entity.scale` rather than triggering termination directly — the unit dissolves only when scale falls below a minimum threshold.

---

## 3. Behavior Mode

Behavior mode controls how the entity moves between ticks. It is deliberately separated from action rules, which control what the entity does at each position. The same action rules apply regardless of behavior mode — a entity that forages when hungry does so whether it is patrolling, traveling toward a goal, or has just woken from dormancy.

Mode can change at runtime via HFSM state transitions (Tier 3) or goal completion and reassignment (Tier 2).

| Mode | Parameters | Behavior |
|---|---|---|
| `STATIONARY` | `anchor_location`, `drift_radius` (optional) | Entity remains at anchor location. May drift within radius. No pathfinding cost. Tick executes at fixed location each tick. |
| `PATROL` | `waypoints: list[location]`, `loop: bool`, `speed_modifier` | Entity follows waypoint sequence, looping or terminating at end. Tick executes at each waypoint and at configurable intervals between them. |
| `PATH_TO_GOAL` | `goal_location`, `path_algorithm: astar \| waterway \| road_preference`, `replan_on_block: bool` | Entity paths toward goal location. Replans if blocked by event or terrain change. Mode terminates on arrival. |
| `GOAL_SEEKING` | `goal_condition: expression`, `search_radius`, `approach_mode` | Entity has no fixed destination. Searches within radius for condition to become true, then paths toward it. Most computationally expensive mode — limit concurrent active instances. |
| `FOLLOWING` | `target_entity_id`, `follow_distance`, `break_condition: expression` | Entity maintains proximity to target. Breaks on condition evaluation. |
| `DORMANT` | `wake_conditions: list[event_or_expression]`, `dormancy_cost_modifier` | Entity does not move. All need decays multiplied by `dormancy_cost_modifier` (typically < 1.0). Wakes immediately when any wake condition fires. |

> **Performance note.** `GOAL_SEEKING` performs condition reads against live Layer 0–2.5 state every tick until the goal condition is satisfied. For large entity populations, limit the number of entities in `GOAL_SEEKING` simultaneously or apply tick-rate reduction for entities in this mode.

---

## 4. Passive Aura Effects

Auras are always-on per-tick effects that radiate from the entity's current location. They require no trigger condition — they execute every tick the entity exists and is not in a suppressing state. Auras write continuous world-state mutations that accumulate over time and are indexed in the 4D R*-tree event log as sustained-interval records.

Auras are how entities exert ambient influence on their environment: a productive presence that increases local output, a harmful presence that degrades surrounding conditions, a suppressive presence that inhibits certain field values. The effect is directional (it modifies a specific world-state field) but not discrete — it flows continuously rather than firing as an event.

### 4A — Aura Entry Structure

| Field | Type | Description |
|---|---|---|
| `aura_id` | `string` | Identifier for this aura on this entity. Used in R*-tree records for attribution. |
| `radius` | `float (world units)` | Effect radius from entity current location. `0` = self or immediate cell only. |
| `z_range` | `[z_min_offset, z_max_offset]` | Vertical band of effect relative to entity's z position. Defaults to `[0, 0]` (same layer only). Set explicitly for effects that should not propagate vertically across strata. |
| `falloff` | `enum: flat \| linear \| inverse_square` | How effect magnitude scales with distance from entity center. |
| `target_layer` | `enum: L0 \| L1 \| L2 \| L2_5 \| L3_entities` | Which simulation layer this aura modifies. |
| `target_field` | `string (field path)` | The specific field in the target layer. Uses dot notation: `L2.settlement.resource_stocks[tools]`, `L1.current_population[species_id]`, `L2_5.norm_vectors[violence_tolerance]`. |
| `effect_type` | `enum: modify_rate \| modify_value \| suppress_field \| spawn_entity` | Operation applied to the target field each tick. |
| `effect_magnitude` | `float \| expression` | Amount applied per tick. Can reference entity state: `0.01 * entity.need[energy_reserve]`. Negative values are valid for depletion effects. |
| `condition` | `expression \| null` | Optional activation condition. Aura is only active when this evaluates true. Example: `entity.behavior_mode == STATIONARY`. |
| `event_log_frequency` | `enum: every_tick \| on_threshold \| never` | Controls R*-tree insertion frequency. `on_threshold` = log only when target field crosses a defined boundary value. Use `never` for high-frequency low-significance auras to limit tree growth. |

### 4B — Aura Record in Event Log

Active auras produce a single R*-tree record per activation period, not one record per tick. The record's temporal bounding box spans `[t_activation, t_deactivation]`. The tick engine maintains an active aura map (`entity_id + aura_id → record_id`) and extends `t_end` in-place each tick rather than inserting new records. This keeps record volume proportional to the number of aura activation periods, not to simulation duration.

---

## 5. Action Rules

Action rules are the conditional behavior of the entity — `IF condition THEN world_state_delta_list`. They fire during behavior ticks when conditions are met, produce discrete world-state mutations, and insert point-in-time records into the 4D R*-tree event log with full causal attribution.

Action rules are what produce named causal events: a supply raid, a territory claim, an infrastructure failure, a population displacement. These events are queryable, attributable to a specific entity and rule, and chainable via the `causal_parent` field.

### 5A — Action Rule Entry Structure

| Field | Type | Description |
|---|---|---|
| `rule_id` | `string` | Identifier. Used in R*-tree event attribution and debugging. Must be unique within the entity definition. |
| `priority` | `int 1–10` | Evaluation order. Higher priority rules evaluate first. If a higher-priority rule fires and `multi_fire` is false, lower-priority rules are skipped this tick. |
| `cooldown_ticks` | `int` | Minimum ticks between firings of this rule. Prevents event storms from high-frequency conditions. |
| `condition` | `expression` | Boolean expression over entity state and local world-state variables. See [5B — Condition Reference](#5b--condition-reference). |
| `effects` | `list[world_delta]` | Ordered list of world-state mutations applied when rule fires. See [5C — Delta Types](#5c--delta-types). |
| `effect_shape` | `enum: sphere \| cylinder \| cone \| contact` | 3D spatial footprint for the weighted spatial join. Determines which layer cells receive the effect and in what proportions. Default: `contact` (entity's current cell only). |
| `multi_fire` | `bool` | If true, this rule can fire in the same tick as other rules regardless of priority. Default `false`. |
| `event_log_entry` | `string template` | Message written to R*-tree record. Available variables: `{entity.name}`, `{entity.id}`, `{location.name}`, `{delta.field}`, `{delta.amount}`, `{tick}`. |
| `narrative_flag` | `bool \| expression` | If true or expression evaluates true, event is added to GM notification queue on next query. Use expressions for conditional flagging: `entity.scale < 0.5 * entity.initial_scale`. |
| `causal_parent_trigger` | `event_type \| null` | If this rule fires in response to a specific event type, the tick engine looks up that event's ID and sets it as `causal_parent` in the R*-tree record automatically. Enables causal chain traversal. |

### 5B — Condition Reference

Conditions are boolean expressions evaluated against the following variable namespaces:

| Namespace | Refers To | Example |
|---|---|---|
| `entity.need[id]` | Current normalized value (0.0–1.0) of a need in this entity's vector | `entity.need[fuel] < 0.3` |
| `entity.behavior_mode` | Current behavior mode enum value | `entity.behavior_mode == DORMANT` |
| `entity.scale` | Current scale value (individual = 1.0; groups = headcount or mass) | `entity.scale < 100` |
| `entity.hfsm_state` | Current named HFSM state (Tier 3 only) | `entity.hfsm_state == ACTIVE` |
| `entity.location.type` | Terrain or settlement type at current cell | `entity.location.type == settlement` |
| `entity.location.cell[field]` | Any Layer 0 field at current cell | `entity.location.cell[special_resource_flux] > 0.6` |
| `entity.location.distance_to(target)` | Distance in world units to a named location or entity | `entity.location.distance_to(home_base) < 3` |
| `local.L1[field]` | Layer 1 field at current cell | `local.L1[current_population[prey_species]] > 5` |
| `local.L2[field]` | Layer 2 field at nearest settlement within detection radius | `local.L2[settlement.food_stores] > 0` |
| `local.L2_5[field]` | Layer 2.5 field for current region | `local.L2_5[norm_vectors.violence_tolerance] > 0.4` |
| `local.entities[filter]` | Count of entities matching filter within detection radius | `local.entities[faction==hostile, tier<=2] > 0` |
| `world.time_since[event_type]` | Ticks elapsed since last event of this type affected this entity | `world.time_since[entity.last_resupply] > 30` |
| `world.tick` | Current absolute simulation tick | `world.tick % 10 == 0` |

Conditions support standard boolean operators (`AND`, `OR`, `NOT`) and comparison operators (`<`, `>`, `<=`, `>=`, `==`, `!=`). Numeric expressions support basic arithmetic.

### 5C — Delta Types

| Delta Type | Parameters | Description |
|---|---|---|
| `modify_field` | `layer`, `field_path`, `operation: add \| multiply \| set`, `value` | Modifies a numeric field in the target simulation layer. Primary mutation type. Value is distributed across affected cells via weighted spatial join using `effect_shape`. |
| `transfer_resource` | `from: layer+field_path`, `to: layer+field_path`, `amount`, `transfer_type: consume \| raid \| trade` | Moves a resource quantity between two world-state fields atomically. Amount deducted from source is exactly the amount added to destination. |
| `spawn_entity` | `archetype_id`, `location`, `behavior_mode`, `need_vector_overrides`, `scale` | Creates a new entity instance registered in the EPL. The new entity is a child of this entity for provenance purposes — its resource consumption deducts from the same aggregate pool. |
| `dissolve_entity` | `entity_id`, `cause` | Removes an entity from simulation. Releases all resource claims back to parent aggregate pool. Writes termination record to R*-tree. |
| `modify_entity_state` | `target_entity_id`, `field`, `operation`, `value` | Modifies a field on another entity. Used for relationship changes, damage in entity-to-entity interactions, need transfers between entities. |
| `set_behavior_mode` | `new_mode`, `mode_parameters` | Changes this entity's behavior mode. Primary mechanism for HFSM state transitions in Tier 3 entities. |
| `trigger_event` | `event_type`, `location`, `payload` | Fires a named event into the event system. May trigger event system production rules. Used for high-consequence actions with cascading world-level effects. |
| `notify_wm` | `message_template`, `priority: low \| normal \| high \| immediate` | Sends a notification to the World Master task queue. Use when the entity action has narrative consequences that require WM-level world-building response. |

### 5D — Authoring Example: Natural Language to Compiled Rule

The GM describes intent in natural language. The LLM compiler produces the formal rule.

**GM input:**

> A supply unit running low on provisions will requisition food from settlements it passes through, but only from settlements that belong to the same faction or that are too weak to resist. Requisitioning should reduce the settlement's food stocks and slightly reduce local morale. Flag it for GM attention if the settlement is left critically short.

**Compiled output:**

```
rule_id: requisition_food_from_settlement
priority: 7
cooldown_ticks: 8
condition:
  entity.need[food_stockpile] < 0.45
  AND entity.location.type == settlement
  AND local.L2[settlement.food_stores] > 0
  AND (
    local.L2_5[faction_control] == entity.faction_id
    OR local.L2[settlement.defense_rating] < entity.scale * 0.3
  )
effects:
  - transfer_resource:
      from: L2.settlement.food_stores
      to: entity.need[food_stockpile]
      amount: min(0.25 * entity.scale_multiplier, L2.settlement.food_stores * 0.4)
      transfer_type: raid
  - modify_field:
      layer: L2, field: settlement.morale
      operation: add, value: -0.08
event_log_entry: "{entity.name} requisitioned supplies from {location.name}.
  Transferred {delta.amount} food units. Settlement stores now at
  {L2.settlement.food_stores:.0%} capacity."
narrative_flag: local.L2[settlement.food_stores] < 0.15
causal_parent_trigger: null
```

> The compiler is the boundary between GM expressiveness and system formalism. Cases the compiler cannot resolve unambiguously should escalate to a clarification request rather than produce a best-guess rule. An incorrect rule that silently produces wrong behavior is worse than a compilation failure.

---

## 6. Narrative Persistence Flags

These fields activate when an entity reaches `notable`, `named`, or `critical` narrative importance. They control what the continuity tracking system records and how contradictions between simulation history and established narrative facts are handled.

| Field | Type | Description |
|---|---|---|
| `continuity_depth` | `enum: none \| shallow \| deep` | `none`: no continuity tracking. `shallow`: records location, behavior_mode, active_goal, companion_ids at each observation. `deep`: all shallow fields plus need state, emotional_proxy value, recent significant events, relationship deltas. |
| `observation_trigger` | `enum: player_scene \| gm_narration \| any_query` | What constitutes an observation that updates the continuity snapshot. `any_query` is appropriate for critical entities; `player_scene` for background notable entities. |
| `contradiction_check_fields` | `list[field_path]` | Fields checked for contradiction between the continuity snapshot and simulation delta since last observation. Defaults: `location_plausibility`, `companion_presence`, `active_goal_consistency`. |
| `stub_lock_on_conflict` | `bool` | If true, entity is locked when contradiction check fails. Queries against a locked entity return `unavailable` until WM resolution completes. Set true for named and critical entities. |
| `wm_notify_on_conflict` | `bool` | Whether to push contradiction to WM notification queue immediately or batch with next cycle. Immediate for critical entities. |
| `query_summary_template` | `string template` | Format of the summary returned by `get_entity_state()` for this entity. Available variables: all entity state fields. This is what the GM reads. |

---

## Appendix A — Tick Execution Sequence

Each simulation tick, for every active entity, the behavior tick engine executes the following sequence atomically:

1. **Position update.** Advance entity along current behavior mode path. Replan if blocked.
2. **Need decay.** Apply base decay rates to all needs. Evaluate and apply decay modifiers.
3. **Aura application.** For each active aura: compute affected cells via weighted spatial join, apply magnitude * weight to each cell's target field, extend aura R*-tree record's `t_end`.
4. **Action rule evaluation.** Evaluate rules in descending priority order. For each rule whose condition is true and cooldown has elapsed: apply effects via spatial join, insert R*-tree event record, set narrative flag if configured.
5. **Termination check.** Evaluate termination conditions (need depletion consequences and event-driven conditions). If triggered: dissolve entity, release resource claims, write termination record.

Steps 3–4 (aura application and action rule effects) each execute the weighted spatial join independently. The spatial join computes intersection weights between the effect footprint and overlapping Layer 0–2.5 cells, distributes `delta_magnitude * weight` to each cell, and inserts one R*-tree record covering the full effect bounding box. The layer state and the event log are always updated together.

---

## Appendix B — Schema Versioning

The schema version is tracked as a world-level metadata field. Changes to field definitions, new delta types, or changes to the condition expression grammar require a schema migration for all existing entity records. Additive changes (new optional fields, new enum values) are backward compatible. Structural changes (field renames, type changes, removal of fields) are breaking and require a migration script.

Entity records store their schema version at registration time. The tick engine validates schema version on load and rejects records that are incompatible with the current engine version.