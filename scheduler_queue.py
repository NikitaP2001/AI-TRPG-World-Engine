"""Agent scheduling queue — persists invoke_self requests across turns.

Storage: game/entity_store/scheduler_queue.json

An entry is a wake-up: the agent asked to be called again at a world time.
Nothing here decides *whether* to call an agent — engine/agent_schedule.py reads
this and the per-agent last-call record to decide what is due.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_QUEUE_FILE = (Path(__file__).resolve().parent / "game" / "entity_store"
               / "scheduler_queue.json")

#: A delay or a duration as an agent writes it: a number and its unit.
_DELAY_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds"
    r"|m|min|mins|minute|minutes|h|hr|hrs|hour|hours"
    r"|d|day|days)\s*$",
    re.IGNORECASE,
)
_DELAY_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

#: A wake-up closer than this is a mistake nobody meant to make, and one
#: further than this stops being upkeep of the world.
MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 30 * 86400


def duration_seconds(text: str) -> int:
    """The seconds a duration names; a refusal when it names none.

    The unit is required, so a bare number cannot be read back as ticks: it is
    refused rather than guessed at. Nothing is silently defaulted — a duration
    the world reads differently from the way it was written is only discovered
    when it has already passed.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(
            "a duration is required, as a number and its unit — seconds, "
            'minutes, hours or days, such as "30s", "6h", "2d".'
        )
    match = _DELAY_RE.match(raw)
    if not match:
        raise ValueError(
            f"{raw!r} is not a duration. Write a number and its unit — "
            'seconds, minutes, hours or days, such as "30s", "6h", "2d".'
        )
    return int(match.group(1)) * _DELAY_UNITS[match.group(2).lower()]


def delay_seconds(text: str) -> int:
    """Seconds until a wake-up the agent asked for, bounded to what a wake-up is.

    Beyond naming the duration, this refuses one that no wake-up could be: too
    soon to be meant, or so far out that the world would be kept waiting.
    """
    seconds = duration_seconds(text)
    raw = str(text or "").strip()
    if seconds < MIN_DELAY_SECONDS:
        raise ValueError(
            f"delay {raw!r} is under a minute, which is sooner than the world "
            "can come back to anyone."
        )
    if seconds > MAX_DELAY_SECONDS:
        raise ValueError(
            f"delay {raw!r} is over {MAX_DELAY_SECONDS // 86400} days, which is "
            "longer than this world is kept waiting for a wake-up."
        )
    return seconds


def current_world_tick() -> int:
    """World time in ticks (seconds), as the timeline holds it."""
    from world.clock import current_tick

    return current_tick()


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

    Exactly one of the two times is given. A delay is relative to the world
    time now, an absolute time is a world time in ticks.

    Args:
        agent: 'wm', 'gm', or 'sm'.
        delay_ticks: Run after this many ticks from now. Negative means unused.
        world_time: Run at this absolute world tick. Negative means unused.
        comment: Reason for invocation.
    """
    if delay_ticks < 0 and world_time < 0:
        return {"status": "error", "error": "give either delay_ticks or world_time"}

    now = current_world_tick()
    if world_time >= 0:
        scheduled_tick = world_time
        basis = "world_time"
    else:
        scheduled_tick = now + delay_ticks
        basis = "delay"

    queue = _load()
    entry = {
        "agent": agent,
        "scheduled_tick": scheduled_tick,
        "delay_ticks": delay_ticks,
        "world_time": world_time,
        "comment": comment,
        "basis": basis,
        "created_tick": now,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    _save(queue)
    return {"scheduled_tick": scheduled_tick, "basis": basis, "comment": comment}


def pop_due_invocations(
    current_tick: int,
    agents: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Get and remove invocations scheduled at or before current_tick.

    Args:
        current_tick: World tick to compare against.
        agents: When given, only these agents' entries are taken; the rest stay
            queued, so a caller that handles part of the queue does not drop
            what it does not handle.
        limit: When given, at most this many are taken, earliest first; the
            ones left stay queued for the next call rather than being lost.

    An entry that names no time at all is dropped rather than kept forever.
    """
    queue = _load()
    due = []
    remaining = []
    for entry in queue:
        tick = entry.get("scheduled_tick", -1)
        wanted = agents is None or str(entry.get("agent") or "") in agents
        if 0 <= tick <= current_tick and wanted:
            due.append(entry)
        elif tick < 0:
            continue
        else:
            remaining.append(entry)
    if limit is not None and len(due) > limit:
        due.sort(key=lambda e: int(e.get("scheduled_tick") or 0))
        remaining.extend(due[limit:])
        due = due[:limit]
    if len(remaining) != len(queue):
        _save(remaining)
    return due


def pending_invocations() -> List[Dict[str, Any]]:
    """Every queued wake-up, read-only, whatever its time."""
    return _load()


def peek_due_invocations(current_tick: int) -> List[Dict[str, Any]]:
    """Read due invocations without removing them."""
    queue = _load()
    return [e for e in queue if 0 <= e.get("scheduled_tick", -1) <= current_tick]
