"""Shared summarisation runner — single free-text JSON pattern for all agents.

Every agent follows the same flow:
  1. Build a task prompt that asks the LLM for ``{"summary": "..."}``
     (optionally also ``"name": "..."`` for paragraph/arc titles).
  2. Call ``runner.run_summary(task_prompt, ...)``.
  3. Get back ``(name_or_None, summary)``.
  4. Persist wherever the agent needs (history, story.json, diary.json).

Usage::

    runner = SummaryRunner(meta, history_path, prompt_text, scope="scene_manager")

    result = runner.run_summary(
        task_prompt="Your instruction...\\nRespond with JSON: ...",
        extra_pinned=pinned_msgs,
        label="sm_summary",
    )
    if result:
        name, summary = result  # name is None for summary-only (e.g. diary)
        # persist ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from memory_store import load_history
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks
from engine.history_meta import HistoryMeta


class SummaryRunner:
    """Shared helper for agent summarization — single ``run_summary`` method.

    Args:
        meta: HistoryMeta instance for tracking summarization state.
        history_path: Path to the agent's message history JSON.
        prompt_text: Agent system prompt text.
        temperature: LLM temperature (default 0.7).
        scope: Logging scope label (e.g. ``"game_master"``, ``"scene_manager"``).
    """

    def __init__(
        self,
        meta: Optional[HistoryMeta],
        history_path: Path,
        prompt_text: str,
        temperature: float = 0.7,
        scope: str = "game_master",
    ) -> None:
        self._meta = meta
        self._history_path = history_path
        self._prompt_text = prompt_text
        self._temperature = temperature
        self._scope = scope

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_task_prompt(
        self,
        template: str,
        last_ref: str = "",
    ) -> str:
        """Fill *template* with ``{last_ref}`` and ``{refs}`` (interaction list).

        If no meta is configured, ``{refs}`` is replaced with ``"(none)"``.
        """
        refs = "(none)"
        if self._meta is not None:
            last_idx = self._meta.load().get("last_summarized_at_idx", -1)
            interactions = self._meta.get_real_interactions_since(last_idx)
            if interactions:
                refs = "\n".join(
                    f"  [{e['idx']}] {e['label']}" for e in interactions[-20:]
                )
        return template.format(last_ref=last_ref or "(none)", refs=refs)

    def run_summary(
        self,
        *,
        task_prompt: str,
        title_suffix: str = "-summary",
        max_tokens: int = 1500,
        label: str = "summary",
        extra_pinned: Optional[List[SystemMessage]] = None,
        extra_history: Optional[List[BaseMessage]] = None,
    ) -> Optional[Tuple[Optional[str], str]]:
        """Run a free-text summarization.

        Expects the LLM to return JSON with at least ``"summary"``.
        ``"name"`` is optional (used for paragraph/arc titles).

        Returns ``(name_or_None, summary_text)`` or ``None`` on failure.

        Args:
            task_prompt: The instruction to send to the LLM.
            title_suffix: Suffix for the LLM title (for logging).
            max_tokens: Max tokens for the LLM response.
            label: Logging label.
            extra_pinned: Extra SystemMessages to inject (e.g. pinned summaries).
            extra_history: Extra messages appended after history (for retry corrections).
        """
        llm = self._build_llm(title_suffix, max_tokens)
        messages = self._build_messages(task_prompt, extra_pinned, extra_history)

        callbacks = openrouter_logging_callbacks(scope=self._scope, label=label)
        try:
            out = llm.invoke(messages, config={"callbacks": callbacks})
        except Exception:
            return None

        raw = str(getattr(out, "content", "") or "").strip()
        if not raw:
            return None

        # Try JSON parse — expect {"summary": "...", "name": "..."} (name optional)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                name = str(parsed.get("name") or "").strip() or None
                summary = str(parsed.get("summary") or "").strip()
                if summary:
                    return (name, summary)
        except Exception:
            pass

        # Fallback: treat the whole response as summary-only
        return (None, raw)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_llm(self, title_suffix: str, max_tokens: int):
        return build_openrouter_chat_llm(
            temperature=float(self._temperature),
            streaming=True,
            title_suffix=title_suffix,
            max_tokens=max_tokens,
            parallel_tool_calls=False,
        )

    def _load_history_msgs(self) -> List[BaseMessage]:
        msgs: List[BaseMessage] = []
        if self._history_path and self._history_path.exists():
            history = load_history(self._history_path)
            for h in history:
                role = (h.get("role") or "").strip().lower()
                content = str(h.get("content") or "")
                if role == "user":
                    msgs.append(HumanMessage(content=content))
                elif role == "assistant":
                    msgs.append(AIMessage(content=content))
        return msgs

    def _build_messages(
        self,
        task_prompt: str,
        extra_pinned: Optional[List[SystemMessage]] = None,
        extra_history: Optional[List[BaseMessage]] = None,
    ) -> List[BaseMessage]:
        msgs: List[BaseMessage] = [
            SystemMessage(content=self._prompt_text),
        ]
        if extra_pinned:
            msgs.extend(extra_pinned)
        msgs.extend(self._load_history_msgs())
        if extra_history:
            msgs.extend(extra_history)
        msgs.append(HumanMessage(content=task_prompt))
        return msgs
