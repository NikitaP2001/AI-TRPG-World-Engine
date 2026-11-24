# Scene Design — Gap Analysis & Implementation Plan

*What `scene_design.md` sketches, what exists today, and what it takes to close
the gap.*

---

## Status

`scene_design.md` is an early-direction sketch, not a full spec — it does not
address all corner cases. This document is a companion review: it reads that
sketch, checks it against the current codebase, fills in the gaps the sketch
leaves implicit, and lays out a phased path from the current round-based scene
runtime to a real segment scheduler. Broader agent/tool architecture lives in
[`architecture.md`](architecture.md); this document is the scene-specific detail.

**Update — a first implementation pass has landed.** Phases 1-8 below are
implemented (with documented simplifications); phase 9's corner cases were
decided pragmatically rather than debated further. See §6 for what actually
shipped, what's simplified vs. the sketch, and what's still open.

---

## §1 What `scene_design.md` Specifies (recap)

- **§1 Shared timeline** — one continuous timeline, no per-character time
  offsets; asynchrony expressed only via interval scope.
- **§2 Awareness** — base awareness from `location_inside` + a location
  connectivity graph (`connected_to`, `connections[].blocks_line_of_sight`);
  extended awareness via registered sensor links. The manager's view is
  unfiltered.
- **§3 Intent/trigger schema** — one schema for every declaration (action,
  wait, standing reaction, manager-authored NPC/world action):
  `actor/action_type/target/area/interval/is_hostile/visibility/
  detection_difficulty/trigger_condition/trigger_priority/on_trigger/
  prepared_reaction/on_timeout`. Includes persistent subscriptions
  (`interval: [t, null]`) and cancellation via `cancel_subscription_ids`.
- **§4 Segment resolution** — the scheduler algorithm: collect active intents
  → structural trigger join → perception gate (only for `private`) → apply in
  `trigger_priority` order → timeout handling → record to resolved history.
  Only the perception gate and schema-violation fallback require the scene
  manager; everything else is structural.
- **§5 Planning horizon `T`** — the manager sets a look-ahead point per cycle;
  agents declare within `[t_now, T]`; re-invocation happens only on a trigger
  match, never solely because `T` was reached.
- **§6 Resolved history / narrative blocks** — scheduler state is independent
  of "publishing" prose to players.
- **§7-§8 Manager as participant** — same schema as characters; only pulled in
  for two narrow structural queries (perception gate, schema-violation
  fallback), not open narrative judgment.
- **§9 Per-invocation input contracts** for character agents and the manager.
- **§10 Future work** — mechanical perception replacement, `true_intent` for
  concealed actions.

---

## §2 Current Implementation — Brief Project Review

The project follows the narrative-only agent architecture in
[`architecture.md`](architecture.md). Three agent tiers — WM (world manager), GM
(game master), SM (scene manager) — plus character agents, orchestrated by
`console_app.py` / `turn_runner.py`. Storage is consolidating into an entity
registry (`world_manager/entity_store.py`, `_composite_backend.py`) with an
event log; effects/scripts are stored but not yet executed.

Scene execution today is a **round-based loop**, not a segment scheduler:

- `scene/runtime.py` (`Scene`) — flat state: `characters: {name: {acted,
  last_decision}}`, `initiative_order`, `declared_intents` (a plain list),
  `npcs`. No timeline, no subscriptions, no per-character time offsets.
- `turn_runner.py` — runs each character once in initiative order, forwarding
  a crude "public intents notice" heuristic (`_build_public_intents_notice`)
  as the only form of awareness.
- `console_app.py::_finalize_turn_if_ready` — a bounded `while round_num <
  max_rounds` (max 5) loop where the SM's `resolve_turn` LLM call decides
  `proceed_to_narration`, `reinvoke` (full character re-run), or `correction`
  (escalate to GM). This is the closest current analog to §4 step 4, but it's
  a per-round LLM judgment over the whole intent set, not a structural
  resolution.
- `scene_manager/core.py::resolve_turn` / `run_turn_narration` — the SM makes
  free-form tool-choice decisions across all declared intents at once; there
  is no per-segment structural join.

### Concrete breakage found

- `console_app.py` (`_finalize_turn_if_ready`) imports `find_boundaries`,
  `build_segments`, `format_segments_for_sm`, `parse_duration_seconds` from
  `scheduler.segment_scheduler` — **this module does not exist on disk**. The
  call is wrapped in a bare `except Exception`, so it silently fails on every
  turn and falls back to `timeline_segments=[]` / `timeline_text=""`. The
  `horizon_sec = 900` constant next to it is unreachable dead code.
- The location connectivity graph the design assumes (`connected_to`,
  `blocks_line_of_sight`) does not exist on the current `world/` location
  model. A related but different `connections[]` field is sketched for the
  entity-based location model in [`architecture.md`](architecture.md) §4.

### Schema fields present vs. missing

`declare_intent` (`character/agent.py`) captures `action_type`, `target`,
`description`, `interval` (free-text string, not `[t_start, t_end]`),
`visibility`, `trigger_condition`, `trigger_priority`, `on_trigger`,
`prepared_reaction`. It does **not** capture `is_hostile`, `area`,
`detection_difficulty`, or `on_timeout`, and there is no
`cancel_subscription_ids` intake or `[t, null]` persistent-subscription
convention. `visibility` is stored but never gates anything — there is no
perception-gate check against `detection_difficulty` anywhere in the code.

---

## §3 Target Picture — Filling the Sketch's Gaps

Components needed to realize the design, including decisions the sketch
leaves implicit:

**a. A real timeline / segment engine** (e.g. `scheduler/timeline.py`)
Intents stored with numeric `t_start`/`t_end` in seconds (convertible via the
existing `world/time.py` `WorldTime`/`WorldDuration`), not opaque strings. Pure
functions for boundary detection and structural matching. The segment loop
implements §4 steps 1, 2, 4, 5, 6 as deterministic code — only step 3
(perception gate on `private` intents) and the schema-violation fallback (§8)
call into the SM/LLM, exactly as §4's closing line and §8 intend.

**b. An awareness component**
Requires the location connectivity graph the doc assumes. For v1: a boolean
reachability check (aware / not aware) gated by `blocks_line_of_sight`,
deferring graded/partial perception to a v2 — the doc itself defers the harder
mechanical version to §10 future work.

**c. Intent/subscription persistence separate from scene bookkeeping**
Intents currently live inside `scene.json`'s flat `declared_intents` list,
wiped by `Scene.end_with_gm_output()` → `clear_scene()`. §3.2 persistent
subscriptions and §6 resolved history both require state that survives
scene/narrative-block boundaries — this needs its own store, keyed by timeline
position, independent of the "acted: bool per character" turn-taking state.

**d. Manager-as-participant + horizon `T`**
The SM needs to author its own `actor: "manager"` intents (and persistent
subscriptions) that enter the same scheduler *before* `T` is set for a cycle,
per §5/§7. Today NPC behavior is folded entirely into end-of-turn narration
prose — NPCs have no independent intents at all.

**e. Corner cases to lock down before implementation**
- `interval` serialization: relative seconds from scene epoch, or absolute
  `WorldTime` strings — the doc doesn't say which.
- `area` shape for `target == "area"` — needs at least a minimal schema
  (anchor + radius, or named zone) to be checkable in §4 step 2.
- Subscription re-fire bounds: the doc says persistent subscriptions "remain
  active after firing" but doesn't bound re-fire frequency on broad
  conditions — needs a one-fire-per-segment rule at minimum.
- The "capped in count" language in §9.1 needs an actual number, tied to
  context budget.

---

## §4 Gap Matrix

| Design concept (§) | Current implementation | Status |
|---|---|---|
| Shared timeline, numeric intervals (§1) | `interval` is a free-text string on `declare_intent`; no shared timeline structure | Not implemented |
| Awareness via connectivity graph (§2) | `_build_public_intents_notice()` (`turn_runner.py`) — flat heuristic: any `public` intent of certain `action_type`s is shown to everyone in the scene, regardless of location/distance/line-of-sight | Ad-hoc stand-in, not the design's model |
| Extended awareness / sensor links (§2) | None found | Not implemented |
| Unified intent schema (§3) | `declare_intent` captures most narrative fields but not `is_hostile`, `area`, `detection_difficulty`, `on_timeout` | Partially implemented |
| §3.1 action_type constraints / schema-violation fallback | No validation anywhere | Not implemented |
| §3.2 Persistent subscriptions + cancellation | No `[t, null]` convention, no `cancel_subscription_ids` path | Not implemented |
| §3.3 Visibility / perception gate | `visibility` stored, used only by the public-intents heuristic; `private` intents have no gating, no `detection_difficulty` check | Not implemented (field present, gate absent) |
| §3.4 Trigger priority | Field captured, never used to order resolution | Not implemented |
| §4 Segment resolution | `scheduler/segment_scheduler.py` is imported (`console_app.py`) but **does not exist**; failure silently swallowed | Broken / dead code |
| §5 Planning horizon `T` | Hardcoded `horizon_sec = 900` inside the same dead code path | Not implemented (unreachable) |
| §6 Resolved history independent of narrative blocks | No resolved-history structure; intents wiped on `clear_scene()` | Not implemented |
| §7 Manager as participant (NPC/world intents) | NPCs have no intents; SM only narrates at turn end | Not implemented |
| §8 SM resolution calls as narrow structured queries | SM's real role (`resolve_turn`/`run_turn_narration`) is a broad free-form LLM judgment over the entire intent list each round | Inverted from design |
| §9 Per-invocation input contracts | Informal equivalents exist (scene description, NPC list, ad-hoc notice); no subscription list, no `T`, no awareness-filtered history delta | Partially implemented, informally |

The intent *vocabulary* of §3 largely exists as text fields threaded through
`scene_manager/core.py`'s message builders; the structural *mechanism* that's
supposed to interpret it (§4) does not.

---

## §5 Phased Implementation Plan

1. **Numeric `interval`.** Convert to `[t_start_sec, t_end_sec|None]`
   (relative to scene start, via `world/time.py` conversions). Everything
   below depends on this.
2. **Build the segment scheduler.** Implement the module `console_app.py`
   already assumes exists: boundary detection, structural join
   (`actor/action_type/target` vs. `trigger_condition`), priority-ordered
   application, timeout handling — all as deterministic code (§4 steps
   1,2,4,5,6). Remove the dead try/except once real.
3. **Separate scheduler state from scene lifecycle.** Persist
   intents/subscriptions in their own store, independent of `scene.json`,
   surviving scene/narrative-block boundaries.
4. **Complete the schema.** Add `is_hostile`, `area`, `detection_difficulty`,
   `on_timeout` to `declare_intent`; add `cancel_subscription_ids` intake and
   the `[t, null]` persistent-subscription convention.
5. **Implement the perception gate as a narrow SM call.** Replace the public-
   intents heuristic with: structural match → if `private`, one narrow
   SM/mechanical call vs. `detection_difficulty`; if `public`, pass
   automatically. Requires a minimal location connectivity model
   (`connected_to` + `blocks_line_of_sight`), which doesn't exist yet.
6. **Give NPCs/manager first-class intents.** Let the SM author
   `actor: "manager"` intents and persistent subscriptions that enter the same
   scheduler, instead of folding NPC behavior into narration prose.
7. **Introduce horizon `T`.** Replace the fixed `max_rounds = 5` loop in
   `_finalize_turn_if_ready` with trigger-driven re-invocation per §4/§5.
8. **Shrink the SM's per-round judgment call.** Move most of `resolve_turn`'s
   responsibility into the structural scheduler, leaving the LLM only the
   perception gate and schema-violation fallback (§8). This is the largest
   behavioral shift: today the SM *is* the scheduler; the design wants the
   scheduler to be code that occasionally calls the SM as an oracle.
9. **Lock the open corner cases** (§3e above — `area` shape, subscription
   re-fire bounds, per-invocation caps, `interval` serialization) before
   freezing the schema.

Given the scope — new scheduler module, awareness/connectivity model, schema
and persistence changes, SM role change — this is a multi-phase rework, not an
incremental patch. Track remaining agent/orchestrator gaps in
[`architecture.md`](architecture.md) §8–§9.

---

## §6 What Was Actually Implemented

A first pass implementing phases 1-8 has landed. This section is the ground
truth for what exists now — read it alongside §4's gap matrix, which describes
the *pre-implementation* state.

### New/changed files

- **`scheduler/timeline.py`** (new) — the segment-resolution engine: `Intent`,
  `SegmentScheduler` (§4 steps 1,2,4,5,6 as deterministic code), `validate_schema`
  (§3.1 structural checks), `instantiate_sub_intent` (builds a new `Intent` from
  a `prepared_reaction`/`on_timeout` spec), `format_resolved_history_for_sm`.
  Only `perception_gate` (§3.3) is a caller-supplied hook into the SM — every
  other step is pure/testable code. Covered by `tests/test_timeline.py`
  (`python3 -m unittest tests.test_timeline`).
- **`world/state.py`** — added `WorldPaths.timeline_json` and
  `World.get_timeline()`/`set_timeline()`/`clear_timeline()`. This is the
  persistence phase 3 called for: intents/subscriptions and resolved history
  now live in `game/world/timeline.json`, independent of `scene.json`, and
  survive `Scene.end_with_gm_output()` → `clear_scene()`.
- **`character/agent.py`** — `declare_intent` now takes `t_start`/`duration`
  (parsed to numeric seconds anchored on the invocation's `world_time`,
  instead of a free-text `interval`), plus `is_hostile`, `area`,
  `detection_difficulty`, `on_timeout`. Added a `cancel_subscription` tool
  (§3.2) and surfaced the character's own active subscriptions in its context
  (§9.1, via `turn_runner._format_active_subscriptions`).
- **`scene_manager/core.py`** — added `SceneManager.perception_check()` (§3.3/§8:
  one narrow forced-tool call per private-visibility structural match — not
  the broad per-round judgment call `resolve_turn` used to make) and
  `SceneManager.plan_npc_intents()` (§7: lets the SM author `actor: <npc>`
  intents that enter the same scheduler as character intents, instead of
  folding NPC behavior only into end-of-turn narration prose).
- **`console_app.py`** (`_finalize_turn_if_ready`) — replaced the dead
  `scheduler.segment_scheduler` import and the `max_rounds`-bounded
  `resolve_turn` LLM loop with: load carried-over intents from
  `World.get_timeline()` → merge with this cycle's declared + NPC-planned
  intents → run `SegmentScheduler` → re-invoke only the characters the
  scheduler actually flagged via `reinvoke` (trigger-driven, §4 step 4 /
  §5 — not "keep looping until the LLM says stop or 5 rounds pass") → persist
  carry-over + resolved history back to the timeline store → narrate from the
  resolved history via `format_resolved_history_for_sm`.
- **`turn_runner.py`** — `_run_single_character` now accepts
  `world_time_override` so a re-invoked character's new intents are anchored
  at the scheduler's current resolution point, not the scene's original start
  time (§4 step 4: "re-invoked ... from this segment's start time forward").
  Added `_format_active_subscriptions` / `_apply_cancelled_subscriptions`.
- **`tests/test_timeline.py`** (new) — unit tests for the scheduler: plain
  action recording, prepared-reaction firing, trigger-driven re-invocation,
  persistent-subscription carry-over, timeout handling, and perception-gate
  pass/fail. Runs without any of the project's LLM dependencies installed.

### Deliberate simplifications (documented in code, not silently dropped)

- **Awareness (§2) is a stub.** `_awareness_fn` in `console_app.py` returns
  `True` unconditionally — the location connectivity graph (`connected_to`,
  `blocks_line_of_sight`) still doesn't exist anywhere in the world model, so
  "aware" currently just means "present in this single-location scene," same
  granularity the old code had. `perceivers()` in `scheduler/timeline.py`
  takes an `awareness_fn` precisely so this can be swapped later without
  touching the scheduler.
- **Schema-violation semantic fallback (§3.1/§8) is not wired.** Only the
  structural, field-level checks in `validate_schema()` run (e.g. cosmetic +
  `is_hostile`); the "description implies effects outside the declared
  fields" semantic check is left as future work, same as the doc's own framing
  in §10.
- **`prepared_reaction`/`on_timeout` are free text**, not the full intent
  sub-schema — `instantiate_sub_intent()` defaults the resulting reaction to
  `action_type="speech", target="all"` when the character only supplied a
  description. Keeps the character-facing tool signature simple; revisit if
  reactions need richer typed sub-intents.
- **Horizon `T` is a flat default cap** (`now + 900s`), not adjusted for
  manager-authored intents' own end times or the nearest likely subscription
  match, as §5 allows. The hook point (`horizon = cursor + DEFAULT_HORIZON_SECONDS`
  in `console_app.py`) is isolated so a smarter calculation can replace it later.
- **NPC re-invocation is not implemented.** If a manager-authored NPC intent's
  trigger fires with `on_trigger: "interrupt_with_new_decision"`, there's no
  NPC agent to re-invoke — those trigger notes are collected
  (`npc_trigger_notes`) and handed to the SM as free-text context for
  narration instead.
- **Per-invocation subscription cap (§9.1 "capped in count")** is fixed at 8
  (`_format_active_subscriptions`'s `MAX_SHOWN`), chosen pragmatically rather
  than derived from a context budget.
- **`resolve_turn`** (the old broad per-round SM judgment call) is left in
  `scene_manager/core.py` but is no longer called by the turn flow — kept in
  case a narrative-escalation path is still wanted later.

### Not attempted in this pass

- `true_intent` / facade divergence for concealed actions (§10) — explicitly
  future work in the original doc.
- A real location connectivity graph and multi-location scenes.
- Deriving horizon `T` from manager intents/subscription trajectories.
