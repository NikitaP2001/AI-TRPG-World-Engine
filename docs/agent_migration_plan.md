# Agent Tool Migration Plan

*Migration of GM and SM agents to the v2 toolset,
plus shared backend for all three agents.*

---

## Status

- **WM:** 18 tools implemented. entity_store + event_log backends working.
- **GM:** v2 tools implemented (16 tools). Termination changed to `close_invocation` (design, not yet coded).
- **SM:** v2 tools implemented (9 tools). Termination changed to `close_invocation` (design, not yet coded).
- **Shared backend:** Entity Registry (JSON), Event Log (JSONL) — implemented.
- **Invocation context:** New design — see `invocation_context_design.md`. Not yet implemented.

---

## Part I — What We Keep (do not touch)

The following subsystems are stable, working, and will remain as-is during the
migration. Only the tools each agent calls will change — the orchestration
layers, prompts, guardrails, and persistence for narrative content are untouched.

### 1. `world/` package (narrative state)

The `world/` package is the current persistent storage for narrative content:
locations, NPCs, characters, scenes, story arcs, and world metadata. It uses
JSON files in `game/world/`. The v2 Entity Registry will eventually replace
it, but during migration both systems coexist:

- New tools write to the Entity Registry.
- Old code that reads from `world/` continues working until fully migrated.
- No changes to `world/__init__.py`, `world/story.py`, `world/time.py`,
  `world/io.py`, `world/context.py`, `world/delta.py`, `world/state.py`,
  `world/migrations.py`, `world/json_pointer.py`.

### 2. `scene/` package (active scene runtime)

The `scene/` package manages the active scene: which characters and NPCs are
present, initiative order, timeline. The SM's `add_character_to_scene`,
`remove_character_from_scene`, etc. delegate to this package.

Kept as-is. In the v2 design, scene composition becomes implicit — entities
with `position: {"inside": "scene_id"}` — but the runtime still needs to track
initiative order, turn state, etc.

No changes to `scene/__init__.py`, `scene/context.py`, `scene/runtime.py`.

### 3. `engine/tool_base.py` (shared infrastructure)

Shared singletons and guardrails used by both GM and SM tools:

- `_WORLD`, `_SCENE` — narrative state singletons.
- `_TURN_LOCKED`, `reset_turn_lock`, `_guard_tool` — turn execution guardrails.
- `_json`, `_override_*` — JSON and override helpers.

Kept as-is. In the future, `_WORLD` may delegate to the Entity Registry, but
the singleton pattern and guardrail logic remain.

### 4. `gm/react_loop.py` (ReAct loop)

The `react_loop_iteration()` function. Used by all three agents. No changes.

### 5. `gm/` injection system

- `gm/history_injector.py` — GMHistoryInjector.
- `gm/injection_models.py` — InjectionProfile, InjectionRule, InjectionEngine.
- `gm/injection_builders.py` — context block builders.
- `gm/full_history.py` — full message history load/save/trim.
- `gm/history_meta.py` — HistoryMeta.

No changes. These handle message injection into GM context, independent of tools.

### 6. `engine/` narrative engine

- `engine/story_tracker.py` — story progress queries.
- `engine/gm_context.py` — GMContextManager.
- `engine/pinned_context.py` — pinned context blocks.
- `engine/summarizer.py` — SummaryRunner.
- `engine/history_meta.py` — HistoryMeta (shared base).
- `engine/history_injector.py` — GMHistoryInjector base.

No changes. These manage narrative state, not entity state.

### 7. `scene_manager/core.py` (SM orchestration phases)

The Scene Manager has a complex multi-phase flow that must be preserved
in structure. Only the tools called within each phase change:

- **World Planning phase** — pre-establishes hidden facts via GM.
- **Character Execution phase** — loops over characters, calls character agents.
- **Turn Narration phase** — produces narrative output with correction rounds.
- **Summarization phase** — produces paragraph summaries.

Phase orchestration logic, error handling, retry loops, history management
remain. Only the tool names and parameters change.

### 8. `gm/game_master.py` (GameMaster class)

The `GameMaster` class with `call_gm()`, LLM configuration, history paths,
summary runners. No structural changes. Only the tool list changes.

---

## Part II — Current GM Tools (already migrated)

GM tools migrated to v2. Full list:

| Tool | Source | Notes |
|---|---|---|
| `set_entity` | entity_store | Universal CRUD |
| `set_entity_facts` | entity_store | Facts-only update |
| `get_entity` | entity_store | Full entity read |
| `get_entity_facts` | entity_store | Facts-only read |
| `add_tags` / `remove_tags` | entity_store | Tag management |
| `query_entities` | entity_store | Filtered search |
| `query_events` | event_log | Event log read |
| `record_event` | event_log | Event log write (no t_override) |
| `attach_script` / `detach_script` | entity_store | Script management |
| `set_effect` / `remove_effect` | entity_store | Effect management |
| `invoke_self` | scheduler_queue | Pure scheduling (no termination) |
| `notify_wm` | file stub | GM→WM notification |
| `escalate_to_sm` | file stub | GM→SM instruction |
| `close_invocation` | *design* | New universal termination (see invocation_context_design.md) |
| `read_tool_doc` | tool_docs_data | Parameter documentation |

**Termination:** Currently `invoke_self_gm` (temp). Target: `close_invocation`.

**Readonly tools:** `get_entity`, `get_entity_facts`, `query_entities`, `query_events`

**GM-only:** `notify_wm`, `escalate_to_sm`

Old tools removed: `ready_to_proceed`, `answer_lore_question`, `gm_summary_result`,
`create_location`, `get_location`, `update_location`, `delete_location_path`,
`delete_location`, `create_npc`, `get_npc`, `update_npc`, `delete_npc_path`,
`delete_npc`, `get_character_detail`, `update_character_state`,
`update_character_skills`, `update_character_equipment`, `read_character_diary`.

---

## Part III — Current SM Tools (already migrated)

SM tools migrated to v2. Full list:

| Tool | Source | Notes |
|---|---|---|
| `get_entity_facts` | entity_store | NPC/character facts read |
| `query_events` | event_log | Location-scoped event read |
| `record_event` | event_log | Event write (current tick only) |
| `invoke_self` | scheduler_queue | Pure scheduling |
| `get_scene` | entity_store + event_log | Read location + participants + events |
| `get_entity_state` | entity_store | Read-only mechanical state |
| `update_entity_stat` | entity_store | Single-stat scene update |
| `apply_effect` | entity_store | One-shot scene effect |
| `escalate_to_gm` | file stub | Structured escalation |
| `close_invocation` | *design* | New universal termination |
| `read_tool_doc` | tool_docs_data | Parameter documentation |

**Termination:** Currently forced tool pairs (`record_event` + `invoke_self` etc).
Target: `close_invocation`.

**Readonly tools:** `get_entity_facts`, `query_events`, `get_scene`, `get_entity_state`

Old tools removed: `turn_narration`, `correct_character_intents`, `run_scene`,
`answer_character`, `call_gm`, `add_character_to_scene`, `remove_character_from_scene`,
`add_npc_to_scene`, `remove_npc_from_scene`.

### Scene composition is now implicit

An entity with `position: {"inside": "location_id"}` is present in that location's
scene. `get_scene(location_id)` reads all entities positioned there.

---

## Part IV — Shared Backend (Implemented)

### Layer 1 — Entity Registry (JSON)

File: `world_manager/entity_store.py`
Storage: `game/entity_store/entities.json`

Implemented: set_entity, get_entity, query_entities, delete_entity,
add_tags, remove_tags, set_entity_facts, get_entity_facts,
attach_script, detach_script, set_effect, remove_effect, set_movement.

Template inheritance (base_type) is supported. Tag and type queries work.
Spatial queries use haversine distance.

### Layer 2 — 4D Event Log (JSONL)

File: `world_manager/event_log.py`
Storage: `game/entity_store/events.jsonl`

Implemented: record_event, query_events. Filters by tag, entity_id,
time range, narrative_flag, causal_parent. Spatial filters are basic.

### Layer 3 — Effects System

Stored on entity records in entities.json. Effect application engine
(per_tick, duration expiry) is not yet implemented — only storage.

### Layer 4 — Script Engine

Stored on entity records. Script execution engine not yet implemented.

### Layer 5 — Scheduling

File: `scheduler_queue.py`
Storage: `game/entity_store/scheduler_queue.json`

Implemented: schedule_invocation, pop_due_invocations. Used by invoke_self.
Orchestrator checks queue after each turn (console_app.py line ~350).

**For detailed invocation context design (newspaper, summaries,
close_invocation), see `invocation_context_design.md`.**

---

## Part V — Remaining Work

### High Priority — Invocation Context System (invocation_context_design.md)

- [ ] Implement `close_invocation` tool (WM, GM, SM)
- [ ] Per-agent summary storage (game/*_state/summaries.jsonl)
- [ ] News queue files per agent (game/*_state/news_queue.jsonl)
- [ ] Newspaper assembly in orchestrator (console_app.py)
- [ ] Save/clear protocol for news queues

### Medium Priority

- [ ] `World.get_npcs()` delegation to entity_store (bridge)
- [ ] `World.get_locations()` delegation to entity_store (bridge)
- [ ] Name→ID lookup for NPCs/locations in entity_store

### Low Priority / Future

- [ ] Scene runtime replacement (scene_design.md)
- [ ] Context builders read from entity_store directly
- [ ] Effects engine (per_tick execution, duration expiry)
- [ ] Script execution engine
- [ ] Retire old game/world/*.json files

---

## Part VI — Non-Goals (Explicitly Out of Scope)

- Character agent tools (`observe`, `recall`, `declare_intent`, etc.) are not
  part of this migration. Character agent architecture is a separate concern.
- Multi-agent scheduling (the orchestrator's tick loop) is partially implemented.
  Full invoke_self scheduling is in invocation_context_design.md.
- Script execution engine (running attached Python scripts) is deferred.
  Scripts are stored but not executed.
- Full-text semantic search for `facts_search` may be stubbed (return empty
  results) in the initial implementation.

---

## Part VII — World Seed Design

### Concept

The World Seed is a single structured JSON document generated by the WM in one
LLM call (forced tool, same pattern as the current `world_setting_result`).
It contains everything the orchestrator needs to bootstrap the world **at the
meta level** — before any entity is created.

The World Seed does NOT create locations, NPCs, factions, or any entities.
Entity creation is always done by the appropriate tool (`create_location`,
`create_npc`, `set_entity(type="faction")`) in the agent's ReAct loop.
The seed only describes *what* world the agents are about to build in.

### Why a single document

The current flow already works this way: one LLM call, one forced tool, three
fields. The expansion keeps the same architecture — the seed grows to include
meta-parameters, but the call pattern (LLM → JSON → orchestrator parses →
files written) does not change.

A single document means:
- One retry boundary (not N tool calls in a ReAct loop).
- LLM works in its strong area (generating cohesive description).
- Orchestrator works in its strong area (parsing, validation, file I/O).
- The seed is reviewable as a whole before any state change happens.

### What the World Seed contains

**Section 1 — World Meta (required, unchanged from current)**
- `world_essence` — prose description of the world's nature.
- `gurps_calibration` — power tiers, tech_mana_baseline, supernatural_spectrum,
  optional social_fabric and cosmic_context.
- `initial_world_time` — calendar date in `Y0000-01-01 00:00` format.

These three fields are validated with the same strict rules as now. If any
fail, the seed is rejected and the LLM retries (up to 3 attempts).

**Section 2 — Player Reference (optional)**
- `location` — the name of a location where players should start.
- `notes` — narrative context (e.g. "players begin at the tavern after a
  storm").

This is a **hint, not a command**. It does not create the location. It is
stored in `world_setting.json` as a reference for the GM, who creates the
actual location via `create_location` and places players there.

### What the World Seed does NOT do

| Not its job | Who does it | Why |
|---|---|---|
| Create locations | `create_location` tool (GM/WM ReAct loop) | Only `create_location` knows coordinate system, parent hierarchy, sublocation linkage |
| Create NPCs | `create_npc` tool (GM/WM ReAct loop) | NPCs must be anchored to existing locations, have GURPS stats, tags, position |
| Create factions | `set_entity(type="faction")` (WM ReAct loop) | Factions reference settlements, entities, relationships — none exist at seed time |
| Place player characters | `set_entity(type="character", position=...)` (GM/initialization) | Characters are entities with full GURPS blocks, inventory, relationships |
| Define world rules | `declare_world_state` or `set_entity(type="rule")` | Rules reference entity_ids and stats that don't exist until entities are created |
| Run simulation | Not applicable | This is a narrative-only system |

### How the orchestrator processes the seed

The current `_maybe_create_world_setting()` method in `console_app.py` already
handles the flow. Its expansion:

1. Call `WM.create_world_setting(characters, plot)` — forced tool, returns seed
2. Validate required fields (world_essence, gurps_calibration, initial_world_time)
3. Save `world_setting.json` (as now)
4. If `player_reference` present — save to `world_setting.json` for GM visibility
5. Set `_world_setting_ready = True` — first turn unblocked

Steps 4 is optional. If the seed contains only the three required fields,
the flow is identical to the current implementation.

### Relationship to the old `initialize_world()` ReAct loop

The WM's `initialize_world()` method (which runs the ReAct loop with tools)
remains. Its job changes:

- **Before seed:** the loop had to establish everything — world orientation,
  player start, first locations, first NPCs, factions.
- **After seed:** the loop receives the seed's meta-context. It knows the
  world's essence, its parameters, and where players should start. It uses
  `create_location`, `create_npc`, `set_entity(type="faction")` to create
  the actual entities, guided by the seed's descriptions.

The ReAct loop is shorter and more focused because the seed already answered
the big-picture questions. The loop now answers: "given this world, what
specific locations and NPCs should exist right now?"

