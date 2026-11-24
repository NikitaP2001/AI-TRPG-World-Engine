from __future__ import annotations

import json
import os
import re
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


#: What a record carries beside its words: the kind it belongs to, the version
#: key of the fact it states, and the moment it was entered. Kept in the file so
#: that the code finds a record again by what it says it is, rather than by what
#: its text happens to look like; what the agent reads is the words alone.
ENVELOPE_KEYS = ("kind", "key", "at")


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
        record = {"role": role, "content": content}
        for field_name in ENVELOPE_KEYS:
            value = str(item.get(field_name) or "")
            if value:
                record[field_name] = value
        # Whatever the writer had to say about the record and could not put in
        # its words: a name the code needs afterwards and the agent does not.
        meta = item.get("meta")
        if isinstance(meta, dict) and meta:
            record["meta"] = dict(meta)
        out.append(record)
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


def append_message(
    path: Path,
    *,
    role: str,
    content: str,
    limits: Optional[HistoryLimits] = None,
) -> List[Dict[str, str]]:
    """Append one record, stamped with the world time, trimming if needed.

    The stamp is what lets a record say when it happened: a history without one
    cannot be summarised in terms of when — only in terms of what.

    Returns the records that no longer fit, oldest first — empty when nothing
    fell out. What fell out is still in the caller's own context, which is why
    the caller, and not this function, is the one that can write it down.

    The records are written to the file immediately either way: retention is the
    caller's business (see engine/history_retirement.py), not this function's.
    """
    role = (role or "").strip().lower()
    if role not in {"user", "assistant"}:
        return []
    content = content or ""
    if not content.strip():
        return []
    content = _stamped(content)

    lim = limits or limits_from_env()
    msgs = load_history(path)
    msgs.append({"role": role, "content": content})
    trimmed = trim_history(msgs, limits=lim)
    dropped: List[Dict[str, str]] = []
    # Bump trim counter if messages were actually removed
    if len(trimmed) < len(msgs):
        _increment_trim_counter(path)
        dropped = msgs[: len(msgs) - len(trimmed)]
    _write_json(path, trimmed)
    return dropped


def save_history(
    path: Path,
    messages: List[Dict[str, str]],
    *,
    limits: Optional[HistoryLimits] = None,
) -> List[Dict[str, str]]:
    """Write the records back, trimming if they no longer fit.

    For callers that remove records rather than append them — an outdated record
    is not something to keep, and rewriting the file is the only way to drop it.
    Returns what the trim removed, oldest first.
    """
    lim = limits or limits_from_env()
    msgs = list(messages or [])
    trimmed = trim_history(msgs, limits=lim)
    dropped: List[Dict[str, str]] = []
    if len(trimmed) < len(msgs):
        _increment_trim_counter(path)
        dropped = msgs[: len(msgs) - len(trimmed)]
    _write_json(path, trimmed)
    return dropped


#: A record's own world time, at its front.
_STAMP_RE = re.compile(r"^\[Y\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def stamp(content: str) -> str:
    """Prepend the world time, unless there is no clock or it is already there.

    Public because the pipeline that owns the write (history_pipeline) decides
    what a record looks like; this function only says what the stamp is.
    """
    try:
        from world.clock import now_string
        now = now_string()
    except Exception:
        now = ""
    if not now or _STAMP_RE.match(content):
        return content
    return f"[{now}] {content}"


#: Kept for readers of the older private name.
_stamped = stamp


# ---------------------------------------------------------------------------
# Retention: what a trim could not summarise
# ---------------------------------------------------------------------------


def _pending_retirement_path(history_path: Path) -> Path:
    return history_path.with_suffix(history_path.suffix + ".retired.json")


def save_pending_retirement(
    history_path: Path,
    *,
    records: List[Dict[str, str]],
    label: str = "",
) -> None:
    """Keep what fell out but could not be summarised yet.

    This is the failure path only: the summary is normally written from the
    caller's own context, where the records still are. When there is no such
    context left, the text has to be read back, and this file is where it is.
    """
    try:
        _write_json(
            _pending_retirement_path(history_path),
            {"label": label, "records": records},
        )
    except Exception:
        pass


def load_pending_retirement(history_path: Path) -> Optional[Dict[str, Any]]:
    path = _pending_retirement_path(history_path)
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return None
    return data


def clear_pending_retirement(history_path: Path) -> None:
    try:
        _pending_retirement_path(history_path).unlink(missing_ok=True)
    except Exception:
        pass
