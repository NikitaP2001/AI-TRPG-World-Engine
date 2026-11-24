"""The world's clock, as the storage layer reads it.

A record is written at a moment of world time, and that moment belongs to the
record: without it nothing written down can say when it happened. This module is
the one place that answers "what time is it in the world" — the timeline cursor
once a cycle has run, the info file for a fresh world, and 0 when there is no
world at all (a test, a scratch directory).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

TIMELINE_FILE = Path(__file__).resolve().parent.parent / "game" / "world" / "timeline.json"
INFO_FILE = Path(__file__).resolve().parent.parent / "game" / "world" / "info.json"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def current_tick() -> int:
    """World time in ticks (seconds); 0 when there is no world to ask."""
    data = _read_json(TIMELINE_FILE)
    if data:
        try:
            cursor = int(data.get("cursor") or 0)
            if cursor > 0:
                return cursor
        except Exception:
            pass

    info = _read_json(INFO_FILE)
    if info:
        time_str = str(info.get("time") or "").strip()
        if time_str:
            try:
                from world.time import WorldTime
                return int(WorldTime.parse(time_str).to_seconds())
            except Exception:
                return 0
    return 0


def now_string() -> str:
    """World time as the readable form used in records; "" when there is none."""
    tick = current_tick()
    if tick <= 0:
        return ""
    try:
        from world.time import WorldTime
        return WorldTime.from_seconds(tick).to_string()
    except Exception:
        return ""
