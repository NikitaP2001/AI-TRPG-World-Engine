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
    # After a trim, history is reduced to this fraction of max_history_tokens,
    # leaving room for growth before the next trim triggers cache invalidation.
    # Higher = more aggressive trimming (less room), lower = less frequent trims.
    trim_target_factor: float = 0.7

    @property
    def max_history_tokens(self) -> int:
        max_tokens = int(self.model_max_context_tokens * self.history_fraction)
        return max(512, max_tokens)

    @property
    def trim_target_tokens(self) -> int:
        """Soft target: after trim, history should be at most this many tokens."""
        target = int(self.max_history_tokens * self.trim_target_factor)
        return max(256, target)


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
    raw_trim = (os.getenv("LLM_WORLD_TRIM_TARGET_FACTOR") or "").strip()

    ctx = 65536
    frac = 1.0
    trim = 0.7

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

    try:
        if raw_trim:
            trim = float(raw_trim)
    except Exception:
        trim = 0.7

    # clamp
    if ctx < 2048:
        ctx = 2048
    if frac <= 0:
        frac = 1.0
    if frac > 1.0:
        frac = 1.0
    if trim <= 0:
        trim = 0.7
    if trim > 1.0:
        trim = 1.0

    return HistoryLimits(model_max_context_tokens=ctx, history_fraction=frac, trim_target_factor=trim)


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


def _get_trim_counter_path(history_path: Path) -> Path:
    """Sidecar file: a simple integer counter bumped on each trim."""
    return history_path.with_suffix(history_path.suffix + ".trim")


def get_trim_counter(history_path: Path) -> int:
    """Read the trim counter for a history file. 0 = never trimmed."""
    try:
        raw = _get_trim_counter_path(history_path).read_text(encoding="utf-8").strip()
        return max(0, int(raw))
    except Exception:
        return 0


def _increment_trim_counter(history_path: Path) -> None:
    """Bump trim counter by 1."""
    try:
        p = _get_trim_counter_path(history_path)
        cur = get_trim_counter(history_path)
        p.write_text(str(cur + 1), encoding="utf-8")
    except Exception:
        pass


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
    target = limits.trim_target_tokens

    # Keep newest content within budget, always retaining the newest token slice.
    # Trim to the soft target (not the hard max) so new messages can be added
    # without immediately triggering another trim + cache invalidation.
    kept: List[Dict[str, str]] = []
    running = 0
    for msg in reversed(messages):
        content = str(msg.get("content") or "")
        cost = approx_token_count(content)
        if running + cost <= target:
            kept.append(msg)
            running += cost
            continue

        # Message would exceed target. Skip it (and older messages) — done.
        break

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
    trimmed = trim_history(msgs, limits=lim)
    # Bump trim counter if messages were actually removed
    if len(trimmed) < len(msgs):
        _increment_trim_counter(path)
    _write_json(path, trimmed)
