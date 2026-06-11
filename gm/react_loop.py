"""Shared ReAct loop utilities for tool-based agents.

Extracted from gm/operator.py for reuse by both Storage Assistant and Game Master.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def coerce_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
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


def sanitize_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Normalize message sequence before provider call.

    Fixes provider-strict schema issues: orphan tool messages,
    assistant content=None, tool_calls[].args=None.
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
            safe_calls = coerce_tool_calls(raw_calls)
            if safe_calls:
                id_calls = [tc for tc in safe_calls if str(tc.get("id") or "").strip()]
                if id_calls:
                    for tc in id_calls:
                        open_tool_ids.add(str(tc.get("id") or "").strip())
                    out.append(AIMessage(content=content, tool_calls=id_calls))
                else:
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
                    continue
            fixed.append(m)
        return fixed

    return out


def _replace_pinned_messages(
    messages: List[BaseMessage],
    fresh_pinned: List[SystemMessage],
) -> None:
    """Replace existing pinned SystemMessages with fresh ones in-place.

    Matches by content prefix (e.g. "[storage_notice]"). Any SystemMessage
    whose content starts with the same prefix as a fresh message gets replaced.
    Fresh messages without a match are appended.
    """
    for fresh in fresh_pinned:
        fresh_content = str(getattr(fresh, "content", "") or "")
        fresh_prefix = fresh_content.split("\n")[0] if fresh_content else ""
        if not fresh_prefix:
            continue
        replaced = False
        for i, existing in enumerate(messages):
            if not isinstance(existing, SystemMessage):
                continue
            existing_content = str(getattr(existing, "content", "") or "")
            if existing_content.startswith(fresh_prefix):
                messages[i] = fresh
                replaced = True
                break
        if not replaced:
            messages.append(fresh)


def react_loop_iteration(
    messages: List[BaseMessage],
    llm_with_tools: Any,
    tools_by_name: Dict[str, Any],
    *,
    termination_tools: set[str],
    readonly_tools: set[str],
    max_iterations: int = 50,
    pinned_refresh_fn: Optional[Callable[[], List[SystemMessage]]] = None,
) -> dict:
    """Run a ReAct loop that stops on termination tools or context changes.

    Args:
        messages: Current message history (modified in place).
        llm_with_tools: LLM with bound tools.
        tools_by_name: Dict mapping tool name to tool callable.
        termination_tools: Set of tool names that end the loop when called,
            e.g. {"ready_to_proceed", "answer_lore_question", "gm_summary_result"}.
        readonly_tools: Set of tool names that are read-only (no mutation).
        max_iterations: Safety limit.
        pinned_refresh_fn: Optional callable() -> List[SystemMessage] that rebuilds
            pinned messages (e.g. storage notice) after each iteration. Called only
            when at least one non-readonly tool was executed. Returned messages
            replace any existing SystemMessages with matching content prefixes.

    Returns:
        dict with keys: "messages" (updated history), "exit_tool" (name of termination
        tool called) or None, "exit_args" (args of termination tool) or None.
    """
    from openrouter_langchain_logging import logs_enabled

    iteration = 0
    consecutive_readonly = 0

    while iteration < max_iterations:
        iteration += 1

        try:
            response = llm_with_tools.invoke(sanitize_messages(messages))
        except Exception as e:
            if logs_enabled():
                print(f"[trace] react_loop: LLM error: {e}")
            break

        content_text = str(getattr(response, "content", "") or "").strip()
        tool_calls = coerce_tool_calls(getattr(response, "tool_calls", None) or [])

        # Capture thinking: text output is the agent's reasoning
        msg = AIMessage(content=content_text)
        if tool_calls:
            # tool_calls must be carried forward so sanitize_messages
            # can match ToolMessage IDs and preserve tool results.
            msg.tool_calls = [
                {"id": tc.get("id", ""), "name": tc.get("name", ""),
                 "args": tc.get("args", {}), "type": "tool_call"}
                for tc in tool_calls
            ]
        messages.append(msg)

        if not tool_calls:
            # No tool calls → agent has nothing to say; stop
            break

        # Execute tools
        write_confirmations: List[str] = []
        exit_tool = None
        exit_args = None

        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            tc_id = tc.get("id", "")

            if logs_enabled():
                print(f"[trace] react_loop: calling {name}")

            tool_fn = tools_by_name.get(name)
            if not tool_fn:
                result = f"Error: Tool '{name}' not found"
            else:
                try:
                    result = tool_fn.invoke(args)
                except Exception as e:
                    result = f"Error: {e}"

            messages.append(ToolMessage(content=str(result), tool_call_id=tc_id, name=name))

            if name in termination_tools:
                exit_tool = name
                exit_args = args

            if name not in readonly_tools and name not in termination_tools:
                write_confirmations.append(f"[tool:{name}] wrote data")

        # Write confirmation for non-termination mutations
        if write_confirmations:
            messages.append(AIMessage(content="\n".join(write_confirmations)))

        # Refresh pinned messages after mutation (e.g. storage notice)
        if write_confirmations and pinned_refresh_fn:
            try:
                fresh_pinned = pinned_refresh_fn()
                if fresh_pinned:
                    _replace_pinned_messages(messages, fresh_pinned)
            except Exception:
                pass

        if exit_tool:
            return {"messages": messages, "exit_tool": exit_tool, "exit_args": exit_args}

        # Read-only loop detection
        all_readonly = all(tc.get("name", "") in readonly_tools for tc in tool_calls)
        if all_readonly:
            consecutive_readonly += 1
            if consecutive_readonly >= 8:
                if logs_enabled():
                    print(f"[trace] react_loop: 3 consecutive read-only iterations, stopping")
                break
        else:
            consecutive_readonly = 0

    return {"messages": messages, "exit_tool": None, "exit_args": None}
