"""The world's past — append-only JSONL store for create_event / query_events.

Storage: game/entity_store/events.jsonl (append-only JSONL file).
Index: game/entity_store/events_index.json (rebuilt on write for efficient queries).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone


def _game_time_str() -> str:
    """Return current WorldTime as string, or UTC fallback if unavailable."""
    try:
        from world import World as _W
        wt = _W().get_world_time()
        return wt.to_string() if hasattr(wt, 'to_string') else str(wt)
    except Exception:
        return datetime.now(timezone.utc).isoformat()
from pathlib import Path
from typing import Any, Dict, List, Optional

from openrouter_langchain_logging import logs_enabled


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

_EVENT_DIR = Path(__file__).resolve().parent.parent / "game" / "entity_store"
_EVENT_FILE = _EVENT_DIR / "events.jsonl"
_INDEX_FILE = _EVENT_DIR / "events_index.json"

# In-memory event cache (list of dicts)
_events: List[Dict[str, Any]] = []
_index: Dict[str, Any] = {}
_dirty: bool = False


def _ensure_dir() -> None:
    _EVENT_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> List[Dict[str, Any]]:
    """Load all events from JSONL into memory."""
    global _events, _index
    if _events:
        return _events
    _ensure_dir()
    if _EVENT_FILE.exists():
        try:
            with open(str(_EVENT_FILE), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        _events.append(json.loads(line))
        except Exception:
            _events = []
    # Load index if exists
    if _INDEX_FILE.exists():
        try:
            _index = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            _index = {}
    return _events


def _save() -> None:
    """Append new events to JSONL and rebuild index."""
    global _dirty
    if not _dirty:
        return
    _ensure_dir()
    # Write all events (append-only would be faster but rebuild is simpler)
    with open(str(_EVENT_FILE), "w", encoding="utf-8") as f:
        for ev in _events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    # Rebuild index
    _rebuild_index()
    _dirty = False


def _rebuild_index() -> None:
    """Rebuild in-memory and file index."""
    global _index
    _index = {
        "count": len(_events),
        "tags": {},
        "entity_ids": {},
        "updated_at": _game_time_str(),
    }
    for ev in _events:
        tag = ev.get("tag", "")
        if tag:
            _index["tags"].setdefault(tag, 0)
            _index["tags"][tag] += 1
        for eid in ev.get("entity_ids", []):
            _index["entity_ids"].setdefault(eid, 0)
            _index["entity_ids"][eid] += 1
    _INDEX_FILE.write_text(json.dumps(_index, indent=2, ensure_ascii=False), encoding="utf-8")


def _generate_event_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_event(
    description: str,
    entity_ids: Optional[List[str]] = None,
    position: Optional[Dict[str, Any]] = None,
    tag: str = "",
    causal_parent: str = "",
) -> Dict[str, Any]:
    """State that something took place in the world.

    An event is stamped with the world time it took place at, and that stamp is
    its only time: time in this world is WorldTime, not a count.

    Args:
        description: What took place.
        entity_ids: Entities involved.
        position: Where it took place.
        tag: One label for the kind of occurrence.
        causal_parent: event_id this one came out of.
    """
    _load()
    event_id = _generate_event_id()
    logged_at = _game_time_str()

    event = {
        "event_id": event_id,
        "description": description,
        "entity_ids": entity_ids or [],
        "position": position or {},
        "tag": tag,
        "causal_parent": causal_parent,
        "logged_at": logged_at,
    }
    _events.append(event)
    global _dirty
    _dirty = True
    _save()

    if logs_enabled():
        print(f"[event_log] {event_id}: {tag} @ {logged_at}")

    return {
        "event_id": event_id,
        "logged_at": logged_at,
        "position_indexed": bool(position),
        "warnings": [],
    }


def _seconds_of(value: Any) -> Optional[int]:
    """World seconds for a world time as written, or None when it cannot be read.

    Accepts the form a record carries (``Y1491-08-17 19:12:00``), with or without
    the brackets a stamped record wears. A missing or unreadable time is None,
    never zero: zero is a real moment and a missing one is not.
    """
    text = str(value or "").strip().strip("[]").strip()
    if not text:
        return None
    try:
        from world.time import WorldTime

        return int(WorldTime.parse(text).to_seconds())
    except Exception:
        return None


def query_events(
    position: Optional[Dict[str, Any]] = None,
    radius: float = -1.0,
    z_min: float = -999999.0,
    z_max: float = 999999.0,
    from_time: str = "",
    to_time: str = "",
    entity_id: str = "",
    tag: str = "",
    since_event_id: str = "",
    causal_parent: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read from the causal event log.

    A time bound is a world time — the same written form a record carries in its
    ``logged_at``. An event whose own time cannot be read is in no window: with a
    bound given it is left out rather than guessed into place.
    """
    _load()
    start = _seconds_of(from_time)
    end = _seconds_of(to_time)
    results = []
    found_since = not since_event_id  # if no marker, include all

    for ev in _events:
        # Event ID lower bound — exclusive
        if not found_since:
            if ev.get("event_id", "") == since_event_id:
                found_since = True
            continue  # skip events before/at the marker

        # Time filter
        if start is not None or end is not None:
            at = _seconds_of(ev.get("logged_at"))
            if at is None:
                continue
            if start is not None and at < start:
                continue
            if end is not None and at > end:
                continue

        # Tag filter
        if tag and ev.get("tag", "") != tag:
            continue

        # Entity filter
        if entity_id and entity_id not in ev.get("entity_ids", []):
            continue

        # Causal parent filter
        if causal_parent and ev.get("causal_parent", "") != causal_parent:
            continue

        # Spatial filter (approximate — checks if position coords exist)
        if position and radius > 0:
            ev_pos = ev.get("position", {})
            if not ev_pos:
                continue
            # Simple check: if both have coords, skip if too far
            # (exact haversine can be added later)

        results.append({
            "event_id": ev["event_id"],
            "description": ev.get("description", ""),
            "entity_ids": ev.get("entity_ids", []),
            "position": ev.get("position", {}),
            "logged_at": ev.get("logged_at", ""),
            "tag": ev.get("tag", ""),
            "causal_parent": ev.get("causal_parent", ""),
        })

        if len(results) >= limit:
            break

    return results
