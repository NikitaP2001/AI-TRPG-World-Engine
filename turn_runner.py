"""Scene management runner for GameOrchestrator.

Simplified from the old SA turn-orchestration: this module only handles
scene-level orchestration (world seed, scene auto-start). SA bookkeeping
is invoked explicitly at trigger points by console_app.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from langchain_core.messages import HumanMessage

from gm.tools import reset_turn_lock
from openrouter_langchain_logging import logs_enabled

if TYPE_CHECKING:
    from console_app import GameOrchestrator as ConsoleApp


def run_turn(app: "GameOrchestrator", user_msg: Optional[HumanMessage]) -> None:
    """Execute one scene-management step.

    Handles world seed bootstrap and scene auto-start.
    Does NOT invoke SA --- SA is called explicitly at trigger points.
    """
    app.world.ensure_initialized()
    app._clear_turn_temp_dir()
    reset_turn_lock()
    if not app._maybe_create_world_setting():
        # World setting required before first turn can proceed
        return

    # Bootstrap next scene if none active — uses SM.determine_next_scene()
    # which returns ALL characters at the target location.
    if app._bootstrap_next_scene():
        return

    # Try to finalize a turn if all characters have acted.
    app._finalize_turn_if_ready()
