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
| Tick engines mutating cell grids / need vectors | Simple automation: effects and subscriptions |
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
  │       observe/recall → SM.resolve_player_question
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
  └─ drain cold path: drain_cold_path()        cycle_runner.py
        who is due: engine/agent_schedule.py
          scheduled — the wake-ups the agents queued themselves
                      (game/entity_store/scheduler_queue.json)
          interval  — an agent that queued none and has gone longer than
                      LLM_WORLD_AGENT_CALL_INTERVAL_SECONDS since its last call
        wm → WM.call_wm        gm → GM.call_gm
        each invocation records when it ran (world time and tick) in
        game/{gm,wm}_history_meta.json, which is what the interval is measured
        from; a wake-up that could not be served goes back in the queue
```

**Hot path today:** Character agents + Scene Manager + SegmentScheduler.
**Cold path (target):** WM and GM wake from subscriptions, corrections, the change
outlines their inferiors report, and `invoke_self` — not from every turn.

Background workers (TickScheduler): paragraph/arc summaries, character reflection,
diary, relationship review. These are orthogonal to scene resolution.

### As implemented

Verified against the code. The spine is `cycle_runner.run_cycle()`; sessions and
finalization belong to `engine/orchestrator.py`.

```
run_cycle()                                    cycle_runner.py:485
  ensure_world_setting()                       engine/orchestrator.py:128
  _run_startup_agents() — once per world        cycle_runner.py:411
      gated by game/world/initialized.json, written when both agents get through
      WM.call_wm("initialize_world") → GM.call_gm(notice="", invoked_as="you are
      invoked to initialise the world")
  begin_session()                              engine/orchestrator.py:648
  bootstrap_scene() — the world's first session only engine/orchestrator.py:309
      gated by game/world/awareness.json, written when the opening is given: a
      restarted process is a new process, not a new world. Every session after
      the first is informed by other channels — what the SM states in a resolve
      cycle, question answering, and each character's own scene context. The
      per-character conditions that once made this partial (the character changed
      place, or was inactive for long) were dropped on purpose: awareness is given
      once for the world.
      SM.resolve_active_groups() gives the places the players stand in, and the
      close is held to them; SM.run_bootstrap() appends its marker and no words
      about the scene — the scene is the session's own opening — and the scene
      manager enters, for each character standing in the scene, what that
      character perceives of it (add_perception) — the opening he is given;
      Orchestrator.deliver_awareness() hands each character the knowledge he was
      given, in game/players/{name}/messages.json, marked `[account]`: what stands
      in a character's file as something he was told is readable as such, and not
      as a record he wrote himself
  players without an active intent:
      _players_without_active_intent → _invoke_players → _run_single_player →
      player.agent.run_player_agent (observe / recall / declare_intent /
      cancel_subscription / commit_decision)
  finalize_cycle() → _do_finalize_cycle()       engine/orchestrator.py:359
      SM.run_resolve_cycle(payload) — reviews intents by the id each carries,
      authors NPC intents, cancels or cuts short player intents (a cut calls its
      owner back with the reason). The phase is told the moment it stands at, the
      advance the cycle will make if the review changes nothing, and what that
      advance is estimated by — the nearest change in the pool it is given
      (`nearest_change`), with the attempt standing at that moment marked where
      it is listed
      ≤ 10 SegmentScheduler rounds (scheduler/timeline.py): structural trigger
      matching → SM.perception_check → carry-over; a re-invoked character runs
      _run_single_player again with the trigger notes as its notice. A telling is
      not one of those doors: what the scene manager gave a character is read at
      his next turn, and an attempt cut short is the only hand that calls a
      character back inside the cycle he is in
      each round reaches the nearest moment the scene changes: what ends in that
      segment is resolved, what runs longer is carried over with the span it
      declared, and a pool that will not change by itself gets the structural
      pause
      resolved segments are appended to SM's hot buffer
      the timeline is persisted either way, and the world clock moves to the
      scheduler cursor with it — an open session does not hold the clock back
  scheduler.tick()                              scheduler/core.py
```

Session end: `Orchestrator.end_session()` → `SM.run_session_summary()` →
`sm.end_session()` → `player.agent.end_session()` (messages.json written once per
character) → session diagnostics report.

Players are grouped by the location recorded on the entity, not by a `Scene`
object. `turn_runner.py`, `scene/runtime.py`, `describe_scene`,
`run_turn_narration`, `end_scene` and `publish_narrative_block` are not in the
tree; §3 above describes the intended design, not the current one.

### What is sent, and where it is written

The prompt a model is given is assembled inside the client — after every
conversion, role mapping and parameter merge — so no hand-written log line can
show it whole. The capture therefore sits on the transport: one shared `httpx`
client (`api_wire_capture.py`) whose request hook writes the body verbatim.

* `logs/last_prompts/{scope}__{label}.txt` — the request rendered to be read:
  each message with its role, content, tool calls and their arguments, its
  tool-call id and the reasoning being sent back; then every tool with its
  description and parameters; then every other field of the body. Nothing is
  summarised or dropped, so the file can answer what the model was given.
* `LLM_WORLD_LOG_WIRE=0` turns it off, `LLM_WORLD_LOG_WIRE_JSONL=1` also appends
  each body to `logs/api_wire.jsonl`, `LLM_WORLD_LOG_WIRE_RESPONSES=1` appends a
  response body for non-streaming calls (a streamed body belongs to the client
  reading it and is never taken).

The invocation a request belongs to is not visible in the body, so the logging
callbacks record their scope and label (`set_current_scope`) and the hook reads
them back in the same thread. The scope/label names are the ones the phases
already use: `scene_manager__resolve_cycle`, `character__{name}`,
`game_master__call_gm`, `world_manager__call_wm`, `reflection__{name}`, …

---

## 4. Core data model

### Entities

Everything durable and shared is an entity (location, NPC, faction, template,
object, law, …). Type is a free string; the engine does not hard-code a taxonomy.

Storage is unified behind `EntityCore` / `CompositeBackend`:

| Backend | Backing store | Holds |
|---|---|---|
| `JsonFileBackend` | `game/entity_store/entities.json` | every mechanical entity: NPCs, locations, templates, factions, player mechanical data |
| `CompositeBackend` | — | thin pass-through to `JsonFileBackend` |

`CharacterFileBackend` and `LocationFileBackend` were removed. Player-specific
files (description.json, memory.json, messages.json) still live
under `game/players/{name}/`; the entity store holds only mechanical data.

**Decision:** PCs are addressable through the same tool API as other entities.
Narrative agents should not invent mechanical fields into prose `facts`.

### Entity fields (canonical)

| Field | Role |
|---|---|
| `id`, `type`, `base_type` | Identity + optional template inheritance |
| `facts` | Prose world knowledge (no HP/stats in prose) |
| `gurps`, `stats` | Mechanical numbers |
| `state`, `skills`, `equipment` | Runtime / inventory (extended schema) |
| `tags` | Queryable labels (`faction:…`, `status:…`) |
| `position` | `inside` / `near` / `coords` or hierarchical parent for locations |
| `effects`, `movement` | Automation attachments |

### Event log

Append-only causal log (`create_event` / `query_events`). Indexed by space and time
for queries. **Not** a simulation substrate that mutates numeric world layers.

### Timeline (scene)

`game/world/timeline.json`: active intents (including persistent subscriptions),
resolved history, cursor. Owned by the segment scheduler — see
[`scene_design.md`](scene_design.md).

### Story

Hierarchical narrative memory: turns → paragraphs → arcs in `story.json`.
Summaries are indexes; tools retrieve detail when needed.

### What stands in an agent's own memory

Every agent's durable memory is a list of records, each wearing the marker of its
kind. `history_kinds.py` is the catalogue — the single place a kind's marker,
durability and repeatability are declared. For a character, the markers that
matter are `[session_begin]` (the situation he opened with), `[account]` (what the
world told him, delivered by the ledger), `[player_answer]` (a question he asked and
the answer he was given), the
sheet records `[entity:{name}/{block}@v:{hash}]` (a block written when its content
changed; nothing is withdrawn), his turn context `Location: …`,
and `[intent]` (what he committed to). A record carries one marker and no other
labels: a reader never has to guess whose prose he is looking at.

A turn's own records — the situation he was given, what he asked and was told,
what he decided — are entered where the turn ends (`player.agent.record_turn`),
not at the session's close: the branch is a queue, and a record entered at the
close would stand after everything the world delivered in the meantime, leaving
the session reading as if he had been told what happened before he was told where
he stood. The file itself is still written once, when the session closes.

What the world told him is read at his next turn. Within a session his turn is the
conversation it runs in, and a record the delivery entered in his memory is not
part of that conversation — so the turn is handed what he was given since his last
one, and that is the only way it reaches him where he is. His first turn of the
session is the exception: there his memory itself is what is read, and the record
already stands in it, so it is not said to him a second time.

### Knowledge, and who knows it

What a session produces is **knowledge**: something happened, something was
declared, someone was asked and answered, something was perceived. The scene
manager's ledger (`scene_manager/awareness.py`) enters each piece once under an id
of its own — `knowledge1`, `knowledge2`, …; the id is the term itself plus the
number of the piece, never a bare letter, so the word the framework uses and the
word the agent reads are the same word. The ledger answers one question about a
piece: *who knows it*.

The id stands beside the thing it names, wherever the scene manager reads that
thing:

* an **attempt** is a piece from the moment it stands in the session. Its id is
  listed beside it in the resolve phase that reviews it (`Orchestrator._enter_attempts`
  enters the pool before the review is handed it), and it is the same piece the act
  is entered as when the attempt resolves (`_enter_resolved_actions`) — one act,
  one piece, one id from the declaration to the account of the session. Who was
  there to notice it is recorded with the resolution; **who is told of it is the
  scene manager's own act**, and nothing else puts knowledge into a character's
  memory — an act that reached a character by itself would stand in his memory as
  something he noticed, with nobody having decided that he did.
  An attempt a player declares and an attempt the scene manager declares for an NPC
  read the same way (`Ledger.attempt_piece`), which is what makes the id in a
  tool's result the id of the attempt it made.
* a **change** — a call that alters the world rather than making an attempt — is
  known as the record of the call, and its id is appended to that call's result.
* a **perception** is entered with the id it takes (`add_perception` answers with
  `[knowledge] {name} knowledge ids of evidence he knows: [{id}]`, the id as the
  number the piece is known by), and what a character was told of a piece he
  already holds is given by naming its id.

An id the scene manager has not read beside its piece is an id it cannot use, so
nothing is entered without being stated where the agent acts on it. `add_perception`
gives a character what he perceives, and the pieces the session holds that he is
given, named by their ids. Knowledge is only ever added — a character who was told
something keeps it. The framework hands over what a character was given, once, by
writing it into his own memory; the id stays in the session and never reaches a
character's file.

---

## 5. Tool contract

Principles:

1. One tool = one atomic write or read. Complexity lives in scripts / agent reasoning.
2. Same operations appear across agents at different **scopes** (access matrix below).
3. `invoke_self` / `close_invocation` are the scheduling primitives.
4. Tool docstrings are the whole documentation: every parameter is described in
   the docstring itself, never in a side file.

### Access matrix

| Tool | Char | SM | GM | WM |
|---|---|---|---|---|
| `observe`, `recall`, `declare_intent`, `cancel_subscription`, `commit_decision` | ✓ | | | |
| `character_reflection`, `relationship_update`, `relationship_read`, `write_diary_entry` | ✓ | | | |
| `get_entity_facts`, `get_entity_state`, `query_events` | | ✓ | ✓ | ✓ |
| `get_entity`, `query_entities` | | | ✓ | ✓ |
| `get_location_info` | | ✓ | | |
| `create_event` | | ✓ | ✓ | ✓ |
| `adjust_entity` (HP, conditions, items, …) | | ✓ | ✓ | ✓ |
| `set_effect`, `remove_effect` | | ✓ | ✓ | ✓ |
| `set_entity`, `set_entity_facts`, `add_tags`, `remove_tags` | | | ✓ | ✓ |
| `escalate_to_gm` | | ✓ | | |
| `declare_intent`, `modify_intent`, `manage_intent`, `add_perception`, `answer_player_question`, `write_session_narrative` | | ✓ | | |
| `notify_wm` | | | ✓ | |
| `invoke_self` | | | ✓ | ✓ |
| `close_invocation` | | ✓ | ✓ | ✓ |
| `invoke_self_gm`, `answer_lore_question` | | | ✓ | |
| `delete_entity`, `write_correction` | | | | ✓ |

The bound sets are the source of truth: `ToolRegistry.for_wm()`,
`ToolRegistry.for_gm()` (plus the two GM-only tools defined in
`gm/game_master.py`) and `SceneManager._get_sm_tools()`. `ToolRegistry.for_sm()`
is defined but not used. `perception_result` is built inside
`SceneManager.perception_check` and bound for that one call only.

Character tools never touch the entity store. SM may adjust in-scene state and
record events; world creation belongs to GM/WM.

### Effects

An effect is a stat modifier with a period and a duration (`set_effect`,
`remove_effect`): a change the entity carries until it is taken off, landing
every period on the world clock. The engine that lands it is run by the cycle
(`advance_world_processes`), so an effect stated is an effect that happens.

Attached behaviours (scripts) are not part of the game for now: a behaviour that
has to decide, branch and act is a capability the surface does not yet state in
the vocabulary the rest of it uses, and half a promise is worse than none. What
is not here is not offered to any agent.

### Inter-agent messaging (target)

| Channel | Writer | Consumer |
|---|---|---|
| `escalate_to_gm` | SM | One invoker, installed by the orchestrator: `SM.ask_the_gm` → `GM.call_gm`, in every phase |
| `notify_wm` | GM | Orchestrator schedules / wakes WM |
| `write_correction` | WM | Next GM invocation injects correction |
| `escalate_to_sm` | GM | Next SM invocation injects instruction |
| `invoke_self` | any | Orchestrator enqueues that **same** agent |
| `[{sm,gm,wm}_changes]` | every agent with write tools | The authority above: the peer's memory, one record per invocation |

JSONL files alone are not a finished design — the orchestrator must consume them.

---

## 6. Scene resolution

Scene play uses a **shared timeline** of intents, not sequential "A acts then B
acts." Characters declare time-scoped intents (with optional triggers /
prepared reactions / timeouts). The reach of a cycle is the moment the scene
next changes — the nearest declared start or end; a scene that will not change
by itself gets the structural pause. The
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
3. **Injected** — scene description, active subscriptions, corrections, change
   outlines reported by inferiors.
4. **Retrieved on demand** — tool docs, entity facts, event queries.

Termination: prefer `close_invocation` (the orchestrator then serves what the
agent scheduled, and the agent's change outline reaches its authority) over
sentinel-only tools. `invoke_self` must schedule the **calling** agent, not
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
- `close_invocation` terminates ReAct but does not serve the queue it
  schedules from
- Inter-agent tools (`notify_wm`, `write_correction`) mostly
  append JSONL that nothing consumes
- Narration `type == "correction"` prints and does not call GM
- Scheduled SM path calls missing `run_sm_scene`
- Parallel write paths: `World.create_location` / `create_npc` vs `set_entity`

### Removed / obsolete

- Entire `simulation/` package (L0–L3 layers) — deleted from the tree
- `docs/wm_tools.md` and the full-simulation design docs (archived by this rewrite)
- Attached behaviours (scripts): `attach_script`, `detach_script`, the engine that
  ran them and the storage that held them — the concept is parked, not replaced
  (see "Effects" above)

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

1. **Orchestrator wiring** — consume corrections / escalations; fix
   `invoke_self` agent identity; implement or remove `run_sm_scene`; call GM on
   narration corrections.
2. **Single entity write path** — retire duplicate `World.create_*` once tools
   cover preset load and GM/WM creation.
3. **Wake WM/GM from subscriptions** — make cold-path agents real participants,
   not dead letter boxes.
4. **Run effect/script ticks** — after scene finalize or on a TickScheduler job;
   finish spatial script triggers or drop them from the contract.
5. **`close_invocation` + change outline** — proper termination, with each
   agent's changes in the world reported to the authority above it.
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
