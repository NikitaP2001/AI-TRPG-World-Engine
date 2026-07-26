# Architecture

*Single source of truth for the AI-TRPG World Engine design.*

This document replaces the old layered world-simulation architecture and the
scattered migration / integration / tool-plan docs that grew around it. Scene
mechanics live in [`scene_design.md`](scene_design.md); GUI product requirements
live in [`gui_requirements.md`](gui_requirements.md).

---

## 1. What we are building

A multi-agent TRPG engine where **coherence comes from persistent records and
tool boundaries**, not from a mathematical world simulation.

| Declined (old direction) | Current direction |
|---|---|
| L0–L3 numeric layers (geology, climate, ecology, civilization equations) | Narrative-only world: facts written by agents via tools |
| Tick engines mutating cell grids / need vectors | Simple automation: entity scripts, effects, subscriptions |
| R*-tree spatial join feeding layer fields | Append-only event log + entity store |
| WM as author of simulation parameters | WM as author of world *rules and distant structure* via the same entity tools |

The product problem remains the one in the README: language models invent, forget,
and leak information. The solution is structural isolation (separate agents with
scoped tools) and tool-written state that later invocations must read.

---

## 2. Agent roles

There are four agent kinds. On the architectural level, player characters and NPCs
use the same character-agent structure; the difference is who drives decisions
(human UI vs full autonomy).

```
┌─────────────────────────────────────────────────────────────┐
│ World Manager (WM)                                          │
│  Scope: world-scale rules, templates, distant structure     │
│  Invoked: subscriptions / scheduled self-invoke / startup   │
│  Does not narrate scenes                                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ notify / correction (when wired)
┌───────────────────────────▼─────────────────────────────────┐
│ Game Master (GM)                                            │
│  Scope: persistent places, NPCs, campaign facts             │
│  Invoked: subscriptions / SM escalate / scheduled           │
│  Creates facts once; later reads them                       │
└───────────────────────────┬─────────────────────────────────┘
                            │ escalate (rare)
┌───────────────────────────▼─────────────────────────────────┐
│ Scene Manager (SM)                                          │
│  Scope: active scene — describe, answer perception, narrate │
│  Owns segment scheduler cycle + planning horizon            │
│  Invoked: every turn (hot path)                             │
└───────────────────────────┬─────────────────────────────────┘
                            │ observe / recall answers
┌───────────────────────────▼─────────────────────────────────┐
│ Character agents (one per PC / rich NPC)                    │
│  Scope: own perception, memory, relationships, intents      │
│  Output: declare_intent (+ optional standing subscriptions) │
└─────────────────────────────────────────────────────────────┘
```

### Authority rules

- **Characters** never write world state. They declare intents and update personal
  memory / relationships.
- **SM** narrates what the scheduler resolved, answers what a character can
  perceive, and may adjust in-scene mechanical state. Escalates to GM when the
  answer requires campaign authority SM does not have.
- **GM** creates and updates world entities/facts relevant to play. Does not run
  every turn. Participates when subscriptions fire or SM escalates.
- **WM** authors templates, long-horizon structure, and world-scale movement /
  corrections. Does not participate in active scenes. Plays its own game via
  subscriptions and `invoke_self`.

### Naming note

Code uses `WorldManager` (`world_manager/core.py`). Docs and product language may
say "World Master"; both refer to the same agent. Prefer **World Manager** in new
writing to match the code.

---

## 3. Runtime game loop (target)

This is the intended turn spine. Items marked *gap* exist partially or not at all
in code today — see §8.

```
continue / turn
  │
  ├─ (once) WM.create_world_setting
  ├─ SM: determine / describe scene
  ├─ for each character in scene:
  │     run_character_agent
  │       observe/recall → SM.resolve_character_question
  │         └─ escalate_to_gm → GM.call_gm   (when needed)
  │       declare_intent / cancel_subscription / commit_decision
  │
  ├─ finalize cycle:
  │     SM.plan_npc_intents
  │     SegmentScheduler (shared timeline, triggers, perception gate)
  │     re-invoke characters whose subscriptions fired
  │     SM.run_turn_narration (from resolved history)
  │     SM.end_turn / advance world time
  │
  └─ drain scheduler_queue:   [*gap: incomplete wiring]
        wm → WM.call_wm
        gm → GM.call_gm
        sm → SM scene work
```

**Hot path today:** Character agents + Scene Manager + SegmentScheduler.
**Cold path (target):** WM and GM wake from subscriptions, news, corrections, and
`invoke_self` — not from every turn.

Background workers (TickScheduler): paragraph/arc summaries, character reflection,
diary, relationship review. These are orthogonal to scene resolution.

---

## 4. Core data model

### Entities

Everything durable and shared is an entity (location, NPC, faction, template,
object, law, …). Type is a free string; the engine does not hard-code a taxonomy.

Storage is unified behind `EntityCore` / `CompositeBackend`:

| Backend | Backing store | Holds |
|---|---|---|
| `JsonFileBackend` | `game/entity_store/entities.json` | NPCs, templates, factions, generic entities |
| `CharacterFileBackend` | `game/characters/{name}/` | Player characters (per-file layout) |
| `LocationFileBackend` | `game/world/locations.json` | Locations |

**Decision:** PCs are addressable through the same tool API as other entities
(via the character backend), but keep their existing file layout. Narrative agents
should not invent mechanical fields into prose `facts`.

### Entity fields (canonical)

| Field | Role |
|---|---|
| `id`, `type`, `base_type` | Identity + optional template inheritance |
| `facts` | Prose world knowledge (no HP/stats in prose) |
| `gurps`, `stats` | Mechanical numbers |
| `state`, `skills`, `equipment` | Runtime / inventory (extended schema) |
| `tags` | Queryable labels (`faction:…`, `status:…`) |
| `position` | `inside` / `near` / `coords` or hierarchical parent for locations |
| `scripts`, `effects`, `movement` | Automation attachments |

### Event log

Append-only causal log (`record_event` / `query_events`). Indexed by space and time
for queries. **Not** a simulation substrate that mutates numeric world layers.

### Timeline (scene)

`game/world/timeline.json`: active intents (including persistent subscriptions),
resolved history, cursor, horizon. Owned by the segment scheduler — see
[`scene_design.md`](scene_design.md).

### Story

Hierarchical narrative memory: turns → paragraphs → arcs in `story.json`.
Summaries are indexes; tools retrieve detail when needed.

---

## 5. Tool contract

Principles:

1. One tool = one atomic write or read. Complexity lives in scripts / agent reasoning.
2. Same operations appear across agents at different **scopes** (access matrix below).
3. `invoke_self` / `close_invocation` are the scheduling primitives.
4. Tool docs for large vocabularies are loaded on demand (`read_tool_doc`).

### Access matrix

| Tool | Char | SM | GM | WM |
|---|---|---|---|---|
| `observe`, `recall`, `declare_intent`, `cancel_subscription`, `commit_decision` | ✓ | | | |
| `get_scene`, scene-local resolve tools, `perception_result`, NPC intent tools | | ✓ | | |
| `get_entity_facts`, `get_entity_state`, `query_events` | | ✓ | ✓ | ✓ |
| `record_event` | | ✓ | ✓ | ✓ (+ optional `t_override`) |
| `adjust_entity` (HP, conditions, items, …) | | ✓ | ✓ | ✓ |
| `set_entity`, `set_entity_facts`, tags, effects, scripts | | | ✓ | ✓ |
| `query_entities`, `get_entity` | | | ✓ | ✓ |
| `notify_wm`, `escalate_to_sm` | | | ✓ | |
| `escalate_to_gm` | | ✓ | | |
| `set_movement`, `delete_entity`, `write_correction` | | | | ✓ |
| `invoke_self`, `close_invocation` | | ✓ | ✓ | ✓ |
| `read_tool_doc` | | | ✓ | ✓ |

Character tools never touch the entity store. SM may adjust in-scene state and
record events; world creation belongs to GM/WM.

### Scripts and effects

Scripts are Python attached to entities (`attach_script`), triggered by
`on_tick`, thresholds, enter-range, nearby events, etc. Scripts may call the
owning agent's tools (limited stdlib). Effects are persistent / timed stat
modifiers (`set_effect`).

**Rule:** scripts calculate; agents decide. If narrative judgment is needed, the
script calls `invoke_self` rather than inventing prose.

Until the orchestrator actually runs effect/script ticks, these are storage-only —
see §8.

### Inter-agent messaging (target)

| Channel | Writer | Consumer |
|---|---|---|
| `escalate_to_gm` | SM | Orchestrator calls `GM.call_gm` with the question |
| `notify_wm` | GM | Orchestrator schedules / wakes WM |
| `write_correction` | WM | Next GM invocation injects correction |
| `escalate_to_sm` | GM | Next SM invocation injects instruction |
| `invoke_self` | any | Orchestrator enqueues that **same** agent |
| News / newspaper | `close_invocation` | Summaries delivered on next relevant wake |

JSONL files alone are not a finished design — the orchestrator must consume them.

---

## 6. Scene resolution

Scene play uses a **shared timeline** of intents, not sequential "A acts then B
acts." Characters declare time-scoped intents (with optional triggers /
prepared reactions / timeouts). The Scene Manager sets a planning horizon; the
`SegmentScheduler` resolves segments, awareness, and the perception gate for
private actions. Standing subscriptions persist across cycles until cancelled or
resolved.

Full specification: [`scene_design.md`](scene_design.md).
Implementation status: [`scene_design_implementation_plan.md`](scene_design_implementation_plan.md).

---

## 7. Invocation context (summary)

Agent context is layered:

1. **Pinned** — identity, world setting, hard constraints (always present).
2. **Working memory** — recent messages, trimmed to a history budget.
3. **Injected** — scene description, active subscriptions, news, corrections.
4. **Retrieved on demand** — tool docs, entity facts, event queries.

Termination: prefer `close_invocation` (orchestrator processes news / schedules)
over sentinel-only tools. `invoke_self` must schedule the **calling** agent, not
hardcode WM.

---

## 8. Implementation status (honest)

As of the 2026-07 audit of the working tree:

### Working (live path)

- Character agent loop: observe / recall / declare_intent / cancel_subscription /
  commit_decision
- SM: scene describe, character Q&A, NPC intents, perception gate, turn narration
- SegmentScheduler + `timeline.json` persistence
- WM `create_world_setting` (once at startup)
- Entity backends + event log storage APIs
- TickScheduler jobs: summaries, reflection, diary, relationship review

### Partial / unwired

- `WorldManager.call_wm` / `GameMaster.call_gm` exist but are rarely on the critical path
- Effect engine and script engine: implemented in isolation, **not** called from the game loop
- `invoke_self` hardcodes `agent="wm"` in the registry
- `close_invocation` terminates ReAct but does not process news / schedules
- Inter-agent tools (`notify_wm`, `write_correction`, `escalate_to_sm`) mostly
  append JSONL that nothing consumes
- Narration `type == "correction"` prints and does not call GM
- Scheduled SM path calls missing `run_sm_scene`
- Parallel write paths: `World.create_location` / `create_npc` vs `set_entity`

### Removed / obsolete

- Entire `simulation/` package (L0–L3 layers) — deleted from the tree
- `docs/wm_tools.md` and the full-simulation design docs (archived by this rewrite)

### Doc set after cleanup

| Keep | Role |
|---|---|
| `docs/architecture.md` | This file — product + agent + tool architecture |
| `docs/scene_design.md` | Scene timeline / intent spec |
| `docs/scene_design_implementation_plan.md` | Scene implementation status |
| `docs/gui_requirements.md` | Native GUI product requirements |
| `README.md` | Problem statement + high-level pitch (aligned with this doc) |

---

## 9. Roadmap (priority order)

1. **Orchestrator wiring** — consume news / corrections / escalations; fix
   `invoke_self` agent identity; implement or remove `run_sm_scene`; call GM on
   narration corrections.
2. **Single entity write path** — retire duplicate `World.create_*` once tools
   cover preset load and GM/WM creation.
3. **Wake WM/GM from subscriptions** — make cold-path agents real participants,
   not dead letter boxes.
4. **Run effect/script ticks** — after scene finalize or on a TickScheduler job;
   finish spatial script triggers or drop them from the contract.
5. **`close_invocation` + newspaper** — proper termination with delivered news.
6. **Align SM tool binding** — either use `ToolRegistry.for_sm()` or document
   method-local tools as the SM contract (one source of truth).
7. **Tests** — expand beyond `tests/test_timeline.py` to entity store, scheduler
   queue, and escalate paths.
8. **GUI** — per `gui_requirements.md`, against this architecture (not L-layers).

---

## 10. Non-goals

- Rebuilding a numeric L0–L3 world simulation.
- Making the GM invent distant history at query time instead of reading records.
- Calling WM or GM every turn "just in case."
- Collapsing character / SM / GM / WM into one shared context window.
