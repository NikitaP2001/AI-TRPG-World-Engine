"""EntityCore — high-level facade over CompositeBackend.

Provides the public API that entity_store.py (and eventually all tools) calls.
Adds validation, logging, and cross-cutting concerns on top of the backend.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._json_backend import JsonFileBackend
from world_manager._character_backend import CharacterFileBackend
from world_manager._location_backend import LocationFileBackend
from world_manager._composite_backend import CompositeBackend


# Module-level singleton
_backend: Optional[CompositeBackend] = None


def _get_backend() -> CompositeBackend:
    """Lazy-init singleton."""
    global _backend
    if _backend is None:
        json_backend = JsonFileBackend()
        char_backend = CharacterFileBackend()
        loc_backend = LocationFileBackend()
        _backend = CompositeBackend(json_backend, char_backend, loc_backend)
    return _backend


def reset_backend() -> None:
    """Reset the singleton (for testing)."""
    global _backend
    _backend = None


# ── Public API (same signatures as old entity_store.py) ──


def _validate_position(position: Optional[Dict[str, Any]]) -> Optional[str]:
    """Check position format. Returns error string or None if valid."""
    if not position:
        return None
    valid_keys = {"inside", "near", "coords", "offset"}
    extra = set(position.keys()) - valid_keys
    if extra:
        return f"Unknown position key(s): {', '.join(sorted(extra))}."
    if "coords" in position:
        coords = position["coords"]
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            return "coords must be [lat, lon] or [lat, lon, z]."
        lat, lon = coords[0], coords[1]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return "coords values must be numbers."
        if lat < -90 or lat > 90:
            return f"Latitude must be between -90 and 90, got {lat}."
        if lon < -180 or lon > 180:
            return f"Longitude must be between -180 and 180, got {lon}."
    for key in ("inside", "near"):
        if key in position:
            ref = position[key]
            if not isinstance(ref, str) or not ref.strip():
                return f"{key} must be a non-empty entity_id."
    return None


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
) -> Dict[str, Any]:
    """Create or update any entity. Routes to correct backend automatically.

    Position validation: coords must be valid lat/lon (-90..90, -180..180);
    inside/near must reference a non-empty entity_id.
    """
    err = _validate_position(position)
    if err:
        return {"status": "error", "error": f"Invalid position: {err}"}
    return _get_backend().set(
        id=id, type=type, base_type=base_type,
        facts=facts, gurps=gurps, stats=stats,
        tags=tags, position=position,
        state=state, skills=skills, equipment=equipment,
    )


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


def set_movement(
    entity_id: str,
    destination: Optional[Dict[str, Any]] = None,
    speed: float = 1.0,
    comment: str = "",
) -> Dict[str, Any]:
    """Set an entity in motion toward a destination."""
    # Only JsonFileBackend supports movement
    return _get_backend()._json.set_movement(entity_id, destination, speed, comment)


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
) -> Dict[str, Any]:
    """Apply targeted runtime mutations to an entity (NPC or character)."""
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
    )


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
