"""Incident recording — make silent failures visible without making them fatal.

Rule: silence is acceptable only where a failure cannot leave game state
inconsistent. Everything else must be recorded and surfaced.

Three mechanisms:

- ``record_incident`` — count and log a swallowed failure.
  Used where the failure is tolerable (best-effort reads, telemetry, optional
  maintenance), so the code stays quiet but we can answer "did this ever fire?".
- ``guarded_buffer`` — an atomic boundary around a pipeline phase. On failure
  the phase's partial state is rolled back before the exception propagates, so
  a failed phase cannot poison the next one.
- ``format_incident_report`` — one compact block per session, printed by the
  orchestrator, so failures stop drowning in mid-run stdout.

Everything here is best-effort and never raises.
"""

from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, MutableSequence, Optional

PHASE_PREFIX = "phase_failed:"

_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {}
_LOGGED: set = set()


def _key(category: str, exc: Optional[BaseException]) -> str:
    if exc is None:
        return str(category or "unknown")
    return f"{category or 'unknown'}:{type(exc).__name__}"


def record_incident(
    category: str,
    exc: Optional[BaseException] = None,
    **ctx: Any,
) -> None:
    """Count a swallowed failure; write one line to the log per unique key."""
    try:
        key = _key(category, exc)
        with _LOCK:
            _COUNTS[key] = _COUNTS.get(key, 0) + 1
            first_time = key not in _LOGGED
            if first_time:
                _LOGGED.add(key)

        if not first_time:
            return

        from openrouter_langchain_logging import _append_jsonl, logs_dir

        row: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": str(category or "unknown"),
            "exc_type": type(exc).__name__ if exc is not None else "",
            "exc": str(exc)[:500] if exc is not None else "",
        }
        if ctx:
            row["context"] = {str(k): str(v)[:200] for k, v in ctx.items()}
        _append_jsonl(logs_dir() / "swallowed.jsonl", row)
    except Exception:
        pass


@contextmanager
def guarded_buffer(
    buffer: MutableSequence[Any],
    phase: str,
    **ctx: Any,
) -> Iterator[None]:
    """Atomic phase boundary for a message buffer.

    If the phase body raises, everything it appended to ``buffer`` is removed
    before the exception propagates — a failed phase must not leave partial
    state behind for the next phase to interpret.
    """
    snapshot = len(buffer)
    try:
        yield
    except BaseException as exc:
        try:
            del buffer[snapshot:]
        except Exception:
            pass
        record_incident(f"{PHASE_PREFIX}{phase}", exc, **ctx)
        raise


def phase_guard(phase: str, buffer_attr: str = "_hot_buffer"):
    """Decorator form of :func:`guarded_buffer` for pipeline phase methods.

    Rolls the phase's buffer appends back on failure and records the incident
    before the exception propagates.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            buffer = getattr(self, buffer_attr, None)
            if buffer is None:
                return fn(self, *args, **kwargs)
            with guarded_buffer(buffer, phase):
                return fn(self, *args, **kwargs)
        return wrapper
    return decorator


def incident_counts() -> Dict[str, int]:
    with _LOCK:
        return dict(_COUNTS)


def format_incident_report() -> str:
    """Compact end-of-session block; empty string when nothing was recorded."""
    try:
        counts = incident_counts()
        if not counts:
            return ""

        phases: List[str] = []
        others: List[str] = []
        for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if key.startswith(PHASE_PREFIX):
                phases.append(f"{key[len(PHASE_PREFIX):]} ×{n}")
            else:
                others.append(f"{key} ×{n}")

        parts = ["── session diagnostics ──"]
        if phases:
            parts.append("phases failed: " + ", ".join(phases))
        if others:
            parts.append("swallowed: " + ", ".join(others))
        return "\n".join(parts)
    except Exception:
        return ""
