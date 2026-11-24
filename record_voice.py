"""A record as the agent reads it.

The file keeps what the code needs to find a record again: a marker that says
what kind it is, ids in brackets, fields one to a line. None of that is the
world, and an agent that reads it is reading the framework's bookkeeping — so
what reaches the agent is this rendering: the same record in the world's voice,
the brackets gone, and an id that must be acted on stated as a number beside the
thing it names.

The stored text is untouched. The markers stay in the file, where the reader,
the classifier and the versioning need them; the agent meets the words alone.
This is the whole of the marking problem solved, and solved strictly outside the
agent's context: one function, applied where a store becomes a conversation.

What is deliberately kept: the world-time stamp at the front of a record, the
number of a cycle, the number of a piece of knowledge. A date and a count are
the world's, not the framework's.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Tuple

#: The world time a record is entered at, in front of it. Kept as it stands.
_STAMP = re.compile(r"^\[Y\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")

#: A field of a record the framework wrote: indentation, a name, a colon.
_FIELD = re.compile(r"^\s+([A-Za-z_][\w ]*):\s?(.*)$")

#: A thing stated with the kind it is, as the roster states it.
_TAGGED = re.compile(r"^(?P<indent>\s*)(?P<name>.+?) \[(?P<kind>[^\]]+)\]$")

#: How one attempt reads in a memory, when the review read it.
_ATTEMPT = re.compile(
    r"^\[(?P<id>[^\]]+)\] (?P<actor>.+?): (?P<kind>.+?) → (?P<target>.+)$")
#: How the same attempt reads where it came to pass.
_ATTEMPT_DONE = re.compile(r"^\[(?P<id>[^\]]+)\] — resolved$")

#: One block of an agent's own sheet, with the version of its content.
_SHEET = re.compile(r"^entity:(?P<name>[^/]+)/(?P<block>[^@]+)(?:@v:(?P<version>.+))?$")

#: What one character was told he perceives.
_PERCEIVED = re.compile(r"^perception[^→]*→ (?P<name>.+)$")

#: What the agent declared it would do.
_DECLARED = re.compile(r"^(?P<actor>[^:]+): (?P<kind>.+?) on (?P<target>.+?) "
                       r"\((?P<visibility>[^)]*)\)$")

#: The same declaration as the agent's own memory keeps it: the kind first, then
#: what it was aimed at, with how far it runs.
_DECLARED_STORED = re.compile(
    r"^(?P<kind>[^:>]+?) -> (?P<target>.+?) \((?P<visibility>[^,)]*)"
    r"(?:,\s*(?P<span>[^)]*))?\)$")

#: A record opens with its marker, and the words may follow on the same line.
_LEAD = re.compile(r"^\[(?P<marker>[^\[\]\n]{1,120})\](?P<rest>.*)$", re.S)


# ---------------------------------------------------------------------------
# what a record's own lines look like once the marker is gone
# ---------------------------------------------------------------------------

def _body(rest: str) -> str:
    return rest.strip("\n")


def _deindent(text: str) -> str:
    """A record's lines without the two spaces that marked them as fields."""
    out: List[str] = []
    for line in text.split("\n"):
        out.append(line[2:] if line.startswith("  ") else line)
    return "\n".join(out)


def _fields(text: str) -> Tuple[List[str], List[str]]:
    """Split a body into its field lines and the prose that is not one."""
    fields: List[str] = []
    prose: List[str] = []
    for line in text.split("\n"):
        if _FIELD.match(line):
            fields.append(line.strip())
        elif line.strip():
            prose.append(line.strip() if not prose else line)
    return fields, prose


# ---------------------------------------------------------------------------
# one renderer per kind that reads badly as stored
# ---------------------------------------------------------------------------

def _session_opens(rest: str) -> str:
    """The roster: where the players stand, and what stands with them."""
    lines: List[str] = []
    for line in _body(rest).split("\n"):
        stripped = line.strip()
        if stripped.startswith("[Locations and entities"):
            lines.append("Where the players stand, and what stands there with them:")
            continue
        tagged = _TAGGED.match(line)
        if tagged:
            lines.append(f"{tagged.group('indent')}{tagged.group('name')} "
                         f"({tagged.group('kind')})")
            continue
        more = re.match(r"^\s*\.\.\. (\d+) more present$", line)
        if more:
            lines.append(f"  and {more.group(1)} more")
            continue
        lines.append(line)
    roster = "\n".join(lines).strip()
    return "The scene is set." + ("\n\n" + roster if roster else "")


def _session_closes(rest: str) -> str:
    when = ""
    cycles = ""
    rest_lines: List[str] = []
    for line in _body(rest).split("\n"):
        stamp = re.match(r"^time:\s*(.+)$", line.strip())
        if stamp and not when and not rest_lines:
            when = stamp.group(1).strip()
            continue
        span = re.match(r"^cycles:\s*(.+)$", line.strip())
        if span and not cycles and not rest_lines:
            cycles = span.group(1).strip()
            continue
        rest_lines.append(line)
    account = "\n".join(rest_lines).strip()
    subject = "The stretch"
    if cycles:
        subject = f"Cycles {cycles}" if "–" in cycles else f"Cycle {cycles}"
    head = (f"{subject} came to an end at {when}." if when
            else f"{subject} came to an end.")
    return head + ("\n\n" + account if account else "")


def _attempt_read(rest: str) -> str:
    """An attempt as the review read it, with the id it stood under."""
    head, _sep, body = _body(rest).partition("\n")
    match = _ATTEMPT.match(head.strip())
    fields, prose = _fields(body)
    if match is None:
        # A shape this renderer does not know: the words still matter, the
        # marker does not.
        return _deindent(_body(rest))
    lead = (f"{match.group('actor')} declared an attempt, {match.group('id')}: "
            f"{match.group('kind')}, aimed at {match.group('target')}.")
    out = [lead]
    out += ["  " + line for line in prose]
    out += ["  " + line for line in fields]
    return "\n".join(out)


def _attempt_done(rest: str) -> str:
    """The same attempt where it came to pass, and who was there to see it."""
    head, _sep, body = _body(rest).partition("\n")
    match = _ATTEMPT_DONE.match(head.strip())
    ident = match.group("id") if match else ""
    fields, _prose = _fields(body)
    seen = ""
    for line in fields:
        name, _colon, value = line.partition(":")
        if name.strip().lower() in ("perceived by", "seen by"):
            seen = value.strip()
    lead = f"Attempt {ident} reached its end." if ident else "An attempt reached its end."
    if seen:
        lead += f" Perceived by {seen}."
    return lead


def _perception_given(marker: str, rest: str) -> str:
    match = _PERCEIVED.match(marker.strip())
    text = _deindent(_body(rest)).strip()
    if match is None:
        return text
    name = match.group("name").strip()
    lead = f"You told {name} what he perceives:"
    return lead + ("\n" + text if text else "")


def _knowledge_stated(marker: str, rest: str) -> str:
    """One piece of knowledge, named by the number the session holds it under."""
    number = marker.partition(":")[2].strip()
    text = _body(rest).strip()
    lead = f"Piece of knowledge {number}." if number else "A piece of knowledge."
    return lead + ("\n" + text if text else "")


def _answered(rest: str) -> str:
    """What a character asked, and what came back."""
    head, _sep, body = _body(rest).partition("\n")
    asked = head.strip()
    answer = _deindent(body).strip()
    lead = f"You asked: {asked}" if asked else "You asked:"
    return lead + ("\n" + answer if answer else "")


def _world_changed(marker: str, rest: str) -> str:
    """One entity-changing call: what it did, and to what."""
    head, _sep, body = _body(rest).partition("\n")
    tool = head.strip()
    fields, prose = _fields(body)
    out = [f"Through {tool or 'a call'} the world changed:"]
    out += ["  " + line for line in fields + prose]
    return "\n".join(out)


def _sheet_block(marker: str, rest: str) -> str:
    """One block of the agent's own sheet, as it now stands.

    The version its marker carries is not said: it is the framework's way of
    knowing whether the block changed, and an agent has no use for it.
    """
    match = _SHEET.match(marker.strip())
    block = (match.group("block") if match else "sheet").replace("_", " ")
    text = _body(rest).strip("\n")
    lead = f"What the world now holds of your {block}:"
    return lead + ("\n" + text if text else "")


def _declared(rest: str) -> str:
    head, _sep, body = _body(rest).partition("\n")
    match = _DECLARED.match(head.strip())
    description = _deindent(body).strip()
    if match is None:
        stored = _DECLARED_STORED.match(head.strip())
        if stored is None:
            return description or head.strip()
        visibility = stored.group("visibility").strip()
        span = (stored.group("span") or "").strip()
        lead = (f"You declared: {stored.group('kind').strip()}, aimed at "
                f"{stored.group('target').strip()}"
                + (f" ({visibility})" if visibility else "")
                + (f", over {span}" if span else "") + ".")
        return lead + ("\n" + description if description else "")
    visibility = match.group("visibility").strip()
    lead = (f"You declared: {match.group('kind')} on {match.group('target')}"
            + (f" ({visibility})" if visibility else "") + ".")
    return lead + ("\n" + description if description else "")


def _withdrawn(rest: str) -> str:
    text = _body(rest).strip()
    if not text or text == "(none)":
        return "You withdrew nothing."
    return f"You withdrew: {text}."


def _written(rest: str) -> str:
    """What the invocation wrote, as each tool reported it."""
    lines: List[str] = []
    for line in _body(rest).split("\n"):
        line = re.sub(r"^\[tool:([^\]]+)\]\s*", r"\1 — ", line.strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _invoked(marker: str, rest: str) -> str:
    asked = marker.partition(":")[2].strip() or _body(rest).strip()
    return f"An invocation was made: {asked}." if asked else "An invocation was made."


def _closed_on(rest: str) -> str:
    text = _body(rest).strip()
    return f"Closed on: {text}" if text else "Closed."


def _asked_again(rest: str) -> str:
    text = _body(rest).strip()
    return f"You asked to be woken again: {text}" if text else "You asked to be woken again."


def _correction(rest: str) -> str:
    text = _body(rest).strip()
    return f"A correction: {text}" if text else "A correction."


def _account(rest: str) -> str:
    """What the world told this agent, which is already prose."""
    return _body(rest).strip()


def _plain(rest: str) -> str:
    """A kind whose marker is the whole of what it says: the words follow."""
    return _deindent(_body(rest)).strip()


def _exchange(rest: str) -> str:
    """A question put to another agent, and the answer that came back."""
    out: List[str] = []
    for line in _deindent(_body(rest)).split("\n"):
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("question:"):
            out.append("Asked: " + stripped[len("question:"):].strip())
        elif lowered.startswith("answer:"):
            out.append("Answered: " + stripped[len("answer:"):].strip())
        else:
            out.append(line)
    return "\n".join(out).strip()


#: Marker at the head of a record → how the rest of it reads. A renderer that
#: needs the marker itself — a number inside it, a name it was addressed to —
#: takes it as the first of its two arguments.
_SAY: Dict[str, Callable[..., str]] = {
    "session_begin": _session_opens,
    "session_end": _session_closes,
    "accepted": _attempt_read,
    "perception": _perception_given,
    "knowledge": _knowledge_stated,
    "player_answer": _answered,
    "entity": _world_changed,
    "intent": _declared,
    "cancelled": _withdrawn,
    "written": _written,
    "closed": _closed_on,
    "invoke_self": _asked_again,
    "correction": _correction,
    "account": _account,
    # A kind that states itself and then says it: the marker is the bookkeeping.
    "reasoning": _plain,
    "answer": _plain,
    "thoughts": _plain,
    "disposition": _plain,
    "world_setting": _plain,
    "awareness": _plain,
    "exchange": _exchange,
}

#: The renderers that read the marker as well as the words after it.
_NEEDS_MARKER = {"perception", "knowledge", "entity"}


def _split(text: str) -> Tuple[str, str]:
    """The marker a record opens with, and what follows it.

    A marker may stand alone on its line or introduce words on the same line;
    both are the same record, and both are read here.
    """
    lead = _LEAD.match(text)
    if lead is None:
        return "", text
    rest = lead.group("rest")
    if rest.startswith("\n"):
        rest = rest[1:]
    elif rest.startswith(" "):
        rest = rest[1:]
    return lead.group("marker").strip(), rest


def spoken(content: str) -> str:
    """One record, as the agent reads it.

    The stamp stays where it is: a record says when it happened, and a date is
    the world's. Everything else the framework put there to find the record again
    is said in words, or is not said at all.
    """
    raw = str(content or "")
    if not raw.strip():
        return ""
    stamp = ""
    match = _STAMP.match(raw)
    if match:
        stamp = match.group(0)
        raw = raw[match.end():]

    marker, rest = _split(raw)
    out = ""

    if not marker:
        # A record that opens without a marker is already in the world's voice:
        # it is what an agent wrote, or what the world told it.
        out = raw.strip("\n")
    elif _SHEET.match(marker):
        out = _sheet_block(marker, rest)
    elif marker.startswith("call_gm:") or marker.startswith("call_wm:"):
        out = _invoked(marker, rest)
    else:
        key = re.match(r"^[A-Za-z_]+", marker)
        name = key.group(0) if key else ""
        say = _SAY.get(name)
        if name == "accepted":
            line = rest.partition("\n")[0].strip()
            out = _attempt_done(rest) if _ATTEMPT_DONE.match(line) \
                else _attempt_read(rest)
        elif say is not None:
            out = say(marker, rest) if name in _NEEDS_MARKER else say(rest)
        else:
            # A kind nothing here knows how to say: its own line is bookkeeping,
            # and the words after it are still the agent's to read.
            out = _deindent(rest).strip()

    out = out.strip("\n")
    return f"{stamp}{out}" if out else ""


__all__ = ["spoken"]
