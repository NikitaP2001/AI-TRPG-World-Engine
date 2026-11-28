"""Shared tool infrastructure — singletons, guards, and helpers for agent tools.

Historically lived in ``gm/tools.py``, extracted so agents (GM, SM, etc.) can
share the same World/Scene instances and turn-lock state without depending on
each other's modules.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from world import World


# ---------------------------------------------------------------------------
# Shared singletons
# ---------------------------------------------------------------------------

_WORLD = World()


# ---------------------------------------------------------------------------
# Cycle-lock guardrail
# ---------------------------------------------------------------------------
# After a termination tool finalises a cycle, further mutating tool calls
# within the same invocation are rejected.

_CYCLE_LOCKED: bool = False
_CONTEXT_CHANGED: bool = False


def reset_cycle_lock() -> None:
    global _CYCLE_LOCKED, _CONTEXT_CHANGED
    _CYCLE_LOCKED = False
    _CONTEXT_CHANGED = False


def is_context_changed() -> bool:
    return _CONTEXT_CHANGED


def is_cycle_locked() -> bool:
    return _CYCLE_LOCKED


def _signal_context_changed() -> None:
    global _CONTEXT_CHANGED
    _CONTEXT_CHANGED = True


# Public alias
signal_context_changed = _signal_context_changed
