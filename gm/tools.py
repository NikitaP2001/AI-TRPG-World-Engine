from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from character.agent import run_character_agent
from scene import Scene
from world import World, WorldDuration
from openrouter_langchain_logging import logs_enabled


__all__ = [
    "GM_TOOLS",
    "gm_allowed_tools",
    "gm_tools_for_current_context",
    "reset_turn_lock",
    "is_context_changed",
    "is_turn_locked",
    "signal_context_changed",
    "is_scene_request_pending",
    "clear_scene_request",
    # Tool callables
    "create_location",
    "get_location",
    "update_location",
    "delete_location_path",
    "delete_location",
    "create_npc",
    "get_npc",
    "update_npc",
    "delete_npc_path",
    "delete_npc",
    "get_character_detail",
    "read_character_diary",
    "update_character",
    "add_character",
    "delete_character_path",
    "run_scene",
]


_WORLD = World()
_SCENE = Scene(world=_WORLD)

# Guardrail: after `gm_output_turn` finalizes a turn, we reject further
# turn-advancing / mutating tool calls within the same ReAct invocation.
# This prevents the GM from chaining multiple turns in one graph run.
_TURN_LOCKED: bool = False

# Set when a tool call changes the allowed tools (e.g., all characters ended).
# This signals invoke_once to end the current invocation so a fresh one can start
# with the correct tool bindings.
_CONTEXT_CHANGED: bool = False
_SCENE_REQUESTED: bool = False


def _override_state_path() -> str:
    try:
        # Stored under game/user_inputs so the web UI can coordinate.
        return str((_WORLD.game_root / "user_inputs" / "override_state.json").resolve())
    except Exception:
        return ""


def _override_load() -> Dict[str, Any]:
    path = _override_state_path()
    if not path:
        return {"armed_character": "", "pending_prompt": None, "pending_decision": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"armed_character": "", "pending_prompt": None, "pending_decision": None}


def _override_save(data: Dict[str, Any]) -> None:
    path = _override_state_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Never let override plumbing break the game.
        return


def _override_disarm() -> None:
    data = _override_load()
    data["armed_character"] = ""
    data["pending_prompt"] = None
    data["pending_decision"] = None
    _override_save(data)


def _active_scene_dict() -> Dict[str, Any]:
    try:
        _WORLD.ensure_initialized()
        scene = _WORLD.get_scene()
        return scene if isinstance(scene, dict) else {}
    except Exception:
        return {}


def gm_allowed_tools() -> List[str]:
    """Return the full SA tool list without scene-state gating."""

    return sorted([
        "run_scene",
        "get_location",
        "get_npc",
        "get_character_detail",
        "read_character_diary",
        "update_location",
        "delete_location_path",
        "update_npc",
        "delete_npc_path",
        "delete_npc",
        "update_character",
        "delete_character_path",
        "create_npc",
        "create_location",
    ])


def _require_tool_allowed(tool_name: str, *, extra_hint: str = "") -> None:
    allowed = set(gm_allowed_tools())
    if tool_name not in allowed:
        hint = (" " + extra_hint.strip()) if extra_hint.strip() else ""
        allowed_list = ", ".join(sorted(allowed))
        raise ValueError(
            f"Tool '{tool_name}' is not available in the current context.{hint} "
            f"Currently allowed tools: {allowed_list}. "
            "Call one of these tools instead."
        )


def reset_turn_lock() -> None:
    global _TURN_LOCKED, _CONTEXT_CHANGED
    _TURN_LOCKED = False
    _CONTEXT_CHANGED = False


def is_context_changed() -> bool:
    """Return True if a tool changed the allowed tools since the last reset."""
    return _CONTEXT_CHANGED


def is_turn_locked() -> bool:
    """Return True if gm_output_turn was called and the turn is finalized."""
    return _TURN_LOCKED


def _signal_context_changed() -> None:
    """Signal that the tool context changed.
    
    Sets a flag that invoke_once checks. When set, invoke_once will exit early
    after the current graph invocation completes, allowing a fresh invocation
    with updated tool bindings.
    """
    global _CONTEXT_CHANGED
    _CONTEXT_CHANGED = True


# Public alias for external use (e.g., auto character execution)
signal_context_changed = _signal_context_changed


def is_scene_request_pending() -> bool:
    return bool(_SCENE_REQUESTED)


def clear_scene_request() -> None:
    global _SCENE_REQUESTED
    _SCENE_REQUESTED = False


def _require_turn_unlocked() -> None:
    if _TURN_LOCKED:
        raise ValueError("Turn already finalized; wait for next user message")


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _auto_character_input(scene: Dict[str, Any], character_name: str) -> str:
    """Build character_input from scene description and prior actions."""

    parts: List[str] = []

    scene_desc = str(scene.get("scene_description") or "").strip()
    if scene_desc:
        parts.append(scene_desc)

    chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}

    order = scene.get("initiative_order") if isinstance(scene.get("initiative_order"), list) else []
    if not order:
        order = list(chars.keys()) if isinstance(chars, dict) else []

    # Show actions of characters who already acted (earlier in initiative order).
    prior_lines: List[str] = []
    for name in order:
        if str(name) == str(character_name):
            break
        entry = chars.get(name) if isinstance(chars.get(name), dict) else {}
        if not entry.get("acted"):
            continue
        action = str(entry.get("last_decision") or "").strip()
        if action:
            prior_lines.append(f"{name}: {action}")

    if prior_lines:
        parts.append("Other announced actions this turn:\n" + "\n".join(["- " + x for x in prior_lines]))

    return "\n\n".join([p for p in parts if p.strip()]).strip()


def _tool_error(message: str) -> str:
    return f"ERROR: {str(message or '').strip()}".strip()


def _guard_tool(
    tool_name: str,
    *,
    require_unlocked: bool = False,
    extra_hint: str = "",
) -> Optional[str]:
    """Return an error string if the tool call should be rejected.
    
    Also checks if context has changed (e.g., a prior tool like run_scene changed
    the allowed tool set) and RAISES an exception to immediately stop execution.
    """

    # If context has already changed, raise an exception to stop all tool execution.
    # This prevents parallel tool calls from continuing when context changed.
    if _CONTEXT_CHANGED:
        raise ValueError(
            f"Context changed during this invocation. Tool '{tool_name}' cannot run. "
            f"Wait for the next invocation where tools will be re-bound."
        )

    try:
        _require_tool_allowed(tool_name, extra_hint=extra_hint)
    except Exception as e:  # noqa: BLE001
        return _tool_error(str(e))

    if require_unlocked:
        try:
            _require_turn_unlocked()
        except Exception as e:  # noqa: BLE001
            return _tool_error(str(e))

    return None


@tool
def create_location(name: str, summary: str, details: str, parent_location: str = "") -> str:
    """Create a new location by name with summary + details.

    Args:
        name: Location name.
        summary: Short location summary.
        details: Longer location details.
        parent_location: Optional parent location name (empty by default).
    """

    err = _guard_tool("create_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    loc = _WORLD.create_location(
        name=name,
        summary=summary,
        details=details,
        parent_location=parent_location,
    )
    return _json(loc)


@tool
def get_location(name: str) -> str:
    """Fetch a location object by name."""

    err = _guard_tool("get_location")
    if err:
        return err
    _WORLD.ensure_initialized()
    return _json(_WORLD.get_location(name))


@tool
def update_location(name: str, json_pointer: str, value_json: str) -> str:
    """Upsert a JSON field in a location record using JSON Pointer (creates missing containers)."""

    err = _guard_tool("update_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if ptr in {"/name", ""}:
        return _tool_error("/name cannot be modified via update_location")
    if ptr == "/last_active":
        return _tool_error("/last_active is runtime-managed; it cannot be set via GM tools")
    if ptr in {"/parent_location", "/sublocations_names"}:
        return _tool_error(f"{ptr} is auto-synced from location hierarchy; update parent_location on the child location instead")

    data = _WORLD.add_location_json(name=name, pointer=json_pointer, value=value_json)
    return _json(data)


@tool
def delete_location_path(name: str, json_pointer: str) -> str:
    """Delete a field from a location record using JSON Pointer."""

    err = _guard_tool("delete_location_path", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if ptr == "/":
        return _tool_error("Cannot delete document root")
    if ptr == "/name":
        return _tool_error("/name cannot be deleted")
    if ptr == "/last_active":
        return _tool_error("/last_active is runtime-managed; it cannot be deleted via GM tools")
    if ptr in {"/parent_location", "/sublocations_names"}:
        return _tool_error(f"{ptr} is auto-synced from location hierarchy; delete parent_location on the child location instead")

    if logs_enabled():
        print(f"[trace] delete_location_path: {name} {ptr}")

    data = _WORLD.delete_location_json(name=name, pointer=ptr)
    return _json(data)


@tool
def delete_location(name: str) -> str:
    """Delete a location that no longer exists or is no longer relevant."""

    err = _guard_tool("delete_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    nm = str(name or "").strip()
    if not nm:
        return _tool_error("name is required")

    if logs_enabled():
        print(f"[trace] delete_location: {nm}")

    _WORLD.delete_location(nm)
    return f"Location '{nm}' deleted."


@tool
def create_npc(name: str, location: str, current_state: str, description: str) -> str:
    """Create an NPC and store it in npc.json (GM-controlled).

    Errors if the NPC already exists; use `update_npc` to modify an existing NPC.
    """

    err = _guard_tool("create_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    npc = _WORLD.create_npc(name=name, location=location, current_state=current_state, description=description)

    # If a scene is active, this NPC participates in it.
    try:
        scene = _WORLD.get_scene()
        if isinstance(scene, dict) and scene.get("state") == "active":
            _SCENE.add_npc_to_scene(name)
    except Exception:
        pass

    return _json(npc)


@tool
def get_npc(name: str) -> str:
    """Fetch an NPC object by name."""

    err = _guard_tool("get_npc")
    if err:
        return err
    _WORLD.ensure_initialized()
    return _json(_WORLD.get_npc(name))


@tool
def update_npc(name: str, json_pointer: str, value_json: str) -> str:
    """Upsert a JSON field in an NPC record using JSON Pointer (creates missing containers)."""

    err = _guard_tool("update_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    val_raw = str(value_json or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if not val_raw:
        return _tool_error("value_json is required")

    if ptr in {"/name", ""}:
        return _tool_error("/name cannot be modified via update_npc")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it is synced from the scene when a turn ends")
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it is set automatically when a turn ends")

    if logs_enabled():
        print(f"[trace] update_npc field: {name} {ptr} = {val_raw}")

    data = _WORLD.add_npc_json(name=name, pointer=json_pointer, value=value_json)
    return _json(data)


@tool
def delete_npc_path(name: str, json_pointer: str) -> str:
    """Delete a field from an NPC record using JSON Pointer."""

    err = _guard_tool("delete_npc_path", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it cannot be deleted via GM tools")
    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if ptr == "/":
        return _tool_error("Cannot delete document root")
    if ptr == "/name":
        return _tool_error("/name cannot be deleted")
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be deleted via GM tools")

    if logs_enabled():
        print(f"[trace] delete_npc_path: {name} {ptr}")

    data = _WORLD.delete_npc_json(name=name, pointer=ptr)
    return _json(data)


@tool
def delete_npc(name: str) -> str:
    """Delete an NPC that is no longer relevant and most probably won't appear anymore."""

    err = _guard_tool("delete_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    nm = str(name or "").strip()
    if not nm:
        return _tool_error("name is required")

    if logs_enabled():
        print(f"[trace] delete_npc: {nm}")

    _WORLD.delete_npc(nm)
    return f"NPC '{nm}' deleted."


@tool
def get_character_detail(name: str) -> str:
    """Fetch a character's full description.json."""

    err = _guard_tool("get_character_detail")
    if err:
        return err
    _WORLD.ensure_initialized()
    return _json(_WORLD.get_character_description(name))


@tool
def read_character_diary(name: str) -> str:
    """Read a character's private diary — their personal summary of past experiences.

    Returns the diary content including arc summaries and recent paragraphs.
    """
    err = _guard_tool("read_character_diary")
    if err:
        return err

    try:
        from character.reflection import load_diary
        diary = load_diary(name)
    except Exception as exc:
        return _tool_error(f"Failed to load diary for '{name}': {exc}")

    arc_summaries = diary.get("arc_summaries") or []
    paragraphs = diary.get("paragraphs") or []

    if not arc_summaries and not paragraphs:
        return f"Character '{name}' has no diary entries yet."

    return _json({"arc_summaries": arc_summaries, "paragraphs": paragraphs})


@tool
def update_character(name: str, json_pointer: str, value_json: str) -> str:
    """Upsert a JSON field in a character description using JSON Pointer (creates missing containers)."""

    err = _guard_tool("update_character", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be set via GM tools")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it is synced from the scene when a turn ends")

    try:
        data = _WORLD.add_character_json(name=name, pointer=json_pointer, value=value_json)
    except Exception as e:  # noqa: BLE001
        return _tool_error(str(e))
    return _json(data)


@tool
def add_character(name: str, json_pointer: str, value_json: str) -> str:
    """Add/create a JSON field in a character description using JSON Pointer (creates missing containers)."""

    err = _guard_tool("add_character", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be set via GM tools")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it is synced from the scene when a turn ends")

    err = _guard_tool("add_character", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    if str(json_pointer or "").strip() == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be set via GM tools")

    try:
        data = _WORLD.add_character_json(name=name, pointer=json_pointer, value=value_json)
    except Exception as e:  # noqa: BLE001
        return _tool_error(str(e))
    return _json(data)


@tool
def delete_character_path(name: str, json_pointer: str) -> str:
    """Delete a field from a character description using JSON Pointer.

    Use this to remove outdated or wrong data cleanly instead of overwriting
    with empty strings.
    """

    err = _guard_tool("delete_character_path", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if ptr == "/":
        return _tool_error("Cannot delete document root")
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be deleted via GM tools")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it cannot be deleted via GM tools")

    if logs_enabled():
        print(f"[trace] delete_character_path: {name} {ptr}")

    data = _WORLD.delete_character_json(name=name, pointer=ptr)
    return _json(data)


@tool
def run_scene() -> str:
    """Request scene progression.

    Actual scene selection/description/start is handled by Python orchestration
    immediately after this tool call.
    """

    try:
        _require_turn_unlocked()
    except Exception as e:  # noqa: BLE001
        return _tool_error(str(e))
    _WORLD.ensure_initialized()

    try:
        _require_tool_allowed("run_scene", extra_hint="Run scene only when there is no active scene.")
    except Exception as e:  # noqa: BLE001
        return _tool_error(str(e))

    global _SCENE_REQUESTED

    try:
        existing = _WORLD.get_scene()
    except Exception:
        existing = {}

    if isinstance(existing, dict) and str(existing.get("state") or "").strip() == "active":
        return _tool_error("A scene is already active")

    _SCENE_REQUESTED = True
    _signal_context_changed()
    return "Scene requested. Orchestration will pick, describe, and start the scene immediately."


# gm_output_turn has been removed. Turn finalization is now handled directly
# in console_app.py by invoking the Game Master with TURN_NARRATION task.


GM_TOOLS = [
    create_location,
    get_location,
    update_location,
    delete_location_path,
    delete_location,
    create_npc,
    update_npc,
    delete_npc_path,
    delete_npc,
    get_character_detail,
    read_character_diary,
    update_character,
    add_character,
    delete_character_path,
    run_scene,
]


def gm_tools_for_current_context() -> List[Any]:
    """Return full SA tool bindings (no scene-state filtering)."""
    return list(GM_TOOLS)
