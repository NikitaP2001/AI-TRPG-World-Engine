from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class HistoryLimits:
    # We cannot reliably discover per-model context window via OpenRouter without an API call.
    # So we make it configurable and keep a conservative default.
    model_max_context_tokens: int = 32768
    # Default: allow persistent chat history to use the full model context window.
    # The console/agents still apply additional trimming/headroom at call time.
    history_fraction: float = 1.0

    @property
    def max_history_tokens(self) -> int:
        max_tokens = int(self.model_max_context_tokens * self.history_fraction)
        return max(512, max_tokens)


def limits_from_env() -> HistoryLimits:
    # Keep environment handling consistent across entrypoints.
    # Other modules (e.g., OpenRouter client) load .env with override=True.
    # If we read limits before that happens, history/context sizes can appear to
    # "flip" between runs.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except Exception:
        pass

    raw_ctx = (os.getenv("LLM_WORLD_MODEL_CONTEXT_TOKENS") or "").strip()
    raw_frac = (os.getenv("LLM_WORLD_HISTORY_FRACTION") or "").strip()

    ctx = 65536
    frac = 1.0

    try:
        if raw_ctx:
            ctx = int(raw_ctx)
    except Exception:
        ctx = 32768

    try:
        if raw_frac:
            frac = float(raw_frac)
    except Exception:
        frac = 1.0

    # clamp
    if ctx < 2048:
        ctx = 2048
    if frac <= 0:
        frac = 1.0
    if frac > 1.0:
        frac = 1.0

    return HistoryLimits(model_max_context_tokens=ctx, history_fraction=frac)


def approx_token_count(text: str) -> int:
    # Very rough heuristic: for English-ish text, ~4 chars per token.
    # (Works better than word-count for mixed punctuation/JSON.)
    s = text or ""
    return max(1, (len(s) + 3) // 4)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_history(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    try:
        data = _read_json(path)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        if role not in {"user", "assistant"}:
            continue
        if not content.strip():
            continue
        out.append({"role": role, "content": content})
    return out


def trim_history(messages: List[Dict[str, str]], *, limits: HistoryLimits) -> List[Dict[str, str]]:
    max_tokens = limits.max_history_tokens

    # Keep newest content within budget, always retaining the newest token slice.
    kept: List[Dict[str, str]] = []
    running = 0
    for msg in reversed(messages):
        content = str(msg.get("content") or "")
        cost = approx_token_count(content)
        remaining = int(max_tokens) - int(running)
        if remaining <= 0:
            break
        if cost <= remaining:
            kept.append(msg)
            running += cost
            continue

        # Message does not fully fit. Skip it and continue with newer messages only.
        continue

    kept.reverse()
    return kept


def append_message(path: Path, *, role: str, content: str, limits: Optional[HistoryLimits] = None) -> None:
    role = (role or "").strip().lower()
    if role not in {"user", "assistant"}:
        return
    content = content or ""
    if not content.strip():
        return

    lim = limits or limits_from_env()
    msgs = load_history(path)
    msgs.append({"role": role, "content": content})
    msgs = trim_history(msgs, limits=lim)
    _write_json(path, msgs)
