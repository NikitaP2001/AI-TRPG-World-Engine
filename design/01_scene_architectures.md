# Item 1: Resolve the Two Scene Architectures

## Confirmed Problem

Two classes—`scene/runtime.py`'s `Scene` and `scheduler/timeline.py`'s
`SegmentScheduler`—are both instantiated and used. Their docstrings describe
different sequencing models (initiative-order vs shared continuous timeline).
We need to know which one actually drives the game.

## Investigation Findings

### Who calls what

Every live `Scene` call site, traced:

| Call site | File:line | What it does |
|---|---|---|
| `self._scene = Scene(world=self.world)` | `engine/orchestrator.py:73` | Instantiation |
| `self._scene.start(...)` | `engine/orchestrator.py:324` | Bootstrap: sets scene metadata, character list, player descriptions |
| `app._scene.mark_player_acted(...)` | `cycle_runner.py:277` | After player agent returns: sets `acted=True` in scene JSON |
| `orch._scene.end_with_gm_output(...)` | `console_app.py:585` | Advances world time, persists turn data, resets `acted` flags |

That's it. Scene is used for **bootstrap, acted tracking, and world-time
advancement**. It does NOT drive turn order or initiative — those are
handled by `cycle_runner.py`'s own logic (`_players_without_active_intent`,
`_invoke_players`) plus `SegmentScheduler`.

### SegmentScheduler — where it runs

`SegmentScheduler` is called **inside** `_finalize_cycle_logic()`
(`console_app.py:395-440`), after `all_acted` has been confirmed by Scene.
It resolves the shared timeline of intents, produces `resolved_history`,
handles carry-over, trigger matching, and re-invocation. It is the
**resolution engine**, not a replacement for Scene's lifecycle.

### The docstring contradiction

`Scene`'s own docstring (`scene/runtime.py:13-24`) says:

> New architecture (v2): ... Characters are executed one-by-one in
> initiative order automatically; no multi-round iteration logic.

This describes a world where Scene drives execution order. But the actual
`cycle_runner.py` flow does NOT use `Scene` for execution order. The
`player_order` key in scene JSON (written by `add_character_to_scene` at
`scene/runtime.py:176`) is never read by any caller. The "initiative order"
v2 architecture described in the docstring was **never completed** — the
code was written, but `cycle_runner.py` was subsequently rewritten to use
the shared-timeline model, and Scene's docstring was never updated.

### Conclusion: SegmentScheduler has taken over

`Scene` is reduced to:
- **Bootstrap/persistence**: who's present, where, scene metadata
  (`scene.json`, `start()`, `add_character_to_scene()`)
- **Acted flag tracking**: `mark_player_acted()`, `all_players_ended()`,
  `reset_acted_flags()`
- **World time advancement**: `end_with_gm_output()`

`SegmentScheduler` handles:
- Intent resolution, trigger matching, carry-over, re-invocation

The sequencing in `cycle_runner.py` uses BOTH: Scene for
bootstrap/acted/world-time, SegmentScheduler for resolution. Neither is
redundant — they operate at different layers.

## Recommendation

1. **Update `Scene`'s docstring** to reflect its current role:
   bootstrap/persistence/acted-tracking/world-time. Remove references to
   initiative order and "v2 architecture".

2. **Remove dead code** from `Scene`:
   - `validate_character_in_scene()` — never called externally (checked via
     grep), only used internally by `update_character_intent()` which is
     also uncalled.
   - `add_character_to_scene()` / `remove_character_from_scene()` — never
     called (characters are added once in `Scene.start()`).
   - `player_order` key in scene JSON — written but never read.

3. **Keep `Scene` as is** for bootstrap, acted-tracking, and
   `end_with_gm_output()`. These are not duplicating SegmentScheduler —
   they serve orthogonal concerns.

## Out of scope

- The two-clock problem (cursor vs world_time) is Item 2.
- The horizon/900s structural-pause is Item 3.
- `describe_scene`'s `horizon_t` is Item 4.
