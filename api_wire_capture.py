"""What actually leaves the process: every request body, as the API receives it.

A hand-written log line can only ever show what whoever wrote the logging
remembered to print, and the prompt a model is really given is assembled inside
the client — after every conversion, role mapping and parameter merge. The
transport is the one place that sees the result whole, so the capture sits
there: an ``httpx`` client whose request hook writes down the body verbatim.

What it writes, per invocation:

* ``logs/last_prompts/{scope}__{label}.txt`` — the request rendered to be read:
  every message with its role, its content, its tool calls with their arguments,
  its tool-call id, the reasoning that is being sent back, and anything else the
  message carries; then every tool with its description and parameters; then
  every other field of the body as sent. Nothing is summarised and nothing is
  dropped: a capture that edits the payload cannot be used to answer what the
  model was given.
* optionally ``logs/api_wire.jsonl`` — one line per request with the body as
  JSON, for machine inspection (``LLM_WORLD_LOG_WIRE_JSONL=1``).

Which invocation a request belongs to is not visible in the body, so the agents'
logging callbacks hand it over: the callback constructed for a phase records its
scope and label here (``set_current_scope``), and the hook reads them back in the
same thread before the request goes out.

Switches: ``LLM_WORLD_LOG_WIRE=0`` turns the capture off;
``LLM_WORLD_LOG_WIRE_RESPONSES=1`` also appends a response body — only for
non-streaming calls, because a streamed body belongs to the client that reads it.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_SEQ_LOCK = threading.Lock()
_CLIENT = None
_CLIENT_TRIED = False
_SCOPE = threading.local()


# ---------------------------------------------------------------------------
# Which invocation is this request part of
# ---------------------------------------------------------------------------


def set_current_scope(scope: str, label: Optional[str] = None) -> None:
    """Record the invocation the next request from this thread belongs to."""
    _SCOPE.scope = str(scope or "").strip() or "unknown"
    _SCOPE.label = str(label or "").strip() or None


def current_scope() -> tuple:
    return (getattr(_SCOPE, "scope", None) or "unknown",
            getattr(_SCOPE, "label", None))


# ---------------------------------------------------------------------------
# The one client every model shares
# ---------------------------------------------------------------------------


def wire_http_client():
    """The shared transport that writes down what it sends, or None.

    One client for the whole process: the capture is the point, and a shared
    connection pool costs nothing extra. Returns None only when httpx cannot be
    used at all, so a missing capture never takes the game down with it.
    """
    global _CLIENT, _CLIENT_TRIED
    with _SEQ_LOCK:
        if _CLIENT_TRIED:
            return _CLIENT
        _CLIENT_TRIED = True
        try:
            import httpx

            _CLIENT = httpx.Client(
                event_hooks={"request": [_on_request], "response": [_on_response]},
            )
        except Exception:
            _CLIENT = None
        return _CLIENT


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "t", "yes", "y", "on"}


def capture_enabled() -> bool:
    return _flag("LLM_WORLD_LOG_WIRE", True)


def capture_responses() -> bool:
    return _flag("LLM_WORLD_LOG_WIRE_RESPONSES", False)


def capture_jsonl() -> bool:
    return _flag("LLM_WORLD_LOG_WIRE_JSONL", False)


# ---------------------------------------------------------------------------
# Where it goes
# ---------------------------------------------------------------------------


def _safe(name: Optional[str]) -> str:
    out = []
    for ch in (str(name or "").strip() or "unknown"):
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    return "".join(out).strip("_") or "unknown"


def _logs_dir() -> Path:
    from openrouter_langchain_logging import logs_dir

    return logs_dir()


def prompt_path(scope: str, label: Optional[str]) -> Path:
    return _logs_dir() / "last_prompts" / f"{_safe(scope)}__{_safe(label)}.txt"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def _on_request(request) -> None:
    """Write down the outgoing request. Never raises into the caller."""
    if not capture_enabled():
        return
    try:
        body = request.content
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            return
        scope, label = current_scope()
        text = _render(payload, url=str(request.url), scope=scope, label=label)
        # Only what carries messages is a prompt; another endpoint's body is
        # still written to the wire log when that is switched on.
        if isinstance(payload.get("messages"), list):
            _write(prompt_path(scope, label), text)
        if capture_jsonl():
            _append(_logs_dir() / "api_wire.jsonl",
                    json.dumps({"ts": _now(), "url": str(request.url), "scope": scope,
                                "label": label, "body": payload},
                               ensure_ascii=False, default=str) + "\n")
    except Exception:
        # The capture is a debugging aid; it must never break a request.
        pass


def _on_response(response) -> None:
    if not capture_enabled() or not capture_responses():
        return
    try:
        content_type = str(response.headers.get("content-type") or "").lower()
        if "event-stream" in content_type:
            # A streamed body belongs to the client reading it; taking it here
            # would leave the caller with nothing.
            return
        scope, label = current_scope()
        path = prompt_path(scope, label)
        if not path.exists():
            return
        _append(path, "\n=== response " + str(response.status_code) + " ===\n"
                      + str(response.text) + "\n")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render(payload: Dict[str, Any], *, url: str, scope: str, label: Optional[str]) -> str:
    """The request, written out whole. Falls back to raw JSON on any surprise."""
    try:
        return _render_payload(payload, url=url, scope=scope, label=label)
    except Exception:
        return (f"# SENT TO THE API: {_now()}\n# {url}\n# scope={scope} label={label}\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _render_payload(payload: Dict[str, Any], *, url: str,
                    scope: str, label: Optional[str]) -> str:
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    out: list = [
        f"# SENT TO THE API: {_now()}",
        f"# {url}",
        f"# scope={scope} label={label} model={payload.get('model')}",
        f"# messages={len(messages)} tools={len(tools)}",
    ]
    rest = {k: v for k, v in payload.items() if k not in ("messages", "tools")}
    if rest:
        out.append("# other fields as sent: " + json.dumps(rest, ensure_ascii=False))
    out.append("")
    out.append(f"=== messages ({len(messages)}) ===")
    out.append("")
    for i, m in enumerate(messages):
        out.extend(_render_message(i, m))
    out.append(f"=== tools ({len(tools)}) ===")
    for tool in tools:
        out.extend(_render_tool(tool))
    return "\n".join(out).rstrip() + "\n"


_MSG_KNOWN = {"role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content"}


def _arguments(args: Any) -> str:
    """A call's arguments as the body carries them: a JSON string.

    Shown parsed when it parses — the same values, readable — and as it stands
    when it does not, because what the model sent is what has to be visible.
    """
    if isinstance(args, str):
        try:
            return json.dumps(json.loads(args), ensure_ascii=False)
        except Exception:
            return args
    return json.dumps(args, ensure_ascii=False)


def _render_message(index: int, message: Any) -> list:
    if not isinstance(message, dict):
        return [f"[{index}] {message!r}", ""]
    head = f"[{index}] {message.get('role') or '?'}"
    if message.get("name"):
        head += f" name={message['name']}"
    if message.get("tool_call_id"):
        head += f" tool_call_id={message['tool_call_id']}"
    out = [head]
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            out.append(content)
    elif content not in (None, "", [], {}):
        out.append(json.dumps(content, ensure_ascii=False, indent=2))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        cid = call.get("id") if isinstance(call, dict) else None
        out.append(f"  -> {name or '?'}({_arguments(args)})" + (f"  [call {cid}]" if cid else ""))
    reasoning = message.get("reasoning_content")
    if reasoning:
        out.append(f"  --- reasoning re-sent, {len(str(reasoning))} chars ---")
        out.append(str(reasoning))
    extra = {k: v for k, v in message.items() if k not in _MSG_KNOWN}
    if extra:
        out.append("  [also sent: " + json.dumps(extra, ensure_ascii=False, default=str) + "]")
    out.append("")
    return out


def _render_tool(tool: Any) -> list:
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict):
        return [json.dumps(tool, ensure_ascii=False, default=str), ""]
    out = ["", f"* {fn.get('name') or '?'}"]
    description = str(fn.get("description") or "").strip()
    if description:
        out.append(description)
    if fn.get("parameters") is not None:
        out.append("parameters: " + json.dumps(fn["parameters"], ensure_ascii=False, indent=2))
    extra = {k: v for k, v in tool.items() if k != "function"}
    if extra:
        out.append("[also sent: " + json.dumps(extra, ensure_ascii=False, default=str) + "]")
    return out
