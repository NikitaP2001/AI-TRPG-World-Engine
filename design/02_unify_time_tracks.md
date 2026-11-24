# Item 2: Unify the Two Independent Time Tracks Into One

## Confirmed Problem

`SegmentScheduler`'s `cursor` (in `timeline.json`) and authoritative
`world_time` (in `info.json`) advance independently. The design doc
(`scene_design.md` §1) describes **one** continuous timeline, never two
clocks. The `cursor` concept appears nowhere in the design doc — it is
implementation-only.

## Investigation Findings

### All cursor read/write sites (console_app.py)

| Location | Expression | Role |
|---|---|---|
| L374 | `cursor = max(start_sec, int(timeline_store.get("cursor") or 0))` | Initialized from persisted timeline |
| L397 | `_dict_to_intent(d, anchor_sec=cursor)` | Carry-over intents anchored to cursor |
| L405 | `horizon = cursor + DEFAULT_HORIZON_SECONDS` | Resolution horizon for SegmentScheduler |
| L415 | `SegmentScheduler(..., now=cursor, horizon=horizon)` | Fed to scheduler as `now` |
| L428 | `cursor = result.next_cursor` | Updated by scheduler after resolution |
| L441 | `reinvoke_time_str = _WT.from_seconds(cursor).to_string()` | Time override for re-invoked characters |
| L464 | `_dict_to_intent(d, anchor_sec=cursor)` | Fresh intents anchored to cursor |
| L483 | `new_cursor = cursor + 900` | Structural pause jump |
| L488 | `"cursor": new_cursor` | Persisted to timeline.json |
| L500 | `new_cursor = cursor + 900` | Buffer-not-ready jump |
| L523 | `"cursor": cursor` | Persisted after successful narration |

Total: 11 read/write sites, all in `console_app.py:_finalize_cycle_logic()`.

### World time advancement

Only advances via `Scene.end_with_gm_output(cycle_duration=duration)`
(`scene/runtime.py:289-310`). `duration` comes from
`publish_narrative_block(duration=...)` — free text written by SM with no
structural connection to resolved intents.

### `covers_t_range` in the design doc

`scene_design.md` §6.4 defines `publish_narrative_block`'s signature as:

```
publish_narrative_block(
    since_event_id: str,
    narration: str,
    covers_t_range: [t_from, t_to],
)
```

No `duration` field. The design doc says `covers_t_range` is the timeline
range this block narrates. `duration` is a code invention that replaced
`covers_t_range`.

### The drift mechanism

1. Cycles with content but buffer < 3000 chars: cursor jumps +900s, world
   time does NOT move (console_app.py L500).
2. Structural pauses (no content): cursor jumps +900s, world time does NOT
   move (console_app.py L483).
3. When narration finally fires: world time advances by SM's free-text
   `duration` from the **original** scene `start_time`
   (scene/runtime.py L304), which may be far behind cursor.

After N structural-pause cycles, cursor can be hours ahead of world time.

## Key Insight: SM Does Not Decide the Time Range

The SegmentScheduler (§4) has already resolved which intents completed and
when. By the time SM calls `publish_narrative_block`, the time range this
block covers is **already a mechanical fact** — from the last published
event to the current resolved point on the unified timeline. SM is not
deciding what happened or when; SM is **describing** what already happened.

Therefore the time range should not be any kind of SM input — neither
`covers_t_range` nor `duration`. The system computes it mechanically.

## Options Considered

### Option A (previous draft): `covers_t_range` from SM

Restore `covers_t_range: [t_start_sec, t_end_sec]` as SM parameters.
Duration derived from range.

Rejected because: the range is a mechanical consequence, not an SM
decision. Making SM compute absolute seconds ("SM must compute absolute
seconds, harder than relative duration") adds complexity and an
opportunity for error with no benefit — the system already knows the
answer.

### Option B (previous draft): validate `duration` against `covers_t_range`

Keep `duration` as SM-writable, validate against `covers_t_range`.

Rejected because: still requires SM to provide redundant information.
Two parameters where zero are needed.

### Option C: System-computed range (recommended)

`publish_narrative_block` has **no time parameters at all**. The system:

1. Tracks `last_published_event_id` as internal state (set to the latest
   `event_id` covered by each `publish_narrative_block` call — this is
   what `since_event_id` was supposed to express, but as system state,
   not agent input).
2. When SM calls `publish_narrative_block`, computes:
   - `t_start_sec` = timestamp of `last_published_event_id` in the event
     log (or beginning of scene if first publication)
   - `t_end_sec` = current resolved point on the unified timeline
     (= `world_time` after unification, previously `cursor`)
   - `duration_sec = t_end_sec - t_start_sec`
3. Advances world time to `t_end_sec`.
4. Updates `last_published_event_id` to the latest event covered.

No divergence from `scene_design.md` — §1 says one timeline, §6.4 says
`covers_t_range` but that's the block's **metadata**, and the system can
fill it in without agent help.

## Recommendation: Option C — system-computed range

### Changes required

#### 1. `publish_narrative_block` signature

```python
publish_narrative_block(
    narration: Dict[str, str],
    factual_summary: str,
) -> str
```

No time parameters. SM describes only what happened.

#### 2. Unify cursor → world_time

Replace `cursor` with `world_time.to_seconds()` everywhere in
`console_app.py:_finalize_cycle_logic()`:

| As-is | To-be |
|---|---|
| `cursor = max(start_sec, int(tl.get("cursor") or 0))` | `now_sec = world_time.to_seconds()` |
| `horizon = cursor + 900` | `horizon = now_sec + horizon_cap` |
| `cursor = result.next_cursor` | `now_sec = result.next_cursor` → `world_time = _WT.from_seconds(now_sec)` |
| `"cursor": new_cursor` persisted | Removed from timeline.json |
| `_dict_to_intent(d, anchor_sec=cursor)` | `_dict_to_intent(d, anchor_sec=now_sec)` |

World time advances by either:
- SegmentScheduler resolution (normal case): `world_time = resolved_t_end`
- Zero-progress fallback (Item 3): mechanical default or SM escalation

#### 3. Track `last_published_event_id`

Add a new field to the scene or timeline state:
`last_published_event_id: str`. Updated after each successful
`publish_narrative_block`. Used to compute the block's time range.

#### 4. Structural pauses

Structural pauses (no content / buffer not ready) do NOT advance world
time at all — the unified timeline simply does not move. Time only
advances via resolution or the zero-progress fallback (Item 3). This
eliminates the cursor/world_time drift entirely.

### Tool signature final

```
publish_narrative_block(
    narration: Dict[str, str],    # per-character personalized narrative
    factual_summary: str,          # objective summary → SM history + change record
)
```

### What this changes

| As-is | To-be |
|---|---|
| `cursor` in timeline.json | Removed — world time is the cursor |
| `duration: str` on tool | Removed — not an SM concern |
| `covers_t_range` | Not a tool parameter — system-computed from `last_published_event_id` → now |
| `since_event_id` | Not a tool parameter — tracked as system state `last_published_event_id` |
| SM writes free-text duration | SM writes nothing about time |
| Structural pause jumps cursor (+900s) | Structural pause does nothing — world time stays. Zero-progress fallback (Item 3) handles advancement |
| Two clocks drifting | One clock — world time |

## Out of scope

- The zero-progress fallback (Item 3) — handles the case where nothing
  resolves and time needs to advance.
- The `horizon` parameter of SegmentScheduler (still `now + cap` where
  `now` is world time — just a resolution bound, not a separate clock).
- The `describe_scene` horizon_t (Item 4).
- Per-player narration vs factual_summary (already implemented, just needs
  `duration` removed from the tool).
