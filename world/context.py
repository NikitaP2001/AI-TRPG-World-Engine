from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from world.time import WorldTime


def _prepare_scene_for_context(scene: Dict[str, Any], world_time: "WorldTime") -> Dict[str, Any]:
    """Return a copy of *scene* ready for JSON injection into context blocks.

    * Removes the ``state`` key (the section header already says Active scene).
    * For active scenes whose ``start_time`` is behind *world_time*, adds a
      ``behind_world_time_by`` human-readable delta so the GM / SA can see and
      reason about the gap.
    """
    out = {k: v for k, v in scene.items() if k != "state"}
    try:
        st = WorldTime.parse(str(scene.get("start_time", "")))
        delta = world_time.to_seconds() - st.to_seconds()
        if delta > 0 and scene.get("state") == "active":
            out["behind_world_time_by"] = _format_seconds_delta(delta)
    except Exception:
        pass
    return out


def _prepare_scene_for_qa_context(scene: Dict[str, Any], world_time: "WorldTime") -> Dict[str, Any]:
    """Return a compact scene snapshot for ANSWER_QUESTION context.

    This intentionally avoids emitting the full scene payload with all nested
    details. Only essential top-level metadata for the current active scene is
    preserved so the GM can answer perception questions without a large prompt.
    """
    out: Dict[str, Any] = {}
    if not isinstance(scene, dict):
        return out

    for key in [
        "name",
        "location",
        "title",
        "summary",
        "description",
        "start_time",
        "kind",
        "scene_type",
        "mode",
    ]:
        if key in scene:
            out[key] = scene[key]

    chars = scene.get("characters")
    if isinstance(chars, dict):
        out["characters"] = [str(name) for name in chars.keys() if str(name).strip()]
    elif isinstance(chars, list):
        out["characters"] = [str(name) for name in chars if str(name).strip()]

    npcs = scene.get("npcs")
    if isinstance(npcs, list):
        out["npcs"] = [str(name).strip() for name in npcs if str(name).strip()]

    try:
        st = WorldTime.parse(str(scene.get("start_time", "")))
        delta = world_time.to_seconds() - st.to_seconds()
        if delta > 0 and scene.get("state") == "active":
            out["behind_world_time_by"] = _format_seconds_delta(delta)
    except Exception:
        pass

    return out


def _format_seconds_delta(seconds: int) -> str:
    """Format a time delta in seconds into a human-readable string like '1h 23m'."""
    if seconds <= 0:
        return "0m"
    parts: List[str] = []
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _enrich_characters_with_time_behind(
    info_for_prompt: Dict[str, Any],
    world_time: WorldTime,
) -> None:
    """Add ``behind_world_time`` to every character entry inside *info_for_prompt* (in-place).

    When a character has been without an active scene for more than 8 hours,
    an extra ``notice`` field is added to draw the GM's attention.
    """
    _LONG_ABSENCE_SECONDS = 8 * 3600  # 8 hours

    chars = info_for_prompt.get("characters")
    if not isinstance(chars, list):
        return
    wt_sec = world_time.to_seconds()
    for entry in chars:
        if not isinstance(entry, dict):
            continue
        la = str(entry.get("last_acted") or "").strip()
        if not la or la == "never":
            entry["behind_world_time"] = "unknown"
            continue
        try:
            la_wt = WorldTime.parse(la)
            la_sec = la_wt.to_seconds()
            delta = wt_sec - la_sec
            delta = max(delta, 0)
            entry["behind_world_time"] = _format_seconds_delta(delta)
            entry["time_of_day"] = la_wt.time_of_day()
            if delta >= _LONG_ABSENCE_SECONDS:
                entry["notice"] = (
                    f"This player has not been in an active scene for over "
                    f"{_format_seconds_delta(delta)}. Consider giving them a scene soon."
                )
        except Exception:
            entry["behind_world_time"] = "unknown"


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for x in value:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _story_total_turns(arcs: List[Any]) -> int:
    """Count story turns across arcs, excluding legacy plot_seed turns."""
    total = 0
    for arc in (arcs if isinstance(arcs, list) else []):
        if not isinstance(arc, dict):
            continue

        ongoing = _safe_dict(arc.get("ongoing_paragraph"))
        for t in _safe_list(ongoing.get("turns")):
            if isinstance(t, dict) and str(t.get("kind") or "").strip() == "plot_seed":
                continue
            total += 1

        for p in _safe_list(arc.get("paragraphs")):
            if not isinstance(p, dict):
                continue
            for t in _safe_list(p.get("turns")):
                if isinstance(t, dict) and str(t.get("kind") or "").strip() == "plot_seed":
                    continue
                total += 1

    return max(0, total)


def _all_character_descriptions_for_context(
    world: Any,
    *,
    exclude_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Return all character descriptions for early-story bootstrapping context."""
    out: List[Dict[str, Any]] = []
    exclude = {str(x).strip().lower() for x in (exclude_names or []) if str(x).strip()}
    try:
        names = world.list_character_names()
    except Exception:
        return out

    for name in names:
        nm = str(name).strip()
        if not nm:
            continue
        if nm.lower() in exclude:
            continue
        try:
            desc = world.get_character_description(nm)
            if isinstance(desc, dict):
                desc = {k: v for k, v in desc.items() if str(k) != "last_acted"}
            out.append({"name": nm, "description": desc})
        except Exception as e:  # noqa: BLE001
            out.append({"name": nm, "error": str(e)})

    return out


def _has_meaningful_data(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        return any(_has_meaningful_data(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_data(v) for v in value)
    return True


def _append_section(parts: List[str], title: str, payload: Any) -> None:
    if not _has_meaningful_data(payload):
        return
    if isinstance(payload, str):
        parts.append(f"{title}\n{payload.strip()}")
    else:
        parts.append(f"{title}\n" + json.dumps(payload, ensure_ascii=False, indent=2))


def _arc_total_turns(arc: Dict[str, Any]) -> int:
    paras = _safe_list(arc.get("paragraphs"))
    para_turns = 0
    for p in paras:
        if not isinstance(p, dict):
            continue
        try:
            para_turns += int(p.get("turn_count") or 0)
        except Exception:
            continue

    ongoing = _safe_dict(arc.get("ongoing_paragraph"))
    turns = _safe_list(ongoing.get("turns"))
    return max(0, para_turns + len(turns))


def _last_paragraph_meta(arcs: List[Any]) -> Optional[Dict[str, Any]]:
    """Return metadata for the latest summarized paragraph across arcs.

    Prefers current arc (arcs[0]); if it has no paragraphs, falls back to the most
    recent previous arc with at least one paragraph.
    """

    for arc_index in range(0, len(arcs)):
        arc = arcs[arc_index]
        if not isinstance(arc, dict):
            continue
        paras = arc.get("paragraphs")
        if not isinstance(paras, list) or not paras:
            continue
        last_para = paras[-1]
        if not isinstance(last_para, dict):
            continue
        return {
            "arc_name": str(arc.get("name") or "").strip(),
            "paragraph": {
                "name": last_para.get("name") or "",
                "start_time": last_para.get("start_time") or "",
                "end_time": last_para.get("end_time") or "",
                "locations": _as_str_list(last_para.get("locations")),
                "characters": _as_str_list(last_para.get("characters")),
                "npcs": _as_str_list(last_para.get("npcs")),
                "turn_count": last_para.get("turn_count") or 0,
                "summary": last_para.get("summary") or "",
            },
        }
    return None


def _last_n_paragraph_metas(arcs: List[Any], n: int = 10) -> List[Dict[str, Any]]:
    """Return up to the last N summarized paragraph metadata entries across arcs.

    Selection rule:
    - Prefer the current arc (arcs[0]) first.
    - If it has fewer than N paragraphs, include the most recent paragraphs from
      previous arcs (arcs[1], arcs[2], ...) until we reach N or run out.

    Output order:
    - Oldest -> newest within the returned window (chronological for readability).
    """

    try:
        limit = int(n)
    except Exception:
        limit = 10
    limit = max(0, min(limit, 50))
    if limit <= 0:
        return []

    collected: List[Dict[str, Any]] = []
    for arc_index in range(0, len(arcs)):
        arc = arcs[arc_index]
        if not isinstance(arc, dict):
            continue
        paras = arc.get("paragraphs")
        if not isinstance(paras, list) or not paras:
            continue

        arc_name = str(arc.get("name") or "").strip()
        for p in reversed(paras):
            if not isinstance(p, dict):
                continue
            collected.append(
                {
                    "arc_name": arc_name,
                    "paragraph": {
                        "name": p.get("name") or "",
                        "start_time": p.get("start_time") or "",
                        "end_time": p.get("end_time") or "",
                        "locations": _as_str_list(p.get("locations")),
                        "characters": _as_str_list(p.get("characters")),
                        "npcs": _as_str_list(p.get("npcs")),
                        "turn_count": p.get("turn_count") or 0,
                        "summary": p.get("summary") or "",
                    },
                }
            )
            if len(collected) >= limit:
                break

        if len(collected) >= limit:
            break

    # collected is newest-first; flip to oldest-first for readability.
    return list(reversed(collected))


def _filter_paragraph_metas_for_context(
    para_metas: List[Dict[str, Any]],
    arc_summary: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop paragraph summaries that belong to the same arc as arc_summary.

    This avoids duplicating a finalized arc's full paragraph summaries when we
    already include that arc's top-level summary in context.
    """
    if not para_metas:
        return []
    summarized_arc_name = str((arc_summary or {}).get("arc_name") or "").strip()
    if not summarized_arc_name:
        return para_metas

    filtered: List[Dict[str, Any]] = []
    for meta in para_metas:
        if not isinstance(meta, dict):
            continue
        if str(meta.get("arc_name") or "").strip() == summarized_arc_name:
            continue
        filtered.append(meta)
    return filtered


def _last_arc_summary(arcs: List[Any]) -> Optional[Dict[str, Any]]:
    """Return the most recent arc-level summary, if present.

    Expected future schema (example):
      {"name": "Arc Name", "summary": "...", ...}
    We look at previous arcs first (arcs[1], arcs[2], ...) and fall back to arcs[0]
    if it contains a summary.
    """

    scan_order = list(range(1, len(arcs))) + ([0] if arcs else [])
    for i in scan_order:
        arc = arcs[i]
        if not isinstance(arc, dict):
            continue
        summary = str(arc.get("summary") or "").strip()
        if not summary:
            continue

        out: Dict[str, Any] = {
            "arc_name": str(arc.get("name") or "").strip(),
            "summary": summary,
        }

        # Optional future fields (kept only if present / non-empty).
        for k in ("start_time", "end_time"):
            v = str(arc.get(k) or "").strip()
            if v:
                out[k] = v
        for k in ("locations", "characters", "npcs"):
            v2 = _as_str_list(arc.get(k))
            if v2:
                out[k] = v2
        try:
            pc = int(arc.get("paragraph_count") or 0)
            if pc:
                out["paragraph_count"] = pc
        except Exception:
            pass

        return out
    return None


def _ongoing_entities(arc: Dict[str, Any]) -> Dict[str, Any]:
    ongoing = _safe_dict(arc.get("ongoing_paragraph"))
    return {
        "arc_name": str(arc.get("name") or "").strip(),
        "start_time": str(ongoing.get("start_time") or "").strip(),
        "turn_count": len(_safe_list(ongoing.get("turns"))),
        "locations": _as_str_list(ongoing.get("locations")),
        "characters": _as_str_list(ongoing.get("characters")),
        "npcs": _as_str_list(ongoing.get("npcs")),
    }


def _previous_arc_entities_if_needed(arcs: List[Any]) -> Optional[Dict[str, Any]]:
    if not arcs or not isinstance(arcs[0], dict):
        return None

    current_arc: Dict[str, Any] = arcs[0]
    if _arc_total_turns(current_arc) >= 2:
        return None

    # Previous arc is typically arcs[1] (newest previous), if present.
    prev_arc = arcs[1] if len(arcs) > 1 else None
    if not isinstance(prev_arc, dict):
        return None

    # Prefer the last summarized paragraph participants for "previous arc lists".
    paras = prev_arc.get("paragraphs")
    if isinstance(paras, list) and paras and isinstance(paras[-1], dict):
        p = paras[-1]
        return {
            "arc_name": str(prev_arc.get("name") or "").strip(),
            "source": "previous_arc_last_paragraph",
            "locations": _as_str_list(p.get("locations")),
            "characters": _as_str_list(p.get("characters")),
            "npcs": _as_str_list(p.get("npcs")),
        }

    # Otherwise fall back to that arc's ongoing paragraph lists (if any).
    ongoing = _safe_dict(prev_arc.get("ongoing_paragraph"))
    return {
        "arc_name": str(prev_arc.get("name") or "").strip(),
        "source": "previous_arc_ongoing_paragraph",
        "locations": _as_str_list(ongoing.get("locations")),
        "characters": _as_str_list(ongoing.get("characters")),
        "npcs": _as_str_list(ongoing.get("npcs")),
    }


def _strip_scene_description_prefix(narration: str) -> str:
    """Remove a leading 'Scene description:...' block from a turn narration.

    The block ends at the first 'Actions:' marker (or at the end of the
    string if no Actions marker is found and the text is all scene desc).
    Returns the remainder (Actions + GM outcome), or the original text
    if no scene-description prefix is detected.
    """
    marker = "Scene description:"
    idx = narration.find(marker)
    if idx == -1:
        return narration
    actions_marker = "Actions:"
    actions_idx = narration.find(actions_marker, idx)
    if actions_idx != -1:
        return narration[actions_idx:].strip()
    # No Actions marker — the whole narration is scene description; keep as-is.
    return narration


def _last_n_turns(arcs: List[Any], n: int = 10) -> List[Dict[str, Any]]:
    """Return up to the last N turns across arcs.

    Turn sources (newest-first collection):
    - arc.ongoing_paragraph.turns
    - arc.paragraphs[*].turns (kept for offline tooling; used here as fallback)

    Output order: oldest -> newest within the returned window.
    """

    try:
        limit = int(n)
    except Exception:
        limit = 10
    limit = max(0, min(limit, 50))
    if limit <= 0:
        return []

    def _turn_view(t: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(t, dict):
            return None
        if str(t.get("kind") or "").strip() == "plot_seed":
            return None
        narration = str(t.get("narration") or "")
        # Strip the mechanically-prepended "Scene description:" block so that
        # the context window is not polluted with identical scene descriptions
        # every turn.  Keep only Actions + GM outcome narration.
        narration = _strip_scene_description_prefix(narration)
        out: Dict[str, Any] = {
            "start_time": str(t.get("start_time") or ""),
            "end_time": str(t.get("end_time") or ""),
            "location": str(t.get("location") or ""),
            "characters": _as_str_list(t.get("characters")),
            "npcs": _as_str_list(t.get("npcs")),
            "narration": narration,
        }
        kind = str(t.get("kind") or "").strip()
        if kind:
            out["kind"] = kind
        return out

    collected: List[Dict[str, Any]] = []
    for arc_index in range(0, len(arcs)):
        arc = arcs[arc_index]
        if not isinstance(arc, dict):
            continue

        # Ongoing turns (most recent activity).
        ongoing = _safe_dict(arc.get("ongoing_paragraph"))
        turns = _safe_list(ongoing.get("turns"))
        for t in reversed(turns):
            tv = _turn_view(t)
            if tv is not None:
                collected.append(tv)
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break

        # Fallback to paragraph turns if ongoing is empty or insufficient.
        paras = _safe_list(arc.get("paragraphs"))
        for p in reversed(paras):
            if not isinstance(p, dict):
                continue
            p_turns = _safe_list(p.get("turns"))
            for t in reversed(p_turns):
                tv = _turn_view(t)
                if tv is not None:
                    collected.append(tv)
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break

    return list(reversed(collected))


def build_game_master_qa_context(world: Any) -> str:
    """Minimal context for ANSWER_QUESTION calls.

    The GM's persistent history already holds arc summaries and paragraph
    metas, so only the current scene state (participants, location, world
    time) is injected here to avoid repeating kilobytes of text on every
    per-character question.
    """
    info = world.get_info()
    wt = world.get_world_time()
    scene = world.get_scene()
    scene_active = isinstance(scene, dict) and scene.get("state") == "active"

    # Minimal world snapshot: time + location only
    world_snapshot: Dict[str, Any] = {}
    try:
        world_snapshot["world_time"] = str(wt) if wt else None
    except Exception:
        pass
    try:
        world_snapshot["last_location"] = str(info.get("last_location") or info.get("last_loc") or "") or None
    except Exception:
        pass
    world_snapshot = {k: v for k, v in world_snapshot.items() if v}

    active_scene_section: Any = None
    scene_character_details_section: Any = None
    scene_npc_details_section: Any = None

    if scene_active:
        active_scene_section = _prepare_scene_for_qa_context(scene, wt)
        if isinstance(active_scene_section, dict):
            allowed_keys = {
                "name",
                "location",
                "title",
                "summary",
                "description",
                "start_time",
                "kind",
                "scene_type",
                "mode",
                "characters",
                "npcs",
                "behind_world_time_by",
            }
            extra_keys = set(active_scene_section.keys()) - allowed_keys
            assert not extra_keys, (
                "QA scene context contains unexpected fields: "
                + ", ".join(sorted(extra_keys))
            )

    parts: List[str] = []
    parts.append("# Current scene")
    _append_section(parts, "### World state", world_snapshot)
    if scene_active:
        _append_section(parts, "### Active scene", active_scene_section)
        _append_section(parts, "### Scene player descriptions", scene_character_details_section)
        _append_section(parts, "### Scene NPC details", scene_npc_details_section)

    return "\n\n".join(parts) + "\n"


def build_game_master_context_block(world: Any) -> str:
    """Build a narrative-friendly context block for the Game Master.

    Avoids tool/iteration mechanics while preserving world facts and story history.
    """

    info = world.get_info()
    if isinstance(info, dict):
        info_for_prompt = {k: v for k, v in info.items() if str(k) != "gm_bootstrap"}
    else:
        info_for_prompt = {}

    wt = world.get_world_time()

    # Enrich character entries with how far each is behind world time.
    try:
        _enrich_characters_with_time_behind(info_for_prompt, wt)
    except Exception:
        pass

    # Keep GM world-info compact: scene-pick task message carries dedicated
    # character timing appendix, so avoid duplicating helper timing fields here.
    for ch in (info_for_prompt.get("characters") or []):
        if isinstance(ch, dict):
            ch.pop("time_of_day", None)
            ch.pop("behind_world_time", None)

    # Inject NPC names from npc.json so the GM sees who exists.
    try:
        npcs_dict = world.get_npcs()
        if isinstance(npcs_dict, dict) and npcs_dict:
            info_for_prompt["npcs"] = sorted(npcs_dict.keys())
    except Exception:
        pass

    last_loc_name = str(info.get(world.K_LAST_LOCATION, "") or "").strip()

    location_section: Any = None
    if last_loc_name:
        try:
            loc = world.get_location(last_loc_name)
            location_section = loc
        except Exception:
            location_section = f"Last location '{last_loc_name}' is not found in locations.json."

    scene = world.get_scene()
    scene_active = isinstance(scene, dict) and scene.get("state") == "active"

    active_scene_section: Any = None
    scene_character_details_section: Any = None
    scene_npc_details_section: Any = None
    if scene_active:
        active_scene_section = _prepare_scene_for_context(scene, world.get_world_time())
    # Inject character and NPC descriptions for scene participants so the GM
    # knows what the characters look like, their traits, equipment, etc.
    if scene_active:
        try:
            chars = scene.get("characters") if isinstance(scene, dict) else None
            char_details: List[Dict[str, Any]] = []
            if isinstance(chars, dict):
                for name in chars.keys():
                    nm = str(name)
                    try:
                        char_details.append({"name": nm, "description": world.get_character_description(nm)})
                    except Exception:
                        char_details.append({"name": nm, "error": "not found"})
            if char_details:
                scene_character_details_section = char_details
        except Exception:
            pass

        try:
            npc_names = scene.get("npcs") if isinstance(scene, dict) else None
            npc_details: List[Dict[str, Any]] = []
            if isinstance(npc_names, list):
                for npc_name in npc_names:
                    nm = str(npc_name).strip()
                    if not nm:
                        continue
                    try:
                        npc_details.append(world.get_npc(nm))
                    except Exception:
                        npc_details.append({"name": nm, "error": "not found"})
            if npc_details:
                scene_npc_details_section = npc_details
        except Exception:
            pass

    arcs = []
    try:
        arcs = world.get_story()
    except Exception:
        arcs = []

    last_arc_summary_section: Any = None
    last_paragraph_summaries_section: Any = None
    ongoing_entities_section: Any = None
    prev_arc_entities_section: Any = None

    try:
        if isinstance(arcs, list) and arcs and isinstance(arcs[0], dict):
            arc_summary = _last_arc_summary(arcs)
            if arc_summary:
                last_arc_summary_section = arc_summary

            para_metas = _last_n_paragraph_metas(arcs, n=10)
            para_metas = _filter_paragraph_metas_for_context(para_metas, arc_summary)
            if para_metas:
                last_paragraph_summaries_section = para_metas

            ongoing_entities_section = _ongoing_entities(arcs[0])

            prev_entities = _previous_arc_entities_if_needed(arcs)
            if prev_entities:
                prev_arc_entities_section = prev_entities
    except Exception:
        pass

    early_story_character_descriptions: Any = None
    try:
        active_scene_names: List[str] = []
        if scene_active and isinstance(scene, dict):
            chars = scene.get("characters")
            if isinstance(chars, dict):
                active_scene_names = [str(x).strip() for x in chars.keys() if str(x).strip()]
        early_story_character_descriptions = _all_character_descriptions_for_context(
            world,
            exclude_names=active_scene_names,
        )
    except Exception:
        early_story_character_descriptions = None

    parts: List[str] = []
    parts.append("# World snapshot")
    _append_section(parts, "### World info", info_for_prompt)
    _append_section(parts, "### Last known location", location_section)
    _append_section(parts, "### All character descriptions", early_story_character_descriptions)

    if scene_active:
        _append_section(parts, "### Active scene", active_scene_section)
        _append_section(parts, "### Scene player descriptions", scene_character_details_section)
        _append_section(parts, "### Scene NPC details", scene_npc_details_section)

    _append_section(parts, "### Last arc summary", last_arc_summary_section)
    _append_section(parts, "### Recent paragraph summaries", last_paragraph_summaries_section)
    _append_section(parts, "### Ongoing paragraph entities", ongoing_entities_section)
    _append_section(parts, "### Previous arc entities", prev_arc_entities_section)

    return "\n\n".join(parts) + "\n"
