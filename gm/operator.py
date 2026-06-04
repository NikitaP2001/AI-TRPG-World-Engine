"""Storage Assistant agent: ReAct loop with tools for world/scene management.

This is one of two main LLM agents in the system:

1. Storage Assistant (this file):
    - ReAct agent with tools (run_scene + storage maintenance)
   - Manages simulation state and scene flow
    - Persistent history in game/storage_assistant_messages.json
    - Context: atomic marker-based deltas in persistent history
    - Prompt: agents/storage_assistant/prompt.txt

2. Game Master (gm/game_master.py):
   - Narrative agent for creative writing (world seed, scene descriptions, narration)
   - Maintains roleplay identity with persistent conversation history
   - History stored in game/game_master_messages.json
   - Context: build_game_master_context_block() without iteration mechanics
   - Prompt: agents/game_master/prompt.txt
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import atexit
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI

from bootstrap import initialize_game_dir
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from .tools import gm_tools_for_current_context, is_context_changed, is_turn_locked


_SA_MAX_TEXT_WORDS = 50  # Max words allowed in SA text output (no tool calls)
_SA_MAX_TEXT_VIOLATIONS_BEFORE_STOP = 3  # Force-stop after this many consecutive violations


def _sa_max_text_words() -> int:
    """Return max allowed SA text words.

    Set LLM_WORLD_SA_MAX_TEXT_WORDS=0 (or negative) to disable this guard.
    """

    raw = (os.getenv("LLM_WORLD_SA_MAX_TEXT_WORDS") or "").strip()
    if not raw:
        return int(_SA_MAX_TEXT_WORDS)
    try:
        return int(raw)
    except Exception:
        return int(_SA_MAX_TEXT_WORDS)


def _sa_max_text_violations_before_stop() -> int:
    raw = (os.getenv("LLM_WORLD_SA_MAX_TEXT_VIOLATIONS") or "").strip()
    if not raw:
        return int(_SA_MAX_TEXT_VIOLATIONS_BEFORE_STOP)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on", "y"}


def _format_sa_write_confirmation(tool_name: str, tool_args: Any) -> str:
    """Build a compact confirmation line for SA write operations.

    Format is intentionally explicit so users can distinguish SA writes from
    automatic runtime writes performed elsewhere in Python.
    """
    tname = str(tool_name or "").strip() or "unknown_tool"
    args = tool_args if isinstance(tool_args, dict) else {}

    name = str(args.get("name") or "").strip()
    pointer = str(args.get("json_pointer") or "").strip()

    details: list[str] = []
    if name:
        details.append(f"name={name}")
    if pointer:
        details.append(f"pointer={pointer}")

    if details:
        return f"SA_WRITE {tname} ({', '.join(details)})"
    return f"SA_WRITE {tname}"


_LOCAL_SA_RUNTIME_PROC: Optional[subprocess.Popen] = None


def _parse_host_port(url: str) -> tuple[str, int]:
    u = urlparse(str(url or "").strip())
    host = u.hostname or "127.0.0.1"
    port = int(u.port or (443 if (u.scheme or "").lower() == "https" else 80))
    return host, port


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _stop_local_sa_runtime() -> None:
    global _LOCAL_SA_RUNTIME_PROC
    proc = _LOCAL_SA_RUNTIME_PROC
    _LOCAL_SA_RUNTIME_PROC = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _ensure_local_sa_runtime(*, base_url: str, api_key: str) -> None:
    """Ensure local SA OpenAI-compatible backend is reachable.

    Current auto-start implementation targets Ollama at localhost:11434.
    """
    host, port = _parse_host_port(base_url)
    if _tcp_open(host, port):
        return

    auto_manage = _env_flag("LLM_WORLD_SA_LOCAL_AUTO_MANAGE", default=True)
    if not auto_manage:
        raise RuntimeError(
            f"Local SA backend is unreachable at {host}:{port}. "
            "Start your local model server or set LLM_WORLD_SA_LOCAL_AUTO_MANAGE=1."
        )

    # Auto-manage only loopback Ollama endpoint with local key semantics.
    if host not in {"127.0.0.1", "localhost", "::1"} or int(port) != 11434 or str(api_key).strip().lower() not in {"", "local"}:
        raise RuntimeError(
            f"Local SA backend is unreachable at {host}:{port}. "
            "Auto-manage is only supported for loopback Ollama endpoint (:11434) with local API key."
        )

    global _LOCAL_SA_RUNTIME_PROC
    if _LOCAL_SA_RUNTIME_PROC is None or _LOCAL_SA_RUNTIME_PROC.poll() is not None:
        try:
            _LOCAL_SA_RUNTIME_PROC = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            atexit.register(_stop_local_sa_runtime)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "LLM_WORLD_SA_BACKEND=local is enabled, but 'ollama' is not installed. "
                "Install Ollama, or switch LLM_WORLD_SA_BACKEND=deepseek."
            ) from exc

    deadline = time.time() + 20.0
    while time.time() < deadline:
        if _tcp_open(host, port):
            return
        if _LOCAL_SA_RUNTIME_PROC is not None and _LOCAL_SA_RUNTIME_PROC.poll() is not None:
            break
        time.sleep(0.25)

    raise RuntimeError(
        f"Failed to connect to local SA backend at {host}:{port} after auto-start. "
        "Verify Ollama is healthy and the model is available."
    )
    try:
        v = int(raw)
        return max(1, v)
    except Exception:
        return int(_SA_MAX_TEXT_VIOLATIONS_BEFORE_STOP)


def _coerce_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    """Return LangChain-style tool calls with safe defaults.

    Ensures every call has:
    - `name` as non-empty string
    - `args` as dict (never None)
    - `id` as string (empty allowed)
    """

    if not isinstance(tool_calls, list):
        return []

    out: List[Dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue

        name = str(tc.get("name") or "").strip()
        if not name and isinstance(tc.get("function"), dict):
            name = str((tc.get("function") or {}).get("name") or "").strip()
        if not name:
            continue

        args = tc.get("args")
        if not isinstance(args, dict):
            args = None
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            raw_args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    args = None
        if not isinstance(args, dict):
            args = {}

        tc_id = str(tc.get("id") or tc.get("tool_call_id") or "")
        out.append({"name": name, "args": args, "id": tc_id, "type": "tool_call"})

    return out


def _sanitize_messages_for_invoke(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Normalize message sequence before provider call.

    Fixes provider-strict schema issues such as:
    - assistant content=None
    - assistant tool_calls[].args=None
    - orphan tool messages (missing/unknown tool_call_id)
    """

    out: List[BaseMessage] = []
    open_tool_ids: set[str] = set()

    for m in list(messages or []):
        t = str(getattr(m, "type", "") or "").strip().lower()

        if t in {"ai", "assistant"}:
            content = str(getattr(m, "content", "") or "")
            raw_calls = getattr(m, "tool_calls", None)
            if not raw_calls:
                ak = getattr(m, "additional_kwargs", None) or {}
                if isinstance(ak, dict):
                    raw_calls = ak.get("tool_calls")
            safe_calls = _coerce_tool_calls(raw_calls)
            if safe_calls:
                # Only keep calls that have a non-empty ID; calls without an ID
                # can never be matched to a ToolMessage and will cause a 400 error.
                id_calls = [tc for tc in safe_calls if str(tc.get("id") or "").strip()]
                if id_calls:
                    for tc in id_calls:
                        open_tool_ids.add(str(tc.get("id") or "").strip())
                    out.append(AIMessage(content=content, tool_calls=id_calls))
                else:
                    # All calls lacked IDs — treat message as plain text only
                    out.append(AIMessage(content=content))
            else:
                out.append(AIMessage(content=content))
            continue

        if t == "tool":
            tcid = str(getattr(m, "tool_call_id", "") or "").strip()
            if not tcid or tcid not in open_tool_ids:
                continue
            content = str(getattr(m, "content", "") or "")
            name = getattr(m, "name", None)
            out.append(ToolMessage(content=content, tool_call_id=tcid, name=(str(name) if name is not None else None)))
            open_tool_ids.discard(tcid)
            continue

        if t in {"human", "user"}:
            out.append(HumanMessage(content=str(getattr(m, "content", "") or "")))
            continue

        if t == "system":
            out.append(SystemMessage(content=str(getattr(m, "content", "") or "")))
            continue

        out.append(m)

    # Post-fix: if any tool_call IDs are still open (AI message had tool_calls but
    # no matching tool response was found), strip those tool_calls from the AI message
    # to avoid the "tool_calls not followed by tool messages" provider error.
    if open_tool_ids:
        fixed: List[BaseMessage] = []
        for m in out:
            t = str(getattr(m, "type", "") or "").strip().lower()
            if t in {"ai", "assistant"}:
                tc = getattr(m, "tool_calls", None) or []
                if any(str(c.get("id") or "").strip() in open_tool_ids for c in tc):
                    content = str(getattr(m, "content", "") or "").strip()
                    if content:
                        fixed.append(AIMessage(content=content))
                    # else: drop the empty AI+tool_calls message entirely
                    continue
            fixed.append(m)
        return fixed

    return out


def _custom_react_loop(input_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Custom ReAct loop that stops immediately when context changes.
    
    This replaces create_react_agent to give us control over when to stop.
    The loop checks is_context_changed() after EACH tool execution.
    """
    from openrouter_langchain_logging import logs_enabled
    
    messages = _sanitize_messages_for_invoke(list(input_dict.get("messages") or []))
    llm_with_tools = input_dict["llm_with_tools"]
    tools_by_name = input_dict["tools_by_name"]
    
    max_iterations = 50  # Safety limit
    iteration = 0
    consecutive_text_violations = 0  # Track consecutive over-limit text-only outputs
    consecutive_readonly_iterations = 0  # Track read-only tool loops
    _READONLY_TOOLS = {"get_location", "get_npc", "get_character_detail", "read_character_diary"}
    max_text_words = _sa_max_text_words()
    max_text_violations = _sa_max_text_violations_before_stop()
    
    while iteration < max_iterations:
        iteration += 1
        
        # Call LLM with current messages
        try:
            response = llm_with_tools.invoke(_sanitize_messages_for_invoke(messages))
        except Exception as e:
            if logs_enabled():
                print(f"[trace] custom_react_loop: LLM error: {e}")
            # For provider/transport errors, return current state without adding
            # assistant error messages into conversational history.
            error_str = str(e).lower()
            # Remove any orphaned ToolMessages from this iteration to avoid invalid
            # message sequences when aborting before adding the AI response.
            cleaned_messages = []
            last_ai_with_tools_ids = set()
            for msg in messages:
                msg_type = getattr(msg, "type", "")
                if msg_type in {"ai", "assistant"}:
                    tool_calls = getattr(msg, "tool_calls", None) or []
                    last_ai_with_tools_ids = {tc.get("id", "") for tc in tool_calls if tc.get("id")}
                    cleaned_messages.append(msg)
                elif msg_type == "tool":
                    tool_call_id = getattr(msg, "tool_call_id", "")
                    if tool_call_id in last_ai_with_tools_ids:
                        cleaned_messages.append(msg)
                        last_ai_with_tools_ids.discard(tool_call_id)
                else:
                    cleaned_messages.append(msg)

            if any(
                x in error_str
                for x in [
                    "provider",
                    "upstream",
                    "400",
                    "500",
                    "502",
                    "503",
                    "504",
                    "connection error",
                    "connect",
                    "timeout",
                    "timed out",
                    "refused",
                    "unreachable",
                ]
            ):
                if logs_enabled():
                    print("[trace] custom_react_loop: Provider/transport error detected, returning for retry")
                return {"messages": cleaned_messages}

            if logs_enabled():
                print("[trace] custom_react_loop: Non-provider LLM error detected, returning without polluting history")
            return {"messages": cleaned_messages}
        
        # --- Word count guard ---
        # SA must communicate via tool calls, not verbose text.
        content_text = str(getattr(response, "content", "") or "").strip()
        word_count = len(content_text.split()) if content_text else 0
        tool_calls = _coerce_tool_calls(getattr(response, "tool_calls", None) or [])
        
        if max_text_words > 0 and word_count > max_text_words:
            if not tool_calls:
                # Text-only response that's too long — reject it entirely
                consecutive_text_violations += 1
                if logs_enabled():
                    print(f"[trace] custom_react_loop: Text-only response too long "
                          f"({word_count} words > {max_text_words}), "
                          f"violation {consecutive_text_violations}/{max_text_violations}")
                
                if consecutive_text_violations >= max_text_violations:
                    if logs_enabled():
                        print(f"[trace] custom_react_loop: Too many text violations, force-stopping")
                    messages.append(response)  # Keep last response for history
                    break
                
                # Don't add the verbose response; inject a corrective message instead
                messages.append(AIMessage(content="(text output suppressed — too verbose)"))
                messages.append(HumanMessage(content=(
                    f"ERROR: Your last output was {word_count} words of plain text. "
                    f"Maximum allowed is {max_text_words} words. "
                    "You must NOT write explanatory text, summaries, or commentary. "
                    "Communicate ONLY via tool calls. Call exactly one tool now."
                )))
                continue  # Retry the loop
            else:
                # Has tool calls but also verbose text — strip the text, keep the tools
                if logs_enabled():
                    print(f"[trace] custom_react_loop: Stripping verbose text "
                          f"({word_count} words) from tool-call response")
                response = AIMessage(content="", tool_calls=tool_calls)
                consecutive_text_violations = 0
        else:
            consecutive_text_violations = 0

        # Ensure the appended AI message always has provider-safe shapes.
        # IMPORTANT: Strip text content from tool-call responses to prevent
        # the model from echoing confirmations as its own text output.
        if tool_calls:
            response = AIMessage(content="", tool_calls=tool_calls)
        else:
            response = AIMessage(content=str(getattr(response, "content", "") or ""))
        
        # Add AI response to messages
        messages.append(response)
        
        # Check if there are tool calls
        if not tool_calls:
            # No tool calls — the SA is done.  Strip any text content since
            # the SA must communicate exclusively via tool calls; any trailing
            # text is useless self-narration (or potential garbage tokens) that
            # pollutes the conversation history.
            if content_text:
                if logs_enabled():
                    print(f"[trace] custom_react_loop: Stripping final text-only output "
                          f"({word_count} words) — SA should not produce text")
                messages[-1] = AIMessage(content="")
            break
        
        # Execute each tool call and collect all ToolMessages first before appending
        # any confirmation.  All ToolMessages for a single AIMessage batch MUST be
        # contiguous — inserting an AIMessage between them breaks the OpenAI protocol
        # and causes the model to see an invalid message sequence on the next invoke.
        write_confirmations: list[str] = []
        early_return: Optional[dict] = None
        for tool_call in tool_calls:
            tool_name = tool_call.get("name", "")
            tool_args = tool_call.get("args", {})
            tool_id = tool_call.get("id", "")
            
            if logs_enabled():
                print(f"[trace] custom_react_loop: Calling tool {tool_name}")
            
            # Find and execute the tool
            tool_func = tools_by_name.get(tool_name)
            if not tool_func:
                result = f"Error: Tool '{tool_name}' not found"
                if logs_enabled():
                    print(f"[trace] custom_react_loop: Tool not found: {tool_name}")
            else:
                try:
                    result = tool_func.invoke(tool_args)
                except Exception as e:
                    result = f"Error executing {tool_name}: {e}"
                    if logs_enabled():
                        print(f"[trace] custom_react_loop: Tool error: {e}")
            
            # Add tool result to messages — keep all ToolMessages contiguous.
            messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tool_id,
                name=tool_name,
            ))

            # Collect a one-line write confirmation (deferred until after all ToolMessages).
            try:
                if tool_name not in _READONLY_TOOLS:
                    write_confirmations.append(
                        _format_sa_write_confirmation(tool_name=tool_name, tool_args=tool_args)
                    )
            except Exception:
                pass

            # Check early-exit conditions; note them but finish appending ToolMessages
            # for all remaining calls before acting on them so the message sequence stays valid.
            if is_turn_locked() or is_context_changed():
                early_return = {"messages": messages}
                # Continue iterating to flush remaining tool calls before returning.

        # After all ToolMessages are appended, emit a single aggregated confirmation
        # AIMessage for any write tools that ran.  One AIMessage per batch keeps the
        # conversation history clean and avoids protocol violations.
        if write_confirmations:
            conf_text = "\n".join(write_confirmations)
            messages.append(AIMessage(content=conf_text))

        if early_return is not None:
            if is_turn_locked() and logs_enabled():
                print(f"[trace] custom_react_loop: Turn finalized, stopping")
            elif logs_enabled():
                print(f"[trace] custom_react_loop: Context changed, stopping")
            return {"messages": messages}
        
        # Detect read-only tool loops: if several consecutive iterations called
        # only read-only tools, the SA has no useful work and is aimlessly looping.
        all_readonly = all(
            tool_call.get("name", "") in _READONLY_TOOLS
            for tool_call in tool_calls
        )
        if all_readonly:
            consecutive_readonly_iterations += 1
            if consecutive_readonly_iterations >= 3:
                if logs_enabled():
                    print(f"[trace] custom_react_loop: {consecutive_readonly_iterations} consecutive "
                          f"read-only iterations; stopping to avoid aimless loop")
                break
        else:
            consecutive_readonly_iterations = 0

        # After all tools executed, check if turn finalized or context changed before continuing loop
        if is_turn_locked():
            if logs_enabled():
                print(f"[trace] custom_react_loop: Turn finalized, stopping")
            break
        
        if is_context_changed():
            if logs_enabled():
                print(f"[trace] custom_react_loop: Context changed, stopping")
            break
    
    if iteration >= max_iterations and logs_enabled():
        print(f"[trace] custom_react_loop: Hit max iterations ({max_iterations})")
    
    return {"messages": messages}


class StorageAssistantFactory:
    """Builds a fresh storage assistant ReAct agent with a context-specific tool list."""

    def __init__(self) -> None:
        load_dotenv(override=True)
        initialize_game_dir()

        self._prompt_text = read_prompt_text("agents/storage_assistant/prompt.txt")
        self._temperature = 0.7
        backend_raw = (os.getenv("LLM_WORLD_SA_BACKEND") or "deepseek").strip().lower()
        if backend_raw in {"local"}:
            self._sa_backend = "local"
        elif backend_raw in {"openrouter", "deepseek", "remote", "deepseek_api"}:
            self._sa_backend = "deepseek"
        else:
            self._sa_backend = "deepseek"
            if logs_enabled():
                print(
                    f"[trace] unknown LLM_WORLD_SA_BACKEND={backend_raw!r}; "
                    "falling back to deepseek"
                )

        self._sa_use_local = _env_flag("LLM_WORLD_SA_USE_LOCAL", default=False) or (self._sa_backend == "local")

        if self._sa_use_local:
            # Local OpenAI-compatible backend (example: Ollama/vLLM/LM Studio).
            self._model = (
                os.getenv("LLM_WORLD_SA_LOCAL_MODEL")
                or os.getenv("LLM_WORLD_SA_MODEL")
                or "qwen2.5:14b-instruct"
            )
            self._base_url = (
                os.getenv("LLM_WORLD_SA_LOCAL_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or "http://127.0.0.1:11434/v1"
            )
            self._api_key = (
                os.getenv("LLM_WORLD_SA_LOCAL_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or "local"
            )
            _ensure_local_sa_runtime(base_url=self._base_url, api_key=self._api_key)
        else:
            self._model = (
                os.getenv("DEEPSEEK_MODEL_SA")
                or os.getenv("OPENROUTER_MODEL_SA")
                or os.getenv("DEEPSEEK_STORAGE_ASSISTANT_MODEL")
                or os.getenv("OPENROUTER_STORAGE_ASSISTANT_MODEL")
                or "deepseek-v4-flash"
            )
            self._base_url = None
            self._api_key = None

    def build(self, *, tools: Optional[List[Any]] = None):
        # Default to the context-filtered tool set; callers can explicitly pass
        # GM_TOOLS to expose the full set.
        tools_list = tools if tools is not None else gm_tools_for_current_context()

        # Output budgeting:
        # - Keep typical GM outputs short to reduce rambling and cost.
        # - Allow a bit more headroom when the scene is ready to finalize and
        #   the only legal tool is gm_output_turn (narration + state updates).
        gm_max_tokens_default = int(os.getenv("LLM_WORLD_GM_MAX_TOKENS", "1000") or "1000")
        gm_max_tokens_finalize = int(
            os.getenv("LLM_WORLD_GM_MAX_TOKENS_FINALIZE", "1000") or "1000"
        )

        tool_names = set()
        for t in tools_list:
            try:
                name = getattr(t, "name", None) or getattr(t, "__name__", None)
                if name:
                    tool_names.add(str(name))
            except Exception:
                continue

        max_tokens = gm_max_tokens_default
        if tool_names == {"gm_output_turn"}:
            max_tokens = gm_max_tokens_finalize

        # Important: use a fresh LLM instance per build so tool bindings cannot
        # accidentally persist across invocations.
        #
        # LLM_WORLD_SA_THINKING=1 opts the SA into DeepSeek thinking mode.
        # The SA uses free bind_tools (no tool_choice), so thinking mode is safe here.
        # All other agents force tool_choice and must stay in non-thinking mode.
        sa_thinking_env = (os.getenv("LLM_WORLD_SA_THINKING") or "").strip().lower()
        sa_thinking: Optional[bool] = True if sa_thinking_env in {"1", "true", "yes"} else None

        llm = build_openrouter_chat_llm(
            temperature=float(self._temperature),
            model=self._model,
            base_url=self._base_url,
            api_key=self._api_key,
            include_headers=(False if self._sa_use_local else None),
            streaming=True,
            max_tokens=max_tokens,
            title_suffix=f"-storage-assistant-{max_tokens}t",
            parallel_tool_calls=False,
            thinking=sa_thinking,
        )

        # Bind tools and system prompt to LLM (parallel_tool_calls already in model_kwargs)
        llm_with_tools = llm.bind_tools(tools_list)
        
        # Create tools lookup dict for custom react loop
        tools_by_name = {}
        for tool in tools_list:
            try:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
                if name:
                    tools_by_name[str(name)] = tool
            except Exception:
                continue
        
        # Build custom ReAct graph using our loop that stops on context change
        def _react_with_context_check(input_dict: dict) -> dict:
            # Inject system prompt as first message if not present
            msgs = list(input_dict.get("messages") or [])
            if self._prompt_text:
                prompt_text = self._prompt_text
                has_prompt = any(
                    isinstance(m, SystemMessage) and prompt_text in (m.content or "")
                    for m in msgs
                )
                if not has_prompt:
                    msgs = [SystemMessage(content=prompt_text)] + msgs
            
            # Run custom react loop with tools and LLM
            return _custom_react_loop({
                "messages": msgs,
                "llm_with_tools": llm_with_tools,
                "tools_by_name": tools_by_name,
            })
        
        graph = RunnableLambda(_react_with_context_check)

        graph = graph.with_config(
            {
                "callbacks": openrouter_logging_callbacks(
                    scope="storage_assistant",
                    label="storage_assistant",
                )
            }
        )

        return graph


def build_storage_assistant_graph():
    """Helper: builds a storage assistant with context-filtered tools."""

    return StorageAssistantFactory().build()
