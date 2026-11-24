"""The single way a record enters an agent's memory.

One agent has one **thread** (``messages.json``) — the only durable record. Work
happens on a **branch** of it: the same records, held in memory, not yet merged.
Closing the branch merges the durable records into the thread and drops the rest.
See ``design/05_history_write_pipeline.md``.

This module is the only writer. Nothing else may call ``memory_store``'s writing
functions: one writer is what makes "why does this memory contain X" answerable in
one place, and it is what makes a record written mid-session visible in that same
session instead of a session later (a branch is read by its owner as it grows).

What it owns, in one place:

* the branch, and what happens to it when it closes;
* the world-time stamp on a record (through ``memory_store.stamp``);
* the identity test — the same thing said twice is written once;
* replacement, for kinds that are versions of one fact (the catalogue declares
  which: see ``history_kinds``);
* durability, read from the catalogue and never decided here.

What it does not own: what a record means. That is the catalogue's business.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import history_kinds
from memory_store import (
    ENVELOPE_KEYS,
    HistoryLimits,
    limits_from_env,
    load_history,
    save_history,
    stamp,
)

_LOCK = threading.RLock()
_HISTORIES: Dict[str, "History"] = {}


def _stored(record: Dict[str, Any], *, content: str) -> Dict[str, str]:
    """A record as it goes to the file: its words, and the envelope beside them.

    The envelope is the framework's own knowledge of the record — what kind it is,
    which version of what fact it states, and when it was entered. It is kept out
    of the words on purpose: a record that named itself in brackets was readable
    by the code only as long as it stayed framework-shaped, and every marker of
    that kind is one more thing in front of the agent that is not the world.
    """
    out: Dict[str, Any] = {"role": str(record.get("role") or "user"),
                           "content": content}
    for field_name in ENVELOPE_KEYS:
        value = str(record.get(field_name) or "")
        if value:
            out[field_name] = value
    meta = record.get("meta")
    if isinstance(meta, dict) and meta:
        out["meta"] = dict(meta)
    return out


def _moment() -> str:
    """The world time a record is entered at; "" when the world has no clock."""
    try:
        from world.clock import now_string

        return str(now_string() or "")
    except Exception:
        return ""


def _with_moment(body: str, at: str = "") -> str:
    """A record as it reads with the moment it was entered in front of it.

    The moment belongs to the record and not to the writing of the file: a
    session's records are read while its branch is still open, and a reader that
    saw no moment on them before the session closed would be reading its own acts
    undated while everything the sessions before it wrote carried its moment. A
    record that already carries one keeps it; one entered without a clock is left
    as it stands.
    """
    text = str(body or "")
    if not at:
        return text
    if history_kinds.payload(text) != text.lstrip():
        return text
    return f"[{at}] {text}"


@dataclass(frozen=True)
class WriteResult:
    """What happened to one record.

    ``written`` False means the record was already there in exactly that form —
    the identity test, not an error. ``kind`` is None when the record's marker is
    not in the catalogue, which is reported rather than guessed.
    """

    written: bool
    kind: Optional[str]
    durability: str
    to_branch: bool
    replaced: int = 0
    debris_dropped: int = 0
    trimmed_out: Tuple[Dict[str, str], ...] = ()
    reason: str = ""


class History:
    """One agent's memory: a thread on disk, plus the branch being worked on."""

    def __init__(self, path: Path, *, limits: Optional[HistoryLimits] = None) -> None:
        self._path = Path(path)
        self._limits = limits
        self._branch: List[Dict[str, Any]] = []
        self._open = False

    # ── identity ──────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def branch_open(self) -> bool:
        return self._open

    @property
    def limits(self) -> HistoryLimits:
        return self._limits or limits_from_env()

    def branch(self) -> List[Dict[str, Any]]:
        """What is on the branch, in order — a copy, for inspection and tests."""
        return [dict(r) for r in self._branch]

    # ── lifecycle ─────────────────────────────────────────────────────────

    def open_branch(self) -> None:
        """Begin working on a branch: records accumulate, the thread is untouched.

        Idempotent. A session that is re-entered (agents are re-invoked within
        one session) continues the branch it already has — reopening must not
        discard what was written before the re-entry.
        """
        with _LOCK:
            self._open = True

    def close_branch(self) -> Dict[str, Any]:
        """Merge the branch into the thread, then discard it.

        The thread is written here and nowhere else: while the branch is open
        nothing reaches the file, so what an interrupted session leaves behind is
        the file its predecessor closed rather than half of a session nobody
        finished. What the world changed in the meantime is in the world — the
        tools wrote it there — not waiting on this file.
        """
        with _LOCK:
            records = self._branch
            self._branch = []
            self._open = False
            return self._merge(records)

    def records(self) -> List[Dict[str, str]]:
        """The thread as the agent reads it.

        What is merged, then what the open branch has added — the order the file
        will have once it merges. An agent's own memory is therefore complete
        while it is still being built: nothing it was told is visible only to a
        later session. What the branch adds is read with the moment it was
        entered, so a session that reads its own acts sees when it made them and
        not only the moments of the sessions before it.

        Words and role only: the envelope is the frame's, and is read with
        ``envelopes`` by the code that needs it.
        """
        out: List[Dict[str, str]] = [{"role": str(r.get("role") or "user"),
                                       "content": str(r.get("content") or "")}
                                      for r in load_history(self._path)]
        out += [{"role": str(r.get("role") or "user"),
                 "content": _with_moment(str(r.get("content") or ""),
                                         str(r.get("at") or ""))}
                for r in self._branch]
        return out

    def envelopes(self) -> List[Dict[str, str]]:
        """The thread with what the code reads of each record: its kind and moment.

        What a reader of the memory itself needs — the one that finds a session's
        bounds, what a fact's latest version is, or what happened in a stretch of
        cycles — without looking at a word of the text to tell.
        """
        out: List[Dict[str, str]] = [
            _stored(r, content=str(r.get("content") or ""))
            for r in load_history(self._path)]
        out += [_stored(r, content=_with_moment(str(r.get("content") or ""),
                                                str(r.get("at") or "")))
                for r in self._branch]
        return out

    def drop_fact(self, kind_name: str, key: str) -> int:
        """Remove the record that versions a fact — the counterpart of replacement.

        A versioned record is written when it differs and replaced when a newer
        version arrives; when the fact itself stops being true there is nothing
        left to version, and the record goes. Called with the kind and the key
        (``history_kinds.replacement_key`` of the record it removes).
        """
        with _LOCK:
            def _matches(record: Dict[str, Any]) -> bool:
                return self._same_fact(record, kind_name, key)

            self._branch = [r for r in self._branch if not _matches(r)]
            thread = load_history(self._path)
            kept = [r for r in thread if not _matches(r)]
            if len(kept) == len(thread):
                return 0
            dropped = len(thread) - len(kept)
            save_history(self._path, kept, limits=self.limits)
            return dropped

    # ── the write entry point ─────────────────────────────────────────────

    def record(
        self,
        content: str,
        *,
        kind: Optional[str] = None,
        key: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> WriteResult:
        """Enter one record. ``kind``/``key`` are only needed for a record whose
        marker does not name it (the catalogue lists which those are).

        ``meta`` is what the writer knows about the record and the reader of its
        words does not need: the id an attempt stood under, who perceived it. It
        is kept beside the text because putting it in the text was what made a
        record readable only while it stayed framework-shaped.
        """
        text = str(content or "")
        if not text.strip():
            return WriteResult(False, None, history_kinds.UNKNOWN_DURABILITY, self._open,
                               reason="empty record")

        body = history_kinds.payload(text)
        declared = self._declared(body, kind)
        if declared is not None and declared.debris:
            return WriteResult(False, declared.name, declared.durability, self._open,
                               reason="declared debris")
        name, durability, replace = self._classify(declared)
        effective_key = key or (history_kinds.replacement_key(body) if replace else None)

        with _LOCK:
            if self._open:
                replaced = 0
                if replace and effective_key:
                    seen = (self._same_version(self._branch, name, effective_key, body)
                            or self._same_version(load_history(self._path), name,
                                                  effective_key, body))
                    if seen:
                        return WriteResult(False, name, durability, True,
                                           reason="this version is already recorded")
                    before = len(self._branch)
                    self._branch = [
                        r for r in self._branch
                        if not self._same_fact(r, name, effective_key)
                    ]
                    replaced = before - len(self._branch)
                elif not (declared is not None and declared.repeatable):
                    # A record of a repeatable kind — a session's opening, its
                    # close — stands on its own: two of them that read the same
                    # are still two boundaries, and one does not answer for the
                    # other.
                    if self._present(self._branch, name, body):
                        return WriteResult(False, name, durability, True,
                                           reason="already on this branch")
                    if self._present(load_history(self._path), name, body):
                        # Said already, in an earlier branch or before the branch
                        # opened: saying it again adds nothing to a reader.
                        return WriteResult(False, name, durability, True,
                                           reason="already in the thread")
                self._branch.append({"role": "user", "content": body,
                                     "kind": name or "", "key": effective_key or "",
                                     "at": _moment(), "meta": dict(meta or {})})
                return WriteResult(True, name, durability, True, replaced=replaced)

            # No branch: one record, written now through the same rules.
            outcome = self._merge([{"role": "user", "content": body,
                                    "kind": name or "", "key": effective_key or "",
                                    "at": _moment(), "meta": dict(meta or {})}])
            return WriteResult(
                outcome["merged"] > 0, name, durability, False,
                replaced=outcome["replaced"],
                debris_dropped=outcome["debris_dropped"],
                trimmed_out=tuple(outcome["trimmed_out"]),
                reason="" if outcome["merged"] else outcome["skipped"],
            )

    # ── classification ────────────────────────────────────────────────────

    def _declared(self, body: str, kind: Optional[str]) -> Optional[history_kinds.Kind]:
        return history_kinds.kind(kind) if kind else history_kinds.resolve(body)

    def _kind_of(self, record: Dict[str, Any]) -> Optional[history_kinds.Kind]:
        """The kind a stored record belongs to: its own word for it, or its marker.

        The envelope is the authority — a record says what it is — and the marker
        in the text is the fallback for records written before it was kept.
        """
        declared = str(record.get("kind") or "")
        if declared:
            try:
                return history_kinds.kind(declared)
            except KeyError:
                pass
        return history_kinds.resolve(str(record.get("content") or ""))

    def _key_of(self, record: Dict[str, Any]) -> str:
        """The version key of the fact a record states: its own, or its marker's.

        A writer that names the key states it in the envelope; a record that only
        says it in its marker line keeps answering with that, so a fact written
        before the envelope existed is still replaced rather than duplicated.
        """
        stored = str(record.get("key") or "")
        if stored:
            return stored
        return history_kinds.replacement_key(str(record.get("content") or "")) or ""

    def _classify(self, declared: Optional[history_kinds.Kind]) -> Tuple[Optional[str], str, bool]:
        if declared is None:
            return None, history_kinds.UNKNOWN_DURABILITY, False
        return declared.name, declared.durability, declared.replace_stale

    def _present(self, records: List[Dict[str, Any]], name: Optional[str], body: str) -> bool:
        """Is exactly this record already there?"""
        for rec in records:
            existing = str(rec.get("content") or "")
            if history_kinds.payload(existing) != body:
                continue
            if name is None:
                return True
            found = self._kind_of(rec)
            if found is None or found.name == name:
                return True
        return False

    def _same_fact(self, record: Dict[str, Any], name: Optional[str], key: Optional[str]) -> bool:
        """Is this record a version of the same fact as the one being written?"""
        if name is None or not key:
            return False
        found = self._kind_of(record)
        if found is None or found.name != name:
            return False
        return self._key_of(record) == key

    def _same_version(self, records: List[Dict[str, Any]], name: Optional[str],
                      key: str, body: str) -> bool:
        """Is this exact version of the fact already recorded?"""
        return any(
            self._same_fact(rec, name, key)
            and history_kinds.payload(str(rec.get("content") or "")) == body
            for rec in records
        )

    # ── merging ───────────────────────────────────────────────────────────

    def _merge(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Enter records into the thread: durable ones merged, the rest dropped."""
        thread = load_history(self._path)
        merged_names: List[str] = []
        branch_only: List[str] = []
        replaced = 0
        rewrote = False
        pending: List[Dict[str, str]] = []
        skipped = ""

        # Debris: a kind the catalogue declares is not a record any more is
        # dropped where it is found, so a thread clears itself as it is written.
        without_debris = [r for r in thread
                          if not history_kinds.is_debris(str(r.get("content") or ""))]
        debris_dropped = len(thread) - len(without_debris)
        if debris_dropped:
            thread = without_debris
            rewrote = True

        for rec in records:
            body = str(rec.get("content") or "")
            if not body.strip():
                continue
            declared = self._declared(body, rec.get("kind"))
            name, durability, replace = self._classify(declared)
            repeatable = bool(declared is not None and declared.repeatable)
            if durability != history_kinds.DURABLE:
                branch_only.append(name or "(unrecognised)")
                continue

            key = rec.get("key") or (history_kinds.replacement_key(body) if replace else None)
            if replace and key:
                # The same version written again is not a new version: nothing
                # is written and nothing is replaced.
                if self._same_version(thread, name, key, body) \
                        or self._same_version(pending, name, key, body):
                    skipped = "this version is already recorded"
                    continue
                kept = [r for r in thread
                        if not self._same_fact(r, name, key)]
                if len(kept) != len(thread):
                    replaced += len(thread) - len(kept)
                    thread = kept
                    rewrote = True
                before = len(pending)
                pending = [p for p in pending
                           if not self._same_fact(p, name, key)]
                if len(pending) != before:
                    replaced += before - len(pending)
            elif not repeatable and (self._present(thread, name, body)
                                     or self._present(pending, name, body)):
                skipped = "already in the thread"
                continue

            pending.append(_stored({"role": "user", "content": body, "kind": name or "",
                                    "key": key or "", "at": str(rec.get("at") or ""),
                                    "meta": rec.get("meta")},
                                   content=body))
            merged_names.append(name or "(unrecognised)")

        trimmed_out: List[Dict[str, str]] = []
        if pending:
            final = thread + [
                _stored(p, content=_with_moment(str(p["content"]),
                                                str(p.get("at") or "")))
                for p in pending]
            trimmed_out = save_history(self._path, final, limits=self.limits)
        elif rewrote:
            # A replacement, a withdrawal or a debris removal may take a record
            # out without putting one in: the thread still has to be written for
            # the removal to exist.
            trimmed_out = save_history(self._path, thread, limits=self.limits)

        return {
            "merged": len(merged_names),
            "merged_names": merged_names,
            "branch_only": branch_only,
            "replaced": replaced,
            "debris_dropped": debris_dropped,
            "trimmed_out": trimmed_out,
            "skipped": skipped,
        }


def memory_at(path: Path | str, *, limits: Optional[HistoryLimits] = None) -> History:
    """The one handle for a history file.

    Handed out per resolved path, because branch state belongs to the file: two
    handles for one file would mean two branches, and a record written through
    one of them would not be seen by the other. This also covers the case of an
    agent writing into a peer's memory (the change records do exactly that).
    """
    key = os.path.normcase(str(Path(path).resolve()))
    with _LOCK:
        existing = _HISTORIES.get(key)
        if existing is None:
            existing = History(path, limits=limits)
            _HISTORIES[key] = existing
        elif limits is not None:
            existing._limits = limits
        return existing


def reset_histories() -> None:
    """Forget every handle. For tests, and for a restarted process."""
    with _LOCK:
        _HISTORIES.clear()


__all__ = ["History", "WriteResult", "memory_at", "reset_histories"]
