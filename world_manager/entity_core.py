"""EntityCore — high-level facade over CompositeBackend.

Provides the public API that entity_store.py (and eventually all tools) calls.
Adds validation, logging, and cross-cutting concerns on top of the backend.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._json_backend import JsonFileBackend
from world_manager._composite_backend import CompositeBackend
from world_manager.entity_validation import (
    validate_position,
    validate_facts,
)


# Module-level singleton
_backend: Optional[CompositeBackend] = None


def _get_backend() -> CompositeBackend:
    """Lazy-init singleton."""
    global _backend
    if _backend is None:
        json_backend = JsonFileBackend()
        _backend = CompositeBackend(json_backend)
    return _backend


def reset_backend() -> None:
    """Reset the singleton (for testing) and invocation counter."""
    global _backend
    _backend = None
    JsonFileBackend.reset_invocation_counter()


# ── Helpers ──


def _is_meaningful(val: Any) -> bool:
    """A value is meaningful (non-empty) for storage purposes.

    Empty strings, dicts, lists, and None are not stored — they waste
    space and convey no information. This rule applies universally,
    regardless of entity type, so WM-invented types get clean storage
    for free without any per-type schema.
    """
    if val is None:
        return False
    if isinstance(val, (str, list, dict)) and not val:
        return False
    return True


def _compact(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove empty/default values from a record dict.

    Empty values convey nothing and waste storage. `id` and `type` are
    always provided by the caller and are meaningful strings, so they
    survive this filter naturally.
    """
    return {k: v for k, v in record.items() if _is_meaningful(v)}


# ── Public API ──


def reset_creation_counter() -> None:
    """Reset per-invocation creation rate limit. Call at start of agent invocation."""
    JsonFileBackend.reset_invocation_counter()


def set_entity(
    id: str = "",
    type: str = "",
    base_type: str = "",
    facts: str = "",
    gurps: Optional[Dict[str, Any]] = None,
    stats: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
    position: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    skills: Optional[Dict[str, Any]] = None,
    equipment: Optional[Dict[str, Any]] = None,
    parent_location: str = "",
    sublocations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create or update any entity.

    Entity type is an arbitrary string defined by the WM — there are no
    reserved types and no per-type schema. Only non-empty values are
    persisted; empty defaults (gurps={}, stats={}, tags=[], etc.)
    are silently omitted from storage regardless of entity type.

    Position validation (coords lat/lon bounds, inside/near + offset format)
    is the only generic form check — it applies to every entity type.
    Per-type structural rules ("location must have facts") are the WM's
    responsibility via base_type templates, not the engine's.
    """
    # 1. Check if entity already exists (set_entity is create-only)
    if id:
        existing = _get_backend().get(id)
        if existing is not None:
            return {"status": "error", "error": f"Entity '{id}' already exists. Use adjust_entity to modify runtime state or position."}

    # 2. Generic form validation (position format, facts safety)
    err = validate_position(position)
    if err:
        return {"status": "error", "error": f"Invalid position: {err}"}

    # 3. Collect all API params and strip empty/None values
    #    (tool defaults like gurps={} become "not provided" — not stored)
    record = _compact({
        "id": id,
        "type": type,
        "base_type": base_type,
        "facts": facts,
        "gurps": gurps,
        "stats": stats,
        "tags": tags,
        "position": position,
        "state": state,
        "skills": skills,
        "equipment": equipment,
        "parent_location": parent_location,
        "sublocations": sublocations,
    })

    et = record.pop("type", type or "")
    eid = record.pop("id", id or "")

    # 4. Pass to backend — id/type as explicit params, record as kwargs
    return _get_backend().set(id=eid, type=et, **record)


def get_entity(id: str) -> Optional[Dict[str, Any]]:
    """Read a single entity in full. Returns None if not found or deleted."""
    return _get_backend().get(id)


def get_entity_facts(id: str) -> Optional[str]:
    """Read only the facts string of an entity."""
    return _get_backend().get_facts(id)


def set_entity_facts(id: str, facts: str) -> Dict[str, Any]:
    """Update only the prose facts of an entity."""
    return _get_backend().set_facts(id, facts)


def get_entity_state(entity_id: str) -> Optional[Dict[str, Any]]:
    """Read mechanical state: gurps, state, skills, equipment, stats, effects."""
    return _get_backend().get_state(entity_id)


def delete_entity(id: str, reason: str = "") -> Dict[str, Any]:
    """Mark entity as deleted."""
    return _get_backend().delete(id, reason)


def query_entities(
    type_filter: str = "",
    base_type: str = "",
    tags: Optional[List[str]] = None,
    position: Optional[Dict[str, Any]] = None,
    radius: float = -1.0,
    facts_search: str = "",
    include_templates: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search entities with filtering."""
    return _get_backend().query(
        type_filter=type_filter, base_type=base_type,
        tags=tags, position=position, radius=radius,
        facts_search=facts_search,
        include_templates=include_templates, limit=limit,
    )


def add_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Add tags to an entity."""
    return _get_backend().add_tags(entity_id, tags)


def remove_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Remove tags from an entity."""
    return _get_backend().remove_tags(entity_id, tags)


def set_effect(
    entity_id: str,
    effect_id: str = "",
    stat_deltas: Optional[Dict[str, Any]] = None,
    per_tick: bool = True,
    duration_ticks: int = -1,
    description: str = "",
) -> Dict[str, Any]:
    """Apply a persistent stat modifier to an entity."""
    return _get_backend().set_effect(
        entity_id=entity_id, effect_id=effect_id,
        stat_deltas=stat_deltas, per_tick=per_tick,
        duration_ticks=duration_ticks, description=description,
    )


def remove_effect(entity_id: str, effect_id: str) -> Dict[str, Any]:
    """Remove a named effect from an entity."""
    return _get_backend().remove_effect(entity_id, effect_id)


def attach_script(
    entity_id: str,
    script_id: str = "",
    trigger_type: str = "",
    trigger_params: Optional[Dict[str, Any]] = None,
    script: str = "",
    comment: str = "",
) -> Dict[str, Any]:
    """Attach a behavior script to an entity."""
    return _get_backend().attach_script(
        entity_id=entity_id, script_id=script_id,
        trigger_type=trigger_type, trigger_params=trigger_params,
        script=script, comment=comment,
    )


def detach_script(entity_id: str, script_id: str) -> Dict[str, Any]:
    """Remove a script from an entity."""
    return _get_backend().detach_script(entity_id, script_id)


def adjust_entity(
    entity_id: str,
    hp: Optional[int] = None,
    fp: Optional[int] = None,
    er: Optional[int] = None,
    add_condition: str = "",
    remove_condition: str = "",
    add_injury: str = "",
    remove_injury: str = "",
    add_item: str = "",
    remove_item: str = "",
    currency_delta: Optional[Dict[str, int]] = None,
    add_ability: str = "",
    remove_ability: str = "",
    add_skill: str = "",
    remove_skill: str = "",
    current_state: str = "",
    gurps: Optional[Dict[str, Any]] = None,
    position: Optional[Dict[str, Any]] = None,
    connections: Optional[Dict[str, Any]] = None,
    skills: Optional[Dict[str, Any]] = None,
    equipment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply targeted runtime mutations to an entity (NPC or character)."""
    # Validate position if provided
    if position is not None:
        from world_manager.entity_validation import validate_position
        err = validate_position(position)
        if err:
            return {"status": "error", "error": f"Invalid position: {err}"}
    # Validate connections if provided
    if connections is not None:
        from world_manager.entity_validation import validate_connections
        err = validate_connections(connections)
        if err:
            return {"status": "error", "error": f"Invalid connections: {err}"}
    return _get_backend().adjust(
        entity_id=entity_id,
        hp=hp, fp=fp, er=er,
        add_condition=add_condition, remove_condition=remove_condition,
        add_injury=add_injury, remove_injury=remove_injury,
        add_item=add_item, remove_item=remove_item,
        currency_delta=currency_delta,
        add_ability=add_ability, remove_ability=remove_ability,
        add_skill=add_skill, remove_skill=remove_skill,
        current_state=current_state,
        gurps=gurps,
        position=position if position else None,
        connections=connections,
        skills=skills,
        equipment=equipment,
    )


def compute_entry_offset(
    source_id: str,
    target_id: str,
    exit_offset: List[float],
) -> Dict[str, Any]:
    """Compute the entry point offset in target's local space.

    Given a connection from source to target where both entities are inside
    the same parent (same position.inside), computes where the connection
    opens in the target's local coordinate system.

    Returns {"offset": [x, y, z]} with the computed entry point,
    or {"offset": exit_offset} if entities are not in the same parent
    or position data is insufficient.
    """
    backend = _get_backend()
    source = backend.get(source_id)
    target = backend.get(target_id)
    if not source or not target:
        return {"offset": list(exit_offset)}

    src_pos = source.get("position") or {}
    tgt_pos = target.get("position") or {}

    src_parent = src_pos.get("inside", "")
    tgt_parent = tgt_pos.get("inside", "")
    src_off = src_pos.get("offset")
    tgt_off = tgt_pos.get("offset")

    # Both must share the same parent and have offsets
    if not src_parent or src_parent != tgt_parent:
        return {"offset": list(exit_offset)}
    if not isinstance(src_off, (list, tuple)) or len(src_off) != 3:
        return {"offset": list(exit_offset)}
    if not isinstance(tgt_off, (list, tuple)) or len(tgt_off) != 3:
        return {"offset": list(exit_offset)}

    # Door position in parent space
    door_x = src_off[0] + exit_offset[0]
    door_y = src_off[1] + exit_offset[1]
    door_z = src_off[2] + exit_offset[2]

    # Entry point in target's local space
    entry = [door_x - tgt_off[0], door_y - tgt_off[1], door_z - tgt_off[2]]
    return {"offset": entry}


def tick(current_tick: int, world_time: int = 0) -> Dict[str, Any]:
    """Process all effects (per_tick, expiry, auras) and scripts for all entities.

    Called by orchestrator after each world tick.

    Args:
        current_tick: Current world tick number.
        world_time: Current world time for context.

    Returns:
        Combined summary dict from effect engine and script engine.
    """
    from world_manager.effect_engine import process_all_entities as _process_effects
    from world_manager.script_engine import process_all_entities as _process_scripts
    backend = _get_backend()._json

    effects_result = _process_effects(current_tick, world_time, backend)
    scripts_result = _process_scripts(current_tick, world_time, backend)

    return {
        "tick": current_tick,
        "effects": effects_result,
        "scripts": scripts_result,
    }
