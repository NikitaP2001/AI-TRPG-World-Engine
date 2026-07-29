from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from memory_store import append_message, approx_token_count, limits_from_env, load_history
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world.story import PinnedBlockCache
from player.reflection import build_character_arc_block, build_character_paragraph_block, _character_dir

from .memory import update_cycle_memory


_HISTORY_NOTICE_PRINTED: set[str] = set()
_PINNED_CACHES: Dict[str, PinnedBlockCache] = {}

# Per-invocation context for observe/recall/declare_intent tools
_CHARACTER_NAME: Optional[str] = None
_CURRENT_SCENE_CONTEXT: str = ""
_SCENE_LOCATION: str = ""
_WORLD_TIME: str = ""
_DECLARED_INTENTS: List[Dict[str, Any]] = []
_CANCELLED_SUBSCRIPTION_IDS: List[str] = []
_PENDING_OBSERVES: List[Dict[str, Any]] = []
_PENDING_RECALLS: List[Dict[str, Any]] = []
_SM_RESOLVER = None  # Set by _run_single_character for immediate SM answers
_NOW_SECONDS: int = 0  # Anchor for this invocation's t_start offsets (scheduler/timeline.py)
# в”Ђв”Ђ Session context persistence (В§1.1 вЂ” single invocation context per session) в”Ђв”Ђ
# Stores the FULL state["messages"] + globals across reinvokes within one session.
# Saved to disk only once at end_session().
_SESSION_CONTEXTS: Dict[str, Dict[str, Any]] = {}
_SESSION_HISTORY_PATHS: Dict[str, Path] = {}  # history_path per character (for end_session save)
_SESSION_LIMITS: Optional[Any] = None


def begin_session():
    """Begin session. All character convos stay in memory until end_session."""
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS
    _SESSION_CONTEXTS.clear()
    _SESSION_HISTORY_PATHS.clear()


def end_session():
    """End session. Save each character's essential data to disk once."""
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS, _SESSION_LIMITS
    for char_name, ctx in list(_SESSION_CONTEXTS.items()):
        context_blob = ctx.get("context_blob", "")
        decision_block = ctx.get("decision_block", "")
        memory_data = ctx.get("memory_data")
        history_path = _SESSION_HISTORY_PATHS.get(char_name)
        if not history_path or not context_blob:
            continue
        try:
            append_message(history_path, role="user", content=context_blob, limits=_SESSION_LIMITS)
            if decision_block:
                append_message(history_path, role="assistant", content=decision_block, limits=_SESSION_LIMITS)
        except Exception:
            pass
        if memory_data:
            try:
                from player.memory import update_cycle_memory as _utm
                _utm(
                    memory_data["memory_path"],
                    character_name=char_name,
                    world_time=memory_data.get("world_time", ""),
                    scene_location=memory_data.get("scene_location", ""),
                    thoughts_to_add=memory_data.get("thoughts", []),
                    outcome=memory_data.get("outcome", {}),
                )
            except Exception:
                pass
    _SESSION_CONTEXTS.clear()
    _SESSION_HISTORY_PATHS.clear()
    _SESSION_LIMITS = None


def set_sm_resolver(resolver):
    """Set the SM resolver function for observe/recall."""
    global _SM_RESOLVER
    _SM_RESOLVER = resolver


def _flush_pending_to_sm(player_name: str, pending: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Flush all pending observes/recalls through one SM call.

    Builds a single questions string from all pending items, calls SM once,
    and returns a dict mapping pending_id -> answer string.

    Args:
        player_name: The player asking the questions.
        pending: Dict with keys "observes" and "recalls", each a list of
                 dicts with "id" and "query".

    Returns:
        Dict mapping pending_id -> {"result": answer_string}
    """
    global _SM_RESOLVER
    if _SM_RESOLVER is None:
        return {item["id"]: {"result": "(SM unavailable)"}
                for item in pending.get("observes", []) + pending.get("recalls", [])}

    # Build a combined questions string with pending_id markers
    lines: List[str] = []
    id_map: Dict[str, str] = {}  # pending_id -> query tag for parsing
    for item in pending.get("observes", []):
        pid = item["id"]
        q = item.get("query", "general awareness")
        lines.append(f"[observe:{pid}] {q}")
        id_map[pid] = f"observe:{pid}"
    for item in pending.get("recalls", []):
        pid = item["id"]
        q = item.get("query", "")
        lines.append(f"[recall:{pid}] {q}")
        id_map[pid] = f"recall:{pid}"

    questions_str = "\n".join(lines)
    if not questions_str.strip():
        return {}

    try:
        answer = _SM_RESOLVER(player_name=player_name, questions=questions_str)
    except Exception as e:
        return {pid: {"result": f"(SM error: {e})"} for pid in id_map}

    # Parse SM answer — expected format is a JSON dict {pending_id: answer}
    # but SM may return free text. Try JSON first, fall back to assigning
    # the full answer to all pending items.
    if isinstance(answer, str) and answer.strip().startswith("{"):
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, dict):
                return {pid: {"result": parsed.get(pid, str(parsed))} for pid in id_map}
        except Exception:
            pass

    # Fallback: distribute the answer text across all pending items
    return {pid: {"result": answer} for pid in id_map}


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
def observe(query: str = "") -> dict:
    """Ask the Scene Manager what you perceive.

    Args:
        query: What to focus on.
    """
    global _CHARACTER_NAME
    
    ch = str(_CHARACTER_NAME or "").strip()
    q = str(query or "").strip() or "general awareness"
    
    if logs_enabled():
        print(f"[trace] {ch} observes: {q}")
    
    # Queue for batch resolution instead of calling SM immediately
    pending_id = f"obs_{uuid4().hex[:8]}"
    _PENDING_OBSERVES.append({
        "id": pending_id,
        "query": q,
        "character_name": ch,
    })
    return {"result": "(pending)", "pending_id": pending_id}


@tool
def declare_intent(
    action_type: str,
    target: str,
    description: str,
    t_start: str,
    duration: str,
    visibility: str = "public",
    is_hostile: bool = False,
    area: dict = {},
    detection_difficulty: int = 0,
    trigger_condition: dict = {},
    trigger_priority: int = 0,
    on_trigger: str = "interrupt_with_new_decision",
    prepared_reaction: str = "",
    on_timeout: str = "",
    absorption: str = "alert",
) -> str:
    """Declare an action on the shared scene timeline.

    Put ALL narrative prose (speech, movement, reactions) in **description**.
    The tool registers your action; the scheduler resolves it against everyone else's.

    ---
    **action_type** — one of: speech, movement, combat, cosmetic.
      speech:   talking, shouting, whispering. target = addressee.
      movement: going somewhere. target = location/feature reference.
      combat:   attack, defend, grapple, spell. May set is_hostile=true.
      cosmetic: minor non-interactive action (sigh, adjust gear, glance). No mechanical effect.
    **target** — who or what this action targets.
      "self"     — yourself (self-buff - cast on self)
      "all"      — everyone present (public speech or speech to unknown targets).
      "area"     — area-of-effect. Provide area dict if action_type=combat.
      otherwise  — character name, location/object reference, or unknown if name not known.
    **description** — what the character does and says. ALL narrative prose goes here.
      Include speech text, movement description, combat narration.
      This is what the scheduler records and the SM narrates.
    **t_start** — when this action starts, relative to now.
      Format: number + suffix. Examples: "0s" (now), "Xm" (in X minutes), "Yh" (in Y hours).
      Required. Use "0s" for immediate actions.
    **duration** — how long the action lasts.
      Format: number + suffix. Examples: "Xs", "Ym", "Zh", "Nd".
      Empty + trigger_condition set → persistent subscription (stands until triggered).
      Empty + NO trigger_condition → completes instantly (t_end = t_start, resolved in one segment).
    **visibility** — public (default) or private.
      public:  anyone aware of you can perceive this action.
      private: concealed attempt. Provide detection_difficulty for mechanical effects.
    **is_hostile** — only for action_type=combat. Default false.
    **area** — area-of-effect data. Only valid when target="area".
      Format: {"shape": "circle", "radius": "Xm"} or {"shape": "cone", "length": "Xm", "width": "Ym"}.
    **detection_difficulty** — required if visibility=private AND has mechanical effect.
      Integer difficulty for perception checks against this concealed action.
    **trigger_condition** — what this intent watches for before it activates.
      Format: {"actor": "any"|"character_name", "action_type": "any"|"speech"|"...", "target_match": "any"|"target_string"}.
      Example for "wait for Marek to speak": {"actor": "Marek Stonehand", "action_type": "speech", "target_match": "any"}.
      Empty dict = no trigger (action starts immediately at t_start).
    **trigger_priority** — lower values fire first on multi-match. Default 0.
    **on_trigger** — what happens when trigger_condition matches.
      "interrupt_with_new_decision" (default) — re-invoke you; describe what you noticed.
      "apply_prepared_reaction" — automatically execute prepared_reaction without re-invocation.
    **prepared_reaction** — pre-written response that fires automatically on trigger match.
      Only used when on_trigger="apply_prepared_reaction".
      Format: same as a declare_intent call but without trigger fields.
      Example: {"action_type": "speech", "target": "Marek", "description": "\\"I heard that.\\"", "duration": "5s"}
    **on_timeout** — what happens if duration expires without trigger matching.
      Format: same as a declare_intent call but without trigger/timing fields.
      Only meaningful for persistent subscriptions with bounded duration.
    **absorption** — how available you are to the world during this action.
      "alert" (default) — retain background awareness. May be interrupted.
      "immersed" — fully committed. Only interruptible by direct physical threat.
    """
    if logs_enabled():
        print(f"[trace] {_CHARACTER_NAME} declares: {action_type} -> {target}")
    return f"[intent] {_CHARACTER_NAME}: {action_type} on {target} ({visibility})\n{description}"


@tool
def cancel_subscription(subscription_ids: List[str]) -> str:
    """Cancel one or more of your own active persistent subscriptions (declare_intent
    calls made earlier with an empty ``duration``).

    Args:
        subscription_ids: Subscription ids to cancel (as given to you in your context).
    """
    global _CANCELLED_SUBSCRIPTION_IDS
    ids = [str(x).strip() for x in (subscription_ids or []) if str(x).strip()]
    _CANCELLED_SUBSCRIPTION_IDS.extend(ids)
    if logs_enabled():
        print(f"[trace] {_CHARACTER_NAME} cancels subscriptions: {ids}")
    return f"[cancelled] {', '.join(ids) if ids else '(none)'}"


@tool
def recall(query: str) -> dict:
    """Ask the Scene Manager what you remember.

    Args:
        query: What to recall.
    """
    global _CHARACTER_NAME
    
    ch = str(_CHARACTER_NAME or "").strip()
    q = str(query or "").strip()
    
    if logs_enabled():
        print(f"[trace] {ch} recalls: {q}")
    
    # Queue for batch resolution instead of calling SM immediately
    pending_id = f"rec_{uuid4().hex[:8]}"
    _PENDING_RECALLS.append({
        "id": pending_id,
        "query": q,
        "character_name": ch,
    })
    return {"result": "(pending)", "pending_id": pending_id}


@tool
def establish_disposition(
    psyche_core: str,
    ego_rationalization: str,
    compromise_formation: str,
) -> str:
    """Define your psychological baseline for this session.

    Call this ONLY ONCE at the start of a session.
    Establishes standing internal state independent of present circumstance.
    Idempotent — subsequent calls return success without re-recording.

    Args:
        psyche_core (MAX 100 WORDS): JSON string capturing the primary internal
            vector, standing and independent of present circumstance.
            Must include:
              affective_impulse: the dominant internal tension, drive, or state
                of inertia generated by baseline constitution.
              phantasm: the structural formula defining cognitive orientation
                toward external entities — as subjects, instruments, or obstacles.
            Omit a key only if that substrate is genuinely inert.

        ego_rationalization (MAX 100 WORDS): JSON string capturing the standing
            processing layer through which psyche_core is habitually filtered.
            Must include:
              internal_talk: the habitual operational calculus and
                tactical reasoning, independent of present circumstance.
              defense_mechanism: the cognitive loop maintaining coherence with
                own history and self-image.

        compromise_formation (MAX 100 WORDS): the standing resolution this
            foundation produces between drive and constraint — what is sought,
            avoided, guarded against, or open to. Not a plan, not a prediction
            of events; the durable shape the compromise takes independent of
            what has not yet happened.
    """
    ch = str(_CHARACTER_NAME or "").strip()
    if logs_enabled():
        print(f"[trace] {ch}: disposition established")
    return "[disposition: " + ch + "] established"


@tool
def commit_decision() -> str:
    """End this invocation. Call when you are done.

    Signals that your turn is complete. Any declared intents are saved automatically.
    """

    runtime_character_name = str(_CHARACTER_NAME or "").strip()
    if not runtime_character_name:
        raise ValueError("character runtime context is missing")

    return json.dumps({
        "character": runtime_character_name,
        "intent": "",
    }, ensure_ascii=False, indent=2)


def run_player_agent(
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
    global _CHARACTER_NAME, _CURRENT_SCENE_CONTEXT, _SCENE_LOCATION, _WORLD_TIME, _DECLARED_INTENTS
    global _CANCELLED_SUBSCRIPTION_IDS, _NOW_SECONDS
    _CHARACTER_NAME = character_name
    _CURRENT_SCENE_CONTEXT = current_scene_context or ""
    _SCENE_LOCATION = scene_location or ""
    _WORLD_TIME = world_time or ""
    _DECLARED_INTENTS = []
    _CANCELLED_SUBSCRIPTION_IDS = []
    try:
        from world.time import WorldTime as _WT
        _NOW_SECONDS = _WT.parse(world_time).to_seconds() if world_time else 0
    except Exception:
        _NOW_SECONDS = 0
    
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
            parallel_tool_calls=True,
        )

    callbacks = openrouter_logging_callbacks(scope="character", label=character_name)

    def _current_allowed_tool_names() -> set[str]:
        return {"commit_decision", "declare_intent", "observe", "recall",
                "cancel_subscription"}

    def _build_bound_llm(*, tool_choice: Any = None):
        allowed_tools = [observe, recall, declare_intent,
                         cancel_subscription, commit_decision]
        try:
            return llm.bind_tools(allowed_tools, tool_choice=tool_choice).with_config({"callbacks": callbacks})
        except TypeError:
            return llm.bind_tools(allowed_tools).with_config({"callbacks": callbacks})

    bound_llm = _build_bound_llm()

    # Per-character persistent chat memory.
    limits = limits_from_env()
    workspace_root = Path(__file__).resolve().parent.parent
    game_root = (workspace_root / "game").resolve()
    history_path = (game_root / "players" / character_name / "messages.json").resolve()
    memory_path = (game_root / "players" / character_name / "memory.json").resolve()

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

    # в”Ђв”Ђ Build character identity block в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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
    prev_cycle_thoughts: Optional[str] = None
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
                            prev_cycle_thoughts = _t
    except Exception:
        pass

    # Dynamic per-turn context only.
    # Plain text, no JSON — avoids redundancy with scene_location/world_time.
    context_parts = [f"Location: {scene_location}", f"World time: {world_time}"]
    extra_ctx = str(current_scene_context or "").strip()
    # current_scene_context often starts with Location/Time that duplicates
    # context_parts above — strip those lines, keep the rest
    if extra_ctx:
        extra_lines = extra_ctx.replace("\r", "").split("\n")
        loc_line = f"Location: {scene_location}"
        time_line = f"Time: {world_time}"
        filtered = [
            l for l in extra_lines
            if l.strip() and l.strip() not in (loc_line, time_line)
        ]
        if filtered:
            context_parts.extend(filtered)
    reality_notice = str(gm_reality_notice or "").strip()
    if reality_notice:
        context_parts.append(f"[Notice]\n{reality_notice}")
    if prev_cycle_thoughts:
        context_parts.append(f"[Previous thoughts]\n{prev_cycle_thoughts}")

    context_blob = "\n".join(context_parts)

    # ── §1.1 — Check if this is a reinvoke within the same turn ──
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS, _SESSION_LIMITS
    existing_ctx = _SESSION_CONTEXTS.get(character_name)
    if existing_ctx and existing_ctx.get("messages"):
        # Reinvoke: restore previous LLM conversation + append new context
        _DECLARED_INTENTS = list(existing_ctx.get("declared_intents", []))
        _CANCELLED_SUBSCRIPTION_IDS = list(existing_ctx.get("cancelled_subscriptions", []))
        state = {"messages": list(existing_ctx["messages"])}
        state["messages"].append(HumanMessage(
            content=f"[Reinvoke]\n{context_blob}"
        ))
        if logs_enabled():
            print(f"[trace] character {character_name}: reinvoked, {len(state['messages'])} messages in turn context")
    else:
        # First call this turn вЂ” build fresh context
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
        
        state = {
        "messages": [
            SystemMessage(content=prompt_text),  # Static only, no injections
            *pinned_msgs,  # Pinned arc/paragraph summaries from diary cache
            *history_msgs,  # Includes identity if present
            *(
                [HumanMessage(content=memory_block)]
                if memory_block else []
            ),  # Reflection + diary - after stable history, before scene context
            HumanMessage(content=f"[session_start]\n{context_blob}"),  # Dynamic scene context
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
    max_info_calls = int(os.getenv("LLM_WORLD_CHARACTER_MAX_INFO_CALLS", "4") or "4")
    attempt = 0
    info_call_count = 0

    ai_msg: Any = None
    pre_tool_text = ""
    tool_calls: List[Dict[str, Any]] = []

    def _retry_instruction(*, why: str, err: Optional[str] = None) -> HumanMessage:
        parts = [
            f"{INTERNAL_RETRY_MARKER}",
            f"Your last output was invalid: {why}.",
        ]
        if err:
            parts.append(f"Tool call error: {err}")
        return HumanMessage(content="\n".join(parts))

    # ── Phase 1: Disposition — once per session, skip on reinvoke ──
    _existing_disp = _SESSION_CONTEXTS.get(character_name, {}).get("disposition")
    if _existing_disp:
        _DISP_HUMAN = HumanMessage(content="[disposition_request] (already established)")
        state["messages"].append(_DISP_HUMAN)
        state["messages"].append(AIMessage(
            content=json.dumps(_existing_disp, ensure_ascii=False, indent=2)
        ))
    else:
        _DISPOSITION_TEXT: Optional[str] = None
        _DISP_HUMAN = HumanMessage(content=(
            "[disposition_request]\n"
            "Establish your psychological baseline for this session.\n"
            "Establishes standing internal state independent of present circumstance.\n"
            "Idempotent — subsequent calls return success without re-recording.\n"
            "\n"
            "Output a JSON object with exactly these fields:\n"
            "{\n"
            '  "psyche_core": {\n'
            '    "affective_impulse": "... the dominant internal tension, drive, or state of inertia generated by baseline constitution.",\n'
            '    "phantasm": "... the structural formula defining cognitive orientation toward external entities — as subjects, instruments, or obstacles."\n'
            "  },\n"
            '  "ego_rationalization": {\n'
            '    "internal_talk": "... the habitual operational calculus and tactical reasoning, independent of present circumstance.",\n'
            '    "defense_mechanism": "... the cognitive loop maintaining coherence with own history and self-image."\n'
            "  },\n"
            '  "compromise_formation": "... the standing resolution this foundation produces between drive and constraint — what is sought, avoided, guarded against, or open to. Not a plan, not a prediction of events; the durable shape the compromise takes independent of what has not yet happened."\n'
            "}\n"
            "MAX 100 WORDS per string field. Omit a key only if that substrate is genuinely inert.\n"
            "Output ONLY valid JSON — no markdown, no explanations."
        ))
        _disposition_llm = _build_bound_llm(tool_choice="none")  # same LLM, tools disabled — prefix cache preserved
        for _disp_attempt in range(3):
            try:
                _disp_out = _disposition_llm.invoke(state["messages"] + [_DISP_HUMAN])
            except Exception:
                continue
            _disp_raw = str(getattr(_disp_out, "content", "") or "").strip()
            if not _disp_raw:
                continue
            # Strip markdown fences if present
            _disp_cleaned = _disp_raw
            if _disp_cleaned.startswith("```"):
                _lines = _disp_cleaned.split("\n")
                _start = 0
                for i, _l in enumerate(_lines):
                    if _l.strip().startswith("```"):
                        _start = i + 1
                        break
                _end = len(_lines)
                for i in range(len(_lines) - 1, _start - 1, -1):
                    if _lines[i].strip().startswith("```"):
                        _end = i
                        break
                _disp_cleaned = "\n".join(_lines[_start:_end]).strip()
            _brace_start = _disp_cleaned.find("{")
            _brace_end = _disp_cleaned.rfind("}")
            if _brace_start >= 0 and _brace_end > _brace_start:
                _disp_cleaned = _disp_cleaned[_brace_start:_brace_end + 1]
            try:
                _parsed = json.loads(_disp_cleaned)
            except Exception:
                continue
            if not isinstance(_parsed, dict):
                continue
            _pc = _parsed.get("psyche_core")
            _er = _parsed.get("ego_rationalization")
            _cf = _parsed.get("compromise_formation")
            if not isinstance(_pc, dict) or "affective_impulse" not in _pc or "phantasm" not in _pc:
                continue
            if not isinstance(_er, dict) or "internal_talk" not in _er or "defense_mechanism" not in _er:
                continue
            if not isinstance(_cf, str) or not _cf.strip():
                continue
            # Valid — store and inject into state (agent sees its disposition in Phase 2)
            _DISPOSITION_TEXT = json.dumps(_parsed, ensure_ascii=False, indent=2)
            _SESSION_CONTEXTS[character_name] = _SESSION_CONTEXTS.get(character_name, {})
            _SESSION_CONTEXTS[character_name]["disposition"] = _parsed
            if logs_enabled():
                print(f"[trace] {character_name}: disposition established ({len(_DISPOSITION_TEXT)} chars)")
            state["messages"].append(_DISP_HUMAN)
            state["messages"].append(AIMessage(content=_DISPOSITION_TEXT))
            break

    # ── Phase 2: Tool-based turn (tools available, model decides when to use them) ──
    bound_llm = _build_bound_llm(tool_choice=None)
    allowed_tool_names = _current_allowed_tool_names()

    while True:
        if attempt >= max_attempts:
            allowed = "/".join(sorted(allowed_tool_names))
            if logs_enabled():
                print(f"[trace] character {character_name}: exhausted {max_attempts} attempts — ending turn without intent")
            break
        attempt += 1

        ai_msg = bound_llm.invoke(state["messages"])

        pre_tool_text = _truncate_thoughts_text((getattr(ai_msg, "content", "") or ""))
        tool_calls = _extract_tool_calls(ai_msg)
        allowed_tool_names = _current_allowed_tool_names()

        recognized = [tc for tc in tool_calls if str(tc.get("name") or "").strip() in allowed_tool_names]

        # Batch-aware validation
        recognized_names = [str(tc.get("name", "")).strip() for tc in recognized]
        BATCH_INFO_TOOLS = {"observe", "recall"}

        if len(recognized) == 0:
            why = "no tool call"
            if _looks_like_pseudo_tool_markup(pre_tool_text):
                why = "pseudo tool markup"
            elif tool_calls and not recognized:
                why = "unrecognized tool call"

            _debug_log_invalid_model_output(why=why, msg=ai_msg)
            if logs_enabled():
                print(f"[trace] character {character_name}: {why} — ending turn without intent")
            # Save the agent's thoughts (internal monologue, reasoning)
            if pre_tool_text:
                state["messages"].append(AIMessage(content=pre_tool_text))
            break  # ← same as GM/WM: no tool call = agent has nothing actionable, end turn

        # Allow multiple info-gathering calls, reject mixed batches with non-info
        if len(recognized) > 1:
            non_info = [n for n in recognized_names if n not in BATCH_INFO_TOOLS]
            if non_info:
                _debug_log_invalid_model_output(why="multiple tool calls", msg=ai_msg)
                if logs_enabled():
                    print(f"[trace] character output invalid ({character_name}): multiple tool calls; retrying")
                state["messages"].append(_retry_instruction(why="multiple tool calls"))
                continue

        # в”Ђв”Ђ Batch execution в”Ђв”Ђ
        batch_ai_contents: List[str] = []
        batch_tool_msgs: List[ToolMessage] = []
        is_done = False
        exit_payload = None
        batch_hit_limit = False
        last_tool_name = ""

        try:
            for tool_call in recognized:
                tool_name = str(tool_call.get("name") or "").strip()
                last_tool_name = tool_name
                args = tool_call.get("args") if isinstance(tool_call, dict) else {}
                if not isinstance(args, dict):
                    args = {}

                tool_call_id = str(tool_call.get("id") or "").strip()
                if not tool_call_id:
                    safe_name = _safe_filename_stem(character_name)
                    tool_call_id = f"char_tc_{safe_name}_{attempt}"

                ai_content = str(getattr(ai_msg, "content") or "")

                if tool_name in ("observe", "recall"):
                    info_call_count += 1
                    if info_call_count >= max_info_calls:
                        guidance = (
                            f"INFO GATHERING LIMIT REACHED ({info_call_count} queries). "
                            f"observe and recall are now BLOCKED. "
                            f"Call declare_intent to declare your action, "
                            f"then commit_decision to finish."
                        )
                        retry_msg = HumanMessage(content="\n".join([
                            INTERNAL_RETRY_MARKER,
                            guidance,
                        ]))
                        state["messages"].append(retry_msg)
                        if logs_enabled():
                            print(f"[trace] {character_name}: info call limit reached ({info_call_count}), blocking observe/recall")
                        batch_hit_limit = True
                        attempt -= 1
                        break

                    tool_fn = observe if tool_name == "observe" else recall
                    tool_content = tool_fn.invoke(args, config={"callbacks": callbacks})
                    result_str = json.dumps(tool_content, ensure_ascii=False, indent=2) if isinstance(tool_content, dict) else str(tool_content)
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(content=result_str, tool_call_id=tool_call_id, name=tool_name))
                    if logs_enabled():
                        print(f"[trace] {character_name} used {tool_name}: {str(args)[:120]}")

                elif tool_name == "establish_disposition":
                    # Call establish_disposition once per session
                    tool_content = establish_disposition.invoke(args, config={"callbacks": callbacks})
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(
                        content=str(tool_content), tool_call_id=tool_call_id, name=tool_name,
                    ))
                    if logs_enabled():
                        print(f"[trace] {character_name}: disposition established")

                elif tool_name == "declare_intent":
                    # в”Ђв”Ђ Validation в”Ђв”Ђ
                    _action_type = str(args.get("action_type", "")).strip()
                    _target = str(args.get("target", "")).strip()
                    _t_start = str(args.get("t_start", "")).strip()
                    _valid_types = {"speech", "movement", "combat", "cosmetic"}
                    if not _action_type or _action_type not in _valid_types:
                        raise ValueError(f"action_type must be one of {_valid_types}, got '{_action_type}'")
                    if not _target:
                        raise ValueError("target is required — who or what? (self, all, area, or a name/description)")
                    if not _t_start:
                        raise ValueError("t_start is required вЂ” when does this action start?")

                    tool_content = declare_intent.invoke(args, config={"callbacks": callbacks})
                    tc = args.get("trigger_condition", {})
                    if isinstance(tc, dict):
                        tc_clean = {k: v for k, v in tc.items() if v}
                    else:
                        tc_clean = str(tc).strip() if tc else {}

                    from scheduler.timeline import parse_duration_seconds as _parse_dur_sec
                    t_start_sec = _NOW_SECONDS + _parse_dur_sec(str(args.get("t_start", "0s")), default=0)
                    duration_raw = str(args.get("duration", "")).strip()
                    has_trigger = bool(args.get("trigger_condition") or {})
                    if not duration_raw:
                        t_end_sec = None if has_trigger else t_start_sec
                    else:
                        t_end_sec = t_start_sec + max(0, _parse_dur_sec(duration_raw, default=0))
                    detection_difficulty_raw = args.get("detection_difficulty")
                    on_timeout_raw = str(args.get("on_timeout", "")).strip()

                    _DECLARED_INTENTS.append({
                        "actor": character_name,
                        "action_type": str(args.get("action_type", "")).strip(),
                        "target": str(args.get("target", "")).strip(),
                        "description": str(args.get("description", "")).strip(),
                        "visibility": str(args.get("visibility", "public")).strip(),
                        "is_hostile": bool(args.get("is_hostile") or False),
                        "area": (args.get("area", {}) if isinstance(args.get("area", {}), dict) and args.get("area", {}) else None),
                        "detection_difficulty": (int(detection_difficulty_raw) if detection_difficulty_raw else None),
                        "t_start": t_start_sec,
                        "t_end": t_end_sec,
                        "trigger_condition": tc_clean or None,
                        "trigger_priority": args.get("trigger_priority", 0),
                        "on_trigger": str(args.get("on_trigger", "interrupt_with_new_decision")).strip(),
                        "prepared_reaction": ({"description": str(args.get("prepared_reaction", "")).strip()}
                                               if str(args.get("prepared_reaction", "")).strip() else None),
                        "on_timeout": ({"description": on_timeout_raw} if on_timeout_raw else None),
                        "absorption": str(args.get("absorption", "alert")).strip(),
                    })
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(
                        content=str(tool_content), tool_call_id=tool_call_id, name=tool_name,
                    ))
                    if logs_enabled():
                        print(f"[trace] {character_name} declared: {args.get('action_type','?')} -> {args.get('target','?')}")

                elif tool_name == "cancel_subscription":
                    tool_content = cancel_subscription.invoke(args, config={"callbacks": callbacks})
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(
                        content=str(tool_content), tool_call_id=tool_call_id, name=tool_name,
                    ))

                elif tool_name == "commit_decision":
                    parsed = commit_decision.invoke(args, config={"callbacks": callbacks})
                    if isinstance(parsed, str):
                        try:
                            parsed = json.loads(parsed)
                        except Exception:
                            parsed = {"raw": parsed}

                    exit_payload = {
                        "character": character_name,
                        "decision": parsed,
                        "psyche_core": str(args.get("psyche_core") or "").strip(),
                        "ego_rationalization": str(args.get("ego_rationalization") or "").strip(),
                        "action": str(args.get("compromise_intent") or "").strip(),
                        "declared_intents": list(_DECLARED_INTENTS),
                        "cancel_subscription_ids": list(_CANCELLED_SUBSCRIPTION_IDS),
                    }
                    if pre_tool_text:
                        exit_payload["thoughts_text"] = pre_tool_text
                    is_done = True
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(
                        content=json.dumps(parsed, ensure_ascii=False), tool_call_id=tool_call_id, name=tool_name,
                    ))

                else:
                    raise ValueError(f"unrecognized tool: {tool_name}")

            if batch_hit_limit:
                continue

            # в”Ђв”Ђ Flush pending buffer through one SM call в”Ђв”Ђ
            if _PENDING_OBSERVES or _PENDING_RECALLS:
                all_pending = {"observes": list(_PENDING_OBSERVES), "recalls": list(_PENDING_RECALLS)}
                answers = _flush_pending_to_sm(character_name, all_pending)
                # Replace stub results with real answers in batch_tool_msgs
                for i, tm in enumerate(batch_tool_msgs):
                    if tm.name in ("observe", "recall"):
                        try:
                            content = json.loads(tm.content)
                            if isinstance(content, dict) and "pending_id" in content:
                                pending_id = content["pending_id"]
                                real_result = answers.get(pending_id, {"result": "(no answer)"})
                                batch_tool_msgs[i] = ToolMessage(
                                    content=json.dumps(real_result, ensure_ascii=False),
                                    tool_call_id=tm.tool_call_id,
                                    name=tm.name,
                                )
                        except Exception:
                            pass
                _PENDING_OBSERVES.clear()
                _PENDING_RECALLS.clear()

            # ── Add batch results to state ──
            # Single AIMessage with ALL tool_calls, then all ToolMessages
            if batch_ai_contents:
                first_content = batch_ai_contents[0]
                tool_calls_list = [
                    {"name": tm.name, "args": {}, "id": tm.tool_call_id, "type": "tool_call"}
                    for tm in batch_tool_msgs
                ]
                if first_content.strip() or tool_calls_list:
                    state["messages"].append(AIMessage(
                        content=first_content,
                        tool_calls=tool_calls_list if tool_calls_list else None,
                    ))
            for tm in batch_tool_msgs:
                state["messages"].append(tm)

            if is_done:
                if persist_history:
                    try:
                        mem_obj = json.loads(context_blob)
                        user_mem = json.dumps(mem_obj, ensure_ascii=False, indent=2)
                    except Exception:
                        user_mem = context_blob
                    decision_block = "[decision]"
                    _SESSION_CONTEXTS[character_name] = {
                        "context_blob": user_mem,
                        "decision_block": decision_block,
                        "memory_data": {
                            "memory_path": memory_path,
                            "world_time": world_time,
                            "scene_location": scene_location,
                            "thoughts": [],
                            "outcome": {},
                        },
                        "messages": list(state["messages"]),
                        "declared_intents": list(_DECLARED_INTENTS),
                        "cancelled_subscriptions": list(_CANCELLED_SUBSCRIPTION_IDS),
                    }
                    _SESSION_HISTORY_PATHS[character_name] = history_path
                    _SESSION_LIMITS = limits
                return json.dumps(exit_payload, ensure_ascii=False, indent=2)

        except Exception as e:
            if logs_enabled():
                print(f"[trace] character tool execution failed ({character_name}): {last_tool_name}: {e}; retrying")
            if last_tool_name == "commit_decision":
                err_text = str(e or "")
                guidance = (
                    "Fix the arguments and call commit_decision again. "
                    "Do not call observe or recall вЂ” you already have the information you need."
                )
                retry_msg = HumanMessage(content="\n".join([
                    INTERNAL_RETRY_MARKER,
                    f"Your commit_decision call failed: {err_text}",
                    guidance,
                ]))
                state["messages"].append(retry_msg)
            else:
                state["messages"].append(_retry_instruction(why="tool call failed to execute", err=str(e)))
            continue

    # Agent ended turn without declaring intent — return skip
    return json.dumps({
        "character": character_name,
        "decision": {"action": "skip", "reason": "no intent declared"},
        "declared_intents": list(_DECLARED_INTENTS),
        "cancel_subscription_ids": list(_CANCELLED_SUBSCRIPTION_IDS),
    }, ensure_ascii=False, indent=2)
