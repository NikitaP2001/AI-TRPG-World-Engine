"""SM-specific tools — scene manager tools for turn resolution and narration.

These are permanent SM tools, callable from any SM context.
They are NOT character tools — they use SM authority to modify the scene.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

# SM-declared intents (for NPCs, world entities, or player intent corrections).
# Reset at the start of each SM turn, consumed by orchestrator at resolution.
_SM_AUTHORED_INTS: List[Dict[str, Any]] = []


def reset_sm_intents() -> None:
    """Call at start of each SM invocation."""
    _SM_AUTHORED_INTS.clear()


def get_sm_intents() -> List[Dict[str, Any]]:
    """Return and clear SM-authored intents for this cycle."""
    result = list(_SM_AUTHORED_INTS)
    _SM_AUTHORED_INTS.clear()
    return result


def _parse_time_sec(text: str, default: int = 0) -> int:
    """Parse a time string like '0s', '5m', '2h', '1d' to seconds."""
    from scheduler.timeline import parse_duration_seconds
    return parse_duration_seconds(text.strip(), default=default)


@tool
def proceed_to_narration() -> str:
    """All intents resolved. Narrate the turn.
    
    Only available during the resolve phase — signals the SM is ready
    to move from reviewing intents to producing narration.
    """
    return "ready"


@tool
def publish_narrative_block(
    narration: Dict[str, str],
    factual_summary: str,
) -> str:
    """
    Use this to deliver the resolved action to players as immersive prose.
    This is distinct from record_event, which writes structured facts to
    the event log and does not produce narrative text.

    Call this when you have reviewed the resolved history and are ready
    to describe what happened.

    For each active character, write a short narrative from their
    perspective — what they saw, heard, felt. Write in Russian,
    third-person objective, novelistic tone, matching character knowledge.

    Also write a factual summary: 2-4 dry, objective sentences covering
    only what actually occurred, without personalization.

    Args:
        narration: Dict mapping each active character name to their
            personal narrative. Key = character name, value = what
            this character experienced. Must include all active characters.
        factual_summary: 2-4 dry, objective sentences — only facts,
            no personalization. What actually happened this cycle.
    """
    parts = []
    for name, text in narration.items():
        parts.append(f"[{name}]\n{text}")
    return "\n\n".join(parts)


@tool
def manage_intent(
    intent_id: str,
    action: str,
    reason: str,
    interrupt_at_offset: str = "",
    character_name: str = "",
) -> str:
    """Manage a player's intent — remove it entirely or interrupt it mid-execution.

    Use ``action="remove"`` when the intent was impossible, contradictory,
    or misdirected — it is cancelled entirely and the player is re-invoked.

    Use ``action="interrupt"`` when the intent was valid and partially resolved,
    but new circumstances override it from this point forward. The intent's
    ``t_end`` is set to ``now + interrupt_at_offset``, letting the resolved
    portion stand while cutting off the remainder. The player is re-invoked
    to decide what to do after the interrupt point.

    Interruption is constrained by the player's own ``absorption`` declaration:
    - "alert" players: freely available for any significant event.
    - "immersed" players: only for direct physical threat to that player.

    Args:
        intent_id: The intent to manage.
        action: ``"remove"`` to cancel entirely, ``"interrupt"`` to cut short.
        reason: Why this action is taken. The player sees this.
            Must reference a specific resolved event when interrupting.
        interrupt_at_offset: Only for ``action="interrupt"`` — a duration string
            for how long the intent still runs before being cut off.
            If empty, the interrupt is immediate (t_end = now).
            Valid units: s, m, h, d.
        character_name: The player who owns this intent. If empty, inferred
            from the current turn's declared intents.
    """
    mode = "removed" if action == "remove" else "interrupted"
    extra = f" at +{interrupt_at_offset}" if interrupt_at_offset else ""
    return f"[{mode}:{intent_id}]{extra} {reason}"


@tool
def declare_intent(
    actor: str,
    action_type: str,
    target: str,
    description: str,
    t_start: str = "0s",
    duration: str = "",
    visibility: str = "public",
    is_hostile: bool = False,
    area: Optional[Dict[str, Any]] = None,
    detection_difficulty: Optional[int] = None,
    trigger_condition: Optional[Dict[str, Any]] = None,
    trigger_priority: int = 0,
    on_trigger: str = "interrupt_with_new_decision",
    prepared_reaction: str = "",
    on_timeout: str = "",
    absorption: str = "alert",
) -> str:
    """Declare an intent on behalf of any entity (NPC, world object, or player).

    Use this to give NPCs actions, have environment react, or correct a player
    intent's details. The intent is merged with player-declared intents before
    resolution by the SegmentScheduler.

    Args:
        actor: entity_id of the acting entity (NPC, location, or player).
        action_type: speech / movement / combat / cosmetic.
        target: who or what this action targets.
        description: narrative prose of the action.
        t_start: when it starts. "0s" = now. Suffix: s, m, h, d.
        duration: how long it lasts. Empty = instant (resolves in one segment).
        visibility: "public" or "private".
        is_hostile: only for combat. Default false.
        area: area-of-effect data for target="area".
        detection_difficulty: for private actions with mechanical effect.
        trigger_condition: conditional activation. {"actor": "...", "action_type": "...", "target_match": "..."}.
        trigger_priority: lower fires first. Default 0.
        on_trigger: "interrupt_with_new_decision" or "apply_prepared_reaction".
        prepared_reaction: pre-written sub-intent on trigger.
        on_timeout: sub-intent if duration expires without trigger.
        absorption: "alert" or "immersed".
    """
    t_start_sec = _parse_time_sec(t_start, default=0)
    t_end_sec = None
    if duration:
        t_end_sec = t_start_sec + _parse_time_sec(duration, default=0)

    intent = {
        "actor": actor,
        "action_type": action_type,
        "target": target,
        "description": description,
        "visibility": visibility,
        "is_hostile": is_hostile,
        "area": area or None,
        "detection_difficulty": detection_difficulty,
        "t_start": t_start_sec,
        "t_end": t_end_sec,
        "trigger_condition": trigger_condition or None,
        "trigger_priority": trigger_priority,
        "on_trigger": on_trigger,
        "prepared_reaction": {"description": prepared_reaction} if prepared_reaction.strip() else None,
        "on_timeout": {"description": on_timeout} if on_timeout.strip() else None,
        "absorption": absorption,
        "_origin": "sm",
    }
    _SM_AUTHORED_INTS.append(intent)
    return f"[intent] {actor}: {action_type} on {target} ({visibility})"


@tool
def modify_intent(
    intent_id: str,
    duration: str = "",
    visibility: str = "",
    is_hostile: Optional[bool] = None,
    detection_difficulty: Optional[int] = None,
    trigger_condition: Optional[Dict[str, Any]] = None,
    trigger_priority: Optional[int] = None,
    on_trigger: str = "",
    absorption: str = "",
) -> str:
    """Modify an existing intent by its ID.

    Only provided fields are changed — omitted fields keep their current value.
    To change actor, remove the intent and declare a new one.

    Args:
        intent_id: The intent's id (returned by declare_intent).
        duration: New duration string. Empty = keep current.
        visibility: New visibility.
        is_hostile: New hostility flag.
        detection_difficulty: New detection difficulty.
        trigger_condition: New trigger condition.
        trigger_priority: New priority.
        on_trigger: New trigger response.
        absorption: New absorption level.
    """
    from scheduler.timeline import parse_duration_seconds
    patch: Dict[str, Any] = {}
    if duration:
        dur_sec = parse_duration_seconds(duration, default=0)
        patch["_duration_raw"] = duration
    if visibility:
        patch["visibility"] = visibility
    if is_hostile is not None:
        patch["is_hostile"] = is_hostile
    if detection_difficulty is not None:
        patch["detection_difficulty"] = detection_difficulty
    if trigger_condition is not None:
        patch["trigger_condition"] = trigger_condition or None
    if trigger_priority is not None:
        patch["trigger_priority"] = trigger_priority
    if on_trigger:
        patch["on_trigger"] = on_trigger
    if absorption:
        patch["absorption"] = absorption

    if patch:
        patch["_patched"] = True
        patch["id"] = intent_id
        _SM_AUTHORED_INTS.append(patch)
    return f"[modified intent:{intent_id}]" + (f" -> {', '.join(patch.keys())}" if patch else " no changes")


@tool
def answer_player_question(answer: str) -> str:
    """Respond to a player's observe or recall query.

    Args:
        answer: What the character perceives or recalls,
            from their perspective. Immersive prose in Russian.
    """
    return f"[player_answer] {answer}"


@tool
def update_awareness(character_names: List[str], context: str) -> str:
    """Provide players with their personal full perception narration.
    They are re-invoked with this information added to their turn context.
    Use when relevant, for example where significant or relevant changes 
    around player happen, which his (player`s) character should have 
    noticed, but player stil is not aware about it.

    Args:
        character_names: Which players to update.
        context: immersive full desription from their perspective
    """
    return f"[awareness:{','.join(character_names)}] {context}"
