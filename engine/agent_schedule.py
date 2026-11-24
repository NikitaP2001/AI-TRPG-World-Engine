"""What wakes a cold-path agent (GM, WM).

The scene runs turn by turn; the GM and WM do not. They are called when

  - they asked to be: an invocation that named a delay or an absolute world
    time left a wake-up in the queue (``scheduler_queue``), or
  - nothing asked for them and enough world time has passed since their last
    invocation (the fallback interval below).

This module is the single place that answers "who is due now". It reads the
persisted wake-up queue and each agent's own record of when it was last called;
it calls nobody — the cycle does that, so the decision stays in one place and
the invocation stays a normal invocation.

The fallback exists for the case where an agent closed its invocation without
naming a time: it still has a part of the world to look after, and the world
does not depend on the agent remembering to ask.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.history_meta import HistoryMeta
from scheduler_queue import current_world_tick, pending_invocations, pop_due_invocations

#: Agents that are called from outside the turn loop.
COLD_AGENTS = ("gm", "wm")

#: One day of world time, in ticks (a tick is a second).
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60

#: What a fallback invocation is announced by, when the agent named no reason.
INTERVAL_NOTICE = "interval"


def _game_root(game_root: Optional[Path]) -> Path:
    """Where the world lives: the caller's root, or the repository's own.

    Anchored rather than relative to the working directory, so a record of when
    an agent was last called cannot end up in a directory the game never reads.
    """
    if game_root is not None:
        return Path(game_root)
    return Path(__file__).resolve().parent.parent / "game"


def meta_for(agent: str, game_root: Optional[Path] = None) -> HistoryMeta:
    """The sidecar holding this agent's own record of its invocations."""
    return HistoryMeta(_game_root(game_root) / f"{agent}_history_meta.json")


def interval_seconds() -> int:
    """Fallback cadence in world ticks; ``LLM_WORLD_AGENT_CALL_INTERVAL_SECONDS``."""
    raw = (os.getenv("LLM_WORLD_AGENT_CALL_INTERVAL_SECONDS") or "").strip()
    if raw:
        try:
            value = int(float(raw))
            if value > 0:
                return value
        except Exception:
            pass
    return DEFAULT_INTERVAL_SECONDS


def record_call(agent: str, game_root: Optional[Path] = None) -> None:
    """Note that this agent was invoked just now.

    Called by the agent's own invocation path, so any caller counts: the startup
    step, an escalation, a subscription, or the cycle's wake-up drain.
    """
    from world.time import WorldTime

    tick = current_world_tick()
    time_str = WorldTime.from_seconds(tick).to_string() if tick > 0 else ""
    meta_for(agent, game_root).set_last_call(time_str, tick)


def last_call_tick(agent: str, game_root: Optional[Path] = None) -> int:
    """World tick of this agent's last invocation; 0 when never called."""
    return meta_for(agent, game_root).last_call_tick


def ensure_call_records(game_root: Optional[Path] = None) -> None:
    """Give every cold agent a starting point for its interval.

    The interval is measured from the last invocation, so an agent invoked
    before this record existed has nothing to measure from. The readable
    last-call time is used when it is there; otherwise the world time now is the
    starting point, marked ``last_call_seeded`` because the past cannot be
    recovered and a made-up date must not pass for a record.

    Idempotent: an agent that has a tick already is left alone.
    """
    from world.time import WorldTime

    for agent in COLD_AGENTS:
        meta = meta_for(agent, game_root)
        if meta.last_call_tick > 0:
            continue
        time_str = str(meta.last_call_time or "").strip()
        tick = 0
        if time_str:
            try:
                tick = int(WorldTime.parse(time_str).to_seconds())
            except Exception:
                tick = 0
        seeded = tick <= 0
        if seeded:
            tick = current_world_tick()
        if tick <= 0:
            continue
        meta.set_last_call(time_str or WorldTime.from_seconds(tick).to_string(), tick)
        if seeded:
            data = meta.load()
            data["last_call_seeded"] = True
            meta.save(data)


def due_invocations(
    current_tick: int,
    game_root: Optional[Path] = None,
    *,
    consume: bool = True,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Wake-ups that are due now.

    Returns a list of dicts: ``agent``, ``notice`` (what the agent reads first),
    ``source`` (``"scheduled"`` or ``"interval"``) and ``scheduled_tick``.

    Scheduled entries are removed from the queue when *consume* is true, so a
    wake-up fires once; at most *limit* of them are taken, and the rest wait in
    the queue. An agent that has any queued wake-up — even a later one — gets no
    fallback: it has said when it wants to run.
    """
    pending_agents = {str(e.get("agent") or "") for e in pending_invocations()}
    entries = (
        pop_due_invocations(current_tick, agents=list(COLD_AGENTS), limit=limit)
        if consume
        else [e for e in pending_invocations()
              if 0 <= int(e.get("scheduled_tick") or -1) <= current_tick
              and str(e.get("agent") or "") in COLD_AGENTS]
    )

    out: List[Dict[str, Any]] = []
    due_agents = set()
    for entry in entries:
        agent = str(entry.get("agent") or "").strip()
        if agent not in COLD_AGENTS:
            continue
        due_agents.add(agent)
        comment = str(entry.get("comment") or "").strip()
        out.append({
            "agent": agent,
            "notice": comment or INTERVAL_NOTICE,
            "source": "scheduled",
            "scheduled_tick": int(entry.get("scheduled_tick") or 0),
        })

    interval = interval_seconds()
    for agent in COLD_AGENTS:
        if agent in due_agents or agent in pending_agents:
            continue
        since = last_call_tick(agent, game_root)
        if since <= 0:
            # Never called: there is no interval to measure from. The agent's
            # first invocation is the world's startup step, not this.
            continue
        if current_tick - since >= interval:
            out.append({
                "agent": agent,
                "notice": INTERVAL_NOTICE,
                "source": "interval",
                "scheduled_tick": since + interval,
            })
    return out
