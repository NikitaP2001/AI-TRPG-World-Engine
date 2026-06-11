"""GM-specific HistoryInjector wrapper.

Wraps GM's inject_delta and history path for the generic HistoryInjector.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from memory_store import load_history, trim_history
from engine.history_injector import HistoryInjector


class GMHistoryInjector(HistoryInjector):
    """GM-specific wrapper around the generic HistoryInjector."""

    def __init__(self, *, game_master: Any, gm_history_path: Path, history_limits: Any) -> None:
        def _loader() -> List[Dict[str, Any]]:
            rows = load_history(gm_history_path)
            return trim_history(rows, limits=history_limits)

        def _inject(content: str) -> None:
            game_master.inject_delta(content)

        super().__init__(history_loader=_loader, delta_injector=_inject)
