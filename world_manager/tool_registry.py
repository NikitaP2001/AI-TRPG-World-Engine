"""ToolRegistry — single source of truth for all agent tools.

Every tool is defined ONCE here. Each agent gets a subset via classmethods:
  ToolRegistry.for_wm() → List[Tool]  (18 tools, no set_entity)
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

    adjust_entity as _store_adjust_entity,
)
from world_manager.event_log import (
    record_event as _log_record_event,
    query_events as _log_query_events,
)
from scheduler_queue import schedule_invocation as _sched


# ======================================================================
# Close-invocation validation hook
# ======================================================================
# Set by GM's call_gm() to prevent exit until character files are complete.
CLOSE_VALIDATION_HOOK = None  # Callable[[], Optional[str]]


def set_close_validation_hook(fn):
    global CLOSE_VALIDATION_HOOK
    CLOSE_VALIDATION_HOOK = fn


# ======================================================================
# Synchronous inline escalation: GM→WM (notify_wm blocking=True)
# and SM→GM (escalate_to_gm)
# ======================================================================
# Inline invoker — set by orchestrator before GM's call_wm().
# When notify_wm(blocking=True) is called, it invokes WM synchronously
# through this callable, just like player observe/recall invokes SM inline.
_WM_INVOKER = None  # Callable[[str], str] — takes message, returns response


def set_wm_invoker(invoker):
    global _WM_INVOKER
    _WM_INVOKER = invoker


# Same for SM→GM: inline GM invoker, set by orchestrator before SM calls.
_GM_INVOKER = None  # Callable[[str, str], str] — takes question, context


def set_gm_invoker(invoker):
    global _GM_INVOKER
    _GM_INVOKER = invoker


# ======================================================================
# Doc-read tracker: prevents agents from re-reading docs already in context
# ======================================================================
# Reset at start of each agent invocation. Keyed by agent name ("wm", "gm", "sm").
_DOC_READ_TRACKER: dict = {}  # agent_key -> set[tool_name]
_CURRENT_AGENT: str = ""


def reset_doc_read_tracker(agent_key: str) -> None:
    """Call at the start of an agent invocation. Clears tracker and sets agent context."""
    global _DOC_READ_TRACKER, _CURRENT_AGENT
    _DOC_READ_TRACKER[agent_key] = set()
    _CURRENT_AGENT = agent_key


def clear_current_agent() -> None:
    """Call after agent invocation ends."""
    global _CURRENT_AGENT
    _CURRENT_AGENT = ""


# ======================================================================
# 1.  Entity tools (WM + GM)
# ======================================================================


@tool
def set_entity(
    name: str = "",
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

    On create (empty name): auto-generates and returns a new identifier.
    On update (existing name): merges gurps/stats, replaces state/skills/equipment.

    Inheritance via base_type: facts, gurps, stats, tags cascade from template.

    Position validation (lat/lon bounds, valid keys) and type-specific entity
    validation (required fields, valid references) are handled automatically
    by the entity store layer.

    Args:
        name: The entity's name. For named characters, use their full name
            (e.g. "Elena Brightwood"). For locations, use a descriptive slug
            (e.g. "tavern_sleeping_dragon"). Empty = auto-generates a new id.
        type: Entity type (e.g. "npc", "character", "location", "faction").
        base_type: Template entity_id to inherit from.
        facts: Static narrative identity. NOT for mechanical data.
        gurps: GURPS stat block.
        stats: Free-form numeric properties.
        tags: Structured labels "namespace:value" for queries.
        position: Where the entity is. Forms (pick only ONE form relevant for this entity):
            {"coords": [lat, lon]} or [lat, lon, z] — absolute world coordinates.
            {"inside": "entity_id", "offset": [x, y, z]} — contained within
            another entity at local 3D coordinates (meters).
            {"near": "entity_id", "offset": [x, y, z]} — approximately
            co-located, offset relative to reference entity.
        state: Dynamic state block {hp, fp, conditions, injuries}.
        skills: Skills block.
        equipment: Equipment block.
    """
    kwargs = dict(id=name, type=type, base_type=base_type, facts=facts,
                  gurps=gurps, stats=stats, tags=tags, position=position)
    if state is not None:
        kwargs["state"] = state
    if skills is not None:
        kwargs["skills"] = skills
    if equipment is not None:
        kwargs["equipment"] = equipment
    return _store_set_entity(**kwargs)


@tool
def set_entity_facts(name: str, facts: str) -> Dict[str, Any]:
    """Update only the prose facts of an entity.

    Does not touch gurps, stats, tags, state, skills, or equipment.

    Args:
        name: The entity's name (e.g. "Elena Brightwood").
        facts: Complete replacement of the facts text.
    """
    return _store_set_entity_facts(id=name, facts=facts)


@tool
def get_entity(name: str) -> Dict[str, Any]:
    """Read a single entity in full, including all blocks.

    Returns all fields: facts, gurps, state, skills, equipment, stats,
    tags, position, scripts, effects, movement.

    Args:
        name: The entity's name (e.g. "Elena Brightwood").
    """
    result = _store_get_entity(name)
    if result is None:
        return {"error": f"Entity '{name}' not found"}
    return result


@tool
def get_entity_facts(name: str) -> str:
    """Read only the prose facts of an entity.

    Lightweight — does not return mechanical stats.
    Use for loading NPC/character context on demand.

    Args:
        name: The entity's name (e.g. "Elena Brightwood").
    """
    result = _store_get_entity_facts(name)
    if result is None:
        return f"Entity '{name}' not found"
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
    gurps: Optional[Dict[str, Any]] = None,
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
    position: Dict[str, Any] = {},
    connections: Dict[str, Any] = {},
    skills: Optional[Dict[str, Any]] = None,
    equipment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply targeted runtime mutations to any entity.

    Unlike set_entity (which creates new entities), this is for changing
    runtime state: HP, conditions, items, position, connections, etc.

    Every parameter is optional. Only provided fields are changed.
    Position, connections, skills, equipment, and gurps are fully replaced.

    Args:
        entity_id: Target entity.
        gurps: Full replacement of GURPS stat block (e.g. {"ST": 11, "DX": 12}).
            None = no change.
        hp/fp/er: New value (absolute set, not delta).
        add/remove_condition: Append/remove one condition string.
        add/remove_injury: Append/remove one injury string.
        add/remove_item: Append/remove one inventory item string.
        currency_delta: Delta map, e.g. {"gold": -5, "silver": 10}.
        add/remove_ability: Append/remove one passive ability.
        add/remove_skill: Append/remove one cached skill.
        current_state: Overwrite one-line current activity description.
        position: Full replacement of position. Use exactly ONE form:
            {"coords": [lat, lon]} — absolute coordinates.
            {"inside": "entity_id", "offset": [x, y, z]} — inside another entity.
            {"near": "entity_id", "offset": [x, y, z]} — near another entity.
            Empty dict = no change.
        connections: Full replacement of location connections. Dict mapping
            connection names to descriptors:
            {"to_kitchen": {"target": "tavern_kitchen",
                            "exit_offset": [x, y, z],
                            "blocks_line_of_sight": true,
                            "description": "door to kitchen"}}
            Empty dict = no change.
        skills: Full replacement of skills block (e.g. {"Broadsword": 14}).
            None = no change.
        equipment: Full replacement of equipment block.
            None = no change.
    """
    return _store_adjust_entity(
        entity_id=entity_id, gurps=gurps,
        hp=hp, fp=fp, er=er,
        add_condition=add_condition, remove_condition=remove_condition,
        add_injury=add_injury, remove_injury=remove_injury,
        add_item=add_item, remove_item=remove_item,
        currency_delta=currency_delta,
        add_ability=add_ability, remove_ability=remove_ability,
        add_skill=add_skill, remove_skill=remove_skill,
        current_state=current_state,
        position=position if position else None,
        connections=connections if connections else None,
        skills=skills,
        equipment=equipment,
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
    invoke_self: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """End this invocation with a summary for future context.

    Call this when you have completed your work. The summary is saved
    to your message history and feeds your summary block in pinned
    context.

    Args:
        summary: 2-4 sentences describing what was achieved.
            This is the ONLY source for your summary block.
        invoke_self: Optional scheduling {"delay_ticks": N}
            or {"world_time": T}. May include "comment".
    """
    if not summary or not summary.strip():
        return {"status": "error", "error": "summary is required"}
    # Check validation hook (GM character file completeness, etc.)
    global CLOSE_VALIDATION_HOOK
    if CLOSE_VALIDATION_HOOK is not None:
        try:
            err_msg = CLOSE_VALIDATION_HOOK()
            if err_msg:
                return {"status": "error", "error": err_msg}
        except Exception as e:
            return {"status": "error", "error": f"Validation error: {e}"}
    return {"status": "invocation_closed", "summary_length": len(summary.strip())}


# ======================================================================
# 6.  WM-only tools
# ======================================================================


@tool
def delete_entity(name: str, reason: str = "") -> Dict[str, Any]:
    """Permanently remove an entity. WM-only.

    Past events that reference it remain in the event log.
    Sets a deleted_at marker — the entity record is not physically removed.

    Args:
        name: The entity's name (e.g. "Elena Brightwood").
        reason: Why — written to event log.
    """
    return _store_delete_entity(id=name, reason=reason)





@tool
def write_correction(
    message: str,
    entity_id: str = "",
) -> Dict[str, Any]:
    """Send a forward-facing correction to the GM. WM-only.

    Does not retroactively change anything. Forward-correction: the
    narrative continues with the corrected facts, nothing is retconned.
    Use when WM sees a GM-created entity that contradicts world canon.

    Also clears any pending blocking escalation from GM's notify_wm —
    GM's close_invocation will then succeed.

    Args:
        message: The correction text — what the entity actually is.
        entity_id: The entity needing correction (optional).
    """
    import json, uuid
    from pathlib import Path
    correction_id = uuid.uuid4().hex[:12]

    # Log correction for GM's next invocation
    path = Path("game") / "entity_store" / "corrections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "entity_id": entity_id,
             "correction_id": correction_id,
             "resolved_notification": True}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "correction_logged", "correction_id": correction_id,
            "info": "Correction logged. Any pending GM escalation is cleared."}


@tool
def read_tool_doc(name: str = "", parameter: str = "") -> str:
    """Read documentation for any tool or parameter.

    Once read, the documentation stays in your conversation context.
    Re-reading the same tool returns a reminder instead — look up.

    Args:
        name: Tool name (e.g. "set_entity"). Empty = list all tools.
        parameter: Specific parameter name. Empty = full tool doc.
    """
    from world_manager.tool_docs_data import DOCS as _DOCS
    if not name:
        return "Available tools:\n" + "\n".join(f"  - {k}" for k in sorted(_DOCS.keys()))
    # Track reads per invocation per agent
    global _CURRENT_AGENT, _DOC_READ_TRACKER
    if _CURRENT_AGENT and name:
        tracker = _DOC_READ_TRACKER.setdefault(_CURRENT_AGENT, set())
        if name in tracker:
            return (f"[already read] Documentation for '{name}' was already retrieved "
                    f"earlier in this invocation — see above in your conversation.")
        tracker.add(name)
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

    Two modes:
      blocking=False (default): message is queued for WM's newspaper digest.
        GM continues immediately — WM sees this on its next regular invocation.

      blocking=True: schedules an immediate WM invocation and blocks GM's
        close_invocation until WM responds with write_correction.
        Use for canon conflicts or situations where GM needs WM's structural
        authority to resolve something before the invocation can end.

    Args:
        message: What the WM should know.
        blocking: True = force immediate WM invocation and wait for response.
    """
    import uuid
    notification_id = uuid.uuid4().hex[:12]

    if blocking:
        # Synchronous inline WM invocation (like player→SM observe/recall)
        global _WM_INVOKER
        if _WM_INVOKER is not None:
            try:
                wm_response = _WM_INVOKER(message)
                return {"status": "wm_responded",
                        "notification_id": notification_id,
                        "wm_response": str(wm_response)[:500]}
            except Exception as e:
                return {"status": "error",
                        "error": f"WM invocation failed: {e}",
                        "notification_id": notification_id}
        else:
            return {"status": "error",
                    "error": "WM invoker not available (orchestrator not set up)."}

    # Non-blocking: queue for WM's newspaper (read on next regular invocation)
    import json
    from pathlib import Path
    path = Path("game") / "entity_store" / "wm_notifications.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "blocking": False,
             "notification_id": notification_id}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "gm_to_wm_notified", "notification_id": notification_id}





# ======================================================================
# 8.  SM-only tools
# ======================================================================


@tool
def get_location_info(location_id: str) -> Dict[str, Any]:
    """Read current state of a location: who is present, their state, events.

    SM-only. Combines location data, entities at this location,
    and recent events into a single snapshot.

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
def escalate_to_gm(question: str, context: str = "") -> str:
    """Ask the Game Master a question. SM-only.

    Invokes GM synchronously inline (like player→SM observe/recall).
    GM sees the question and responds within the same call.

    Args:
        question: What you need from GM.
        context: Relevant scene state.
    """
    import uuid
    escalation_id = uuid.uuid4().hex[:12]
    # Inline GM invocation
    global _GM_INVOKER
    if _GM_INVOKER is not None:
        try:
            gm_response = _GM_INVOKER(question, context)
            return (f"GM responded: {str(gm_response)[:300]}")
        except Exception as e:
            return f"GM invocation failed: {e}"
    else:
        return "GM invoker not available (orchestrator not set up)."


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
            set_entity,
            set_entity_facts, adjust_entity,
            get_entity, get_entity_facts, get_entity_state,
            query_entities,
            add_tags, remove_tags,
            *cls._EFFECT_TOOLS,
            *cls._SCRIPT_TOOLS,
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            delete_entity,
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
        ]

    @classmethod
    def for_sm(cls) -> list:
        return [
            get_entity_facts,
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            get_location_info,
            get_entity_state,
            adjust_entity,
            set_effect, remove_effect,
            escalate_to_gm,
        ]
