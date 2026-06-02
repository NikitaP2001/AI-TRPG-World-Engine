"""Character self-reflection agent.

Every N turns (counted per-character via memory.json entries), the character
pauses to reflect on recent experiences and produces a structured
self-reflection update stored in ``reflection.json``.

After reflection, the character also writes a diary summary of recent turns
(stored in ``diary.json``), working like world paragraph summarisation but
from the character's personal perspective.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from memory_store import load_history
from openrouter_langchain_logging import logs_enabled, enable_direct_text_abort, disable_direct_text_abort
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world.io import _read_json as _locked_read_json, _write_json as _locked_write_json


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REFLECTION_INTERVAL = 5  # every N memory entries
RECENT_THOUGHTS_COUNT = 10  # last N thought entries to inject
MAX_RELATIONSHIPS = 12
MAX_GOALS = 8
MAX_BELIEFS = 20
MAX_EMOTIONAL_STATE_CHARS = 320

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return _locked_read_json(path)


def _write_json(path: Path, data: Any) -> None:
    _locked_write_json(path, data)


def _game_root() -> Path:
    return (Path(__file__).resolve().parent.parent / "game").resolve()


def _character_dir(name: str) -> Path:
    return _game_root() / "characters" / name


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _clean_text(value: Any, *, max_chars: int = 500) -> str:
    s = str(value or "").strip()
    return s


def _safe_name_for_logging(name: str) -> str:
    """Convert character name to ASCII-safe string for use in file names and logging labels.
    
    This prevents Unicode encoding errors when logging systems try to write to files
    or format strings with ASCII-only environments.
    """
    return name.encode("ascii", errors="replace").decode("ascii")


def _dedupe_strings(values: Any, *, max_items: int, max_chars: int = 280) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in values:
        s = _clean_text(item, max_chars=max_chars)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max(1, int(max_items)):
            break
    return out


def _normalize_relationships(values: Any, *, max_items: int) -> List[Dict[str, str]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        entity = _clean_text(item.get("entity"), max_chars=120)
        if not entity:
            continue
        key = entity.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "entity": entity,
                "nature": _clean_text(item.get("nature"), max_chars=120),
                "attitude": _clean_text(item.get("attitude"), max_chars=120),
                "notes": _clean_text(item.get("notes"), max_chars=0),
            }
        )
        if len(out) >= max(1, int(max_items)):
            break
    return out


def _normalize_goals(values: Any, *, max_items: int) -> List[Dict[str, str]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    valid_priority = {"immediate", "short-term", "long-term"}
    valid_status = {"active", "planned", "completed", "abandoned"}
    for item in values:
        if not isinstance(item, dict):
            continue
        goal = _clean_text(item.get("goal"), max_chars=220)
        if not goal:
            continue
        key = goal.lower()
        if key in seen:
            continue
        seen.add(key)
        pr = _clean_text(item.get("priority"), max_chars=32).lower() or "short-term"
        st = _clean_text(item.get("status"), max_chars=32).lower() or "active"
        if pr not in valid_priority:
            pr = "short-term"
        if st not in valid_status:
            st = "active"
        out.append({"goal": goal, "priority": pr, "status": st})
        if len(out) >= max(1, int(max_items)):
            break
    return out


def _apply_reflection_limits(
    *,
    character_name: str,
    new_result: Dict[str, Any],
    previous_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    goals = _normalize_goals(new_result.get("goals"), max_items=MAX_GOALS)
    beliefs = _dedupe_strings(new_result.get("beliefs"), max_items=MAX_BELIEFS, max_chars=220)
    emotional_state = _clean_text(new_result.get("emotional_state"), max_chars=MAX_EMOTIONAL_STATE_CHARS)

    # Upsert relationships into separate persistent storage (no total-count cap).
    new_rels = _normalize_relationships(new_result.get("relationships"), max_items=20)
    if new_rels:
        _upsert_relationships(character_name, new_rels)

    # reflection.json stores goals / beliefs / emotional_state only.
    return {
        "goals": goals,
        "beliefs": beliefs,
        "emotional_state": emotional_state,
    }


def _has_nonempty_reflection_content(value: Any) -> bool:
    """Return True when reflection has at least one meaningful field."""
    if not isinstance(value, dict):
        return False

    relationships = value.get("relationships")
    goals = value.get("goals")
    beliefs = value.get("beliefs")
    emotional_state = str(value.get("emotional_state") or "").strip()

    if isinstance(relationships, list) and any(isinstance(x, dict) for x in relationships):
        return True
    if isinstance(goals, list) and any(isinstance(x, dict) for x in goals):
        return True
    if isinstance(beliefs, list) and any(str(x or "").strip() for x in beliefs):
        return True
    if emotional_state:
        return True
    return False


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

@tool
def output_reflection(
    relationships: list,
    goals: list,
    beliefs: list,
    emotional_state: str,
) -> str:
    """Output your self-reflection.

    This is the ONLY tool available during the reflection phase.
    Call it exactly once with your updated self-reflection.

    Args:
        relationships: List of relationship objects, each with keys: entity, nature, attitude, notes
        goals: List of goal objects, each with keys: goal, priority (immediate/short-term/long-term), status (active/planned/completed/abandoned)
        beliefs: List of belief strings — factual beliefs about the world
        emotional_state: Brief description of current emotional state and trajectory
    """
    return json.dumps(
        {
            "relationships": relationships,
            "goals": goals,
            "beliefs": beliefs,
            "emotional_state": str(emotional_state or ""),
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Reflection state queries
# ---------------------------------------------------------------------------

def get_reflection_interval() -> int:
    raw = (os.getenv("LLM_WORLD_REFLECTION_INTERVAL") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except Exception:
            pass
    return DEFAULT_REFLECTION_INTERVAL


def count_memory_entries(character_name: str) -> int:
    """Return the number of turn entries in a character's memory.json."""
    p = _character_dir(character_name) / "memory.json"
    data = _read_json(p)
    if isinstance(data, list):
        return len(data)
    return 0


def load_reflection(character_name: str) -> Optional[Dict[str, Any]]:
    """Load the character's current reflection.json, or None if absent."""
    p = _character_dir(character_name) / "reflection.json"
    data = _read_json(p)
    if isinstance(data, dict):
        return data
    return None


def needs_reflection(character_name: str) -> bool:
    """Check whether this character is due for a reflection.

    Returns True when reflection OR diary has not been produced in the last
    `interval` memory entries. This makes retries self-healing: if either
    phase fails on one turn, the next turn will trigger it again.
    """
    interval = get_reflection_interval()
    entry_count = count_memory_entries(character_name)
    if entry_count < interval:
        return False

    reflection = load_reflection(character_name)
    diary = load_diary(character_name)

    last_reflection_entry_count = 0
    if reflection:
        last_reflection_entry_count = int(reflection.get("_meta", {}).get("entry_count", 0) or 0)

    last_diary_entry_count = int(diary.get("_meta", {}).get("entry_count", 0) or 0)

    reflection_is_stale = (entry_count - last_reflection_entry_count) >= interval
    diary_is_stale = (entry_count - last_diary_entry_count) >= interval
    return reflection_is_stale or diary_is_stale


# ---------------------------------------------------------------------------
# Gather recent thoughts
# ---------------------------------------------------------------------------

def _gather_recent_thoughts(character_name: str, n: int = RECENT_THOUGHTS_COUNT) -> List[str]:
    """Extract the last N thought strings from memory.json."""
    p = _character_dir(character_name) / "memory.json"
    data = _read_json(p)
    if not isinstance(data, list):
        return []

    thoughts: List[str] = []
    for entry in reversed(data):
        if not isinstance(entry, dict):
            continue
        # Collect from 'thoughts' list
        entry_thoughts = entry.get("thoughts")
        if isinstance(entry_thoughts, list):
            for t in reversed(entry_thoughts):
                s = str(t or "").strip()
                if s:
                    thoughts.append(s)
                    if len(thoughts) >= n:
                        break
        if len(thoughts) >= n:
            break

    thoughts.reverse()
    return thoughts[-n:]


# ---------------------------------------------------------------------------
# Relationships storage
# ---------------------------------------------------------------------------

def load_relationships(character_name: str) -> List[Dict[str, str]]:
    """Load all relationships from relationships.json.

    On first call (file missing), auto-migrates any relationships already
    stored inside reflection.json so existing games carry over seamlessly.
    """
    p = _character_dir(character_name) / "relationships.json"
    data = _read_json(p)
    if isinstance(data, dict):
        rels = data.get("relationships")
        if isinstance(rels, list):
            return [r for r in rels if isinstance(r, dict)]

    # Auto-migrate from reflection.json when relationships.json doesn't exist yet.
    if not p.exists():
        reflection = _read_json(_character_dir(character_name) / "reflection.json")
        if isinstance(reflection, dict):
            old_rels = reflection.get("relationships")
            if isinstance(old_rels, list) and old_rels:
                migrated = [r for r in old_rels if isinstance(r, dict)]
                if migrated:
                    _write_json(p, {
                        "relationships": migrated,
                        "_meta": {
                            "character": character_name,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "count": len(migrated),
                            "migrated_from": "reflection.json",
                        },
                    })
                    return migrated
    return []


def _get_known_entity_names() -> set:
    """Return lowercase names of all known characters and NPCs from world storage."""
    game_root = _game_root()
    known: set = set()
    try:
        info = _read_json(game_root / "world" / "info.json") or {}
        for entry in (info.get("characters") or []):
            if isinstance(entry, dict):
                n = str(entry.get("name") or "").strip()
                if n:
                    known.add(n.lower())
    except Exception:
        pass
    try:
        npc_data = _read_json(game_root / "world" / "npc.json") or {}
        if isinstance(npc_data, dict):
            for k in npc_data:
                if k:
                    known.add(str(k).lower())
    except Exception:
        pass
    return known


def _upsert_relationships(character_name: str, new_rels: List[Dict[str, str]]) -> None:
    """Upsert relationships into relationships.json (update matching entity, append new)."""
    rels_path = _character_dir(character_name) / "relationships.json"
    existing_data = _read_json(rels_path)
    if not isinstance(existing_data, dict):
        existing_data = {}

    rel_list: List[Dict[str, str]] = []
    raw = existing_data.get("relationships")
    if isinstance(raw, list):
        rel_list = [r for r in raw if isinstance(r, dict)]

    # Index by lowercase entity name for O(1) lookup.
    by_entity: Dict[str, int] = {
        str(r.get("entity") or "").strip().lower(): i
        for i, r in enumerate(rel_list)
        if str(r.get("entity") or "").strip()
    }

    now_ts = datetime.now(timezone.utc).isoformat()
    for r in new_rels:
        if not isinstance(r, dict):
            continue
        entity = str(r.get("entity") or "").strip()
        if not entity:
            continue
        key = entity.lower()
        entry: Dict[str, str] = {
            "entity": entity,
            "nature": str(r.get("nature") or "").strip(),
            "attitude": str(r.get("attitude") or "").strip(),
            "notes": str(r.get("notes") or "").strip(),
            "updated_at": now_ts,
        }
        if key in by_entity:
            existing = rel_list[by_entity[key]]
            entry["added_at"] = existing.get("added_at") or existing.get("updated_at") or now_ts
            rel_list[by_entity[key]] = entry
        else:
            entry["added_at"] = now_ts
            by_entity[key] = len(rel_list)
            rel_list.append(entry)

    _write_json(rels_path, {
        "relationships": rel_list,
        "_meta": {
            "character": character_name,
            "updated_at": now_ts,
            "count": len(rel_list),
        },
    })


def _get_relevant_relationships(character_name: str) -> List[Dict[str, str]]:
    """Return relationships for entities currently present at the character's recent locations.

    Algorithm:
    1. Collect ``scene_location`` values from the last 5 memory.json entries.
    2. From world/info.json (player characters) and world/npc.json (NPCs), find
       every entity whose current location matches any of those locations.
    3. Return the subset of relationships.json whose entity is in that set.
    """
    # Step 1: locations from last 5 memory entries.
    memory_path = _character_dir(character_name) / "memory.json"
    data = _read_json(memory_path)
    recent_locs: set = set()
    if isinstance(data, list):
        for entry in data[-5:]:
            if isinstance(entry, dict):
                meta = entry.get("meta") or {}
                loc = str(meta.get("scene_location") or "").strip()
                if loc:
                    recent_locs.add(loc)

    if not recent_locs:
        return []

    # Step 2: who is currently at those locations (characters + NPCs).
    game_root = _game_root()
    info = _read_json(game_root / "world" / "info.json") or {}
    npc_data = _read_json(game_root / "world" / "npc.json") or {}

    present_keys: set = set()
    for char_entry in (info.get("characters") or []):
        if not isinstance(char_entry, dict):
            continue
        name = str(char_entry.get("name") or "").strip()
        loc = str(char_entry.get("location") or "").strip()
        if name and loc in recent_locs:
            present_keys.add(name.lower())

    if isinstance(npc_data, dict):
        for npc_name, npc_info in npc_data.items():
            if not isinstance(npc_info, dict):
                continue
            loc = str(npc_info.get("location") or "").strip()
            if loc in recent_locs:
                present_keys.add(str(npc_name).lower())

    # Step 3: filter relationships to entities that are present.
    all_rels = load_relationships(character_name)
    return [
        r for r in all_rels
        if str(r.get("entity") or "").strip().lower() in present_keys
    ]


def _get_recently_met_names(character_name: str, exclude_name: str = "") -> List[str]:
    """Return canonical names of characters/NPCs co-present in the last 5 memory entry locations.

    'Recently met' is defined as any entity currently located at a scene location
    that the character visited in their last 5 turns.
    """
    memory_path = _character_dir(character_name) / "memory.json"
    data = _read_json(memory_path)
    recent_locs: set = set()
    if isinstance(data, list):
        for entry in data[-5:]:
            if isinstance(entry, dict):
                meta = entry.get("meta") or {}
                loc = str(meta.get("scene_location") or "").strip()
                if loc:
                    recent_locs.add(loc)

    if not recent_locs:
        return []

    game_root = _game_root()
    info = _read_json(game_root / "world" / "info.json") or {}
    npc_data = _read_json(game_root / "world" / "npc.json") or {}

    names: List[str] = []
    exclude_lower = str(exclude_name or "").strip().lower()

    for char_entry in (info.get("characters") or []):
        if not isinstance(char_entry, dict):
            continue
        name = str(char_entry.get("name") or "").strip()
        loc = str(char_entry.get("location") or "").strip()
        if name and loc in recent_locs and name.lower() != exclude_lower:
            names.append(name)

    if isinstance(npc_data, dict):
        for npc_name, npc_info in npc_data.items():
            if not isinstance(npc_info, dict):
                continue
            loc = str(npc_info.get("location") or "").strip()
            if loc in recent_locs and str(npc_name).lower() != exclude_lower:
                names.append(str(npc_name))

    return names


# ---------------------------------------------------------------------------
# Run reflection agent
# ---------------------------------------------------------------------------

def run_reflection(
    *,
    character_name: str,
    character_description: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Run the self-reflection phase for a character.

    Returns the reflection dict on success, or None on failure.
    """
    if logs_enabled():
        print(f"[trace] starting self-reflection for {character_name}")

    # 1. Load core character prompt (same prompt the character always uses)
    core_prompt = read_prompt_text("agents/character_agent/prompt.txt").replace(
        "{name}", str(character_name or "")
    )

    # 2. Load reflection task template
    reflection_task = read_prompt_text("agents/character_agent/reflection_task.txt")

    # 3. Gather recent thoughts
    recent_thoughts = _gather_recent_thoughts(character_name)
    thoughts_text = "\n".join(f"- {t}" for t in recent_thoughts) if recent_thoughts else "(no recent thoughts recorded)"

    # 4. Load current reflection (if any)
    current = load_reflection(character_name)
    if current:
        # Strip internal meta before showing to character
        display = {k: v for k, v in current.items() if not k.startswith("_")}
        current_text = json.dumps(display, ensure_ascii=False, indent=2)
    else:
        current_text = "(this is your first reflection — no prior self-reflection exists)"

    # 5. Build recently-met note from last 5 memory entries' co-present entities.
    try:
        recently_met_names = _get_recently_met_names(character_name, character_name)
    except Exception:
        recently_met_names = []

    # 5b. Fill template
    task_text = reflection_task.replace("{recent_thoughts}", thoughts_text).replace("{current_reflection}", current_text)

    if recently_met_names:
        recently_met_note = "Recently encountered characters/NPCs (last few turns): " + ", ".join(recently_met_names)
        task_text = recently_met_note + "\n\n" + task_text

    # 6. Build messages: core prompt as system, then character history for tone continuity,
    #    then the reflection task as a human message
    workspace_root = Path(__file__).resolve().parent.parent
    history_path = (workspace_root / "game" / "characters" / character_name / "messages.json").resolve()

    def _history_to_messages(history):
        from langchain_core.messages import AIMessage
        msgs = []
        for h in history:
            role = (h.get("role") or "").strip().lower()
            content = str(h.get("content") or "")
            if role == "user":
                msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                msgs.append(AIMessage(content=content))
        return msgs

    # Include some history for tone/personality continuity (last few exchanges)
    history = load_history(history_path) if history_path.exists() else []
    # Take only last 6 messages to keep context small
    history_tail = history[-6:] if len(history) > 6 else history

    # Build context blob with character identity
    context_info = json.dumps(
        {
            "character": character_name,
            "description": character_description,
        },
        ensure_ascii=False,
        indent=2,
    )

    messages = [
        SystemMessage(content=core_prompt),
        *_history_to_messages(history_tail),
        HumanMessage(content=f"Character context:\n{context_info}\n\n{task_text}"),
    ]

    # 7. Build LLM with only output_reflection tool
    safe_name = _safe_name_for_logging(character_name)
    llm = build_openrouter_chat_llm(
        temperature=0.7,
        streaming=True,
        max_tokens=1500,
        title_suffix=f"-reflection-{safe_name}",
    )

    callbacks = openrouter_logging_callbacks(scope="reflection", label=safe_name)

    try:
        bound_llm = llm.bind_tools(
            [output_reflection],
            tool_choice={"type": "function", "function": {"name": "output_reflection"}},
        ).with_config({"callbacks": callbacks})
    except TypeError:
        bound_llm = llm.bind_tools([output_reflection]).with_config({"callbacks": callbacks})

    # 8. Invoke with retries
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            enable_direct_text_abort(max_words=15)
            try:
                ai_msg = bound_llm.invoke(messages)
            except KeyboardInterrupt:
                if logs_enabled():
                    print(f"[trace] reflection {character_name}: direct text abort — retrying")
                # Don't append the failed output; just add a stronger correction
                messages.append(HumanMessage(
                    content="ERROR: You produced raw text instead of calling output_reflection. "
                    "Do NOT write text. Call the output_reflection tool NOW with your self-reflection."
                ))
                continue
            finally:
                disable_direct_text_abort()

            # Extract tool call
            tool_calls = getattr(ai_msg, "tool_calls", None)
            if not tool_calls:
                additional = getattr(ai_msg, "additional_kwargs", {}) or {}
                tool_calls = additional.get("tool_calls", [])

            if not tool_calls:
                if logs_enabled():
                    content_preview = str(getattr(ai_msg, "content", "") or "")
                    print(f"[trace] reflection attempt {attempt}: no tool call (content: {content_preview!r}), retrying")
                # Don't append failed ai_msg — it grows context and confuses the model
                messages.append(HumanMessage(
                    content="CRITICAL: You MUST call output_reflection tool NOW. "
                    "Do not write any text. Use the tool calling interface to call output_reflection "
                    "with: relationships, goals, beliefs, emotional_state."
                ))
                continue

            # Parse the first output_reflection call
            tc = tool_calls[0]
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if not isinstance(args, dict):
                args = {}

            # Invoke the tool to get structured output
            result_str = output_reflection.invoke(args, config={"callbacks": callbacks})
            result = json.loads(result_str)
            if not isinstance(result, dict):
                result = {}

            # Check content validity on the raw LLM result (before relationships are split off).
            if not _has_nonempty_reflection_content(result):
                if logs_enabled():
                    print(f"[trace] reflection attempt {attempt}: empty reflection content, retrying")
                messages.append(HumanMessage(
                    content=(
                        "Your reflection was empty. Call output_reflection again with meaningful content. "
                        "At minimum provide emotional_state and/or at least one item in relationships/goals/beliefs."
                    )
                ))
                continue

            # Filter out relationship entities not in the known characters/NPCs list.
            # We silently drop unknown entities rather than retrying, because the
            # known-entity list may not include all historical/background NPCs that
            # the character legitimately references in their reflection.
            rels_in_result = result.get("relationships") or []
            if isinstance(rels_in_result, list) and rels_in_result:
                known_entities = _get_known_entity_names()
                if known_entities:  # Only filter when world data is available
                    filtered_rels: List[Dict] = []
                    removed_names: List[str] = []
                    for rel in rels_in_result:
                        if not isinstance(rel, dict):
                            continue
                        entity = str(rel.get("entity") or "").strip()
                        if entity and entity.lower() not in known_entities:
                            removed_names.append(entity)
                        else:
                            filtered_rels.append(rel)
                    if removed_names:
                        if logs_enabled():
                            print(f"[trace] reflection: filtered unknown relationship entities for {character_name}: {removed_names}")
                        result["relationships"] = filtered_rels

            # Apply deterministic limits/deduping.
            # Relationships are upserted into relationships.json; reflection.json stores
            # goals / beliefs / emotional_state only.
            result = _apply_reflection_limits(
                character_name=character_name,
                new_result=result,
                previous_result=current,
            )

            # 9. Save reflection.json
            entry_count = count_memory_entries(character_name)
            result["_meta"] = {
                "character": character_name,
                "entry_count": entry_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            reflection_path = _character_dir(character_name) / "reflection.json"
            _write_json(reflection_path, result)

            if logs_enabled():
                n_rels = len(load_relationships(character_name))
                n_goals = len(result.get("goals") or [])
                n_beliefs = len(result.get("beliefs") or [])
                print(
                    f"[trace] reflection saved for {character_name}: "
                    f"relationships(total)={n_rels}, goals={n_goals}, "
                    f"beliefs={n_beliefs}"
                )

            # 10. Run diary summary phase (separate tool call)
            _run_diary_summary(character_name=character_name, messages_path=history_path)

            return result

        except Exception as e:
            if logs_enabled():
                safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
                print(f"[trace] reflection attempt {attempt} failed for {safe_name}: {e}")
            if attempt >= max_attempts:
                if logs_enabled():
                    safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
                    print(f"[trace] reflection failed for {safe_name} after {max_attempts} attempts")
                return None

    return None


# ---------------------------------------------------------------------------
# Character Diary
# ---------------------------------------------------------------------------
# diary.json schema:
# {
#   "paragraphs": [                      # last N diary paragraphs (oldest first)
#     {"summary": "..."},
#     ...
#   ],
#   "arc_summaries": [                   # completed arc summaries
#     {"summary": "..."},
#     ...
#   ]
# }
#
# Every reflection step, the character writes a new paragraph summarising
# the last batch of turns from their own perspective.  After DIARY_ARC_SIZE
# paragraphs, the oldest paragraphs are rolled into an arc summary (like
# the world story mechanism), and those paragraphs are removed.

DIARY_MAX_PARAGRAPHS = 10
DIARY_ARC_SIZE = 10  # roll into arc summary after this many paragraphs


def _diary_path(character_name: str) -> Path:
    return _character_dir(character_name) / "diary.json"


def load_diary(character_name: str) -> Dict[str, Any]:
    """Load diary.json for a character, returning a valid diary dict."""
    data = _read_json(_diary_path(character_name))
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("paragraphs"), list):
        data["paragraphs"] = []
    if not isinstance(data.get("arc_summaries"), list):
        data["arc_summaries"] = []
    if not isinstance(data.get("_meta"), dict):
        data["_meta"] = {}
    return data


def _save_diary(character_name: str, diary: Dict[str, Any]) -> None:
    _write_json(_diary_path(character_name), diary)


def _roll_diary_arc_if_needed(character_name: str, diary: Dict[str, Any]) -> None:
    """If paragraphs >= DIARY_ARC_SIZE, summarize them into an arc summary.

    Uses the same summarizer LLM to compress the paragraphs into a single
    arc summary, then clears the paragraphs list.
    """
    paras = diary.get("paragraphs") or []
    if len(paras) < DIARY_ARC_SIZE:
        return

    # Combine all paragraph summaries into one text to summarize
    combined = "\n\n".join(
        f"- {str(p.get('summary') or '').strip()}"
        for p in paras
        if isinstance(p, dict) and str(p.get("summary") or "").strip()
    )
    if not combined:
        diary["paragraphs"] = []
        return

    try:
        safe_name = _safe_name_for_logging(character_name)
        llm = build_openrouter_chat_llm(
            temperature=0.5,
            streaming=False,
            max_tokens=800,
            title_suffix=f"-diary-arc-{safe_name}",
        )
        callbacks = openrouter_logging_callbacks(scope="diary_arc", label=safe_name)

        messages = [
            SystemMessage(content=(
                "You are a personal journal assistant. "
                "Compress the following diary paragraphs into a single arc summary. "
                "Write from the character's first-person perspective. "
                "Keep the most important events, discoveries, relationships, and emotional milestones. "
                "Be concise but preserve key information. One to three paragraphs."
            )),
            HumanMessage(content=f"Diary paragraphs to compress:\n{combined}"),
        ]

        result = llm.invoke(messages, config={"callbacks": callbacks})
        arc_text = str(getattr(result, "content", "") or "").strip()
        if not arc_text:
            arc_text = combined
    except Exception as e:
        if logs_enabled():
            safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
            print(f"[trace] diary arc summarization failed for {safe_name}: {e}")
        arc_text = combined

    diary.setdefault("arc_summaries", []).append({"summary": arc_text})
    diary["paragraphs"] = []

    if logs_enabled():
        safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
        print(f"[trace] diary arc rolled for {safe_name} ({len(paras)} paragraphs → arc)")


@tool
def write_diary_entry(summary: str) -> str:
    """Write a diary entry summarizing your recent experiences.

    Write a personal summary of what happened to you recently — events,
    discoveries, conversations, feelings, lessons learned. This is YOUR
    private diary. Include whatever you consider most valuable to remember.

    Args:
        summary: Your diary entry text — a personal summary of recent events from your perspective.
    """
    return json.dumps({"summary": str(summary or "").strip()}, ensure_ascii=False)


def _run_diary_summary(*, character_name: str, messages_path: Path) -> None:
    """Run the diary summary phase: ask the character to write a diary entry.

    This is called AFTER the reflection phase completes.  It uses a separate
    tool (write_diary_entry) so the character doesn't confuse it with
    output_reflection.
    """
    if logs_enabled():
        print(f"[trace] starting diary summary for {character_name}")

    # Hard gate: never write diary without a non-empty reflection.
    current_reflection = load_reflection(character_name)
    if not _has_nonempty_reflection_content(current_reflection):
        if logs_enabled():
            print(f"[trace] diary skipped for {character_name}: missing or empty reflection")
        return

    # Load recent character messages for context
    history = load_history(messages_path) if messages_path.exists() else []
    # The character should summarize based on recent turns they experienced
    history_tail = history[-20:] if len(history) > 20 else history

    def _history_to_messages(hist):
        from langchain_core.messages import AIMessage
        msgs = []
        for h in hist:
            role = (h.get("role") or "").strip().lower()
            content = str(h.get("content") or "")
            if role == "user":
                msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                msgs.append(AIMessage(content=content))
        return msgs

    # Load existing diary for context
    diary = load_diary(character_name)
    existing_paras = diary.get("paragraphs") or []
    existing_arcs = diary.get("arc_summaries") or []

    diary_context_parts = []
    if existing_arcs:
        diary_context_parts.append("Previous arc summaries:")
        for i, arc in enumerate(existing_arcs, 1):
            diary_context_parts.append(f"  Arc {i}: {str(arc.get('summary') or '').strip()}")
    if existing_paras:
        diary_context_parts.append("Recent diary entries:")
        for p in existing_paras[-3:]:  # show last 3 for continuity
            diary_context_parts.append(f"  - {str(p.get('summary') or '').strip()}")
    diary_context = "\n".join(diary_context_parts) if diary_context_parts else "(this is your first diary entry)"

    task_text = (
        "## Diary Phase\n\n"
        "Now write a diary entry. Summarize what happened to you recently — "
        "the events, conversations, discoveries, feelings, and anything else "
        "you personally find valuable to remember. This is your private diary.\n\n"
        "Write from your own perspective. Focus on what matters to YOU — not "
        "everything that happened, but what you want to keep in memory.\n\n"
        f"Your diary so far:\n{diary_context}\n\n"
        "Call the `write_diary_entry` tool with your summary."
    )

    core_prompt = read_prompt_text("agents/character_agent/prompt.txt").replace(
        "{name}", str(character_name or "")
    )

    messages = [
        SystemMessage(content=core_prompt),
        *_history_to_messages(history_tail),
        HumanMessage(content=task_text),
    ]

    safe_name = _safe_name_for_logging(character_name)
    llm = build_openrouter_chat_llm(
        temperature=0.7,
        streaming=True,
        max_tokens=800,
        title_suffix=f"-diary-{safe_name}",
    )
    callbacks = openrouter_logging_callbacks(scope="diary", label=safe_name)

    try:
        bound_llm = llm.bind_tools(
            [write_diary_entry],
            tool_choice={"type": "function", "function": {"name": "write_diary_entry"}},
        ).with_config({"callbacks": callbacks})
    except TypeError:
        bound_llm = llm.bind_tools([write_diary_entry]).with_config({"callbacks": callbacks})

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            enable_direct_text_abort(max_words=15)
            try:
                ai_msg = bound_llm.invoke(messages)
            except KeyboardInterrupt:
                if logs_enabled():
                    print(f"[trace] diary {character_name}: direct text abort — retrying")
                messages.append(HumanMessage(
                    content="ERROR: You produced raw text instead of calling write_diary_entry. "
                    "Call the write_diary_entry tool NOW with your diary summary."
                ))
                continue
            finally:
                disable_direct_text_abort()

            tool_calls = getattr(ai_msg, "tool_calls", None)
            if not tool_calls:
                additional = getattr(ai_msg, "additional_kwargs", {}) or {}
                tool_calls = additional.get("tool_calls", [])

            if not tool_calls:
                if logs_enabled():
                    print(f"[trace] diary attempt {attempt}: no tool call, retrying")
                messages.append(HumanMessage(
                    content="CRITICAL: Call write_diary_entry tool NOW with your diary summary."
                ))
                continue

            tc = tool_calls[0]
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            if not isinstance(args, dict):
                args = {}

            result_str = write_diary_entry.invoke(args, config={"callbacks": callbacks})
            result = json.loads(result_str)
            summary_text = str(result.get("summary") or "").strip()

            if not summary_text:
                if logs_enabled():
                    print(f"[trace] diary attempt {attempt}: empty summary, retrying")
                messages.append(HumanMessage(content="Your diary entry was empty. Write something meaningful."))
                continue

            # Append new paragraph
            diary["paragraphs"].append({"summary": summary_text})

            # Roll arc if needed
            _roll_diary_arc_if_needed(character_name, diary)

            diary["_meta"] = {
                "character": character_name,
                "entry_count": count_memory_entries(character_name),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            # Save
            _save_diary(character_name, diary)

            if logs_enabled():
                n_paras = len(diary.get("paragraphs") or [])
                n_arcs = len(diary.get("arc_summaries") or [])
                print(f"[trace] diary saved for {character_name}: paragraphs={n_paras}, arcs={n_arcs}")

            return

        except Exception as e:
            if logs_enabled():
                safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
                print(f"[trace] diary attempt {attempt} failed for {safe_name}: {e}")
            if attempt >= max_attempts:
                if logs_enabled():
                    safe_name = character_name.encode('ascii', errors='replace').decode('ascii')
                    print(f"[trace] diary failed for {safe_name} after {max_attempts} attempts")
                return
