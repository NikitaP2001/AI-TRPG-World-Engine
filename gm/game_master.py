"""Game Master agent: Tool-based ReAct agent.

Called on-demand via call_gm(). Uses tools to manage the world.
No longer called every turn — scenes auto-advance.

Companion agents:
  1. Storage Assistant (gm/operator.py) — bookkeeper ReAct agent
  2. Scene Manager (scene_manager/core.py) — scene lifecycle
  3. Game Master (this file) — world management, on-demand
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from memory_store import append_message, load_history, limits_from_env
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world import World, WorldTime
from gm.react_loop import react_loop_iteration
from engine.history_meta import HistoryMeta as GMHistoryMeta
from engine.summarizer import SummaryRunner

_WORLD = World()


# ---------------------------------------------------------------------------
# GM tools (termination + utility)
# ---------------------------------------------------------------------------


@tool
def ready_to_proceed() -> str:
    """Call this when you have finished your planing. Control passes forward.

    This is a termination tool — calling it ends the current invocation.
    """
    try:
        info = _WORLD.get_info()
        chars = info.get("characters") if isinstance(info, dict) else []
        for ch in chars:
            if not isinstance(ch, dict):
                continue
            pname = str(ch.get("name") or "").strip()
            if not pname:
                continue
            missing = []
            if not _WORLD.get_character_state(pname):
                missing.append("state")
            if not _WORLD.get_character_skills(pname):
                missing.append("skills")
            if not _WORLD.get_character_equipment(pname):
                missing.append("equipment")
            if missing:
                tools_hint = ", ".join(f"update_character_{m}(json_pointer='/', ...)" for m in missing)
                return (
                    f"ERROR: Character '{pname}' storage not initialized: {', '.join(missing)}. "
                    f"Call {tools_hint} with complete data first."
                )
    except Exception:
        pass
    return "ok"


@tool
def answer_lore_question(content: str) -> str:
    """Answer a question or provide lore information.

    If neede plan everything and setup before answering.
    Use this to respond to specific questions from the caller.
    This is a termination tool — calling it ends the current invocation.

    Args:
        content: Your answer or information to provide.
    """
    try:
        info = _WORLD.get_info()
        chars = info.get("characters") if isinstance(info, dict) else []
        for ch in chars:
            if not isinstance(ch, dict):
                continue
            pname = str(ch.get("name") or "").strip()
            if not pname:
                continue
            missing = []
            if not _WORLD.get_character_state(pname):
                missing.append("state")
            if not _WORLD.get_character_skills(pname):
                missing.append("skills")
            if not _WORLD.get_character_equipment(pname):
                missing.append("equipment")
            if missing:
                tools_hint = ", ".join(f"update_character_{m}(json_pointer='/', ...)" for m in missing)
                return (
                    f"ERROR: Character '{pname}' storage not initialized: {', '.join(missing)}. "
                    f"Call {tools_hint} with complete data first."
                )
    except Exception:
        pass
    return "ok"


# ---------------------------------------------------------------------------
# GameMaster class
# ---------------------------------------------------------------------------


@tool
def gm_summary_result(paragraph_name: str, summary: str) -> str:
    """Submit a story summary paragraph. This is a termination tool.

    Args:
        paragraph_name: Short title (3-10 words), unique.
        summary: 5-10 sentence summary of the events covered.
    """
    return "ok"


def parse_game_master_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON parser for GM outputs.

    Accepts raw JSON, fenced json blocks, or the first outermost object.
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    fence_start = raw.find("```json")
    if fence_start != -1:
        fence_end = raw.find("```", fence_start + 7)
        if fence_end != -1:
            inner = raw[fence_start + 7:fence_end].strip()
            try:
                obj = json.loads(inner)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
    brace_start = raw.find("{")
    if brace_start != -1:
        brace_end = raw.rfind("}")
        if brace_end > brace_start:
            inner = raw[brace_start:brace_end + 1]
            try:
                obj = json.loads(inner)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
    return {}


class GameMaster:
    """Game Master agent: ReAct-based world manager. Called on-demand via call_gm()."""

    def __init__(self, *, prompt_path: str = "agents/game_master/prompt.txt", history_path: Optional[Path] = None) -> None:
        self._prompt_text = read_prompt_text(prompt_path)
        self._temperature = 0.7
        self._history_path = history_path
        self._meta = GMHistoryMeta(
            (self._history_path.parent / "gm_history_meta.json")
            if self._history_path else Path("gm_history_meta.json"),
        )
    def inject_delta(self, content: str) -> None:
        """Inject a standalone world-state delta message into GM history."""
        if not content or not content.strip():
            return
        if not self._history_path:
            return
        limits = limits_from_env()
        append_message(self._history_path, role="user", content=content.strip(), limits=limits)
        self._meta.append_entry("auto_injection", content.strip()[:60])

    def call_gm(
        self,
        *,
        notice: str,
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        llm: Optional[ChatOpenAI] = None,
    ) -> Dict[str, Any]:
        """Invoke GM with a notice via ReAct loop. GM uses tools until termination.

        Args:
            notice: Text explaining why GM was called.
            context: Optional dict with world_time, players list, etc.
            tools: Override tool list (defaults to all GM tools).
            llm: Optional LLM instance.

        Returns:
            Dict with: "exit_tool" (str|None), "exit_args" (dict|None), "thinking" (str).
        """
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-game-master",
                max_tokens=2000,
                parallel_tool_calls=False,
            )

        from gm.tools import (
            reset_read_tracker,
            create_location, get_location, update_location,
            delete_location_path, delete_location,
            create_npc, get_npc, update_npc, delete_npc_path, delete_npc,
            get_character_detail, read_character_diary,
            read_character_diary,
            update_character_state, update_character_skills, update_character_equipment,
            get_character_state, get_character_skills, get_character_equipment,
        )

        all_tools = tools or [
            ready_to_proceed,
            answer_lore_question,
            create_location, get_location, update_location,
            delete_location_path, delete_location,
            create_npc, get_npc, update_npc, delete_npc_path, delete_npc,
            get_character_detail, read_character_diary,
            update_character_state, update_character_skills, update_character_equipment,
            get_character_state, get_character_skills, get_character_equipment,
        ]

        # Reset invocation-scoped state before each GM call.
        reset_read_tracker()

        tools_by_name = {t.name: t for t in all_tools}
        llm_with_tools = llm.bind_tools(all_tools).with_config({
            "callbacks": openrouter_logging_callbacks(scope="game_master", label="call_gm"),
        })

        # Build message: system prompt + history + notice + context
        history_msgs: List[BaseMessage] = []
        if self._history_path and self._history_path.exists():
            history = load_history(self._history_path)
            for h in history:
                role = (h.get("role") or "").strip().lower()
                content = str(h.get("content") or "")
                if role == "user":
                    history_msgs.append(HumanMessage(content=content))
                elif role == "assistant":
                    history_msgs.append(AIMessage(content=content))

        ctx_parts = [f"[call_gm: {notice}]"]
        if context:
            world_time_str = context.get("world_time", "")
            if world_time_str:
                ctx_parts.append(f"\nWorld time: {world_time_str}")
            # Last-call reminder so GM knows the time gap
            last_call = self._meta.last_call_world_time
            if last_call:
                ctx_parts.append(f"GM was last called at: {last_call}")
            players = context.get("players", [])
            wt = None
            try:
                if world_time_str:
                    wt = WorldTime.parse(world_time_str)
            except Exception:
                pass
            if players:
                ctx_parts.append("\nPlayers:")
                for p in players:
                    name = p.get("name", "?")
                    loc = p.get("location", "?")
                    la = p.get("last_acted", "?")
                    suffix = ""
                    if wt and la and la != "never":
                        try:
                            la_wt = WorldTime.parse(la)
                            gap_h = (wt.to_seconds() - la_wt.to_seconds()) / 3600
                            if gap_h >= 12:
                                suffix = " | ⚠ 12h+ behind — cannot be picked"
                        except Exception:
                            pass
                    ctx_parts.append(f"  {name} | location: {loc} | last_acted: {la}{suffix}")
            scene = context.get("current_scene", "")
            if scene:
                ctx_parts.append(f"\nActive scene: {scene}")

        human_msg = "\n".join(ctx_parts)

        # Inject pinned system messages via unified PinnedContext builder
        from engine.pinned_context import PinnedContext
        ctx = PinnedContext(self._history_path, _WORLD)
        persistent = (
            ctx.add_world_setting()
               .add_arc_summaries()
               .add_paragraph_summaries()
               .add_active_characters()
               .build_persistent()
        )
        if self._history_path:
            from world.story import build_gm_summaries_block
            history_msgs = ctx.add_gm_summaries(history_msgs)
        invocation = ctx.add_storage_notice().build_invocation()
        sys_msgs = [SystemMessage(content=self._prompt_text), *persistent, *invocation]
        messages = [*sys_msgs, *history_msgs, HumanMessage(content=human_msg)]

        result = react_loop_iteration(
            messages,
            llm_with_tools,
            tools_by_name,
            termination_tools={"ready_to_proceed", "answer_lore_question", "gm_summary_result"},
            readonly_tools={"get_location", "get_npc", "get_character_detail", "read_character_diary"},
            pinned_refresh_fn=ctx.rebuild_storage_notice,
        )

        # Persist to GM history + tag entries
        if self._history_path:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=human_msg, limits=limits)
            self._meta.append_entry("interaction", f"call_gm: {notice[:60]}")
            for m in result["messages"]:
                t = str(getattr(m, "type", "") or "").strip().lower()
                content = str(getattr(m, "content", "") or "").strip()
                if t in {"ai", "assistant"} and content:
                    append_message(self._history_path, role="assistant", content=content, limits=limits)
                    exit_tool = result.get("exit_tool", "")
                    self._meta.append_entry("interaction", f"GM output: {exit_tool or 'thinking'}")

        thinking = []
        for m in result["messages"]:
            t = str(getattr(m, "type", "") or "").strip().lower()
            content = str(getattr(m, "content", "") or "").strip()
            if t in {"ai", "assistant"} and content:
                thinking.append(content)

        # Record last-call world time for future reminders
        if context and context.get("world_time"):
            self._meta.set_last_call_time(str(context["world_time"]))

        # Summarization triggers (every 10 invocations, every 10 paragraphs)
        inv_count = self._meta.increment_invocation()
        if inv_count > 0 and inv_count % 10 == 0:
            self._run_summarization()

        para_count = self._meta.paragraph_count
        if para_count > 0 and para_count % 10 == 0 and inv_count % 10 != 0:
            self._run_arc_summary()

        return {
            "exit_tool": result.get("exit_tool"),
            "exit_args": result.get("exit_args"),
            "thinking": "\n\n".join(thinking),
        }

    def _run_summarization(self) -> None:
        """Run GM story summarization (every 10 invocations)."""
        if not self._history_path:
            return
        try:
            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="game_master",
            )
            last_para = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[gm_summary_task]\n"
                "Summarize the story since paragraph \"{last_ref}\".\n"
                "Below are the events since then (auto-injected context excluded).\n\n"
                "Events to summarize:\n"
                "{refs}\n\n"
                "Write a paragraph with a short title (3-10 words) and 5-10 sentence summary.\n"
                "Respond with JSON: {\"name\": \"short title\", \"summary\": \"your summary\"}.",
                last_ref=last_para,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-game-master-summary",
                label="gm_summary",
            )
            if result:
                pname, summary = result
                pname = pname or "Summary"
                summary_block = f"[gm_summary:{pname}]\n{summary}"
                limits = limits_from_env()
                append_message(self._history_path, role="user", content=summary_block, limits=limits)
                self._meta.append_entry("auto_injection", f"[gm_summary:{pname}]")
                self._meta.mark_summarized(pname)
                self._meta.increment_paragraph()
                if logs_enabled():
                    print(f"[trace] GM summary: '{pname}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] GM _run_summarization error: {e}")

    def _run_arc_summary(self) -> None:
        """Run GM arc summary (every 10 paragraphs)."""
        if not self._history_path:
            return
        try:
            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="game_master",
            )
            last_para = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[gm_arc_summary_task]\n"
                "Since arc summary after paragraph \"{last_ref}\", "
                "10 new paragraphs have been summarized.\n"
                "Write an arc-level summary covering all these paragraphs.\n"
                "Respond with JSON: {\"name\": \"arc title\", \"summary\": \"your summary\"}.",
                last_ref=last_para,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-game-master-arc",
                label="gm_arc_summary",
            )
            if result:
                arc_name, summary = result
                arc_name = arc_name or "Arc Summary"
                arc_block = f"[gm_arc_summary:{arc_name}]\n{summary}"
                limits = limits_from_env()
                append_message(self._history_path, role="user", content=arc_block, limits=limits)
                self._meta.append_entry("auto_injection", f"[gm_arc_summary:{arc_name}]")
                if logs_enabled():
                    print(f"[trace] GM arc summary: '{arc_name}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] GM _run_arc_summary error: {e}")

    # run_world_seed removed — world setting creation moved to WorldManager
