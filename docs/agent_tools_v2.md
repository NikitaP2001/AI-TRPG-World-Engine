# Agent Tool Reference

*Complete tool definitions for World Master, Game Master,
Scene Manager, and Character agents.*

---

## Design Principles

Every tool does exactly one atomic thing. Complexity lives in scripts, not in
parameters. The same atomic operations appear across agents at different scopes.
`invoke_self` is the universal scheduling primitive. Scripts receive the same tools
as their owning agent plus a limited Python stdlib (math, random, re, datetime).
Storage is automatic — every tool call persists immediately, no separate save step.

---

## Part I — Core Concepts

### Entities

Everything is an entity. A race, a faction, a character, a location, a trap, a
political alliance, a ship deck, a spell. Type is a string the WM or GM defines.
The engine has no opinion about what kinds of things exist.

### Type Templates

An entity whose `id` is used as `base_type` by other entities. When an entity is
created with `base_type`, it inherits all `facts`, `gurps`, `stats`, `tags`, and
attached scripts from that template. Fields set on the instance override the
template; fields not set on the instance fall through to the template.
Template updates propagate to all instances that have not locally overridden the
changed field.

Templates are themselves entities — they are created with `set_entity` and their
type string by convention ends in `_template` or `_type`, e.g.
`type="race_template"`, `type="class_template"`. This is convention, not enforcement.

### Tags

Structured string labels attached to entities. Queryable. Composable. Used for
faction membership, race classification, status, and script targeting.
Format: `"namespace:value"`, e.g. `"faction:nazarick"`, `"race:undead"`,
`"status:hostile"`, `"tier:legendary"`. An entity may have any number of tags.
Tags are the primary mechanism for group membership — not prose.

### Position

One of three forms:
- `{"inside": "entity_id"}` — contained within another entity (a room, a ship,
  a dungeon floor). Inherits the container's coordinates.
- `{"near": "entity_id"}` — approximately co-located. Radius is world-defined.
- `{"coords": [x, y, z]}` — absolute world coordinates in the WM-defined
  coordinate system. Units are whatever the WM declared for this world.

Position is always optional. Non-spatial entities (a law, an alliance, a prophecy)
have `position: null`.

### GURPS Stats Block

The `gurps` parameter is a structured dict with defined keys. These are the stats
the system uses for mechanical resolution. Unknown keys are ignored.

```
{
  # Primary attributes — GURPS Basic Set p. 14
  "ST":  int,    # Strength. Lifting, melee damage. Human average: 10
  "DX":  int,    # Dexterity. Agility, coordination. Human average: 10
  "IQ":  int,    # Intelligence. Perception, willpower base. Human average: 10
  "HT":  int,    # Health. Stamina, resistance. Human average: 10

  # Secondary characteristics — derived by default, override here if different
  "HP":  int,    # Hit points. Default: ST. Override for non-standard entities.
  "Will": int,   # Willpower. Default: IQ. Fear checks, mental resistance.
  "Per":  int,   # Perception. Default: IQ. Noticing things.
  "FP":   int,   # Fatigue points. Default: HT. Exertion, casting cost.
  "Basic_Speed": float,  # Default: (DX+HT)/4. Initiative order.
  "Basic_Move":  int,    # Default: floor(Basic_Speed). Yards per second.

  # Combat
  "DR":   int,   # Damage Resistance. Subtracted from every hit. Default: 0.
  "Dodge": int,  # Active defense. Default: floor(Basic_Speed)+3.

  # Resistances (0.0–1.0, fraction of damage negated after DR)
  "poison_resist":    float,  # 0.0 = no resistance, 1.0 = immune
  "disease_resist":   float,
  "fire_resist":      float,
  "mental_resist":    float,  # vs fear, charm, illusion
  "holy_resist":      float,  # negative = vulnerable to holy damage
  "cold_resist":      float,

  # Power tier — for quick relative comparison across settings
  # 1 = typical civilian, 5 = elite soldier, 10 = demigod
  # Set this honestly; scripts use it for threat assessment
  "power_tier": int,

  # Character points — total GURPS CP. Optional; used for advancement tracking.
  "cp_total": int,
}
```

### Custom Stats

The `stats` parameter is a free dict for any numeric property beyond GURPS.
Keys are strings; values are floats. No reserved keys. Examples:
`{"level": 100}`, `{"magic_affinity": 3}`, `{"crew_capacity": 200}`,
`{"hull_integrity": 0.85}`, `{"loyalty": 1.0}`.

### Scripts

Python source attached to an entity. Called with a `context` dict:
```python
context = {
    "entity": dict,        # the entity this script is on
    "trigger_data": dict,  # what fired it; includes "actor_id" if an entity
                           # caused the trigger, "event_id" if an event did
    "world_tick": int,
}
```
Scripts may call any tool available to their owning agent. Available stdlib:
`math`, `random`, `re`, `datetime`. No network, no filesystem, no subprocess.
Scripts that need narrative judgment should call `invoke_self` and let the
agent handle it — scripts calculate, agents decide.

### The 4D Event Log

Append-only. Every `record_event` call creates an entry indexed by:
- `x, y, z` — spatial coordinates (bounding box for area effects)
- `t` — world tick (range `[t_start, t_end]` for sustained effects)

Queries can filter on any combination of spatial region, time range, entity,
and tag. The log is never modified or deleted.

---

## Part II — World Master Tools

---

### `set_entity`

Create or update any entity. The universal write operation. Works for templates,
characters, factions, locations, objects, effects, laws, alliances — anything.

```python
@tool
def set_entity(

    # ── Identity ──────────────────────────────────────────────────────────────

    id: str = "",
    # Unique entity identifier. If empty, auto-generated and returned.
    # On update: provide existing id to modify the entity in place.
    # Convention: use readable slugs, e.g. "char_ainz", "faction_nazarick",
    # "tpl_undead", "loc_e_rantel". Avoid spaces.

    type: str = "",
    # Entity type string. Required on create; ignored on update.
    # The WM defines these strings — the engine has no reserved types.
    # Conventions (not enforced): "race_template", "class_template",
    # "character", "npc", "faction", "location", "object", "effect_source",
    # "political_entity", "concept". Use whatever fits the world.

    base_type: str = "",
    # entity_id of a template to inherit from. Optional.
    # Inheritance: facts, gurps, stats, tags, and scripts from the template
    # apply unless overridden here. Overrides are per-field.
    # For multiple inheritance (e.g. race + class), use a chain:
    # create "tpl_elf_fighter" with base_type="tpl_elf", then override
    # with fighter stats. Or set base_type once and manually merge —
    # the system does not support multiple base_type values per entity.

    # ── Narrative ─────────────────────────────────────────────────────────────

    facts: str = "",
    # Prose description for LLM agents. Keep this minimal and functional.
    # This is NOT the place for lore — it is context the GM and SM need
    # to play this entity correctly: personality, current situation,
    # relationships, secrets, behavioral tendencies.
    # Bad: "Ainz is the last surviving member of his guild who was transported..."
    # Good: "Performs the role of omniscient overlord while privately improvising.
    #        Agrees with any plan a guardian presents as 'confirmation of his design.'
    #        Motivated by loyalty to absent guildmates, expressed through the NPCs."
    # Max recommended: 200 words. If longer, move background to record_event.

    # ── Mechanical ────────────────────────────────────────────────────────────

    gurps: dict = {},
    # GURPS stat block. See §Core Concepts — GURPS Stats Block for all keys.
    # Unset keys inherit from base_type or use GURPS defaults.
    # Example for a human-average NPC: {"ST":10,"DX":10,"IQ":10,"HT":10}
    # Example for a legendary swordsman: {"ST":14,"DX":16,"IQ":12,"HT":13,
    #   "DR":3,"power_tier":6}
    # Example for an undead entity immune to mental effects:
    #   {"mental_resist":1.0,"poison_resist":1.0,"disease_resist":1.0}

    stats: dict = {},
    # Free numeric properties beyond GURPS. Any string keys, float values.
    # Use for setting-specific properties not covered by GURPS.
    # Examples: {"level":100}, {"magic_affinity":3.0}, {"loyalty":1.0},
    # {"hull_integrity":1.0}, {"population":120000}, {"food_reserves":0.6}
    # Scripts and queries can read and modify these.

    tags: list = [],
    # Structured labels. Format: "namespace:value".
    # Standard namespaces (extend as needed for your world):
    #   "race:undead"         — biological/ontological category
    #   "faction:nazarick"    — organizational membership
    #   "class:warrior"       — role/profession
    #   "tier:legendary"      — relative power (common/elite/legendary/mythic)
    #   "status:hostile"      — current disposition toward players
    #   "alignment:evil"      — moral alignment if relevant to setting
    #   "energy:negative"     — energy affinity (negative/positive/void/etc.)
    #   "setting:new_world"   — which setting/plane this belongs to
    #   "created:yggdrasil"   — origin marker
    # Tags are the query surface for group membership. Use them.
    # Not using tags means you'll write scripts that parse facts text instead.

    # ── Spatial ───────────────────────────────────────────────────────────────

    position: dict = {},
    # Where this entity is. Three forms:
    #   {"inside": "entity_id"}  — contained in a location/vessel
    #   {"near":   "entity_id"}  — approximately co-located
    #   {"coords": [x, y, z]}   — absolute coordinates
    # Omit for non-spatial entities (laws, alliances, abstract concepts).
    # Position updates via set_movement for entities that move over time.

) -> dict:
    # Returns:
    # {
    #   "entity_id": str,         # the id (auto-generated if not provided)
    #   "operation": "created" | "updated",
    #   "inherited_from": str,    # base_type id if inheritance applied
    #   "tags_total": list[str],  # effective tags (own + inherited)
    #   "scripts_inherited": int, # scripts pulled in from base_type
    #   "warnings": list[str]
    # }
```

---

### `attach_script`

Attach a behavior script to an entity. Scripts run automatically when their
trigger fires. Multiple scripts may be attached to one entity.

```python
@tool
def attach_script(

    entity_id: str,
    # The entity this script is attached to.

    trigger_type: str,
    # What causes this script to run. One of:
    #
    # "on_tick"
    #   Fires every N world ticks.
    #   trigger_params: {"interval_ticks": int}  # e.g. 1, 30, 365
    #
    # "on_stat_threshold"
    #   Fires when a stat on this entity crosses a threshold.
    #   trigger_params: {
    #     "stat":      str,              # key in gurps or stats dict
    #     "threshold": float,
    #     "direction": "above" | "below"
    #   }
    #
    # "on_entity_enter"
    #   Fires when any entity matching a tag filter enters this entity's
    #   location (i.e. sets position to inside/near this entity).
    #   trigger_params: {
    #     "tag_filter": str,  # e.g. "race:human" — empty = any entity
    #     "radius": float     # how close counts as "enter", in world units
    #   }
    #
    # "on_event_nearby"
    #   Fires when an event with a matching tag is recorded within radius.
    #   trigger_params: {
    #     "tag_filter": str,  # event tag to match
    #     "radius": float
    #   }
    #
    # "on_world_time"
    #   Fires once at a specific world tick.
    #   trigger_params: {"tick": int}
    #
    # "on_tag_added"
    #   Fires when a specific tag is added to this entity.
    #   trigger_params: {"tag": str}

    trigger_params: dict,
    # Parameters for the chosen trigger_type. See above.

    script: str,
    # Python source. Called with context dict (see §Core Concepts — Scripts).
    # Available tools: all WM tools (for WM scripts) or all GM tools
    # (for GM scripts). Available stdlib: math, random, re, datetime.
    # Scripts should be narrow and deterministic. If judgment is needed,
    # call invoke_self() with a comment and let the agent decide.

    comment: str = "",
    # Why this script exists. Appears in the newspaper when script fires.
    # Helps WM/GM understand what triggered their invocation.
    # Example: "Fear aura — fires every tick, applies Will check to nearby living"

) -> dict:
    # Returns:
    # {
    #   "script_id": str,    # auto-generated; use this to detach later
    #   "entity_id": str,
    #   "trigger_type": str,
    #   "warnings": list[str]
    # }
```

---

### `detach_script`

Remove a script from an entity.

```python
@tool
def detach_script(
    entity_id: str,
    script_id: str,   # returned by attach_script
) -> dict:
    # Returns: {"entity_id": str, "script_id": str, "status": "removed"}
```

---

### `set_effect`

Apply a persistent stat modifier to an entity. Use for simple, arithmetic
auto-applying effects: bleeding, regeneration, poison progression, power drain.
For effects that require world queries or conditional logic, use attach_script.

```python
@tool
def set_effect(

    entity_id: str,

    effect_id: str = "",
    # Unique id for this effect on this entity. Auto-generated if empty.
    # Use a readable id if you'll need to remove it later:
    # "bleeding_wound_1", "lich_fear_debuff", "poison_stage_2"

    stat_deltas: dict = {},
    # Stats to modify, and by how much each tick (if per_tick=True)
    # or as a one-shot (if per_tick=False).
    # Keys must exist in gurps or stats on the entity, or will be created.
    # Examples:
    #   {"HP": -2}                  — bleeding: lose 2 HP per tick
    #   {"HP": 1}                   — regeneration: gain 1 HP per tick
    #   {"FP": -1, "Basic_Move": -1} — exhaustion
    #   {"attack_penalty": -2}      — fear debuff on custom stat
    # Negative values deplete, positive values restore.

    per_tick: bool = True,
    # True: delta applied every world tick until duration_ticks expires
    #       or remove_effect is called.
    # False: one-shot delta applied immediately on set_effect call.

    duration_ticks: int = -1,
    # How many ticks before this effect expires automatically.
    # -1 = permanent until remove_effect is called.
    # Example: 10 = lasts 10 world ticks.

    description: str = "",
    # Human-readable name. Appears in entity state, newspaper, GM context.
    # Example: "bleeding wound", "poison stage 2", "fear debuff from lich aura"

) -> dict:
    # Returns:
    # {
    #   "effect_id": str,
    #   "entity_id": str,
    #   "expires_at_tick": int | null,
    #   "warnings": list[str]
    # }
```

---

### `remove_effect`

Remove a named effect from an entity immediately.

```python
@tool
def remove_effect(
    entity_id: str,
    effect_id: str,
) -> dict:
    # Returns: {"entity_id": str, "effect_id": str, "status": "removed"}
```

---

### `set_movement`

Set an entity in motion toward a destination. The system updates position
automatically over ticks, writes position events to the 4D log, and fires
`on_entity_enter` and `on_arrive` triggers on arrival. WM-only — positional
authority over world-scale entity movement belongs to the WM.

```python
@tool
def set_movement(

    entity_id: str,

    destination: dict,
    # Where the entity is heading. Same three forms as position:
    #   {"inside": "entity_id"}  — moving into a location
    #   {"near":   "entity_id"}  — moving toward another entity
    #   {"coords": [x, y, z]}   — moving to absolute coordinates
    # If destination is another entity that is itself moving, the system
    # tracks the target's position at each tick. Use for pursuit/escort.

    speed: float = 1.0,
    # World units per tick. Set to 0 for instant teleportation.
    # Calibrate against your world's coordinate scale.
    # Example for a world where 1 unit = 1 km and 1 tick = 1 day:
    #   human walking speed ≈ 0.04 (40 km/day)
    #   army marching      ≈ 0.025
    #   dragon flying      ≈ 0.5
    #   teleportation      = 0

    comment: str = "",
    # Why this entity is moving. Written to movement event log.

) -> dict:
    # Returns:
    # {
    #   "entity_id": str,
    #   "destination": dict,
    #   "estimated_arrival_tick": int | null,  # null if speed==0
    #   "movement_id": str   # for cancelling with set_movement(speed=0)
    # }
```

---

### `add_tags` / `remove_tags`

Add or remove tags from an entity without touching other fields.

```python
@tool
def add_tags(
    entity_id: str,
    tags: list,       # list of "namespace:value" strings to add
) -> dict:
    # Returns: {"entity_id": str, "tags_added": list, "tags_total": list}

@tool
def remove_tags(
    entity_id: str,
    tags: list,       # list of "namespace:value" strings to remove
) -> dict:
    # Returns: {"entity_id": str, "tags_removed": list, "tags_total": list}
```

---

### `record_event`

Write an event to the 4D causal log. Use for things that *happened*:
a battle, a death, a diplomatic exchange, a discovery. The log is append-only.

For asserting current entity state, use `set_entity`. This tool is for
recording *occurrences*, not *conditions*.

```python
@tool
def record_event(

    description: str,
    # What happened. Written for LLM consumption — clear and specific.
    # Bad: "conflict occurred"
    # Good: "Gazef Stronoff's unit engaged undead forces north of E-Rantel.
    #        Undead dispersed without significant losses. Gazef's unit: 3 wounded."

    entity_ids: list = [],
    # All entities causally involved. Used for query filtering.
    # Include both actors and patients: attacker, defender, witness.

    position: dict = {},
    # Where it happened. Same three forms as entity position.
    # Used for spatial 4D log indexing. Omit for non-spatial events.

    t_override: int = -1,
    # Specific world tick to log this event at. WM-only field.
    # -1 = current world tick (default and only option for GM).
    # Use for asserting historical events that preceded current play:
    # "this faction sent a messenger on tick 840" even if current tick is 900.
    # Do not use to retroactively change what players have already seen.

    tag: str = "",
    # Single categorical label for query filtering.
    # Examples: "combat", "political", "discovery", "death", "first_contact",
    #           "economic", "magic", "wm_correction"

    narrative_flag: bool = False,
    # True = this event appears in GM and SM newspaper on next invocation.
    # Use for events that should proactively reach narrative agents.
    # False = event is in the log but agents must query to find it.

    causal_parent: str = "",
    # event_id of the event that caused this one.
    # Enables causal chain traversal in query_events.
    # Leave empty if this event has no traceable cause.

) -> dict:
    # Returns:
    # {
    #   "event_id": str,
    #   "logged_at_tick": int,
    #   "position_indexed": bool,
    #   "warnings": list[str]
    # }
```

---

### `query_entities`

Read entities from world state. Supports flexible filtering.

```python
@tool
def query_entities(

    type: str = "",
    # Filter by entity type string. Empty = any type.

    base_type: str = "",
    # Filter to instances of a specific template (direct or inherited).
    # e.g. base_type="tpl_undead" returns all undead entities.

    tags: list = [],
    # Filter by tags. All listed tags must be present on matching entities.
    # e.g. ["faction:nazarick", "tier:legendary"] = legendary Nazarick entities.

    position: dict = {},
    # Spatial filter center. Same three forms as entity position.

    radius: float = -1.0,
    # Spatial radius. Only valid with position. -1 = no spatial filter.
    # Units match the world's coordinate system.

    facts_search: str = "",
    # Semantic search over entity facts text. Fuzzy match.
    # Use for natural language queries: "entities with a grudge against humans"
    # Use sparingly — structural queries (tags, type) are more reliable.

    include_templates: bool = False,
    # True = include template entities in results.
    # False (default) = exclude templates; return only instances.

    limit: int = 50,
    # Maximum results returned. Increase for broad scans.

) -> list:
    # Returns list of entity dicts, each containing:
    # {
    #   "id": str,
    #   "type": str,
    #   "base_type": str,
    #   "facts": str,
    #   "gurps": dict,
    #   "stats": dict,
    #   "tags": list[str],           # effective tags (own + inherited)
    #   "position": dict,
    #   "active_effects": list[dict],
    #   "script_count": int,
    # }
```

---

### `get_entity`

Read a single entity in full, including scripts.

```python
@tool
def get_entity(
    id: str,
) -> dict:
    # Returns full entity dict including:
    # - all fields from query_entities results
    # - "scripts": list of {script_id, trigger_type, trigger_params, comment}
    #   (script source not included — use for auditing, not re-reading code)
    # - "effect_ids": list of active effect ids
    # - "movement": current movement record if entity is in transit
```

---

### `query_events`

Read from the 4D causal event log.

```python
@tool
def query_events(

    position: dict = {},
    # Spatial filter center.

    radius: float = -1.0,
    # Spatial radius. -1 = no spatial filter.

    z_min: float = -999999.0,
    z_max: float = 999999.0,
    # Vertical band. Default covers all depths.
    # Use z_min=-500, z_max=0 to query underground events only.

    t_from: int = -1,
    t_to: int = -1,
    # Tick range. -1 = unbounded.
    # Example: t_from=world_tick-30, t_to=world_tick = last 30 ticks.

    entity_id: str = "",
    # Filter: events where this entity is listed in entity_ids.

    tag: str = "",
    # Filter: events with this tag.

    narrative_flag_only: bool = False,
    # True = return only events flagged for GM/SM attention.

    causal_parent: str = "",
    # Return events caused by this event_id (consequence chain).

    limit: int = 100,

) -> list:
    # Returns list of event dicts:
    # {
    #   "event_id": str,
    #   "description": str,
    #   "entity_ids": list[str],
    #   "position": dict,
    #   "tick": int,
    #   "tag": str,
    #   "narrative_flag": bool,
    #   "causal_parent": str
    # }
```

---

### `delete_entity`

Permanently remove an entity. WM-only.
Does not remove the entity from the event log — past events that reference it
remain. Sets a `deleted_at_tick` marker on the entity record.

```python
@tool
def delete_entity(
    id: str,
    reason: str = "",   # why — written to event log
) -> dict:
    # Returns: {"entity_id": str, "status": "deleted", "event_id": str}
```

---

### `invoke_self`

Schedule the calling agent's next invocation. Universal scheduling primitive.
Can be called from tool calls and from inside scripts.

```python
@tool
def invoke_self(

    delay_ticks: int = -1,
    # Relative delay: invoke this agent N ticks from now.
    # -1 = not used (provide world_time instead).

    world_time: int = -1,
    # Absolute world tick to invoke at.
    # -1 = not used (provide delay_ticks instead).
    # Exactly one of delay_ticks or world_time must be provided.

    comment: str = "",
    # Why — appears at the top of the agent's context on invocation.
    # Be specific: "Demiurge's scheming tick — review political events"
    # is better than "periodic check".

) -> dict:
    # Returns: {"scheduled_tick": int, "comment": str}
```

---

### `write_correction` (WM only)

Send a forward-facing correction to the GM. Does not retroactively change anything.
Injected into GM's context on next invocation. Use when WM sees a GM-created entity
or event that contradicts world canon — correct going forward, not backward.

```python
@tool
def write_correction(

    message: str,
    # What the WM wants the GM to know and apply going forward.
    # Be specific: state what was wrong and what should be true instead.
    # The GM will apply this in narration; no automatic state changes occur.

    entity_id: str = "",
    # If the correction involves a specific entity, reference it here.
    # WM should also call set_entity/set_entity_facts to update the record
    # alongside this message — the correction message reaches the GM,
    # but entity facts must be updated separately.

) -> dict:
    # Returns: {"correction_id": str, "delivered_at": "next_gm_invocation"}
```

---

### `set_entity_facts` (WM and GM)

Update only the prose facts of an entity. Does not touch stats, tags, or scripts.
Use for targeted fact updates without risk of overwriting other fields.

```python
@tool
def set_entity_facts(
    id: str,
    facts: str,   # complete replacement of the facts field
) -> dict:
    # Returns: {"entity_id": str, "operation": "facts_updated"}
```

---

## Part III — Game Master Tools

GM has: `set_entity`, `set_entity_facts`, `add_tags`, `remove_tags`,
`attach_script`, `detach_script`, `set_effect`, `remove_effect`,
`record_event` (without `t_override`), `query_entities`, `get_entity`,
`query_events`, `invoke_self`.

GM does NOT have: `set_movement`, `delete_entity`, `write_correction`.

Two additional GM-only tools:

---

### `get_entity_facts` (GM and SM)

Read only the prose facts of an entity. Used by SM to load NPC context
on demand without receiving full mechanical stats.

```python
@tool
def get_entity_facts(
    id: str,
) -> str:   # returns the facts string only
```

---

### `notify_wm` (GM only)

Notify the World Master of a significant event or pending action.

```python
@tool
def notify_wm(

    message: str,
    # What the GM wants the WM to know. Clear and specific.
    # Example: "Creating a dragon entity in the dungeon. Is this consistent
    #           with world canon? Proceeding unless blocked."

    blocking: bool = False,
    # False (default): message queued for WM's next newspaper.
    #   Use for: informing WM of significant creations, notable events.
    # True: GM pauses. WM is invoked immediately. GM continues only after
    #   WM calls write_correction or explicitly releases the block.
    #   Use for: actions that could fundamentally break world canon and
    #   cannot be corrected forward easily. Use sparingly.

) -> dict:
    # If blocking=False: {"status": "queued", "appears_in": "next_wm_newspaper"}
    # If blocking=True:  {"status": "wm_invoked", "correction": str | null}
    #                    # correction is the WM's write_correction message if sent
```

---

### `escalate_to_sm` (GM only)

Send an instruction or context update to the Scene Manager.

```python
@tool
def escalate_to_sm(
    message: str,   # instruction or context for the current or next scene
) -> dict:
    # Returns: {"status": "queued", "appears_in": "next_sm_invocation"}
```

---

## Part IV — Scene Manager Tools

SM has: `get_entity_facts`, `query_events` (location-scoped),
`record_event` (current tick only), `invoke_self`.

SM additionally has these scene-specific tools:

---

### `get_scene`

Read current state of a location: who is present, their mechanical state,
recent events.

```python
@tool
def get_scene(
    location_id: str,   # entity_id of the location
) -> dict:
    # Returns:
    # {
    #   "location": {entity summary},
    #   "entities_present": list of {id, type, tags, gurps summary,
    #                                stats, active_effects, position},
    #   "recent_events": list of last 20 events at this location,
    #   "active_scripts_firing": list of script comments that fired
    #                            since last SM invocation
    # }
```

---

### `get_entity_state`

Read current mechanical state of one entity: stats and active effects.
Does not include facts (use `get_entity_facts` for narrative context).

```python
@tool
def get_entity_state(
    entity_id: str,
) -> dict:
    # Returns:
    # {
    #   "id": str,
    #   "gurps": dict,         # current values after all effects applied
    #   "stats": dict,         # current values
    #   "active_effects": list of {effect_id, description, remaining_ticks,
    #                              stat_deltas, per_tick}
    # }
```

---

### `update_entity_stat`

Update a single stat value on an entity. For scene-resolution outcomes:
HP loss from combat, FP loss from spellcasting, etc.

```python
@tool
def update_entity_stat(
    entity_id: str,
    stat: str,    # key in gurps or stats dict
    value: float, # new absolute value (not delta — set the result directly)
) -> dict:
    # Returns: {"entity_id": str, "stat": str, "old_value": float, "new_value": float}
```

---

### `apply_effect` (SM)

Apply an effect during scene resolution. Same as `set_effect` on GM/WM
but called from the Scene Manager during active scene resolution.

```python
@tool
def apply_effect(
    entity_id: str,
    effect_id: str = "",
    stat_deltas: dict = {},
    per_tick: bool = False,   # SM effects default to one-shot
    duration_ticks: int = -1,
    description: str = "",
) -> dict:
    # Returns: {"effect_id": str, "entity_id": str}
```

---

### `escalate_to_gm`

Request GM involvement. Use when scene requires creation of new persistent
entities or facts beyond SM authority.

```python
@tool
def escalate_to_gm(
    question: str,    # what SM needs from GM
    context: str,     # relevant scene state GM needs to answer
    blocking: bool = True,   # almost always True for SM — needs answer before continuing
) -> str:   # GM's response message
```

---

## Part V — Character Agent Tools

---

### `observe`

Perceive the current scene. Returns only what this character can observe
given their position, awareness, and active senses. Awareness-filtered
by the system — character cannot see what they could not physically perceive.

```python
@tool
def observe(
    query: str = "",
    # Directed attention. Empty = general awareness snapshot.
    # "I examine the lock closely" — focused; returns detail on the lock.
    # "I listen at the door" — sensory; returns auditory events nearby.
    # "I scan the room for threats" — tactical; returns entities by threat.
    # The system filters results by what this character could plausibly
    # perceive, given their Per stat and current conditions.
) -> dict:
    # Returns awareness-filtered perception:
    # {
    #   "location": str,             # where the character is
    #   "entities_visible": list,    # entities character can see/hear
    #   "recent_events_perceived": list,  # events character witnessed
    #   "active_effects_on_self": list,   # own current effects
    #   "own_stats_current": dict,        # own current GURPS stats
    # }
```

---

### `recall`

Query own memory: self-reflection, relationship assessments, personal history.

```python
@tool
def recall(
    query: str,
    # Natural language. Examples:
    # "what do I know about Ainz Ooal Gown"
    # "what are my current goals"
    # "what happened last time I was in E-Rantel"
) -> dict:
    # Returns from character's own memory files:
    # {
    #   "self_reflection": str,    # current beliefs, goals, emotional state
    #   "relationship_records": list of {entity_id, assessment},
    #   "relevant_memories": list  # matched from memory file
    # }
```

---

### `update_relationship`

Update this character's subjective assessment of another entity.

```python
@tool
def update_relationship(
    entity_id: str,
    assessment: str,   # this character's current view — subjective, first-person
                       # "I trust Sebas completely but suspect he hides Ainz's
                       #  true nature from me"
) -> dict:
    # Returns: {"entity_id": str, "status": "updated"}
```

---

### `update_memory`

Update this character's self-reflection.

```python
@tool
def update_memory(
    content: str,   # complete replacement of self-reflection
                    # current beliefs, goals, emotional state, priorities
) -> dict:
    # Returns: {"status": "updated"}
```

---

### `declare_intent`

Declare an action or standing reaction on the shared scene timeline.
See scene_design.md for full intent resolution semantics.

```python
@tool
def declare_intent(

    action_type: str,
    # "speech"   — verbal communication
    # "movement" — physical displacement
    # "combat"   — hostile physical or magical action
    # "cosmetic" — visible but non-mechanical action (gesture, expression)
    # Open: any string. Unlisted types treated as cosmetic for schema validation.

    target: str,
    # entity_id of specific target, or:
    # "all"  — affects everyone in awareness range
    # "area" — affects a zone (requires area parameter)
    # "self" — affects only self

    interval: list,
    # [t_start, t_end]
    # t_start: when this action begins (world tick)
    # t_end:   when it ends. null = persistent subscription (never expires
    #          until cancel_subscription is called)

    description: str,
    # What the character is doing. Narrative content for SM narration.

    visibility: str = "public",
    # "public"  — observable by anyone with awareness. No perception check.
    # "private" — deliberate concealment. Perception gate applies.
    #             Requires detection_difficulty if action has mechanical effect.

    detection_difficulty: int = -1,
    # GURPS difficulty for observers to notice this action.
    # Required if visibility="private" and action has mechanical effect.
    # -1 = not applicable (public actions).
    # Scale: 3 = trivially easy, 10 = average, 15 = very hard, 20 = legendary.

    trigger_condition: dict = {},
    # Optional: this intent only activates if this condition is met.
    # {
    #   "actor":       str,   # entity_id or "any"
    #   "action_type": str,   # action type to watch for, or "any"
    #   "target_match": str   # my name, "all", or "any"
    # }
    # Empty = intent is unconditional.

    trigger_priority: int = 0,
    # When multiple intents trigger on the same event, lower priority fires first.
    # Use for standing reactions that should always happen before others:
    # Albedo responding before any other guardian = trigger_priority: -10.
    # Default 0 = standard order.

    on_trigger: str = "interrupt_with_new_decision",
    # "interrupt_with_new_decision"  — re-invoke this agent when trigger fires.
    #                                  Agent decides response in context.
    # "apply_prepared_reaction"      — mechanically apply prepared_reaction
    #                                  without re-invoking agent.

    prepared_reaction: dict = {},
    # Pre-authored response intent. Only used if on_trigger =
    # "apply_prepared_reaction". Same schema as declare_intent.

    on_timeout: dict = {},
    # What to do if trigger_condition never fires before t_end.
    # Same schema as declare_intent. Empty = intent simply expires.

) -> str:   # returns subscription_id for cancel_subscription
```

---

### `cancel_subscription`

Cancel an active persistent subscription or bounded intent.

```python
@tool
def cancel_subscription(
    subscription_id: str,  # returned by declare_intent
) -> dict:
    # Returns: {"subscription_id": str, "status": "cancelled"}
```

---

### `commit_decision`

The character's final output for this invocation. The only tool whose output
is saved to the character's persistent message history.
Must be called once per invocation, after any observe/recall calls.

```python
@tool
def commit_decision(

    psyche_core: str,
    # MAX 100 WORDS. The character's raw internal state:
    # affective impulse (what they feel/want), structural orientation
    # toward other entities (as subjects, instruments, or obstacles).
    # Not filtered. Not rationalized. The character as they are.

    ego_rationalization: str,
    # MAX 100 WORDS. How the character processes their psyche_core
    # against the current situation:
    # internal reasoning, tactical calculus, defense mechanisms,
    # how they maintain consistency with their own past behavior.

    action: str,
    # MAX 150 WORDS. What the character actually does and says.
    # The output of psyche_core filtered through ego_rationalization.
    # Should also declare any intents not already submitted via
    # declare_intent: conditions they're watching for, how long
    # they intend to maintain this action.

) -> str:   # confirmation; this output is saved to history
```

---

## Part VI — Access Scope Reference

| Tool                     | WM | GM | SM | Char |
|--------------------------|----|----|----|------|
| `set_entity`             | ✓  | ✓  | —  | —    |
| `set_entity_facts`       | ✓  | ✓  | —  | —    |
| `get_entity` (full)      | ✓  | ✓  | —  | —    |
| `get_entity_facts`       | ✓  | ✓  | ✓  | —    |
| `get_entity_state`       | —  | —  | ✓  | —    |
| `add_tags`               | ✓  | ✓  | —  | —    |
| `remove_tags`            | ✓  | ✓  | —  | —    |
| `query_entities`         | ✓ global | ✓ global | — | — |
| `query_events`           | ✓ global | ✓ global | ✓ location | — |
| `get_scene`              | —  | —  | ✓  | —    |
| `record_event`           | ✓ + t_override | ✓ current tick | ✓ location | — |
| `attach_script`          | ✓  | ✓  | —  | —    |
| `detach_script`          | ✓  | ✓  | —  | —    |
| `set_effect`             | ✓  | ✓  | —  | —    |
| `apply_effect`           | —  | —  | ✓  | —    |
| `remove_effect`          | ✓  | ✓  | ✓  | —    |
| `update_entity_stat`     | —  | —  | ✓  | —    |
| `set_movement`           | ✓  | —  | —  | —    |
| `delete_entity`          | ✓  | —  | —  | —    |
| `invoke_self`            | ✓  | ✓  | ✓  | —    |
| `write_correction`       | ✓  | —  | —  | —    |
| `notify_wm`              | —  | ✓  | —  | —    |
| `escalate_to_gm`         | —  | —  | ✓  | —    |
| `escalate_to_sm`         | —  | ✓  | —  | —    |
| `observe`                | —  | —  | —  | ✓    |
| `recall`                 | —  | —  | —  | ✓    |
| `update_relationship`    | —  | —  | —  | ✓    |
| `update_memory`          | —  | —  | —  | ✓    |
| `declare_intent`         | —  | —  | —  | ✓    |
| `cancel_subscription`    | —  | —  | —  | ✓    |
| `commit_decision`        | —  | —  | —  | ✓    |
