"""Per-agent story management — replaces monolithic story.json.

Each agent (SM, GM, WM) has two files:
  {agent}_story.json      — full story data (paragraphs + arcs + ongoing paragraph status)
  {agent}_summaries.json  — active summaries for pinned cache (arc-compressed)

Design invariants:
  - Summaries are NEVER written to messages.json (survive trims).
  - When an arc is generated, its N preceding paragraphs are REMOVED from
    summaries.json (arc compresses them). Paragraphs remain in story.json
    for completeness.
  - build_agent_summaries_block() reads from summaries.json only.
  - PinnedBlockCache invalidates by mtime of summaries.json, not by trim counter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .io import _read_json, _write_json


def _ensure_path(p: Path) -> Path:
    """Coerce str to Path if needed."""
    return Path(p) if isinstance(p, str) else p


# ---------------------------------------------------------------------------
# Default structures
# ---------------------------------------------------------------------------

def _default_summaries() -> Dict[str, Any]:
    return {"paragraphs": [], "arcs": []}


def _default_story() -> Dict[str, Any]:
    return {
        "paragraphs": [],
        "arcs": [],
        "ongoing_paragraph": {
            "turn_count": 0,
            "start_time": "",
            "end_time": "",
            "locations": [],
            "players": [],
        },
    }


def _load_json(path: Path, default_factory) -> Dict[str, Any]:
    """Load JSON or return default if missing/corrupt."""
    path = _ensure_path(path)
    try:
        if path.exists():
            data = _read_json(path)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return default_factory()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_paragraph(
    summaries_path: Path,
    story_path: Path,
    *,
    name: str,
    summary: str,
    turn_count: int = 0,
    start_time: str = "",
    end_time: str = "",
    locations: Optional[List[str]] = None,
    players: Optional[List[str]] = None,
) -> None:
    """Add a completed paragraph to both story and summaries files.

    Args:
        summaries_path: Path to {agent}_summaries.json.
        story_path: Path to {agent}_story.json.
        name: Short paragraph title (3-10 words).
        summary: Paragraph summary text.
        turn_count: Number of turns covered by this paragraph.
        start_time: WorldTime string of paragraph start.
        end_time: WorldTime string of paragraph end.
        locations: Location names involved.
        players: Player/character names involved.
    """
    summaries_path = _ensure_path(summaries_path)
    story_path = _ensure_path(story_path)

    # --- Story: append to paragraphs[], reset ongoing ---
    story = _load_json(story_path, _default_story)
    story.setdefault("paragraphs", []).append({
        "name": name,
        "summary": summary,
        "turn_count": turn_count,
        "start_time": start_time,
        "end_time": end_time,
        "locations": locations or [],
        "players": players or [],
    })
    story["ongoing_paragraph"] = {
        "turn_count": 0,
        "start_time": "",
        "end_time": "",
        "locations": [],
        "players": [],
    }
    _write_json(story_path, story)

    # --- Summaries: append paragraph entry ---
    summaries = _load_json(summaries_path, _default_summaries)
    summaries.setdefault("paragraphs", []).append({
        "name": name,
        "summary": summary,
    })
    _write_json(summaries_path, summaries)


def append_arc(
    summaries_path: Path,
    story_path: Path,
    *,
    name: str,
    summary: str,
    para_count: int = 10,
    start_time: str = "",
    end_time: str = "",
    locations: Optional[List[str]] = None,
    players: Optional[List[str]] = None,
) -> None:
    """Add an arc summary to story + summaries.

    When an arc is added, the *para_count* most recent paragraph entries
    are REMOVED from summaries (arc compresses them).
    The paragraphs remain in story.json for completeness.

    Args:
        summaries_path: Path to {agent}_summaries.json.
        story_path: Path to {agent}_story.json.
        name: Arc title.
        summary: Arc-level summary.
        para_count: Number of paragraphs this arc covers (default 10).
        start_time: WorldTime string of arc start.
        end_time: WorldTime string of arc end.
        locations: Location names involved.
        players: Player/character names involved.
    """
    summaries_path = _ensure_path(summaries_path)
    story_path = _ensure_path(story_path)

    # --- Story: append to arcs[] ---
    story = _load_json(story_path, _default_story)
    story.setdefault("arcs", []).append({
        "name": name,
        "summary": summary,
        "para_count": para_count,
        "start_time": start_time,
        "end_time": end_time,
        "locations": locations or [],
        "players": players or [],
    })
    _write_json(story_path, story)

    # --- Summaries: remove last N paragraphs, append arc ---
    summaries = _load_json(summaries_path, _default_summaries)
    paras = summaries.get("paragraphs", [])
    if len(paras) >= para_count:
        summaries["paragraphs"] = paras[:-para_count]
    else:
        summaries["paragraphs"] = []
    summaries.setdefault("arcs", []).append({
        "name": name,
        "summary": summary,
    })
    _write_json(summaries_path, summaries)


def build_agent_summaries_block(summaries_path: Path) -> Optional[str]:
    """Build a text block from {agent}_summaries.json for pinned cache.

    Returns multiline string (arcs first, then remaining paragraphs),
    or None if no summaries exist.

    This is the builder function passed to PinnedBlockCache.get().
    """
    summaries_path = _ensure_path(summaries_path)
    try:
        data = _read_json(summaries_path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    lines: List[str] = []

    # Arcs first (most compressed, highest priority)
    for arc in (data.get("arcs") or []):
        if isinstance(arc, dict):
            a_name = str(arc.get("name") or "").strip()
            a_summary = str(arc.get("summary") or "").strip()
            if a_name and a_summary:
                lines.append(f"Arc — {a_name}: {a_summary[:500]}")

    # Then remaining (not-yet-arc'd) paragraphs
    for para in (data.get("paragraphs") or []):
        if isinstance(para, dict):
            p_name = str(para.get("name") or "").strip()
            p_summary = str(para.get("summary") or "").strip()
            if p_name and p_summary:
                lines.append(f"{p_name}: {p_summary[:300]}")

    return "\n\n".join(lines) if lines else None
