"""4D Event Log — append-only JSONL store for record_event / query_events.

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
from pathlib import Path
from typing import Any, Dict, List, Optional

from openrouter_langchain_logging import logs_enabled


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

_EVENT_DIR = Path("game") / "entity_store"
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
        "updated_at": datetime.now(timezone.utc).isoformat(),
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


def record_event(
    description: str,
    entity_ids: Optional[List[str]] = None,
    position: Optional[Dict[str, Any]] = None,
    t_override: int = -1,
    tag: str = "",
    narrative_flag: bool = False,
    causal_parent: str = "",
    current_tick: int = 0,
) -> Dict[str, Any]:
    """Write an event to the 4D causal log. Append-only.

    Args:
        description: What happened.
        entity_ids: Entities causally involved.
        position: Where it happened.
        t_override: Specific tick. -1 = current_tick.
        tag: Single categorical label.
        narrative_flag: True = appears in GM/SM newspaper.
        causal_parent: event_id that caused this one.
        current_tick: Current world tick (provided by orchestrator).
    """
    _load()
    event_id = _generate_event_id()
    tick = t_override if t_override >= 0 else current_tick

    event = {
        "event_id": event_id,
        "description": description,
        "entity_ids": entity_ids or [],
        "position": position or {},
        "tick": tick,
        "tag": tag,
        "narrative_flag": narrative_flag,
        "causal_parent": causal_parent,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    _events.append(event)
    global _dirty
    _dirty = True
    _save()

    if logs_enabled():
        print(f"[event_log] {event_id}: {tag} @ tick {tick}")

    return {
        "event_id": event_id,
        "logged_at_tick": tick,
        "position_indexed": bool(position),
        "warnings": [],
    }


def query_events(
    position: Optional[Dict[str, Any]] = None,
    radius: float = -1.0,
    z_min: float = -999999.0,
    z_max: float = 999999.0,
    t_from: int = -1,
    t_to: int = -1,
    entity_id: str = "",
    tag: str = "",
    narrative_flag_only: bool = False,
    causal_parent: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read from the 4D causal event log."""
    _load()
    results = []

    for ev in _events:
        # Time filter
        ev_tick = ev.get("tick", 0)
        if t_from >= 0 and ev_tick < t_from:
            continue
        if t_to >= 0 and ev_tick > t_to:
            continue

        # Tag filter
        if tag and ev.get("tag", "") != tag:
            continue

        # Entity filter
        if entity_id and entity_id not in ev.get("entity_ids", []):
            continue

        # Narrative flag filter
        if narrative_flag_only and not ev.get("narrative_flag", False):
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
            "tick": ev.get("tick", 0),
            "tag": ev.get("tag", ""),
            "narrative_flag": ev.get("narrative_flag", False),
            "causal_parent": ev.get("causal_parent", ""),
        })

        if len(results) >= limit:
            break

    return results
