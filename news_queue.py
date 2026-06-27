"""NewsQueue — per-agent FIFO news accumulation.

Lower agents append news items. Orchestrator consumes when invoking higher agent.
Queue is file-backed (JSONL) so it survives restarts.

News flow:
  SM closes → news → GM's NewsQueue
  GM closes → news → WM's NewsQueue
  record_event(narrative_flag=True) → adds to higher agent's queue automatically
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional


# Which agent feeds which higher agent
_NEWS_CHAIN = {
    "sm": "gm",   # SM news → GM
    "gm": "wm",   # GM news → WM
    "wm": "",     # WM is top — no higher agent
}


def _queue_path(agent: str) -> Path:
    return Path("game") / "entity_store" / f"{agent}_news.jsonl"


def append(agent: str, items: List[str], source: str = "") -> dict:
    """Append news items to an agent's news queue.

    Args:
        agent: Target agent ("gm", "wm").
        items: News item strings (1-3 recommended).
        source: Who generated this news (for tracing).

    Returns:
        {"agent": agent, "appended": len(items), "total": total_in_queue}
    """
    if not items:
        return {"agent": agent, "appended": 0, "total": 0}

    path = _queue_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    for item in items:
        if item and item.strip():
            entry = {
                "news": item.strip(),
                "source": source,
            }
            with open(str(path), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                count += 1

    total = 0
    if path.exists():
        with open(str(path), "r", encoding="utf-8") as f:
            total = sum(1 for _ in f)

    return {"agent": agent, "appended": count, "total": total}


def consume(agent: str) -> List[str]:
    """Read all news items and clear the queue.

    Args:
        agent: Agent whose queue to consume.

    Returns:
        List of news item strings (empty if none).
    """
    path = _queue_path(agent)
    if not path.exists():
        return []

    items: List[str] = []
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        text = entry.get("news", "")
                        if text:
                            items.append(text)
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    # Clear the queue (atomic: write empty then remove)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        # Fallback: overwrite with empty
        try:
            path.write_text("", encoding="utf-8")
        except Exception:
            pass

    return items


def peek(agent: str, limit: int = 5) -> List[str]:
    """Read without consuming.

    Args:
        agent: Agent whose queue to peek.
        limit: Max items to return.

    Returns:
        List of news item strings (empty if none).
    """
    path = _queue_path(agent)
    if not path.exists():
        return []

    items: List[str] = []
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        text = entry.get("news", "")
                        if text:
                            items.append(text)
                            if len(items) >= limit:
                                break
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return items


def higher_agent(agent: str) -> str:
    """Return the agent that this agent feeds news to."""
    return _NEWS_CHAIN.get(agent, "")


def close_invocation(
    summary: str,
    news: Optional[List[str]] = None,
    invoke_self: Optional[dict] = None,
) -> dict:
    """Universal termination tool for all agents.

    This is a sentinel function — the orchestrator detects it was called
    and processes its arguments. The function itself is a no-op; all work
    happens in the orchestrator loop.

    Args:
        summary: 2-4 sentences describing what this invocation achieved.
            Saved to messages.json → feeds PinnedBlockCache for future calls.
        news: 0-3 items for the higher agent's newspaper.
        invoke_self: Optional scheduling: {"delay_ticks": N} or {"world_time": T}.
            Optional "comment" key for the [reason] block.

    Returns:
        {"status": "invocation_closed", "summary_length": len(summary)}
    """
    # Validation
    if not summary or not summary.strip():
        return {"status": "error", "error": "summary is required"}
    if news and len(news) > 5:
        news = news[:5]

    # All processing is done by the orchestrator after detecting this tool call.
    # This function only validates and returns a sentinel result.
    return {
        "status": "invocation_closed",
        "summary_length": len(summary.strip()),
        "news_count": len(news) if news else 0,
    }
