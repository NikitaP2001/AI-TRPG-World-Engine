"""History metadata sidecar — message tagging + summarization tracking.

Lives alongside any agent's message history JSON file as a sidecar
(e.g. ``game/gm_history_meta.json`` for the GM).

Tags each appended message as ``auto_injection`` or ``interaction`` so the
summarization system can distinguish real events from maintenance context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class HistoryMeta:
    """Manages history meta JSON — message tagging + summarization state."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {
            "entries": [],
            "invocation_count": 0,
            "last_summarized_at_idx": -1,
            "last_summarized_paragraph": "",
            "paragraph_count": 0,
        }

    def save(self, data: Dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Entry management
    # ------------------------------------------------------------------

    def append_entry(self, type_: str, label: str) -> int:
        """Append a history entry tag and return its index."""
        data = self.load()
        entries = data.setdefault("entries", [])
        idx = len(entries)
        entries.append({"idx": idx, "type": type_, "label": str(label or "")[:120]})
        self.save(data)
        return idx

    def get_real_interactions_since(self, last_idx: int) -> List[Dict[str, Any]]:
        """Return ``interaction`` entries after *last_idx* (exclusive)."""
        data = self.load()
        return [
            e for e in data.get("entries", [])
            if e["idx"] > last_idx and e["type"] == "interaction"
        ]

    # ------------------------------------------------------------------
    # Invocation counting
    # ------------------------------------------------------------------

    def increment_invocation(self) -> int:
        """Increment invocation count, return new count."""
        data = self.load()
        data["invocation_count"] = data.get("invocation_count", 0) + 1
        self.save(data)
        return data["invocation_count"]

    def increment_paragraph(self) -> int:
        """Increment paragraph count, return new count."""
        data = self.load()
        data["paragraph_count"] = data.get("paragraph_count", 0) + 1
        self.save(data)
        return data["paragraph_count"]

    # ------------------------------------------------------------------
    # Summarization state
    # ------------------------------------------------------------------

    def mark_summarized(self, paragraph_name: str) -> None:
        """Mark the current position as summarized."""
        data = self.load()
        entries = data.get("entries", [])
        data["last_summarized_at_idx"] = len(entries) - 1 if entries else 0
        data["last_summarized_paragraph"] = paragraph_name
        self.save(data)

    @property
    def invocation_count(self) -> int:
        return self.load().get("invocation_count", 0)

    @property
    def paragraph_count(self) -> int:
        return self.load().get("paragraph_count", 0)

    @property
    def last_summarized_paragraph(self) -> str:
        return self.load().get("last_summarized_paragraph", "")

    # ------------------------------------------------------------------
    # Last call time tracking
    # ------------------------------------------------------------------

    def set_last_call_time(self, time_str: str) -> None:
        data = self.load()
        data["last_call_time"] = str(time_str or "")
        self.save(data)

    @property
    def last_call_time(self) -> str:
        return self.load().get("last_call_time", "")
