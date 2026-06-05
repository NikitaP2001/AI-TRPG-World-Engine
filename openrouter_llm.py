from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Union

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from config import load_deepseek_config
from openrouter_langchain_logging import OpenRouterLoggingCallback, logs_enabled


def read_prompt_text(path: Union[str, Path]) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


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
) -> ChatOpenAI:
    """Create a ChatOpenAI client configured for DeepSeek (or any OpenAI-compatible backend).

    `title_suffix` is appended to the X-Title header for easier debugging.
    `parallel_tool_calls` controls whether the model can call multiple tools in one response.
    """

    # Ensure .env is applied even if the parent process has stale env vars.
    load_dotenv(override=True)

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

    kwargs = {
        "api_key": effective_api_key,
        "base_url": effective_base_url,
        "model": effective_model,
        "temperature": float(temperature),
        "streaming": bool(streaming),
        "timeout": float(timeout),
        "max_retries": int(max_retries),
        # Move parallel_tool_calls to model_kwargs to avoid LangChain warning
        "model_kwargs": {
            "parallel_tool_calls": bool(parallel_tool_calls),
        },
    }

    # DeepSeek direct API defaults to thinking mode ON, which forbids tool_choice.
    # Auto-disable thinking when hitting deepseek.com unless caller explicitly opts in.
    # Pass thinking=True at the call site (or via SA env var) to enable it where safe.
    # Use extra_body (not a bare model_kwarg) because the OpenAI SDK only accepts
    # DeepSeek-specific fields via the extra_body passthrough, not as direct kwargs.
    if thinking is None:
        thinking = False if "deepseek.com" in effective_base_url.lower() else None
    if thinking is not None:
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    if default_headers is not None:
        kwargs["default_headers"] = default_headers
    
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

    return [
        OpenRouterLoggingCallback(
            scope=scope,
            runs_log_path=runs_log_path,
            tools_log_path=tools_log_path,
            model_outputs_log_path=model_outputs_log_path,
        ),
        LiveStreamCallback(scope=scope, label=label),
    ]
