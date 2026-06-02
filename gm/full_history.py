from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.messages import BaseMessage

from memory_store import HistoryLimits, approx_token_count, limits_from_env
from message_codec import dicts_to_messages, messages_to_dicts


def _extract_tool_name_and_args(tc: Dict[str, Any]) -> Dict[str, Any] | None:
    """Convert any supported tool-call schema to compact {'name','args'} form."""

    if not isinstance(tc, dict):
        return None

    # Legacy LangChain-style: {"name": ..., "args": {...}}
    name = tc.get("name")
    args = tc.get("args")
    if isinstance(name, str) and name.strip() and isinstance(args, dict):
        return {"name": name.strip(), "args": args}

    # OpenAI-style: {"type": "function", "function": {"name": ..., "arguments": "...json..."}}
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
    if isinstance(fn, dict):
        fn_name = fn.get("name")
        raw_args = fn.get("arguments")
        parsed_args: Dict[str, Any] = {}

        if isinstance(raw_args, dict):
            parsed_args = raw_args
        elif isinstance(raw_args, str):
            try:
                loaded = json.loads(raw_args)
                if isinstance(loaded, dict):
                    parsed_args = loaded
            except Exception:
                parsed_args = {}

        if isinstance(fn_name, str) and fn_name.strip():
            return {"name": fn_name.strip(), "args": parsed_args}

    return None


def _compact_storage_assistant_dicts(dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop SA persistence-only metadata (tool ids/types) while keeping tool name+args."""

    out: List[Dict[str, Any]] = []
    for d in dicts:
        if not isinstance(d, dict):
            continue
        dd = dict(d)
        t = str(dd.get("type") or "").strip().lower()

        if t in {"ai", "assistant"}:
            raw_calls = dd.get("tool_calls")
            if isinstance(raw_calls, list):
                compact_calls: List[Dict[str, Any]] = []
                for tc in raw_calls:
                    compact = _extract_tool_name_and_args(tc if isinstance(tc, dict) else {})
                    if compact is not None:
                        compact_calls.append(compact)
                if compact_calls:
                    dd["tool_calls"] = compact_calls
                else:
                    dd.pop("tool_calls", None)

        elif t == "tool":
            # Keep tool result + tool function name; drop call-linking metadata.
            dd.pop("tool_call_id", None)

        out.append(dd)

    return out


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write to avoid leaving a truncated/invalid JSON file if the process
    # is interrupted mid-write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def gm_max_turns_from_env() -> int:
    raw = (os.getenv("LLM_WORLD_GM_MAX_TURNS") or "").strip()
    if not raw:
        # Disabled by default. When set (>0), keeps only the last N turns
        # before token sliding-window trimming.
        return 0
    try:
        v = int(raw)
        if v <= 0:
            return 0
        return max(1, min(100, v))
    except Exception:
        return 0


def load_full_gm_messages(path: Path) -> List[BaseMessage]:
    if not path.exists():
        return []
    try:
        data = _read_json(path)
    except Exception:
        # If the main file is temporarily corrupted (e.g., interrupted write), try
        # the last-known-good backup created by the console app.
        try:
            prev = path.with_name(f"{path.stem}.prev.json")
            if prev.exists():
                data = _read_json(prev)
            else:
                return []
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    dicts: List[Dict[str, Any]] = [d for d in data if isinstance(d, dict)]
    return dicts_to_messages(dicts)


def _trim_by_turns(dicts: List[Dict[str, Any]], *, max_turns: int) -> List[Dict[str, Any]]:
    # A "turn" is counted by gm_output_turn tool-result messages.
    gm_output_indices: List[int] = []
    for i, d in enumerate(dicts):
        if str(d.get("type") or "").strip().lower() != "tool":
            continue
        if str(d.get("name") or "").strip() == "gm_output_turn":
            gm_output_indices.append(i)

    if len(gm_output_indices) <= max_turns:
        return dicts

    # Keep full turns: start after the gm_output_turn that precedes the earliest kept turn.
    # Example: to keep last 10 turns, cut after the (N-10-1)th gm_output_turn.
    # BUT: always keep the gm_output_turn message itself, as it contains character actions
    # needed by the GUI. So cut just before it, not after.
    cut_before_idx = gm_output_indices[len(gm_output_indices) - max_turns - 1]
    return dicts[cut_before_idx:]


def _trim_by_tokens(dicts: List[Dict[str, Any]], *, limits: HistoryLimits) -> List[Dict[str, Any]]:
    max_tokens = limits.max_history_tokens

    kept: List[Dict[str, Any]] = []
    running = 0
    for d in reversed(dicts):
        content = str(d.get("content") or "")
        cost = approx_token_count(content)
        remaining = int(max_tokens) - int(running)
        if remaining <= 0:
            break
        if cost <= remaining:
            kept.append(d)
            running += cost
            continue

        # Message does not fully fit. Skip it and continue with newer messages only.
        continue

    kept.reverse()
    return kept


def _compact_tool_payloads(dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact verbose tool payloads for persistence.

    Tool results often append a full "UPDATED CONTEXT" snapshot after the actual
    result payload. Keeping that snapshot in history quickly consumes the token
    budget and hides recent tool-call progression. For persisted SA history, keep
    only the tool result section before the marker.
    """

    marker = "\n\n--- UPDATED CONTEXT ---\n"
    out: List[Dict[str, Any]] = []
    for d in dicts:
        if str(d.get("type") or "").strip().lower() == "tool":
            content = str(d.get("content") or "")
            if marker in content:
                head = content.split(marker, 1)[0].rstrip()
                dd = dict(d)
                dd["content"] = head
                out.append(dd)
                continue
        out.append(d)
    return out


def trim_full_gm_messages(
    messages: List[BaseMessage],
    *,
    limits: HistoryLimits | None = None,
    max_turns: int | None = None,
) -> List[BaseMessage]:
    """Return a trimmed copy of GM messages.

    Trims by:
    - last N turns (counted by gm_output_turn tool results)
    - then by history token budget

    This keeps in-memory history consistent with what we persist on disk.
    """

    lim = limits or limits_from_env()
    mt = max_turns if isinstance(max_turns, int) else gm_max_turns_from_env()

    # Separate pinned world-snapshot anchor messages so they are never evicted
    # by token-budget trimming.  Anchors are HumanMessages whose content starts
    # with the "[world_snapshot:" tag written by world/delta.py.
    # Also pin per-entity description messages (character, location, NPC) that
    # were injected by _maybe_inject_gm_entity_description: these form the
    # stable world-knowledge prefix used for prefix caching.
    _PINNED_PREFIXES = (
        "[world_snapshot:",
        "[player_description:",
        "[character_description:",  # legacy tag kept for backwards compat with existing histories
        "[location_description:",
        "[npc_description:",
    )

    def _is_pinned(msg: BaseMessage) -> bool:
        content = str(getattr(msg, "content", "") or "")
        return any(content.startswith(p) for p in _PINNED_PREFIXES)

    pinned = [m for m in messages if _is_pinned(m)]
    rest = [m for m in messages if not _is_pinned(m)]

    dicts = messages_to_dicts(rest)
    dicts = _compact_tool_payloads(dicts)

    if int(mt) > 0:
        dicts = _trim_by_turns(dicts, max_turns=mt)
    dicts = _trim_by_tokens(dicts, limits=lim)
    return pinned + dicts_to_messages(dicts)


def save_full_gm_messages(path: Path, messages: List[BaseMessage]) -> None:
    # Callers are responsible for trimming according to their own limits/max_turns.
    # Avoid double-trimming here, which can lead to surprising history drops when
    # environment/config differs between call sites.
    dicts = messages_to_dicts(messages)

    # Keep Storage Assistant history compact and readable in JSON:
    # - AI tool calls: only function name + args
    # - Tool results: no tool_call_id metadata
    if str(path.name).startswith("storage_assistant_messages"):
        dicts = _compact_storage_assistant_dicts(dicts)

    _write_json(path, dicts)
