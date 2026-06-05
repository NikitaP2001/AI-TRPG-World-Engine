"""Scheduled relationship review: character assesses beings met in the last N turns.

Called by TickScheduler every 10 of the character's own turns (memory entries).
The character agent is invoked with a focused context containing the list of
entities encountered in the review window.  Output from ``relationship_update``
is captured and persisted to ``messages.json`` as a permanent record.

On each decision turn, a lightweight stale-check is also performed: if an entity
with a stored relationship appears in the current scene but hasn't been seen
for a while, a one-line reminder is injected into the context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .reflection import (
    relationship_update as _rel_up_tool,
    _get_known_relationships,
    _character_dir,
)
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from memory_store import append_message, load_history, limits_from_env
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI


REVIEW_INTERVAL = 10  # every N memory entries


# ---------------------------------------------------------------------------
# Encounter discovery
# ---------------------------------------------------------------------------

def build_encounter_list(
    character_name: str,
    *,
    world: Any = None,
    last_n_entries: int = 10,
) -> List[Dict[str, str]]:
    """Build a deduplicated list of beings the character met in the last N turns.

    Returns list of dicts::

        [{"name": "Момонга", "type": "player", "last_seen": "Y2126-09-23 19:48:30"},
         {"name": "Хоб",      "type": "npc",    "last_seen": "Y2126-09-23 20:02:30"}]
    """
    memory_path = _character_dir(character_name) / "memory.json"
    try:
        mem_data = json.loads(memory_path.read_text(encoding="utf-8"))
    except Exception:
        mem_data = []
    if not isinstance(mem_data, list):
        mem_data = []

    # Gather scene locations from the last N memory entries
    locations: List[str] = []
    for entry in mem_data[-last_n_entries:]:
        if not isinstance(entry, dict):
            continue
        loc = str(entry.get("scene_location") or "").strip()
        if loc and loc not in locations:
            locations.append(loc)

    # Find which player characters and NPCs were at those locations
    known: Dict[str, str] = {}  # name_lower -> "player" | "npc"

    if world is not None:
        # Player characters from info.json
        try:
            info = world.get_info()
            for ch in (info.get("characters") or []):
                if isinstance(ch, dict):
                    n = str(ch.get("name") or "").strip()
                    loc = str(ch.get("location") or "").strip()
                    if n and n.lower() != character_name.lower():
                        known[n.lower()] = "player"
        except Exception:
            pass

        # NPCs from npc.json
        try:
            npcs = world.get_npcs() or {}
            for n in npcs:
                if n:
                    known[n.lower()] = "npc"
        except Exception:
            pass

    # Filter to only those at recent locations or in story turns
    # Also include ANY entity already in relationships.json
    rels = _get_known_relationships(character_name)
    for r in rels:
        n = str(r.get("name") or "").strip()
        a = str(r.get("attitude") or "").strip()
        if n:
            known[n.lower()] = known.get(n.lower(), "npc")

    # Build final list with types
    encounter_list: List[Dict[str, str]] = []
    seen_names: set = set()
    for name_lower, etype in known.items():
        if name_lower in seen_names:
            continue
        seen_names.add(name_lower)
        encounter_list.append({
            "name": name_lower.title(),  # best-effort casing
            "type": etype,
        })

    return encounter_list


# ---------------------------------------------------------------------------
# Stale-relationship check (called on every decision turn)
# ---------------------------------------------------------------------------

def check_stale_relationships(
    character_name: str,
    *,
    current_scene_npcs: List[str] = None,
    current_scene_players: List[str] = None,
) -> Optional[str]:
    """If the character has stored relationships with beings NOW in scene,
    return a brief reminder string.  Otherwise returns None.

    This is called on every decision turn, before the agent runs, to re-inject
    a one-line summary so the agent doesn't need to look up old data.
    """
    rels = _get_known_relationships(character_name)
    if not rels:
        return None

    scene_names: set = set()
    for n in (current_scene_npcs or []):
        if n:
            scene_names.add(n.lower())
    for n in (current_scene_players or []):
        if n and n.lower() != character_name.lower():
            scene_names.add(n.lower())

    if not scene_names:
        return None

    relevant: List[str] = []
    for r in rels:
        rn = str(r.get("name") or "").strip().lower()
        ra = str(r.get("attitude") or "").strip()
        if rn in scene_names and ra:
            relevant.append(f"{r['name']} ({ra})")

    if not relevant:
        return None

    return "## Known Relationships in Scene\n" + "\n".join(f"- {x}" for x in relevant)


# ---------------------------------------------------------------------------
# Relationship review agent call
# ---------------------------------------------------------------------------

def run_relationship_review(
    character_name: str,
    *,
    world: Any = None,
    prompt_path: str = "agents/character_agent/prompt.txt",
) -> int:
    """Call the character agent to review relationships.

    The agent receives a focused context listing beings met in the last N turns
    and their current relationship data.  It must call ``relationship_update``
    for each entity.  Output is saved to ``messages.json``.

    Returns the number of relationships updated.
    """
    from .reflection import relationship_update as _rel_up

    prompt_text = read_prompt_text(prompt_path).replace("{name}", str(character_name or ""))

    encounter_list = build_encounter_list(character_name, world=world, last_n_entries=REVIEW_INTERVAL)
    if not encounter_list:
        return 0  # nothing to review

    rels = _get_known_relationships(character_name)
    rels_map: Dict[str, str] = {}
    for r in rels:
        nm = str(r.get("name") or "").strip().lower()
        at = str(r.get("attitude") or "").strip()
        if nm:
            rels_map[nm] = at

    # Build context
    lines: List[str] = ["## Relationship Review", "Review the beings you have encountered recently."]
    for entity in encounter_list:
        ename = entity["name"]
        existing = rels_map.get(ename.lower())
        if existing:
            lines.append(f"- {ename} ({entity['type']}) — your current impression: {existing}")
        else:
            lines.append(f"- {ename} ({entity['type']}) — new encounter, no impression yet")

    review_context = "\n".join(lines)

    # Build the agent call — only relationship_update tool available
    history_path = _character_dir(character_name) / "messages.json"
    limits = limits_from_env()

    llm = build_openrouter_chat_llm(
        temperature=0.7,
        streaming=True,
        max_tokens=1000,
        title_suffix=f"-char-rel-{character_name}",
        parallel_tool_calls=False,
    )
    callbacks = openrouter_logging_callbacks(scope="character", label=f"{character_name}_review")

    bound = llm.bind_tools(
        [_rel_up],
        tool_choice="required",
    ).with_config({"callbacks": callbacks})

    messages = [
        SystemMessage(content=(
            prompt_text + "\n\n"
            "Your task: review beings you have encountered. "
            "For each one, call relationship_update with your current impression."
        )),
        HumanMessage(content=review_context),
    ]

    updated = 0
    max_rounds = min(len(encounter_list) + 2, 8)

    for _round in range(max_rounds):
        try:
            out = bound.invoke(messages)
        except Exception:
            break

        tcs = getattr(out, "tool_calls", None) or []
        if not tcs:
            break

        for tc in tcs:
            name = getattr(tc, "name", "") or ""
            args = getattr(tc, "args", {}) or {}
            if name != "relationship_update":
                continue
            entity = str(args.get("entity") or "").strip()
            nature = str(args.get("nature") or "").strip()
            attitude = str(args.get("attitude") or "").strip()
            observation = str(args.get("observation") or "").strip()
            if not entity:
                continue

            # Execute the tool
            try:
                _rel_up.invoke(args)
            except Exception:
                continue

            # Save to persistent memory as an assistant message
            rel_entry = json.dumps({
                "entity": entity,
                "nature": nature,
                "attitude": attitude,
                "observation": observation,
            }, ensure_ascii=False)
            append_message(history_path, role="assistant",
                           content=f"[relationship_update]\n{rel_entry}", limits=limits)
            updated += 1

        # Append tool result and continue
        messages.append(out)
        for tc in tcs:
            tid = str(getattr(tc, "id", "") or "")
            tname = str(getattr(tc, "name", "") or "")
            messages.append(ToolMessage(content="ok", tool_call_id=tid, name=tname))

    return updated
