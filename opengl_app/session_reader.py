"""What a session was, in a form a reader can look at.

The sessions themselves are the records the scene manager closes with: one
`[session_end]` per session, carrying the moment it closed at and the narrative
of what happened in it. This module turns those records — and the world's own
story, its save points and its logs of what took place — into plain values for
the GUI: text without markers, a session list, the chapters those sessions fall
into, and the deeds of one session.

Nothing here is a second source of truth: every function reads the game on disk
and returns plain dicts, so the reader shows what the engine wrote and no more.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE = Path(__file__).resolve().parent.parent
GAME = WORKSPACE / "game"

#: The record a session closes with, and the field its narrative follows.
_SESSION_END = "[session_end]"
_TIME_LINE = re.compile(r"^\s*time:\s*(.+?)\s*$")
_STAMP = re.compile(r"^\[([^\]]+)\]\s*")
#: Any marker a record may wear in front of what it says.
_META_STAMP = re.compile(r"^\[(?:[^\]]{0,60})\]\s*")
_SESSION_START = "[session_begin]"


# ── reading the game ─────────────────────────────────────────────────────

def _records(path: Path) -> List[Dict[str, Any]]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _content(record: Dict[str, Any]) -> str:
    return str(record.get("content") or "")


def _is(record: Dict[str, Any], kind: str, marker: str) -> bool:
    """Is this record of that kind — by its own account, or by its marker.

    A record says what it is in a field beside its words; the marker in the text
    is what records written before that field carry, and is read only for them.
    """
    if str(record.get("kind") or "") == kind:
        return True
    return marker in _content(record)


def world_time() -> str:
    """The moment the world stands at."""
    from run_session import world_time as _world_time

    return _world_time()


def session_count() -> int:
    """How many sessions this world has lived, by its own count."""
    from run_session import session_count as _session_count

    return _session_count()


def _story_path() -> Optional[Path]:
    """The world's story, from whichever agent kept one."""
    world = GAME / "world"
    for name in ("sm_story.json", "gm_story.json", "wm_story.json"):
        path = world / name
        if path.exists():
            return path
    return None


# ── text without markers ─────────────────────────────────────────────────

def _strip_markers(text: str) -> str:
    """A line without the markers in front of it, and nothing else changed.

    A record wears the moment it was written, the kind of record it is, and
    sometimes the id the framework files it under. All of it is the framework's
    business; what a reader is given is the sentence that follows. A line that
    is nothing but markers carries nothing and reads as empty.
    """
    out = str(text or "").strip()
    for _ in range(4):
        match = _META_STAMP.match(out)
        if not match:
            break
        rest = out[match.end():].strip()
        if not rest:
            return ""
        out = rest
    return out


def _plain(text: str) -> str:
    """What one record says, without the marker it wears or the meta around it.

    A reader is given the prose and nothing else: the marker names the kind of
    record and is the framework's business, and a line the framework wrote for
    itself — the moment, the place — is not something the reader has to read
    around.
    """
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        if not raw.strip():
            lines.append("")
            continue
        body = _strip_markers(raw)
        if not body:
            continue
        if body.lower().startswith(("time:", "location:", "world time:")):
            continue
        lines.append(body)

    out = "\n".join(lines).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ── the sessions ─────────────────────────────────────────────────────────

def sessions() -> List[Dict[str, Any]]:
    """Every session this world closed, oldest first.

    A session is the record the scene manager wrote when it closed: the moment
    it closed at, and the narrative of its events. What is returned is what the
    reader shows — the number of the session in this world, its moment, its
    prose, and a short opening for the list to name it by.
    """
    records = _records(GAME / "scene_manager_messages.json")
    out: List[Dict[str, Any]] = []
    for record in records:
        text = _content(record)
        if not _is(record, "session_end", _SESSION_END):
            continue
        body = _plain(text)
        # The moment stands in the line the record wrote for itself, then in its
        # envelope, and in the stamp a raw record wears.
        stamp = _STAMP.match(text.strip())
        moment = str(stamp.group(1)).strip() if stamp else ""
        for line in text.splitlines():
            found = _TIME_LINE.match(line)
            if found and found.group(1):
                moment = found.group(1).strip()
                break
        if not moment:
            moment = str(record.get("at") or "")
        if not body:
            continue
        opening = next((p.strip() for p in body.split("\n\n") if p.strip()), "")
        out.append({
            "index": len(out),
            "number": len(out) + 1,
            "world_time": moment,
            "text": body,
            "opening": opening,
            "paragraphs": [p for p in body.split("\n\n") if p.strip()],
        })
    return out


# ── chapters: what the sessions fall into ────────────────────────────────

def _order(time_text: str) -> Tuple[int, str]:
    """Sort key for a world time, so times compare without parsing them."""
    from world.time import WorldTime

    try:
        return (WorldTime.parse(str(time_text)).to_seconds(), str(time_text))
    except Exception:
        return (0, str(time_text))


def _holds(start: str, end: str, moment: str) -> bool:
    """Whether a moment falls in a stretch of world time."""
    if not moment:
        return False
    low, high = _order(start), _order(end)
    now = _order(moment)
    if low[0] and high[0]:
        return low[0] <= now[0] <= high[0]
    return str(start) <= str(moment) <= str(end)


def chapters() -> List[Dict[str, Any]]:
    """The world's chapters and the sessions that fall in them.

    A chapter is the world's own summary of a stretch of play — a paragraph, or
    an arc that covers several of them — with the world times it covers. Each
    session is placed in the narrowest stretch that holds it, so a reader can go
    from a chapter to the sessions played under it rather than read the summary.
    A world that keeps no story yet has no chapters, and the list is the
    sessions themselves.
    """
    path = _story_path()
    if path is None:
        return []
    try:
        story = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(story, dict):
        return []

    spans: List[Dict[str, Any]] = []
    arcs = [a for a in (story.get("arcs") or []) if isinstance(a, dict)]
    for arc in arcs:
        spans.append({
            "kind": "arc",
            "name": str(arc.get("name") or "").strip() or "Глава",
            "summary": str(arc.get("summary") or "").strip(),
            "start": str(arc.get("start_time") or "").strip(),
            "end": str(arc.get("end_time") or "").strip(),
        })
    for para in [p for p in (story.get("paragraphs") or []) if isinstance(p, dict)]:
        spans.append({
            "kind": "paragraph",
            "name": str(para.get("name") or "").strip() or "Часть",
            "summary": str(para.get("summary") or "").strip(),
            "start": str(para.get("start_time") or "").strip(),
            "end": str(para.get("end_time") or "").strip(),
        })

    placed = sessions()
    for span in spans:
        span["sessions"] = [
            s["number"] for s in placed
            if _holds(span["start"], span["end"], s["world_time"])
        ]
    # A session belongs to the narrowest stretch that holds it: a paragraph seen
    # through its arc would otherwise be listed under both.
    narrowest: Dict[int, int] = {}
    for span in spans:
        for number in span["sessions"]:
            others = [s for s in spans if number in s["sessions"]]
            if not others:
                continue
            pick = min(others, key=lambda s: len(s["sessions"]))
            narrowest.setdefault(number, id(pick))
    for span in spans:
        span["sessions"] = [n for n in span["sessions"] if narrowest.get(n) == id(span)]
    return [s for s in spans if s["sessions"]]


# ── what one session was made of ─────────────────────────────────────────

_ACCEPTED = "[accepted]"


def _session_window(placed: List[Dict[str, Any]], number: int) -> Tuple[str, str]:
    """The stretch of world time one session covers: after the one before it."""
    before = ""
    for item in placed:
        if item["number"] == number - 1:
            before = item["world_time"]
    for item in placed:
        if item["number"] == number:
            return before, item["world_time"]
    return "", ""


def _in_window(moment: str, low: str, high: str) -> bool:
    """Whether a moment falls in the stretch one session covers.

    A session covers what stands after the close of the one before it and up to
    its own close: the first session has nothing on that side of it, and a deed
    recorded at the moment a session closed belongs to the session that closed
    there and not to the one that came after. A record that names no moment
    cannot be placed at all and is left where it stands.
    """
    if not moment:
        return True
    if high and _order(moment) > _order(high):
        return False
    if low and _order(moment) <= _order(low):
        return False
    return True


def _accepted_records(close_moment: str) -> List[Dict[str, Any]]:
    """What the scene manager read and what it brought to pass, in one session.

    The file is read in order and the session is the stretch between the two
    closes that bound it: a record's own moment is a truer thing to know than to
    place by, because a session makes its first records at the moment the one
    before it closed and the two would answer for each other.

    One record is one deed: the line the scene manager wrote names the act, and
    what follows it is the act's own text. Who perceived it is the framework's
    mark on the deed and is read out beside it rather than among the text.
    """
    span: List[str] = []
    for record in _records(GAME / "scene_manager_messages.json"):
        text = _content(record)
        if _is(record, "session_end", _SESSION_END):
            # The close names its moment twice — in the body, and in the stamp the
            # framework puts in front of every record — and either one places it.
            moments = set()
            stamp = _STAMP.match(text.strip())
            if stamp:
                moments.add(str(stamp.group(1)).strip())
            at = str(record.get("at") or "")
            if at:
                moments.add(at)
            for line in text.splitlines():
                found = _TIME_LINE.match(line)
                if found and found.group(1):
                    moments.add(found.group(1).strip())
            if close_moment and close_moment in moments:
                break
            span = []
            continue
        if _is(record, "accepted_intent", _ACCEPTED):
            span.append(text)

    out: List[Dict[str, Any]] = []
    for text in span:
        lines = text.splitlines()
        head = next((i for i, line in enumerate(lines) if _ACCEPTED in line), 0)
        stamp = _STAMP.match(text.strip())
        moment = str(stamp.group(1)).strip() if stamp else ""
        act = _plain(lines[head])
        # The record a deed closes with names it by the id it was filed under —
        # the id is the framework's, and what a reader is told is that the deed
        # named above it came to an end.
        if act.startswith("—") and "resolved" in act.lower():
            act = "это намерение доведено до конца"
        seen: List[str] = []
        told: List[str] = []
        for line in (_plain(raw) for raw in lines[head + 1:]):
            if not line:
                continue
            if line.lower().startswith("perceived by:"):
                seen = [n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()]
                continue
            if line.lower().startswith("timing:"):
                continue
            told.append(line)
        out.append({"moment": moment, "act": act,
                    "text": "\n".join(told), "seen_by": seen})
    return out


def _events(low: str, high: str) -> List[Dict[str, Any]]:
    """What took place in the world in one session."""
    path = GAME / "entity_store" / "events.jsonl"
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        moment = str(event.get("logged_at") or "")
        if not _in_window(moment, low, high):
            continue
        out.append({
            "id": str(event.get("event_id") or ""),
            "moment": moment,
            "description": _plain(str(event.get("description") or "")),
            "entities": [str(e) for e in (event.get("entity_ids") or [])],
            "tag": str(event.get("tag") or ""),
        })
    return out


def session_details(number: int, placed: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """What one session was made of: the attempts taken, and what took place."""
    placed = placed if placed is not None else sessions()
    low, high = _session_window(placed, number)
    item = next((s for s in placed if s["number"] == number), None)
    return {
        "accepted": _accepted_records(item["world_time"] if item else ""),
        "events": _events(low, high),
        "window": (low, high),
    }


def characters() -> List[str]:
    """The characters this world holds, by their own files."""
    players = GAME / "players"
    if not players.is_dir():
        return []
    return sorted((p.name for p in players.iterdir() if p.is_dir()), key=str.lower)


# ── the points the campaign passed through ───────────────────────────────

def points() -> List[Dict[str, Any]]:
    """Every point this game kept, oldest first."""
    from run_session import points as _points

    return _points()


def line_at() -> Optional[int]:
    """Where the line of play currently stands, when the game says."""
    from run_session import LINE, _read_json

    at = _read_json(LINE).get("at")
    return int(at) if isinstance(at, int) else None


def session_logs() -> List[Path]:
    """The logs of the sessions that were played, newest first."""
    logs = WORKSPACE / "logs" / "sessions"
    if not logs.is_dir():
        return []
    return sorted((p for p in logs.glob("*.txt")), key=lambda p: p.name, reverse=True)


# ── the hand on one character ────────────────────────────────────────────

def _timeline_path() -> Path:
    return GAME / "world" / "timeline.json"


def _timeline() -> Dict[str, Any]:
    try:
        data = json.loads(_timeline_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_timeline(data: Dict[str, Any]) -> None:
    path = _timeline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def player_intents(name: str) -> List[Dict[str, Any]]:
    """This character's attempts that are still standing."""
    out: List[Dict[str, Any]] = []
    for intent in (_timeline().get("intents") or []):
        if not isinstance(intent, dict):
            continue
        if str(intent.get("actor") or "") != str(name):
            continue
        if str(intent.get("status") or "active") != "active":
            continue
        out.append({
            "id": str(intent.get("id") or ""),
            "action_type": str(intent.get("action_type") or ""),
            "target": str(intent.get("target") or ""),
            "description": str(intent.get("description") or ""),
            "t_start": intent.get("t_start"),
            "t_end": intent.get("t_end"),
            "trigger": str(intent.get("trigger_condition") or ""),
        })
    return out


def player_situation(name: str) -> Dict[str, Any]:
    """Where this character stands and what he is doing, as the world holds it."""
    try:
        store = json.loads((GAME / "entity_store" / "entities.json").read_text(encoding="utf-8"))
    except Exception:
        store = {}
    entities = store.get("entities", store) if isinstance(store, dict) else {}
    entity = entities.get(name) if isinstance(entities, dict) else None
    entity = entity if isinstance(entity, dict) else {}
    position = entity.get("position") or {}
    place = ""
    if isinstance(position, dict):
        place = str(position.get("inside") or position.get("near") or "")
    return {
        "place": place,
        "current_state": _plain(str(entity.get("current_state") or "")),
        "world_time": world_time(),
    }


def player_memory(name: str, limit: int = 14) -> List[Dict[str, str]]:
    """The last records of this character's memory, in the order he read them."""
    rows = _records(GAME / "players" / name / "messages.json")
    out: List[Dict[str, str]] = []
    for record in rows[-limit:]:
        text = _plain(_content(record))
        if text:
            out.append({"text": text})
    return out


def declare_for(
    name: str,
    *,
    action_type: str,
    target: str,
    description: str,
    t_start: str = "0s",
    duration: str = "",
    visibility: str = "public",
    absorption: str = "alert",
    trigger_condition: str = "",
) -> Dict[str, Any]:
    """Put an attempt of this character's into the world, as his own turn would.

    The attempt is written where the game reads what stands: the timeline the
    cycle resolves from. It is an attempt and not an outcome — nothing here
    settles on its own — and the character who carries it is a character with
    something running, so the cycle does not ask his agent for a decision.
    """
    from scheduler.timeline import Intent, new_intent_id, parse_duration_seconds
    from world.time import WorldTime

    now = WorldTime.parse(world_time()).to_seconds()
    begin = now + max(0, parse_duration_seconds(t_start, default=0))
    span = str(duration or "").strip()
    intent = Intent(
        id=new_intent_id(), actor=str(name), action_type=str(action_type),
        target=str(target or ""), description=str(description or "").strip(),
        t_start=begin,
        t_end=(begin + max(0, parse_duration_seconds(span, default=0)) if span else None),
        visibility=str(visibility or "public"),
        trigger_condition=(str(trigger_condition or "").strip() or None),
        absorption=str(absorption or "alert"),
    )
    data = _timeline()
    intents = [i for i in (data.get("intents") or []) if isinstance(i, dict)]
    intents.append(intent.to_dict())
    data["intents"] = intents
    _write_timeline(data)
    return {"id": intent.id, "actor": intent.actor, "t_start": begin, "t_end": intent.t_end}


def withdraw_intent(intent_id: str) -> bool:
    """Take one of this character's standing attempts out of the world."""
    data = _timeline()
    touched = False
    for intent in (data.get("intents") or []):
        if isinstance(intent, dict) and str(intent.get("id") or "") == str(intent_id):
            intent["status"] = "cancelled"
            touched = True
    if touched:
        _write_timeline(data)
    return touched

