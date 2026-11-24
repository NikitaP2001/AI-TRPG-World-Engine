# Item 3: Zero-Progress and Duration-Plausibility Fallbacks

## Confirmed Problem

Two degenerate cases have no handling:

1. **Zero progress**: a resolution pass produces no intent with positive
   duration — nothing advances the shared timeline.
2. **Implausible duration**: a single intent's declared duration is wildly
   inconsistent with its `action_type` and `description` (e.g., "glance
   around the room" declared as `"2h"`).

Both are currently silent no-ops: zero-progress falls through to a
structural-pause cursor jump (`+900s`), and implausible durations are
accepted verbatim.

## Investigation Findings

### Current zero-progress handling

In `_finalize_cycle_logic()` (`console_app.py:480-495`):

```python
if not cycle_resolved_history and not char_reinvokes and not npc_trigger_notes:
    new_cursor = cursor + DEFAULT_HORIZON_SECONDS  # +900s
    ...
    return True  # structural pause
```

This always advances cursor by 900s regardless of how long it's been since
anything happened. Zero consecutive vs 50 consecutive = same jump.

**Cross-reference to Item 2**: after unification, structural pauses do
NOT advance time at all. The zero-progress fallback defined here is the
**only** mechanism that advances world time when no resolution occurs.
This is intentional — structural pauses (no content / buffer not ready)
simply wait; if they repeat enough, the zero-progress counter triggers.

### SM session lifecycle for escalation

The SM session is opened once per cycle at `cycle_runner.py:493`
(`begin_session()`) and closed in the `finally` block at line 576
(`end_session()`). All resolution and narration happens within this single
session via `_session_messages`. The escalation round would be inserted
**after** the SegmentScheduler pass and **before** the publish/narration
decision, inside `_finalize_cycle_logic()`, as an additional tool-call
round on the already-open SM session.

### Existing pattern: `consecutive_readonly >= 8`

`gm/react_loop.py:278` implements a "N consecutive readonly → escalate"
pattern as a soft push rather than a hard limit. Same spirit applies here.

## Options Considered

### Two-tier design (recommended)

**Tier 1 — Mechanical default (no LLM call):**

| Condition | Default action |
|---|---|
| Zero progress, < N consecutive occurrences | Advance world time by 30s (conservative tick) |
| Implausible duration, < N consecutive occurrences | Clamp duration to `max_reasonable` for that `action_type` |

Starting values (tunable):

```python
ZERO_PROGRESS_FALLBACK_SECONDS = 30    # conservative tick
IM PLAUSIBLE_CLAMP_SECONDS = 300       # 5 min default clamp
MAX_CONSECUTIVE_SILENT = 5             # N before escalation
```

Reasonable-max mapping for implausible durations (starting values):

| Action type | Max reasonable seconds |
|---|---|
| `speech` | 120 (2 min) |
| `cosmetic` | 60 (1 min) |
| `movement` | 3600 (1 hour, for travel) |
| `combat` | 30 (one combat round) |
| `interact` | 600 (10 min) |
| default | 3600 (1 hour) |

**Tier 2 — Escalation to SM (LLM call):**

After N consecutive zero-progress or implausible-duration occurrences,
insert a single additional tool-call round into the SM's already-open
session, after SegmentScheduler resolution and before the buffer/publish
decision in `_finalize_cycle_logic()`. The SM is asked a narrow,
structural question — no new system prompt, no new pinned summaries:

> "The last {N} resolution passes produced no forward progress [or:
> implausible durations]. Current world time: {T}. What should happen?
> Options: (a) advance time by X, (b) inject a scene event, (c) other?"

The SM responds with a tool call (use existing `create_event` or a new
`resolve_time_anomaly` tool), and the cycle continues to the buffer/publish
decision normally.

### Where in the code this round is inserted

In `_finalize_cycle_logic()` (`console_app.py`), after the SegmentScheduler
resolution loop (L470) and before the structural-pause / buffer-ready
decision (L480). Pseudocode:

```python
# After resolution loop, before structural-pause check:
if _detect_zero_progress(cycle_resolved_history, consecutive_zero):
    consecutive_zero += 1
    if consecutive_zero >= MAX_CONSECUTIVE_SILENT:
        # Escalate to SM: one tool-call round on open session
        sm_answer = orch._sm.resolve_time_anomaly(...)
        # Apply SM's decision
    else:
        # Mechanical default: advance by 30s
        world_time += ZERO_PROGRESS_FALLBACK_SECONDS
else:
    consecutive_zero = 0  # reset on any progress
```

### What tool SM uses for the escalation

Reuse `create_event` — it already accepts structured facts. Add an
optional `advance_time_sec` parameter for the SM to explicitly advance
time if needed. This keeps the mechanism narrow (one additional parameter)
rather than a new standalone tool.

Alternatively, a dedicated `resolve_pause(reason: str, advance_time_sec: int)`
tool is cleaner and doesn't overload `create_event`. The task description
says "narrow, structural, system-initiated queries to SM, not a new
standalone tool SM calls proactively" — but this IS system-initiated (the
orchestrator detects the condition and inserts the round), not SM-initiated.
The tool SM calls in response can be simple.

Recommendation: add a lightweight `resolve_pause` tool available ONLY
during this escalation round (not in the normal tool list).

## Recommendation

1. Implement Tier 1 first: silent mechanical defaults with the starting
   values above. No LLM call for < 5 consecutive occurrences.
2. Implement Tier 2 as a follow-up: escalation to SM after 5 consecutive.
3. Mark ALL starting values (30s, 300s, 5, per-action-type caps) as tunable
   starting assumptions, same pattern as `_session_buffer_publish_chars`.

## Out of scope

- The `horizon` parameter of SegmentScheduler (replaced by unifying clocks
  in Item 2 — zero-progress is now a world-time concern).
- The per-action-type max table (can be expanded later; start with
  reasonable defaults).
- Any new prompt text or pinned summaries (escalation round reuses the
  existing SM session context).
