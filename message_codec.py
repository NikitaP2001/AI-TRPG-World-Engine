from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


def _normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    """Normalize tool call payloads to OpenAI-compatible schema.

    We historically persisted LangChain-style tool call dicts like:
      {"name": "create_location", "args": {...}, "id": "...", "type": "tool_call"}

    Some providers (notably Gemini via OpenRouter) reject this when replayed in history,
    expecting:
      {"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}
    """

    if not isinstance(tool_calls, list):
        return []

    out: List[Dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue

        # Already OpenAI-style.
        if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
            fn = dict(tc.get("function") or {})
            if "arguments" in fn and not isinstance(fn.get("arguments"), str):
                try:
                    fn["arguments"] = json.dumps(fn.get("arguments"), ensure_ascii=False)
                except Exception:
                    fn["arguments"] = str(fn.get("arguments"))
            out.append({**tc, "type": "function", "function": fn})
            continue

        # Legacy LangChain-style.
        name = tc.get("name")
        args = tc.get("args")
        tc_id = tc.get("id") or tc.get("tool_call_id") or ""

        if isinstance(name, str) and name.strip() and isinstance(args, dict):
            try:
                arg_str = json.dumps(args, ensure_ascii=False)
            except Exception:
                arg_str = "{}"
            out.append(
                {
                    "id": str(tc_id),
                    "type": "function",
                    "function": {"name": name.strip(), "arguments": arg_str},
                }
            )
            continue

        # Best-effort: if it at least has a function object, coerce type.
        if isinstance(tc.get("function"), dict):
            fn2 = dict(tc.get("function") or {})
            if "arguments" in fn2 and not isinstance(fn2.get("arguments"), str):
                try:
                    fn2["arguments"] = json.dumps(fn2.get("arguments"), ensure_ascii=False)
                except Exception:
                    fn2["arguments"] = str(fn2.get("arguments"))
            tc2 = {**tc, "type": "function", "function": fn2}
            if "id" not in tc2:
                tc2["id"] = str(tc_id)
            out.append(tc2)

    return out


def message_to_dict(m: BaseMessage) -> Dict[str, Any]:
    t = getattr(m, "type", "") or m.__class__.__name__.lower()
    d: Dict[str, Any] = {
        "type": t,
        "content": getattr(m, "content", "") or "",
    }

    # Tool messages
    if t == "tool":
        tool_call_id = getattr(m, "tool_call_id", None)
        if tool_call_id:
            d["tool_call_id"] = tool_call_id
        name = getattr(m, "name", None)
        if name:
            d["name"] = name
        return d

    # AI messages: keep tool_calls if present (helps replay the exact interaction)
    if t in {"ai", "assistant"}:
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = _normalize_tool_calls(tool_calls)
        else:
            ak = getattr(m, "additional_kwargs", None) or {}
            if isinstance(ak, dict) and ak.get("tool_calls"):
                d["tool_calls"] = _normalize_tool_calls(ak.get("tool_calls"))
        return d

    return d


def dict_to_message(d: Dict[str, Any]) -> Optional[BaseMessage]:
    # Accept both schemas:
    # - full-history schema: {"type": "human|ai|tool", ...}
    # - memory-store schema: {"role": "user|assistant", ...}
    t = str(d.get("type") or d.get("role") or "").strip().lower()
    content = str(d.get("content") or "")
    if content.startswith("[gm_thoughts]"):
        content = "[world_facts]" + content[len("[gm_thoughts]"):]

    if t in {"human", "user"}:
        return HumanMessage(content=content)

    if t in {"ai", "assistant"}:
        tool_calls = d.get("tool_calls")
        if tool_calls:
            # Be conservative across langchain versions: store tool_calls in additional_kwargs.
            return AIMessage(content=content, additional_kwargs={"tool_calls": _normalize_tool_calls(tool_calls)})
        return AIMessage(content=content)

    if t == "tool":
        tool_call_id = str(d.get("tool_call_id") or "")
        name = d.get("name")
        if name is not None:
            name = str(name)
        if tool_call_id:
            return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)
        # If tool_call_id is missing, still keep content.
        return ToolMessage(content=content, tool_call_id="", name=name)

    # Unknown message types are ignored.
    return None


def messages_to_dicts(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
    return [message_to_dict(m) for m in messages]


def dicts_to_messages(items: List[Dict[str, Any]]) -> List[BaseMessage]:
    out: List[BaseMessage] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        m = dict_to_message(it)
        if m is not None:
            out.append(m)
    return out
