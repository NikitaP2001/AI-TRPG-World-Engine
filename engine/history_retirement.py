"""Covering what a trim drops.

A history is trimmed from the front: the oldest records no longer fit and are
removed. At that moment they are not lost yet — the agent that just wrote its
history still has them in its own context. So the summary is written by that
same agent, from that same material.

The request carries the agent's own memory unchanged — the same prompt, the
same pinned blocks, the same records it just read, stretch included — and adds
one short task on top. Nothing is re-quoted: the stretch is already in that
memory, so sending it again would double it. The whole prefix is therefore
identical to the invocation that just ran and the provider still serves it from
cache; only the few hundred tokens of the task are new.

The task does not restate the material either. It names the stretch by two
markers that the memory itself contains — the record it starts at and the first
record after it — because every record carries the world time it was written at.
The boundaries are found by reading, not by counting, so a miscount cannot shift
them; and the material being covered is whatever lies between the two markers,
which is by construction exactly what fell out.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from langchain_core.messages import BaseMessage, HumanMessage

from engine.summarizer import SummaryRunner
from memory_store import (
    clear_pending_retirement,
    load_pending_retirement,
    save_pending_retirement,
)

#: What a retirement may produce: a titled paragraph, or a diary entry.
KINDS = ("paragraph", "diary")


def _as_message(record: Union[Dict[str, Any], BaseMessage]) -> BaseMessage:
    """History as the model reads it: every stored record is a user record."""
    if isinstance(record, BaseMessage):
        return record
    return HumanMessage(content=str((record or {}).get("content") or ""))


def _as_messages(records: Sequence[Union[Dict[str, Any], BaseMessage]]) -> List[BaseMessage]:
    return [_as_message(r) for r in (records or [])]


def _fragment(record: Union[Dict[str, Any], BaseMessage], limit: int = 160) -> str:
    """The opening of a record, as a handle for it: whitespace collapsed, short."""
    text = str((record.get("content") if isinstance(record, dict)
                else getattr(record, "content", "")) or "")
    return " ".join(text.split())[:limit]


def _task(kind: str, start_marker: str, end_marker: str) -> str:
    """The short instruction: the memory is the material, this only marks it.

    Two markers, both of them quotable in the memory the request carries: the
    record the stretch starts at, and the first record after it. Nothing else —
    no count, no time span, no restatement of the records themselves, so the
    request stays small and the cacheable prefix is untouched.
    """
    marks = ""
    if start_marker:
        marks += f"Coverage starts at this record:\n{start_marker}\n"
    if end_marker:
        marks += f"Coverage ends just before this record:\n{end_marker}\n"

    stamps_note = (
        "Each record begins with the world time it was written at; that is to place "
        "the stretch in time, not to date every sentence.\n"
    )

    if kind == "diary":
        return (
            "[history_retirement_task]\n"
            + marks
            + "\n"
            + stamps_note
            + "\nWrite the diary entry covering what is between them, in your own "
            "voice, about what it meant to you. One to three paragraphs.\n"
            "Respond with JSON: {\"summary\": \"your diary entry\"}."
        )
    return (
        "[history_retirement_task]\n"
        + marks
        + "\n"
        + stamps_note
        + "\nWrite the paragraph covering what is between them: a short title "
        "(3-10 words) and 5-10 sentences naming what happened, who it concerns and "
        "how it stands. Write only what those records establish.\n"
        "Respond with JSON: {\"name\": \"short title\", \"summary\": \"your summary\"}."
    )


def retire(
    history_path: Path,
    *,
    dropped: Sequence[Dict[str, Any]] = (),
    prefix: Sequence[BaseMessage] = (),
    history_messages: Sequence[Union[Dict[str, Any], BaseMessage]] = (),
    prompt_text: str,
    temperature: float,
    scope: str,
    write: Callable[[Optional[str], str], None],
    kind: str = "paragraph",
    label: str = "history_retirement",
    max_tokens: int = 1500,
) -> bool:
    """Cover whatever a trim just dropped; return whether it was written.

    Args:
        history_path: The history that was trimmed; pending work is kept beside it.
        dropped: Records that fell out of the file now, oldest first.
        prefix: The messages before the history in the agent's own context —
            pinned blocks.
        history_messages: The memory as the agent last saw it, stretch included.
            The request carries it unchanged, so the provider serves the whole
            prefix from cache and only the short task is new.
        prompt_text: The agent's own prompt.
        write: Called with ``(name, summary)`` when a summary is produced; the
            agent decides where it goes.
        kind: ``"paragraph"`` for a titled paragraph, ``"diary"`` for a diary entry.

    Returns:
        True when there is nothing to cover or the cover was written and stored.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown retirement kind: {kind}")

    pending = load_pending_retirement(history_path) or {}
    pending_records: List[Any] = list(pending.get("records") or [])
    records: List[Any] = pending_records + list(dropped or [])
    if not records:
        return True

    memory = _as_messages(history_messages)
    if pending_records:
        # The stretch is no longer in the file, so it is put back at the head of
        # the memory for this one call: the markers have to point at something.
        memory = _as_messages(pending_records) + memory

    start_marker = _fragment(records[0])
    end_marker = (
        _fragment(memory[len(records)]) if len(memory) > len(records) else ""
    )
    task = _task(kind, start_marker=start_marker, end_marker=end_marker)

    runner = SummaryRunner(
        meta=None,
        history_path=history_path,
        prompt_text=prompt_text,
        temperature=temperature,
        scope=scope,
    )
    result = runner.run_summary(
        task_prompt=task,
        title_suffix=f"-retired-{scope}",
        max_tokens=max_tokens,
        label=label,
        extra_pinned=list(prefix) or None,
        history_override=memory,
    )
    if not result or not str(result[1] or "").strip():
        save_pending_retirement(history_path, records=records, label=start_marker[:80])
        return False

    name, summary = result
    write(name, summary)
    clear_pending_retirement(history_path)
    return True


def pending_retirement(history_path: Path) -> Optional[Dict[str, Any]]:
    """What a previous retirement could not cover, if anything."""
    return load_pending_retirement(history_path)


def dropped_from(
    history_path: Path,
    before: Sequence[Union[Dict[str, Any], BaseMessage]],
) -> List[Dict[str, str]]:
    """Which records of *before* are no longer in the file.

    A trim always removes a prefix, so the survivors are a suffix of *before*:
    the file now begins with the newest record of *before* that is still there,
    and everything ahead of it fell out.
    """
    from memory_store import load_history

    after = load_history(history_path)
    if not after:
        # Nothing is left, so nothing of what we had survived.
        return [
            {"role": "user", "content": str(getattr(_as_message(r), "content", "") or "")}
            for r in before
        ]

    first_kept = str(after[0].get("content") or "")
    start = None
    for idx in range(len(before) - 1, -1, -1):
        if str(getattr(_as_message(before[idx]), "content", "") or "") == first_kept:
            start = idx
            break
    if start is None:
        # The file begins with something that was not in *before* (a record
        # written since), so everything we had fell out.
        start = 0
    return [
        {"role": "user", "content": str(getattr(_as_message(r), "content", "") or "")}
        for r in list(before)[:start]
    ]
