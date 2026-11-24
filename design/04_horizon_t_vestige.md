# Item 4: Remove the `describe_scene` `horizon_t` Vestige

## Confirmed Problem

`scene_manager/core.py`'s `describe_scene()` returns `"horizon_t": "15m"`
as a hardcoded literal. This is a remnant of the removed `T` planning
horizon concept from the old architecture.

## Investigation Findings

### Write sites (2)

| Location | Code |
|---|---|
| `scene_manager/core.py:718` | `return {"personal": personal, "combined": combined, "horizon_t": "15m"}` |
| `scene_manager/core.py:720` | `return {"personal": {}, "combined": "", "horizon_t": "15m"}` |

### Read sites (0)

`grep -r "horizon_t"` across the entire codebase finds zero readers beyond
the two write sites above.

### Consumer of `describe_scene` return value

`describe_scene()` is called at `engine/orchestrator.py:306`:

```python
desc_result = self._sm.describe_scene(...)
player_descriptions = desc_result.get("personal") or {}
```

Only `personal` is read. `combined` and `horizon_t` are ignored.

## Recommendation

Remove `"horizon_t": "15m"` from both return statements. The dict becomes:

```python
return {"personal": personal, "combined": combined}
```

No other code change needed — nothing reads it.

## Out of scope

- Whether `combined` (the concatenated per-player scene descriptions) is
  still useful. It's not read by any code either, but removing it is a
  separate decision (it may be useful for debugging or future features).
