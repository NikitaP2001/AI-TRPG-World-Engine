"""Shared tool infrastructure — singletons, guards, and helpers for agent tools.

Historically lived in ``gm/tools.py``, extracted so agents (GM, SM, etc.) can
share the same World/Scene instances and turn-lock state without depending on
each other's modules.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from scene import Scene
from world import World


# ---------------------------------------------------------------------------
# Shared singletons
# ---------------------------------------------------------------------------

_WORLD = World()
_SCENE = Scene(world=_WORLD)


# ---------------------------------------------------------------------------
# Turn-lock guardrail
# ---------------------------------------------------------------------------
# After a termination tool finalises a turn, further mutating tool calls
# within the same invocation are rejected.

_TURN_LOCKED: bool = False
_CONTEXT_CHANGED: bool = False


def reset_turn_lock() -> None:
    global _TURN_LOCKED, _CONTEXT_CHANGED
    _TURN_LOCKED = False
    _CONTEXT_CHANGED = False


def is_context_changed() -> bool:
    return _CONTEXT_CHANGED


def is_turn_locked() -> bool:
    return _TURN_LOCKED


def _signal_context_changed() -> None:
    global _CONTEXT_CHANGED
    _CONTEXT_CHANGED = True


# Public alias
signal_context_changed = _signal_context_changed


def _require_turn_unlocked() -> None:
    if _TURN_LOCKED:
        raise ValueError("Turn already finalised; wait for next user message")


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------


def _json(obj: Any) -> str:
    """Compact JSON dump (indent=2)."""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _tool_error(message: str) -> str:
    return f"ERROR: {str(message or '').strip()}".strip()


def _guard_tool(
    tool_name: str,
    allowed_tools_fn: Optional[Callable[[], List[str]]] = None,
    *,
    require_unlocked: bool = False,
    extra_hint: str = "",
) -> Optional[str]:
    """Check tool permissions and turn-lock state.

    Args:
        tool_name: Name of the tool being called.
        allowed_tools_fn: Callable returning the list of currently allowed tool
            names.  If ``None`` the permission check is skipped.
        require_unlocked: If True, also check the turn is not locked.
        extra_hint: Extra text appended to the error message.

    Returns:
        ``None`` if the call is allowed, or an error string if rejected.
    """
    if _CONTEXT_CHANGED:
        raise ValueError(
            f"Context changed during this invocation. Tool '{tool_name}' cannot run. "
            "Wait for the next invocation where tools will be re-bound."
        )

    if allowed_tools_fn is not None:
        allowed = set(allowed_tools_fn())
        if tool_name not in allowed:
            hint = (" " + extra_hint.strip()) if extra_hint.strip() else ""
            allowed_list = ", ".join(sorted(allowed))
            return _tool_error(
                f"Tool '{tool_name}' is not available in the current context.{hint} "
                f"Currently allowed tools: {allowed_list}. "
                "Call one of these tools instead."
            )

    if require_unlocked:
        try:
            _require_turn_unlocked()
        except ValueError as e:
            return _tool_error(str(e))

    return None


# ---------------------------------------------------------------------------
# Override state helpers (used by web UI)
# ---------------------------------------------------------------------------


def _override_state_path() -> str:
    try:
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
