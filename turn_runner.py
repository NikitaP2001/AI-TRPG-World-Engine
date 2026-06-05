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
    app._ensure_sa_bootstrap()

    if app._maybe_run_world_seed():
        return

    # Hard guard: do not progress simulation while world time is still placeholder.
    try:
        if app.world.get_world_time().to_string() == "Y0000-01-01 00:00:00":
            if logs_enabled():
                print("[trace] run_turn: world time still default; waiting for valid world_seed time")
            return
    except Exception:
        pass

    # Auto-start next scene if needed (no SA involvement).
    if app._auto_start_next_scene():
        return

    # Try to finalize a turn if all characters have acted.
    app._finalize_turn_if_ready()
