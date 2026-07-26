"""SM-specific tools — scene manager tools for turn resolution and narration.

These are permanent SM tools, callable from any SM context.
They are NOT character tools — they use SM authority to modify the scene.
"""

from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.tools import tool


@tool
def proceed_to_narration() -> str:
    """All intents resolved. Narrate the turn.
    
    Only available during the resolve phase — signals the SM is ready
    to move from reviewing intents to producing narration.
    """
    return "ready"


@tool
def publish_narrative_block(
    since_event_id: str,
    narration: str,
    covers_t_range: str,
    duration: str,
) -> str:
    """
    Use this to deliver the resolved action to players as immersive prose.
    This is distinct from record_event, which writes structured facts to
    the event log and does not produce narrative text.

    Call this when you have reviewed the resolved history and are ready
    to describe what happened. The duration determines how much in-game
    time passes.

    Args:
        since_event_id: The resolved-history event id to start from
            (normally the id after the previous publication).
        narration: The assembled narrative text in Russian. Third-person
            objective, novelistic tone, matching character knowledge.
        covers_t_range: Timeline range this block covers, format
            "t_start_sec..t_end_sec" as absolute seconds.
        duration: How much in-game time this block covers.
            Required. Valid units: s, m, h, d.
    """
    return narration


@tool
def end_scene(
    reason: str,
    final_narration: str = "",
    duration: str = "",
    next_location: str = "",
) -> str:
    """Explicitly end the current scene.

    Use when the scene is narratively complete — characters leave,
    a major time skip occurs, or the story moves to a different
    location and context. After calling this, the next cycle will
    determine a new scene.

    Do NOT call this just because nothing happened — use the
    natural structural pause instead (the scheduler jumps T forward
    automatically). Only call this when the scene is truly done.

    Args:
        reason: Why the scene ends.
        final_narration: Optional final narrative text to conclude the scene.
        duration: How much in-game time this conclusion covers. Optional.
            Valid units: s, m, h, d.
        next_location: Hint for where the next scene should take place.
    """
    parts = [f"[end_scene] {reason}"]
    if final_narration:
        parts.append(final_narration)
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
def update_awareness(character_names: List[str], context: str) -> str:
    """Notify players and give them additional context about what is happening.
    They are re-invoked with this information added to their turn context.
    Their declared intents stay unchanged.

    Args:
        character_names: Which players to update.
        context: What they need to know about events they may have missed.
    """
    return f"[awareness:{','.join(character_names)}] {context}"
