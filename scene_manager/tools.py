"""Scene Manager tool definitions.

Tools moved from the GM agent because they belong to scene execution
(world planning, turn narration, character correction), not scene picking.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from engine.tool_base import (
    _guard_tool,
    _require_turn_unlocked,
    _json,
    _WORLD,
    _SCENE,
)


# ---------------------------------------------------------------------------
# Scene Manager tools
# ---------------------------------------------------------------------------

@tool
def turn_narration(shared: str, duration: str, personal_json: str = "") -> str:
    """Submit turn narration: shared outcome visible to all plus optional per-player personal parts.
You will see player intents for this turn given via private channel.
They do not know each other intents YET, intents may be unaware of other scene players actions - in that case use tool to correct main contradictions or unacceptable intents within the turn duration.
Then produce story narrative within this turn. 
Include:
- Include relevant perceivable world and parts of declared player intents you would like to accept and their results decided by you considering also Powerplay Prohibited and Prohibit Tricks paragraphs
- Players speech ONLY stated in their intent, if they speech is realistically possible under current state
- Players physical actions stated in intent you want to accept: decide their execution and observable outcome.
- Description of how actions failed if any.
- Visible appearance and state changes caused by events, as well introduce new details of players appearance.
- Elapsed time the player intents consumed.
Never include:
- Any undeclared voluntary player actions or speech not stated in their declared intent.
- Any details which involve not yet directly declared other players events, actions or their results - keep them HIDDEN.
- Meta information and unkown to player characters terms - strictly immersive
Args:
shared: 3rd-person narrative of what happened — all events visible/audible to everyone
    present. Contains no private or secret information.
duration: Elapsed time (e.g. "30s", "5m", "2h")
personal_json: Optional JSON object mapping player names to personal additions —
    only what THAT player exclusively experienced/perceived, not in shared.
    Written in 2nd person ("you"). Omit or pass "" if nothing is private.
    Example: {"Alice": "You feel a sharp chill run down your spine."}
    """
    err = _guard_tool("turn_narration")
    if err:
        return err
    return "ok"


@tool
def correct_character_intents(character_name: str, turn_insight: str) -> str:
    """Ask one character to revise an impossible or contradictory intent before final narration.

    Args:
        character_name: Exact participant name whose current intent must be revised.
        turn_insight: In-world, character-facing notice that explains what they perceive
            now and why their declared intent should be corrected.
    """
    err = _guard_tool("correct_character_intents")
    if err:
        return err
    return "ok"


@tool
def run_scene(
    player_names: List[str],
    location: str,
    scene_npc: Optional[List[str]] = None,
    shared: str = "",
    personal_json: str = "",
) -> str:
    """Create scene description for a new scene at the given location.

    Called by Scene Manager when players move to a new location that differs
    from their previous scene location. Produces scene description only —
    does NOT pick players or location (those are predetermined).

    This is a termination tool — calling it ends the current invocation.

    Args:
        player_names: Players in the scene (predetermined).
        location: Location name. Must already exist in the world.
        scene_npc: All sentient beings present who are not player characters.
            Each must already exist.
        shared: 3rd-person scene description covering what every player can observe.
        personal_json: Optional JSON mapping player names to exclusive personal additions.
    """
    err = _guard_tool("run_scene")
    if err:
        return err

    # --- Validation guards ---
    # All locations and characters/NPCs must be filled with details before a scene can start.
    try:
        loc = _WORLD.get_location(location)
        if not isinstance(loc, dict):
            return (
                f"ERROR: scene could not start without filled locations "
                f"and all characters and npc`s details."
            )
    except Exception:
        return (
            f"ERROR: scene could not start without filled locations "
            f"and all characters and npc`s details."
        )

    # Check character storage exists for all players
    for pname in (player_names or []):
        missing = []
        if not _WORLD.get_character_state(pname):
            missing.append("state")
        if not _WORLD.get_character_skills(pname):
            missing.append("skills")
        if not _WORLD.get_character_equipment(pname):
            missing.append("equipment")
        if missing:
            return (
                f"ERROR: scene could not start without filled locations "
                f"and all characters and npc`s details."
            )

    # Check all NPCs exist
    for npc_name in (scene_npc or []):
        try:
            _WORLD.get_npc(npc_name)
        except Exception:
            return (
                f"ERROR: scene could not start without filled locations "
                f"and all characters and npc`s details."
            )

    return "ok"


@tool
def answer_character(content: str) -> str:
    """Answer a character's question directly from your knowledge or world data.

    Use this when you can resolve the question using world tools (get_location,
    get_npc, get_character_detail, read_character_diary).

    This is a termination tool — calling it ends the current invocation.

    Args:
        content: Your answer to the character's question.
    """
    return "ok"


@tool
def call_gm(notice: str) -> str:
    """Escalate a question to the Game Master when you cannot answer it.

    The GM has access to the full world history and can provide lore, plans,
    hidden details, or resolve ambiguities.

    This is a termination tool — calling it ends the current invocation.

    Args:
        notice: The question or information request to send to the GM.
    """
    return "ok"


@tool
def add_character_to_scene(character_name: str) -> str:
    """Add a player character to the currently active scene.

    The character will be added to the initiative order. Use when a character
    arrives or becomes part of the ongoing scene.

    Args:
        character_name: Name of the character to add.
    """
    err = _guard_tool("add_character_to_scene")
    if err:
        return err
    try:
        result = _SCENE.add_character_to_scene(character_name)
        return _json(result)
    except Exception as e:
        return f"ERROR: {e}"


@tool
def remove_character_from_scene(character_name: str) -> str:
    """Remove a player character from the currently active scene.

    The character is removed from the character list and initiative order.
    Use when a character leaves or exits the ongoing scene.

    Args:
        character_name: Name of the character to remove.
    """
    err = _guard_tool("remove_character_from_scene")
    if err:
        return err
    try:
        result = _SCENE.remove_character_from_scene(character_name)
        return _json(result)
    except Exception as e:
        return f"ERROR: {e}"


@tool
def add_npc_to_scene(npc_name: str) -> str:
    """Add an NPC to the currently active scene.

    The NPC must already exist in the world. Use when an NPC enters the scene.

    Args:
        npc_name: Name of the NPC to add.
    """
    err = _guard_tool("add_npc_to_scene")
    if err:
        return err
    try:
        result = _SCENE.add_npc_to_scene(npc_name)
        return _json(result)
    except Exception as e:
        return f"ERROR: {e}"


@tool
def remove_npc_from_scene(npc_name: str) -> str:
    """Remove an NPC from the currently active scene.

    Use when an NPC leaves or exits the ongoing scene.

    Args:
        npc_name: Name of the NPC to remove.
    """
    err = _guard_tool("remove_npc_from_scene")
    if err:
        return err
    try:
        result = _SCENE.remove_npc_from_scene(npc_name)
        return _json(result)
    except Exception as e:
        return f"ERROR: {e}"


