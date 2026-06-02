"""Game Master agent: Narrative generation without tools.

This is one of two main LLM agents in the system:

1. Storage Assistant (gm/operator.py):
    - ReAct agent with tools (run_scene + storage maintenance)
   - Manages simulation state and scene flow
    - Persistent history in game/storage_assistant_messages.json
    - Context: atomic marker-based deltas in persistent history
    - Prompt: agents/storage_assistant/prompt.txt

2. Game Master (this file):
   - Narrative agent for creative writing (world seed, scene descriptions, narration)
   - Maintains roleplay identity with persistent conversation history
   - History stored in game/game_master_messages.json
   - Context: build_game_master_context_block() without iteration mechanics
   - Prompt: agents/game_master/prompt.txt
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from memory_store import append_message, load_history, limits_from_env, HistoryLimits
from openrouter_langchain_logging import logs_enabled, enable_direct_text_abort, disable_direct_text_abort
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from stream_watchdog import clear_detected_invalid_pattern, get_detected_invalid_pattern, _clear_watchdog_abort
from world import World, WorldTime, WorldDuration, build_game_master_context_block, build_game_master_qa_context as _build_qa_context


@tool
def gm_scene_description(
    player_names: List[str],
    location: str,
    npc_names: Optional[List[str]] = None,
    time_shift: str = "0",
    shared: str = "",
    personal_json: str = "",
    names_order_reasoning: str = "",
) -> str:
    """Return selected scene metadata and scene description in one call.

    Args:
        player_names: Players to include in the scene.
        location: Location name for the scene (existing or new if narratively needed).
        npc_names: Optional NPC names to include.
        time_shift: Optional skipped time before the scene starts. Default is "0".
        shared: 3rd-person scene description - perception intersection, covering what every player can observe
            atmosphere, environment, NPC activity, sensory details. No secrets or private info.
        personal_json: Optional JSON object mapping player names to their exclusive personal
            additions — only what THAT player alone perceived, not present in shared.
            Written in 2nd person ("you"). Omit or pass "" if nothing is private.
            Example: {"Alice": "You notice the guard's gaze lingers on you specifically."}
        names_order_reasoning: Reasoning for player_names ordering.
    """
    return "ok"


@tool
def gm_turn_narration(shared: str, duration: str, personal_json: str = "", world_facts: str = "") -> str:
    """Submit turn narration: shared outcome visible to all plus optional per-player personal parts.

    Args:
        shared: 3rd-person narrative of what happened — all events visible/audible to everyone
            present. Contains no private or secret information.
        duration: Elapsed time (e.g. "30s", "5m", "2h")
        personal_json: Optional JSON object mapping player names to personal additions —
            only what THAT player exclusively experienced/perceived, not in shared.
            Written in 2nd person ("you"). Omit or pass "" if nothing is private.
            Example: {"Alice": "You feel a sharp chill run down your spine."}
        world_facts: Off-camera canonical world facts — dry, encyclopedic entries about
            what exists outside the current scene, unknown to players.
            
    """
    return "ok"


@tool
def gm_correct_character_intents(character_name: str, turn_insight: str) -> str:
    """Ask one character to revise an impossible or contradictory intent before final narration.

    Args:
        character_name: Exact participant name whose current intent must be revised.
        turn_insight: In-world, character-facing notice that explains what they perceive
            now and why their declared intent should be corrected.
    """
    return "ok"


@tool
def gm_world_seed_result(world_time: str, seed_text: str) -> str:
    """Return world-seed output with validated world time as a separate parameter.

    Args:
        world_time: Exact world datetime in format Y0000-01-01 00:00:00.
        seed_text: Seed body text containing LOCATIONS and PLAYER LOCATIONS sections.
            Do not include WORLD TIME in this text block.
    """
    return "ok"


_DURATION_EXTRACT_RE = re.compile(
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\b",
    flags=re.IGNORECASE,
)


def _coerce_duration(raw: str) -> Optional[str]:
    """Normalize a messy GM-supplied duration string.

    Accepts exact values ('5m', '30 seconds') directly.  Falls back to
    extracting the first valid number+unit from text like 'about 5 minutes'
    or '5-10 minutes'.  Returns None if no valid duration can be found.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    # Fast path: already valid.
    try:
        WorldDuration.parse_user_input(raw)
        return raw
    except ValueError:
        pass
    # Extract first number+unit from messy text.
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
    def _normalize(tc: Any) -> Optional[Dict[str, Any]]:
        if isinstance(tc, dict):
            return tc
        try:
            name = getattr(tc, "name", None)
            args = getattr(tc, "args", None)
            if name is not None:
                out: Dict[str, Any] = {"name": str(name)}
                if isinstance(args, dict):
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
    """Append an AI tool-call response + synthetic ToolMessage(s) so the message sequence stays valid.

    Providers require that every AIMessage with tool_calls is immediately followed by a
    ToolMessage for each tool_call_id.  Appending a plain HumanMessage instead causes a
    400 'insufficient tool messages' error.

    If ``out`` has no tool_calls (plain-text response), falls back to a plain HumanMessage
    because no ToolMessage pairing is needed in that case.
    """
    messages.append(out)
    tcs = getattr(out, "tool_calls", None) or []
    if not isinstance(tcs, list):
        tcs = []
    # Also check additional_kwargs in case tool_calls are stored there.
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
        # No tool_calls present — a plain correction HumanMessage is valid.
        messages.append(HumanMessage(content=error_text))


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


def _context_for_turn_narration(context_text: str) -> str:
    return str(context_text or "")

class GameMaster:
    """Game Master agent: roleplay and narration only, no tools.
    
    Maintains persistent conversation history to preserve roleplay identity across narrative tasks.
    Each task is added as a user message, responses are saved as assistant messages.
    """

    def __init__(self, *, prompt_path: str = "agents/game_master/prompt.txt", history_path: Optional[Path] = None) -> None:
        self._prompt_text = read_prompt_text(prompt_path)
        self._temperature = 0.7
        self._history_path = history_path
        # In-memory Q&A log for the current active turn.
        # Accumulates answers given to players during character execution;
        # visible to subsequent ANSWER_QUESTION calls and to TURN_NARRATION.
        # Cleared after a successful narration.
        self._turn_qa_buffer: List[Dict[str, str]] = []

    def clear_turn_qa_buffer(self) -> None:
        """Discard all Q&A entries from the current turn buffer."""
        self._turn_qa_buffer.clear()

    def inject_delta(self, content: str) -> None:
        """Inject a standalone world-state delta message into GM history.

        These messages accumulate world-state changes (paragraph summaries, fact
        updates, etc.) so that the stable history prefix carries up-to-date context
        without needing to re-attach the full context blob on every task call.
        No LLM is invoked — the message is written directly to the history file.
        """
        if not content or not content.strip():
            return
        if not self._history_path:
            return
        limits = limits_from_env()
        append_message(self._history_path, role="user", content=content.strip(), limits=limits)

    def run_task(
        self,
        *,
        task: str,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[ChatOpenAI] = None,
        ephemeral: bool = False,
    ) -> str:
        """Run a narrative task with persistent conversation history.
        
        Each task is added as a user message to the conversation history.
        The GM's response is saved as an assistant message, preserving roleplay continuity.
        
        The context_text should be built using build_game_master_context() which uses
        build_game_master_context_block() - a narrative-friendly context that excludes
        iteration mechanics and planning details (unlike the Storage Assistant context).
        
        Args:
            task: Task identifier (WORLD_SEED, SCENE_DESCRIPTION, TURN_NARRATION, ANSWER_QUESTION)
            payload: Task-specific data
            context_text: Narrative context from build_game_master_context()
            llm: Optional LLM instance (creates default if None)
            ephemeral: If True, skip saving the exchange to GM history. Use for
                transient queries (e.g. per-character questions) that must not
                accumulate in the GM context.
            
        Returns:
            Raw text output from the LLM
        """
        if llm is None:
            # ANSWER_QUESTION and SCENE_DESCRIPTION tasks need less space
            max_tokens = 800 if task == "ANSWER_QUESTION" else (1500 if task == "SCENE_DESCRIPTION" else 1200)
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-game-master",
                max_tokens=max_tokens,
                parallel_tool_calls=False,
            )

        llm = llm.with_config({"callbacks": openrouter_logging_callbacks(scope="game_master", label=task.lower())})

        if str(task or "").strip().upper() == "ANSWER_QUESTION" and context_text:
            assert "# World snapshot" not in context_text, (
                "ANSWER_QUESTION must use minimal QA context (build_game_master_qa_context), "
                "not the full world snapshot builder."
            )

        # Build natural language message based on task type
        human_msg = self._task_to_message(task, payload)

        # Load conversation history
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

        # Context is appended to the task message so the history prefix is stable
        # and eligible for DeepSeek prefix caching on every call.
        ctx_note = ("\n\n---\n" + context_text.strip()) if context_text and context_text.strip() else ""
        messages = [
            SystemMessage(content=self._prompt_text),
            *history_msgs,
            HumanMessage(content=human_msg + ctx_note),
        ]

        out = llm.invoke(messages)
        response = str(getattr(out, "content", "") or "").strip()
        
        history_user_msg = self._task_to_history_message(task, payload, human_msg)

        # Save to conversation history only on success.
        # PARAGRAPH_SUMMARY is bookkeeping, not narrative — skip history.
        # ephemeral=True callers (e.g. per-character Q&A) must not bloat GM context.
        if not ephemeral and task != "PARAGRAPH_SUMMARY" and self._history_path and response:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            append_message(self._history_path, role="assistant", content=response, limits=limits)

        # Accumulate Q&A in the turn buffer regardless of ephemeral flag.
        # This keeps all per-character answers visible within the same turn
        # for subsequent ANSWER_QUESTION calls and for TURN_NARRATION.
        if task == "ANSWER_QUESTION" and response:
            self._turn_qa_buffer.append({
                "character_name": str(payload.get("character_name") or "Unknown").strip(),
                "questions": str(payload.get("questions") or "").strip(),
                "answer": response,
            })
        
        return response

    def ask_ephemeral(self, *, question: str, context_text: str) -> str:
        """Ask the GM a one-off question without saving to history.

        The question and answer are never persisted — the GM will not
        remember them on subsequent invocations.
        """
        llm = build_openrouter_chat_llm(
            temperature=float(self._temperature),
            streaming=True,
            title_suffix="-game-master",
            max_tokens=800,
            parallel_tool_calls=False,
        )
        llm = llm.with_config({"callbacks": openrouter_logging_callbacks(scope="game_master", label="ephemeral_qa")})

        # Load existing history (read-only) for continuity.
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

        if context_text:
            assert "# World snapshot" not in context_text, (
                "Ephemeral GM questions must use minimal scene context, not full world-snapshot context."
            )

        ctx_note = ("\n\n---\n" + context_text.strip()) if context_text and context_text.strip() else ""
        messages = [
            SystemMessage(content=self._prompt_text),
            *history_msgs,
            HumanMessage(content=(
                "Out-of-character debug question from the operator (not a player).\n"
                "Answer concisely based on your knowledge of the world state.\n\n"
                + question
                + ctx_note
            )),
        ]

        out = llm.invoke(messages)
        return str(getattr(out, "content", "") or "").strip()

    def run_world_seed(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[ChatOpenAI] = None,
    ) -> Dict[str, str]:
        """Run WORLD_SEED using a tool call that returns world_time + seed_text."""
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-game-master",
                max_tokens=1200,
                parallel_tool_calls=False,
            )

        callbacks = openrouter_logging_callbacks(scope="game_master", label="world_seed")
        human_msg = self._task_to_message("WORLD_SEED", payload)

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

        ctx_note = ("\n\n---\n" + context_text.strip()) if context_text and context_text.strip() else ""
        messages = [
            SystemMessage(content=self._prompt_text),
            *history_msgs,
            HumanMessage(content=human_msg + ctx_note),
        ]

        try:
            bound_llm = llm.bind_tools(
                [gm_world_seed_result],
                tool_choice={"type": "function", "function": {"name": "gm_world_seed_result"}},
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([gm_world_seed_result]).with_config({"callbacks": callbacks})

        max_retries = 4
        world_time = ""
        seed_text = ""
        for _attempt in range(max_retries):
            enable_direct_text_abort(max_words=15)
            try:
                out = bound_llm.invoke(messages)
            except KeyboardInterrupt:
                if logs_enabled():
                    print(f"[trace] GM run_world_seed: direct text abort — retrying")
                messages.append(AIMessage(content="(text output suppressed)"))
                messages.append(HumanMessage(
                    content=(
                        "ERROR: You produced raw text instead of calling gm_world_seed_result. "
                        "You MUST use the gm_world_seed_result tool."
                    )
                ))
                continue
            finally:
                disable_direct_text_abort()

            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                if logs_enabled():
                    print(f"[trace] GM run_world_seed: no tool call, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(messages, out, "ERROR: You MUST call gm_world_seed_result tool. Do not write text directly.")
                continue

            # Validate tool args; if invalid, provide corrective feedback and retry.
            tc = tool_calls[0] if tool_calls else {}
            args = tc.get("args") if isinstance(tc, dict) else None
            if not isinstance(args, dict):
                args = {}

            world_time = str(args.get("world_time") or "").strip()
            seed_text = str(args.get("seed_text") or "").strip()

            # Accept missing leading 'Y' and normalize to canonical world time format.
            if world_time and world_time[:1].isdigit() and len(world_time) >= 5 and world_time[4:5] == "-":
                world_time = f"Y{world_time}"
                args["world_time"] = world_time

            missing: List[str] = []
            if not world_time:
                missing.append("world_time")
            if not seed_text:
                missing.append("seed_text")

            has_locations = "LOCATIONS:" in seed_text
            has_char_locations = "PLAYER LOCATIONS:" in seed_text or "CHARACTER LOCATIONS:" in seed_text

            world_time_parse_error = ""
            if world_time:
                try:
                    parsed_world_time = WorldTime.parse(world_time)
                    if parsed_world_time.year <= 1:
                        world_time_parse_error = (
                            "world_time must be meaningful; bootstrap-like years (0000/0001) are not allowed"
                        )
                except Exception:
                    world_time_parse_error = (
                        "world_time must match exact format Y0000-01-01 00:00:00 "
                        "(optional seconds are allowed and normalized)"
                    )

            if not missing and has_locations and has_char_locations and not world_time_parse_error:
                # Valid seed result.
                break

            if logs_enabled():
                print(
                    "[trace] GM run_world_seed: invalid tool args, retrying "
                    f"({_attempt + 1}/{max_retries})"
                )
            detail_bits: List[str] = []
            if missing:
                detail_bits.append("missing: " + ", ".join(missing))
            if seed_text and (not has_locations or not has_char_locations):
                detail_bits.append("seed_text must contain both 'LOCATIONS:' and 'PLAYER LOCATIONS:' sections")
            if seed_text and "WORLD TIME:" in seed_text.upper():
                detail_bits.append("seed_text must NOT include a WORLD TIME line (pass time only in world_time arg)")
            if world_time_parse_error:
                detail_bits.append(world_time_parse_error)
            detail = "; ".join(detail_bits) if detail_bits else "invalid seed arguments"
            _append_tool_rejection(
                messages, out,
                "ERROR: Invalid gm_world_seed_result arguments (" + detail + "). Call gm_world_seed_result again with valid values."
            )
            continue
        else:
            # Loop exhausted with no valid result.
            world_time = ""
            seed_text = ""

        history_user_msg = self._task_to_history_message("WORLD_SEED", payload, human_msg)
        if self._history_path and seed_text:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            append_message(
                self._history_path,
                role="assistant",
                content=(
                    "WORLD_SEED via gm_world_seed_result:\n"
                    f"- world_time: {world_time}\n\n"
                    f"{seed_text}"
                ),
                limits=limits,
            )

        return {"world_time": world_time, "seed_text": seed_text}

    def run_turn_narration(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[ChatOpenAI] = None,
    ) -> Dict[str, Any]:
        """Run TURN_NARRATION using a single tool call that returns per-player narrations and duration."""
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-game-master",
                max_tokens=2000,
                parallel_tool_calls=False,
            )

        callbacks = openrouter_logging_callbacks(scope="game_master", label="turn_narration")

        # Build natural language message based on task type
        human_msg = self._task_to_message("TURN_NARRATION", payload)

        # Load conversation history
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
            *history_msgs,
            HumanMessage(content=human_msg + ctx_note),
        ]

        # Require exactly one tool call: either final narration or character correction.
        # NOTE: bind_tools() MUST come before with_config() — calling
        # with_config first wraps the LLM in a RunnableBinding, and
        # bind_tools on that delegates via __getattr__ to the underlying
        # ChatOpenAI, creating a new binding that drops the callbacks.
        try:
            bound_llm = llm.bind_tools(
                [gm_turn_narration, gm_correct_character_intents],
                tool_choice="required",
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([gm_turn_narration, gm_correct_character_intents]).with_config({"callbacks": callbacks})

        # Participants whose narrations must be present in the response.
        char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
        selected_characters = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]

        narrations: Dict[str, str] = {}
        _coerced_dur: Optional[str] = None
        _world_facts: str = ""
        _shared_narration: str = ""
        _personal_narration: Dict[str, str] = {}

        max_retries = 3
        for _attempt in range(max_retries):
            # Defensive reset for per-invocation watchdog/callback state.
            clear_detected_invalid_pattern()
            _clear_watchdog_abort()
            enable_direct_text_abort(max_words=15)
            try:
                out = bound_llm.invoke(messages)
            except KeyboardInterrupt:
                if logs_enabled():
                    print(f"[trace] GM run_turn_narration: direct text abort — retrying")
                messages.append(AIMessage(content="(text output suppressed)"))
                messages.append(HumanMessage(
                    content="ERROR: You produced raw text instead of calling a tool. "
                    "You MUST use gm_turn_narration or gm_correct_character_intents. Call one now."
                ))
                continue
            finally:
                disable_direct_text_abort()

            # Some providers/LangChain paths may swallow callback KeyboardInterrupt
            # and return a partial/empty response. Treat a detected direct_text
            # pattern as an explicit retry signal.
            detected_pattern = get_detected_invalid_pattern()
            if detected_pattern and str(detected_pattern).startswith("direct_text"):
                if logs_enabled():
                    print(
                        f"[trace] GM run_turn_narration: detected {detected_pattern}; retrying ({_attempt + 1}/{max_retries})"
                    )
                messages.append(AIMessage(content="(text output suppressed)"))
                messages.append(HumanMessage(
                    content=(
                        "ERROR: You produced raw text instead of calling a tool. "
                        "You MUST use gm_turn_narration or gm_correct_character_intents. Call one now."
                    )
                ))
                continue

            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                if logs_enabled():
                    print(f"[trace] GM run_turn_narration: no tool call, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(messages, out, "ERROR: You MUST call gm_turn_narration or gm_correct_character_intents tool. Do not write text directly.")
                continue

            tc0 = tool_calls[0]
            tool_name0 = str(tc0.get("name") or "").strip()
            args0 = tc0.get("args") if isinstance(tc0, dict) else {}
            if not isinstance(args0, dict):
                args0 = {}

            if tool_name0 == "gm_correct_character_intents":
                correction_character = str(args0.get("character_name") or "").strip()
                correction_notice = str(args0.get("turn_insight") or "").strip()
                if not correction_character or not correction_notice:
                    if logs_enabled():
                        print(
                            f"[trace] GM run_turn_narration: invalid correction args, retrying ({_attempt + 1}/{max_retries})"
                        )
                    _append_tool_rejection(
                        messages,
                        out,
                        "ERROR: gm_correct_character_intents requires non-empty character_name and turn_insight. "
                        "Call gm_correct_character_intents again with valid arguments.",
                    )
                    continue

                # Correction rounds are turn-ephemeral and intentionally not persisted.
                return {
                    "type": "correction",
                    "character_name": correction_character,
                    "turn_insight": correction_notice,
                }

            if tool_name0 != "gm_turn_narration":
                if logs_enabled():
                    print(
                        f"[trace] GM run_turn_narration: unexpected tool {tool_name0!r}, retrying ({_attempt + 1}/{max_retries})"
                    )
                _append_tool_rejection(
                    messages,
                    out,
                    "ERROR: Invalid tool for TURN_NARRATION. Use gm_turn_narration to finalize "
                    "or gm_correct_character_intents to request one character re-plan.",
                )
                continue

            _shared0 = str(args0.get("shared") or "").strip()
            _personal_raw0 = str(args0.get("personal_json") or "").strip()
            duration0 = str(args0.get("duration") or "").strip()

            # Parse optional personal additions dict.
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
                    print(f"[trace] GM run_turn_narration: empty shared, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(messages, out, "ERROR: shared is empty. Call gm_turn_narration with a non-empty shared narration.")
                continue

            _coerced_dur = _coerce_duration(duration0)
            if _coerced_dur is None:
                if logs_enabled():
                    print(
                        f"[trace] GM run_turn_narration: invalid duration {duration0!r}, "
                        f"retrying ({_attempt + 1}/{max_retries})"
                    )
                _append_tool_rejection(
                    messages, out,
                    f"ERROR: Invalid duration {duration0!r} in gm_turn_narration. "
                    "duration must be a plain number + unit, e.g. '5m', '30s', '2h', '1d'. "
                    "No ranges, approximations, or word numbers. "
                    "Call gm_turn_narration again with the same narration and a valid duration."
                )
                continue

            # Discard personal entries for non-participants.
            _selected_set0 = set(selected_characters)
            _personal0 = {k: v for k, v in _personal0.items() if k in _selected_set0}
            # Assemble per-player narrations: shared + individual personal addition.
            narrations = {
                c: (_shared0 + "\n\n" + _personal0[c]) if c in _personal0 else _shared0
                for c in selected_characters
            }
            _world_facts = str(args0.get("world_facts") or "").strip()
            _shared_narration = _shared0
            _personal_narration = _personal0
            break
        else:
            narrations = {}
            _coerced_dur = None

        duration = _coerced_dur or ""
        world_facts = _world_facts

        # Combined narration for history, recaps and fallback delivery.
        # Format: shared text + optional personal blocks (only the per-player delta).
        _narration_personal_blocks = "\n\n".join(
            f"[{n}]\n{_personal_narration[n]}" for n in selected_characters if n in _personal_narration
        )
        combined_narration = (_shared_narration + "\n\n" + _narration_personal_blocks).strip() if _narration_personal_blocks else _shared_narration

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

        history_user_msg = self._task_to_history_message("TURN_NARRATION", history_payload, human_msg)

        # Save to conversation history only on success — avoid orphaned user
        # messages from failed retries that pollute the history.
        if self._history_path and combined_narration:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            assistant_mem = (
                (
                    (f"Turn end time: {turn_end_time}\n" if turn_end_time else "")
                    + f"Turn duration: {duration}\n\n{combined_narration}"
                ).strip()
                if duration
                else combined_narration
            )
            append_message(self._history_path, role="assistant", content=assistant_mem, limits=limits)

        # Clear the per-turn Q&A buffer once narration is committed.
        if combined_narration:
            self._turn_qa_buffer.clear()

        return {
            "type": "narration",
            "narration": combined_narration,
            "narrations": narrations,
            "duration": duration,
            "world_facts": world_facts,
        }

    def run_scene_description(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
        llm: Optional[ChatOpenAI] = None,
    ) -> Dict[str, Any]:
        """Run SCENE_DESCRIPTION in one tool call: scene select + per-player descriptions."""
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-game-master",
                max_tokens=2000,
                parallel_tool_calls=False,
            )

        callbacks = openrouter_logging_callbacks(scope="game_master", label="scene_description")

        human_msg = self._task_to_message("SCENE_DESCRIPTION", payload)

        # Load conversation history
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

        ctx_note = ("\n\n---\n" + context_text.strip()) if context_text and context_text.strip() else ""
        messages = [
            SystemMessage(content=self._prompt_text),
            *history_msgs,
            HumanMessage(content=human_msg + ctx_note),
        ]

        # NOTE: bind_tools() before with_config() — see run_turn_narration for explanation.
        try:
            bound_llm = llm.bind_tools([gm_scene_description], tool_choice={"type": "function", "function": {"name": "gm_scene_description"}}).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([gm_scene_description]).with_config({"callbacks": callbacks})

        selected_characters_hint = [str(x).strip() for x in (payload.get("character_names") or []) if str(x).strip()]
        selected_location_hint = str(payload.get("location") or "").strip()
        selected_npcs_hint = [str(x).strip() for x in (payload.get("npcs") or []) if str(x).strip()]
        scene_time_shift_hint = str(payload.get("time_shift") or "0").strip() or "0"

        max_retries = 3
        descriptions: Dict[str, str] = {}
        _selected_characters: List[str] = selected_characters_hint
        _selected_location: str = selected_location_hint
        _selected_npcs: List[str] = selected_npcs_hint
        _time_shift: str = scene_time_shift_hint
        _shared_result: str = ""
        _personal_result: Dict[str, str] = {}
        for _attempt in range(max_retries):
            enable_direct_text_abort(max_words=15)
            try:
                out = bound_llm.invoke(messages)
            except KeyboardInterrupt:
                if logs_enabled():
                    print(f"[trace] GM run_scene_description: direct text abort — retrying")
                messages.append(AIMessage(content="(text output suppressed)"))
                messages.append(HumanMessage(
                    content="ERROR: You produced raw text instead of calling gm_scene_description. "
                ))
                continue
            finally:
                disable_direct_text_abort()

            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                if logs_enabled():
                    print(f"[trace] GM run_scene_description: no tool call, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(messages, out, "ERROR: You MUST call gm_scene_description tool. Do not write text directly.")
                continue

            tc = tool_calls[0]
            args = tc.get("args") if isinstance(tc, dict) else {}
            if not isinstance(args, dict):
                args = {}

            raw_characters = args.get("player_names")
            if isinstance(raw_characters, list):
                _selected_characters = [str(x).strip() for x in raw_characters if str(x).strip()]
            elif isinstance(raw_characters, str):
                _selected_characters = [raw_characters.strip()] if raw_characters.strip() else []

            _selected_location = str(args.get("location") or "").strip()

            raw_npcs = args.get("npc_names")
            if isinstance(raw_npcs, list):
                _selected_npcs = [str(x).strip() for x in raw_npcs if str(x).strip()]
            elif isinstance(raw_npcs, str):
                _selected_npcs = [raw_npcs.strip()] if raw_npcs.strip() else []
            else:
                _selected_npcs = []

            _time_shift = str(args.get("time_shift") or "0").strip() or "0"
            names_order_reasoning = str(args.get("names_order_reasoning") or "").strip()
            if names_order_reasoning:
                print(f"[trace] names_order_reasoning from GM run_scene_description:\n{names_order_reasoning}")

            _shared = str(args.get("shared") or "").strip()
            _personal_raw = str(args.get("personal_json") or "").strip()
            _personal: Dict[str, str] = {}
            if _personal_raw:
                try:
                    obj = json.loads(_personal_raw)
                    if isinstance(obj, dict):
                        _personal = {str(k): str(v).strip() for k, v in obj.items() if str(v).strip()}
                except Exception:
                    pass

            if not _shared:
                if logs_enabled():
                    print(f"[trace] GM run_scene_description: empty shared, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(
                    messages, out,
                    "ERROR: shared is empty. Call gm_scene_description with a non-empty shared scene description."
                )
                continue

            if not _selected_characters or not _selected_location:
                if logs_enabled():
                    print(f"[trace] GM run_scene_description: missing player_names/location, retrying ({_attempt + 1}/{max_retries})")
                _append_tool_rejection(
                    messages,
                    out,
                    "ERROR: gm_scene_description must include non-empty player_names and location along with description fields.",
                )
                continue

            # Discard personal entries for non-participants.
            _selected_set = set(_selected_characters)
            _personal = {k: v for k, v in _personal.items() if k in _selected_set}

            # Assemble per-player descriptions: shared + individual personal addition.
            descriptions = {
                c: (_shared + "\n\n" + _personal[c]) if c in _personal else _shared
                for c in _selected_characters
            }
            _shared_result = _shared
            _personal_result = _personal
            break

        history_user_msg = self._task_to_history_message("SCENE_DESCRIPTION", payload, human_msg)

        # Build compact combined text for GM history: shared + per-player personal blocks.
        _personal_blocks = "\n\n".join(
            f"[{n}]\n{_personal_result[n]}" for n in _selected_characters if n in _personal_result
        )
        _combined = (_shared_result + "\n\n" + _personal_blocks).strip() if _personal_blocks else _shared_result

        # Save to conversation history only on success — avoid orphaned user
        # messages from failed retries that pollute the history.
        if self._history_path and descriptions and _selected_characters and _selected_location:
            limits = limits_from_env()
            append_message(self._history_path, role="user", content=history_user_msg, limits=limits)
            assistant_mem = (
                "Selected scene + description via gm_scene_description:\n"
                f"- location: {_selected_location}\n"
                f"- players: {', '.join(_selected_characters)}\n"
                f"- npcs: {', '.join(_selected_npcs) if _selected_npcs else '(none)'}\n"
                f"- time_shift: {_time_shift}\n\n"
                f"{_combined}"
            )
            append_message(self._history_path, role="assistant", content=assistant_mem, limits=limits)

        return {
            "character_names": _selected_characters,
            "location": _selected_location,
            "npc_names": _selected_npcs,
            "time_shift": _time_shift,
            "shared": _shared_result,
            "personal": _personal_result,
            "descriptions": descriptions,
            "combined": _combined,
        }

    def _task_to_message(self, task: str, payload: Dict[str, Any]) -> str:
        """Convert task + payload into a natural language message for the GM."""
        task = str(task or "").strip().upper()
        
        if task == "WORLD_SEED":
            provided_characters = payload.get("character_names") if isinstance(payload.get("character_names"), list) else []
            provided_characters = [str(x).strip() for x in provided_characters if str(x).strip()]
            chars_line = ", ".join(provided_characters) if provided_characters else "(none provided)"
            return (
                "Generate the initial world seed for this adventure and return it via the gm_world_seed_result function.\n\n"
                "How to use the function:\n"
                "1) world_time argument: one exact datetime string in format Y0000-01-01 00:00:00.\n"
                "   Choose a meaningful in-world date/time that fits the plot and setting (era, date, time of day).\n"
                "   Do NOT use bootstrap-like values such as year 0000 or 0001.\n"
                "2) seed_text argument: a plain-text seed body with the sections below.\n"
                "   Put WORLD TIME only in world_time argument, not in seed_text.\n\n"
                f"Provided characters to place: {chars_line}\n"
                "Every provided player must appear once in PLAYER LOCATIONS.\n\n"
                "seed_text template:\n"
                "WORLD SEED\n"
                "LOCATIONS:\n"
                "- Name: <location name>\n"
                "  Summary: <one-line summary>\n"
                "  Details: <multi-line description>\n"
                "(repeat location blocks as needed; at least one location)\n\n"
                "PLAYER LOCATIONS:\n"
                "- <player name> -> <location name>\n"
                "(one line per provided player)\n"
            )
        
        elif task == "SCENE_DESCRIPTION":
            chars = payload.get("character_names") if isinstance(payload.get("character_names"), list) else []
            npcs = payload.get("npcs") if isinstance(payload.get("npcs"), list) else []
            location = str(payload.get("location") or "").strip()
            missing_locations = payload.get("missing_locations") if isinstance(payload.get("missing_locations"), list) else []
            missing_npcs = payload.get("missing_npcs") if isinstance(payload.get("missing_npcs"), list) else []
            world_tod = str(payload.get("world_time_of_day") or "").strip()
            overview = payload.get("character_time_overview")

            msg = (
                "Create and describe the next scene using gm_scene_description.\n"
                "Follow both ## Creating a scene and ## Scene description paragraphs.\n"
                "\n"
                f"Preselected location hint: {location or '(none)'}\n"
                f"Preselected players hint: {', '.join(str(x) for x in chars) if chars else '(none)'}\n"
                f"Preselected NPCs hint: {', '.join(str(x) for x in npcs) if npcs else '(none)'}\n"
            )

            if missing_locations:
                for nm in missing_locations:
                    msg += (
                        f"⚠ Location '{nm}' does not exist in world data yet. "
                        "In this description, include brief info about it\n"
                    )
            if missing_npcs:
                for nm in missing_npcs:
                    msg += (
                        f"⚠ NPC '{nm}' does not exist in world data yet. "
                        "In this description, include brief info about it\n"
                    )

            if isinstance(overview, list) and overview:
                msg += "\nPlayer overview (oldest local time first):\n"
                for item in overview:
                    if not isinstance(item, dict):
                        continue
                    nm = str(item.get("name") or "?").strip() or "?"
                    la = str(item.get("last_acted") or "never").strip() or "never"
                    loc = str(item.get("location") or "unknown").strip() or "unknown"
                    twa = item.get("turns_without_attention")
                    twa_str = f" | turns_without_attention: {int(twa)}" if twa is not None else ""
                    msg += f"- {nm} | last_acted: {la} | location: {loc}{twa_str}\n"
            if world_tod:
                msg += f"World time_of_day: {world_tod}\n"

            msg += (
                "Call gm_scene_description with:\n"
                "- player_names: ordered list of scene participants (who should receive descriptions).\n"
                "- location: selected scene location.\n"
                "- npc_names: optional list of scene NPCs.\n"
                "- time_shift: skipped passive interval before scene start (or '0').\n"
                "- shared: 3rd-person description covering everything ALL players can observe "
                "(atmosphere, environment, NPC activity, sensory details). No secrets or private info.\n"
                "- personal_json: (if (turn_players > 1) JSON object for players who percieve something "
                "EXCLUSIVELY private not percieved by others, addressed in 2nd person ('you')"
                "!Omit entirely if nothing is relevant is private!\n"
            )
            return msg
        
        elif task == "TURN_NARRATION":
            char_plans = payload.get("character_plans") if isinstance(payload.get("character_plans"), list) else []
            participant_names = [str(p.get("name") or "").strip() for p in char_plans if str(p.get("name") or "").strip()]
            round_history = payload.get("turn_round_history") if isinstance(payload.get("turn_round_history"), list) else []

            msg = "Resolve this turn by choosing exactly one tool:\n"
            msg += (
                "- gm_turn_narration: finalize the turn according to Narrating a turn paragraph\n"
                "- gm_correct_character_intents: request exactly one character to revise intent before narration accoring to Correcting character intents.\n"
                "  Use args: character_name, turn_insight. turn_insight must be in-world notice for that character.\n"
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
                        msg += f"\n[Round {item_round} correction] {char_name}: intent rejected, replanning requested\n"
                    elif item_type == "replan":
                        char_name = str(item.get("character_name") or "").strip() or "Unknown"
                        msg += f"\n[Round {item_round} replan] {char_name}: new intent submitted (see current intentions below)\n"

            if participant_names:
                msg += "\nParticipants:\n"
                msg += ", ".join(participant_names) + "\n"

            if char_plans:
                msg += "\nPlayer intentions for this turn:\n"
                msg += "\n--- TURN BEGIN ---\n"
                for plan in char_plans:
                    if isinstance(plan, dict):
                        char_name = plan.get("name") or plan.get("character_name") or "Unknown"
                        action = plan.get("intent") or plan.get("action") or plan.get("decision") or "acts"
                        thoughts = str(plan.get("thoughts") or "").strip()

                        msg += f"\n{char_name}:\n"
                        msg += f"  Intent: {action}\n"
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
        
        elif task == "PARAGRAPH_SUMMARY":
            ongoing = payload.get("ongoing_paragraph") or {}
            do_arc_summary = bool(payload.get("do_arc_summary"))
            turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
            # Build a compact representation of turns for the GM
            turn_lines = []
            for i, t in enumerate(turns, 1):
                if not isinstance(t, dict):
                    continue
                loc = str(t.get("location") or "").strip()
                narr = str(t.get("narration") or "").strip()
                chars = t.get("characters") if isinstance(t.get("characters"), list) else []
                prefix = f"[{loc}]" if loc else ""
                char_str = f" ({', '.join(str(c) for c in chars)})" if chars else ""
                turn_lines.append(f"Turn {i}{prefix}{char_str}: {narr}")

            turns_text = "\n\n".join(turn_lines) if turn_lines else "(no turns)"
            locations = ongoing.get("locations") if isinstance(ongoing.get("locations"), list) else []
            characters = ongoing.get("characters") if isinstance(ongoing.get("characters"), list) else []
            npcs = ongoing.get("npcs") if isinstance(ongoing.get("npcs"), list) else []

            existing_names = payload.get("existing_paragraph_names") or []
            existing_warning = ""
            if existing_names:
                existing_warning = (
                    f"\nExisting paragraph names (do NOT reuse any of these): "
                    f"{', '.join(repr(n) for n in existing_names)}\n"
                )

            return (
                "Summarize the following sequence of turns into a single named paragraph for the story record.\n\n"
                f"Locations involved: {', '.join(str(x) for x in locations) if locations else 'unknown'}\n"
                f"Characters involved: {', '.join(str(x) for x in characters) if characters else 'unknown'}\n"
                f"NPCs involved: {', '.join(str(x) for x in npcs) if npcs else 'none'}\n"
                f"{existing_warning}\n"
                f"Turns:\n{turns_text}\n\n"
                "Reply with STRICT JSON only (no markdown, no explanation):\n"
                '{"name": "<short paragraph title, 3-10 words>", "summary": "<5-10 sentence summary of events>"}\n\n'
                "Rules:\n"
                "- Use only information from the turns above.\n"
                "- Keep it in-world (no meta commentary).\n"
                "- Focus on what happened, what changed, and what the players learned or achieved.\n"
                "- The name MUST be unique — never repeat an existing paragraph name.\n"
            )

        elif task == "ANSWER_QUESTION":
            char_name = str(payload.get("character_name") or "Unknown").strip()
            questions = str(payload.get("questions") or "").strip()

            prior_qa = ""
            if self._turn_qa_buffer:
                lines = ["Answers you already gave to players this turn:"]
                for entry in self._turn_qa_buffer:
                    lines.append(f"[{entry['character_name']}]")
                    lines.append(f"  Q: {entry['questions']}")
                    lines.append(f"  A: {entry['answer']}")
                prior_qa = "\n".join(lines) + "\n\n"

            return (
                prior_qa
                + "Player question (as recall to their self-knowledge or perception).\n"
                f"Player: {char_name}\n"
                f"Questions:\n{questions}\n\n"
                "Provide short, clear in-character answers based on what player CHARACTER can perceive and know. Do not give any meta-knonwledge"
            )
        
        else:
            # Fallback to JSON for unknown tasks
            return json.dumps({"task": task, "payload": payload}, ensure_ascii=False, indent=2)

    def _task_to_history_message(self, task: str, payload: Dict[str, Any], default_msg: str) -> str:
        """Return a compact, durable history entry for a task.

        This intentionally avoids persisting large instructional blocks used only
        for the current invocation.
        """
        task_u = str(task or "").strip().upper()

        if task_u == "SCENE_DESCRIPTION":
            # Include full guidance in history so GM remembers the rules for SCENE_DESCRIPTION
            return default_msg

        if task_u == "TURN_NARRATION":
            # Include full guidance in history so GM remembers the rules for TURN_NARRATION
            # This preserves the "Narrate per ## Narrating a turn using gm_turn_narration call"
            # instruction along with character plans.
            # Strip TURN BEGIN/END anchors — they are prompt-only cues, not history.
            return default_msg.replace("\n--- TURN BEGIN ---\n", "\n").replace("\n--- TURN END ---\n", "")

        if task_u == "ANSWER_QUESTION":
            nm = str(payload.get("character_name") or "Unknown").strip()
            return f"ANSWER_QUESTION | player: {nm}"

        if task_u == "WORLD_SEED":
            return "WORLD_SEED request"

        msg = str(default_msg or "").strip()
        return msg


def parse_game_master_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON parser for GM outputs.

    Accepts raw JSON, fenced json blocks, or the first outermost object.
    """

    raw = (text or "").strip()
    if not raw:
        return {}

    # Direct parse
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    # Fenced block
    fence_start = raw.find("```json")
    if fence_start != -1:
        fence_end = raw.find("```", fence_start + 7)
        if fence_end != -1:
            fenced = raw[fence_start + 7 : fence_end].strip()
            try:
                obj = json.loads(fenced)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass

    # Outermost object
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        snippet = raw[start : end + 1]
        try:
            obj = json.loads(snippet)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass

    return {}


def build_game_master_context(world: World) -> str:
    """Build a GM context block (narrative-friendly)."""

    try:
        return build_game_master_context_block(world)
    except Exception:
        return ""


def build_game_master_qa_context(world: World) -> str:
    """Minimal scene-only context for ANSWER_QUESTION calls."""

    try:
        return _build_qa_context(world)
    except Exception:
        return ""
