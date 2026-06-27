"""ToolRegistry — single source of truth for all agent tools.

Every tool is defined ONCE here. Each agent gets a subset via classmethods:
  ToolRegistry.for_wm() → List[Tool]  (19 tools)
  ToolRegistry.for_gm() → List[Tool]  (18 tools)
  ToolRegistry.for_sm() → List[Tool]  (11 tools)

Tool docstrings serve as the primary documentation. Keep them concise.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

# ── Backend implementations ──
from world_manager.entity_store import (
    set_entity as _store_set_entity,
    get_entity as _store_get_entity,
    get_entity_facts as _store_get_entity_facts,
    set_entity_facts as _store_set_entity_facts,
    get_entity_state as _store_get_entity_state,
    delete_entity as _store_delete_entity,
    query_entities as _store_query_entities,
    add_tags as _store_add_tags,
    remove_tags as _store_remove_tags,
    set_effect as _store_set_effect,
    remove_effect as _store_remove_effect,
    attach_script as _store_attach_script,
    detach_script as _store_detach_script,
    set_movement as _store_set_movement,
    adjust_entity as _store_adjust_entity,
)
from world_manager.event_log import (
    record_event as _log_record_event,
    query_events as _log_query_events,
)
from scheduler_queue import schedule_invocation as _sched
from news_queue import close_invocation as _ci


# ======================================================================
# 1.  Entity tools (WM + GM)
# ======================================================================


@tool
def set_entity(
    id: str = "",
    type: str = "",
    base_type: str = "",
    facts: str = "",
    gurps: Dict[str, Any] = {},
    stats: Dict[str, Any] = {},
    tags: List[str] = [],
    position: Dict[str, Any] = {},
    state: Optional[Dict[str, Any]] = None,
    skills: Optional[Dict[str, Any]] = None,
    equipment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create or update any entity. The universal write operation.

    Works for NPCs, characters, locations, factions, items — anything.
    Entity types are strings defined by the WM; no reserved types.

    On create (empty id): auto-generates and returns a new id.
    On update (existing id): merges gurps/stats, replaces state/skills/equipment.

    Inheritance via base_type: facts, gurps, stats, tags cascade from template.

    Args:
        id: Unique identifier. Empty = auto-generated.
        type: Entity type (e.g. "npc", "character", "location", "faction").
        base_type: Template entity_id to inherit from.
        facts: Static narrative identity. NOT for mechanical data.
        gurps: GURPS stat block. Keys: ST, DX, IQ, HT, HP, Will, Per, FP, etc.
        stats: Free-form numeric properties.
        tags: Structured labels "namespace:value" for queries.
        position: Where: {"inside": id}, {"near": id}, {"coords": [x,y,z]}.
        state: Dynamic state block {hp, fp, conditions, injuries}.
        skills: Skills block {ST, DX, IQ, HT, passive_abilities, cached_skills}.
        equipment: Equipment block {signature, inventory, currency}.
    """
    kwargs = dict(id=id, type=type, base_type=base_type, facts=facts,
                  gurps=gurps, stats=stats, tags=tags, position=position)
    if state is not None:
        kwargs["state"] = state
    if skills is not None:
        kwargs["skills"] = skills
    if equipment is not None:
        kwargs["equipment"] = equipment
    return _store_set_entity(**kwargs)


@tool
def set_entity_facts(id: str, facts: str) -> Dict[str, Any]:
    """Update only the prose facts of an entity.

    Does not touch gurps, stats, tags, state, skills, or equipment.
    """
    return _store_set_entity_facts(id=id, facts=facts)


@tool
def get_entity(id: str) -> Dict[str, Any]:
    """Read a single entity in full, including all blocks.

    Returns all fields: facts, gurps, state, skills, equipment, stats,
    tags, position, scripts, effects, movement.
    """
    result = _store_get_entity(id)
    if result is None:
        return {"error": f"Entity '{id}' not found"}
    return result


@tool
def get_entity_facts(id: str) -> str:
    """Read only the prose facts of an entity.

    Lightweight — does not return mechanical stats.
    Use for loading NPC/character context on demand.
    """
    result = _store_get_entity_facts(id)
    if result is None:
        return f"Entity '{id}' not found"
    return result


@tool
def get_entity_state(entity_id: str) -> Dict[str, Any]:
    """Read mechanical state of an entity: gurps, state, skills, equipment, effects.

    Does NOT include facts (use get_entity_facts for narrative context).
    Use for scene resolution and combat checks.
    """
    result = _store_get_entity_state(entity_id)
    if result is None:
        return {"error": f"Entity '{entity_id}' not found"}
    return result


@tool
def query_entities(
    type: str = "",
    base_type: str = "",
    tags: List[str] = [],
    position: Dict[str, Any] = {},
    radius: float = -1.0,
    facts_search: str = "",
    include_templates: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search entities with flexible filtering.

    Structural queries (tags, type) are fast. facts_search is text-based.

    Args:
        type: Entity type filter. Empty = any type.
        tags: All listed tags must be present.
        position: Spatial center for radius search.
        radius: Spatial radius in world units. -1 = no spatial filter.
        facts_search: Case-insensitive word search on facts text.
        include_templates: True = include templates in results.
        limit: Max results. Default 50.
    """
    return _store_query_entities(
        type_filter=type, base_type=base_type, tags=tags,
        position=position, radius=radius,
        facts_search=facts_search,
        include_templates=include_templates, limit=limit)


@tool
def add_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Add tags to an entity. Tags are searchable labels 'namespace:value'."""
    return _store_add_tags(entity_id=entity_id, tags=tags)


@tool
def remove_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Remove tags from an entity. Idempotent — missing tags are ignored."""
    return _store_remove_tags(entity_id=entity_id, tags=tags)


@tool
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
    """Apply targeted runtime mutations to any entity.

    Unlike set_entity (which replaces static blocks), this is for small
    frequent changes: HP loss, conditions gained, items picked up.

    Every parameter is optional. Only provided fields are changed.

    Args:
        entity_id: Target entity.
        hp/fp/er: New value (absolute set, not delta).
        add/remove_condition: Append/remove one condition string.
        add/remove_injury: Append/remove one injury string.
        add/remove_item: Append/remove one inventory item string.
        currency_delta: Delta map, e.g. {"gold": -5, "silver": 10}.
        add/remove_ability: Append/remove one passive ability.
        add/remove_skill: Append/remove one cached skill.
        current_state: Overwrite one-line current activity description.
    """
    return _store_adjust_entity(
        entity_id=entity_id, hp=hp, fp=fp, er=er,
        add_condition=add_condition, remove_condition=remove_condition,
        add_injury=add_injury, remove_injury=remove_injury,
        add_item=add_item, remove_item=remove_item,
        currency_delta=currency_delta,
        add_ability=add_ability, remove_ability=remove_ability,
        add_skill=add_skill, remove_skill=remove_skill,
        current_state=current_state,
    )


# ======================================================================
# 2.  Effect tools (WM + GM + SM)
# ======================================================================


@tool
def set_effect(
    entity_id: str,
    effect_id: str = "",
    stat_deltas: Dict[str, Any] = {},
    per_tick: bool = True,
    duration_ticks: int = -1,
    description: str = "",
    aura_radius: float = -1.0,
) -> Dict[str, Any]:
    """Apply a persistent stat modifier to an entity.

    Use for bleeding, regeneration, poison, power drain.
    For effects requiring conditional logic, use attach_script.

    If aura_radius > 0, the effect applies to ALL entities within
    that radius (in world units), not just the target entity.

    Args:
        entity_id: Target entity.
        effect_id: Unique id. Auto-generated if empty.
        stat_deltas: {"HP": -2} — negative = deplete, positive = restore.
        per_tick: True = delta applied every tick. False = one-shot.
        duration_ticks: Ticks before expiry. -1 = permanent.
        description: Human-readable name (e.g. "bleeding wound").
        aura_radius: Radius in world units. -1 = self-only.
    """
    return _store_set_effect(
        entity_id=entity_id, effect_id=effect_id,
        stat_deltas=stat_deltas, per_tick=per_tick,
        duration_ticks=duration_ticks, description=description,
        aura_radius=aura_radius)


@tool
def remove_effect(entity_id: str, effect_id: str) -> Dict[str, Any]:
    """Remove a named effect from an entity immediately."""
    return _store_remove_effect(entity_id=entity_id, effect_id=effect_id)


# ======================================================================
# 3.  Script tools (WM + GM)
# ======================================================================


@tool
def attach_script(
    entity_id: str,
    trigger_type: str,
    trigger_params: Dict[str, Any],
    script: str,
    comment: str = "",
) -> Dict[str, Any]:
    """Attach a behavior script to an entity.

    Scripts run automatically when their trigger fires.
    Trigger types: on_tick, on_stat_threshold, on_entity_enter,
    on_event_nearby, on_world_time, on_tag_added.
    Script code has access to: get_entity, query_entities, set_effect, etc.

    Args:
        entity_id: Target entity.
        trigger_type: What causes execution (see above).
        trigger_params: Parameters for the trigger.
        script: Python source. Must define def run(ctx): ...
        comment: Why this script exists (appears in newspaper).
    """
    return _store_attach_script(
        entity_id=entity_id, trigger_type=trigger_type,
        trigger_params=trigger_params, script=script, comment=comment)


@tool
def detach_script(entity_id: str, script_id: str) -> Dict[str, Any]:
    """Remove a script from an entity."""
    return _store_detach_script(entity_id=entity_id, script_id=script_id)


# ======================================================================
# 4.  Event tools (WM + GM + SM)
# ======================================================================


@tool
def record_event(
    description: str,
    entity_ids: List[str] = [],
    position: Dict[str, Any] = {},
    tag: str = "",
    narrative_flag: bool = False,
    causal_parent: str = "",
) -> Dict[str, Any]:
    """Write an event to the 4D causal log.

    Use for recording occurrences — battles, deaths, discoveries.
    For asserting entity state, use set_entity or adjust_entity.

    Args:
        description: What happened. Clear and specific.
        entity_ids: All entities causally involved.
        position: Where it happened.
        tag: Single categorical label for filtering.
        narrative_flag: True = appears in agent newspaper.
        causal_parent: event_id that caused this one.
    """
    return _log_record_event(
        description=description, entity_ids=entity_ids,
        position=position, tag=tag,
        narrative_flag=narrative_flag, causal_parent=causal_parent)


@tool
def query_events(
    position: Dict[str, Any] = {},
    radius: float = -1.0,
    z_min: float = -999999.0,
    z_max: float = 999999.0,
    t_from: int = -1,
    t_to: int = -1,
    entity_id: str = "",
    tag: str = "",
    narrative_flag_only: bool = False,
    causal_parent: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read from the 4D causal event log.

    Filter by spatial region, time range, entity, tag, or causality.
    """
    return _log_query_events(
        position=position, radius=radius,
        z_min=z_min, z_max=z_max,
        t_from=t_from, t_to=t_to,
        entity_id=entity_id, tag=tag,
        narrative_flag_only=narrative_flag_only,
        causal_parent=causal_parent, limit=limit)


# ======================================================================
# 5.  Agent lifecycle tools (all agents)
# ======================================================================


@tool
def invoke_self(
    delay_ticks: int = -1,
    world_time: int = -1,
    comment: str = "",
) -> Dict[str, Any]:
    """Schedule the calling agent's next invocation.

    Exactly one of delay_ticks or world_time must be provided.
    Use comment to summarise what was done for the [reason] block.

    Args:
        delay_ticks: Invoke N ticks from now.
        world_time: Absolute world tick to invoke at.
        comment: Why — appears as [reason] on next invocation.
    """
    # Agent is set per-call by the orchestrator; default to "wm"
    return _sched(agent="wm", delay_ticks=delay_ticks,
                  world_time=world_time, comment=comment)


@tool
def close_invocation(
    summary: str,
    news: Optional[List[str]] = None,
    invoke_self: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """End this invocation with a summary for future context.

    Call this when you have completed your work. The summary is saved
    to your message history and feeds your summary block in pinned
    context. News items are forwarded to the higher agent's newspaper.

    Args:
        summary: 2-4 sentences describing what was achieved.
            This is the ONLY source for your summary block.
        news: 0-3 short items for the higher agent's newspaper.
        invoke_self: Optional scheduling {"delay_ticks": N}
            or {"world_time": T}. May include "comment".
    """
    if not summary or not summary.strip():
        return {"status": "error", "error": "summary is required"}
    return _ci(summary=summary, news=news, invoke_self=invoke_self)


# ======================================================================
# 6.  WM-only tools
# ======================================================================


@tool
def delete_entity(id: str, reason: str = "") -> Dict[str, Any]:
    """Permanently remove an entity. WM-only.

    Past events that reference it remain in the event log.
    Sets a deleted_at marker — the entity record is not physically removed.
    """
    return _store_delete_entity(id=id, reason=reason)


@tool
def set_movement(
    entity_id: str,
    destination: Dict[str, Any],
    speed: float = 1.0,
    comment: str = "",
) -> Dict[str, Any]:
    """Set an entity in motion toward a destination. WM-only.

    The system updates position automatically over ticks.
    If destination is a moving entity, tracks target position each tick.

    Args:
        entity_id: Entity to move.
        destination: Where heading. Same three forms as position.
        speed: World units per tick. 0 = instant teleportation.
        comment: Why this entity is moving.
    """
    return _store_set_movement(
        entity_id=entity_id, destination=destination,
        speed=speed, comment=comment)


@tool
def write_correction(
    message: str,
    entity_id: str = "",
) -> Dict[str, Any]:
    """Send a forward-facing correction to the GM. WM-only.

    Does not retroactively change anything. Injected into GM's context
    on next invocation. Use when WM sees a GM-created entity that
    contradicts world canon.
    """
    # Stub — writes to corrections queue
    import json, uuid
    from pathlib import Path
    path = Path("game") / "entity_store" / "corrections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "entity_id": entity_id,
             "correction_id": uuid.uuid4().hex[:12]}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "correction_logged", "correction_id": entry["correction_id"]}


@tool
def read_tool_doc(name: str = "", parameter: str = "") -> str:
    """Read documentation for any tool or parameter.

    Args:
        name: Tool name (e.g. "set_entity"). Empty = list all tools.
        parameter: Specific parameter name. Empty = full tool doc.
    """
    from world_manager.tool_docs_data import DOCS as _DOCS
    if not name:
        return "Available tools:\n" + "\n".join(f"  - {k}" for k in sorted(_DOCS.keys()))
    doc = _DOCS.get(name)
    if doc is None:
        return f"Tool '{name}' not found."
    if parameter:
        param_doc = doc.get(parameter)
        if param_doc is None:
            return f"Parameter '{parameter}' not found in tool '{name}'."
        return param_doc
    lines = [f"## {name}"]
    for pname, pdoc in doc.items():
        if pname == "_tool":
            lines.append(pdoc)
        else:
            lines.append(f"\n**{pname}:** {pdoc}")
    return "\n\n".join(lines)


# ======================================================================
# 7.  GM-only tools
# ======================================================================


@tool
def notify_wm(message: str, blocking: bool = False) -> Dict[str, Any]:
    """Notify the World Master of a significant event. GM-only.

    Use for events that may affect world-level state: faction changes,
    discovered locations, world-wide phenomena.

    Args:
        message: What the WM should know.
        blocking: True = WM should investigate before next GM call.
    """
    import json, uuid
    from pathlib import Path
    path = Path("game") / "entity_store" / "wm_notifications.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "blocking": blocking,
             "notification_id": uuid.uuid4().hex[:12]}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "gm_to_wm_notified", "notification_id": entry["notification_id"]}


@tool
def escalate_to_sm(message: str) -> Dict[str, Any]:
    """Send instructions to the Scene Manager. GM-only.

    Use to set the next scene, define NPC intents, or provide
    world context the SM needs.

    Args:
        message: Instructions or scene guidance for SM.
    """
    import json, uuid
    from pathlib import Path
    path = Path("game") / "entity_store" / "sm_instructions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "instruction_id": uuid.uuid4().hex[:12]}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "gm_to_sm_instructed", "instruction_id": entry["instruction_id"]}


# ======================================================================
# 8.  SM-only tools
# ======================================================================


@tool
def get_scene(location_id: str) -> Dict[str, Any]:
    """Read current state of a location: who is present, their state, events.

    SM-only. Combines location data, entities at this location,
    and recent events into a single scene snapshot.

    Args:
        location_id: entity_id of the location.

    Returns:
        Dict with location, entities_present, recent_events.
    """
    location = _store_get_entity(location_id)
    if location is None:
        return {"error": f"Location '{location_id}' not found"}

    present = _store_query_entities(
        position={"inside": location_id}, radius=-1.0,
        include_templates=False, limit=100)
    events = _log_query_events(entity_id=location_id, limit=20)

    return {
        "location": location,
        "entities_present": present,
        "recent_events": events,
        "active_scripts_firing": [],
    }


@tool
def escalate_to_gm(question: str, context: str, blocking: bool = True) -> str:
    """Request Game Master involvement. SM-only.

    Use when scene requires new persistent entities or facts
    beyond SM authority.

    Args:
        question: What SM needs from GM.
        context: Relevant scene state.
        blocking: Almost always True for SM.
    """
    import json
    from pathlib import Path
    path = Path("game") / "entity_store" / "gm_questions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps({"question": question, "context": context,
                            "blocking": blocking}, ensure_ascii=False) + "\n")
    return f"[stub] GM notified: {question[:100]}"


# ======================================================================
# 9.  Agent tool sets
# ======================================================================


class ToolRegistry:
    """Factory for agent tool sets. Every tool defined once above."""

    # ── Shared entity tools ──
    _ENTITY_TOOLS = [
        set_entity, set_entity_facts, adjust_entity,
        get_entity, get_entity_facts, get_entity_state,
        query_entities,
        add_tags, remove_tags,
    ]

    # ── Effect tools ──
    _EFFECT_TOOLS = [set_effect, remove_effect]

    # ── Script tools ──
    _SCRIPT_TOOLS = [attach_script, detach_script]

    # ── Event tools ──
    _EVENT_TOOLS = [record_event, query_events]

    # ── Lifecycle tools ──
    _LIFECYCLE_TOOLS = [invoke_self, close_invocation]

    @classmethod
    def for_wm(cls) -> list:
        return [
            *cls._ENTITY_TOOLS,
            *cls._EFFECT_TOOLS,
            *cls._SCRIPT_TOOLS,
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            delete_entity,
            set_movement,
            write_correction,
            read_tool_doc,
        ]

    @classmethod
    def for_gm(cls) -> list:
        return [
            *cls._ENTITY_TOOLS,
            *cls._EFFECT_TOOLS,
            *cls._SCRIPT_TOOLS,
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            notify_wm,
            escalate_to_sm,
        ]

    @classmethod
    def for_sm(cls) -> list:
        return [
            get_entity_facts,
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            get_scene,
            get_entity_state,
            adjust_entity,
            set_effect, remove_effect,
            escalate_to_gm,
        ]
