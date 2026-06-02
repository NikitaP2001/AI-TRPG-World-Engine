"""Scene context utilities — history-based helpers for scene description and reuse.

Extracted from ConsoleApp to keep pure world-query logic separate from orchestration.
All functions take ``world: World`` explicitly; they have no side effects.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from world import World, WorldDuration
from world.time import WorldTime


# ---------------------------------------------------------------------------
# Turn fingerprinting
# ---------------------------------------------------------------------------

def story_turn_fingerprint(turn: Dict[str, Any]) -> str:
    """Return a stable JSON string that uniquely identifies a story turn."""
    if not isinstance(turn, dict):
        return ""
    chars = turn.get("characters") if isinstance(turn.get("characters"), list) else []
    npcs = turn.get("npcs") if isinstance(turn.get("npcs"), list) else []
    data = {
        "start_time": str(turn.get("start_time") or "").strip(),
        "end_time": str(turn.get("end_time") or "").strip(),
        "location": str(turn.get("location") or "").strip(),
        "characters": sorted(str(x).strip() for x in chars if str(x).strip()),
        "npcs": sorted(str(x).strip() for x in npcs if str(x).strip()),
        "narration": str(turn.get("narration") or "").strip(),
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Story turn collection
# ---------------------------------------------------------------------------

def collect_story_turns_newest_first(world: World) -> List[Dict[str, Any]]:
    """Return all story turns across all arcs, newest first."""
    out: List[Dict[str, Any]] = []
    try:
        arcs = world.get_story()
    except Exception:
        return out
    if not isinstance(arcs, list):
        return out

    for arc in arcs:
        if not isinstance(arc, dict):
            continue

        ongoing = arc.get("ongoing_paragraph") if isinstance(arc.get("ongoing_paragraph"), dict) else {}
        ongoing_turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
        for t in reversed(ongoing_turns):
            if isinstance(t, dict):
                out.append(t)

        paragraphs = arc.get("paragraphs") if isinstance(arc.get("paragraphs"), list) else []
        for p in reversed(paragraphs):
            if not isinstance(p, dict):
                continue
            p_turns = p.get("turns") if isinstance(p.get("turns"), list) else []
            for t in reversed(p_turns):
                if isinstance(t, dict):
                    out.append(t)

    return out


# ---------------------------------------------------------------------------
# Scene description reuse
# ---------------------------------------------------------------------------

def extract_scene_result_from_narration(narration: str) -> str:
    """Extract the reusable result portion from a turn narration string."""
    text = str(narration or "")
    if not text.strip():
        return ""

    outcome_marker = "Outcome:"
    outcome_idx = text.find(outcome_marker)
    if outcome_idx != -1:
        return text[outcome_idx + len(outcome_marker):].strip()

    actions_marker = "Actions:"
    actions_idx = text.find(actions_marker)
    if actions_idx != -1:
        tail = text[actions_idx + len(actions_marker):]
        # Stored narration format uses \n\n between Actions block and GM outcome.
        sep_idx = tail.find("\n\n")
        if sep_idx != -1:
            return tail[sep_idx + 2:].strip()
        return ""

    # Fallback: if no explicit Actions marker is present, treat full narration
    # as reusable result text (legacy/alternate formatting).
    return text.strip()


def find_reusable_scene_description(
    world: World,
    *,
    selected_characters: List[str],
    selected_location: str,
    selected_npcs: List[str],
) -> str:
    """Return a prior scene description if the same cast/location last appeared together.

    Returns an empty string when no reusable description is available.
    """
    if not selected_characters or not selected_location:
        return ""

    turns = collect_story_turns_newest_first(world)
    if not turns:
        return ""

    newest_turn = turns[0] if isinstance(turns[0], dict) else None

    def _last_turn_for_character(name: str) -> Optional[Dict[str, Any]]:
        target = str(name or "").strip()
        if not target:
            return None
        for turn in turns:
            chars = turn.get("characters") if isinstance(turn.get("characters"), list) else []
            if any(str(x).strip() == target for x in chars):
                return turn
        return None

    last_turns: List[Dict[str, Any]] = []
    for cname in selected_characters:
        turn = _last_turn_for_character(cname)
        if not isinstance(turn, dict):
            return ""
        last_turns.append(turn)

    fps = {story_turn_fingerprint(t) for t in last_turns if isinstance(t, dict)}
    if len([x for x in fps if x]) != 1:
        return ""

    turn0 = last_turns[0]

    # Reuse only when this matched shared turn is the latest turn overall.
    # If any other scene happened in between, it may carry world updates
    # that should be reintroduced via a fresh scene description.
    if isinstance(newest_turn, dict):
        if story_turn_fingerprint(turn0) != story_turn_fingerprint(newest_turn):
            return ""

    turn_chars = turn0.get("characters") if isinstance(turn0.get("characters"), list) else []
    turn_char_names = sorted(str(x).strip() for x in turn_chars if str(x).strip())
    selected_char_names = sorted(str(x).strip() for x in (selected_characters or []) if str(x).strip())
    if turn_char_names != selected_char_names:
        return ""

    if str(turn0.get("location") or "").strip() != str(selected_location or "").strip():
        return ""

    turn_npcs = turn0.get("npcs") if isinstance(turn0.get("npcs"), list) else []
    turn_npc_names = sorted(str(x).strip() for x in turn_npcs if str(x).strip())
    selected_npc_names = sorted(str(x).strip() for x in (selected_npcs or []) if str(x).strip())
    if turn_npc_names != selected_npc_names:
        return ""

    return extract_scene_result_from_narration(str(turn0.get("narration") or ""))


# ---------------------------------------------------------------------------
# Focused scene context
# ---------------------------------------------------------------------------

def build_focused_scene_context(
    world: World,
    *,
    selected_characters: List[str],
    selected_location: str,
    selected_npcs: List[str],
) -> Tuple[str, List[str], List[str]]:
    """Build a focused context blob for the scene description GM call.

    Returns ``(context_text, missing_locations, missing_npcs)``.
    ``context_text`` is empty when no useful per-entity history was found.
    """
    turns = collect_story_turns_newest_first(world)
    recent_turns = turns[:10]
    seen_fingerprints = {
        fp for fp in (story_turn_fingerprint(t) for t in recent_turns) if fp
    }

    def _add_turn_if_new(bucket: List[Dict[str, Any]], turn: Dict[str, Any]) -> None:
        fp = story_turn_fingerprint(turn)
        if not fp or fp in seen_fingerprints:
            return
        seen_fingerprints.add(fp)
        bucket.append(turn)

    character_last_turns: List[Dict[str, Any]] = []
    for cname in selected_characters:
        for t in turns:
            chars = t.get("characters") if isinstance(t.get("characters"), list) else []
            if any(str(x).strip() == cname for x in chars):
                _add_turn_if_new(character_last_turns, t)
                break

    location_last_turns: List[Dict[str, Any]] = []
    if selected_location:
        for t in turns:
            if str(t.get("location") or "").strip() == selected_location:
                _add_turn_if_new(location_last_turns, t)
                break

    npc_last_turns: List[Dict[str, Any]] = []
    for npc_name in selected_npcs:
        for t in turns:
            npcs = t.get("npcs") if isinstance(t.get("npcs"), list) else []
            if any(str(x).strip() == npc_name for x in npcs):
                _add_turn_if_new(npc_last_turns, t)
                break

    missing_locations: List[str] = []
    selected_location_entry: Optional[Dict[str, Any]] = None
    if selected_location:
        try:
            selected_location_entry = world.get_location(selected_location)
        except Exception:
            missing_locations.append(selected_location)

    missing_npcs: List[str] = []
    selected_npc_entries: List[Dict[str, Any]] = []
    try:
        npc_map = world.get_npcs()
    except Exception:
        npc_map = {}
    if not isinstance(npc_map, dict):
        npc_map = {}

    for npc_name in selected_npcs:
        entry = npc_map.get(npc_name)
        if isinstance(entry, dict):
            selected_npc_entries.append(entry)
        else:
            missing_npcs.append(npc_name)

    focused_payload: Dict[str, Any] = {
        "selected_scene": {
            "location": selected_location,
            "characters": selected_characters,
            "npcs": selected_npcs,
        }
    }
    if selected_location_entry:
        focused_payload["selected_location_entry"] = selected_location_entry
    if character_last_turns:
        focused_payload["last_turns_for_selected_characters"] = character_last_turns
    if location_last_turns:
        focused_payload["last_turn_for_selected_location"] = location_last_turns[0]
    if selected_npc_entries:
        focused_payload["selected_npc_entries"] = selected_npc_entries
    if npc_last_turns:
        focused_payload["last_turns_for_selected_npcs"] = npc_last_turns

    if len(focused_payload) == 1:
        return "", missing_locations, missing_npcs

    text = "### Focused selected-scene context\n" + json.dumps(
        focused_payload,
        ensure_ascii=False,
        indent=2,
    )
    return text, missing_locations, missing_npcs


# ---------------------------------------------------------------------------
# Scene start-time estimation
# ---------------------------------------------------------------------------

def estimate_scene_start_time_for_history(
    world: World,
    *,
    selected_characters: List[str],
    scene_time_shift: str,
) -> str:
    """Best-effort start-time estimate for scene-description history anchoring."""
    try:
        world_now = world.get_world_time().to_string()
    except Exception:
        world_now = ""

    times_sec: List[int] = []
    for cname in selected_characters:
        try:
            desc = world.get_character_description(str(cname))
        except Exception:
            desc = {}
        la = str(desc.get("last_acted") or "").strip() if isinstance(desc, dict) else ""
        if not la or la == "never":
            continue
        try:
            times_sec.append(WorldTime.parse(la).to_seconds())
        except Exception:
            continue

    try:
        if times_sec:
            # Multi-character scenes are timeline-aligned to max selected time before start.
            if len([x for x in selected_characters if str(x).strip()]) > 1:
                base = WorldTime.from_seconds(max(times_sec))
            else:
                base = WorldTime.from_seconds(min(times_sec))
        else:
            base = WorldTime.parse(world_now) if world_now else world.get_world_time()

        shift = str(scene_time_shift or "0").strip() or "0"
        if shift.lower() not in {"0", "0s", "0m", "0h", "0d"}:
            base = base.add_duration(WorldDuration.parse_user_input(shift))
        return base.to_string()
    except Exception:
        return world_now
