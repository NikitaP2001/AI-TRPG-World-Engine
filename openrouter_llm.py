from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from config import load_deepseek_config
from openrouter_langchain_logging import OpenRouterLoggingCallback, logs_enabled


def read_prompt_text(path: Union[str, Path]) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


_REASONING_PATCH_FLAG = "_llm_world_reasoning_patch"


def _preserve_reasoning_content() -> None:
    """Keep the provider's `reasoning_content` on a message, in both directions.

    A reasoning-capable backend answers through two separate channels: the model's
    own reasoning, and the deliberate answer. The LangChain OpenAI adapter drops the
    first one both ways.

    *Incoming*: without the field the two channels are indistinguishable at every
    call site, and reasoning that has nowhere to go is emitted as the answer.

    *Outgoing*: while reasoning is on, the backend requires the reasoning of the
    message being continued to be sent back with it and rejects the request
    otherwise (``The `reasoning_content` in the thinking mode must be passed back
    to the API``). That applies to every request that does not end with a user
    message — that is, to every tool loop.

    Applied once, lazily, when an LLM is built.
    """
    try:
        from langchain_openai.chat_models import base as _base  # type: ignore
    except Exception:
        return
    if getattr(_base, _REASONING_PATCH_FLAG, False):
        return

    def _reasoning_of(message: Any) -> Optional[str]:
        ak = getattr(message, "additional_kwargs", None) or {}
        if not isinstance(ak, dict):
            return None
        rc = ak.get("reasoning_content")
        return str(rc) if rc else None

    _orig_dict_to_message = _base._convert_dict_to_message  # type: ignore[attr-defined]
    _orig_delta_to_chunk = _base._convert_delta_to_message_chunk  # type: ignore[attr-defined]
    _orig_message_to_dict = getattr(_base, "_convert_message_to_dict", None)

    def _dict_to_message(_dict):  # type: ignore[no-untyped-def]
        msg = _orig_dict_to_message(_dict)
        _reasoning = _dict.get("reasoning_content")
        if _reasoning:
            try:
                msg.additional_kwargs["reasoning_content"] = _reasoning
            except Exception:
                pass
        return msg

    def _delta_to_chunk(_dict, default_class):  # type: ignore[no-untyped-def]
        chunk = _orig_delta_to_chunk(_dict, default_class)
        _reasoning = _dict.get("reasoning_content")
        if _reasoning:
            try:
                chunk.additional_kwargs["reasoning_content"] = _reasoning
            except Exception:
                pass
        return chunk

    def _message_to_dict(message, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = _orig_message_to_dict(message, *args, **kwargs)
        if isinstance(out, dict) and out.get("role") == "assistant":
            _reasoning = _reasoning_of(message)
            if _reasoning:
                out["reasoning_content"] = _reasoning
        return out

    _base._convert_dict_to_message = _dict_to_message  # type: ignore[attr-defined]
    _base._convert_delta_to_message_chunk = _delta_to_chunk  # type: ignore[attr-defined]
    if _orig_message_to_dict is not None:
        _base._convert_message_to_dict = _message_to_dict  # type: ignore[attr-defined]
    setattr(_base, _REASONING_PATCH_FLAG, True)


def extract_reasoning(msg: Any) -> str:
    """The model's own reasoning, when the backend delivers it as its own channel.

    Empty when the backend does not separate reasoning from the answer: in that
    case what the model thought is indistinguishable from what it said, and
    ``content`` is the only source there is.
    """
    try:
        ak = getattr(msg, "additional_kwargs", None) or {}
        if not isinstance(ak, dict):
            return ""
        return str(ak.get("reasoning_content") or "").strip()
    except Exception:
        return ""


def build_openrouter_chat_llm(
    *,
    temperature: float,
    model: Optional[str] = None,
    title_suffix: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    include_headers: Optional[bool] = None,
    timeout: float = 60.0,
    max_retries: int = 2,
    streaming: bool = True,
    max_tokens: Optional[int] = None,
    parallel_tool_calls: bool = True,
    thinking: Optional[bool] = None,
    effort: Optional[str] = None,
) -> ChatOpenAI:
    """Create a ChatOpenAI client configured for DeepSeek (or any OpenAI-compatible backend).

    `title_suffix` is appended to the X-Title header for easier debugging.
    `parallel_tool_calls` controls whether the model can call multiple tools in one response.
    `effort` is how hard the model is asked to think while reasoning is on — one of
    none, minimal, low, medium, high, xhigh, ultra, max. Asked, not guaranteed: the
    backend accepts the word and what it does with it is its own business.
    """

    # Ensure .env is applied even if the parent process has stale env vars.
    load_dotenv(override=True)

    _preserve_reasoning_content()

    # If caller provides explicit transport settings, allow using any
    # OpenAI-compatible backend (e.g., local Qwen server) without requiring
    # DEEPSEEK_API_KEY.
    explicit_transport = bool(str(base_url or "").strip() or str(api_key or "").strip())

    cfg = None
    if not explicit_transport:
        cfg = load_deepseek_config()

    effective_model = (str(model).strip() if str(model or "").strip() else (cfg.model if cfg else ""))
    effective_base_url = (
        str(base_url).strip()
        if str(base_url or "").strip()
        else (cfg.base_url if cfg else (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com/v1"))
    )
    effective_api_key = (
        str(api_key).strip()
        if str(api_key or "").strip()
        else (
            (cfg.api_key if cfg else "")
            or (os.getenv("OPENAI_API_KEY") or "").strip()
            or "local"
        )
    )

    _use_headers = include_headers
    if _use_headers is None:
        _use_headers = not explicit_transport

    default_headers = None
    if _use_headers:
        title = "llm_world"
        if title_suffix:
            s = str(title_suffix).strip()
            if s:
                if not s.startswith("-"):
                    s = "-" + s
                title = title + s
        # httpx.Headers enforces ASCII — replace any non-ASCII chars
        title = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in title)
        default_headers = {
            "X-Title": title,
        }

    model_kwargs: Dict[str, Any] = {
        # Moved here to avoid a LangChain warning.
        "parallel_tool_calls": bool(parallel_tool_calls),
    }

    kwargs = {
        "api_key": effective_api_key,
        "base_url": effective_base_url,
        "model": effective_model,
        "temperature": float(temperature),
        "streaming": bool(streaming),
        "timeout": float(timeout),
        "max_retries": int(max_retries),
        "model_kwargs": model_kwargs,
    }

    # A backend may keep the model's reasoning in a channel of its own. Turning that
    # on is not free: the reasoning then becomes a required part of every assistant
    # message that a later request continues, and it is re-sent with it. The switch
    # stays off until that cost is accepted; a phase that forces a tool call cannot
    # turn it on at all — a forced choice is rejected outright while reasoning is on.
    # Use extra_body (not a bare model_kwarg) because the OpenAI SDK only accepts
    # provider-specific fields via the extra_body passthrough, not as direct kwargs.
    if thinking is None:
        thinking = False if "deepseek.com" in effective_base_url.lower() else None
    extra_body: Dict[str, Any] = {}
    if thinking is not None:
        extra_body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if effort and str(effort).strip():
        extra_body["reasoning_effort"] = str(effort).strip()
    if extra_body:
        kwargs["extra_body"] = extra_body
    if default_headers is not None:
        kwargs["default_headers"] = default_headers

    # Every request this client makes is written down as it leaves the process:
    # the prompt a model is actually given is assembled inside the client, after
    # every conversion, so only the transport sees it whole (api_wire_capture).
    from api_wire_capture import wire_http_client

    _wire = wire_http_client()
    if _wire is not None:
        kwargs["http_client"] = _wire
    elif logs_enabled():
        print("[wire] transport capture unavailable — logs/last_prompts will not be written")
    
    if logs_enabled():
        import sys
        print(f"[DEBUG] DeepSeek LLM created with parallel_tool_calls={bool(parallel_tool_calls)}", file=sys.stderr)
    
    if max_tokens is not None:
        try:
            mt = int(max_tokens)
            if mt > 0:
                kwargs["max_tokens"] = mt
        except Exception:
            pass

    return ChatOpenAI(**kwargs)


def openrouter_logging_callbacks(
    *,
    scope: str,
    label: Optional[str] = None,
    runs_log_path: str = "logs/runs.jsonl",
    tools_log_path: str = "logs/tool_calls.jsonl",
    model_outputs_log_path: str = "logs/model_outputs.jsonl",
    stream: bool = True,
) -> List[OpenRouterLoggingCallback]:
    from openrouter_langchain_logging import LiveStreamCallback

    def _resolve_log_path(path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            return str(p)
        logs_dir = (os.getenv("LLM_WORLD_LOGS_DIR") or "").strip()
        if logs_dir:
            return str(Path(logs_dir) / p)
        return str(p)

    runs_log_path = _resolve_log_path(runs_log_path)
    tools_log_path = _resolve_log_path(tools_log_path)
    model_outputs_log_path = _resolve_log_path(model_outputs_log_path)

    # Which invocation the next request belongs to. The request itself carries
    # no such thing, and the capture on the transport reads it back in the same
    # thread before the body goes out.
    from api_wire_capture import set_current_scope

    set_current_scope(scope, label)

    cbs = [
        OpenRouterLoggingCallback(
            scope=scope,
            runs_log_path=runs_log_path,
            tools_log_path=tools_log_path,
            model_outputs_log_path=model_outputs_log_path,
        ),
    ]
    if stream:
        cbs.append(LiveStreamCallback(scope=scope, label=label))
    return cbs


def bind_agent_tools(
    llm: Any,
    tools: List[Any],
    *,
    callbacks: Optional[List[Any]] = None,
    tool_choice: Any = None,
) -> Any:
    """Bind tools to an LLM and attach logging callbacks.

    ``tool_choice`` is passed to the model only when it is given: some
    LangChain builds reject the argument, and in that case the binding
    falls back to free tool choice rather than failing the phase.

    This is the single place where a phase decides whether the model may
    answer with plain text or is forced onto a tool.
    """
    cfg = {"callbacks": callbacks} if callbacks else {}
    if tool_choice is None:
        return llm.bind_tools(tools).with_config(cfg)
    try:
        return llm.bind_tools(tools, tool_choice=tool_choice).with_config(cfg)
    except TypeError:
        return llm.bind_tools(tools).with_config(cfg)
