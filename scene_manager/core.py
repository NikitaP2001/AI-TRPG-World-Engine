"""Scene Manager agent: reactive, GM-dependent scene lifecycle.

Receives a pre-chosen scene from the Game Master and handles:
- World planning (pre-establish hidden facts)
- Character execution loop
- Turn narration with correction rounds
- Paragraph summarization

Has its own message history and prompt log output.
Does NOT pick scenes РІР‚вЂќ the Game Master does that.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from memory_store import append_message, load_history, limits_from_env
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world import World, WorldDuration, WorldTime, build_game_master_context_block
from scene_manager.tools import (
    get_entity_facts,
    query_events as sm_query_events,
    record_event as sm_record_event,
    get_scene as sm_get_scene,
    get_entity_state,
    update_entity_stat,
    adjust_entity as sm_adjust_entity,
    apply_effect,
    escalate_to_gm as sm_escalate_to_gm,
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


def _format_intent_interval(di: Dict[str, Any]) -> str:
    """Render an intent's timing for display РІР‚вЂќ supports both the legacy free-text
    ``interval`` field and the current numeric ``t_start``/``t_end`` (Р’В§1/Р’В§3.2)."""
    if di.get("t_start") is not None or di.get("t_end") is not None:
        t_start = di.get("t_start")
        t_end = di.get("t_end")
        if t_end is None:
            return f"starts +{t_start}s (persistent subscription)"
        return f"+{t_start}s..+{t_end}s"
    return str(di.get("interval", "")).strip()


def _context_for_narration(context_text: str) -> str:
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


def _parse_perspectives(text: str, character_names: List[str]) -> tuple:
    """Parse SM scene description into per-character perspectives + shared intro.

    Tries multiple section-header formats (``## Name``, ``**Name**``, ``--- Name ---``).
    Returns (personal, shared) on success, or ({}, text) if parsing fails — caller
    then falls back to using the full text as shared.

    Validation: every character_name must have a non-empty, unique perspective.
    """
    char_set = set(str(n).strip() for n in character_names if str(n).strip())
    if not char_set:
        return {}, text

    lines = text.split("\n")
    current_name: Optional[str] = None
    current_lines: List[str] = []
    personal: Dict[str, str] = {}
    shared_lines: List[str] = []
    in_section = False

    # Detect heading markers: ##, **, ---
    heading_chars = ('#', '*', '-')

    def _is_header(line: str) -> Optional[str]:
        s = line.strip()
        if not s:
            return None
        # Exact header match
        for cn in char_set:
            if s in (f"**{cn}**", f"--- {cn} ---", f"## {cn}", f"## {cn} —", cn):
                return cn
        # Lines with heading markers: try substring match
        if any(c in s for c in heading_chars):
            candidates = [cn for cn in char_set if cn in s]
            if len(candidates) == 1:
                return candidates[0]
            # Try positional match: count ## headers and map to char_set order
            if '##' in s:
                # Will be handled by positional fallback below
                return None
        return None

    # First pass: try exact/header match
    for line in lines:
        header_name = _is_header(line)
        if header_name:
            if current_name and current_lines:
                personal[current_name] = "\n".join(current_lines).strip()
            current_name = header_name
            current_lines = []
            in_section = True
        elif in_section:
            current_lines.append(line)
        else:
            shared_lines.append(line)

    if current_name and current_lines:
        personal[current_name] = "\n".join(current_lines).strip()

    shared = "\n".join(shared_lines).strip()

    # Validation: all chars present, non-empty, no duplicates
    if personal and char_set.issubset(set(personal.keys())):
        vals = [v for v in personal.values() if v]
        if len(vals) == len(char_set) and len(vals) == len(set(vals)):
            return personal, shared

    # Fallback: positional match by heading-like lines.
    # Counts ## or deeper (not single # which is shared scene title).
    # Also detects **...** and --- patterns.
    heading_lines = []
    for i, l in enumerate(lines):
        s = l.strip()
        if not s:
            continue
        # H2+ (## or more), or bold (**), or horizontal rule (---)
        if s.startswith('##') or (s.startswith('**') and s.endswith('**')) or s.startswith('---'):
            heading_lines.append((i, l))
    if len(heading_lines) == len(char_set):
        ordered_names = sorted(char_set)
        personal = {}
        shared_lines = lines[:heading_lines[0][0]] if heading_lines else []
        for idx, (line_i, hline) in enumerate(heading_lines):
            content_start = line_i + 1
            content_end = heading_lines[idx + 1][0] if idx + 1 < len(heading_lines) else len(lines)
            body = "\n".join(lines[content_start:content_end]).strip()
            personal[ordered_names[idx]] = body
        shared = "\n".join(shared_lines).strip()
        if personal and all(v for v in personal.values()):
            return personal, shared

    return {}, text


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
        self._session_context_buffer: List[Dict[str, Any]] = []
        # Thresholds: session buffer content before forcing publish
        self._session_buffer_publish_chars: int = 3000    # ~600-900 tokens
        self._session_buffer_force_chars: int = 60000     # ~15-20K tokens session safety cap
        self._session_messages: List[BaseMessage] = []
        self._session_llm: Any = None
        self._world = World()
        self._meta = HistoryMeta(
            (self._history_path.parent / "scene_manager_meta.json")
            if self._history_path else Path("scene_manager_meta.json"),
        )

    def begin_session(self) -> None:
        """Start a new session. All SM ops share one context."""
        self._session_messages = [
            SystemMessage(content=self._prompt_text),
            *self._pinned_summaries(),
        ]
        self._session_llm = None

    def end_session(self) -> None:
        """End session. Clears transient state."""
        self._session_messages = []
        self._session_llm = None

    def _get_session_llm(self):
        """Lazy-create the session-level LLM (no tool binding)."""
        if self._session_llm is None:
            from openrouter_llm import build_openrouter_chat_llm
            self._session_llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-scene-manager",
                max_tokens=2000,
                parallel_tool_calls=False,
            )
        return self._session_llm
    def perception_check(self, matched_intent: Any, observer: str) -> bool:
        """Р’В§3.3 / Р’В§8 perception gate РІР‚вЂќ narrow structured query, NOT open narrative judgment.

        Called by the segment scheduler (``scheduler/timeline.py``) only for structural
        matches on a ``visibility == "private"`` intent. ``matched_intent`` is a
        ``scheduler.timeline.Intent``. Returns True if ``observer`` perceives the
        concealed action.
        """
        actor = str(getattr(matched_intent, "actor", "") or "").strip()
        description = str(getattr(matched_intent, "description", "") or "").strip()
        dd = getattr(matched_intent, "detection_difficulty", None)
        dd_val = dd if dd is not None else 10

        @tool
        def perception_result(detected: bool) -> str:
            """Report whether the observer notices the concealed action.

            Args:
                detected: True if the observer would plausibly notice this action
                    given the scene and its detection_difficulty.
            """
            return "ok"

        human_msg = (
            "Perception check РІР‚вЂќ structural query, answer with the tool only.\n"
            f"Observer: {observer}\n"
            f"Concealed actor: {actor}\n"
            f"Concealed action: {description[:200]}\n"
            f"detection_difficulty: {dd_val}\n"

        )
        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="perception_check")
        try:
            bound = self._get_session_llm().bind_tools(
                [perception_result], tool_choice="required",
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound = self._get_session_llm().bind_tools([perception_result]).with_config({"callbacks": callbacks})

        messages = list(self._session_messages) + [HumanMessage(content=human_msg)]
        for _attempt in range(10):
            try:
                out = bound.invoke(messages)
            except Exception:
                if _attempt == 9:
                    raise
                continue
            tcs = _extract_tool_calls(out)
            if tcs:
                args = tcs[0].get("args") if isinstance(tcs[0], dict) else {}
                if isinstance(args, dict) and "detected" in args:
                    return bool(args.get("detected"))
        raise RuntimeError("perception_check: exhausted 10 attempts without valid tool call")
        return False  # conservative default: concealment holds when the check can't run

    def call_gm(self, notice: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke the Game Master on-demand via ReAct loop.

        Args:
            notice: Text explaining why GM was called.
            context: Dict with world_time, players list, etc.

        Returns:
            Dict with exit_tool, exit_args, thinking from the GM.
        """
        from gm.game_master import GameMaster
        # Use a fresh GameMaster instance РІР‚вЂќ history is loaded from disk.
        gm = GameMaster(
            history_path=self._history_path.parent / "game_master_messages.json"
            if self._history_path else None,
        )
        return gm.call_gm(notice=notice, context=context)

    def resolve_player_question(
        self,
        *,
        player_name: str,
        questions: str,
    ) -> str:
        """Answer a player's question from the scene."""
        from world_manager.tool_registry import reset_doc_read_tracker
        reset_doc_read_tracker("sm")

        from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks
        from scene_manager.tools import (
            get_entity_facts, get_entity_state, get_scene, adjust_entity,
            record_event, query_events,
            close_invocation,
            set_effect, remove_effect,
            escalate_to_gm,
            read_tool_doc,
        )
        from scene_manager.tools import manage_intent, update_awareness, proceed_to_narration

        llm = build_openrouter_chat_llm(
            temperature=0.5,
            streaming=True,
            title_suffix="-scene-manager-qa",
            max_tokens=1000,
            parallel_tool_calls=True,
        )

        tools = [
            get_entity_facts, get_entity_state, get_scene, adjust_entity,
            record_event, query_events,
            close_invocation,
            set_effect, remove_effect,
            escalate_to_gm,
            read_tool_doc,
            manage_intent, update_awareness, proceed_to_narration,
        ]

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="resolve_player_question")

        try:
            bound = llm.bind_tools(
                tools,
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound = llm.bind_tools(tools).with_config({"callbacks": callbacks})

        task_msg = (
            f"Player \"{player_name}\" asks the scene:\n{questions}"
        )

        # Append to turn conversation
        self._session_messages.append(HumanMessage(content=task_msg))

        has_pending_ids = "[observe:" in questions or "[recall:" in questions

        for _attempt in range(10):
            try:
                out = bound.invoke(self._session_messages)
            except Exception:
                if _attempt == 9:
                    raise
                continue

            tcs = getattr(out, "tool_calls", None) or []
            if not tcs:
                # SM answered in text directly РІР‚вЂќ use that
                text = str(getattr(out, "content", "") or "").strip()
                if text:
                    self._session_messages.append(AIMessage(content=text))
                    self._session_context_buffer.append({
                        "type": "qa",
                        "content": f"{player_name}: {questions}\nAnswer: {text[:500]}",
                    })
                    if has_pending_ids:
                        # Return as structured dict: extract pending_ids from questions
                        ids = re.findall(r'\[(observe|recall):([^\]]+)\]', questions)
                        return json.dumps({pid: text for _, pid in ids}, ensure_ascii=False)
                    return text
                continue

            # Process all tool calls in the batch (parallel_tool_calls=True support)
            final_answer = None

            for tc in tcs:
                name = str(tc.get("name") or "").strip()
                args = tc.get("args") if isinstance(tc, dict) else {}
                if not isinstance(args, dict):
                    continue

                if name == "escalate_to_gm":
                    notice = str(args.get("question") or questions).strip()
                    result = self.call_gm(notice)
                    gm_answer = str(result.get("exit_args") or result.get("thinking") or "No answer.").strip()
                    self._session_messages.append(AIMessage(content=gm_answer))
                    self._session_context_buffer.append({
                        "type": "qa",
                        "content": f"{player_name}: {questions}\nAnswer: {gm_answer[:500]}",
                    })
                    if has_pending_ids:
                        ids = re.findall(r'\[(observe|recall):([^\]]+)\]', questions)
                        return json.dumps({pid: gm_answer for _, pid in ids}, ensure_ascii=False)
                    return gm_answer

                _qa_tool_map = {
                    "get_entity_facts": get_entity_facts,
                    "get_entity_state": get_entity_state,
                    "get_scene": get_scene,
                    "adjust_entity": adjust_entity,
                    "record_event": record_event,
                    "query_events": query_events,
                    "close_invocation": close_invocation,
                    "set_effect": set_effect,
                    "remove_effect": remove_effect,
                    "manage_intent": manage_intent,
                    "update_awareness": update_awareness,
                    "proceed_to_narration": proceed_to_narration,
                }
                fn = _qa_tool_map.get(name)
                if fn:
                    try:
                        tool_result = fn.invoke(args)
                        result_str = json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else str(tool_result)
                    except Exception as e:
                        result_str = f"Tool error: {e}"
                    tc_id = str(tc.get("id") or f"qa_{_attempt}") if isinstance(tc, dict) else f"qa_{_attempt}"
                    self._session_messages.append(AIMessage(content=str(getattr(out, "content", "") or ""), tool_calls=[tc]))
                    self._session_messages.append(ToolMessage(content=result_str, tool_call_id=tc_id, name=name))
                    continue

            # If some tools were executed, continue the loop for more SM processing
            continue

        self._session_context_buffer.append({
            "type": "qa",
            "content": f"{player_name}: {questions} [exhausted retries вЂ” no answer]",
        })
        raise RuntimeError(f"resolve_player_question: exhausted 10 attempts for player '{player_name}'")

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
        return {"players": chars, "npcs": npcs}

    def determine_next_scene(self, last_scene_location: str) -> Dict[str, Any]:
        """Find all characters who need a scene (haven't acted) and group them.

        Scene = characters who need to act. No location validation.
        Returns:
            {"type": "auto", "location": str, "character_names": [str],
             "npc_names": [str], "scene_description": ""}
            or {"type": "no_scene", "reason": "..."}
        """
        try:
            chars = self._world.get_players()
            wt = self._world.get_world_time()
            wt_sec = wt.to_seconds()

            # Check for next_scene_hint from end_scene tool
            next_hint = str(self._world.get_info().get("next_scene_hint") or "").strip()
            if next_hint:
                # Clear the hint so it's only used once
                try:
                    from world.io import write_json
                    info["next_scene_hint"] = ""
                    write_json(self._world.paths.info_json, info)
                except Exception:
                    pass
                # Try to find this location
                locs = self._world.get_locations()
                if next_hint in locs:
                    # Gather all characters at or near this location
                    all_at_loc = self.get_entities_at_location(next_hint)
                    all_names = all_at_loc["players"]
                    if all_names:
                        return {
                            "type": "auto",
                            "location": next_hint,
                            "character_names": all_names,
                            "npc_names": all_at_loc["npcs"],
                            "scene_description": "",
                        }

            # Group characters by location
            location_groups: Dict[str, List[str]] = {}
            for ch in chars:
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                loc = str(ch.get("location") or "").strip()
                if not name or not loc:
                    continue
                location_groups.setdefault(loc, []).append(name)

            if not location_groups:
                return {"type": "no_scene", "reason": "no_characters"}

            # Pick the largest group (all characters at the same location resume together)
            target_loc = max(location_groups, key=lambda k: len(location_groups[k]))
            target_chars = location_groups[target_loc]

            # Include other entities at this location
            all_at_loc = self.get_entities_at_location(target_loc)
            all_char_names = list(dict.fromkeys(target_chars + all_at_loc["players"]))

            if not all_char_names:
                return {"type": "no_scene", "reason": "no characters available"}

            return {
                "type": "auto",
                "location": target_loc,
                "character_names": all_char_names,
                "npc_names": all_at_loc["npcs"],
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
    ) -> Dict[str, Any]:
        """Produce a scene description when players move to a new location.

        Calls the SM's LLM with a minimal scene description request.
        Returns dict with shared, personal_json, etc.
        """
        from scene_manager.tools import get_scene as sm_get_scene

        # Pre-fetch location data so SM has everything it needs РІР‚вЂќ no tool binding needed
        location_data: Dict[str, Any] = {}
        try:
            location_data = sm_get_scene.invoke({"location_id": location})
        except Exception:
            pass

        import json as _json
        location_json = _json.dumps(location_data, ensure_ascii=False, indent=2)[:2000]

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="describe_scene", stream=True)
        human_msg = (
            f"Location: {location}\n"
            f"Characters: {', '.join(character_names)}\n"
            f"NPCs present: {', '.join(npc_names or []) or 'none'}\n\n"
            f"Location data:\n{location_json}\n\n"
            f"Describe this scene from each character's perspective."
        )

        # Append to turn conversation so SM sees context
        self._session_messages.append(HumanMessage(content=human_msg))

        for _attempt in range(10):
            try:
                out = self._get_session_llm().with_config({"callbacks": callbacks}).invoke(self._session_messages)
            except Exception:
                if _attempt == 9:
                    raise
                continue

            # Parse per-character descriptions from SM output
            thinking = str(getattr(out, "content", "") or "").strip()
            if thinking:
                # Persist SM response in turn conversation
                self._session_messages.append(AIMessage(content=thinking))

                personal, _shared = _parse_perspectives(thinking, character_names)
                if personal:
                    # Per-character descriptions only — no shared (avoids meta leaks)
                    combined = "\n\n".join(
                        f"{desc}" for name in character_names
                        for desc in [personal.get(name, "")] if desc
                    )
                    return {"personal": personal, "combined": combined, "horizon_t": "15m"}
                # Parsing failed — fall back to first valid personal if possible
                return {"personal": {}, "combined": "", "horizon_t": "15m"}

        raise RuntimeError(f"describe_scene: exhausted 10 attempts for location '{location}'")

    def run_summary_task(
        self,
        *,
        task_type: str,
        last_ref: str = "",
        label: str = "",
    ) -> Optional[str]:
        """Run a summarization task: paragraph or arc.

        Builds a request referencing only interaction entries since last summary.
        No big context dump РІР‚вЂќ the model already has full details in its history.

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

    def add_resolved_segments_to_buffer(self, resolved_entries: List[Any]) -> None:
        """Push scheduler-resolved segments into the cycle context buffer.

        Args:
            resolved_entries: list of ResolvedEntry from SegmentScheduler.run().
                              Each entry has .actor, .action_type, .description,
                              .t_start, .t_end, .kind, .perceivers.
        """
        for entry in resolved_entries:
            desc = str(getattr(entry, "description", "") or "").strip()
            if not desc:
                continue
            actor = str(getattr(entry, "actor", "?") or "?")
            atype = str(getattr(entry, "action_type", "?") or "?")
            target = str(getattr(entry, "target", "?") or "?")
            kind = str(getattr(entry, "kind", "action") or "action")
            content = f"{actor}: [{atype}] -> {target}: {desc[:300]}"
            self._session_context_buffer.append({
                "type": "segment",
                "kind": kind,
                "content": content,
            })

    def clear_session_context_buffer(self) -> None:
        """Clear the session context buffer after publication."""
        self._session_context_buffer.clear()

    def run_narration(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run narration using tool calls that return narrations and duration."""
        from world_manager.tool_registry import reset_doc_read_tracker, clear_current_agent
        reset_doc_read_tracker("sm")

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
        from scene_manager.tools import (
            get_entity_facts, get_entity_state, get_scene, adjust_entity,
            record_event, query_events,
            close_invocation,
            set_effect, remove_effect,
            escalate_to_gm,
            read_tool_doc,
        )
        from scene_manager.tools import manage_intent, update_awareness, publish_narrative_block, end_scene

        human_msg = self._build_narration_message(payload)

        task_context = _context_for_narration(context_text)
        ctx_note = ("\n\n---\n" + task_context.strip()) if task_context and task_context.strip() else ""

        self._session_messages.append(HumanMessage(content=human_msg + ctx_note))

        narration_tools = [
            publish_narrative_block, end_scene, escalate_to_gm,
            get_entity_facts, get_entity_state, get_scene, adjust_entity,
            record_event, query_events,
            close_invocation,
            set_effect, remove_effect,
            manage_intent, update_awareness,
            read_tool_doc,
        ]

        try:
            bound_llm = self._get_session_llm().bind_tools(narration_tools).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = self._get_session_llm().bind_tools(narration_tools).with_config({"callbacks": callbacks})

        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        selected_characters = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]

        _coerced_dur: Optional[str] = None
        _shared_narration: str = ""
        _personal_narration: Dict[str, str] = {}

        max_retries = 10
        for _attempt in range(max_retries):
            try:
                out = bound_llm.invoke(self._session_messages)
            except Exception as exc:
                if logs_enabled():
                    print(f"[trace] SM run_narration: LLM error, retrying: {exc}")
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
                    print(f"[trace] SM run_narration: no tool call, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(
                    self._session_messages, out,
                    "ERROR: You MUST call publish_narrative_block or escalate_to_gm."
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

            if tool_name0 == "escalate_to_gm":
                question = str(args0.get("question") or "").strip()
                context_str = str(args0.get("context") or "").strip()
                if not question:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: empty escalate_to_gm question, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        "ERROR: escalate_to_gm requires non-empty question.",
                    )
                    continue
                # Actually call GM and get the answer
                if logs_enabled():
                    print(f"[trace] SM escalate_to_gm: {question[:120]}")
                gm_result = self.call_gm(question, context={"scene_context": context_str} if context_str else None)
                gm_answer = str(gm_result.get("exit_args") or gm_result.get("thinking") or "No answer from GM.").strip()
                if logs_enabled():
                    print(f"[trace] GM answer: {gm_answer[:200]}")
                # Append GM response to turn messages so SM can use it
                self._session_messages.append(AIMessage(content=f"[GM answer]\n{gm_answer}"))
                # Continue the loop РІР‚вЂќ SM can now publish with the GM's answer incorporated
                continue

            if tool_name0 == "publish_narrative_block":
                # Primary narration path РІР‚вЂќ SM submits narrative block via this tool.
                narration = str(args0.get("narration") or "").strip()
                if not narration:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: empty narration, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        "ERROR: narration is empty.",
                    )
                    continue
                raw_duration = str(args0.get("duration") or "").strip()
                if not raw_duration:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: no duration, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        "ERROR: duration is required. Specify how much in-game time this block covers (format: number + unit).",
                    )
                    continue
                _coerced_dur = _coerce_duration(raw_duration)
                if _coerced_dur is None:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: unparseable duration {raw_duration!r}, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        f"ERROR: duration '{raw_duration}' is not valid. Use format like '15m', '2h', '30s'.",
                    )
                    continue
                _shared0 = narration
                _selected_set0 = set(selected_characters)
                _shared_narration = _shared0
                _personal_narration = {}
                break

            if tool_name0 == "end_scene":
                # SM explicitly ends the scene РІР‚вЂќ scene clears, next cycle bootstraps.
                reason = str(args0.get("reason") or "").strip()
                final_narration = str(args0.get("final_narration") or "").strip()
                raw_duration = str(args0.get("duration") or "").strip()
                coerced_dur = _coerce_duration(raw_duration) if raw_duration else None
                return {
                    "type": "end_scene",
                    "reason": reason,
                    "narration": final_narration,
                    "duration": coerced_dur or "",
                    "next_location": str(args0.get("next_location") or "").strip(),
                }

            if tool_name0 == "record_event":
                # Legacy fallback РІР‚вЂќ prefer publish_narrative_block instead.
                description = str(args0.get("description") or "").strip()
                if not description:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: empty description, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        "ERROR: description is empty.",
                    )
                    continue
                raw_duration = str(args0.get("tag") or "").strip()
                _coerced_dur = _coerce_duration(raw_duration) if raw_duration else None
                if _coerced_dur is None:
                    if logs_enabled():
                        print(f"[trace] SM run_narration: record_event without duration, retrying")
                    _append_tool_rejection(
                        self._session_messages, out,
                        "ERROR: record_event requires a duration in the 'tag' field. "
                        "Prefer publish_narrative_block instead.",
                    )
                    continue
                _shared0 = description
                _selected_set0 = set(selected_characters)
                _shared_narration = _shared0
                _personal_narration = {}
                break

            if tool_name0 != "turn_narration":
                if logs_enabled():
                    print(f"[trace] SM run_narration: unexpected tool {tool_name0!r}, retrying")
                _append_tool_rejection(
                    self._session_messages, out,
                    "ERROR: Use publish_narrative_block or escalate_to_gm.",
                )
                continue

            # Old turn_narration handling РІР‚вЂќ prefer publish_narrative_block instead.
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
                    print(f"[trace] SM run_narration: empty description, retrying")
                _append_tool_rejection(self._session_messages, out, "ERROR: description is empty. Provide non-empty description.")
                continue

            _coerced_dur = _coerce_duration(duration0)
            if _coerced_dur is None:
                if logs_enabled():
                    print(f"[trace] SM run_narration: no duration, retrying")
                _append_tool_rejection(
                    self._session_messages, out,
                    "ERROR: duration is required. Prefer publish_narrative_block.",
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
            raise ValueError(
                "SM run_narration: exhausted retries. SM must call "
                "publish_narrative_block or escalate_to_gm."
            )

        duration = _coerced_dur
        world_facts = ""

        # Per-character narration only — no shared narration (avoids info leaks)
        _per_player_narratives: Dict[str, str] = {}
        for cname in selected_characters:
            parts: List[str] = []
            if cycle_end_time:
                parts.append(cycle_end_time)
            personal = _personal_narration.get(cname, "").strip()
            if personal:
                parts.append(personal)
            if parts:
                _per_player_narratives[cname] = "\n\n".join(parts)

        combined_narration = "\n\n".join(
            f"[{n}]\n{_per_player_narratives[n]}"
            for n in selected_characters if n in _per_player_narratives
        )

        history_payload = dict(payload) if isinstance(payload, dict) else {}
        cycle_end_time = ""
        try:
            start_time = str(history_payload.get("cycle_start_time") or "").strip()
            if start_time and duration:
                start_wt = WorldTime.parse(start_time)
                dur = WorldDuration.parse_user_input(duration)
                cycle_end_time = start_wt.add_duration(dur).to_string()
        except Exception:
            cycle_end_time = ""
        if cycle_end_time:
            history_payload["cycle_end_time"] = cycle_end_time

        history_user_msg = self._build_history_message(history_payload, human_msg)

        if self._history_path and combined_narration:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            self._meta.append_entry("interaction", f"narration at {history_payload.get('location','?')}")
            assistant_parts: List[str] = []
            if cycle_end_time:
                assistant_parts.append(f"Cycle end time: {cycle_end_time}")
            if duration:
                assistant_parts.append(f"Cycle duration: {duration}")
            assistant_parts.append(combined_narration)
            assistant_mem = "\n\n".join(assistant_parts)
            append_message(self._history_path, role="assistant", content=assistant_mem, limits=limits)
            self._meta.append_entry("interaction", f"narrated cycle ({duration}, {len(selected_characters)} chars)")

        if combined_narration:
            self._session_context_buffer.clear()

        return {
            "type": "narration",
            "narration": combined_narration,
            "personal_narration": dict(_per_player_narratives),
            "duration": duration,
            "world_facts": world_facts,
        }

    def _build_narration_message(self, payload: Dict[str, Any]) -> str:
        """Build the human message for turn narration."""
        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        participant_names = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]
        round_history = payload.get("turn_round_history") if isinstance(payload.get("turn_round_history"), list) else []

        msg = "Turn narration:\n"

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

        # Declared intents (structured РІР‚вЂќ from declare_intent tool)
        declared = payload.get("declared_intents") if isinstance(payload.get("declared_intents"), list) else []
        if declared:
            msg += "\nDeclared intents (all participants, unsorted):\n"
            for di in declared:
                actor = di.get("actor", "?")
                atype = di.get("action_type", "?")
                target = di.get("target", "?")
                desc = str(di.get("description", "")).strip()
                cond = di.get("trigger_condition") or ""
                interval = _format_intent_interval(di)
                priority = di.get("trigger_priority", 0)
                on_trig = di.get("on_trigger", "")
                visibility = str(di.get("visibility") or "public")
                parts = [f"  {actor}: [{atype}] -> {target}" + (" (private)" if visibility == "private" else "")]
                if desc:
                    parts.append(f"    Intent: {desc}")
                if isinstance(cond, dict):
                    parts.append(f"    Trigger: {cond.get('actor','?')}/{cond.get('action_type','?')}->{cond.get('target_match','?')}")
                elif cond:
                    parts.append(f"    Trigger: {cond}")
                if interval:
                    parts.append(f"    Timing: {interval}")
                if priority:
                    parts.append(f"    Priority: {priority}")
                if on_trig:
                    parts.append(f"    On trigger: {on_trig}")
                if di.get("is_hostile"):
                    parts.append("    Hostile: true")
                msg += "\n".join(parts) + "\n"

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

        # РІвЂќР‚РІвЂќР‚ Inject cycle context buffer into narration prompt РІвЂќР‚РІвЂќР‚
        buffer_text = self._build_session_buffer_summary()
        if buffer_text:
            msg += f"\n--- Session context (unpublished) ---\n{buffer_text}\n--- end session context ---\n"

        return msg

    def _build_resolve_message(self, payload: Dict[str, Any]) -> str:
        """Build the resolve message for SM РІР‚вЂќ review intents, decide next step."""
        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        participant_names = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]
        declared = payload.get("declared_intents") if isinstance(payload.get("declared_intents"), list) else []

        msg = (
            "Review all participant intents for this turn cycle.\n"
            "Decide: are all intents compatible, or do some players need to react "
            "to new information or correct their actions?\n\n"
        )

        if participant_names:
            msg += f"Participants: {', '.join(participant_names)}\n\n"

        if declared:
            msg += "Declared intents (all participants):\n"
            for di in declared:
                actor = di.get("actor", "?")
                atype = di.get("action_type", "?")
                target = di.get("target", "?")
                desc = str(di.get("description", "")).strip()
                cond = di.get("trigger_condition") or ""
                interval = str(di.get("interval", "")).strip()
                priority = di.get("trigger_priority", 0)
                on_trigger = di.get("on_trigger", "")
                reaction = str(di.get("prepared_reaction", "")).strip()
                parts = [f"  {actor}: [{atype}] -> {target}"]
                if desc:
                    parts.append(f"    {desc}")
                if cond:
                    if isinstance(cond, dict):
                        parts.append(f"    Trigger: {cond.get('actor','?')}/{cond.get('action_type','?')}->{cond.get('target_match','?')}")
                    else:
                        parts.append(f"    Trigger: {cond}")
                if interval:
                    parts.append(f"    Duration: {interval}")
                if priority:
                    parts.append(f"    Priority: {priority}")
                if on_trigger:
                    parts.append(f"    On trigger: {on_trigger}")
                if reaction:
                    parts.append(f"    Prepared: {reaction}")
                msg += "\n".join(parts) + "\n"

        if char_plans:
            msg += "\nCommit descriptions:\n"
            for plan in char_plans:
                if isinstance(plan, dict):
                    name = plan.get("name") or "?"
                    action = plan.get("intent") or plan.get("action") or "acts"
                    thoughts = str(plan.get("thoughts") or "").strip()
                    msg += f"\n{name}: {action}\n"
                    if thoughts:
                        msg += f"  (internal: {thoughts[:200]})\n"

        # Append timeline segments if available
        timeline_text = str(payload.get("timeline_text") or "").strip()
        if timeline_text:
            msg += f"\n---\n{timeline_text}\n---\n"

        return msg

    def session_buffer_chars(self) -> int:
        """Return estimated character count of accumulated session context."""
        return sum(len(str(e.get("content", ""))) for e in self._session_context_buffer)

    def is_session_buffer_ready(self) -> bool:
        """Check if session context buffer has crossed the publication threshold."""
        return self.session_buffer_chars() >= self._session_buffer_publish_chars

    def is_session_buffer_force(self) -> bool:
        """Check if session safety cap is reached (force publish regardless)."""
        return self.session_buffer_chars() >= self._session_buffer_force_chars

    def _build_session_buffer_summary(self) -> str:
        """Build a compact summary of accumulated session context."""
        if not self._session_context_buffer:
            return ""
        parts: List[str] = []
        for entry in self._session_context_buffer:
            etype = entry.get("type", "note")
            content = str(entry.get("content", "")).strip()
            if not content:
                continue
            if etype == "qa":
                parts.append(f"[QA] {content}")
            elif etype == "segment":
                parts.append(f"[Segment] {content}")
            elif etype == "escalation":
                parts.append(f"[Escalation] {content}")
            elif etype == "interrupt":
                parts.append(f"[Interrupt] {content}")
            else:
                parts.append(content)
        return "\n".join(parts)

    def _build_history_message(self, payload: Dict[str, Any], default_msg: str) -> str:
        """Build a compact, durable history entry for a turn."""
        location = str(payload.get("location") or "").strip()
        start_time = str(payload.get("cycle_start_time") or "").strip()
        end_time = str(payload.get("cycle_end_time") or "").strip()
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
