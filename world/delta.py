"""Bootstrap message builders for SA and GM histories.

World-snapshot anchor messages are prepended once to the conversation history so
the LLM has stable initial world context.  They are tagged with
``[world_snapshot:TYPE]`` so they can be identified and protected from
token-budget trimming in gm/full_history.py.

SA anchor types (3 messages):
  [world_snapshot:locations]   – location directory
  [world_snapshot:characters]  – character summaries
  [world_snapshot:npcs]        – NPC directory

GM anchor types (atomic messages, replacing the old monolithic [world_snapshot:world]):
  [world_snapshot:world_meta]  – world name, time, location names, NPC names (stable header)
  Per-character descriptions use the existing [character_description:NAME] marker
  (injected via _maybe_inject_gm_entity_description in console_app.py).

Legacy GM anchor (single message, kept for backwards compatibility with existing histories):
  [world_snapshot:world]       – old monolithic narrative world snapshot
"""
from __future__ import annotations

import json
from typing import Any, List, Set

from langchain_core.messages import HumanMessage

# Anchor types maintained for the Storage Assistant.
SA_ANCHOR_TYPES: tuple = ("locations", "characters", "npcs")

# Tag prefix shared by all anchor messages.
_WORLD_SNAPSHOT_PREFIX = "[world_snapshot:"


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------

def _anchor_tag(anchor_type: str) -> str:
    return f"[world_snapshot:{anchor_type}]"


def is_sa_bootstrap_anchor(content: str) -> bool:
    """Return True if a message's content starts with a world_snapshot tag."""
    return str(content or "").startswith(_WORLD_SNAPSHOT_PREFIX)


def find_present_sa_anchor_types(messages: List[Any]) -> Set[str]:
    """Return the set of anchor types present in the message list."""
    found: Set[str] = set()
    for m in messages:
        content = str(getattr(m, "content", "") or "")
        if content.startswith(_WORLD_SNAPSHOT_PREFIX):
            rest = content[len(_WORLD_SNAPSHOT_PREFIX):]
            atype = rest.split("]")[0].strip().lower()
            if atype:
                found.add(atype)
    return found


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def _build_locations_content(world: Any) -> str:
    try:
        locs = world.get_locations()
        if not locs:
            return "No locations yet."
        lines = []
        if isinstance(locs, dict):
            for name, data in locs.items():
                summary = ""
                parent = ""
                if isinstance(data, dict):
                    summary = str(data.get("summary") or "").strip()
                    parent = str(data.get("parent_location") or "").strip()
                line = f"- {name}"
                if parent:
                    line += f" (in {parent})"
                if summary:
                    line += f": {summary}"
                lines.append(line)
        return "\n".join(lines) if lines else "No locations yet."
    except Exception:
        return "No locations yet."


def _build_characters_content(world: Any) -> str:
    try:
        names = world.list_character_names()
        if not names:
            return "No characters yet."
        lines = []
        for name in names:
            nm = str(name).strip()
            if not nm:
                continue
            try:
                desc = world.get_character_description(nm)
                if isinstance(desc, dict):
                    # Strip dynamic-only fields from bootstrap snapshot.
                    desc = {k: v for k, v in desc.items() if k not in ("last_acted",)}
                lines.append(f"- {nm}: {json.dumps(desc, ensure_ascii=False)}")
            except Exception:
                lines.append(f"- {nm}")
        return "\n".join(lines) if lines else "No characters yet."
    except Exception:
        return "No characters yet."


def _build_npcs_content(world: Any) -> str:
    try:
        npcs = world.get_npcs()
        if not npcs:
            return "No NPCs yet."
        lines = []
        if isinstance(npcs, dict):
            for name, data in npcs.items():
                summary = ""
                if isinstance(data, dict):
                    summary = str(data.get("description") or data.get("summary") or "").strip()
                line = f"- {name}"
                if summary:
                    line += f": {summary}"
                lines.append(line)
        return "\n".join(lines) if lines else "No NPCs yet."
    except Exception:
        return "No NPCs yet."


def _build_anchor_content(world: Any, anchor_type: str) -> str:
    if anchor_type == "locations":
        return _build_locations_content(world)
    if anchor_type == "characters":
        return _build_characters_content(world)
    if anchor_type == "npcs":
        return _build_npcs_content(world)
    return ""


# ---------------------------------------------------------------------------
# Bootstrap message builders
# ---------------------------------------------------------------------------

def build_sa_bootstrap_messages(world: Any) -> List[HumanMessage]:
    """Build 3 bootstrap HumanMessages for SA history: locations, characters, npcs."""
    msgs = []
    for atype in SA_ANCHOR_TYPES:
        tag = _anchor_tag(atype)
        content = _build_anchor_content(world, atype)
        msgs.append(HumanMessage(content=f"{tag}\n{content}"))
    return msgs


def build_gm_world_meta_content(world: Any) -> str:
    """Build compact world-level metadata for [world_snapshot:world_meta].

    Contains only the stable header: world name, time, location names, NPC names.
    Character details are stored in separate per-character messages.
    """
    try:
        info = world.get_info()
        if not isinstance(info, dict):
            return "World not yet initialized."
        meta: dict = {}
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
        # Include compact player timing info (name + location, not full descriptions).
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


def build_gm_bootstrap_message(context_text: str) -> HumanMessage:
    """Build a single bootstrap HumanMessage for GM history from the current context_text.

    DEPRECATED: use atomic GM anchor injection in console_app._ensure_gm_bootstrap()
    instead.  Kept for backwards compatibility.
    """
    body = context_text.strip() if context_text and context_text.strip() else "World not yet initialized."
    return HumanMessage(content=f"[world_snapshot:world]\n{body}")
