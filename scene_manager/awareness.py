"""What the session knows, and which character knows it.

A session produces **knowledge**: something happened, something was declared,
someone was asked and answered, something was perceived. Each piece is entered
here once, under an id of its own (``1``, ``2``, …), and the
question this file answers about it is *who knows it* — one character, several, or
nobody yet.

Two things read the ledger:

* the scene manager — what it is told each phase is which characters' knowledge
  changed, and every piece it reads carries its id where it reads it: beside an
  attempt in the phase that reviews it, appended to the result of the call that
  made a change, in the answer that entered a perception. The ids are never
  abbreviations: a piece is entered as ``1``, ``2``, …, so the
  word the framework uses and the word the agent reads are the same word, and the
  number beside it says which piece it stands for. An id the scene manager has not
  read beside its piece is an id it cannot use, so nothing is entered without being
  stated where the agent acts on it.
* the framework — knowledge a character was given is written into that
  character's own memory, and only there. Nobody re-writes it to deliver it:
  what the session entered is what the character receives.

Why a ledger rather than text markers: a marker says "these names were told this
text", which leaves nothing to reason about afterwards — not who was not told,
not what a name refers to when the same thing is given twice, and not whether the
delivery ever happened. Knowledge is state, so it is kept as state.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

_LOCK = threading.RLock()

# The world-time stamp the pipeline puts in front of a stored record.
_STAMP = re.compile(r"^\[Y\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")


@dataclass
class Record:
    """One piece of knowledge the session produced."""

    id: str
    kind: str
    content: str
    order: int


@dataclass
class Ledger:
    _records: Dict[str, Record] = field(default_factory=dict)
    _by_content: Dict[str, str] = field(default_factory=dict)
    _aware: Dict[str, List[str]] = field(default_factory=dict)
    _delivered: Dict[str, set] = field(default_factory=dict)
    _emitted: Dict[str, tuple] = field(default_factory=dict)
    _next: int = 0

    # ── what the session produced ─────────────────────────────────────────

    def register(
        self,
        *,
        kind: str,
        content: str,
        characters: Sequence[str] = (),
    ) -> str:
        """Enter one piece of knowledge and return the id it reads as.

        ``characters`` names those who know it from the moment it is entered —
        what one character perceives and nobody else does is the normal case.
        With no names, nobody knows it yet: it happened, and who learns of it is
        the scene manager's to decide, not the recorder's.
        """
        text = str(content or "").strip()
        if not text:
            return ""
        with _LOCK:
            self._next += 1
            record_id = str(self._next)
            self._records[record_id] = Record(
                id=record_id,
                kind=str(kind or "record"),
                content=text,
                order=self._next,
            )
            self._by_content[_unstamped(text)] = record_id
            for name in characters:
                name = str(name or "").strip()
                if name:
                    self._mark_aware(name, record_id)
            return record_id

    def id_of(self, content: str) -> Optional[str]:
        """The id a piece of knowledge was entered under, if it was at all.

        The text a reader sees may carry the world-time stamp the pipeline puts
        in front of a stored one; the knowledge is what was entered, so the stamp
        is not part of the match.
        """
        return self._by_content.get(_unstamped(content))

    # ── who knows what ────────────────────────────────────────────────────

    def _mark_aware(self, character: str, record_id: str) -> None:
        held = self._aware.setdefault(character, [])
        if record_id not in held:
            held.append(record_id)

    def give(self, character: str, knowledge: Sequence[object] = ()) -> str:
        """Give one character knowledge the session holds.

        The answer is the address of each piece he was given, one to a line —
        the same shape the id takes wherever it is stated. A number the session
        does not hold is reported as a fault, because a ledger that quietly
        accepted it would let the scene manager believe a character was told what
        he never was.
        """
        name = str(character or "").strip()
        if not name:
            return "ERROR: name a character."
        given: List[str] = []
        unknown: List[str] = []
        with _LOCK:
            for raw in knowledge:
                rid = _clean_id(raw)
                if rid in self._records:
                    self._mark_aware(name, rid)
                    given.append(rid)
                else:
                    unknown.append(rid)
            # What was just given is not delivered yet: it is the framework's
            # queue, and it empties when the character has actually been given it.
            for rid in given:
                self._delivered.setdefault(name, set()).discard(rid)
        lines = [knowledge_label(rid) for rid in given]
        if unknown:
            # Echoed as they were passed, not as numbers: a name the session does
            # not hold is not an id, and reading it back as one would dress a
            # guess up as a piece.
            lines.append("ERROR: no such knowledge id: [" + ", ".join(unknown) + "]")
        if not lines:
            return "ERROR: name the knowledge to give."
        return "\n".join(lines)

    def perceive(self, character: str, perception: str) -> str:
        """Enter what one character perceives, and state the piece's address.

        What he perceives is entered as a piece of its own and marked as his in
        the same act, and the answer is that piece's address and nothing else: the
        only statement of an id is the one carrying its number, so a number met
        later in the session is one its reader met here.
        """
        name = str(character or "").strip()
        if not name:
            return "ERROR: name a character."
        rid = self.register(
            kind="perception", content=perception, characters=[name],
        )
        if not rid:
            return "ERROR: nothing perceived."
        return knowledge_label(rid)

    def knows(self, character: str) -> List[str]:
        """What one character knows, in the order the session made it."""
        return self._ordered(self._aware.get(str(character or "").strip(), []))

    def not_known(self, character: str) -> List[str]:
        """Knowledge this character has not been given, in the order it was made.

        One list for the two ways it happens — never given to him, or given and
        lost with the session — because the question the menu answers is what
        still has to reach him, and both answers are the same answer.
        """
        name = str(character or "").strip()
        known = set(self._aware.get(name, []))
        return [rid for rid, rec in self._unordered() if rid not in known]

    def _unordered(self) -> List[Tuple[str, Record]]:
        return sorted(self._records.items(), key=lambda kv: kv[1].order)

    # ── delivery ──────────────────────────────────────────────────────────

    def pending(self, character: str) -> List[Tuple[str, str]]:
        """What he knows and has not been given yet: (id, content)."""
        name = str(character or "").strip()
        done = self._delivered.get(name, set())
        out = []
        for rid in self._aware.get(name, []):
            if rid in done:
                continue
            rec = self._records.get(rid)
            if rec is not None:
                out.append((rid, rec.content))
        return out

    def mark_delivered(self, character: str, ids: Sequence[str]) -> None:
        name = str(character or "").strip()
        with _LOCK:
            self._delivered.setdefault(name, set()).update(ids)

    # ── what the scene manager reads ──────────────────────────────────────

    def changes(self, characters: Sequence[str]) -> str:
        """State each character whose knowledge is not what it was.

        A character is stated when his knowledge changed: something appeared that
        he was not given, or he was given something new. A character whose
        knowledge is what it already was is not stated again — the ids sit on the
        knowledge itself, and a line repeated every invocation is paid for every
        invocation and says nothing new.

        Ask once, when an invocation begins. Asking after each piece would state
        every character again for every piece, because new knowledge changes every
        character's state at once.
        """
        lines: List[str] = []
        with _LOCK:
            if not self._records:
                # Nothing was produced, so there is nothing to know: saying that
                # every character knows nothing is a line paid for per invocation
                # to report the state the session starts in.
                return ""
            for raw in characters:
                name = str(raw or "").strip()
                if not name:
                    continue
                knows = tuple(self.knows(name))
                not_known = tuple(self.not_known(name))
                signature = (knows, not_known)
                if self._emitted.get(name) == signature:
                    continue
                self._emitted[name] = signature
                lines.append(f"  {name}")
                lines.append(f"    {_KNOWS}: {knowledge_ids(knows)}")
                lines.append(f"    {_NOT_KNOWS}: {knowledge_ids(not_known)}")
        if not lines:
            return ""
        return "Whose knowledge changed, and how it now stands:\n" + "\n".join(lines)

    def _ordered(self, ids: Sequence[str]) -> List[str]:
        """Ids in the order their knowledge was made, which is how they read."""
        known = [(self._records[rid].order, rid) for rid in ids if rid in self._records]
        return [rid for _order, rid in sorted(known)]

    def clear(self) -> None:
        """Empty the ledger in place — a new session starts from nothing.

        In place, because a session's ledger is held by whoever reads it: giving
        out a new object would leave one holder writing into the session that
        ended and another reading the one that began.
        """
        with _LOCK:
            self._records.clear()
            self._by_content.clear()
            self._aware.clear()
            self._delivered.clear()
            self._emitted.clear()
            self._next = 0


#: The two sides of a character's knowledge, named where the scene manager reads
#: them. One wording, whatever is doing the stating: what a character has been
#: told, and what is still to reach him.
_KNOWS = "pieces he knows"
_NOT_KNOWS = "not yet given to him"


def knowledge_number(record_id: object) -> str:
    """An id, as every line that states it writes it."""
    return "" if record_id is None else str(record_id).strip()


def knowledge_label(record_id: object) -> str:
    """How one piece's id is written beside the act that made it.

    In words, with its number: the number is what the scene manager names when it
    gives a piece to someone, and it is the only mark a record carries that is
    not the framework's own — so it is said, not bracketed.
    """
    return f"knowledge {knowledge_number(record_id)}"


def knowledge_ids(ids: Sequence[object]) -> str:
    """A set of pieces, as the numbers it is made of."""
    return "[" + ", ".join(knowledge_number(i) for i in ids) + "]"


def _clean_id(raw: object) -> str:
    """An id as the session holds it: a number, however it was written."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    text = str(raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def _unstamped(content: str) -> str:
    """The record itself, without the world-time stamp a stored one carries."""
    return _STAMP.sub("", str(content or "").strip())


def attempt_piece(actor: object, action_type: object, target: object,
                  description: object) -> str:
    """What an attempt reads as, on either side of the session.

    One shape, wherever the attempt is stated: as the scene manager reads it in
    the pool, as the session enters it, and as it is handed to whoever learns of
    it. The words are the identity of the piece — an attempt entered twice under
    two shapes is two pieces, and a piece nobody can recognise is an id nobody
    can use.
    """
    who = str(actor or "").strip()
    what = str(description or "").strip()
    if not who or not what:
        return ""
    return (f"{who}: {str(action_type or '?').strip()} → "
            f"{str(target or '?').strip()}\n  {what}")


_LEDGER = Ledger()


def ledger() -> Ledger:
    """The session's ledger. One session, one ledger."""
    return _LEDGER


def reset_ledger() -> None:
    """Begin a session's ledger from nothing."""
    _LEDGER.clear()


__all__ = [
    "Ledger", "Record", "attempt_piece", "knowledge_ids", "knowledge_label",
    "knowledge_number", "ledger", "reset_ledger",
]