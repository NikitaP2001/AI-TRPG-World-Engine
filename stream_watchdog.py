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


class StreamAbortError(Exception):
    """Raised when the stream watchdog requests abort."""
    pass


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


def log_pattern_detected(pattern: str, text_snippet: str) -> None:
    """Log when an invalid pattern is detected."""
    # Truncate text snippet for readability
    snippet = text_snippet[:200].replace("\n", "\\n") if text_snippet else ""
    if len(text_snippet) > 200:
        snippet += "..."
    _log_event(
        "PATTERN_DETECTED",
        f"pattern='{pattern}' in text: {snippet}",
        "Requesting abort via watchdog flag"
    )


def log_abort_triggered(pattern: str) -> None:
    """Log when abort is triggered in the callback."""
    _log_event(
        "ABORT_TRIGGERED",
        f"KeyboardInterrupt raised for pattern='{pattern}'",
        "Stream will stop, InvalidGMOutputError will be raised"
    )


def log_retry_with_correction(pattern: str) -> None:
    """Log when we retry with a correction message."""
    _log_event(
        "RETRY_INITIATED",
        f"Retrying after invalid pattern='{pattern}'",
        "Sending correction message to model"
    )


def log_watchdog_started() -> None:
    """Log when watchdog starts monitoring."""
    _log_event("WATCHDOG_STARTED", "Monitoring stream for invalid patterns", "")


def log_watchdog_stopped(detected: Optional[str] = None) -> None:
    """Log when watchdog stops."""
    if detected:
        _log_event("WATCHDOG_STOPPED", f"Stopped with detected pattern='{detected}'", "")
    else:
        _log_event("WATCHDOG_STOPPED", "Stopped normally (no pattern detected)", "")


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


def _request_watchdog_abort() -> None:
    """Request the stream to abort (called by watchdog thread)."""
    global _WATCHDOG_ABORT_REQUESTED
    with _WATCHDOG_ABORT_LOCK:
        _WATCHDOG_ABORT_REQUESTED = True


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


def _get_shared_accumulated_text() -> str:
    """Get shared accumulated text (called by watchdog)."""
    with _SHARED_ACCUMULATED_TEXT_LOCK:
        return _SHARED_ACCUMULATED_TEXT


def _set_shared_in_tool_call(in_tool: bool) -> None:
    """Set shared in-tool-call flag (called by callback)."""
    global _SHARED_IN_TOOL_CALL
    with _SHARED_IN_TOOL_CALL_LOCK:
        _SHARED_IN_TOOL_CALL = in_tool


def _get_shared_in_tool_call() -> bool:
    """Get shared in-tool-call flag (called by watchdog)."""
    with _SHARED_IN_TOOL_CALL_LOCK:
        return _SHARED_IN_TOOL_CALL


# ---------------------------------------------------------------------------
# StreamWatchdog class
# ---------------------------------------------------------------------------

class StreamWatchdog:
    """Watchdog that monitors streaming output and can abort on invalid patterns.
    
    Runs in a separate thread and checks the accumulated text periodically.
    When an invalid pattern is detected, it sets abort flags that the callback
    checks on each token, allowing us to stop the stream early.
    
    Uses module-level shared state to communicate with the LiveStreamCallback.
    
    Detection includes:
    - Explicit invalid patterns (```json, <function_calls>, etc.)
    - Markdown headings (# Title) indicating report-style output
    - Excessive word count (> MAX_WORDS) indicating rambling
    """
    
    # Patterns that indicate invalid GM output (should use tool calls instead)
    INVALID_PATTERNS = [
        "```json",
        "```python",
        "```",
        "<function_calls>",
        "<functioninvoke>",
        "<invoke name=",
        '"iteration"',
        '"remaining_to_answer"',
        '"acted"',
        '"_context_notice"',
    ]
    
    # Max words allowed before we consider it invalid rambling (disabled — 
    # the react loop in operator.py guards per-message verbosity instead)
    MAX_WORDS = None
    
    # Check interval in milliseconds
    CHECK_INTERVAL_MS = 50
    
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._detected_pattern: Optional[str] = None
    
    def start(self) -> None:
        """Start the watchdog thread."""
        _clear_watchdog_abort()
        clear_detected_invalid_pattern()
        _set_shared_accumulated_text("")  # Clear shared text from previous invocation
        _set_shared_in_tool_call(False)   # Clear tool call flag from previous invocation
        self._stop_event.clear()
        self._detected_pattern = None
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        log_watchdog_started()
    
    def stop(self) -> None:
        """Stop the watchdog thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        log_watchdog_stopped(self._detected_pattern)
    
    def get_detected_pattern(self) -> Optional[str]:
        """Return the pattern that triggered abort, if any."""
        return self._detected_pattern
    
    def _check_text(self, text: str) -> Optional[str]:
        """Check text for invalid patterns. Returns pattern name or None."""
        if not text:
            return None
        
        # Check explicit patterns
        for pattern in self.INVALID_PATTERNS:
            if pattern in text:
                return pattern
        
        # Check for non-English characters (Chinese, Japanese, Korean, etc.)
        # Model must output only in English
        if re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text):
            return "non-English output (Chinese/Japanese/Korean detected)"
        
        # Markdown headings are now allowed - GM can use them for structure
        # if re.search(r'(?:^|\n)#{1,6}\s+\S', text):
        #     return "markdown heading (# ...)"
        
        # Check word count - too much text means rambling (disabled)
        if self.MAX_WORDS is not None:
            word_count = len(text.split())
            if word_count > self.MAX_WORDS:
                return f"too many words ({word_count} > {self.MAX_WORDS})"
        
        return None
    
    def _watch_loop(self) -> None:
        """Main watchdog loop - runs in separate thread."""
        while not self._stop_event.is_set():
            try:
                # Get accumulated text from shared state
                text = _get_shared_accumulated_text()
                in_tool = _get_shared_in_tool_call()
                
                # Only check if we're not inside a tool call
                if not in_tool:
                    pattern = self._check_text(text)
                    if pattern:
                        self._detected_pattern = pattern
                        _set_detected_invalid_pattern(pattern)
                        _request_watchdog_abort()
                        
                        # Log the detection
                        log_pattern_detected(pattern, text)
                        return
                
                # Wait before next check
                self._stop_event.wait(self.CHECK_INTERVAL_MS / 1000.0)
            except Exception:
                # Watchdog must never crash the app
                pass
