"""SM-specific tools — scene manager tools for turn resolution and narration.

These are permanent SM tools, callable from any SM context.
They are NOT character tools — they use SM authority to modify the scene.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import tool

# SM-declared intents (for NPCs, world entities, or player intent corrections).
# Reset at the start of each SM turn, consumed by orchestrator at resolution.
_SM_AUTHORED_INTS: List[Dict[str, Any]] = []

#: The hand that can call a character back in the middle of a review, and answer
#: with what he now attempts. The orchestrator holds it for the length of the
#: resolve phase: outside it a management of an attempt is a request applied
#: when the phase ends. Signature: (intent_id, action, cut_after, reason) ->
#: what the review reads back.
_CALL_BACK: Optional[Callable[[str, str, str, str], str]] = None

# The session's narrative, recorded by write_session_narrative and read when the
# session closes. It is the account of the session a later reader gets.
_SESSION_NARRATIVE: List[str] = []


def reset_sm_intents() -> None:
    """Call at start of each SM invocation."""
    _SM_AUTHORED_INTS.clear()


def set_callback_resolver(fn: Optional[Callable[[str, str, str, str], str]]) -> None:
    """Name (or clear) the hand that manages an attempt and takes the new decision.

    Installed by the orchestrator for the length of the resolve phase. While it
    is there, managing an attempt is answered inside the same review: the owner
    is called back and the review is told what he now attempts — under the id it
    can go on to act by.
    """
    global _CALL_BACK
    _CALL_BACK = fn


def has_callback_resolver() -> bool:
    """Is a management of an attempt answered here and now, or when the phase ends?"""
    return _CALL_BACK is not None


def get_sm_intents() -> List[Dict[str, Any]]:
    """Return and clear SM-authored intents for this cycle."""
    result = list(_SM_AUTHORED_INTS)
    _SM_AUTHORED_INTS.clear()
    return result


def get_session_narrative() -> str:
    """The session's narrative as recorded, or '' when none was."""
    return _SESSION_NARRATIVE[-1] if _SESSION_NARRATIVE else ""


def clear_session_narrative() -> None:
    """Forget the session's narrative. Called when a new close begins."""
    _SESSION_NARRATIVE.clear()


def _parse_time_sec(text: str, default: int = 0) -> int:
    """Parse a time string like '0s', '5m', '2h', '1d' to seconds."""
    from scheduler.timeline import parse_duration_seconds
    return parse_duration_seconds(text.strip(), default=default)


@tool
def write_session_narrative(narrative: str) -> str:
    """Record the events a stretch of cycles came to, as a narrative of them.

    Immersive third-person prose narration of what took place in those cycles.

    Args:
        narrative: Those events, narrated in prose.
    """
    text = str(narrative or "").strip()
    if not text:
        return "ERROR: the narrative is empty. Write the events."
    _SESSION_NARRATIVE.append(text)
    return f"ok\nnarrative recorded ({len(text)} characters)"


@tool
def manage_intent(
    intent_id: str,
    action: str,
    reason: str,
    interrupt_at_offset: str = "",
) -> str:
    """Notify a player that he may need to adjust his character intent.

    This is NOT tool to spoiler his intent will fail badly in future, it just a tool to expand
    player knowledge about what his character know or noticed something unexpected, player did not 
    considered on the moment he planned intent.

    The player is invoked right within this cycle given a posibility to adjust 
    his intents. He is told to plan starting from the moment of the begining of the cycle again,
    in case "remove" is used, otherwise he is told to plan like interrupt_at_offset 
    time since cycle start already passed, and he has to plan since then. 
    So interrupt usage like moves the player by interrupt_at_offset time forward,
    and remove usage make him redecide from the same time point.

    In result of this call you will receive new player intents if any and updated expected
    cycle advance estimation, if current player intent changed it.

    Args:
        intent_id: The intent to act on, by the id it carries in the review of
            declared intents.
        action: "remove" discards the intent outright; "interrupt" cuts it
            short from the stated point.
        reason: Why this is happening passed to the player. An interruption must 
            name the resolved event that overrides the intent: this text is what 
            its owner is given when he is called back.
        interrupt_at_offset: For "interrupt" — how much longer the intent
            still runs before it is cut off, as a duration (s, m, h, d).
            Empty cuts it off at once.
    """
    iid = str(intent_id or "").strip()
    mode = str(action or "").strip().lower()
    why = str(reason or "").strip()
    if not iid:
        return "ERROR: name the intent to act on, by the id it carries in the review."
    if mode not in ("remove", "interrupt"):
        return 'ERROR: action must be "remove" or "interrupt".'
    if not why:
        return "ERROR: say why — an interruption is what its owner is told."
    if _CALL_BACK is not None:
        # He is called back here, from the moment the action leaves him standing
        # at, and the review reads his decision as this call's answer.
        return _CALL_BACK(iid, mode, str(interrupt_at_offset or "").strip(), why)
    patch: Dict[str, Any] = {"_patched": True, "id": iid, "_review": mode, "reason": why}
    if mode == "interrupt":
        patch["_cut_after_raw"] = str(interrupt_at_offset or "").strip()
    _SM_AUTHORED_INTS.append(patch)
    if mode == "remove":
        return f"[removed:{iid}] {why}"
    cut = patch["_cut_after_raw"]
    return (f"Attempt {iid} was cut short"
            + (f" after {cut}" if cut else " at once")
            + f" — {why}")


@tool
def declare_intent(
    actor: str,
    action_type: str,
    target: str,
    description: str,
    t_start: str = "0s",
    duration: str = "",
    visibility: str = "public",
    area: Optional[Dict[str, Any]] = None,
    trigger_condition: str = "",
    on_trigger: str = "interrupt_with_new_decision",
    prepared_reaction: str = "",
    absorption: str = "alert",
) -> str:
    """State an intent on behalf of any entity in the scene.

    An intent asserts what an actor means to attempt, and over what span; it
    is not a guarantee and not a record of something already done. The actor
    may be any entity in the scene — a character, a fixture, a location, or
    the environment itself. Declared intents are resolved together with all
    other active intents, so nothing here settles on its own.

    Args:
        actor: The acting entity, by entity id.
        action_type: The kind of behaviour — speech, movement, combat, or
            cosmetic. The kind decides which of the remaining fields are
            meaningful.
        target: What the behaviour is directed at: an entity, "all", "area",
            or "self".
        description: What the actor attempts, in prose. It states the attempt
            and its observable form, not its outcome.
        t_start: When the attempt begins, as a duration from now (s, m, h, d);
            "0s" begins immediately.
        duration: How long the attempt is sustained. Empty means it is
            instantaneous and settles within one segment.
        visibility: "public" makes the attempt apparent to anyone who could
            notice it; "private" means an observer learns of it only if a
            perception check succeeds.
        area: The extent affected when the target is "area".
        trigger_condition: Turns the intent into a standing readiness rather
            than an immediate act: the condition, in words, under which the
            attempt comes to pass. It is held until the condition does.
        on_trigger: What the actor does when the condition is met — interrupt
            the actor for a new decision, or apply the prepared reaction
            automatically instead.
        prepared_reaction: The attempt that replaces this one when the
            condition is met and it is answered by applying the prepared
            reaction rather than by interrupting.
        absorption: How available the actor stays to events beyond its own
            declared condition, for as long as the intent lasts: "alert" keeps
            background awareness, "immersed" commits attention to the attempt
            and answers only to direct threat.
    """
    t_start_sec = _parse_time_sec(t_start, default=0)
    t_end_sec = None
    if duration:
        t_end_sec = t_start_sec + _parse_time_sec(duration, default=0)
    elif not str(trigger_condition or "").strip():
        # No span and nothing to wait for: the attempt is made and settled where
        # it stands, which is what an empty duration says it is.
        t_end_sec = t_start_sec

    intent = {
        "actor": actor,
        "action_type": action_type,
        "target": target,
        "description": description,
        "visibility": visibility,
        "area": area or None,
        "t_start": t_start_sec,
        "t_end": t_end_sec,
        "trigger_condition": str(trigger_condition or "").strip() or None,
        "on_trigger": on_trigger,
        "prepared_reaction": {"description": prepared_reaction} if prepared_reaction.strip() else None,
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
    trigger_condition: str = "",
    on_trigger: str = "",
    absorption: str = "",
) -> str:
    """Change fields of an intent that is already declared.

    Only the fields given are changed; everything else stays as declared.
    The actor, its target and the description of an intent are not editable —
    cancel the intent and declare a new one to change those.

    Args:
        intent_id: The intent to change, as identified in the review of
            declared intents.
        duration: New span for the attempt, as a duration (s, m, h, d).
            Empty keeps the current span.
        visibility: New visibility, "public" or "private". Empty keeps it.
        trigger_condition: The condition in words under which the attempt comes
            to pass. Empty keeps the one it has.
        on_trigger: New response to the condition being met. Empty keeps it.
        absorption: New availability of the actor to outside events, "alert"
            or "immersed". Empty keeps it.
    """
    from scheduler.timeline import parse_duration_seconds
    patch: Dict[str, Any] = {}
    if duration:
        dur_sec = parse_duration_seconds(duration, default=0)
        patch["_duration_raw"] = duration
    if visibility:
        patch["visibility"] = visibility
    if trigger_condition:
        patch["trigger_condition"] = str(trigger_condition).strip() or None
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
def answer_player_question(question_id: str, answer: str) -> str:
    """Answer one question a character asked.

    One call answers one question: the text passed here is the whole of what the
    player receives for that question, and anything written outside the call is
    not delivered. The questions are carried by their ids, and a question left
    unanswered reaches the character as unanswered.

    Args:
        question_id: The id that question carries in the request.
        answer: What that character perceives or recalls, from their own
            perspective and within their knowledge — what they can notice or
            remember, not what is true of the world beyond them.
    """
    return f"[answer recorded: {question_id}]"


@tool
def add_perception(
    player: str,
    perception: str,
    knowledge: List[int] = [],
) -> str:
    """Give a character something to know. This is soft way to tell
    something to a player, that his character noticed.

    What is given here is his from now on, and he reads it when his next cycle
    comes: it is not an interruption, what he is doing now does not change because
    of it, and the cycle it was given in runs on without him. Breaking into the
    cycle a character is already in is a separate act — his attempt cut short —
    and manage_intent is the hand that does it.

    Args:
        player: The player who comes to know it.
        perception: What he perceives, from his point of view within his
            senses and limits.
        knowledge[opt]: optional list of knowledge ids, which are stated as a
            number beside the piece it names ("knowledge 7") wherever the piece
            stands in your context.
    """
    from scene_manager.awareness import ledger

    name = str(player or "").strip()
    if not name:
        return "ERROR: name a player."
    book = ledger()
    said: List[str] = [book.perceive(name, perception)]
    if knowledge:
        said.append(book.give(name, knowledge=knowledge))
    return "\n".join(said)
