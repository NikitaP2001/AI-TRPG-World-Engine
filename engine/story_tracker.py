"""Story progress tracking for the game orchestrator.

Extracted from GameOrchestrator to isolate story-query logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class StoryTracker:
    """Tracks story progress: turns, paragraphs, arc state.

    Depends only on story.json on disk — no agent or world coupling.
    """

    def __init__(self, story_json_path: Path) -> None:
        self._story_json = story_json_path

    def _load_arc0(self) -> Dict[str, Any]:
        """Load arc[0] from story.json."""
        try:
            raw = self._story_json.read_text(encoding="utf-8")
            arcs = json.loads(raw)
            if not isinstance(arcs, list) or not arcs:
                return {}
            arc0 = arcs[0]
            return arc0 if isinstance(arc0, dict) else {}
        except Exception:
            return {}

    def turns_and_paragraphs(self) -> Tuple[int, int]:
        """Return (turns_in_ongoing_paragraph, finalized_paragraphs)."""
        arc0 = self._load_arc0()
        paras_count = 0
        paragraphs = arc0.get("paragraphs") if isinstance(arc0, dict) else None
        if isinstance(paragraphs, list):
            paras_count = len(paragraphs)

        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        if not isinstance(ongoing, dict):
            return (0, paras_count)

        turns = ongoing.get("turns")
        turns_list = turns if isinstance(turns, list) else []
        return (len(turns_list), paras_count)

    def full_progress(self) -> Tuple[int, int, str]:
        """Return (turns_count, paragraphs_count, last_narration_text)."""
        arc0 = self._load_arc0()
        paragraphs = arc0.get("paragraphs") if isinstance(arc0, dict) else None
        paragraphs_list = paragraphs if isinstance(paragraphs, list) else []
        paras_count = len(paragraphs_list)

        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        if not isinstance(ongoing, dict):
            return (0, paras_count, "")

        turns = ongoing.get("turns")
        turns_list = turns if isinstance(turns, list) else []

        turns_count = len(turns_list)
        last_text = ""
        if turns_list:
            last_turn = turns_list[-1] if isinstance(turns_list[-1], dict) else {}
            last_text = str((last_turn or {}).get("narration") or "")
        elif paragraphs_list:
            last_para = paragraphs_list[-1]
            if isinstance(last_para, dict):
                para_turns = last_para.get("turns") if isinstance(last_para.get("turns"), list) else []
                if para_turns:
                    last_turn = para_turns[-1] if isinstance(para_turns[-1], dict) else {}
                    last_text = str((last_turn or {}).get("narration") or "")
                if not last_text:
                    last_text = str(last_para.get("summary") or "")
            else:
                last_text = str(last_para or "")

        return (turns_count, paras_count, last_text)

    def ongoing_paragraph(self) -> Dict[str, Any]:
        """Get the ongoing paragraph dict (or empty)."""
        arc0 = self._load_arc0()
        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        return ongoing if isinstance(ongoing, dict) else {}

    def ongoing_turns(self) -> List[Dict[str, Any]]:
        """Get the list of turns in the ongoing paragraph."""
        ongoing = self.ongoing_paragraph()
        turns = ongoing.get("turns")
        return turns if isinstance(turns, list) else []
