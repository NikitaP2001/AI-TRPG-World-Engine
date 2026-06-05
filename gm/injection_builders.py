"""Pure builder functions for injection content.

Each function takes ``world`` (and optional kwargs) and returns a plain string.
No side effects, no history checks — just data transformation.

These replace the inline logic that used to live in ``HistoryInjector.ensure_*``
methods.  They are intentionally independent of any injector so they can be
reused across profiles (GM, SA, character agents, future zone/world managers).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from world import World


# ======================================================================
# World meta
# ======================================================================

def build_world_meta(world: Any, **kwargs: Any) -> str:
    """Build [world_snapshot:world_meta] content: time, locations, NPCs, players."""
    try:
        info = world.get_info()
        if not isinstance(info, dict):
            return "World not yet initialized."
        meta: Dict[str, Any] = {}
        name = str(info.get("name") or "").strip()
        if name:
            meta["name"] = name
        try:
            wt = world.get_world_time()
            meta["time"] = wt.to_string() if hasattr(wt, "to_string") else str(wt)
        except Exception:
            pass
        try:
            locs = world.get_locations()
            if isinstance(locs, dict) and locs:
                meta["locations"] = sorted(locs.keys())
        except Exception:
            pass
        try:
            npcs_dict = world.get_npcs()
            if isinstance(npcs_dict, dict) and npcs_dict:
                meta["npcs"] = sorted(npcs_dict.keys())
        except Exception:
            pass
        try:
            chars = info.get("characters")
            if isinstance(chars, list) and chars:
                meta["players"] = [
                    {k: v for k, v in c.items() if k in ("name", "location", "last_acted")}
                    for c in chars
                    if isinstance(c, dict)
                ]
        except Exception:
            pass
        return json.dumps(meta, ensure_ascii=False, indent=2) if meta else "World not yet initialized."
    except Exception:
        return "World not yet initialized."


# ======================================================================
# Character descriptions
# ======================================================================

def build_character_description(world: Any, *, name: str, **kwargs: Any) -> str:
    """Build [player_description:<name>] content."""
    nm = str(name or "").strip()
    if not nm:
        return ""
    try:
        desc = world.get_character_description(nm)
        if isinstance(desc, dict):
            desc = {k: v for k, v in desc.items() if str(k) != "last_acted"}
        return json.dumps(desc, ensure_ascii=False, indent=2)
    except Exception:
        return ""


# ======================================================================
# Location descriptions
# ======================================================================

def build_location_description(world: Any, *, location: str, **kwargs: Any) -> str:
    """Build [location_description:<name>] content."""
    loc = str(location or "").strip()
    if not loc:
        return ""
    try:
        data = world.get_location(loc)
        return json.dumps(data, ensure_ascii=False, indent=2) if data else ""
    except Exception:
        return ""


# ======================================================================
# NPC descriptions
# ======================================================================

def build_npc_description(world: Any, *, name: str, **kwargs: Any) -> str:
    """Build [npc_description:<name>] content."""
    nm = str(name or "").strip()
    if not nm:
        return ""
    try:
        npc = world.get_npc(nm)
        return json.dumps(npc, ensure_ascii=False, indent=2) if npc else ""
    except Exception:
        return ""


# ======================================================================
# Story summaries: paragraph + arc
# ======================================================================

def _location_common_top_level(world: World, location_names: List[str]) -> List[str]:
    """Find the top-most ancestor for each location (squash nested)."""
    unique_parents: set = set()
    for x in location_names:
        arc_loc = str(x)
        visited = set()
        while True:
            try:
                loc_json = world.get_location(arc_loc)
            except Exception:
                break
            if not loc_json:
                break
            if arc_loc in visited:
                break
            visited.add(arc_loc)
            parent = loc_json.get("parent_location") if isinstance(loc_json, dict) else ""
            if parent:
                arc_loc = str(parent)
            else:
                break
        unique_parents.add(arc_loc)
    return sorted(str(p) for p in unique_parents)


def build_paragraph_summary(
    world: Any,
    *,
    name: str,
    summary: str,
    start_time: str = "",
    end_time: str = "",
    locations: List[str] = None,
    characters: List[str] = None,
    npcs: List[str] = None,
    **kwargs: Any,
) -> str:
    """Build [paragraph_summary:<name>] content."""
    nm = str(name or "").strip()
    sm = str(summary or "").strip()
    if not nm or not sm or nm == "Summary":
        return ""

    locs = [str(x).strip() for x in (locations or []) if str(x).strip()]
    chars = [str(x).strip() for x in (characters or []) if str(x).strip()]
    npc_list = [str(x).strip() for x in (npcs or []) if str(x).strip()]

    if isinstance(world, World) and locs:
        loc_common = _location_common_top_level(world, locs)
        loc_str = ", ".join(loc_common) if loc_common else "unknown"
    else:
        loc_str = ", ".join(locs) if locs else "unknown"

    char_str = ", ".join(chars) if chars else "unknown"
    npc_str = ", ".join(npc_list) if npc_list else "none"

    parts: List[str] = [f'Story paragraph completed: "{nm}"']
    if start_time or end_time:
        parts.append(f"Time period: {start_time or '?'} -> {end_time or '?'}")
    parts.append(f"Locations: {loc_str}")
    parts.append(f"Players: {char_str}")
    if npc_str != "none":
        parts.append(f"NPCs: {npc_str}")
    parts.append("")
    parts.append(sm)
    return "\n".join(parts)


def build_arc_summary(
    world: Any,
    *,
    arc_name: str,
    arc_summary: str,
    start_time: str = "",
    end_time: str = "",
    paragraph_names: List[str] = None,
    locations: List[str] = None,
    characters: List[str] = None,
    npcs: List[str] = None,
    **kwargs: Any,
) -> str:
    """Build [arc_finalized:<name>] content."""
    an = str(arc_name or "").strip()
    sm = str(arc_summary or "").strip()
    if not an or not sm:
        return ""

    parts: List[str] = [f'Arc finalized: "{an}"']
    if start_time or end_time:
        parts.append(f"Time period: {start_time or '?'} -> {end_time or '?'}")

    names = [str(x).strip() for x in (paragraph_names or []) if str(x).strip()]
    if names:
        parts.append(f"Paragraphs: {', '.join(repr(x) for x in names)}")

    locs = [str(x).strip() for x in (locations or []) if str(x).strip()]
    chars = [str(x).strip() for x in (characters or []) if str(x).strip()]
    npc_list = [str(x).strip() for x in (npcs or []) if str(x).strip()]
    if locs:
        parts.append(f"Arc locations: {', '.join(locs)}")
    if chars:
        parts.append(f"Arc players: {', '.join(chars)}")
    if npc_list:
        parts.append(f"Arc NPCs: {', '.join(npc_list)}")

    parts.append("")
    parts.append(sm)
    return "\n".join(parts)


def build_story_summaries(world: Any, **kwargs: Any) -> str:
    """Aggregate all paragraph/arc summaries into one block."""
    try:
        arcs = world.get_story()
    except Exception:
        arcs = []
    if not isinstance(arcs, list) or not arcs:
        return ""

    parts: List[str] = []
    for arc in arcs:
        if not isinstance(arc, dict):
            continue

        paragraphs = arc.get("paragraphs") if isinstance(arc.get("paragraphs"), list) else []
        for para in paragraphs:
            if not isinstance(para, dict):
                continue
            pname = str(para.get("name") or "").strip()
            psummary = str(para.get("summary") or "").strip()
            if not pname or not psummary or pname == "Summary":
                continue
            plocs = para.get("locations") if isinstance(para.get("locations"), list) else []
            pchars = para.get("characters") if isinstance(para.get("characters"), list) else []
            pnpcs = para.get("npcs") if isinstance(para.get("npcs"), list) else []
            pstart = str(para.get("start_time") or "").strip()
            pend = str(para.get("end_time") or "").strip()
            built = build_paragraph_summary(
                world, name=pname, summary=psummary,
                start_time=pstart, end_time=pend,
                locations=plocs, characters=pchars, npcs=pnpcs,
            )
            if built:
                parts.append(built)

        aname = str(arc.get("name") or "").strip()
        asummary = str(arc.get("summary") or "").strip()
        if aname and asummary:
            alocs = arc.get("locations") if isinstance(arc.get("locations"), list) else []
            achars = arc.get("characters") if isinstance(arc.get("characters"), list) else []
            anpcs = arc.get("npcs") if isinstance(arc.get("npcs"), list) else []
            astart = str(arc.get("start_time") or "").strip()
            aend = str(arc.get("end_time") or "").strip()
            ap_names: List[str] = []
            for p in paragraphs:
                if isinstance(p, dict):
                    pn = str(p.get("name") or "").strip()
                    if pn:
                        ap_names.append(pn)
            built = build_arc_summary(
                world, arc_name=aname, arc_summary=asummary,
                start_time=astart, end_time=aend,
                paragraph_names=ap_names,
                locations=alocs, characters=achars, npcs=anpcs,
            )
            if built:
                parts.append(built)

    return "\n\n---\n\n".join(parts) if parts else ""
