# Two-Level Architecture & NPC Design

*Formal design for the narrative engine / world simulation split,
the NPC-as-entity model, and agent tool design.*

---

## §1 Two-Level Architecture

The system has two distinct levels that share no data stores, no entity IDs,
and no lifecycle. They are connected only through the `World` class which
may delegate reads to the entity store for certain types.

### Level 1 — Narrative Engine (stable, untouched)

```
Purpose:  Player-facing story. Turn execution. Character agency. GM/SM prompts.
Owns:     Player characters, story arcs, paragraphs, turns, scene state.
Storage:  game/world/*.json, game/characters/{name}/*.json
Agents:   GM (narrative), SM (scene lifecycle), Character agents.
Entry:    console_app.py → turn_runner.py → GM/SM/Character calls.
```

**Player characters are NOT entities.** They remain in per-character JSON files:
- `game/characters/{name}/description.json`
- `game/characters/{name}/state.json`
- `game/characters/{name}/skills.json`
- `game/characters/{name}/equipment.json`

The character agent system (`character/agent.py`, `character/memory.py`,
`character/reflection.py`) reads exclusively from these files. No entity_store
access.

**Story is NOT the event log.** `game/world/story.json` contains arcs,
paragraphs, turn history. `events.jsonl` contains simulation-level events
(faction formations, world changes). They are never mixed.

**Context building** (`world/context.py`, `engine/pinned_context.py`,
`gm/injection_builders.py`) reads from `World` class methods, which may
delegate to entity_store for NPC and location data — but the API contract
(the dict shape) does not change.

### Level 2 — World Simulation (new, v2 tools)

```
Purpose:  World model. Factions, NPCs, locations as entities. Event log.
          WM ReAct loop, GM/SM world-facing tools.
Owns:     NPCs, locations (as entities), factions, items, concepts, rules.
Storage:  game/entity_store/entities.json, events.jsonl
Agents:   WM (world master), GM (world-facing tools), SM (read-only queries).
Entry:   invoke_self scheduling, agent ReAct loops.
```

**NPCs ARE entities.** They live exclusively in `entities.json` with
`type="npc"`. They are NOT duplicated in `game/world/npc.json`.

**Locations ARE entities.** They live exclusively in `entities.json` with
`type="location"`. They are NOT duplicated in `game/world/locations.json`.

**Factions, items, concepts, rules, templates** — all entities with
appropriate `type` values. No separate files.

---

## §2 Bridge: World Class Delegation

The `World` class (`world/state.py`) provides the API that narrative engine
code uses. For methods that read NPCs and locations, the World class delegates
to entity_store instead of reading old JSON files:

| World method | Old backend | New backend |
|---|---|---|
| `get_npcs()` → `{name: data}` | `game/world/npc.json` | `entity_store.query_entities(type="npc")` → reformat |
| `get_npc(name)` → `data` | `game/world/npc.json[name]` | `entity_store.query_entities(tags=["name:..."])` or facts search |
| `get_locations()` → `{name: data}` | `game/world/locations.json` | `entity_store.query_entities(type="location")` → reformat |
| `get_location(name)` → `data` | `game/world/locations.json[name]` | `entity_store.query_entities(tags=["name:..."])` or facts search |
| `create_npc(...)` | `npc.json` write | removed — use `set_entity(type="npc")` |
| `create_location(...)` | `locations.json` write | removed — use `set_entity(type="location")` |
| `get_character_*(name)` | `game/characters/{name}/*.json` | unchanged — player chars not entities |
| `set_character_*(name)` | `game/characters/{name}/*.json` | unchanged |

**Format conversion:** The entity_store returns entities with fields `{id, type,
facts, gurps, stats, tags, position}`. The narrative engine expects the old
format `{name, description, location, ...}`. The bridge method renames fields
and extracts what the narrative engine needs.

**Name resolution:** Narrative engine looks up NPCs by human-readable name
("Bob"). Entity_store uses auto-generated IDs (`ent_20260625_0001`). The
bridge searches entities by checking `tags` for `name:<value>` or falls back
to substring search on `facts`. When creating an NPC via `set_entity`, the
caller should include a `"name:<display_name>"` tag for lookup.

---

## §3 NPC as Entity — Complete Design

### 3.1 Entity Fields — What Goes Where

```yaml
NPC entity:
  type: "npc"

  # ═══ NARRATIVE IDENTITY (read by SM for context, survives into pinned cache) ═══
  facts: "Marta Haskel, stout woman in her mid-fifties. Runs The Siren's Rest."
  # STATIC only. Who/what this entity IS, not what they're doing right now.
  # Everything in facts appears in the agent's pinned context — keep it
  # lean and identity-defining. NO mechanical data, NO current state,
  # NO inventory, NO conditions.

  # ═══ MECHANICAL STATS (read by SM for resolution queries) ═══
  gurps: {ST: 10, DX: 10, IQ: 11, HT: 10}
  # GURPS core stats. Static after creation. For adjustments use
  # adjust_entity(hp=..., add_condition=...).

  state: {hp: 10, fp: 10, conditions: [], injuries: []}
  # RUN-TIME STATE. Changes every scene via adjust_entity.

  skills: {passive_abilities: [], cached_skills: []}
  # ABILITIES. Semi-static. Add/remove via adjust_entity.

  equipment: {signature: [], inventory: [], currency: {}}
  # GEAR. Changes via adjust_entity(add_item=..., currency_delta=...).

  # ═══ FREE-FORM EXTENSION (simulation layer only, NOT in pinned context) ═══
  stats: {"current_state": "tending bar", "loyalty": 0.8}
  # The "belly button" — extensibility for WM tools and event conditions.
  # NOT read by narrative engine. NOT in pinned context.

  # ═══ METADATA ═══
  tags: ["name:Bob", "role:barkeep"]
  position: {"inside": "loc_tavern"}
```

#### What `facts` IS:
- Who this entity is ("Marta Haskel, runs The Siren's Rest")
- Appearance and personality ("stout, mid-fifties, knows everyone on the Docks")
- Permanent identity traits (role, reputation, species if relevant)
- **What survives context trimming** — facts is what the LLM always sees

#### What `facts` IS NOT:
- ❌ Current HP/FP — use `state` block via `adjust_entity(hp=..., fp=...)`
- ❌ Conditions or injuries — use `adjust_entity(add_condition=..., add_injury=...)`
- ❌ Inventory items — use `adjust_entity(add_item=..., currency_delta=...)`
- ❌ What the entity is doing right now — use `adjust_entity(current_state=...)` or `stats.current_state`
- ❌ GURPS stats — use `gurps` block or `adjust_entity(add_skill=...)`
- ❌ Skills or abilities — use `skills` block or `adjust_entity(add_ability=...)`
- ❌ Temporary effects — use `set_effect`
- ❌ Relationship scores — use `stats` dict (belly button, not in context)
- ❌ Quest/reward data — use `stats` dict

**Enforcement:** `set_entity` and `adjust_entity` validate that `facts` does not contain patterns matching mechanical data (HP digits, "ST 12" patterns, inventory lists). If detected, the tool returns a warning and suggests the correct structured field.

### 3.2 The "Belly Button" — Extension Point

Every entity type has a free-form `stats` dict. This is the universal
extensibility mechanism — no schema changes needed for new features:

- An NPC can have `{"current_state": "tending bar", "loyalty": 0.8,
  "debt": 50, "knows_secret": "the well is poisoned"}`.
- A location can have `{"population": 1200, "defenses": 0.3,
  "market_access": 0.7}`.
- A faction can have `{"military_strength": 0.6, "treasury": 5000}`.

The narrative engine does not read `stats` — it only reads `facts` and
`gurps`. The upper simulation (WM tools, event conditions) reads `stats`
for rule evaluation. This separation is intentional: the narrative engine
sees what it needs; the simulation layer sees everything.

### 3.3 Relationship to Scene Design (docs/scene_design.md)

NPCs as entities participate in scenes through the shared timeline and
intent/trigger system. The SM's `get_scene()` reads entities positioned at
the scene's location. NPC intents are authored by the SM on their behalf
(§7 — Scene Manager as Participant). NPC stats (`gurps`, `stats`) feed
the perception gate and resolution checks.

No duplication: the entity_store is the single source of truth for NPC
mechanical state. The scene runtime reads from it via `get_entity_state()`.

---

## §4 Entity Creation — One Tool

### 4.1 One `set_entity` for everything

NPCs, locations, factions, items, templates, concepts — all created with
the same `set_entity` tool:

```
set_entity(
  type="npc",          # discriminator
  facts="...",         # STATIC NARRATIVE IDENTITY only. Who they ARE, not what they have.
                       # No HP, no ST 12, no inventory — those go in gurps/state/equipment.
  gurps={...},         # mechanical stats (what SM reads for resolution)
  state={...},         # dynamic state (HP, FP, conditions) — via adjust_entity later
  skills={...},        # abilities, cached_skills — via adjust_entity later
  equipment={...},     # inventory, currency — via adjust_entity later
  stats={...},         # free-form extension (belly button, NOT in pinned context)
  tags=["name:Bob"],   # searchable labels
  position={"inside": "loc_tavern"}
)
```

The agent chooses what parameters to fill based on the entity type.
`read_tool_doc("set_entity")` provides type-specific guidance.

### 4.2 Why one tool, not many

- **Less cognitive load for the LLM:** one universal create/update tool
  instead of 15 special-purpose tools.
- **Extensible without tool changes:** new entity types don't need new tools.
  Just a new `type` string and documentation in `read_tool_doc`.
- **Consistent query surface:** `query_entities(type="npc")` works the same
  as `query_entities(type="faction")`.
- **No tool proliferation:** the old system had `create_location`,
  `create_npc`, `define_entity`, `define_faction`, `define_rule` — all
  doing similar things with different schemas. One tool replaces all.

### 4.3 Agent guidance per type

The LLM needs to know what fields matter for each type. This is provided
by `read_tool_doc("set_entity", parameters=["type"])` which returns the
type-specific guidance:

| Type | Key fields | Notes |
|---|---|---|
| `npc` | `facts`, `gurps`, `position`, `tags`, `stats` | NPCs are visible in scenes |
| `location` | `facts`, `position.coords`, `tags`, `stats` | Locations have spatial position |
| `faction` | `facts`, `tags`, `stats` | Factions are non-spatial |
| `item` | `facts`, `stats`, `position` | Items may have positions |
| `race_template` | `gurps`, `tags`, `facts` | Templates propagate via inheritance |
| `concept` | `facts`, `tags` | Abstract, no position |

### 4.4 Creating NPCs — agent workflow

**WM** (world initialization):
```
set_entity(
  type="npc",
  id="npc_bob",
  facts="A friendly barkeep...",
  gurps={"ST": 10, "DX": 10, "IQ": 10, "HT": 10},
  stats={"current_state": "tending bar"},
  tags=["name:Bob", "role:barkeep", "faction:merchant_guild"],
  position={"inside": "loc_tavern"}
)
```

**GM** (during narrative play):
```
set_entity(
  type="npc",
  id="npc_stranger",
  facts="A hooded figure...",
  gurps={"ST": 12, "DX": 14, "power_tier": 4},
  tags=["name:Stranger", "role:mysterious"],
  position={"inside": "loc_tavern"}
)
```

**SM** (read-only, during scene):
```
get_entity_facts(id="npc_bob")     → "A friendly barkeep..."
get_entity_state(id="npc_bob")     → {gurps: {...}, stats: {...}}
get_scene(location_id="loc_tavern") → lists npc_bob, npc_stranger as present
```

---

## §5 Final State File Map

```
game/
  entity_store/
    entities.json           ← NPCs, locations, factions, items, concepts
    events.jsonl            ← 4D event log (simulation events)
    scheduler_queue.json    ← pending invoke_self requests
    corrections.jsonl       ← WM→GM corrections
    wm_notifications.jsonl  ← GM→WM notifications
    sm_instructions.jsonl   ← GM→SM instructions
    gm_questions.jsonl      ← SM→GM questions

  world/
    info.json               ← world name, time, character list, last_location
    story.json              ← arcs, paragraphs, turn history
    scene.json              ← active scene (legacy, replaced by scene_design.md)

  characters/{name}/
    description.json        ← player character narrative description
    state.json              ← player character GURPS/state
    skills.json             ← player character skills
    equipment.json          ← player character equipment

  wm_state/                 ← WM sidecar (legacy, migration pending)
    world_orientation.json
    player_start.json
    ...

  world_setting.json        ← world_essence, gurps_calibration, time
```

---

## §6 Migration Checkpoints

| Step | What changes | Risk |
|---|---|---|
| 1 | `World.get_npcs()` delegates to entity_store | Low — same API |
| 2 | `World.get_locations()` delegates to entity_store | Low — same API |
| 3 | Old `game/world/npc.json` not written anymore | Low — tool removed |
| 4 | Old `game/world/locations.json` not written anymore | Low — tool removed |
| 5 | Scene runtime replaced by scene_design.md | High — separate project |
| 6 | `world/context.py` reads from entity_store directly | Medium — after bridge proven |
| 7 | `world/state.py` `create_*` methods removed | Low — no callers |
