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
                        "players": _as_str_list(p.get("players")),
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
        for k in ("locations", "players", "npcs"):
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
        "players": _as_str_list(ongoing.get("players")),
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
            "players": _as_str_list(p.get("players")),
            "npcs": _as_str_list(p.get("npcs")),
        }

    # Otherwise fall back to that arc's ongoing paragraph lists (if any).
    ongoing = _safe_dict(prev_arc.get("ongoing_paragraph"))
    return {
        "arc_name": str(prev_arc.get("name") or "").strip(),
        "source": "previous_arc_ongoing_paragraph",
        "locations": _as_str_list(ongoing.get("locations")),
        "players": _as_str_list(ongoing.get("players")),
        "npcs": _as_str_list(ongoing.get("npcs")),
    }



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

    # Inject entity ids the players do not control, so the GM sees who exists.
    try:
        npcs_dict = world.non_player_entities()
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
            chars = scene.get("players") if isinstance(scene, dict) else None
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
                from world_manager.entity_core import get_entity
                for npc_name in npc_names:
                    nm = str(npc_name).strip()
                    if not nm:
                        continue
                    try:
                        entity = get_entity(nm)
                        npc_details.append(entity or {"id": nm, "error": "not found"})
                    except Exception:
                        npc_details.append({"id": nm, "error": "not found"})
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
            chars = scene.get("players")
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


# ---------------------------------------------------------------------------
# Unified character info injection (used by GM, SM, SA, character agent)
# ---------------------------------------------------------------------------

def is_character_active_in_scene(world: Any, name: str) -> bool:
    """Check if character is alive and present in the currently active scene."""
    try:
        scene = world.get_scene()
        if not (isinstance(scene, dict) and scene.get("state") == "active"):
            return False
        chars = scene.get("players")
        if not isinstance(chars, dict):
            return False
        return name in chars
    except Exception:
        return False


def build_character_info_block(char_dir: Path) -> Optional[str]:
    """Build a [character_info] block from character description.json.

    Pinned-cache only: narrative description (general + race + personality).
    Dynamic state/skills/equipment are NOT included — they belong in
    invocation-scoped context, not pinned cache.
    """
    desc_path = char_dir / "description.json"
    try:
        desc = json.loads(desc_path.read_text(encoding="utf-8"))
        if not isinstance(desc, dict):
            return None
        # Keep only narrative fields
        clean = {}
        if desc.get("general"):
            clean["general"] = desc["general"]
        if desc.get("race"):
            clean["race"] = desc["race"]
        if desc.get("personality"):
            clean["personality"] = desc["personality"]
        return "## Description\n" + json.dumps(clean, ensure_ascii=False, indent=2) if clean else None
    except Exception:
        return None


def build_character_info_block_from_entity(entity_id: str) -> Optional[str]:
    """Build a [character_info] block from entity_store and player files.

    Pinned-cache only: narrative description (facts + race + personality).
    Dynamic state/skills/equipment are NOT included — they change every
    turn and belong in invocation-scoped context, not pinned cache.
    """
    try:
        from world_manager.entity_core import _get_backend
        import json as _json
        from pathlib import Path as _P

        # facts from entity store
        backend = _get_backend()
        entity = backend.get(entity_id)
        if entity is None:
            return None

        desc: dict = {}
        facts = entity.get("facts", "")

        # Merge in race, personality from filesystem description.json
        player_dir = _P("game") / "players" / entity_id
        desc_file = player_dir / "description.json"
        if desc_file.exists():
            try:
                raw = _json.loads(desc_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if raw.get("race"):
                        desc["race"] = raw["race"]
                    if raw.get("personality"):
                        desc["personality"] = raw["personality"]
            except Exception:
                pass

        desc["general"] = facts

        return "## Description\n" + _json.dumps(desc, ensure_ascii=False, indent=2)
    except Exception:
        return None
