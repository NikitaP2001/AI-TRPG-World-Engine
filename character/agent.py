from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from memory_store import append_message, approx_token_count, limits_from_env, load_history
from openrouter_langchain_logging import logs_enabled, enable_direct_text_abort, disable_direct_text_abort
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world.story import PinnedBlockCache
from character.reflection import build_character_arc_block, build_character_paragraph_block, _character_dir

from .memory import update_turn_memory


_HISTORY_NOTICE_PRINTED: set[str] = set()
_PINNED_CACHES: Dict[str, PinnedBlockCache] = {}

# Global reference to Scene Manager for ask_scene_manager tool
_SCENE_MANAGER: Optional[Any] = None
_CHARACTER_NAME: Optional[str] = None


def set_scene_manager_for_characters(scene_manager: Any) -> None:
    """Set the Scene Manager reference for character Q&A.
    
    Must be called before running character agents.
    """
    global _SCENE_MANAGER
    _SCENE_MANAGER = scene_manager


@dataclass
class CharacterDecision:
    character_name: str
    intent: str
    thoughts: str


def _decision_to_json(decision: CharacterDecision) -> str:
    return json.dumps(
        {
            "character": decision.character_name,
            "intent": decision.intent,
            "thoughts": decision.thoughts,
        },
        ensure_ascii=False,
        indent=2,
    )


def _safe_filename_stem(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "unknown"
    out: List[str] = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    stem = "".join(out).strip("_")
    return stem or "unknown"


def _format_prompt_messages(messages: List[Any]) -> str:
    lines: List[str] = []
    for msg in messages or []:
        role = "message"
        if isinstance(msg, SystemMessage):
            role = "system"
        elif isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "assistant"
        elif isinstance(msg, ToolMessage):
            role = "tool"
        lines.append(f"[{role}]")
        lines.append(str(getattr(msg, "content", "") or ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _save_character_prompt_snapshot(
    *,
    character_name: str,
    messages: List[Any],
    scene_location: str,
    world_time: str,
) -> None:
    if not logs_enabled():
        return
    try:
        workspace_root = Path(__file__).resolve().parent.parent
        out_dir = (workspace_root / "logs" / "character_prompts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        stem = _safe_filename_stem(character_name)
        out_path = out_dir / f"{ts}__{stem}.txt"

        header = [
            f"# CHARACTER PROMPT SNAPSHOT: {datetime.now(timezone.utc).isoformat()}",
            f"character={character_name}",
            f"scene_location={scene_location}",
            f"world_time={world_time}",
            "",
        ]
        body = _format_prompt_messages(messages)

        tmp = out_path.with_suffix(".tmp")
        tmp.write_text("\n".join(header) + body, encoding="utf-8")
        tmp.replace(out_path)
    except Exception:
        return


@tool
def ask_scene_manager(questions: str) -> str:
    """Ask the Scene Manager questions about the scene or world.
    You may ask questions about the surrounding, world, or things which character may be interested in.
    - You can ask from 1 up to 3 questions in one call. No more than two calls per turn.
    - Submit complex actions or intents prohibited, only what your character already may know or find within a second.
    Use this when you need information not present in your scene description.
    The Scene Manager will answer based on world data or escalate to the Game Master if needed.
    Args:
        questions: Your questions for the SM (can be multiple, one per line),
        no more than 3 questions at a time.
    Returns:
        Answers to your questions
    """
    global _SCENE_MANAGER, _CHARACTER_NAME
    
    if _SCENE_MANAGER is None:
        return "Error: Scene Manager not available for questions"
    
    if _CHARACTER_NAME is None:
        return "Error: Character name not set"
    
    questions_text = str(questions or "").strip()
    if not questions_text:
        return "Error: questions cannot be empty"
    
    if logs_enabled():
        print(f"[trace] {_CHARACTER_NAME} asking SM: {questions_text}")
    
    try:
        answers = _SCENE_MANAGER.resolve_character_question(
            character_name=_CHARACTER_NAME,
            questions=questions_text,
        )
        
        if not answers:
            answers = "No answer available right now."
        
        if logs_enabled():
            print(f"[trace] SM answered {_CHARACTER_NAME}: {answers[:200]}")
        
        return answers
        
    except Exception as e:
        if logs_enabled():
            print(f"[trace] ask_scene_manager error for {_CHARACTER_NAME}: {e}")
        return f"Error: Could not get answer: {e}"


@tool
def character_decision(
    psyche_core: str, 
    ego_rationalization: str, 
    compromise_intent: str
) -> str:
    """Final output - commit to an intent.
This is your final output for each turn following ## Narrative structure rules. 
You MUST call this tool to commit to an character decision for this turn.
**Arguments:**
- `psyche_core` (MAX 100 WORDS): JSON string capturing the primary internal vector of the subject. 
    This is the baseline state before cognitive filtering or contextual adaptation.
    Must include:
    * affective_impulse: The dominant internal tension, drive, or state of inertia generated 
        by the subject's baseline constitution in response to the current state of the environment.
    * phantasm: The structural formula defining the subject's cognitive orientation toward external entities 
        (e.g., as autonomous subjects, passive instruments, or operational obstacles).
    Omit key only if the substrate is completely inert.
        
- `ego_rationalization` (MAX 100 WORDS): The processing and filtering interface that aligns 
    the primary vector ('psyche_core') with the systemic constraints of the current environment. 
    Must include:
    * internal_talk: The internal operational calculus, tactical reasoning, or normative processing.
    * defense_mechanism: The cognitive loop that synthesizes and maintains internal coherence 
        in relation to the subject's own historical outputs, ensuring structural continuity.
        
- compromise_intent (MAX 150 WORDS): The final, objective, and compressed behavioral output. 
    This is the pragmatic vector of action emitted into the environment, representing the exact 
    boundary intersection between internal drive ('psyche_core') and systemic constraint ('ego_rationalization').
**Important:**
- if some relevant other character reaction was not described in previous turn - just wait or cover possible scenarious conditionally.
- The amount of words you provide in arguments does not depend on time frame the intent will take
- If text gets watery, widen the time frame; if intent gets compressed, narrow the time frame.
- Stay within word limits or your output will be rejected.
- Do not announce explicit intents which will be out of scope of current scene/visible surrounding
    """

    intent_text = str(compromise_intent or "").strip()
    if not intent_text:
        raise ValueError("intent is required")

    runtime_character_name = str(_CHARACTER_NAME or "").strip()
    if not runtime_character_name:
        raise ValueError("character runtime context is missing")

    return _decision_to_json(
        CharacterDecision(
            character_name=runtime_character_name,
            intent=intent_text,
            thoughts=str(psyche_core or "") + str(ego_rationalization or ""),
        )
    )


def run_character_agent(
    *,
    character_name: str,
    character_description: Dict[str, Any],
    current_scene_context: str,
    scene_location: str,
    world_time: str,
    scene_npcs: Optional[List[str]] = None,
    scene_players: Optional[List[str]] = None,
    gm_reality_notice: str = "",
    previous_intent: str = "",
    require_decision: bool = False,
    persist_history: bool = True,
    prompt_path: str = "agents/character_agent/prompt.txt",
    llm: Optional[ChatOpenAI] = None,
) -> str:
    global _CHARACTER_NAME
    _CHARACTER_NAME = character_name
    
    prompt_text = read_prompt_text(prompt_path).replace("{name}", str(character_name or ""))

    def _truncate_thoughts_text(text: str) -> str:
        return (text or "").strip()

    if llm is None:
        # Keep character agents' free-text output small so they don't waste tokens
        # on long "thinking aloud". Tool outputs are unaffected.
        # NOTE: This caps *total generation*, including tool-call arguments.
        # Keep it reasonably high, and rely on thoughts_text truncation for context control.
        DEFAULT_CHARACTER_MAX_OUTPUT_TOKENS = 1000

        raw_max_out = (os.getenv("LLM_WORLD_CHARACTER_MAX_OUTPUT_TOKENS") or "").strip()
        max_out: Optional[int]
        if raw_max_out:
            try:
                max_out = int(raw_max_out)
            except Exception:
                max_out = DEFAULT_CHARACTER_MAX_OUTPUT_TOKENS
        else:
            max_out = DEFAULT_CHARACTER_MAX_OUTPUT_TOKENS

        llm = build_openrouter_chat_llm(
            temperature=0.7,
            streaming=True,
            max_tokens=max_out,
            title_suffix=f"-character-{int(max_out or 0)}t",
            parallel_tool_calls=False,
        )

    callbacks = openrouter_logging_callbacks(scope="character", label=character_name)

    # Character can ask GM at most once per turn, then must finish with character_decision.
    can_ask_gm_once = not require_decision

    def _current_allowed_tool_names() -> set[str]:
        names = {"character_decision"}
        if can_ask_gm_once:
            names.add("ask_scene_manager")
        return names

    def _build_bound_llm(*, allow_gm_question: bool):
        allowed_tools = [character_decision]
        if allow_gm_question:
            allowed_tools.insert(0, ask_scene_manager)

        # Force a tool call so models don't spend the output budget on free-form prose.
        # This also makes small max_tokens caps more reliable.
        tool_choice: Any
        if allow_gm_question:
            tool_choice = "required"
        else:
            tool_choice = {"type": "function", "function": {"name": "character_decision"}}

        try:
            return llm.bind_tools(allowed_tools, tool_choice=tool_choice).with_config({"callbacks": callbacks})
        except TypeError:
            # Older LangChain versions may not support tool_choice.
            return llm.bind_tools(allowed_tools).with_config({"callbacks": callbacks})

    bound_llm = _build_bound_llm(allow_gm_question=can_ask_gm_once)

    # Per-character persistent chat memory.
    limits = limits_from_env()
    workspace_root = Path(__file__).resolve().parent.parent
    game_root = (workspace_root / "game").resolve()
    history_path = (game_root / "characters" / character_name / "messages.json").resolve()
    memory_path = (game_root / "characters" / character_name / "memory.json").resolve()

    def _history_to_messages(history: List[Dict[str, str]]):
        msgs = []
        for h in history:
            role = (h.get("role") or "").strip().lower()
            content = str(h.get("content") or "")
            if role == "user":
                msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                msgs.append(AIMessage(content=content))
        return msgs

    # Inject self-reflection (if it exists) into the context blob.
    reflection_data: Optional[Dict[str, Any]] = None
    try:
        from .reflection import load_reflection
        reflection_data = load_reflection(character_name)
        if reflection_data:
            # Strip internal meta before presenting to the character
            reflection_data = {k: v for k, v in reflection_data.items() if not k.startswith("_")}
    except Exception:
        pass

    # Inject diary (if it exists).
    diary_text: Optional[str] = None
    try:
        from .reflection import load_diary
        diary = load_diary(character_name)
        diary_parts: list[str] = []
        arc_summaries = diary.get("arc_summaries") or []
        paragraphs = diary.get("paragraphs") or []
        if arc_summaries:
            for i, arc in enumerate(arc_summaries, 1):
                s = str(arc.get("summary") or "").strip()
                if s:
                    diary_parts.append(f"**Arc {i}:**\n{s}")
        if paragraphs:
            for p in paragraphs:
                s = str(p.get("summary") or "").strip()
                if s:
                    diary_parts.append(f"- {s}")
        if diary_parts:
            diary_text = "\n\n".join(diary_parts)
    except Exception:
        pass

    # Check for stale relationships in current scene (lightweight injection).
    stale_rels_note: Optional[str] = None
    try:
        from .relationship_review import check_stale_relationships
        stale_rels_note = check_stale_relationships(
            character_name,
            current_scene_npcs=scene_npcs,
            current_scene_players=scene_players,
        )
    except Exception:
        pass

    # ── Build character identity block ──────────
    # This will be persisted in history (not injected into SystemMessage)
    # to enable prefix caching like the Storage Assistant does.
    _identity_parts = [
        "## Your Character",
        json.dumps(
            {"character": character_name, "description": character_description},
            ensure_ascii=False, indent=2,
        ),
    ]
    identity_block = "\n\n".join(_identity_parts)

    # Build dynamic memory block (reflection + relationships + diary) - placed after stable history
    # so the stable persisted turns remain a clean cacheable prefix
    _memory_parts: list[str] = []
    if reflection_data:
        _memory_parts.append("## Self Reflection")
        _memory_parts.append(json.dumps(reflection_data, ensure_ascii=False, indent=2))
    if stale_rels_note:
        _memory_parts.append(stale_rels_note)
    if diary_text:
        _memory_parts.append("## Your Diary")
        _memory_parts.append(diary_text)
    memory_block = "\n\n".join(_memory_parts)

    # Load existing history
    history_msgs = _history_to_messages(load_history(history_path) if history_path.exists() else [])

    # Check if identity block exists in history (marker-based detection)
    IDENTITY_MARKER = "[character_identity_v1]"
    identity_exists = any(
        IDENTITY_MARKER in str(getattr(m, "content", "") or "")
        for m in history_msgs
    )

    # If identity doesn't exist in history, inject it as the first user message
    # This happens on first turn or after history trimming removes it
    if not identity_exists:
        identity_msg = HumanMessage(content=f"{IDENTITY_MARKER}\n{identity_block}")
        history_msgs = [identity_msg] + history_msgs
        # Persist the identity immediately so it's in history for next time
        if persist_history:
            append_message(history_path, role="user", content=f"{IDENTITY_MARKER}\n{identity_block}", limits=limits)

    # Load last turn's thoughts for short-term continuity.
    prev_turn_thoughts: Optional[str] = None
    try:
        if memory_path.exists():
            import json as _json_mem
            _mem_data = _json_mem.loads(memory_path.read_text(encoding="utf-8"))
            if isinstance(_mem_data, list) and _mem_data:
                _last = _mem_data[-1]
                if isinstance(_last, dict):
                    _ts = _last.get("thoughts")
                    if isinstance(_ts, list) and _ts:
                        _t = str(_ts[-1] or "").strip()
                        if _t:
                            prev_turn_thoughts = _t
    except Exception:
        pass

    # Dynamic per-turn context only.
    context_obj: Dict[str, Any] = {
        "scene_location": scene_location,
        "world_time": world_time,
        "current_scene_context": current_scene_context or "",
    }
    reality_notice = str(gm_reality_notice or "").strip()
    if reality_notice:
        notice_obj: Dict[str, str] = {
            "notice": reality_notice,
        }
        prev_intent_text = str(previous_intent or "").strip()
        if prev_intent_text:
            notice_obj["previous_intent"] = prev_intent_text
        context_obj["gm_reality_notice"] = notice_obj
    if prev_turn_thoughts:
        context_obj["prev_turn_thoughts"] = prev_turn_thoughts

    context_blob = json.dumps(context_obj, ensure_ascii=False, indent=2)

    # Build messages: static SystemMessage + stable history + dynamic context
    # This pattern enables prefix caching like SA does
    
    # Add a non-persisted guidance message to help the model focus on tool usage
    # This is injected after the last turn context to provide immediate attention
    # to the tool requirements without polluting the persistent history
    _anchor_lines = ["Review your context before acting:  ## Your Character (your identity and traits)"]
    if reflection_data:
        _anchor_lines.append("  ## Self Reflection (your current goals, beliefs, emotional state)")
    if stale_rels_note:
        _anchor_lines.append("  ## Known Relationships in Scene")
    # Build pinned summaries from diary via PinnedBlockCache (only rebuilds on trim)
    pinned_msgs: List[SystemMessage] = []
    if persist_history:
        try:
            cache = _PINNED_CACHES.get(character_name)
            if cache is None:
                cache = PinnedBlockCache(history_path)
                _PINNED_CACHES[character_name] = cache
            diary_path = _character_dir(character_name) / "diary.json"
            if diary_path.exists():
                block = cache.get("arc", diary_path, build_character_arc_block)
                if block:
                    pinned_msgs.append(SystemMessage(content=f"[arc_summaries]\n{block}"))
                block = cache.get("paragraph", diary_path, build_character_paragraph_block)
                if block:
                    pinned_msgs.append(SystemMessage(content=f"[paragraph_summaries]\n{block}"))
        except Exception:
            pass

    if diary_text:
        _anchor_lines.append("  ## Your Diary (your personal history and past experiences)")
    guidance_msg = HumanMessage(content=(
        "\n".join(_anchor_lines) + "\n\n"
        "You must respond using following tools: character_decision | ask_scene_manager.\n"
        "In case you need information: call ask_scene_manager (see prompt section)\n"
        "To commit your intent: call character_decision (see prompt section)\n"
    ))
    
    state = {
        "messages": [
            SystemMessage(content=prompt_text),  # Static only, no injections
            *pinned_msgs,  # Pinned arc/paragraph summaries from diary cache
            *history_msgs,  # Includes identity if present
            *(
                [HumanMessage(content=memory_block)]
                if memory_block else []
            ),  # Reflection + diary - after stable history, before scene context
            HumanMessage(content=context_blob),  # Dynamic scene context
            guidance_msg,  # Non-persisted guidance for attention
        ]
    }

    _save_character_prompt_snapshot(
        character_name=character_name,
        messages=list(state.get("messages") or []),
        scene_location=scene_location,
        world_time=world_time,
    )

    if logs_enabled() and history_path.exists() and character_name not in _HISTORY_NOTICE_PRINTED:
        loaded = load_history(history_path)
        if loaded:
            approx_tokens = 0
            for h in loaded:
                if isinstance(h, dict):
                    approx_tokens += approx_token_count(str(h.get("content") or ""))

            max_hist = int(limits.max_history_tokens)
            max_ctx = int(limits.model_max_context_tokens)
            pct_hist = int(round((approx_tokens / max_hist) * 100.0)) if max_hist > 0 else 0

            print(
                f"[trace] character history loaded: {character_name} (msgs={len(loaded)}) "
                f"| hist~{approx_tokens}/{max_hist} tok (~{pct_hist}%) "
                f"| model_ctx~{max_ctx} tok"
            )
            _HISTORY_NOTICE_PRINTED.add(character_name)

    def _looks_like_pseudo_tool_markup(text: str) -> bool:
        t = (text or "").lower()
        patterns = [
            "<function_calls",
            "</function_calls",
            "<invoke",
            "invoke name=",
            "<functioninvoke",
            "functioninvoke name=",
            "<parameter",
            "</parameter",
        ]
        return any(p in t for p in patterns)

    INTERNAL_RETRY_MARKER = "[internal_retry_v1]"

    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False))
                f.write("\n")
        except Exception:
            return

    def _debug_log_invalid_model_output(*, why: str, msg: Any) -> None:
        if not logs_enabled():
            return
        try:
            workspace_root = Path(__file__).resolve().parent.parent
            log_path = (workspace_root / "logs" / "character_invalid_outputs.jsonl").resolve()

            additional = getattr(msg, "additional_kwargs", {}) or {}
            # Avoid logging huge blobs.
            def _clip(v: Any, n: int = 2000) -> Any:
                return v

            payload = {
                "ts": _utc_now_iso(),
                "character": character_name,
                "why": why,
                "content": str(getattr(msg, "content", "") or ""),
                "tool_calls_attr": _clip(getattr(msg, "tool_calls", None)),
                "additional_kwargs_keys": list(additional.keys()) if isinstance(additional, dict) else [],
                "additional_tool_calls": _clip(additional.get("tool_calls")) if isinstance(additional, dict) else None,
                "additional_function_call": _clip(additional.get("function_call")) if isinstance(additional, dict) else None,
            }
            _append_jsonl(log_path, payload)
        except Exception:
            return

    def _extract_tool_calls(msg: Any) -> List[Dict[str, Any]]:
        def _normalize(tc: Any) -> Optional[Dict[str, Any]]:
            if isinstance(tc, dict):
                return tc
            # LangChain may use ToolCall-like objects.
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

            # Legacy OpenAI function-calling format.
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

    max_attempts = int(os.getenv("LLM_WORLD_CHARACTER_TOOL_RETRY_MAX", "8") or "8")
    attempt = 0

    ai_msg: Any = None
    pre_tool_text = ""
    tool_calls: List[Dict[str, Any]] = []
    gm_answers: List[str] = []

    def _retry_instruction(*, why: str, err: Optional[str] = None) -> HumanMessage:
        allowed = " OR ".join(sorted(_current_allowed_tool_names()))
        parts = [
            f"{INTERNAL_RETRY_MARKER}",
            f"Your last output was invalid: {why}.",
        ]
        if err:
            parts.append(f"Tool call error: {err}")
        parts.append(
            "Do NOT print tool calls (no <function_calls>/<invoke>, no fake Python). "
            f"Use the built-in tool calling interface to call EXACTLY ONE tool now: {allowed}. "
            "Do not add any other text."
        )
        return HumanMessage(content="\n".join(parts))

    while True:
        if attempt >= max_attempts:
            allowed = "/".join(sorted(allowed_tool_names))
            raise ValueError(
                f"Character agent '{character_name}' failed to successfully execute required tool call ({allowed}) after {max_attempts} attempts."
            )
        attempt += 1

        enable_direct_text_abort(max_words=15)
        try:
            ai_msg = bound_llm.invoke(state["messages"])
        except KeyboardInterrupt:
            if logs_enabled():
                print(f"[trace] character {character_name}: direct text abort — must use tool calls, retrying")
            state["messages"].append(_retry_instruction(
                why="you produced raw text instead of calling a tool. ALL output must go through tool calls"
            ))
            continue
        finally:
            disable_direct_text_abort()

        pre_tool_text = _truncate_thoughts_text((getattr(ai_msg, "content", "") or ""))
        tool_calls = _extract_tool_calls(ai_msg)
        allowed_tool_names = _current_allowed_tool_names()

        recognized = [tc for tc in tool_calls if str(tc.get("name") or "").strip() in allowed_tool_names]

        if len(recognized) != 1:
            why = "no tool call"
            if _looks_like_pseudo_tool_markup(pre_tool_text):
                why = "pseudo tool markup"
            elif tool_calls and not recognized:
                why = "unrecognized tool call"
            elif len(recognized) > 1:
                why = "multiple tool calls"

            _debug_log_invalid_model_output(why=why, msg=ai_msg)

            if logs_enabled():
                print(f"[trace] character output invalid ({character_name}): {why}; retrying")

            state["messages"].append(_retry_instruction(why=why))
            continue

        tool_call = recognized[0]
        tool_name = str(tool_call.get("name") or "").strip()

        args = tool_call.get("args") if isinstance(tool_call, dict) else None
        if not isinstance(args, dict):
            args = {}

        try:
            if tool_name == "ask_scene_manager":
                # One SM Q&A allowed per turn: remove ask_scene_manager from the
                # character context immediately after first use.
                can_ask_gm_once = False
                bound_llm = _build_bound_llm(allow_gm_question=False)

                # Character asks GM a question - get answer and continue ReAct loop
                args.setdefault("questions", "")
                
                tool_content = ask_scene_manager.invoke(args, config={"callbacks": callbacks})
                ans = str(tool_content or "").strip()
                if ans and not ans.lower().startswith("error:"):
                    gm_answers.append(ans)
                
                # Add AI message and tool result to conversation.
                # Some providers/paths omit tool_call id; normalize to a stable synthetic id
                # so the subsequent ToolMessage always has a valid pairing.
                tool_call_id = str(tool_call.get("id") or "").strip()
                if not tool_call_id:
                    safe_name = _safe_filename_stem(character_name)
                    tool_call_id = f"char_tc_{safe_name}_{attempt}_{len(gm_answers) + 1}"

                normalized_tool_call = {
                    "name": tool_name,
                    "args": dict(args),
                    "id": tool_call_id,
                    "type": "tool_call",
                }

                state["messages"].append(
                    AIMessage(
                        content=str(getattr(ai_msg, "content", "") or ""),
                        tool_calls=[normalized_tool_call],
                    )
                )
                state["messages"].append(
                    ToolMessage(
                        content=tool_content,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                
                # Continue loop - character can ask more questions or make decision
                continue

            # Decision path - character_decision ends the agent loop
            parsed = character_decision.invoke(args, config={"callbacks": callbacks})
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except Exception:
                    parsed = {"raw": parsed}
            
            payload = {
                "character": character_name,
                "decision": parsed,
            }
            if gm_answers:
                payload["gm_answers"] = list(gm_answers)
            if pre_tool_text:
                payload["thoughts_text"] = pre_tool_text

            if persist_history:
                # Persist interaction — strip ephemeral per-turn fields that are
                # only relevant during the turn and add noise to long-term memory.
                try:
                    mem_obj = json.loads(context_blob)
                    user_mem = json.dumps(mem_obj, ensure_ascii=False, indent=2)
                except Exception:
                    user_mem = context_blob
                if user_mem:
                    append_message(history_path, role="user", content=user_mem, limits=limits)

                # Only store the intent (not thoughts) in history.
                # Thoughts are ephemeral: generated for this turn, forwarded to GM,
                # but intentionally excluded from the character's own history so the
                # model reacts freshly to each scene without anchoring on its own
                # previous reasoning chains.
                intent_text = ""
                if isinstance(parsed, dict):
                    intent_text = str(parsed.get("intent") or "").strip()
                if intent_text:
                    append_message(history_path, role="assistant", content="Intent:\n" + intent_text, limits=limits)

                try:
                    thoughts = ""
                    if isinstance(parsed, dict):
                        thoughts = str(parsed.get("thoughts") or "")
                    update_turn_memory(
                        memory_path,
                        character_name=character_name,
                        world_time=world_time,
                        scene_location=scene_location,
                        thoughts_to_add=[thoughts] if thoughts.strip() else [],
                        outcome={
                            "intent": intent_text,
                            "thoughts": thoughts,
                        },
                    )
                except Exception:
                    pass

            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            if logs_enabled():
                print(f"[trace] character tool execution failed ({character_name}): {tool_name}: {e}; retrying")
            # When character_decision fails (e.g. bad duration), force
            # the model to retry that exact tool instead of falling back
            # to ask_scene_manager.
            if tool_name == "character_decision":
                err_text = str(e or "")
                guidance = (
                    "Fix the arguments and call character_decision again. "
                    "Do NOT call ask_scene_manager — you already have the information you need."
                )
                retry_msg = HumanMessage(content="\n".join([
                    INTERNAL_RETRY_MARKER,
                    f"Your character_decision call failed: {err_text}",
                    guidance,
                ]))
                state["messages"].append(retry_msg)
            else:
                state["messages"].append(_retry_instruction(why="tool call failed to execute", err=str(e)))
            continue
