from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class TurnKey:
    start_time: str
    end_time: str
    location: str


@dataclass
class CharacterRecap:
    name: str
    last_decision: str
    last_thoughts: str


@dataclass
class TurnDetails:
    key: TurnKey
    narration: str
    location_summary: str
    turn_duration: str
    character_inputs: Dict[str, str]
    characters: List[CharacterRecap]


@dataclass
class ParagraphRef:
    arc_index: int
    kind: str  # 'ongoing' | 'paragraph'
    paragraph_index: Optional[int]


class FileCache:
    def __init__(self, path: str):
        self.path = path
        self._mtime: Optional[float] = None
        self._data: Any = None

    def load_json(self) -> Any:
        if not os.path.exists(self.path):
            self._mtime = None
            self._data = None
            return None
        mtime = os.path.getmtime(self.path)
        if self._mtime is None or mtime != self._mtime:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._mtime = mtime
        return self._data

    def mtime(self) -> float:
        if not os.path.exists(self.path):
            return 0.0
        return os.path.getmtime(self.path)


def _safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _split_narration(narration: str) -> Tuple[str, str]:
    """Split into (story_prose, appended_meta)."""
    markers = ["\nScene:", "\nScene summary:", "\nTime:", "\nActions:"]
    idxs = [narration.find(m) for m in markers]
    idxs = [i for i in idxs if i != -1]
    if not idxs:
        return narration.strip(), ""
    cut = min(idxs)
    return narration[:cut].rstrip(), narration[cut:].strip()


_ACTION_HEADER_BLACKLIST = {
    "location",
    "scene",
    "scene description",
    "scene summary",
    "time",
    "actions",
}


def _parse_action_line(line: str) -> Optional[Tuple[str, str]]:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("-"):
        stripped = stripped[1:].strip()
    if ":" not in stripped:
        return None
    name, action = stripped.split(":", 1)
    name = name.strip()
    if not name:
        return None
    if name.lower() in _ACTION_HEADER_BLACKLIST:
        return None
    action = action.strip()
    if not action:
        return None
    return name, action


def _split_actions_and_remainder(text: str) -> Tuple[List[Tuple[str, str]], str]:
    lines = text.splitlines()
    actions: List[Tuple[str, str]] = []
    remainder_lines: List[str] = []
    found_action = False

    for idx, line in enumerate(lines):
        parsed = _parse_action_line(line)
        if parsed:
            found_action = True
            actions.append(parsed)
            continue
        if found_action:
            remainder_lines = lines[idx:]
            break

    remainder = "\n".join(remainder_lines).strip()
    return actions, remainder


def _parse_actions_from_text(text: str) -> List[Dict[str, str]]:
    """Parse an appended Actions block if present."""
    if not text:
        return []
    marker = "Actions:"
    idx = text.find(marker)
    if idx == -1:
        return []
    tail = text[idx + len(marker) :]
    actions, _ = _split_actions_and_remainder(tail)
    rows: List[Dict[str, str]] = []
    for name, action in actions:
        rows.append({"name": name, "intent": action})
    return rows


def _split_scene_and_outcome(narration: str) -> Tuple[str, str]:
    text = (narration or "").strip()
    if not text:
        return "", ""

    scene_marker = "Scene description:"
    outcome_marker = "Outcome:"
    actions_marker = "Actions:"

    scene_idx = text.find(scene_marker)
    outcome_idx = text.find(outcome_marker)
    actions_idx = text.find(actions_marker)

    # Fast path: explicit "Outcome:" sentinel (new format).
    if outcome_idx != -1:
        outcome_text = text[outcome_idx + len(outcome_marker):].strip()
        if scene_idx != -1:
            scene_start = scene_idx + len(scene_marker)
            candidates = [i for i in [actions_idx, outcome_idx] if i > scene_start]
            scene_end = min(candidates) if candidates else len(text)
            scene_text = text[scene_start:scene_end].strip()
        else:
            scene_text = ""
        return scene_text, outcome_text

    # Legacy: no "Outcome:" marker — fall back to Actions: heuristic parsing.
    scene_text = ""
    outcome_text = ""

    if scene_idx != -1:
        scene_start = scene_idx + len(scene_marker)
        scene_end = actions_idx if actions_idx != -1 and actions_idx > scene_start else len(text)
        scene_text = text[scene_start:scene_end].strip()
        if actions_idx != -1 and actions_idx > scene_start:
            tail = text[actions_idx + len(actions_marker) :]
            _, remainder = _split_actions_and_remainder(tail)
            outcome_text = remainder.strip()
    else:
        if actions_idx != -1:
            before = text[:actions_idx].strip()
            tail = text[actions_idx + len(actions_marker) :]
            _, remainder = _split_actions_and_remainder(tail)
            scene_text = before
            outcome_text = remainder.strip()
        else:
            scene_text = text
            outcome_text = ""

    return scene_text, outcome_text


def _shorten(s: str, max_len: int = 140) -> str:
    return " ".join((s or "").split())


class WorldStateReader:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.story_cache = FileCache(os.path.join(repo_root, "game", "world", "story.json"))
        self.storage_assistant_messages_cache = FileCache(
            os.path.join(repo_root, "game", "storage_assistant_messages.json")
        )

    def get_arc_tree(self) -> List[Dict[str, Any]]:
        story = self.story_cache.load_json()
        arcs: List[Dict[str, Any]] = []
        for arc_index, arc in enumerate(story or []):
            paragraphs = arc.get("paragraphs", []) or []
            ongoing = arc.get("ongoing_paragraph")
            is_current_arc = (arc_index == 0)

            arc_item: Dict[str, Any] = {
                "arc_index": arc_index,
                "name": arc.get("name", f"Arc {arc_index}"),
                "paragraphs": [],
                "has_ongoing": bool(is_current_arc and ongoing is not None),
            }

            if is_current_arc and ongoing is not None:
                arc_item["ongoing"] = {
                    "kind": "ongoing",
                    "name": ongoing.get("name", "Ongoing"),
                    "start_time": ongoing.get("start_time"),
                    "end_time": ongoing.get("end_time"),
                }

            for i, p in enumerate(paragraphs):
                arc_item["paragraphs"].append(
                    {
                        "kind": "paragraph",
                        "paragraph_index": i,
                        "name": p.get("name", f"Paragraph {i}"),
                        "start_time": p.get("start_time"),
                        "end_time": p.get("end_time"),
                        "summary": p.get("summary", ""),
                        "turn_count": len(p.get("turns", []) or []),
                    }
                )

            arcs.append(arc_item)
        return arcs

    def get_paragraph(self, ref: ParagraphRef) -> Dict[str, Any]:
        story = self.story_cache.load_json()
        arcs = story if isinstance(story, list) else []

        if not isinstance(ref.arc_index, int) or ref.arc_index < 0 or ref.arc_index >= len(arcs):
            default_name = "Ongoing" if ref.kind == "ongoing" else "Paragraph 0"
            return {"name": default_name, "turns": []}

        arc = arcs[ref.arc_index] if isinstance(arcs[ref.arc_index], dict) else {}
        if ref.kind == "ongoing":
            p = arc.get("ongoing_paragraph") or {}
            return {"name": p.get("name", "Ongoing"), "turns": p.get("turns", []) or []}
        paragraphs = arc.get("paragraphs", []) or []
        idx = ref.paragraph_index or 0
        p = paragraphs[idx] if 0 <= idx < len(paragraphs) else {}
        return {"name": p.get("name", f"Paragraph {idx}"), "turns": p.get("turns", []) or []}

    def get_last_turns(self, ref: ParagraphRef, limit: int = 10) -> List[Dict[str, Any]]:
        para = self.get_paragraph(ref)
        turns = para.get("turns", [])
        return turns[-limit:]

    def build_turn_details_index(self) -> Dict[TurnKey, TurnDetails]:
        """Index turn recaps keyed by (start_time, end_time, location).

        Reads from ``turn_recaps.jsonl`` (one JSON object per line).  Falls back
        to scanning ``storage_assistant_messages.json`` for legacy
        ``gm_output_turn`` tool messages so older game states still work.
        """
        index: Dict[TurnKey, TurnDetails] = {}

        # -- Primary source: turn_recaps.jsonl ---------------------------------
        recaps_path = os.path.join(self.repo_root, "game", "turn_recaps.jsonl")
        if os.path.exists(recaps_path):
            try:
                mtime = os.path.getmtime(recaps_path)
                if (
                    not hasattr(self, "_recaps_mtime")
                    or self._recaps_mtime is None
                    or mtime != self._recaps_mtime
                ):
                    with open(recaps_path, "r", encoding="utf-8") as f:
                        self._recaps_lines = f.readlines()
                    self._recaps_mtime = mtime
                for line in (self._recaps_lines or []):
                    out = _safe_json_loads(line)
                    if not out:
                        continue
                    recap = out.get("scene_recap") or {}
                    start_time = recap.get("start_time")
                    location = out.get("location") or recap.get("location") or ""
                    end_time = out.get("new_time")
                    if not (start_time and end_time and location):
                        continue
                    characters: List[CharacterRecap] = []
                    for ch in recap.get("characters", []) or []:
                        characters.append(
                            CharacterRecap(
                                name=ch.get("name", ""),
                                last_decision=ch.get("last_decision", ""),
                                last_thoughts=ch.get("last_thoughts", ""),
                            )
                        )
                    key = TurnKey(start_time=start_time, end_time=end_time, location=location)
                    index[key] = TurnDetails(
                        key=key,
                        narration=out.get("narration", ""),
                        location_summary=out.get("location_summary", ""),
                        turn_duration=out.get("turn_duration", ""),
                        character_inputs={},
                        characters=characters,
                    )
            except Exception:
                pass

        # -- Fallback: legacy gm_output_turn in SA messages --------------------
        if not index:
            msgs = self.storage_assistant_messages_cache.load_json() or []
            current_scene_start_time: Optional[str] = None
            current_scene_location: Optional[str] = None

            for msg in msgs:
                msg_type = msg.get("type")

                if msg_type == "tool" and msg.get("name") in {"start_scene", "run_scene"}:
                    scene = _safe_json_loads(msg.get("content", "")) or {}
                    current_scene_start_time = scene.get("start_time")
                    current_scene_location = scene.get("location")
                    continue

                if msg_type == "tool" and msg.get("name") == "gm_output_turn":
                    out = _safe_json_loads(msg.get("content", "")) or {}
                    recap = out.get("scene_recap") or {}
                    start_time = recap.get("start_time") or current_scene_start_time
                    location = (out.get("location") or recap.get("location") or current_scene_location or "")
                    end_time = out.get("new_time")
                    if not (start_time and end_time and location):
                        current_scene_start_time = None
                        current_scene_location = None
                        continue
                    characters_legacy: List[CharacterRecap] = []
                    for ch in recap.get("characters", []) or []:
                        characters_legacy.append(
                            CharacterRecap(
                                name=ch.get("name", ""),
                                last_decision=ch.get("last_decision", ""),
                                last_thoughts=ch.get("last_thoughts", ""),
                            )
                        )
                    key = TurnKey(start_time=start_time, end_time=end_time, location=location)
                    index[key] = TurnDetails(
                        key=key,
                        narration=out.get("narration", ""),
                        location_summary=out.get("location_summary", ""),
                        turn_duration=out.get("turn_duration", ""),
                        character_inputs={},
                        characters=characters_legacy,
                    )
                    current_scene_start_time = None
                    current_scene_location = None

        return index

    def enrich_turn(self, turn: Dict[str, Any], details_index: Dict[TurnKey, TurnDetails]) -> Dict[str, Any]:
        key = TurnKey(
            start_time=turn.get("start_time", ""),
            end_time=turn.get("end_time", ""),
            location=turn.get("location", ""),
        )
        details = details_index.get(key)

        narration = turn.get("narration", "") or ""
        prose, meta = _split_narration(narration)
        scene_description, outcome = _split_scene_and_outcome(narration)
        if not outcome and not scene_description:
            outcome = prose or narration

        # Build a stable "participants" list from story.json, then enrich with any
        # captured gm_output_turn recap details (if present).
        turn_characters = [str(x) for x in (turn.get("characters") or []) if str(x).strip()]

        by_name: Dict[str, Dict[str, str]] = {}
        if details is not None:
            for ch in (details.characters or []):
                by_name[str(ch.name)] = {
                    "name": str(ch.name),
                    "intent": ch.last_decision,
                    "intent_short": _shorten(ch.last_decision, 160),
                    "thoughts": ch.last_thoughts,
                    "thoughts_short": _shorten(ch.last_thoughts, 160),
                    "character_input": details.character_inputs.get(ch.name, ""),
                }

        action_rows: List[Dict[str, str]] = []
        for name in turn_characters:
            row = by_name.get(name)
            if row is None:
                row = {
                    "name": name,
                    "intent": "",
                    "intent_short": "(no captured action)",
                    "thoughts": "",
                    "thoughts_short": "",
                    "character_input": (details.character_inputs.get(name, "") if details else ""),
                }
            action_rows.append(row)

        # Fallback/merge: parse an Actions block from narration text.
        parsed = _parse_actions_from_text(meta or narration)
        parsed_map = {
            str(a.get("name", "") or "").strip(): str(a.get("intent", "") or "")
            for a in parsed
            if str(a.get("name", "") or "").strip()
        }

        if not action_rows:
            for nm, action in parsed_map.items():
                action_rows.append(
                    {
                        "name": nm,
                        "intent": action,
                        "intent_short": _shorten(action, 160),
                        "thoughts": "",
                        "thoughts_short": "",
                        "character_input": "",
                    }
                )
        else:
            for row in action_rows:
                if row.get("intent"):
                    continue
                nm = str(row.get("name", "") or "").strip()
                action = parsed_map.get(nm, "")
                if action:
                    row["intent"] = action
                    row["intent_short"] = _shorten(action, 160)
            for nm, action in parsed_map.items():
                if any(r.get("name") == nm for r in action_rows):
                    continue
                action_rows.append(
                    {
                        "name": nm,
                        "intent": action,
                        "intent_short": _shorten(action, 160),
                        "thoughts": "",
                        "thoughts_short": "",
                        "character_input": "",
                    }
                )

        return {
            "start_time": key.start_time,
            "end_time": key.end_time,
            "location": key.location,
            "characters": turn.get("characters", []) or [],
            "npcs": turn.get("npcs", []) or [],
            "prose": prose,
            "scene_description": scene_description,
            "outcome": outcome,
            "meta": meta,
            "raw_narration": narration,
            "has_details": details is not None,
            "details": {
                "turn_duration": details.turn_duration if details else "",
                "location_summary": details.location_summary if details else "",
                "gm_narration": details.narration if details else "",
            },
            "actions": action_rows,
        }
