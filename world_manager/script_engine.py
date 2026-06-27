"""Script Engine — executes attached scripts on trigger conditions.

Scripts are stored on entities as:
  {"script_id": "...", "trigger_type": "on_tick",
   "trigger_params": {"interval_ticks": 5},
   "script": "def run(ctx): ...", "comment": "..."}

The engine evaluates triggers, creates a sandboxed namespace with injected
tool API, and executes the script. Execution is limited to 100ms.
"""
from __future__ import annotations

import types
import threading
from typing import Any, Dict, List, Optional

# ── Injected API — these functions are available inside scripts ──
from world_manager.entity_core import (
    get_entity, get_entity_facts, query_entities,
    adjust_entity,
    add_tags, remove_tags,
)
from world_manager.event_log import record_event


def _noop(*args, **kwargs):
    """Placeholder for tools NOT available in scripts."""
    raise PermissionError("This tool is not available inside scripts.")


# API available to scripts (subset of agent tools)
_SCRIPT_API = {
    "get_entity": get_entity,
    "get_entity_facts": get_entity_facts,
    "query_entities": query_entities,
    "adjust_entity": adjust_entity,
    "add_tags": add_tags,
    "remove_tags": remove_tags,
    "record_event": record_event,
    # set_effect/remove_effect are imported separately below
    # to break circular imports
}


def _inject_api(extra_tools: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the sandbox namespace with injected tool API."""
    ns = dict(_SCRIPT_API)
    from world_manager._json_backend import JsonFileBackend
    _be = JsonFileBackend()
    ns["set_effect"] = _be.set_effect
    ns["remove_effect"] = _be.remove_effect
    if extra_tools:
        ns.update(extra_tools)
    ns["__builtins__"] = {
        "True": True, "False": False, "None": None,
        "int": int, "float": float, "str": str, "bool": bool,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "len": len, "range": range, "min": min, "max": max,
        "abs": abs, "sum": sum, "sorted": sorted, "reversed": reversed,
        "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
        "isinstance": isinstance, "type": type, "hasattr": hasattr, "getattr": getattr,
        "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
        "ZeroDivisionError": ZeroDivisionError,
    }
    return ns


# ── Script execution with timeout ──

class ScriptTimeout(Exception):
    """Raised when a script exceeds its execution time limit."""
    pass


def _execute_with_timeout(code: str, context: dict, timeout_ms: int = 100) -> str:
    """Execute Python code in sandboxed namespace with timeout.

    Args:
        code: Python source. Must define def run(ctx): ...
        context: Dict injected as `ctx` parameter.
        timeout_ms: Max execution time in milliseconds.

    Returns:
        String result from run(), or error message.
    """
    namespace = _inject_api()
    namespace["ctx"] = context

    result_container: list = []

    def _run():
        try:
            exec(compile(code, "<script>", "exec"), namespace)
            if "run" in namespace and callable(namespace["run"]):
                r = namespace["run"](context)
                result_container.append(str(r) if r is not None else "ok")
            else:
                result_container.append("error: no run(ctx) function defined")
        except Exception as e:
            result_container.append(f"error: {type(e).__name__}: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_ms / 1000.0)

    if thread.is_alive():
        return "error: script timed out"

    return result_container[0] if result_container else "error: no result"


# ── Trigger evaluation ──

def _check_trigger(
    trigger_type: str,
    trigger_params: Dict[str, Any],
    entity: Dict[str, Any],
    current_tick: int,
    world_time: int,
    last_fired_tick: int = -1,
) -> bool:
    """Check if a trigger condition is met.

    Args:
        trigger_type: on_tick, on_stat_threshold, on_entity_enter,
                     on_event_nearby, on_world_time, on_tag_added.
        trigger_params: Parameters for the trigger type.
        entity: The entity this script is attached to.
        current_tick: Current world tick.
        world_time: Current world time.
        last_fired_tick: Tick when this script last fired (-1 = never).

    Returns:
        True if trigger condition is satisfied.
    """
    tt = trigger_type.strip().lower()

    if tt == "on_tick":
        interval = int(trigger_params.get("interval_ticks", 1))
        if last_fired_tick < 0:
            return True  # Fire immediately on first check
        return (current_tick - last_fired_tick) >= interval

    if tt == "on_stat_threshold":
        stat = trigger_params.get("stat", "")
        threshold = float(trigger_params.get("threshold", 0))
        direction = trigger_params.get("direction", "below").strip().lower()
        # Check state, gurps, stats
        value = None
        for block in ("state", "gurps", "stats"):
            if stat in entity.get(block, {}):
                value = entity[block].get(stat)
                break
        if value is None:
            return False
        if direction == "below":
            return float(value) < threshold
        elif direction == "above":
            return float(value) > threshold
        elif direction == "equals":
            return float(value) == threshold
        return False

    if tt == "on_world_time":
        target_tick = int(trigger_params.get("tick", -1))
        if target_tick < 0:
            return False
        if last_fired_tick < 0:
            return current_tick >= target_tick
        return last_fired_tick < target_tick <= current_tick

    if tt == "on_tag_added":
        # This trigger is checked when a tag is explicitly added,
        # not during tick evaluation. Always return False here.
        return False

    if tt in ("on_entity_enter", "on_event_nearby"):
        # These require spatial/event monitoring — not implemented in tick loop.
        # They fire via explicit calls from the orchestrator.
        return False

    return False


# ── Main processing ──

def _get_last_fired(script: Dict[str, Any]) -> int:
    """Get the last tick this script fired, from stored metadata."""
    meta = script.get("_meta", {}) or {}
    return int(meta.get("last_fired_tick", -1))


def _set_last_fired(script: Dict[str, Any], tick: int) -> None:
    """Record when this script fired."""
    meta = script.setdefault("_meta", {})
    meta["last_fired_tick"] = tick


def process_entity_scripts(
    entity_id: str,
    entity: Dict[str, Any],
    current_tick: int,
    world_time: int = 0,
) -> List[Dict[str, Any]]:
    """Evaluate and run all scripts attached to a single entity.

    Args:
        entity_id: Entity identifier.
        entity: Entity dict (mutated in place for last_fired tracking).
        current_tick: Current world tick.
        world_time: Current world time.

    Returns:
        List of script execution results:
        [{"script_id": "...", "trigger": "on_tick", "result": "..."}, ...]
    """
    scripts = entity.get("scripts", [])
    if not scripts:
        return []

    results: List[Dict[str, Any]] = []

    for script_entry in scripts:
        trigger_type = script_entry.get("trigger_type", "")
        trigger_params = script_entry.get("trigger_params", {}) or {}
        script_code = script_entry.get("script", "")
        script_id = script_entry.get("script_id", "")

        last_fired = _get_last_fired(script_entry)

        if not _check_trigger(trigger_type, trigger_params, entity,
                              current_tick, world_time, last_fired):
            continue

        # Build context
        ctx = {
            "entity_id": entity_id,
            "current_tick": current_tick,
            "world_time": world_time,
            "trigger_type": trigger_type,
            "trigger_params": trigger_params,
        }

        # Execute
        result = _execute_with_timeout(script_code, ctx)

        # Record firing
        _set_last_fired(script_entry, current_tick)

        results.append({
            "script_id": script_id,
            "trigger": trigger_type,
            "result": result,
        })

    return results


def process_all_entities(
    current_tick: int,
    world_time: int = 0,
    backend: Any = None,
) -> Dict[str, Any]:
    """Evaluate scripts on ALL entities.

    Args:
        current_tick: Current world tick.
        world_time: Current world time.
        backend: JsonFileBackend instance. If None, creates default.

    Returns:
        Summary dict with scripts_fired, errors, details.
    """
    if backend is None:
        from world_manager._json_backend import JsonFileBackend
        backend = JsonFileBackend()

    store = backend.load()
    total_fired = 0
    total_errors = 0
    details: List[Dict[str, Any]] = []

    for entity_id, entity in store.items():
        if entity.get("deleted_at"):
            continue
        results = process_entity_scripts(entity_id, entity, current_tick, world_time)
        for r in results:
            total_fired += 1
            if r.get("result", "").startswith("error"):
                total_errors += 1
            details.append(r)

        if results:
            backend._touch()

    if total_fired > 0:
        backend._flush()

    return {
        "tick": current_tick,
        "scripts_fired": total_fired,
        "errors": total_errors,
        "details": details[:10],  # First 10 for logging
    }
