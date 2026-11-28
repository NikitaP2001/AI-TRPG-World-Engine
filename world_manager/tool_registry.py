"""ToolRegistry — single source of truth for all agent tools.

Every tool is defined ONCE here. Each agent gets a subset via classmethods:
  ToolRegistry.for_wm() → List[Tool]  (19 tools)
  ToolRegistry.for_gm() → List[Tool]  (18 tools)

A tool's docstring is its whole specification: what the operation means, and
what every parameter does. Nothing about a tool is documented anywhere else,
and no tool documentation is delivered on demand.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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
    query_page as _store_query_page,
    add_tags as _store_add_tags,
    remove_tags as _store_remove_tags,
    set_effect as _store_set_effect,
    remove_effect as _store_remove_effect,

    adjust_entity as _store_adjust_entity,
)
from world_manager.event_log import (
    create_event as _log_create_event,
    query_events as _log_query_events,
)
from scheduler_queue import (
    delay_seconds as _delay_seconds,
    duration_seconds as _duration_seconds,
    schedule_invocation as _sched,
)


def _world_file(name: str):
    """A file that belongs to the world, beside the entity store it belongs with.

    Anchored where the code lives rather than where the process was started: a
    record appended to a directory the game does not read is a record nobody
    ever reads.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "game" / "entity_store"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


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


# Same for SM→GM: one inline GM invoker, installed by the orchestrator, so the
# scene manager reaches the Game Master the same way in every phase of a session.
_GM_INVOKER = None  # Callable[[str, str], str] — takes question, context


def set_gm_invoker(invoker):
    global _GM_INVOKER
    _GM_INVOKER = invoker


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
    """Create an entity, or change what an existing one is.

    The single write path for what an entity *is*: its identity, its
    attributes, its labels, where it stands. What happens to it during play —
    damage, inventory, position changes, current activity — is adjust_entity;
    what it did is create_event.

    Naming an entity that does not exist creates it; naming one that exists
    updates it. On an update the blocks given are merged or replaced as stated
    below, and blocks left empty stay as they were. A write that contradicts
    the world — an impossible position, a template that does not exist — is
    rejected with the reason and changes nothing.

    Args:
        name: The entity's id. Named entities carry their full name; unnamed
            ones a descriptive slug. Empty creates an entity under a generated
            id, which cannot be addressed until that id is read back.
        type: What kind of thing this is. The vocabulary belongs to the world,
            not to the engine — no type is reserved, and queries match the
            string exactly. Required when creating, where it establishes a type
            nothing else carries. On update, a different type moves the entity
            into that kind, and is accepted only if the world already carries
            entities of it; empty leaves the type as it was.
        base_type: The id of a template this entity inherits from. The
            template's facts, attributes and labels apply unless
            overridden here, field by field, and templates chain. A name the
            world does not hold is refused, since there would be nothing to
            inherit.
        facts: Prose about the entity, for a reader to play it correctly:
            character, present situation, relationships, intentions. Not lore
            and not mechanics — mechanics are the gurps and stats blocks.
            Held to a short paragraph.
        gurps: The attribute block: primary attributes and the values derived
            from them, resistances, power tier, point total. Merged on update,
            one entry per attribute.
        stats: Numeric properties outside GURPS, keyed freely. Merged on
            update. Queries read and write these.
        tags: Labels of the form namespace:value — the query surface for group
            membership. Merged on update.
        position: Where the entity is, in exactly one form:
              coords — an absolute position as [lat, lon], or with height.
              inside — contained in another entity, with an offset from that
                entity's local origin.
              near — approximately beside another entity, with a relative
                offset.
            A form given replaces the whole position; empty leaves it as it
            was.
        state: The dynamic block — hit and fatigue points, conditions,
            injuries. Replaced wholesale on update.
        skills: The skills block, skill mapped to level. Replaced wholesale.
        equipment: The equipment block, item mapped to how it is. Replaced
            wholesale.
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
    """Replace the prose facts of an entity, and nothing else.

    For correcting how an entity is understood without disturbing anything
    mechanical. Facts together with mechanics is set_entity; mechanics alone
    is adjust_entity.

    Args:
        name: The entity's id, exactly as recorded.
        facts: The text that replaces the current facts entirely. A short
            paragraph a reader can act on.
    """
    return _store_set_entity_facts(id=name, facts=facts)


@tool
def get_entity(name: str) -> Dict[str, Any]:
    """Read one entity whole: identity, mechanics, labels, state, position,
    effects and attached behaviours in a single record.

    Facts alone is get_entity_facts; mechanics alone is get_entity_state.

    Args:
        name: The entity's id, exactly as recorded.
    """
    result = _store_get_entity(name)
    if result is None:
        return {"error": f"Entity '{name}' not found"}
    return result


@tool
def get_entity_facts(name: str) -> str:
    """Read the prose facts of an entity, and nothing else.

    Facts are what an agent needs in order to play the entity correctly: who
    they are, what they want, what they are doing now. Mechanical state is read
    with get_entity_state; the whole record with get_entity.

    Args:
        name: The entity's id, exactly as recorded.
    """
    result = _store_get_entity_facts(name)
    if result is None:
        return f"Entity '{name}' not found"
    return result


@tool
def get_entity_state(entity_id: str) -> Dict[str, Any]:
    """Read the mechanical state of an entity.

    What adjudication reads: attributes and derived values, current hit and
    fatigue points, conditions and injuries, skills, equipment, active effects.
    Not the prose facts — those are get_entity_facts.

    Args:
        entity_id: The entity's id, exactly as recorded.
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
    limit: int = 10,
    offset: int = 0,
) -> Dict[str, Any]:
    """Find entities by what they are and where they are.

    Every filter given must hold; an omitted filter does not restrict.
    Structural filters — type, template, labels, position — are exact. Text
    search over facts is approximate and is a fallback for when no structural
    filter expresses the intent.

    The answer has the same four parts whatever the store holds, so it is read
    at the same size in a world of a dozen entities and in one of thousands:

    store    The store as a whole: how many living instances and how many
             templates it contains, and the vocabulary of instance types with
             their counts. Types past the twelve most numerous are folded into
             one further entry, so the vocabulary never outgrows twelve lines.
    matched  How many entities every given filter kept, and the same bounded
             type breakdown for those.
    page     Which slice of the matched set this answer carries: the offset it
             starts at, the limit asked for, and how many entities came back.
    entities The slice itself. Each entity is reduced to its identity, its
             placement and the opening of its facts; the whole record is read
             with get_entity.

    A matched set larger than the slice is therefore countable without being
    listed, and the remainder is reached by repeating the call with a larger
    offset.

    Args:
        type: Keep entities of exactly this type. Empty admits every type.
        base_type: Keep instances of this template, whether directly or
            through a chain.
        tags: Keep entities carrying every listed label.
        position: Centre of a spatial filter. Empty disables it.
        radius: Distance from position. Negative disables spatial filtering.
        facts_search: Keep entities whose facts contain this text.
            Approximate.
        include_templates: True also counts and returns templates themselves;
            False keeps instances only.
        limit: Size of the slice of the matched set carried in the answer.
        offset: Index of the first matched entity in the slice. Later slices
            of a large matched set follow from raising it.
    """
    return _store_query_page(
        type_filter=type, base_type=base_type, tags=tags,
        position=position, radius=radius,
        facts_search=facts_search,
        include_templates=include_templates, limit=limit, offset=offset)


@tool
def add_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Label an entity, without touching anything else.

    Labels already present are ignored. The labels an entity carries are what
    group queries match on — they are how membership is expressed and found.

    Args:
        entity_id: The entity to label.
        tags: The labels to add, each of the form namespace:value. The
            namespace vocabulary belongs to the world, not to the engine.
    """
    return _store_add_tags(entity_id=entity_id, tags=tags)


@tool
def remove_tags(entity_id: str, tags: List[str]) -> Dict[str, Any]:
    """Take labels off an entity, without touching anything else.

    Labels not present are ignored, so repeating the call is harmless.

    Args:
        entity_id: The entity to change.
        tags: The labels to remove.
    """
    return _store_remove_tags(entity_id=entity_id, tags=tags)


def _carries(entity: Optional[Dict[str, Any]], block: str, field: str, value: str) -> bool:
    """Whether the entity already lists this value under that block and field.

    An entity that cannot be read carries nothing: the write that follows then
    reports what it could not do.
    """
    if not isinstance(entity, dict):
        return False
    holder = entity.get(block)
    if not isinstance(holder, dict):
        return False
    listed = holder.get(field)
    if not isinstance(listed, list):
        return False
    return any(str(item).strip() == value for item in listed)


@tool
def adjust_entity(
    entity_id: str,
    gurps: Optional[Dict[str, Any]] = None,
    hp: Optional[int] = None,
    fp: Optional[int] = None,
    er: Optional[int] = None,
    alter_condition: str = "",
    alter_injury: str = "",
    alter_item: str = "",
    alter_ability: str = "",
    alter_skill: str = "",
    currency_delta: Optional[Dict[str, int]] = None,
    current_state: str = "",
    position: Dict[str, Any] = {},
    connections: Dict[str, Any] = {},
    skills: Optional[Dict[str, Any]] = None,
    equipment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Change the runtime state of an entity.

    For state that changes in play — condition, position, inventory, current
    activity. What the entity is belongs to set_entity and set_entity_facts.

    Only the fields given change; the rest of the entity is untouched. Where a
    field is replaced rather than extended, that is stated below.

    Args:
        entity_id: The entity to change.
        gurps: Full replacement of the attribute block. Omitted leaves it.
        hp: Hit points, given as an absolute value rather than a change.
        fp: Fatigue points, absolute.
        er: Energy reserve, absolute.
        alter_condition: A condition — added if it is not there, removed if
            it is.
        alter_injury: The same, for an injury.
        alter_item: The same, for an item of the inventory.
        alter_ability: The same, for a passive ability.
        alter_skill: The same, for a cached skill.
        currency_delta: Change in money by denomination; negative spends.
        current_state: One line describing what the entity is doing now,
            replacing the line before it.
        position: Full replacement of where the entity is, in exactly one of
            three forms:
              coords — an absolute position as [lat, lon].
              inside — within another entity, with an offset.
              near — beside another entity, with an offset.
            Empty leaves the position unchanged.
        connections: Full replacement of a location's exits. Keys are
            connection names; each value describes one exit:
              target — the entity the exit leads to.
              exit_offset — where the exit sits within this location.
              blocks_line_of_sight — whether the exit hides what is beyond.
              description — how the exit presents itself from here.
            Empty leaves connections unchanged.
        skills: Full replacement of the skills block, as skill mapped to
            level. Omitted leaves it.
        equipment: Full replacement of the equipment block. Omitted leaves it.
    """
    named = [str(v or "").strip() for v in
             (alter_condition, alter_injury, alter_item, alter_ability, alter_skill)]
    current = _store_get_entity(entity_id) if any(named) else None
    add: Dict[str, str] = {}
    drop: Dict[str, str] = {}
    for value, block, field, name in (
        (alter_condition, "state", "conditions", "condition"),
        (alter_injury, "state", "injuries", "injury"),
        (alter_item, "equipment", "inventory", "item"),
        (alter_ability, "skills", "passive_abilities", "ability"),
        (alter_skill, "skills", "cached_skills", "skill"),
    ):
        named = str(value or "").strip()
        if not named:
            continue
        (drop if _carries(current, block, field, named) else add)[name] = named

    return _store_adjust_entity(
        entity_id=entity_id, gurps=gurps,
        hp=hp, fp=fp, er=er,
        add_condition=add.get("condition", ""), remove_condition=drop.get("condition", ""),
        add_injury=add.get("injury", ""), remove_injury=drop.get("injury", ""),
        add_item=add.get("item", ""), remove_item=drop.get("item", ""),
        currency_delta=currency_delta,
        add_ability=add.get("ability", ""), remove_ability=drop.get("ability", ""),
        add_skill=add.get("skill", ""), remove_skill=drop.get("skill", ""),
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
    period: str = "",
    duration: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """Set a change that lands again and again until the effect ends.

    For conditions that work by themselves once stated: bleeding, regeneration,
    poison progressing, a fever rising. A change that has to decide something —
    read the world, branch, act — is not an effect.

    Args:
        entity_id: The entity the effect applies to.
        effect_id: Name for this effect on this entity; it is how the effect is
            addressed when removed. Empty generates one, which leaves the
            effect hard to address later.
        stat_deltas: Stats to change, mapped to the amount of one application. A
            stat absent on the entity is created. Negative depletes, positive
            restores.
        period: How much world time passes between applications, written with
            its unit, as in "30s", "1h" or "2d". Empty applies the change once,
            now, and the effect is finished.
        duration: How long the effect lasts by itself, in the same units. Empty
            leaves it in place until it is removed. Applications falling after
            that moment never happen.
        description: What the effect is, in readable terms; it appears wherever
            the entity's state appears.
    """
    period_seconds = 0
    duration_seconds = 0
    for text, label in ((period, "period"), (duration, "duration")):
        if not str(text or "").strip():
            continue
        try:
            seconds = _duration_seconds(text)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        if seconds <= 0:
            return {"status": "error",
                    "error": f"{label} {text!r} is no time at all; leave it out "
                             "rather than naming zero."}
        if label == "period":
            period_seconds = seconds
        else:
            duration_seconds = seconds
    return _store_set_effect(
        entity_id=entity_id, effect_id=effect_id,
        stat_deltas=stat_deltas, period=period_seconds,
        duration=duration_seconds, description=description)


@tool
def remove_effect(entity_id: str, effect_id: str) -> Dict[str, Any]:
    """End a named effect on an entity.

    The effect stops applying from this point; what it already changed stands.

    Args:
        entity_id: The entity carrying the effect.
        effect_id: The effect to end, as named when it was set.
    """
    return _store_remove_effect(entity_id=entity_id, effect_id=effect_id)


# ======================================================================
# 4.  Event tools (WM + GM + SM)
# ======================================================================


@tool
def create_event(
    description: str,
    entity_ids: List[str] = [],
    position: Dict[str, Any] = {},
    tag: str = "",
    causal_parent: str = "",
) -> Dict[str, Any]:
    """State that something took place in the world.

    For what happens between people, over time, or by circumstance — anything
    that took place and that no other tool carries. What stands as a state
    belongs to the tools that state states; what took place belongs here.

    Args:
        description: What took place, said so a later reader can act on it
            without asking anything further: who, what was done, and what came
            of it. Not a label for the kind of thing it was.
        entity_ids: Everything the occurrence involved — those who acted, those
            it was done to, and those who were there to see it. Something left
            out here is not reachable through this occurrence afterwards.
        position: Where it took place. Empty places it where those it involved
            stand, when the world holds them all in one place; an occurrence
            whose participants are scattered, or unknown to the world, is left
            without a place.
        tag: One label under which occurrences of this kind are found together.
        causal_parent: The occurrence this one came out of. Empty when nothing
            it came from is traced.
    """
    return _log_create_event(
        description=description, entity_ids=entity_ids,
        position=position or _place_of(entity_ids), tag=tag,
        causal_parent=causal_parent)


def _place_of(entity_ids: List[str]) -> Dict[str, Any]:
    """Where those an occurrence involved stand, when they stand in one place.

    Read from the store rather than reasoned about: a place is a fact the world
    holds, so an occurrence that names its participants and no place is placed
    with them, and only when they are all in the same place. Everything else
    leaves it unplaced, which is a fact about the occurrence and not a guess.
    """
    from world_manager.entity_core import get_entity

    places: List[str] = []
    for entity_id in entity_ids or []:
        record = get_entity(str(entity_id))
        if not isinstance(record, dict):
            return {}
        held = record.get("position")
        inside = ""
        if isinstance(held, dict):
            inside = str(held.get("inside") or "").strip()
        if not inside:
            return {}
        places.append(inside)
    if not places or len(set(places)) != 1:
        return {}
    return {"inside": places[0]}


@tool
def query_events(
    position: Dict[str, Any] = {},
    radius: float = -1.0,
    z_min: float = -999999.0,
    z_max: float = 999999.0,
    from_time: str = "",
    to_time: str = "",
    entity_id: str = "",
    tag: str = "",
    causal_parent: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read what took place in the world.

    Filters combine with AND; an omitted filter does not restrict. What took
    place is written once and never rewritten, so results are stable within a
    time range.

    Args:
        position: Center of a spatial filter. Empty disables spatial filtering.
        radius: Distance from position. Negative disables spatial filtering.
        z_min: Lower bound of the vertical band; default spans all depths.
        z_max: Upper bound of the vertical band.
        from_time: World time to keep occurrences from (for example : Y1490-06-15 17:45).
            Empty leaves the start unbounded.
        to_time: World time to keep occurrences up to. Empty leaves the end unbounded.
        entity_id: Keep occurrences that involved this entity.
        tag: Keep occurrences under this label.
        causal_parent: Keep occurrences that came out of this event_id — one
            step down the consequence chain.
        limit: Maximum number of occurrences returned.
    """
    return _log_query_events(
        position=position, radius=radius,
        z_min=z_min, z_max=z_max,
        from_time=from_time, to_time=to_time,
        entity_id=entity_id, tag=tag,
        causal_parent=causal_parent, limit=limit)


# ======================================================================
# 5.  Agent lifecycle tools (all agents)
# ======================================================================


@tool
def invoke_self(
    delay: str = "",
    comment: str = "",
) -> Dict[str, Any]:
    """Schedule this agent's own next invocation.

    An agent runs when it is scheduled and not otherwise, so anything that must
    be followed up on belongs in a scheduled invocation.

    Args:
        delay: How long from now the next invocation is wanted, written with its
            unit — minutes, hours or days, as in "30m" or "2d". The wake-up is
            served by the next turn of the world, so it comes at the first moment
            the world reaches that time, which can be later than the delay names.
        comment: What to attend to on the next run. It is read first on
            invocation, so it carries the reason for the wake-up.
    """
    try:
        ticks = _delay_seconds(delay)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    # Agent is set per-call by the orchestrator; default to "wm"
    return _sched(agent="wm", delay_ticks=ticks, comment=comment)


@tool
def close_invocation() -> Dict[str, Any]:
    """End this invocation.

    Scheduling is not part of this call: an invocation that names no next time
    leaves the agent to the world's own upkeep rather than to a wake-up it asked
    for.
    """
    # Check validation hook (the scene manager's opening gate, etc.)
    global CLOSE_VALIDATION_HOOK
    if CLOSE_VALIDATION_HOOK is not None:
        try:
            err_msg = CLOSE_VALIDATION_HOOK()
            if err_msg:
                return {"status": "error", "error": err_msg}
        except Exception as e:
            return {"status": "error", "error": f"Validation error: {e}"}
    return {"status": "invocation_closed"}


# ======================================================================
# 6.  WM-only tools
# ======================================================================


@tool
def delete_entity(name: str, reason: str = "") -> Dict[str, Any]:
    """Remove an entity from the world permanently.

    It stops being addressable and stops appearing in queries. What it did is
    not erased: events that name it stay in the log and stay retrievable
    through it.

    Args:
        name: The entity to remove, by its id.
        reason: Why it is removed; recorded with the removal.
    """
    return _store_delete_entity(id=name, reason=reason)





@tool
def write_correction(
    message: str,
    entity_id: str = "",
) -> Dict[str, Any]:
    """Tell the Game Master that something contradicts the world.

    Forward-only: the narrative continues correctly from here, and nothing
    already told is rewritten. A notification the Game Master raised as
    blocking stays unanswered until a correction answers it.

    Args:
        message: What is wrong and what is true instead, stated plainly enough
            to narrate from without further questions.
        entity_id: The entity concerned, when the correction is about one. The
            record itself is changed with set_entity, not by this call.
    """
    import json, uuid
    from pathlib import Path
    correction_id = uuid.uuid4().hex[:12]

    # Log correction for GM's next invocation
    path = _world_file("corrections.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "entity_id": entity_id,
             "correction_id": correction_id,
             "resolved_notification": True}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "correction_logged", "correction_id": correction_id,
            "info": "Correction logged. Any pending GM escalation is cleared."}


# ======================================================================
# 7.  GM-only tools
# ======================================================================


@tool
def notify_wm(message: str, blocking: bool = False) -> Dict[str, Any]:
    """Raise something with the World Master.

    For what needs the world's structural authority to settle, and for what the
    World Master must know. The message either waits for the World Master's
    next run or brings that run about immediately.

    Args:
        message: What the World Master should know, specific enough to act on.
        blocking: True calls the World Master at once and holds this invocation
            open until it answers; False queues the message and lets this
            invocation end.
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

    # Non-blocking: queued for the World Manager, read on its next invocation
    import json
    from pathlib import Path
    path = _world_file("wm_notifications.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"message": message, "blocking": False,
             "notification_id": notification_id}
    with open(str(path), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "gm_to_wm_notified", "notification_id": notification_id}





# ======================================================================
# 8.  SM-only tools
# ======================================================================


def _entities_standing_in(location_id: str, *, limit: int = 40) -> List[Dict[str, Any]]:
    """What stands in a place, read from the positions the entities carry.

    A place contains what its position says is inside it — directly, or through
    something that is itself in the place, so what sits on a shoulder is in the
    room the shoulder is in — and what is beside something already there. Asking
    the store for a position filter does not answer this: a place is named, the
    spatial index understands coordinates, and a filter the index cannot apply is
    a filter it does not apply — which is how a room used to read back as the
    whole world.
    """
    try:
        from world_manager.entity_store import query_page

        page = query_page(include_templates=False, limit=500)
    except Exception:
        return []
    items = page.get("entities") if isinstance(page, dict) else page
    rows: List[Tuple[Dict[str, Any], str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("id") or "").strip()
        if not entity_id or entity_id == location_id:
            continue
        position = item.get("position") if isinstance(item.get("position"), dict) else {}
        rows.append((
            {
                "id": entity_id,
                "type": str(item.get("type") or "?"),
                "base_type": str(item.get("base_type") or ""),
                "tags": list(item.get("tags") or []),
                "facts": " ".join(str(item.get("facts") or "").split())[:400],
            },
            str(position.get("inside") or "").strip(),
            str(position.get("near") or "").strip(),
        ))

    present: Dict[str, Dict[str, Any]] = {}
    in_place = {location_id}
    for _pass in range(4):
        added = False
        for entry, inside, near in rows:
            if entry["id"] in present:
                continue
            if (inside and inside in in_place) or (near and near in in_place):
                present[entry["id"]] = entry
                in_place.add(entry["id"])
                added = True
        if not added:
            break
    return list(present.values())[:max(1, int(limit))]


@tool
def get_location_info(location_id: str) -> Dict[str, Any]:
    """Read the present state of a location in one snapshot.

    Covers the location's own record, whatever its position places within it —
    who and what stands there, with the facts about them — and the events indexed
    to it, most recent first. Mechanics are not here: what a character's own
    numbers are is read with the tools that own them. What this returns is what
    can be perceived there from a general vantage, not what any single character
    knows.

    Args:
        location_id: The location's id, exactly as recorded.
    """
    location = _store_get_entity(location_id)
    if location is None:
        return {"error": f"Location '{location_id}' not found"}

    events = _log_query_events(entity_id=location_id, limit=10)

    return {
        "location": location,
        "entities_present": _entities_standing_in(location_id),
        "recent_events": events,
    }


@tool
def escalate_to_gm(question: str, context: str = "") -> str:
    """Ask the Game Master what this scene cannot establish on its own.

    The answer returns within this call, from the authority that owns the fact
    in question: campaign facts, distant places, unstated motivation or
    knowledge. It exists for outcomes that turn on facts the scene does not
    settle.

    Args:
        question: The fact needed, stated precisely enough to be answered
            without further exchange.
        context: The scene state the answer has to fit — whatever is needed in
            order to answer without guessing.
    """
    # Inline GM invocation
    global _GM_INVOKER
    if _GM_INVOKER is None:
        return "ERROR: the Game Master is not reachable from this scene."
    try:
        return str(_GM_INVOKER(question, context))
    except Exception as e:
        return f"ERROR: the Game Master failed to answer: {e}"


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

    # ── Event tools ──
    _EVENT_TOOLS = [create_event, query_events]

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
            *cls._EVENT_TOOLS,
            *cls._LIFECYCLE_TOOLS,
            delete_entity,
            write_correction,
        ]

    @classmethod
    def for_gm(cls) -> list:
        return [
            *cls._ENTITY_TOOLS,
            *cls._EFFECT_TOOLS,
            *cls._EVENT_TOOLS,
            # Not _LIFECYCLE_TOOLS: the registry's invoke_self schedules the World
            # Manager by name, so it does not belong in the Game Master's hands.
            # The Game Master has its own invoke_self_gm and both share
            # close_invocation.
            close_invocation,
            notify_wm,
        ]
