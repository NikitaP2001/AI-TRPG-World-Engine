"""Stream watchdog for detecting invalid LLM output patterns.

This module provides early detection of invalid output patterns in streaming LLM responses.
When the GM model outputs text instead of using tool calls (e.g., ```json blocks, markdown),
the watchdog detects this and triggers an abort so we can retry with corrective instructions.

All detection events are logged to logs/watchdog.log for debugging.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "t", "yes", "y", "on"}


def watchdog_log_path() -> Path:
    """Path to the watchdog log file."""
    raw = (os.getenv("LLM_WORLD_WATCHDOG_LOG_PATH") or "").strip()
    return Path(raw or "logs/watchdog.log")


def watchdog_logging_enabled() -> bool:
    """Whether watchdog logging is enabled."""
    return _env_flag("LLM_WORLD_WATCHDOG_LOG_ENABLED", True)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidGMOutputError(Exception):
    """Raised when streaming detects invalid GM output patterns (e.g., ```json blocks).
    
    This allows us to abort the LLM call early and retry with corrective instructions.
    """
    def __init__(self, pattern: str, accumulated_text: str):
        self.pattern = pattern
        self.accumulated_text = accumulated_text
        super().__init__(f"Invalid GM output detected: {pattern}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(event_type: str, details: str, action: str = "") -> None:
    """Log a watchdog event to the log file.
    
    Args:
        event_type: Type of event (e.g., "PATTERN_DETECTED", "ABORT_REQUESTED")
        details: Details about what was detected
        action: What action was taken in response
    """
    if not watchdog_logging_enabled():
        return
    
    try:
        log_path = watchdog_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        timestamp = _utc_now_iso()
        line = f"[{timestamp}] {event_type}: {details}"
        if action:
            line += f" | ACTION: {action}"
        line += "\n"
        
        with _LOG_LOCK:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        # Never crash the app for logging failures
        pass


def log_abort_triggered(pattern: str) -> None:
    """Log when abort is triggered in the callback."""
    _log_event(
        "ABORT_TRIGGERED",
        f"KeyboardInterrupt raised for pattern='{pattern}'",
        "Stream will stop, InvalidGMOutputError will be raised"
    )


# ---------------------------------------------------------------------------
# Module-level state for watchdog <-> callback communication
# ---------------------------------------------------------------------------

# Detected invalid pattern storage
_DETECTED_INVALID_PATTERN: Optional[str] = None
_DETECTED_PATTERN_LOCK = threading.Lock()

# Watchdog abort flag - when set, the callback will raise to stop the stream
_WATCHDOG_ABORT_REQUESTED: bool = False
_WATCHDOG_ABORT_LOCK = threading.Lock()

# Shared state for watchdog to read from callback
_SHARED_ACCUMULATED_TEXT: str = ""
_SHARED_ACCUMULATED_TEXT_LOCK = threading.Lock()
_SHARED_IN_TOOL_CALL: bool = False
_SHARED_IN_TOOL_CALL_LOCK = threading.Lock()


def _clear_watchdog_abort() -> None:
    """Clear the abort request (called before invoke)."""
    global _WATCHDOG_ABORT_REQUESTED
    with _WATCHDOG_ABORT_LOCK:
        _WATCHDOG_ABORT_REQUESTED = False


def _is_watchdog_abort_requested() -> bool:
    """Check if abort was requested."""
    with _WATCHDOG_ABORT_LOCK:
        return _WATCHDOG_ABORT_REQUESTED


def _set_detected_invalid_pattern(pattern: str) -> None:
    """Set the detected invalid pattern (called from streaming callback)."""
    global _DETECTED_INVALID_PATTERN
    with _DETECTED_PATTERN_LOCK:
        if _DETECTED_INVALID_PATTERN is None:  # Only keep the first detection
            _DETECTED_INVALID_PATTERN = pattern


def get_detected_invalid_pattern() -> Optional[str]:
    """Get and clear the detected invalid pattern (called after invoke)."""
    global _DETECTED_INVALID_PATTERN
    with _DETECTED_PATTERN_LOCK:
        pattern = _DETECTED_INVALID_PATTERN
        _DETECTED_INVALID_PATTERN = None
        return pattern


def clear_detected_invalid_pattern() -> None:
    """Clear any detected invalid pattern (called before invoke)."""
    global _DETECTED_INVALID_PATTERN
    with _DETECTED_PATTERN_LOCK:
        _DETECTED_INVALID_PATTERN = None


def _set_shared_accumulated_text(text: str) -> None:
    """Set shared accumulated text (called by callback)."""
    global _SHARED_ACCUMULATED_TEXT
    with _SHARED_ACCUMULATED_TEXT_LOCK:
        _SHARED_ACCUMULATED_TEXT = text


def _set_shared_in_tool_call(in_tool: bool) -> None:
    """Set shared in-tool-call flag (called by callback)."""
    global _SHARED_IN_TOOL_CALL
    with _SHARED_IN_TOOL_CALL_LOCK:
        _SHARED_IN_TOOL_CALL = in_tool
