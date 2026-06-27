"""Agent scheduling queue — persists invoke_self requests across turns.

Storage: game/entity_store/scheduler_queue.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_QUEUE_FILE = Path("game") / "entity_store" / "scheduler_queue.json"


def _load() -> List[Dict[str, Any]]:
    if not _QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(queue: List[Dict[str, Any]]) -> None:
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


def schedule_invocation(
    agent: str,
    delay_ticks: int = -1,
    world_time: int = -1,
    comment: str = "",
) -> Dict[str, Any]:
    """Add a pending agent invocation request.

    Args:
        agent: 'wm', 'gm', or 'sm'.
        delay_ticks: Relative delay from current_tick.
        world_time: Absolute world tick.
        comment: Reason for invocation.
    """
    queue = _load()
    # Resolve tick
    scheduled_tick = world_time if world_time >= 0 else delay_ticks
    entry = {
        "agent": agent,
        "scheduled_tick": scheduled_tick,
        "delay_ticks": delay_ticks,
        "world_time": world_time,
        "comment": comment,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    _save(queue)
    return {"scheduled_tick": scheduled_tick, "comment": comment}


def pop_due_invocations(current_tick: int) -> List[Dict[str, Any]]:
    """Get and remove all invocations scheduled at or before current_tick."""
    queue = _load()
    due = []
    remaining = []
    for entry in queue:
        tick = entry.get("scheduled_tick", -1)
        if 0 <= tick <= current_tick:
            due.append(entry)
        else:
            remaining.append(entry)
    if due:
        _save(remaining)
    return due


def peek_due_invocations(current_tick: int) -> List[Dict[str, Any]]:
    """Read due invocations without removing them."""
    queue = _load()
    return [e for e in queue if 0 <= e.get("scheduled_tick", -1) <= current_tick]
