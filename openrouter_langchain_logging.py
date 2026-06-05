from __future__ import annotations

import json
import os
import ast
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from memory_store import approx_token_count, limits_from_env

# Import watchdog-related items from the dedicated module
from stream_watchdog import (
    InvalidGMOutputError,
    StreamAbortError,
    StreamWatchdog,
    _clear_watchdog_abort,
    _is_watchdog_abort_requested,
    _set_detected_invalid_pattern,
    get_detected_invalid_pattern,
    clear_detected_invalid_pattern,
    _set_shared_accumulated_text,
    _get_shared_accumulated_text,
    _set_shared_in_tool_call,
    _get_shared_in_tool_call,
    log_abort_triggered,
)


class ContextChangedInCallbackError(Exception):
    """Raised in on_tool_end when a tool changed the context.
    
    This stops the graph BEFORE the LLM can call another tool with stale bindings.
    LangGraph catches exceptions raised inside tools, but not from callbacks.
    """
    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Context changed after tool '{tool_name}'; stopping graph")


# ---------------------------------------------------------------------------
# Direct text abort: mid-stream abort for agents that must use tool calls
# ---------------------------------------------------------------------------
# When active, LiveStreamCallback will raise KeyboardInterrupt if non-tool
# text exceeds the word limit, stopping wasteful token generation early.

_DIRECT_TEXT_ABORT_DEFAULT_MAX_WORDS: int = 15
_DIRECT_TEXT_ABORT_STATE = threading.local()


def _get_abort_state() -> tuple[int, int]:
    depth = int(getattr(_DIRECT_TEXT_ABORT_STATE, "depth", 0) or 0)
    max_words = int(
        getattr(_DIRECT_TEXT_ABORT_STATE, "max_words", _DIRECT_TEXT_ABORT_DEFAULT_MAX_WORDS)
        or _DIRECT_TEXT_ABORT_DEFAULT_MAX_WORDS
    )
    return depth, max_words


def enable_direct_text_abort(max_words: int = 15) -> None:
    """Enable mid-stream abort when non-tool text exceeds *max_words*."""
    depth, _ = _get_abort_state()
    _DIRECT_TEXT_ABORT_STATE.depth = depth + 1
    _DIRECT_TEXT_ABORT_STATE.max_words = int(max_words)


def disable_direct_text_abort() -> None:
    """Disable mid-stream direct text abort."""
    depth, _ = _get_abort_state()
    if depth <= 1:
        _DIRECT_TEXT_ABORT_STATE.depth = 0
    else:
        _DIRECT_TEXT_ABORT_STATE.depth = depth - 1


def _is_direct_text_abort_active() -> bool:
    depth, _ = _get_abort_state()
    return depth > 0


def _get_direct_text_abort_max_words() -> int:
    _, max_words = _get_abort_state()
    return max_words


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "t", "yes", "y", "on"}


def _tool_schema_logging_enabled() -> bool:
    return _env_flag("LLM_WORLD_LOG_TOOL_SCHEMAS", False)


def _api_request_logging_enabled() -> bool:
    return _env_flag("LLM_WORLD_LOG_API_REQUESTS", False)


def logs_enabled() -> bool:
    return _env_flag("LLM_WORLD_LOGS_ENABLED", True)


def logs_dir() -> Path:
    raw = (os.getenv("LLM_WORLD_LOGS_DIR") or "").strip()
    return Path(raw or "logs")


def stream_path() -> Path:
    raw = (os.getenv("LLM_WORLD_STREAM_PATH") or "").strip()
    if raw:
        return Path(raw)
    return logs_dir() / "stream.txt"


def stream_echo_enabled() -> bool:
    return _env_flag("LLM_WORLD_STREAM_ECHO", False)


_STREAM_LOCK = threading.Lock()


def _safe_label(label: Optional[str]) -> str:
    raw = (label or "").strip()
    if not raw:
        return "unknown"
    out: List[str] = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    s = "".join(out).strip("_")
    return s if s else "unknown"


def _last_prompt_path(*, scope: str, label: Optional[str]) -> Path:
    safe_scope = _safe_label(scope)
    safe_label = _safe_label(label)
    return logs_dir() / "last_prompts" / f"{safe_scope}__{safe_label}.txt"


def _last_request_path(*, scope: str, label: Optional[str]) -> Path:
    safe_scope = _safe_label(scope)
    safe_label = _safe_label(label)
    return logs_dir() / "last_requests" / f"{safe_scope}__{safe_label}.json"


def _format_messages_for_prompt(messages: List[List[BaseMessage]], *, scope: str = "", label: str = "") -> str:
    lines: List[str] = []
    # Add timestamp header for debugging context changes
    header = f"# PROMPT CAPTURED: {_utc_now_iso()}"
    if scope or label:
        header += f" | scope={scope} label={label}"
    lines.append(header)
    lines.append("")
    
    for thread in messages or []:
        for m in thread or []:
            role = getattr(m, "type", m.__class__.__name__).lower()
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"

            content = str(getattr(m, "content", "") or "")
            lines.append(f"[{role}]")
            lines.append(content)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _save_last_prompt(*, scope: str, label: Optional[str], text: str) -> None:
    try:
        p = _last_prompt_path(scope=scope, label=label)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(p)
    except Exception:
        # Never block the app for debug capture.
        pass


def _save_last_request(*, scope: str, label: Optional[str], data: Dict[str, Any]) -> None:
    try:
        p = _last_request_path(scope=scope, label=label)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        tmp.replace(p)
    except Exception:
        # Never block the app for debug capture.
        pass


def _to_jsonable_for_log(value: Any, *, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return value

    if isinstance(value, BaseMessage):
        return {
            "type": getattr(value, "type", value.__class__.__name__),
            "content": _to_jsonable_for_log(getattr(value, "content", "") or "", max_chars=max_chars),
            "name": _to_jsonable_for_log(getattr(value, "name", None), max_chars=max_chars),
            "id": _to_jsonable_for_log(getattr(value, "id", None), max_chars=max_chars),
            "additional_kwargs": _to_jsonable_for_log(getattr(value, "additional_kwargs", {}), max_chars=max_chars),
            "tool_calls": _to_jsonable_for_log(getattr(value, "tool_calls", None), max_chars=max_chars),
            "invalid_tool_calls": _to_jsonable_for_log(getattr(value, "invalid_tool_calls", None), max_chars=max_chars),
        }

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = _to_jsonable_for_log(v, max_chars=max_chars)
        return out

    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable_for_log(v, max_chars=max_chars) for v in value]

    if hasattr(value, "dict"):
        try:
            return _to_jsonable_for_log(value.dict(), max_chars=max_chars)
        except Exception:
            pass

    if hasattr(value, "model_dump"):
        try:
            return _to_jsonable_for_log(value.model_dump(), max_chars=max_chars)
        except Exception:
            pass

    return _to_jsonable_for_log(str(value), max_chars=max_chars)


def _capture_api_request(
    *,
    scope: str,
    label: Optional[str],
    run_id: str,
    event: str,
    serialized: Any,
    kwargs: Dict[str, Any],
    payload: Dict[str, Any],
) -> None:
    if not _api_request_logging_enabled():
        return

    try:
        max_chars = int(os.getenv("LLM_WORLD_LOG_API_REQUEST_MAX_CHARS", "0") or "0")
    except Exception:
        max_chars = 0

    req = {
        "ts": _utc_now_iso(),
        "scope": scope,
        "label": label,
        "event": event,
        "run_id": str(run_id),
        "serialized": _to_jsonable_for_log(serialized, max_chars=max_chars),
        "invocation_params": _to_jsonable_for_log(kwargs.get("invocation_params", {}), max_chars=max_chars),
        "tool_payload": _to_jsonable_for_log(_extract_tool_payload_from_kwargs(kwargs), max_chars=max_chars),
        "request_payload": _to_jsonable_for_log(payload, max_chars=max_chars),
    }

    try:
        _append_jsonl(logs_dir() / "api_requests.jsonl", req)
    except Exception:
        pass

    _save_last_request(scope=scope, label=label, data=req)


class LiveStreamCallback(BaseCallbackHandler):
    """Best-effort live stream of LLM output.

    - Always appends to a stream file (tail it in a second terminal).
    - Optionally echoes tokens to the main console when enabled.
    - For GM scope: updates shared state for StreamWatchdog to detect invalid patterns.

    This is intentionally simple and non-blocking; failures are ignored.
    Pattern detection is now handled entirely by StreamWatchdog (separate thread).
    """

    def __init__(self, *, scope: str, label: Optional[str] = None, detect_invalid: bool = True) -> None:
        super().__init__()
        self.scope = str(scope or "").strip() or "unknown"
        self.label = str(label or "").strip() or None
        self._saw_tokens: bool = False
        self._last_token_ended_with_newline: bool = True
        self._console_newline_run: int = 0
        self._detect_invalid = detect_invalid and (self.scope in {"gm", "storage_assistant"})
        self._accumulated_text: str = ""
        self._accumulated_text_lock = threading.Lock()
        self._in_tool_call: bool = False  # Track if we're inside a tool call
        self._in_tool_call_lock = threading.Lock()
        self._current_tool_name: str = "<tool>"  # Track current tool name for context change detection

    def get_accumulated_text(self) -> str:
        """Thread-safe getter for accumulated text (used by watchdog)."""
        with self._accumulated_text_lock:
            return self._accumulated_text

    def is_in_tool_call(self) -> bool:
        """Thread-safe check if currently in a tool call (used by watchdog)."""
        with self._in_tool_call_lock:
            return self._in_tool_call

    def _max_console_consecutive_newlines(self) -> int:
        raw = (os.getenv("LLM_WORLD_STREAM_ECHO_MAX_CONSECUTIVE_NEWLINES") or "").strip()
        if not raw:
            return 2
        try:
            return max(0, int(raw))
        except Exception:
            return 2

    def _filter_for_console_echo(self, text: str) -> str:
        """Filter streamed text for console readability.

        We keep the stream file raw, but collapse excessive blank lines in the console.
        """

        if not text:
            return ""
        max_nl = self._max_console_consecutive_newlines()
        if max_nl < 0:
            return text

        out_chars: List[str] = []
        for ch in text:
            if ch == "\n":
                self._console_newline_run += 1
                if self._console_newline_run <= max_nl:
                    out_chars.append(ch)
                continue
            # Any non-newline resets the run.
            self._console_newline_run = 0
            out_chars.append(ch)
        return "".join(out_chars)

    def _prefix(self) -> str:
        if self.label:
            return f"[{self.scope}:{self.label}] "
        return f"[{self.scope}] "

    def _append(self, text: str) -> None:
        try:
            p = stream_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with _STREAM_LOCK:
                with p.open("a", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
        except Exception:
            pass

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any) -> None:  # noqa: D401
        # Start marker for easier tailing.
        self._saw_tokens = False
        self._last_token_ended_with_newline = True
        self._console_newline_run = 0
        with self._accumulated_text_lock:
            self._accumulated_text = ""
        _set_shared_accumulated_text("")  # Reset shared state for watchdog
        with self._in_tool_call_lock:
            self._in_tool_call = False
        _set_shared_in_tool_call(False)  # Reset shared state for watchdog
        self._append(f"\n{self._prefix()}<llm_start { _utc_now_iso() }>\n")

        # Best-effort: persist last prompt for UI debug (fallback for string prompts).
        try:
            if prompts:
                # Add timestamp header
                header = f"# PROMPT CAPTURED: {_utc_now_iso()} | scope={self.scope} label={self.label}\n\n"
                text = header + "\n\n".join([str(p or "") for p in prompts])
                _save_last_prompt(scope=self.scope, label=self.label, text=text)
        except Exception:
            pass

    def on_chat_model_start(self, serialized: Dict[str, Any], messages: List[List[BaseMessage]], **kwargs: Any) -> None:  # noqa: D401
        # Reset for chat model start as well
        with self._accumulated_text_lock:
            self._accumulated_text = ""
        _set_shared_accumulated_text("")  # Reset shared state for watchdog
        with self._in_tool_call_lock:
            self._in_tool_call = False
        _set_shared_in_tool_call(False)  # Reset shared state for watchdog
        # Best-effort: persist last prompt for UI debug (preferred for chat models).
        try:
            text = _format_messages_for_prompt(messages, scope=self.scope, label=self.label)
            _save_last_prompt(scope=self.scope, label=self.label, text=text)
        except Exception:
            pass

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> None:
        # Mark that we're in a tool call - don't detect patterns in tool arguments
        with self._in_tool_call_lock:
            self._in_tool_call = True
        _set_shared_in_tool_call(True)  # Update shared state for watchdog
        tool = _tool_name_from_serialized(serialized) if isinstance(serialized, dict) else "<tool>"
        self._current_tool_name = tool
        
        # Track whether context was already changed BEFORE this tool started
        # so we only raise ContextChangedInCallbackError for the tool that actually changed it
        try:
            from gm.tools import is_context_changed
            self._context_was_changed_before_tool = is_context_changed()
        except ImportError:
            self._context_was_changed_before_tool = False
        
        self._append(f"\n{self._prefix()}<tool_start {tool}>\n")

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        with self._in_tool_call_lock:
            self._in_tool_call = False
        _set_shared_in_tool_call(False)  # Update shared state for watchdog
        tool_name = getattr(self, "_current_tool_name", "<tool>")
        self._append(f"\n{self._prefix()}<tool_end>\n")
        
        # Check for context change AFTER the tool completes (GM scope only).
        # Only raise if THIS tool changed the context (not a previous tool).
        # This prevents false positives for read-only tools like get_character_detail
        # that run after a context-changing tool like start_scene.
        if self._detect_invalid and self.scope == "gm":
            try:
                from gm.tools import is_context_changed
                context_changed_before = getattr(self, "_context_was_changed_before_tool", False)
                if is_context_changed() and not context_changed_before:
                    self._append(f"\n{self._prefix()}<context_changed_after {tool_name}>\n")
                    raise ContextChangedInCallbackError(tool_name)
            except ImportError:
                pass  # Can't check context change, continue normally

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:  # noqa: D401
        if token is None:
            return
        t = str(token)
        if not t:
            return

        # Check if watchdog requested abort - raise to stop the stream
        # Only applies to GM scope (watchdog only monitors GM output)
        # KeyboardInterrupt is less likely to be caught by LangChain's exception handlers
        if self._detect_invalid and _is_watchdog_abort_requested():
            self._append(f"\n{self._prefix()}<watchdog_abort_triggered>\n")
            # Log the abort to watchdog log
            pattern = get_detected_invalid_pattern()
            if pattern:
                log_abort_triggered(pattern)
                # Re-set the pattern since get_detected_invalid_pattern clears it
                _set_detected_invalid_pattern(pattern)
            raise KeyboardInterrupt("Stream watchdog requested abort")

        self._saw_tokens = True
        self._last_token_ended_with_newline = t.endswith("\n")

        # Accumulate text for pattern detection and direct text abort
        in_tool = self.is_in_tool_call()
        if not in_tool:
            direct_abort_active = _is_direct_text_abort_active()
            if self._detect_invalid or direct_abort_active:
                with self._accumulated_text_lock:
                    self._accumulated_text += t
                    # Update shared state for watchdog to monitor (GM/SA scopes)
                    if self._detect_invalid:
                        _set_shared_accumulated_text(self._accumulated_text)
                    # Direct text abort: stop generation early if agent should
                    # be using tool calls but is producing raw text instead
                    if direct_abort_active:
                        word_count = len(self._accumulated_text.split())
                        max_w = _get_direct_text_abort_max_words()
                        if word_count > max_w:
                            pattern = f"direct_text ({word_count} words > {max_w})"
                            self._append(f"\n{self._prefix()}<direct_text_abort {word_count} words>\n")
                            _set_detected_invalid_pattern(pattern)
                            raise KeyboardInterrupt(
                                f"Direct text abort: {word_count} words without tool call"
                            )

        self._append(t)
        if stream_echo_enabled():
            try:
                # Print with minimal formatting to avoid massive blank-line spam.
                print(self._filter_for_console_echo(t), end="", flush=True)
            except Exception:
                pass

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:  # noqa: D401
        # Ensure a newline even if token streaming was not emitted.
        # If tokens were streamed, don't also dump the full text again.
        # If no tokens were streamed (some providers), fall back to printing the final text.
        try:
            text = ""
            if isinstance(response, LLMResult) and response.generations:
                gen0 = response.generations[0][0] if response.generations[0] else None
                maybe = getattr(gen0, "text", None)
                if isinstance(maybe, str) and maybe.strip():
                    text = maybe
            if text and not self._saw_tokens:
                self._append(text)
                if stream_echo_enabled():
                    print(self._filter_for_console_echo(text), end="", flush=True)
            elif stream_echo_enabled() and self._saw_tokens and not self._last_token_ended_with_newline:
                # Keep the console prompt from sticking to the last token.
                print()
        except Exception:
            pass
        self._append(f"\n{self._prefix()}<llm_end { _utc_now_iso() }>\n")


def _is_game_scope(scope: str) -> bool:
    return str(scope or "").strip().lower() in {"gm", "character", "storage_assistant"}


def reset_logs_if_enabled(
    *,
    runs_log_path: Union[str, Path] = "logs/runs.jsonl",
    tools_log_path: Union[str, Path] = "logs/tool_calls.jsonl",
    model_outputs_log_path: Union[str, Path] = "logs/model_outputs.jsonl",
) -> None:
    if not logs_enabled():
        return

    extra = [Path("logs/malformed_tool_markup.jsonl")]

    for p in [Path(runs_log_path), Path(tools_log_path), Path(model_outputs_log_path)] + extra:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                p.unlink()
        except Exception:
            # Best-effort: logging must never block the app from starting.
            pass


def _append_jsonl(path: Union[str, Path], record: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        # Ensure each record is persisted immediately (useful for hangs/crashes).
        f.flush()
        try:
            if _env_flag("LLM_WORLD_LOGS_FSYNC", False):
                os.fsync(f.fileno())
        except Exception:
            pass


def _messages_to_dicts(messages: List[BaseMessage]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in messages:
        role = getattr(m, "type", m.__class__.__name__).lower()
        # Normalize langchain message types
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        elif role == "system":
            role = "system"
        out.append({"role": role, "content": getattr(m, "content", "") or ""})
    return out


def _message_role(m: BaseMessage) -> str:
    role = getattr(m, "type", m.__class__.__name__).lower()
    if role == "human":
        return "user"
    if role == "ai":
        return "assistant"
    return role


def _is_prompt_usage_scope(scope: str) -> bool:
    return str(scope or "").strip().lower() in {"storage_assistant", "game_master", "character"}


def _estimate_prompt_history_split(messages: List[List[BaseMessage]]) -> Dict[str, int]:
    """Best-effort split of prompt tokens into history vs non-history part.

    - history: prior conversation messages (non-system), excluding an optional
      current tail user message.
    - prompt_part: everything else (system/context/current request).
    """

    flat: List[BaseMessage] = []
    for thread in messages or []:
        for m in thread or []:
            flat.append(m)

    total_tokens = 0
    for m in flat:
        total_tokens += approx_token_count(str(getattr(m, "content", "") or ""))

    current_user_idx = -1
    if flat:
        last = flat[-1]
        if _message_role(last) == "user":
            current_user_idx = len(flat) - 1

    history_tokens = 0
    history_msgs = 0
    for idx, m in enumerate(flat):
        role = _message_role(m)
        if role == "system":
            continue
        if idx == current_user_idx:
            continue
        content = str(getattr(m, "content", "") or "")
        # Internal retry directives are runtime control messages, not history.
        if role == "user" and content.startswith("[internal_retry_v1]"):
            continue
        history_msgs += 1
        history_tokens += approx_token_count(content)

    prompt_part_tokens = max(0, int(total_tokens) - int(history_tokens))
    return {
        "total_tokens": int(total_tokens),
        "history_tokens": int(history_tokens),
        "history_msgs": int(history_msgs),
        "prompt_part_tokens": int(prompt_part_tokens),
        "total_msgs": int(len(flat)),
    }


def _tool_name_from_serialized(serialized: Dict[str, Any]) -> str:
    for k in ("name", "id", "tool", "tool_name"):
        v = serialized.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return "<unknown_tool>"


def _maybe_parse_json(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


def _maybe_parse_python_dict(s: str) -> Any:
    # LangGraph tool inputs are sometimes repr(dict) rather than JSON.
    if not isinstance(s, str):
        return s
    t = s.strip()
    if not (t.startswith("{") or t.startswith("[")):
        return s
    try:
        return ast.literal_eval(t)
    except Exception:
        return s


def _tool_label(tool_name: str, parsed_input: Any, input_str: str) -> Optional[str]:
    data = parsed_input
    if not isinstance(data, dict):
        alt = _maybe_parse_python_dict(input_str)
        if isinstance(alt, dict):
            data = alt
    if not isinstance(data, dict):
        return None

    def _get_str(key: str) -> Optional[str]:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    if tool_name in {"create_location", "get_location"}:
        return _get_str("name")

    if tool_name in {"create_npc"}:
        n = _get_str("name")
        loc = _get_str("location")
        if n and loc:
            return f"{n} @ {loc}"
        return n or loc

    if tool_name == "get_character_detail":
        return _get_str("name")

    if tool_name in {"update_character", "add_character"}:
        n = _get_str("name")
        ptr = _get_str("json_pointer")
        if n and ptr:
            return f"{n} {ptr}"
        return n or ptr

    if tool_name in {"start_scene", "bookkeeping_done"}:
        loc = _get_str("location")
        chars = data.get("character_names")
        chars_s = None
        if isinstance(chars, list):
            clean = [str(x).strip() for x in chars if str(x).strip()]
            if clean:
                chars_s = ", ".join(clean)
        if loc and chars_s:
            return f"{loc} ({chars_s})"
        return loc or chars_s or "pending-scene"

    return None


def _truncate_value(value: Any, *, max_chars: int) -> Any:
    return value


def _extract_tool_payload_from_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort: pull the literal tool schema payload from LangChain callback kwargs.

    Different LangChain/OpenAI wrappers use different keys (tools/functions).
    """

    if not isinstance(kwargs, dict):
        return {}

    # Common for chat models.
    inv = kwargs.get("invocation_params")
    if isinstance(inv, dict):
        for k in ("tools", "functions", "tool_choice", "function_call"):
            if k in inv:
                pass
        return {k: inv.get(k) for k in ("tools", "functions", "tool_choice", "function_call") if k in inv}

    # Fallback: sometimes these are passed top-level.
    out: Dict[str, Any] = {}
    for k in ("tools", "functions", "tool_choice", "function_call"):
        if k in kwargs:
            out[k] = kwargs.get(k)
    return out


_TOOL_MESSAGE_CONTENT_RE = re.compile(r"^content='([\s\S]*?)'\s+name=", flags=re.DOTALL)


def _sanitize_tool_output(output: Any) -> Any:
    """Return game-relevant tool output without transport/model metadata."""

    try:
        if isinstance(output, BaseMessage):
            return getattr(output, "content", "") or ""
    except Exception:
        pass

    # LangChain sometimes passes repr(ToolMessage) as a string.
    if isinstance(output, str):
        m = _TOOL_MESSAGE_CONTENT_RE.match(output.strip())
        if m:
            return m.group(1)
        # Fallback: strip known metadata fragments.
        cleaned = output
        cleaned = re.sub(r"\s+tool_call_id='[^']*'", "", cleaned)
        cleaned = re.sub(r"\s+run_id='[^']*'", "", cleaned)
        cleaned = re.sub(r"\s+parent_run_id='[^']*'", "", cleaned)
        return cleaned.strip()

    # Generic object with .content
    try:
        content = getattr(output, "content", None)
        if isinstance(content, str):
            return content
    except Exception:
        pass

    return output


def _get_model_from_any(inp: Any, llm_output: Any) -> Optional[str]:
    # Prefer llm_output metadata if present.
    if isinstance(llm_output, dict):
        for k in ("model", "model_name"):
            v = llm_output.get(k)
            if isinstance(v, str) and v.strip():
                return v
    if isinstance(inp, dict):
        # We avoid storing the full serialized payload, but if verbose mode
        # captured it, we can still extract the model.
        serialized = inp.get("serialized")
        if isinstance(serialized, dict):
            kwargs = serialized.get("kwargs")
            if isinstance(kwargs, dict):
                v = kwargs.get("model_name") or kwargs.get("model")
                if isinstance(v, str) and v.strip():
                    return v
    return None


def _extract_usage(llm_output: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(llm_output, dict):
        return None
    usage = llm_output.get("token_usage") or llm_output.get("usage")
    return usage if isinstance(usage, dict) else None


def _console_trace_enabled() -> bool:
    # When logs are enabled, we default to tracing unless explicitly disabled.
    return _env_flag("LLM_WORLD_CONSOLE_TRACE", True)


def _console_print(line: str) -> None:
    try:
        print(line, flush=True)
    except Exception:
        pass


@dataclass
class OpenRouterLoggingCallback(BaseCallbackHandler):
    """Logs chat model inputs/outputs to JSONL.

    This is intentionally minimal and robust across minor LangChain versions.
    """

    runs_log_path: str = "logs/runs.jsonl"
    tools_log_path: str = "logs/tool_calls.jsonl"
    model_outputs_log_path: str = "logs/model_outputs.jsonl"
    scope: str = "gm"  # e.g., "gm" or "character"
    enabled: bool = True
    verbose: bool = False
    console_trace: bool = True

    # Stores last seen input per run id.
    _inputs_by_run: Dict[str, Any] = None  # type: ignore[assignment]

    # Best-effort: last model input size (approx tokens) for console trace.
    _last_prompt_tokens_est: int = 0
    _last_prompt_messages_count: int = 0

    def __post_init__(self) -> None:
        if self._inputs_by_run is None:
            self._inputs_by_run = {}
        # Environment wins unless explicitly overridden by caller.
        if self.enabled is True and not logs_enabled():
            self.enabled = False
        # Default to minimal readable logs unless explicitly enabled.
        if self.verbose is False and _env_flag("LLM_WORLD_LOGS_VERBOSE", False):
            self.verbose = True
        if self.console_trace is True and not _console_trace_enabled():
            self.console_trace = False

    def on_chat_model_start(self, serialized: Dict[str, Any], messages: List[List[BaseMessage]], *, run_id: str, **kwargs: Any) -> None:  # noqa: D401
        if not self.enabled:
            return

        # Track approx prompt size for console trace (do not log content).
        try:
            msg_count = 0
            token_est = 0
            for thread in messages or []:
                for m in thread or []:
                    msg_count += 1
                    token_est += approx_token_count(str(getattr(m, "content", "") or ""))
            self._last_prompt_tokens_est = int(token_est)
            self._last_prompt_messages_count = int(msg_count)
        except Exception:
            self._last_prompt_tokens_est = 0
            self._last_prompt_messages_count = 0

        if self.console_trace and _is_prompt_usage_scope(self.scope):
            try:
                usage = _estimate_prompt_history_split(messages)
                lim = limits_from_env()
                max_ctx = int(lim.model_max_context_tokens)
                total_used = int(usage.get("total_tokens", 0))
                history_tokens = int(usage.get("history_tokens", 0))
                history_msgs = int(usage.get("history_msgs", 0))
                prompt_part_tokens = int(usage.get("prompt_part_tokens", 0))
                total_msgs = int(usage.get("total_msgs", 0))

                pct_total = int(round((total_used / max_ctx) * 100.0)) if max_ctx > 0 else 0
                pct_hist = int(round((history_tokens / max_ctx) * 100.0)) if max_ctx > 0 else 0
                pct_prompt = int(round((prompt_part_tokens / max_ctx) * 100.0)) if max_ctx > 0 else 0

                scope_label = str(self.scope or "").strip()
                if self.label:
                    scope_label = f"{scope_label}:{self.label}"

                _console_print(
                    f"[trace] prompt usage {scope_label} | history~{history_tokens}/{max_ctx} tok (~{pct_hist}%, msgs={history_msgs}) "
                    f"| prompt_part~{prompt_part_tokens}/{max_ctx} tok (~{pct_prompt}%) "
                    f"| total~{total_used}/{max_ctx} tok (~{pct_total}%, msgs={total_msgs})"
                )
            except Exception:
                pass

        # We intentionally do NOT store model/provider/tokens/run metadata.
        # Track run_id only to avoid unbounded growth until on_llm_end pops it.
        if self.verbose:
            self._inputs_by_run[run_id] = {
                "messages": [_messages_to_dicts(thread) for thread in messages],
                "prompt_tokens_est": self._last_prompt_tokens_est,
                "prompt_messages_count": self._last_prompt_messages_count,
            }
        else:
            # Keep only size stats to help debugging without leaking prompt content.
            self._inputs_by_run[run_id] = {
                "prompt_tokens_est": self._last_prompt_tokens_est,
                "prompt_messages_count": self._last_prompt_messages_count,
            }

        _capture_api_request(
            scope=self.scope,
            label=getattr(self, "label", None),
            run_id=run_id,
            event="chat_model_start",
            serialized=serialized,
            kwargs=kwargs,
            payload={"messages": messages},
        )

        # Optional: log the literal tool schemas being passed to the model.
        # This answers "what did LangGraph inject" even though tools are not part of the text prompt.
        if _tool_schema_logging_enabled() and _is_game_scope(self.scope):
            try:
                payload = _extract_tool_payload_from_kwargs(kwargs)
                _append_jsonl(
                    "logs/tool_schemas.jsonl",
                    {
                        "ts": _utc_now_iso(),
                        "scope": self.scope,
                        "event": "tool_schemas",
                        "run_id": str(run_id),
                        "tool_payload": payload,
                    },
                )
            except Exception:
                pass

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], *, run_id: str, **kwargs: Any) -> None:  # noqa: D401
        if not self.enabled:
            return
        # Fallback for older versions (string prompts): compute size stats.
        try:
            token_est = 0
            msg_count = 0
            for p in prompts or []:
                msg_count += 1
                token_est += approx_token_count(str(p or ""))
            self._last_prompt_tokens_est = int(token_est)
            self._last_prompt_messages_count = int(msg_count)
        except Exception:
            self._last_prompt_tokens_est = 0
            self._last_prompt_messages_count = 0

        # Fallback for older versions (string prompts).
        if run_id not in self._inputs_by_run:
            if self.verbose:
                self._inputs_by_run[run_id] = {
                    "prompts": prompts,
                    "prompt_tokens_est": self._last_prompt_tokens_est,
                    "prompt_messages_count": self._last_prompt_messages_count,
                }
            else:
                self._inputs_by_run[run_id] = {
                    "prompt_tokens_est": self._last_prompt_tokens_est,
                    "prompt_messages_count": self._last_prompt_messages_count,
                }

        _capture_api_request(
            scope=self.scope,
            label=getattr(self, "label", None),
            run_id=run_id,
            event="llm_start",
            serialized=serialized,
            kwargs=kwargs,
            payload={"prompts": prompts},
        )

    def on_llm_end(self, response: LLMResult, *, run_id: str, **kwargs: Any) -> None:  # noqa: D401
        if not self.enabled:
            return
        _ = self._inputs_by_run.pop(run_id, None)

        generations_text: List[str] = []
        try:
            for gen_list in (response.generations or []):
                for gen in gen_list:
                    generations_text.append(getattr(gen, "text", "") or "")
        except Exception:
            generations_text = []

        # Intentionally do not log raw model outputs (they are model-related, not game state).
        # Game state is reflected by tool calls/results and the persistent JSON storage.

    def on_llm_error(self, error: BaseException, *, run_id: str, **kwargs: Any) -> None:  # noqa: D401
        if not self.enabled:
            return
        _ = self._inputs_by_run.pop(run_id, None)
        if _is_game_scope(self.scope):
            _append_jsonl(
                self.runs_log_path,
                {
                    "ts": _utc_now_iso(),
                    "scope": self.scope,
                    "event": "error",
                    "error": str(error),
                },
            )

    # Tool logging (so we can debug hangs in ToolNode)
    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: str,
        parent_run_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        if not self.enabled:
            return

        tool_name = _tool_name_from_serialized(serialized)
        parsed_input = _maybe_parse_json(input_str)

        label = _tool_label(tool_name, parsed_input, input_str)

        minimal_tool_call = {
            "ts": _utc_now_iso(),
            "scope": self.scope,
            "event": "tool_call",
            "tool": tool_name,
            "params": parsed_input,
        }
        if label:
            minimal_tool_call["label"] = label

        if self.console_trace:
            extra = ""
            if tool_name in {"character_decision"}:
                try:
                    lim = limits_from_env()
                    max_ctx = int(lim.model_max_context_tokens)
                    used = int(self._last_prompt_tokens_est or 0)
                    msgs = int(self._last_prompt_messages_count or 0)
                    if max_ctx > 0 and used > 0:
                        pct = int(round((used / max_ctx) * 100.0))
                        extra = f" | ctx~{used}/{max_ctx} tok (~{pct}%) msgs={msgs}"
                    else:
                        extra = f" | ctx~{used} tok msgs={msgs}"
                except Exception:
                    extra = ""

            if label:
                _console_print(f"[trace] {self.scope} called tool {tool_name} ({label}){extra}")
            else:
                _console_print(f"[trace] {self.scope} called tool {tool_name}{extra}")

        # Run trace (minimal)
        _append_jsonl(self.runs_log_path, minimal_tool_call)

        # Tool-call focused trace (required by app)
        _append_jsonl(
            self.tools_log_path,
            {
                "ts": minimal_tool_call["ts"],
                "scope": minimal_tool_call["scope"],
                "event": minimal_tool_call["event"],
                "tool": minimal_tool_call["tool"],
                "params": minimal_tool_call["params"],
                **({"label": label} if label else {}),
            },
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: str,
        parent_run_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        if not self.enabled:
            return

        sanitized = _sanitize_tool_output(output)
        _append_jsonl(
            self.runs_log_path,
            {
                "ts": _utc_now_iso(),
                "scope": self.scope,
                "event": "tool_result",
                "output": sanitized,
            },
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: str,
        parent_run_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        if not self.enabled:
            return
        _append_jsonl(
            self.runs_log_path,
            {
                "ts": _utc_now_iso(),
                "scope": self.scope,
                "event": "tool_error",
                "error": str(error),
            },
        )
