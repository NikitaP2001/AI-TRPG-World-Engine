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

from history_pipeline import memory_at
from memory_store import load_history
from record_voice import spoken
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import bind_agent_tools, build_openrouter_chat_llm, extract_reasoning, openrouter_logging_callbacks, read_prompt_text
from world import World, WorldTime
from gm.react_loop import react_loop_iteration
from engine.history_meta import HistoryMeta as GMHistoryMeta
from engine.invocation import opening
from engine.summarizer import SummaryRunner

_WORLD = World()


# ---------------------------------------------------------------------------
# GM tools (termination + utility)
# ---------------------------------------------------------------------------


@tool
def invoke_self_gm(
    delay: str = "",
    comment: str = "",
) -> str:
    """Finish this invocation, and say when it should run again.

    Nothing of this invocation carries forward beyond what was written down
    before the call, so anything meant to last must already be recorded. This
    call ends the invocation; the delay says when to return.

    Args:
        delay: How long from now the next invocation is wanted, written with its
            unit — minutes, hours or days, as in "30m" or "2d". The wake-up is
            served by the next turn of the world, so it comes at the first moment
            the world reaches that time, which can be later than the delay names.
        comment: What to attend to then. It is read first on the next run.
    """
    from scheduler_queue import delay_seconds, schedule_invocation

    try:
        ticks = delay_seconds(delay)
    except ValueError as exc:
        return f"Error: {exc}"

    scheduled = schedule_invocation(
        agent="gm", delay_ticks=ticks, comment=comment,
    )
    if scheduled.get("status") == "error":
        return f"Error: {scheduled.get('error')}"
    return f"scheduled at world tick {scheduled.get('scheduled_tick')}"


@tool
def answer_lore_question(content: str) -> str:
    """Answer a question the scene cannot settle on its own.

    Args:
        content: The answer, complete enough that the scene can go on without
            asking again — what is true, and how far it extends.
    """
    return "ok"


class GameMaster:
    """Game Master agent: ReAct-based world manager. Called on-demand via call_gm()."""

    def __init__(self, *, prompt_path: str = "agents/game_master/prompt.txt", history_path: Optional[Path] = None) -> None:
        self._prompt_text = read_prompt_text(prompt_path)
        self._temperature = 0.7
        self._history_path = history_path
        self._peer_history_path: Optional[Path] = None
        self._meta = GMHistoryMeta(
            (self._history_path.parent / "gm_history_meta.json")
            if self._history_path else Path("gm_history_meta.json"),
        )

    def set_peer_history(self, path: Optional[Path]) -> None:
        """Name the memory this agent reports its changes into (the WM's)."""
        self._peer_history_path = path

    def call_gm(
        self,
        *,
        notice: str,
        invoked_as: str,
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        llm: Optional[ChatOpenAI] = None,
    ) -> Dict[str, Any]:
        """Invoke GM with a notice via ReAct loop. GM uses tools until termination.

        Args:
            notice: What the invocation carries — the fact needed, or the reason
                it was woken. Empty for a wake that carries nothing but itself.
            invoked_as: How the call is stated to the Game Master, in words inside
                the brackets: who is asking, and what for. The caller says it,
                because the caller is the one that knows — this call serves a
                question from the scene manager as well as a wake the framework
                served, and only the words distinguish them.
            context: Optional dict with world_time, players list, etc. The moment
                the invocation happens at is stated on its opening either way: a
                caller that does not pass one is dated by the world's clock.
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
                parallel_tool_calls=True,
                thinking=True,  # adjudication is deliberation
                effort="low",
            )

        from world_manager.tool_registry import ToolRegistry

        stated = str(invoked_as or "").strip() or "you are invoked"
        asked = str(notice or "").strip()

        all_tools = tools or [
            invoke_self_gm,
            answer_lore_question,
            *ToolRegistry.for_gm(),
        ]

        tools_by_name = {t.name: t for t in all_tools}
        llm_with_tools = bind_agent_tools(
            llm, all_tools,
            callbacks=openrouter_logging_callbacks(scope="game_master", label="call_gm"),
        )

        # Assemble message: system prompt + history + notice + context
        history_msgs: List[BaseMessage] = []
        if self._history_path and self._history_path.exists():
            for h in load_history(self._history_path):
                role = (h.get("role") or "").strip().lower()
                # Stored entries are records the framework authored, so they are
                # read back as records whatever role they were written with, and
                # in the world's voice: the markers are the code's.
                said = spoken(str(h.get("content") or ""))
                if role in ("user", "assistant") and said.strip():
                    history_msgs.append(HumanMessage(content=said))

        ctx_parts = [f"[{stated}: {asked}]" if asked else f"[{stated}]"]
        world_time_str = ""
        if context:
            world_time_str = str(context.get("world_time", "") or "").strip()
            # Last-call reminder so GM knows the time gap
            last_call = self._meta.last_call_time
            if world_time_str and self._meta.last_call_tick > 0:
                gap_days = 0
                try:
                    gap = (WorldTime.parse(world_time_str).to_seconds()
                           - self._meta.last_call_tick)
                    gap_days = max(0, gap // (24 * 60 * 60))
                except Exception:
                    gap_days = 0
                if gap_days > 0:
                    ctx_parts.append(
                        f"World time advanced {gap_days} day(s) since the GM was last called."
                    )
                elif last_call:
                    ctx_parts.append(f"GM was last called at: {last_call}")
            elif last_call:
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
                    ctx_parts.append(f"  {name} | location: {loc}")
            scene = context.get("current_scene", "")
            if scene:
                ctx_parts.append(f"\nActive scene: {scene}")
            plot = context.get("plot")
            if plot:
                import json as _json
                ctx_parts.append(f"\nPlot:\n{_json.dumps(plot, ensure_ascii=False, indent=2)}")

        # The opening carries the moment it happens at, from one place for every
        # agent: an invocation that does not know when it is cannot say what has
        # happened since it last looked.
        human_msg = opening(ctx_parts, moment=world_time_str)

        # Set up close-invocation validation hook: prevent GM from closing
        # until all character state files are in place.
        from world_manager.tool_registry import set_close_validation_hook
        _players_to_validate = [p.get("name", "") for p in (context or {}).get("players", []) if p.get("name")]

        def _validate_gm_close() -> Optional[str]:
            """Check that GM created all required character data via set_entity."""
            if not _players_to_validate:
                return None
            from world_manager.entity_store import get_entity_state as _get_state, get_entity as _get_entity
            missing = []
            for pname in _players_to_validate:
                entity = _get_entity(pname)
                if entity is None:
                    missing.append(f"{pname}: character entity not found — use set_entity(name=\"{pname}\", ...)")
                    continue
                state = _get_state(pname) or {}
                if not state.get("gurps"):
                    missing.append(f"{pname}: gurps block missing — set ST/DX/IQ/HT")
                else:
                    g = state["gurps"]
                    for attr in ("ST", "DX", "IQ", "HT"):
                        if attr not in g:
                            missing.append(f"{pname}: gurps.{attr} missing")
                if not state.get("state") or not isinstance(state["state"], dict):
                    missing.append(f"{pname}: state block missing — set hp/fp/conditions")
                else:
                    s = state["state"]
                    if not s.get("hp"):
                        missing.append(f"{pname}: state.hp not set")
                    if not s.get("fp"):
                        missing.append(f"{pname}: state.fp not set")
                if not state.get("skills"):
                    missing.append(f"{pname}: skills block missing")
                if not state.get("equipment"):
                    missing.append(f"{pname}: equipment block missing")
                # Location: check entity.position has inside + offset [x,y,z]
                pos = entity.get("position") or {}
                if not pos.get("inside"):
                    missing.append(f"{pname}: position.inside not set — use "
                                   f"position={{\"inside\": \"location_id\", \"offset\": [x, y, z]}}")
                else:
                    offset = pos.get("offset")
                    if not offset or not isinstance(offset, (list, tuple)) or len(offset) != 3:
                        missing.append(f"{pname}: position.offset [x,y,z] required (3 meters)")
            if missing:
                return "GM initialization incomplete:\n" + "\n".join(f"  - {m}" for m in missing)
            return None

        set_close_validation_hook(_validate_gm_close)

        # Inject pinned system messages via unified PinnedContext builder
        from engine.pinned_context import PinnedContext
        ctx = PinnedContext(self._history_path, _WORLD)
        persistent = (
            ctx.add_world_setting()
               .add_gm_summaries()
               .add_all_player_descriptions()
               .add_active_characters()
               .build_persistent()
        )
        invocation = ctx.add_storage_notice().build_invocation()
        sys_msgs = [SystemMessage(content=self._prompt_text), *persistent]
        messages = [*sys_msgs, *history_msgs, *invocation, HumanMessage(content=human_msg)]

        from engine.change_summary import ChangeCollector, append_change_record
        changes = ChangeCollector()

        # The invocation is a branch of the GM's memory, opened when it wakes and
        # closed by the termination tool that ends its work. Whatever it writes
        # while the branch is open is entered then; nothing is written to the
        # thread behind the branch's back.
        own_memory = memory_at(self._history_path) if self._history_path else None
        if own_memory is not None:
            own_memory.open_branch()

        result = react_loop_iteration(
            messages,
            llm_with_tools,
            tools_by_name,
            termination_tools={"invoke_self_gm", "answer_lore_question", "close_invocation"},
            readonly_tools={"get_entity", "get_entity_facts", "get_entity_state",
                            "query_entities", "query_events"},
            pinned_refresh_fn=ctx.rebuild_storage_notice,
            changes=changes,
        )

        # Clear the character-file validation hook regardless of outcome
        set_close_validation_hook(None)

        # What the call carries, and how it was stated. The invocation context —
        # world time, players, scene, plot — is rebuilt for every call, so writing
        # it here would leave a second, staler copy of facts the next call states
        # again; a wake that carried nothing is not a record either, and the
        # invocation itself is kept in the metadata.
        if self._history_path:
            if asked:
                own_memory.record(f"[call_gm: {asked}]")
            self._meta.append_entry("interaction", f"call_gm: {notice[:60]}")
            for m in result["messages"]:
                t = str(getattr(m, "type", "") or "").strip().lower()
                content = str(getattr(m, "content", "") or "").strip()
                if content.startswith("[written]"):
                    # What this invocation wrote through its tools, in the order it
                    # wrote it. Without it the memory keeps the deliberation and
                    # loses the deed, and the next wake reads its own past as
                    # thinking that led nowhere.
                    own_memory.record(content)
                    continue
                # What it thought, and only what it thought: what it said inside
                # the loop is speech to the tools, not a record of the invocation,
                # and a record read as deliberation must not be carrying speech.
                reasoning = extract_reasoning(m) if t in {"ai", "assistant"} else ""
                if reasoning:
                    own_memory.record(f"[reasoning]\n{reasoning}")
                    exit_tool = result.get("exit_tool", "")
                    self._meta.append_entry("interaction", f"GM output: {exit_tool or 'thinking'}")
            # What this invocation changed, in outline: one aggregated record
            # instead of the calls that caused it, sent to the World Manager —
            # the authority above this one — so what the campaign changed reaches
            # the memory that governs the world. Not kept here: the Game Master
            # made these changes itself in this invocation, so a copy of them in
            # its own memory tells it what it already knows, and pushes out what
            # it was told by others.
            changes_block = changes.build("gm")
            append_change_record(self._peer_history_path, changes_block)

        thinking = []
        for m in result["messages"]:
            t = str(getattr(m, "type", "") or "").strip().lower()
            content = str(getattr(m, "content", "") or "").strip() or extract_reasoning(m)
            if t in {"ai", "assistant"} and content:
                thinking.append(content)

        # Record that this invocation happened, in world time and in ticks. The
        # interval fallback and the reminder above are both measured from it, and
        # it is recorded here so every caller counts the same way.
        from engine.agent_schedule import record_call
        record_call("gm")

        # A self-invocation names the reason it asked to be woken again, and an
        # answer names what the invocation ended on; a wake that closed carries
        # nothing of its own beyond its thinking and what it wrote.
        exit_tool = str(result.get("exit_tool") or "")
        exit_args = result.get("exit_args") or {}
        if self._history_path:
            if exit_tool == "invoke_self_gm":
                comment = str(exit_args.get("comment") or "").strip()
                if comment:
                    own_memory.record(f"[invoke_self] {comment}")
            elif exit_tool == "answer_lore_question":
                # What the invocation ended on. The memory keeps the question it
                # was woken with and the deliberation that led to the answer; left
                # unrecorded, the one thing it established is what it loses, and
                # the next wake reads its own past as an answer nobody gave.
                answer = str(exit_args.get("content") or "").strip()
                if answer:
                    own_memory.record(f"[answer]\n{answer}")
            # The termination tool ended the wake: the branch closes with it, and
            # what it kept is merged into the thread.
            own_memory.close_branch()

        # Summarization triggers (every 10 invocations, every 10 paragraphs)
        inv_count = self._meta.increment_invocation()
        if inv_count > 0 and inv_count % 10 == 0:
            self._run_summarization()

        para_count = self._meta.paragraph_count
        if para_count > 0 and para_count % 10 == 0 and inv_count % 10 != 0:
            self._run_arc_summary()

        # The trims that just ran dropped the oldest records — and those records
        # are still in this invocation's own context. So the paragraph carrying
        # them forward is written from here, where they can be pointed at, before
        # the next call opens on a history that begins mid-story.
        self._cover_retired_history(history_msgs, persistent)

        return {
            "exit_tool": result.get("exit_tool"),
            "exit_args": result.get("exit_args"),
            "thinking": "\n\n".join(thinking),
        }

    def _cover_retired_history(self, history_msgs, pinned) -> None:
        """Turn what a trim dropped into a paragraph, while it is still in context.

        ``history_msgs`` is the history as this invocation saw it, ``pinned`` the
        blocks above it. The retirement request reuses both, so its prefix is the
        one this invocation already paid for; only the task at its end is new.
        """
        if not self._history_path:
            return
        try:
            from engine.history_retirement import dropped_from, pending_retirement, retire
            from world.agent_story import append_paragraph

            fell_out = dropped_from(self._history_path, history_msgs)
            if not fell_out and not pending_retirement(self._history_path):
                return

            root = self._history_path.parent

            def _write(name, summary):
                append_paragraph(
                    summaries_path=root / "gm_summaries.json",
                    story_path=root / "gm_story.json",
                    name=name or "Summary",
                    summary=summary,
                )

            if retire(
                self._history_path,
                dropped=fell_out,
                prefix=list(pinned or []),
                history_messages=history_msgs,
                prompt_text=self._prompt_text,
                temperature=self._temperature,
                scope="game_master",
                write=_write,
                kind="paragraph",
                label="gm_history_retirement",
            ):
                self._meta.append_entry("auto_injection", "[gm_history_retirement]")
                if logs_enabled():
                    print("[trace] GM covered retired history")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] GM history retirement failed: {e}")

    def _run_summarization(self) -> None:
        """Run GM story summarization (every 10 invocations).

        Writes to gm_story.json + gm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_paragraph

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
                root = self._history_path.parent
                append_paragraph(
                    summaries_path=root / "gm_summaries.json",
                    story_path=root / "gm_story.json",
                    name=pname,
                    summary=summary,
                )
                self._meta.append_entry("auto_injection", f"[gm_paragraph:{pname}]")
                self._meta.mark_summarized(pname)
                self._meta.increment_paragraph()
                if logs_enabled():
                    print(f"[trace] GM summary: '{pname}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] GM _run_summarization error: {e}")

    def _run_arc_summary(self) -> None:
        """Run GM arc summary (every 10 paragraphs).

        Writes to gm_story.json + gm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_arc

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
                root = self._history_path.parent
                append_arc(
                    summaries_path=root / "gm_summaries.json",
                    story_path=root / "gm_story.json",
                    name=arc_name,
                    summary=summary,
                )
                self._meta.append_entry("auto_injection", f"[gm_arc:{arc_name}]")
                if logs_enabled():
                    print(f"[trace] GM arc summary: '{arc_name}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] GM _run_arc_summary error: {e}")

    # run_world_seed removed — world setting creation moved to WorldManager
