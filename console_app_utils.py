"""Pure utility functions extracted from console_app.py.

Standalone helpers with no dependency on GameOrchestrator or self.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage

from openrouter_langchain_logging import logs_enabled


# ---------------------------------------------------------------------------
# Turn recap persistence (for WebUI display of character thoughts/actions)
# ---------------------------------------------------------------------------
_TURN_RECAPS_FILENAME = "turn_recaps.jsonl"
_GUI_STREAM_INPUT_LOCK = threading.Lock()


def append_turn_recap(game_root: Path, result: Dict[str, Any]) -> None:
    """Append a turn recap dict as a single JSON line to turn_recaps.jsonl.

    The WebUI reads this file to display per-character thoughts and actions
    on each turn card.  We keep it separate from SA messages so we don't
    inject orphaned ToolMessages that would confuse the SA's ReAct loop.
    """
    try:
        recaps_path = game_root / _TURN_RECAPS_FILENAME
        with open(recaps_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backup slug
# ---------------------------------------------------------------------------
def backup_slug(name: str) -> str:
    """Normalize user-provided backup names to a safe folder name."""
    raw = (name or "").strip()
    if not raw:
        raise ValueError("Backup name is required")
    out = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    slug = "".join(out).strip("_")
    if not slug:
        raise ValueError("Backup name must contain letters/numbers")
    return slug[:64]


# ---------------------------------------------------------------------------
# Pseudo tool markup detection
# ---------------------------------------------------------------------------
def looks_like_pseudo_tool_markup(text: str) -> bool:
    """Check if text contains pseudo XML/JSON tool markup from model."""
    t = (text or "").lower()
    patterns = [
        "<function_calls",
        "</function_calls",
        "<invoke",
        "invoke name=",
        "<functioninvoke",
        "functioninvoke name=",
        "<parameter",
        "</parameter",
        "parameterinvfunction_calls",
        "invfunction_calls",
        "<tool_call",
        "</tool_call",
        "<function=",
        "</function>",
        "<parameter=",
        "give_word_to",
        "<invoke name=\"give_word",
        "<invoke name=\"start_scene",
        "<invoke name=\"run_scene",
        "<invoke name=\"gm_output",
        "<invoke name=\"create_",
        "<invoke name=\"update_",
    ]
    if any(p in t for p in patterns):
        return True
    if '"function"' in t and '"parameters"' in t:
        tool_pattern = (
            r'"function"\s*:\s*"('
            r'start_scene|run_scene|create_npc|update_character|create_location|update_location)'
        )
        if re.search(tool_pattern, t, re.IGNORECASE):
            return True
    output_indicators = [
        '"narration"', '"turn_duration"', '"location"',
        '"characters"', '"state"', '"npcs"', '"acted"', '"_context_notice"',
    ]
    count = sum(1 for ind in output_indicators if ind in t)
    if '```json' in t and count >= 2:
        return True
    if count >= 3:
        return True
    return False


def is_invalid_gm_text_output(text: str) -> bool:
    """Check if GM text output is invalid (pseudo tool markup or too verbose)."""
    if looks_like_pseudo_tool_markup(text):
        return True
    stripped = (text or "").strip()
    if stripped:
        word_count = len(stripped.split())
        if word_count > 50:
            return True
    return False


def is_tool_error_message(text: str) -> bool:
    """Check if text is a tool error message."""
    s = str(text or "").strip().lower()
    if not s:
        return False
    if s.startswith("error:"):
        return True
    return any(
        p in s
        for p in [
            "not available in the current context",
            "context changed during this invocation",
            "is not a valid tool",
            "turn already finalized",
            "please fix your mistakes",
            "not all characters ended",
            "is not valid right now",
        ]
    )


def is_transient_assistant_error_message(text: str) -> bool:
    """Check if text is a transient (non-fatal) error message."""
    s = str(text or "").strip().lower()
    if not s.startswith("error:"):
        return False
    return any(
        p in s
        for p in [
            "connection error",
            "connect",
            "timeout",
            "timed out",
            "refused",
            "unreachable",
            "temporarily unavailable",
            "upstream",
            "provider",
            "bad gateway",
        ]
    )


def strip_tool_error_pairs(messages: List[Any]) -> List[Any]:
    """Drop stale tool error messages and their corresponding AI tool calls."""
    dropped_tool_call_ids: set[str] = set()
    for m in messages:
        try:
            if getattr(m, "type", "") != "tool":
                continue
            if not is_tool_error_message(getattr(m, "content", "")):
                continue
            tc_id = str(getattr(m, "tool_call_id", "") or "").strip()
            if tc_id:
                dropped_tool_call_ids.add(tc_id)
        except Exception:
            continue

    if not dropped_tool_call_ids:
        return list(messages)

    cleaned: List[Any] = []
    for m in messages:
        try:
            t = getattr(m, "type", "")
            if t == "tool":
                tc_id = str(getattr(m, "tool_call_id", "") or "").strip()
                if tc_id in dropped_tool_call_ids:
                    continue
                cleaned.append(m)
                continue
            if t in {"ai", "assistant"}:
                content = str(getattr(m, "content", "") or "")
                tool_calls = getattr(m, "tool_calls", None)
                if not isinstance(tool_calls, list) or not tool_calls:
                    ak = getattr(m, "additional_kwargs", None) or {}
                    if isinstance(ak, dict):
                        tc2 = ak.get("tool_calls")
                        if isinstance(tc2, list):
                            tool_calls = tc2
                if not isinstance(tool_calls, list):
                    tool_calls = []
                if tool_calls:
                    filtered_calls = [
                        tc for tc in tool_calls
                        if str((tc or {}).get("id", "") or "").strip() not in dropped_tool_call_ids
                    ]
                    if filtered_calls != tool_calls:
                        if not filtered_calls and not content.strip():
                            continue
                        m = AIMessage(
                            content=content,
                            tool_calls=filtered_calls,
                            additional_kwargs=getattr(m, "additional_kwargs", {}) or {},
                        )
                cleaned.append(m)
                continue
            cleaned.append(m)
        except Exception:
            cleaned.append(m)
    return cleaned


def extract_pseudo_tool_markup_syntax(text: str) -> Tuple[str, Optional[str]]:
    """Extract pseudo tool markup snippet and the tool name if identifiable."""
    s = text or ""
    lower = s.lower()
    candidates = [
        lower.find("<function_calls"),
        lower.find("<functioninvoke"),
        lower.find("<invoke"),
    ]
    starts = [i for i in candidates if i >= 0]
    start = min(starts) if starts else -1
    if start < 0:
        return ("<function_calls> ... </function_calls>", None)
    end = -1
    end_fc = lower.find("</function_calls>", start)
    if end_fc >= 0:
        end = end_fc + len("</function_calls>")
    else:
        end_inv = lower.find("</invoke>", start)
        if end_inv >= 0:
            end = end_inv + len("</invoke>")
    if end < 0:
        end = min(len(s), start + 1600)
    snippet = s[start:end].strip()
    tool_name: Optional[str] = None
    m = re.search(
        r"(?:<invoke|<functioninvoke)\s+name=[\"']([^\"']+)[\"']",
        snippet,
        flags=re.IGNORECASE,
    )
    if m:
        tool_name = str(m.group(1) or "").strip() or None

    def _shorten(v: str) -> str:
        return (v or "").strip()

    snippet = re.sub(
        r"(<parameter[^>]*>)([\s\S]*?)(</parameter>)",
        lambda mm: mm.group(1) + _shorten(mm.group(2)) + mm.group(3),
        snippet,
        flags=re.IGNORECASE,
    )
    snippet = snippet.replace("<", "&lt;").replace(">", "&gt;")
    return (snippet, tool_name)


def log_pseudo_tool_markup_event(text: str) -> None:
    """Log a pseudo tool markup detection event to logs/malformed_tool_markup.jsonl."""
    if not logs_enabled():
        return
    try:
        p = Path("logs/malformed_tool_markup.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        include_snippet = (os.getenv("LLM_WORLD_LOG_PSEUDO_TOOL_SNIPPETS") or "").strip().lower() in {
            "1", "true", "yes", "y", "on",
        }
        _snippet, tool_name = extract_pseudo_tool_markup_syntax(text or "")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "pseudo_tool_markup_detected",
            **({"tool": tool_name} if tool_name else {}),
            **({"snippet": (text or "")} if include_snippet else {}),
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
    except Exception:
        pass
