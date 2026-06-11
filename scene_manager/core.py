"""Scene Manager agent: reactive, GM-dependent scene lifecycle.

Receives a pre-chosen scene from the Game Master and handles:
- World planning (pre-establish hidden facts)
- Character execution loop
- Turn narration with correction rounds
- Paragraph summarization

Has its own message history and prompt log output.
Does NOT pick scenes — the Game Master does that.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from memory_store import append_message, load_history, limits_from_env
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world import World, WorldDuration, WorldTime, build_game_master_context_block
from scene_manager.tools import (
    answer_character, call_gm as sm_call_gm_tool,
    turn_narration, correct_character_intents, run_scene,
    add_character_to_scene, remove_character_from_scene,
    add_npc_to_scene, remove_npc_from_scene,
)
from engine.history_meta import HistoryMeta

_DURATION_EXTRACT_RE = re.compile(
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\b",
    flags=re.IGNORECASE,
)


def _coerce_duration(raw: str) -> Optional[str]:
    """Normalize a messy GM-supplied duration string."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        WorldDuration.parse_user_input(raw)
        return raw
    except ValueError:
        pass
    m = _DURATION_EXTRACT_RE.search(raw)
    if m:
        candidate = m.group(1) + " " + m.group(2)
        try:
            WorldDuration.parse_user_input(candidate)
            return candidate
        except ValueError:
            pass
    return None


def _extract_tool_calls(msg: Any) -> List[Dict[str, Any]]:
    """Extract tool calls from a LangChain message."""

    def _normalize(tc: Any) -> Optional[Dict[str, Any]]:
        if isinstance(tc, dict):
            args_val = tc.get("args")
            if isinstance(args_val, str) and args_val.strip():
                tc = dict(tc)
                try:
                    tc["args"] = json.loads(args_val)
                except Exception:
                    pass
            return tc
        try:
            name = getattr(tc, "name", None)
            args = getattr(tc, "args", None)
            if name is not None:
                out: Dict[str, Any] = {"name": str(name)}
                if isinstance(args, dict):
                    out["args"] = args
                elif isinstance(args, str) and args.strip():
                    try:
                        out["args"] = json.loads(args)
                    except Exception:
                        out["args"] = args
                elif args is not None:
                    out["args"] = args
                return out
        except Exception:
            pass
        return None

    tcs = getattr(msg, "tool_calls", None)
    if isinstance(tcs, list) and tcs:
        out = []
        for tc in tcs:
            n = _normalize(tc)
            if n:
                out.append(n)
        if out:
            return out

    additional = getattr(msg, "additional_kwargs", {}) or {}
    if isinstance(additional, dict):
        tcs2 = additional.get("tool_calls")
        if isinstance(tcs2, list) and tcs2:
            out = []
            for tc in tcs2:
                n = _normalize(tc)
                if n:
                    out.append(n)
            if out:
                return out
        fc = additional.get("function_call")
        if isinstance(fc, dict):
            name = str(fc.get("name") or "").strip()
            args_raw = fc.get("arguments")
            args: Any = {}
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str) and args_raw.strip():
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"_raw": args_raw}
            if name:
                return [{"name": name, "args": args if isinstance(args, dict) else {"_raw": args}}]
    return []


def _append_tool_rejection(messages: List, out: Any, error_text: str) -> None:
    """Append an AI tool-call response + synthetic ToolMessage(s)."""
    messages.append(out)
    tcs = getattr(out, "tool_calls", None) or []
    if not isinstance(tcs, list):
        tcs = []
    if not tcs:
        ak = getattr(out, "additional_kwargs", {}) or {}
        tcs2 = ak.get("tool_calls") if isinstance(ak, dict) else None
        if isinstance(tcs2, list):
            tcs = tcs2
    if tcs:
        for i, tc in enumerate(tcs):
            if isinstance(tc, dict):
                tc_id = str(tc.get("id") or "").strip()
                tc_name = str(tc.get("name") or "tool").strip()
            else:
                tc_id = str(getattr(tc, "id", "") or "").strip()
                tc_name = str(getattr(tc, "name", "") or "tool").strip()
            if not tc_id:
                tc_id = f"synthetic_tc_{i}"
            messages.append(ToolMessage(content=error_text, tool_call_id=tc_id, name=tc_name))
    else:
        messages.append(HumanMessage(content=error_text))


def _context_for_turn_narration(context_text: str) -> str:
    return str(context_text or "")


def _prune_context_sections(context_text: str, *, drop_titles: List[str]) -> str:
    raw = str(context_text or "")
    if not raw.strip() or not drop_titles:
        return raw
    lines = raw.splitlines()
    drop = {str(x).strip().lower() for x in drop_titles if str(x).strip()}
    out: List[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### "):
            title = stripped[4:].strip().lower()
            skip = title in drop
            if not skip:
                out.append(line)
            continue
        if not skip:
            out.append(line)
    pruned = "\n".join(out).strip()
    return (pruned + "\n") if pruned else ""


class SceneManager:
    """Scene Manager agent: scene lifecycle execution.

    Handles character execution, turn narration, auto-advance.
    Can call the Game Master on-demand via call_gm().
    """

    def __init__(
        self,
        *,
        prompt_path: str = "agents/scene_manager/prompt.txt",
        history_path: Optional[Path] = None,
        temperature: float = 0.7,
    ) -> None:
        self._prompt_text = read_prompt_text(prompt_path)
        self._temperature = temperature
        self._history_path = history_path
        self._turn_qa_buffer: List[Dict[str, str]] = []
        self._world = World()
        self._meta = HistoryMeta(
            (self._history_path.parent / "scene_manager_meta.json")
            if self._history_path else Path("scene_manager_meta.json"),
        )
    def call_gm(self, notice: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke the Game Master on-demand via ReAct loop.

        Args:
            notice: Text explaining why GM was called.
            context: Dict with world_time, players list, etc.

        Returns:
            Dict with exit_tool, exit_args, thinking from the GM.
        """
        from gm.game_master import GameMaster
        # Use a fresh GameMaster instance — history is loaded from disk.
        gm = GameMaster(
            history_path=self._history_path.parent / "game_master_messages.json"
            if self._history_path else None,
        )
        return gm.call_gm(notice=notice, context=context)

    def resolve_character_question(
        self,
        *,
        character_name: str,
        questions: str,
    ) -> str:
        """Answer a character's question, optionally escalating to GM.

        Gives the SM LLM a targeted choice:
          - ``answer_character`` — if it knows the answer from world data
          - ``call_gm`` — if it needs to escalate

        Returns the answer text.
        """
        from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks
        from scene_manager.tools import answer_character, call_gm as sm_call_gm_tool

        llm = build_openrouter_chat_llm(
            temperature=0.5,
            streaming=True,
            title_suffix="-scene-manager-qa",
            max_tokens=1000,
            parallel_tool_calls=False,
        )

        from scene_manager.tools import (
            get_location, get_npc, get_character_detail, read_character_diary,
        )

        tools = [
            answer_character,
            sm_call_gm_tool,
            get_location,
            get_npc,
            get_character_detail,
            read_character_diary,
        ]

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="resolve_character_question")

        try:
            bound = llm.bind_tools(
                tools,
                tool_choice="required",
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound = llm.bind_tools(tools).with_config({"callbacks": callbacks})

        task_msg = (
            f"A character named \"{character_name}\" asks:\n"
            f"{questions}\n\n"
            "You have access to world data tools (get_location, get_npc, "
            "get_character_detail, read_character_diary).\n"
            "Choose ONE:\n"
            "  - answer_character — if you can answer from world data\n"
            "  - call_gm — if you don't know and the GM needs to answer\n"
            "Do not call the GM for questions you can answer yourself."
        )

        for _attempt in range(3):
            try:
                out = bound.invoke([
                    SystemMessage(content=self._prompt_text),
                    *self._pinned_summaries(),
                    HumanMessage(content=task_msg),
                ])
            except Exception:
                continue

            tcs = getattr(out, "tool_calls", None) or []
            if not tcs:
                continue
            tc = tcs[0]
            name = str(tc.get("name") or "").strip()
            args = tc.get("args") if isinstance(tc, dict) else {}
            if not isinstance(args, dict):
                continue

            if name == "answer_character":
                return str(args.get("content") or "I don't have an answer.").strip()
            elif name == "call_gm":
                # Escalate to the real GM
                notice = str(args.get("notice") or questions).strip()
                result = self.call_gm(notice)
                return str(result.get("exit_args") or result.get("thinking") or "No answer.").strip()

        return "I could not resolve your question right now."

    def get_entities_at_location(self, location: str) -> Dict[str, List[str]]:
        """Return {characters: [...], npcs: [...]} at this location.

        Used by determine_next_scene() to auto-include entities.
        """
        chars = []
        try:
            for cname in self._world.list_character_names():
                desc = self._world.get_character_description(cname)
                if isinstance(desc, dict) and desc.get("location") == location:
                    chars.append(cname)
        except Exception:
            pass
        npcs = []
        try:
            npcs_data = self._world.get_npcs()
            if isinstance(npcs_data, dict):
                for nname, ndata in npcs_data.items():
                    if isinstance(ndata, dict) and ndata.get("location") == location:
                        npcs.append(nname)
        except Exception:
            pass
        return {"characters": chars, "npcs": npcs}

    def determine_next_scene(self, last_scene_location: str) -> Dict[str, Any]:
        """Determine next scene after a turn ends, without calling GM.

        Returns:
            {"type": "auto", "location": str, "character_names": [str],
             "npc_names": [str], "scene_description": ""}
            or {"type": "needs_gm", "reason": "...", ...}
            or {"type": "no_scene", "reason": "..."}
        """
        try:
            info = self._world.get_info()
            chars = info.get("characters") if isinstance(info, dict) else []
            wt = self._world.get_world_time()
            wt_sec = wt.to_seconds()

            # Find catch-up candidates (12h+ behind)
            catch_up = None
            for ch in chars:
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                loc = str(ch.get("location") or "").strip()
                la = str(ch.get("last_acted") or "").strip()
                if not name or not loc:
                    continue
                if not la or la == "never":
                    catch_up = (name, loc)
                    break
                try:
                    la_wt = WorldTime.parse(la)
                    gap_h = (wt_sec - la_wt.to_seconds()) / 3600
                    if gap_h >= 12:
                        catch_up = (name, loc)
                        break
                except Exception:
                    continue

            if catch_up:
                pname, ploc = catch_up
                # Check if location exists
                try:
                    self._world.get_location(ploc)
                except Exception:
                    return {
                        "type": "needs_gm",
                        "reason": "unknown_location",
                        "player_name": pname,
                        "missing_location": ploc,
                    }
                entities = self.get_entities_at_location(ploc)
                return {
                    "type": "auto",
                    "location": ploc,
                    "character_names": entities["characters"],
                    "npc_names": entities["npcs"],
                    "scene_description": "",
                }

            # No catch-up needed: continue at same location
            try:
                self._world.get_location(last_scene_location)
            except Exception:
                return {"type": "no_scene", "reason": "no_valid_location"}

            entities = self.get_entities_at_location(last_scene_location)
            return {
                "type": "auto",
                "location": last_scene_location,
                "character_names": entities["characters"],
                "npc_names": entities["npcs"],
                "scene_description": "",
            }

        except Exception as e:
            return {"type": "no_scene", "reason": f"error: {e}"}

    def describe_scene(
        self,
        *,
        location: str,
        character_names: List[str],
        npc_names: Optional[List[str]] = None,
        llm: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Produce a scene description when players move to a new location.

        Calls the SM's LLM with a minimal scene description request.
        Returns dict with shared, personal_json, etc.
        """
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-scene-manager",
                max_tokens=1500,
                parallel_tool_calls=False,
            )

        from scene_manager.tools import run_scene as sm_run_scene, call_gm as sm_call_gm_tool

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="describe_scene", stream=True)
        human_msg = (
            f"Players moved to a new location: {location}.\n"
            f"Characters: {', '.join(character_names)}\n"
            f"NPCs present: {', '.join(npc_names or []) or 'none'}\n\n"
            "Describe this scene using the run_scene tool. "
            "If you need the Game Master's help for world details, use call_gm."
        )

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

        messages = [
            SystemMessage(content=self._prompt_text),
            *self._pinned_summaries(),
            *history_msgs,
            HumanMessage(content=human_msg),
        ]

        try:
            bound_llm = llm.bind_tools(
                [sm_run_scene, sm_call_gm_tool],
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([sm_run_scene, sm_call_gm_tool]).with_config({"callbacks": callbacks})

        import json as _json
        for _attempt in range(3):
            try:
                out = bound_llm.invoke(messages)
            except Exception:
                continue

            tool_calls = getattr(out, "tool_calls", None) or []
            if not tool_calls:
                continue

            tc = tool_calls[0]
            name = str(tc.get("name") or "").strip()
            args = tc.get("args") if isinstance(tc, dict) else {}
            if not isinstance(args, dict):
                args = {}

            if name == "call_gm":
                notice = str(args.get("notice") or "").strip()
                if notice:
                    gm_result = self.call_gm(notice)
                    messages.append(HumanMessage(
                        content=f"[GM response]\n{gm_result.get('thinking', gm_result.get('exit_args', 'No response.'))}"
                    ))
                continue

            shared = str(args.get("shared") or "").strip()
            personal_raw = str(args.get("personal_json") or "").strip()
            personal: Dict[str, str] = {}
            if personal_raw:
                try:
                    obj = _json.loads(personal_raw)
                    if isinstance(obj, dict):
                        personal = {str(k): str(v).strip() for k, v in obj.items() if str(v).strip()}
                except Exception:
                    pass

            if shared:
                return {
                    "shared": shared,
                    "personal": personal,
                    "combined": shared + ("\n\n" + "\n\n".join(
                        f"[{n}]\n{t}" for n, t in personal.items()
                    ) if personal else ""),
                }

        return {"shared": "", "personal": {}, "combined": ""}

    def run_summary_task(
        self,
        *,
        task_type: str,
        last_ref: str = "",
        label: str = "",
    ) -> Optional[str]:
        """Run a summarization task: paragraph or arc.

        Builds a request referencing only interaction entries since last summary.
        No big context dump — the model already has full details in its history.

        Args:
            task_type: "paragraph" or "arc"
            last_ref: Last paragraph/arc name to reference as starting point.
            label: Short label for the task message.

        Returns: summary text or None.
        """
        if not self._history_path:
            return None
        try:
            from engine.summarizer import SummaryRunner

            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="scene_manager",
            )

            if task_type == "paragraph":
                template = (
                    "[sm_summary_task]\n"
                    "Summarize the story since \"{last_ref}\".\n"
                    "Below are the events (auto-injected context excluded):\n\n"
                    "{refs}\n\n"
                    "Write a paragraph with short title (3-10 words) and 5-10 sentence summary.\n"
                )
            else:
                template = (
                    "[sm_arc_summary_task]\n"
                    "Since arc \"{last_ref}\", "
                    "10 paragraphs have been summarized.\n"
                    "Write an arc-level summary.\n"
                    "Events covered:\n{refs}\n"
                )

            task_prompt = runner.build_task_prompt(template, last_ref=last_ref)
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-scene-manager-summary",
                label=label or f"{task_type}_summary",
                extra_pinned=self._pinned_summaries(),
            )
            if result:
                name, summary = result
                name = name or "Summary"
                return json.dumps({"name": name, "summary": summary}, ensure_ascii=False)
            return None
        except Exception as e:
            if logs_enabled():
                print(f"[trace] SM run_summary_task error: {e}")
            return None

    def _run_paragraph_summary(self) -> None:
        """Run paragraph summary: called by scheduler every 10 turns."""
        import logging
        try:
            from world.story import finalize_paragraph
            from world.io import _read_json

            story_path = self._world.paths.story_json
            arcs = _read_json(story_path)
            if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
                return

            arc0 = arcs[0]
            ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0.get("ongoing_paragraph"), dict) else None
            if not isinstance(ongoing, dict):
                return

            turns = ongoing.get("turns")
            if not isinstance(turns, list) or len(turns) < 10:
                return

            last_ref = str(arc0.get("name") or "Ongoing Arc")
            result_raw = self.run_summary_task(
                task_type="paragraph",
                last_ref=last_ref,
            )
            if not result_raw:
                return

            try:
                parsed = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            except Exception:
                return
            if not isinstance(parsed, dict):
                return
            name = str(parsed.get("name") or "").strip()
            summary = str(parsed.get("summary") or "").strip()
            if not name or not summary:
                return

            result_obj = {"name": name, "summary": summary}
            end_time = str((turns[-1] or {}).get("end_time") or "")

            finalize_paragraph(
                story_json=story_path,
                paragraph_obj=result_obj,
                end_time=end_time,
            )
            self._meta.mark_summarized(name)
            if logs_enabled():
                print(f"[trace] SM paragraph summary: '{name}' ({len(summary)} chars)")
        except Exception as e:
            logging.exception(f"[scheduler] paragraph_summary failed: {e}")
            raise

    def _run_arc_summary(self) -> None:
        """Run arc summary: called by scheduler every 10 paragraphs."""
        import logging
        try:
            from world.story import finalize_arc
            from world.io import _read_json

            story_path = self._world.paths.story_json
            arcs = _read_json(story_path)
            if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
                return

            arc0 = arcs[0]
            paragraphs = arc0.get("paragraphs") if isinstance(arc0.get("paragraphs"), list) else []
            last_para_name = paragraphs[-1].get("name", "") if paragraphs else ""
            if not last_para_name:
                return

            result_raw = self.run_summary_task(
                task_type="arc",
                last_ref=last_para_name,
                label="arc_summary",
            )
            if not result_raw:
                return

            try:
                parsed = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            except Exception:
                return
            if not isinstance(parsed, dict):
                return
            arc_name = str(parsed.get("name") or "").strip()
            arc_summary = str(parsed.get("summary") or "").strip()
            if not arc_name or not arc_summary:
                return

            finalize_arc(
                story_json=story_path,
                arc_obj={"name": arc_name, "summary": arc_summary},
            )
            self._meta.mark_summarized(arc_name)
            if logs_enabled():
                print(f"[trace] SM arc summary: '{arc_name}' ({len(arc_summary)} chars)")
        except Exception as e:
            logging.exception(f"[scheduler] arc_summary failed: {e}")
            raise

    def inject_delta(self, content: str) -> None:
        """Inject a standalone world-state delta message into SM history."""
        if not content or not content.strip():
            return
        if not self._history_path:
            return
        limits = limits_from_env()
        append_message(self._history_path, role="user", content=content.strip(), limits=limits)
        self._meta.append_entry("auto_injection", content.strip()[:60])

    def _pinned_summaries(self) -> List[SystemMessage]:
        """Build pinned summary SystemMessages via PinnedContext (trim-delayed)."""
        if not self._history_path:
            return []
        try:
            from engine.pinned_context import PinnedContext
            ctx = PinnedContext(self._history_path, self._world)
            return (
                ctx.add_world_setting()
                   .add_arc_summaries()
                   .add_paragraph_summaries()
                   .add_active_characters()
                   .build_persistent()
            )
        except Exception:
            return []

    def clear_turn_qa_buffer(self) -> None:
        self._turn_qa_buffer.clear()

    def run_turn_narration(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run TURN_NARRATION using tool calls that return narrations and duration."""
        if llm is None:
            from openrouter_llm import build_openrouter_chat_llm
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-scene-manager",
                max_tokens=2000,
                parallel_tool_calls=False,
            )

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="turn_narration")
        from scene_manager.tools import turn_narration, correct_character_intents, call_gm as sm_call_gm_tool

        human_msg = self._build_turn_narration_message(payload)

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

        task_context = _context_for_turn_narration(context_text)
        ctx_note = ("\n\n---\n" + task_context.strip()) if task_context and task_context.strip() else ""

        messages = [
            SystemMessage(content=self._prompt_text),
            *self._pinned_summaries(),
            *history_msgs,
            HumanMessage(content=human_msg + ctx_note),
        ]

        try:
            bound_llm = llm.bind_tools(
                [turn_narration, correct_character_intents, sm_call_gm_tool],
                tool_choice="required",
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([turn_narration, correct_character_intents, sm_call_gm_tool]).with_config({"callbacks": callbacks})

        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        selected_characters = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]

        narrations: Dict[str, str] = {}
        _coerced_dur: Optional[str] = None
        _shared_narration: str = ""
        _personal_narration: Dict[str, str] = {}

        max_retries = 3
        for _attempt in range(max_retries):
            try:
                out = bound_llm.invoke(messages)
            except Exception as exc:
                if logs_enabled():
                    print(f"[trace] SM run_turn_narration: LLM error, retrying: {exc}")
                continue

            # Capture thinking text (not saved to messages) for stream visibility.
            thinking_text = str(getattr(out, "content", None) or "").strip()
            if thinking_text and logs_enabled():
                for line in thinking_text.split("\n"):
                    if line.strip():
                        print(f"[think] SM: {line.strip()}")

            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                if logs_enabled():
                    print(f"[trace] SM run_turn_narration: no tool call, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(
                    messages, out,
                    "ERROR: You MUST call turn_narration or correct_character_intents."
                )
                continue

            tc0 = tool_calls[0]
            tool_name0 = str(tc0.get("name") or "").strip()
            args0 = tc0.get("args") if isinstance(tc0, dict) else {}
            if isinstance(args0, str) and args0.strip():
                try:
                    args0 = json.loads(args0)
                except Exception:
                    args0 = {}
            if not isinstance(args0, dict):
                args0 = {}

            if tool_name0 == "correct_character_intents":
                correction_character = str(args0.get("character_name") or "").strip()
                correction_notice = str(args0.get("turn_insight") or "").strip()
                print(f"[debug] SM correction: character={correction_character!r} turn_insight={correction_notice[:100]!r}")
                if not correction_character or not correction_notice:
                    if logs_enabled():
                        print(f"[trace] SM run_turn_narration: invalid correction args, retrying")
                    _append_tool_rejection(
                        messages, out,
                        "ERROR: correct_character_intents requires non-empty character_name and turn_insight.",
                    )
                    continue
                return {
                    "type": "correction",
                    "character_name": correction_character,
                    "turn_insight": correction_notice,
                }

            if tool_name0 == "call_gm":
                notice = str(args0.get("notice") or "").strip()
                if not notice:
                    if logs_enabled():
                        print(f"[trace] SM run_turn_narration: empty call_gm notice, retrying")
                    _append_tool_rejection(
                        messages, out,
                        "ERROR: call_gm requires non-empty notice.",
                    )
                    continue
                gm_result = self.call_gm(notice)
                # Inject GM response back into the conversation so the SM can continue
                messages.append(HumanMessage(
                    content=f"[GM response]\n{gm_result.get('thinking', gm_result.get('exit_args', 'No response.'))}"
                ))
                continue

            if tool_name0 != "turn_narration":
                if logs_enabled():
                    print(f"[trace] SM run_turn_narration: unexpected tool {tool_name0!r}, retrying")
                _append_tool_rejection(
                    messages, out,
                    "ERROR: Use turn_narration or correct_character_intents.",
                )
                continue

            _shared0 = str(args0.get("shared") or "").strip()
            _personal_raw0 = str(args0.get("personal_json") or "").strip()
            duration0 = str(args0.get("duration") or "").strip()

            _personal0: Dict[str, str] = {}
            if _personal_raw0:
                try:
                    obj = json.loads(_personal_raw0)
                    if isinstance(obj, dict):
                        _personal0 = {str(k): str(v).strip() for k, v in obj.items() if str(v).strip()}
                except Exception:
                    pass

            if not _shared0:
                if logs_enabled():
                    print(f"[trace] SM run_turn_narration: empty shared, retrying")
                _append_tool_rejection(messages, out, "ERROR: shared is empty. Provide non-empty shared narration.")
                continue

            _coerced_dur = _coerce_duration(duration0)
            if _coerced_dur is None:
                if logs_enabled():
                    print(f"[trace] SM run_turn_narration: invalid duration {duration0!r}, retrying")
                _append_tool_rejection(
                    messages, out,
                    f"ERROR: Invalid duration {duration0!r}. Use plain number+unit, e.g. '5m', '30s'.",
                )
                continue

            _selected_set0 = set(selected_characters)
            _personal0 = {k: v for k, v in _personal0.items() if k in _selected_set0}
            narrations = {
                c: (_shared0 + "\n\n" + _personal0[c]) if c in _personal0 else _shared0
                for c in selected_characters
            }
            _shared_narration = _shared0
            _personal_narration = _personal0
            break
        else:
            narrations = {}
            _coerced_dur = None

        duration = _coerced_dur or ""
        world_facts = ""

        _narration_personal_blocks = "\n\n".join(
            f"[{n}]\n{_personal_narration[n]}" for n in selected_characters if n in _personal_narration
        )
        combined_narration = (
            _shared_narration + "\n\n" + _narration_personal_blocks
        ).strip() if _narration_personal_blocks else _shared_narration

        history_payload = dict(payload) if isinstance(payload, dict) else {}
        turn_end_time = ""
        try:
            start_time = str(history_payload.get("turn_start_time") or "").strip()
            if start_time and duration:
                start_wt = WorldTime.parse(start_time)
                dur = WorldDuration.parse_user_input(duration)
                turn_end_time = start_wt.add_duration(dur).to_string()
        except Exception:
            turn_end_time = ""
        if turn_end_time:
            history_payload["turn_end_time"] = turn_end_time

        history_user_msg = self._build_turn_history_message(history_payload, human_msg)

        if self._history_path and combined_narration:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            self._meta.append_entry("interaction", f"turn_narration at {history_payload.get('location','?')}")
            assistant_parts: List[str] = []
            if turn_end_time:
                assistant_parts.append(f"Turn end time: {turn_end_time}")
            if duration:
                assistant_parts.append(f"Turn duration: {duration}")
            assistant_parts.append(combined_narration)
            assistant_mem = "\n\n".join(assistant_parts)
            append_message(self._history_path, role="assistant", content=assistant_mem, limits=limits)
            self._meta.append_entry("interaction", f"turn_narration result ({duration})")

        if combined_narration:
            self._turn_qa_buffer.clear()

        return {
            "type": "narration",
            "narration": combined_narration,
            "narrations": narrations,
            "duration": duration,
            "world_facts": world_facts,
        }

    def _build_turn_narration_message(self, payload: Dict[str, Any]) -> str:
        """Build the human message for turn narration."""
        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        participant_names = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]
        round_history = payload.get("turn_round_history") if isinstance(payload.get("turn_round_history"), list) else []

        msg = (
            "Resolve this turn by choosing tools based on Correcting character intents"
            " and Narrating a turn paragraphs:\n"
        )

        if round_history:
            msg += "\nRound history for this same turn (oldest first):\n"
            for item in round_history:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                item_round = str(item.get("round") or "?").strip() or "?"
                if item_type == "plans":
                    plans_hist = item.get("character_plans") if isinstance(item.get("character_plans"), list) else []
                    msg += f"\n[Round {item_round} plans]\n"
                    item_idx = round_history.index(item) if item in round_history else -1
                    has_following_correction = any(
                        isinstance(round_history[i], dict)
                        and str(round_history[i].get("type") or "").strip().lower() in ("correction", "replan")
                        for i in range(item_idx + 1, len(round_history))
                    )
                    if has_following_correction:
                        msg += "DECLINED: do not apply these intents directly; use current intentions below.\n"
                    for plan in plans_hist:
                        if not isinstance(plan, dict):
                            continue
                        char_name = str(plan.get("name") or "").strip() or "Unknown"
                        intent = str(plan.get("intent") or "").strip()
                        thoughts = str(plan.get("thoughts") or "").strip()
                        msg += f"- {char_name}: {intent or '(empty intent)'}\n"
                        if thoughts:
                            msg += f"  thoughts: {thoughts}\n"
                elif item_type == "correction":
                    char_name = str(item.get("character_name") or "").strip() or "Unknown"
                    decline_reason = str(item.get("turn_insight") or "").strip()
                    msg += f"\n[Round {item_round} correction] {char_name}: intent rejected, replanning requested\n"
                    if decline_reason:
                        msg += f"Decline reason: {decline_reason}\n"
                elif item_type == "replan":
                    char_name = str(item.get("character_name") or "").strip() or "Unknown"
                    msg += f"\n[Round {item_round} replan] {char_name}: new intent submitted\n"
                elif item_type == "final_decision":
                    char_name = str(item.get("character_name") or "").strip() or "Unknown"
                    ruling = str(item.get("gm_final_ruling") or "").strip()
                    msg += f"\n[Round {item_round} final_decision] {char_name}: correction limit reached\n"
                    if ruling:
                        msg += f"GM final decision: {ruling}\n"

        if participant_names:
            msg += "\nParticipants:\n" + ", ".join(participant_names) + "\n"

        if char_plans:
            msg += "\nPlayer intentions for this turn:\n"
            msg += "\n--- TURN BEGIN ---\n"
            for plan in char_plans:
                if isinstance(plan, dict):
                    char_name = plan.get("name") or plan.get("character_name") or "Unknown"
                    action = plan.get("intent") or plan.get("action") or plan.get("decision") or "acts"
                    thoughts = str(plan.get("thoughts") or "").strip()
                    msg += f"\n{char_name}:\n  Intent: {action}\n"
                    if thoughts:
                        msg += f"  Secret thoughts: {thoughts}\n"
            msg += "\n--- TURN END ---\n"

        if self._turn_qa_buffer:
            msg += "\nAnswers you gave to player questions this turn:\n"
            for entry in self._turn_qa_buffer:
                msg += f"\n[{entry['character_name']}]\n"
                msg += f"  Q: {entry['questions']}\n"
                msg += f"  A: {entry['answer']}\n"

        return msg

    def _build_turn_history_message(self, payload: Dict[str, Any], default_msg: str) -> str:
        """Build a compact, durable history entry for a turn."""
        location = str(payload.get("location") or "").strip()
        start_time = str(payload.get("turn_start_time") or "").strip()
        end_time = str(payload.get("turn_end_time") or "").strip()
        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []

        header_parts: List[str] = []
        if location:
            header_parts.append(f"location: {location}")
        if start_time:
            header_parts.append(f"start: {start_time}")
        if end_time:
            header_parts.append(f"end: {end_time}")
        parts: List[str] = ["Turn | " + " | ".join(header_parts) if header_parts else "Turn"]

        if char_plans:
            parts.append("Final character intentions:")
            for plan in char_plans:
                if not isinstance(plan, dict):
                    continue
                name = str(plan.get("name") or "").strip() or "Unknown"
                intent = str(plan.get("intent") or plan.get("action") or plan.get("decision") or "").strip()
                thoughts = str(plan.get("thoughts") or "").strip()
                line = f"- {name}: {intent or '(no intent)'}"
                if thoughts:
                    line += f"\n  thoughts: {thoughts}"
                parts.append(line)

        return "\n".join(parts)
