from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .io import _append_jsonl, _read_json, _write_json
from .migrations import ensure_story_schema_v2



def append_turn_to_story(
    *,
    story_json: Path,
    workspace: Path,
    narration: str,
    start_time: str,
    end_time: str,
    location: str,
    characters: List[str],
    npcs: List[str],
) -> None:
    """Append one turn to the ongoing paragraph."""

    try:
        arcs = _read_json(story_json)
    except Exception:
        arcs = None

    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        ensure_story_schema_v2(story_json=story_json)
        try:
            arcs = _read_json(story_json)
        except Exception:
            arcs = None
        if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
            return

    arc0: Dict[str, Any] = arcs[0]
    ongoing = arc0.get("ongoing_paragraph")
    if not isinstance(ongoing, dict):
        ongoing = {
            "start_time": "",
            "turns": [],
            "locations": [],
            "characters": [],
            "npcs": [],
        }

    if not str(ongoing.get("start_time") or "").strip():
        ongoing["start_time"] = str(start_time)

    turns = ongoing.get("turns")
    if not isinstance(turns, list):
        turns = []

    turn_entry = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "location": str(location),
        "narration": narration or "",
        "characters": list(characters or []),
        "npcs": list(npcs or []),
    }
    turns.append(turn_entry)
    ongoing["turns"] = turns

    # Participants (unique, stable order by insertion)
    def _merge_unique(dst: Any, items: List[str]) -> List[str]:
        out: List[str] = []
        if isinstance(dst, list):
            out.extend([str(x) for x in dst if str(x).strip()])
        for it in items:
            s = str(it).strip()
            if s and s not in out:
                out.append(s)
        return out

    ongoing["locations"] = _merge_unique(ongoing.get("locations"), [location])
    ongoing["characters"] = _merge_unique(ongoing.get("characters"), list(characters or []))
    ongoing["npcs"] = _merge_unique(ongoing.get("npcs"), list(npcs or []))

    arcs[0] = arc0

    _write_json(story_json, arcs)

# ---------------------------------------------------------------------------
# Scheduler-callable paragraph finalization
# ---------------------------------------------------------------------------

def finalize_paragraph(
    *,
    story_json: Path,
    paragraph_obj: Dict[str, Any],
    end_time: str,
) -> None:
    """Write a finalized paragraph into the story and optionally trigger arc close.

    Called by the TickScheduler when the paragraph_summary job fires.
    """
    try:
        arcs = _read_json(story_json)
    except Exception:
        arcs = None
    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        return

    arc0: Dict[str, Any] = arcs[0]
    ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0.get("ongoing_paragraph"), dict) else {}
    turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
    end_t = str(end_time or "").strip()

    para = {
        "name": str(paragraph_obj.get("name") or "Summary"),
        "start_time": str(ongoing.get("start_time") or ""),
        "end_time": end_t,
        "locations": ongoing.get("locations") or [],
        "characters": ongoing.get("characters") or [],
        "npcs": ongoing.get("npcs") or [],
        "summary": str(paragraph_obj.get("summary") or ""),
        "turn_count": len(turns),
        "turns": turns,
    }

    paras = arc0.get("paragraphs") if isinstance(arc0.get("paragraphs"), list) else []
    paras.append(para)
    arc0["paragraphs"] = paras

    def _merge_unique(dst: Any, items: List[str]) -> List[str]:
        out: List[str] = []
        if isinstance(dst, list):
            out.extend([str(x) for x in dst if str(x).strip()])
        for it in items:
            s = str(it).strip()
            if s and s not in out:
                out.append(s)
        return out

    # Arc checkpoint: every 10th paragraph
    arc_checkpoint = (len(paras) % 10 == 0)
    if arc_checkpoint:
        agg_locations: List[str] = []
        agg_characters: List[str] = []
        agg_npcs: List[str] = []
        for p in paras:
            if not isinstance(p, dict):
                continue
            agg_locations = _merge_unique(agg_locations, list(p.get("locations") or []))
            agg_characters = _merge_unique(agg_characters, list(p.get("characters") or []))
            agg_npcs = _merge_unique(agg_npcs, list(p.get("npcs") or []))

        finalized_arc_name = (
            str(paragraph_obj.get("arc_name") or "").strip()
            or str(arc0.get("name") or "").strip()
            or "Arc"
        )
        finalized_arc_summary = str(paragraph_obj.get("arc_summary") or "").strip() or str(para.get("summary") or "")

        arc0["name"] = finalized_arc_name
        arc0["summary"] = finalized_arc_summary
        arc0["paragraph_count"] = len(paras)
        if not str(arc0.get("start_time") or "").strip():
            arc0["start_time"] = str(paras[0].get("start_time") or para.get("start_time") or "")
        arc0["end_time"] = str(para.get("end_time") or "")
        arc0["locations"] = agg_locations
        arc0["characters"] = agg_characters
        arc0["npcs"] = agg_npcs

        arc0.pop("ongoing_paragraph", None)
        arcs[0] = arc0
        arcs.insert(
            0,
            {
                "name": "Ongoing Arc",
                "paragraphs": [],
                "ongoing_paragraph": {
                    "start_time": str(end_t),
                    "turns": [],
                    "locations": [],
                    "characters": [],
                    "npcs": [],
                },
            },
        )
    else:
        arc0["ongoing_paragraph"] = {
            "start_time": str(end_t),
            "turns": [],
            "locations": [],
            "characters": [],
            "npcs": [],
        }
        arcs[0] = arc0

    _write_json(story_json, arcs)

    # Log
    try:
        _append_jsonl(
            Path("logs") / "gm_context_summary_updates.jsonl",
            {
                "event": "paragraph_finalized",
                "name": para.get("name"),
                "turn_count": len(turns),
                "summary_length": len(para.get("summary", "")),
                "arc_checkpoint": arc_checkpoint,
                "total_paragraphs": len(paras),
            },
        )
    except Exception:
        pass


def finalize_arc(
    *,
    story_json: Path,
    arc_obj: Dict[str, Any],
) -> None:
    """Update the current arc's name and summary (called by arc_summary job)."""
    try:
        arcs = _read_json(story_json)
    except Exception:
        return
    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        return

    arc0 = arcs[0]
    arc0["name"] = str(arc_obj.get("name") or arc0.get("name") or "Arc")
    arc0["summary"] = str(arc_obj.get("summary") or arc0.get("summary") or "")
    arcs[0] = arc0
    _write_json(story_json, arcs)


# ---------------------------------------------------------------------------
# Pinned-summary builders for agent context injection
# Split by change frequency so stable summaries stay cached longer.
# ---------------------------------------------------------------------------


def build_arc_summaries_block(story_path: Path) -> Optional[str]:
    """Build [arc_summaries] — only finalized arc summaries (~100 turn cadence)."""
    try:
        data = json.loads(story_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None

    lines: List[str] = []
    for arc in data:
        if not isinstance(arc, dict):
            continue
        name = str(arc.get("name") or "").strip()
        summary = str(arc.get("summary") or "").strip()
        if name and summary:
            lines.append(f"[{name}]\n{summary[:500]}")
        # Also include completed paragraphs (already summarized, stable)
        for p in (arc.get("paragraphs") if isinstance(arc.get("paragraphs"), list) else []):
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "").strip()
            psum = str(p.get("summary") or "").strip()
            if pname and psum:
                lines.append(f"  {pname}: {psum[:300]}")
    return "\n\n".join(lines) if lines else None


def build_paragraph_summaries_block(story_path: Path) -> Optional[str]:
    """Build [paragraph_summaries] — ongoing paragraph status (~10 turn cadence)."""
    try:
        data = json.loads(story_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, list):
        return None

    lines: List[str] = []
    for arc in data:
        if not isinstance(arc, dict):
            continue
        arc_name = str(arc.get("name") or "").strip()
        og = arc.get("ongoing_paragraph") if isinstance(arc.get("ongoing_paragraph"), dict) else {}
        if og.get("turns"):
            lines.append(
                f"[{arc_name}] ongoing paragraph: {len(og['turns'])} turns so far"
            )
    return "\n\n".join(lines) if lines else None


def build_gm_summaries_block(history_path: Path) -> Optional[str]:
    """Build [gm_summaries] — GM's own summaries from its history (~10 turn cadence)."""
    try:
        from memory_store import load_history
        history = load_history(history_path)
    except Exception:
        return None

    lines: List[str] = []
    for h in history:
        content = str(h.get("content") or "").strip()
        if content.startswith("[gm_summary:"):
            rest = content[len("[gm_summary:"):]
            bracket_end = rest.find("]")
            if bracket_end != -1:
                pname = rest[:bracket_end].strip()
                psum = rest[bracket_end + 1:].strip()
                if pname and psum:
                    lines.append(f"[GM] {pname}: {psum[:300]}")
    return "\n\n".join(lines) if lines else None


# ---------------------------------------------------------------------------
# PinnedBlockCache — delays pinned-block updates until the next trim.
# ---------------------------------------------------------------------------


class PinnedBlockCache:
    """Caches pinned SystemMessage blocks and only rebuilds on trim.

    Each agent has its own history file and its own trim counter.
    The cache stores built blocks keyed by name + last-seen trim counter.
    On next access, if the trim counter hasn't bumped, the cached block is
    returned — avoiding a cache miss at the LLM provider level.

    Usage::

        cache = PinnedBlockCache(history_path)
        block = cache.get("arc_summaries", story_path, build_arc_summaries_block)
    """

    def __init__(self, history_path: Path) -> None:
        self._history_path = history_path
        self._cache: Dict[str, str] = {}
        self._last_trim: int = -1

    def get(
        self,
        key: str,
        source_path: Path,
        builder,
    ) -> Optional[str]:
        """Return cached block or rebuild if trim counter changed.

        Args:
            key: Cache key (e.g. \"arc_summaries\").
            source_path: Path to the source data file (story.json or history.json).
            builder: Callable(source_path) -> Optional[str].
        """
        from memory_store import get_trim_counter
        current_trim = get_trim_counter(self._history_path)

        # Rebuild if trim counter bumped or source file changed
        if current_trim != self._last_trim:
            self._cache.clear()
            self._last_trim = current_trim

        if key not in self._cache:
            try:
                block = builder(source_path)
                if block:
                    self._cache[key] = block
            except Exception:
                pass

        return self._cache.get(key)

