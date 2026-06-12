# L3 Specification

*Entity Behavior Tick Engine — Individual and Group Actors*

---

## Scope

L3 is the layer of named, individually-tracked actors: NPCs, monsters, military units,
refugee groups, dormant guardians, legendary entities, and any other entity registered
via `define_entity`. L3 entities are the only actors in the simulation with behavior,
need vectors, and action rules — L0 is geography, L1 is population-level ecology,
L2/L2.5 are civilization aggregates. An L3 entity is a discrete thing with a position,
a state, and rules that fire.

This specification consolidates the base tick engine contract (identity, need vector,
behavior mode, auras, action rules, narrative persistence — carried over from the
existing entity definition schema without change) with the extensions required by the
WM tool set: capability use, inventory and items, immunity and pre-engagement combat
effects, authority and leadership profiles, myth seeding, and location discovery.

---

## Part I — Base Contract

### §1 Identity

| Field | Type | Description |
|---|---|---|
| `entity_id` | `string` | Stable identifier, system-assigned or WM-specified. |
| `display_name` | `string` | Name used in event logs, queries, narrative references. |
| `archetype_id` | `string` | Behavior profile identifier; entities sharing an archetype share a rule set for EPL management. |
| `tier` | `1 \| 2 \| 3` | 1 = need-driven. 2 = goal-driven. 3 = state-machine (HFSM). |
| `existence_type` | `string` | Registered `existence_type` concept_id. Determines need vector template and termination model. |
| `scale` | `individual \| party \| unit \| swarm` | Determines per-tick vs per-cluster execution and effect magnitude scaling. |
| `scale_count` | `float` | For `unit`/`swarm`: constituent member count. Referenced by rules as `entity.scale`. |
| `faction_id` | `string \| ""` | Affiliation. Affects L2.5 norm and trust matrix lookups in conditions. |
| `narrative_importance` | `background \| notable \| named \| critical` | Controls continuity tracking depth and contradiction-check strictness. |

### §2 Need Vector

Need vector entries:

| Field | Type | Description |
|---|---|---|
| `need_id` | `string` | Domain-defined identifier. |
| `current_value` | `float 0.0–1.0` | Normalized. |
| `decay_rate` | `float` | Per-tick depletion under baseline conditions. |
| `decay_modifiers` | `list[expression]` | Conditions that multiply base decay rate. |
| `depletion_threshold` | `float` | Distress trigger point; default `0.2`. |
| `depletion_consequence` | `seek_resource \| distress_action \| termination \| cascade:need_id \| none` | Effect at depletion. |
| `replenishment_source` | `L0 \| L1 \| L2 \| self_carried \| raid \| none` | Where this need refills from. |

`existence_type` supplies the default need template at registration. `fauna_species`
registrations also carry `base_birth`/`base_death` and habitat suitability — these
seed L1 population fields directly (see L2/L2.5 §1.1) and are distinct from the
per-individual need vector applied to L3 instances spawned from that population
(e.g. a named individual wolf that becomes narratively significant).

### §3 Behavior Mode

| Mode | Parameters | Behavior |
|---|---|---|
| `STATIONARY` | `anchor_location`, `drift_radius` | Remains at anchor, optional drift. |
| `PATROL` | `waypoints`, `loop`, `speed_modifier` | Follows waypoint sequence. |
| `PATH_TO_GOAL` | `goal_location`, `path_algorithm`, `replan_on_block` | Paths to destination, terminates on arrival. |
| `GOAL_SEEKING` | `goal_condition`, `search_radius`, `approach_mode` | Searches for condition, then paths toward it. Most expensive — limit concurrent instances. |
| `FOLLOWING` | `target_entity_id`, `follow_distance`, `break_condition` | Maintains proximity to target. |
| `DORMANT` | `wake_conditions`, `dormancy_cost_modifier` | No movement; need decay scaled by modifier; wakes on any condition firing. |

Mode transitions occur via HFSM state transitions (Tier 3) or goal completion (Tier 2).
Action rules are independent of behavior mode — an entity forages whether patrolling,
traveling, or dormant, subject to the same rule conditions.

### §4 Passive Aura Effects

| Field | Type | Description |
|---|---|---|
| `aura_id` | `string` | Identifier for event log attribution. |
| `radius` | `float` | Effect radius; `0` = current cell only. |
| `z_range` | `[float, float]` | Vertical band relative to entity z; default `[0,0]`. |
| `falloff` | `flat \| linear \| inverse_square` | Magnitude scaling with distance. |
| `target_layer` | `L0 \| L1 \| L2 \| L2_5 \| L3_entities` | Layer the aura modifies. |
| `target_field` | `string (dot path)` | Field modified, e.g. `L2.settlement.resource_stocks[tools]`. |
| `effect_type` | `modify_rate \| modify_value \| suppress_field \| spawn_entity` | Operation per tick. |
| `effect_magnitude` | `float \| expression` | Per-tick amount; may reference entity state. |
| `condition` | `expression \| null` | Optional activation gate. |
| `event_log_frequency` | `every_tick \| on_threshold \| never` | Event log insertion frequency. |

Active auras produce one event log record per activation period (`t_activation` to
`t_deactivation`), extended in place each tick rather than re-inserted.

### §5 Action Rules

| Field | Type | Description |
|---|---|---|
| `rule_id` | `string` | Unique within entity. |
| `priority` | `int 1–10` | Higher evaluates first. |
| `cooldown_ticks` | `int` | Minimum ticks between firings. |
| `condition` | `expression` | Boolean expression over entity and world state (§5B). |
| `effects` | `list[world_delta]` | Mutations applied on firing (§5C). |
| `effect_shape` | `sphere \| cylinder \| cone \| contact` | Spatial footprint for weighted spatial join. |
| `multi_fire` | `bool` | If true, can fire alongside other rules this tick. |
| `event_log_entry` | `string template` | Message written to event log. |
| `narrative_flag` | `bool \| expression` | If true, added to GM notification queue. |
| `myth_seeds` | `list[myth_seed]` | Myths planted at firing location (see Part III §13). |
| `causal_parent_trigger` | `event_type \| null` | Auto-links causal chain to a triggering event type. |

**§5B Condition namespaces** (unchanged from base schema):

`entity.need[id]`, `entity.behavior_mode`, `entity.scale`, `entity.hfsm_state`,
`entity.location.type`, `entity.location.cell[field]`, `entity.location.distance_to(target)`,
`local.L1[field]`, `local.L2[field]`, `local.L2_5[field]`, `local.entities[filter]`,
`world.time_since[event_type]`, `world.tick`.

Extended namespaces introduced by this specification:

| Namespace | Refers To | Example |
|---|---|---|
| `entity.capability[id]` | Whether a capability is currently usable (prerequisites + cooldown + cost all satisfied) | `entity.capability[fireball] AND entity.need[energy_reserve] > 0.3` |
| `entity.inventory[item_id]` | Whether this entity's inventory contains the named item | `entity.inventory[ancient_catalyst]` |
| `entity.immunity[type]` | Whether this entity has the named immunity declared | `NOT entity.immunity[lanchester_attrition]` |
| `entity.myth_conviction[myth_id]` | This entity's personal conviction value for a myth (for entities that can discover/investigate) | `entity.myth_conviction[dragon_heart_properties] > 0.6` |

**§5C Delta types** (unchanged from base schema, plus two additions):

`modify_field`, `transfer_resource`, `spawn_entity`, `dissolve_entity`,
`modify_entity_state`, `set_behavior_mode`, `trigger_event`, `notify_wm`.

New delta types:

| Delta Type | Parameters | Description |
|---|---|---|
| `use_capability` | `capability_id`, `target` (optional) | Fires `unlock_effects`/use effects of a registered capability, deducting `use_cost` and `use_energy_cost` from this entity's stocks. Fails the rule (no effects applied) if prerequisites or cooldown are not satisfied — the condition `entity.capability[id]` should gate this. |
| `discover_location` | `feature_id` | Reveals a feature's `contains` block (items, dormant entities, myth seeds) per Part III §14. Fired by an entity's own action rule when it enters a `discovery_required` feature. |

### §6 Narrative Persistence

| Field | Type | Description |
|---|---|---|
| `continuity_depth` | `none \| shallow \| deep` | Tracking depth. |
| `observation_trigger` | `player_scene \| gm_narration \| any_query` | What counts as an observation. |
| `contradiction_check_fields` | `list[field_path]` | Fields checked for contradiction against simulation delta. |
| `stub_lock_on_conflict` | `bool` | Lock entity on contradiction until WM resolves. |
| `wm_notify_on_conflict` | `bool` | Immediate vs batched WM notification. |
| `query_summary_template` | `string template` | `get_entity_state()` output format. |

---

## Part II — Combat, Capability, and Authority Extensions

### §7 Immunities

| Field | Type | Description |
|---|---|---|
| `immunity_type` | `string` | One of: a registered `damage_type` concept_id; `"lanchester_attrition"` (immune to L2 aggregate combat damage — see L2/L2.5 §4); `"need_termination"` (cannot die from need depletion; must declare `termination_condition` instead); or `"physics_override:<id>"` (immune to a named `physics_override` feature). |
| `exception_sources` | `list[event_type \| entity_id]` | Sources that bypass this immunity, e.g. `"slain_by_named_hero"`. |

GURPS `Unkillable N` advantages populate `"need_termination"` immunity automatically
when `gurps_sheet` is provided (see Part IV §15). Explicit immunities extend the
GURPS-derived set.

### §8 Pre-Engagement Combat Effects

| Field | Type | Description |
|---|---|---|
| `effect_id` | `string` | Identifier. |
| `condition` | `expression` | When this entity's presence triggers the effect, e.g. `entity.need[energy_reserve] > 0.3`. |
| `target` | `friendly_forces \| hostile_forces \| all` | Which side of the engagement is affected. |
| `field` | `string` | L2 combat parameter modified — `effective_strength`, `morale`, `attrition_rate_modifier`. |
| `operation` | `multiply \| add \| set` | How `value` is applied. |
| `value` | `float \| expression` | Modification amount. |
| `energy_cost` | `float` | Stock quantity deducted when this effect fires. |
| `energy_source` | `string` | `stock_id` paying `energy_cost`. |

Applied in Phase 1 of L2 combat resolution (L2/L2.5 §4), before Lanchester equations
run. This is the mechanism by which a wizard, dragon, or dreadnought modifies an
engagement's outcome without participating in aggregate attrition directly.

### §9 Termination Condition

`termination_condition: string` — an event expression that fires dissolution,
overriding the `existence_type`'s default termination model. Examples:

```
"event.type == 'slain_by_named_hero'"
"event.type == 'destroyed_by_wm_declaration'"
"entity.hfsm_state == 'DESTROYED' AND entity.scale < 1"
```

Used together with `"need_termination"` immunity for entities whose existence is not
governed by their need vector — dragons, dreadnoughts, constructs bound to an artifact.

### §10 Capabilities

| Field | Type | Description |
|---|---|---|
| `capabilities_unlocked` | `list[string]` | `capability` concept_ids this entity possesses at registration. |

Entity-scoped capabilities (`scope: "entity"` or `"both"` on the `capability` concept)
require this entity's own stocks to satisfy `knowledge_prerequisites`,
`material_prerequisites`, and `use_energy_cost`. A wizard's spells are entity
capabilities; using one fires `use_capability` (§5C), consuming `use_cost` materials
and `use_energy_cost` from `entity.energy_reserve`.

Entities also gain capabilities automatically when their faction unlocks a
`scope: "both"` capability, provided the entity independently satisfies any
entity-level prerequisites (e.g. a soldier gains access to `crossbow_production`
output only if armed with the produced item — see §12).

### §11 Authority and Leadership

| Field | Type | Description |
|---|---|---|
| `leadership_profile` | `dict` | Objective weights, risk tolerance, time horizon, constraints, faction disposition modifiers. Shapes L2's autonomous objective function when this entity leads a faction without active direction (Regime B — L2/L2.5 §6). |
| `authority_overrides` | `list[{faction_id, authority_weight, domain}]` | Declares which faction(s) this entity leads and at what council weight and domain. Contributes to `council_members` (L2/L2.5 §10). |

These fields do not change L3 tick execution directly — they are read by the L2
update sequence (L2/L2.5 §12 step 5: effective profile recomputation) each tick. An
entity with `authority_overrides` does not require special L3 rules; its influence is
expressed entirely through the L2/L2.5 leadership architecture. `post_intent` (WM
tool) is the live-play mechanism for this entity's narratively-directed decisions.

---

## Part III — Items, Inventory, and Discovery

### §12 Items

An item is not an entity. It has no behavior mode, no need vector, no tick execution
of its own. It is a named object with optional passive effects, registered via
`define_world_concept` with `concept_type: "item"`.

| Field | Type | Description |
|---|---|---|
| `portable` | `bool` | Whether an entity can carry this item in `inventory`. |
| `properties` | `dict` | Open key-value; what this item satisfies as a prerequisite (e.g. `{"capability_use": "soul_fusion_ritual"}` for `required_materials`/`required_entity`-style research project gates — see L2/L2.5 §5). |
| `aura` | `aura schema (§4)` | Passive radiating effect, active whenever the item exists in the world, regardless of whether it is carried. A cursed artifact radiating corruption even while inert. |
| `combat_effects` | `pre_engagement_effects schema (§8)` | Active only when this item is in the `inventory` of an entity present in an engagement. A cursed sword's effect applies only when wielded. |
| `destructible` | `bool` | Whether consumption (via `use_cost` or `consumed_materials`) can destroy this item. |
| `unique` | `bool` | If true, only one instance of this item may exist. Creating a second while the first exists is an error unless the first has been destroyed or dissolved. |

### §13 Inventory

| Field | Type | Description |
|---|---|---|
| `inventory` | `list[item_id]` | Items carried by this entity. |

When an entity with inventory items is present in an L2 combat engagement, each
carried item's `combat_effects` apply via the same Phase 1 injection as the entity's
own `pre_engagement_effects` (§8). When an entity is dissolved, its inventory items
drop to the entity's last location by default; a `dissolve_entity` delta may specify
`destroy_inventory: true` to destroy non-`destructible: false` items instead.

`required_entity` on a `research_project` (L2/L2.5 §5) may reference either an actor
entity (a master craftsman whose presence is required) or, via `entity.inventory[item_id]`
in the project's gating condition, an item carried by an assigned researcher (an
artifact that must be present for the research to proceed).

### §14 Location Discovery

`alter_feature` supports a `contains` block and a `discovery_required` flag:

```
contains: {
  "items":      list[item_id],
  "entities":   list[entity_id],     # typically behavior_mode = DORMANT
  "myth_seeds": list[myth_seed]      # see §13 of this spec → Part IV §15
}
discovery_required: bool   # default false
```

When `discovery_required: true`, the contents of `contains` are **latent**: they
exist in world state but are not returned by `query_world_state`, `get_region()`, or
any GM/SM query, and contained `entities` do not execute tick logic (effectively
`DORMANT` regardless of their own `behavior_mode` setting) until a `discover_location`
delta fires for this `feature_id`.

`discover_location` is fired by an entity action rule — typically triggered when an
exploring entity's location matches the feature and a condition such as
`entity.location.distance_to(feature) < discovery_radius` holds. On firing:

1. `contains.items` become visible to inventory and prerequisite queries at this
   location.
2. `contains.entities` activate — their registered `behavior_mode` and rules begin
   executing from the next tick.
3. `contains.myth_seeds` are planted at this location (Part IV §13) and begin
   propagating through L2.5 contact networks from this point — not from world
   initialization.

Before discovery, the feature itself (as a `ruin`, `underground_region`, etc.) may be
visible on the map as a geographic feature — only its `contains` block is latent.

---

## Part IV — Myth Seeding (Entity-Side)

### §15 Myth Seeds on Action Rules

| Field | Type | Description |
|---|---|---|
| `myth_id` | `string` | Registered `myth` concept_id. |
| `stratum_id` | `string` | `""` = all strata at firing location. |
| `conviction` | `float` | Initial conviction at seed location, `0.0–1.0`. |
| `radius` | `float` | `0` = single location only; `>0` = nearby locations seeded at reduced conviction via falloff. |

When an entity's action rule fires with `myth_seeds` populated, each entry plants
conviction at the firing location as described in L2/L2.5 §15. This is how a dragon's
raid rule generates the `dragons_exist` myth at the raided settlement, which then
propagates outward through L2.5 contact networks — no myth exists anywhere in the
world until the rule that generates it fires.

---

## Part V — Tick Execution Sequence

Each simulation tick, for every active (non-latent) entity:

1. **Position update.** Advance along current behavior mode path; replan if blocked.
2. **Need decay.** Apply base decay rates and decay modifiers.
3. **Aura application.** Compute affected cells via weighted spatial join; apply
   `effect_magnitude × weight`; extend aura event log record's `t_end`.
4. **Capability availability check.** For each `capabilities_unlocked` entry, evaluate
   whether prerequisites, cooldown, and cost are currently satisfiable; update
   `entity.capability[id]` for use in this tick's condition evaluations.
5. **Action rule evaluation.** Evaluate rules in descending priority order. For each
   rule whose condition is true and cooldown has elapsed: apply effects via spatial
   join (including `use_capability` and `discover_location` deltas where present),
   insert event log record, plant `myth_seeds` if present, set narrative flag if
   configured.
6. **Termination check.** Evaluate `termination_condition` if declared, otherwise the
   `existence_type`'s default need-depletion termination (subject to
   `"need_termination"` immunity). If triggered: dissolve entity, drop or destroy
   inventory per dissolution rules, release resource claims, write termination record.

Steps 3 and 5 each execute the weighted spatial join independently. Layer state and
event log are always updated together.

---

## Part VI — Connections to Other Layers

**← From `define_world_concept` registrations:**
- `existence_type` → need vector template, termination model default (§2, §9)
- `fauna_species` → seeds L1 population fields; individual L3 instances spawned from
  a population inherit `existence_type` need semantics plus species `drops` (L2/L2.5 §1.1)
- `item` → inventory-carryable objects with aura/combat effect schemas (§12)
- `capability` → `capabilities_unlocked` entries; gates `use_capability` delta (§10)
- `myth` → target of `myth_seeds` on action rules (§15)

**→ To L2/L2.5:**
- `pre_engagement_effects` and inventory `combat_effects` → Phase 1 combat injection
  (L2/L2.5 §4)
- `leadership_profile`, `authority_overrides` → effective decision profile computation
  (L2/L2.5 §6, §10)
- `migration_out` events spawn `refugee_group` entities with `GOAL_SEEKING` behavior
  (L2/L2.5 §2)
- `institution_collapse` events spawn claimant/successor entities (L2/L2.5 §9, §10)

**→ To Query API:**
- `get_entity_state()` — identity, need state (if `continuity_depth` allows),
  behavior mode, inventory, capabilities, immunities
- `get_events()` — action rule firings with causal attribution
- Latent `contains` entities and items are excluded from all queries until
  `discover_location` fires (§14)
