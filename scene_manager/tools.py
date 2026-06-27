from __future__ import annotations
from langchain_core.tools import tool
from engine.tool_base import _guard_tool

from world_manager.tool_registry import (
    get_entity_facts, get_entity_state, get_scene, adjust_entity,
    record_event, query_events,
    invoke_self, close_invocation,
    set_effect, remove_effect,
    escalate_to_gm,
)
from world_manager.entity_core import _get_backend

# Deprecated — kept for backward compat. Use adjust_entity instead.
@tool
def update_entity_stat(entity_id: str, stat: str, value: float) -> dict:
    """Update a single stat on an entity. Deprecated — use adjust_entity instead."""
    err = _guard_tool("update_entity_stat")
    if err:
        return {"error": err}
    # Delegate to adjust_entity for hp/fp/er
    if stat.lower() == "hp":
        return adjust_entity(entity_id=entity_id, hp=int(value))
    elif stat.lower() == "fp":
        return adjust_entity(entity_id=entity_id, fp=int(value))
    elif stat.lower() == "er":
        return adjust_entity(entity_id=entity_id, er=int(value))
    # For gurps stats, use set_entity with merge
    from world_manager.entity_core import _get_backend as _be
    backend = _be()
    entity = backend.get(entity_id)
    if entity is None:
        return {"error": f"Entity '{entity_id}' not found"}
    if stat in entity.get("gurps", {}):
        backend._json.set(entity_id=entity_id, gurps={stat: int(value)})
    else:
        backend._json.set(entity_id=entity_id, stats={stat: int(value)})
    return {"entity_id": entity_id, "stat": stat, "new_value": value}

# Deprecated — kept for backward compat. Use set_effect instead.
@tool
def apply_effect(entity_id: str, effect_id: str = "", stat_deltas: dict = {},
                 per_tick: bool = False, duration_ticks: int = -1, description: str = "") -> dict:
    """Apply a one-shot effect. Deprecated — use set_effect instead."""
    err = _guard_tool("apply_effect")
    if err:
        return {"error": err}
    return set_effect(entity_id=entity_id, effect_id=effect_id,
                      stat_deltas=stat_deltas, per_tick=per_tick,
                      duration_ticks=duration_ticks, description=description)

__all__ = [
    "get_entity_facts", "query_events", "record_event",
    "invoke_self", "close_invocation",
    "get_scene", "get_entity_state",
    "update_entity_stat", "adjust_entity",
    "apply_effect", "set_effect", "remove_effect",
    "escalate_to_gm",
]
