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

from history_pipeline import memory_at
from memory_store import approx_token_count, limits_from_env
from record_voice import spoken
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import bind_agent_tools, build_openrouter_chat_llm, extract_reasoning, openrouter_logging_callbacks, read_prompt_text
from world.story import get_pinned_block_cache
from engine.tool_errors import tool_failure
from player.reflection import build_character_arc_block, build_character_paragraph_block, _character_dir

from .memory import update_cycle_memory


_HISTORY_NOTICE_PRINTED: set[str] = set()

# Reasoning is not part of a character's context by default. Measured on a real
# context, the deliberation it adds is tool planning in an operator's register rather
# than the character's own impulse, and it doubles the number of tool calls. The
# blocks stay switchable, for inspecting how a character perceives its scene.
_THOUGHTS_BLOCKS_IN_CONTEXT = (os.getenv("LLM_WORLD_THOUGHTS_BLOCKS", "0") or "0").strip() == "1"

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
# What each character asked and was told in the turn he is in, in the order he
# learned it. Entered into his memory where the turn ends, not where the answer
# arrives: the situation he was given stands before them.
_SESSION_ANSWERS: Dict[str, List[str]] = {}


def begin_session():
    """Begin session. All character convos stay in memory until end_session."""
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS
    _SESSION_CONTEXTS.clear()
    _SESSION_HISTORY_PATHS.clear()
    _SESSION_ANSWERS.clear()


def _valid_baseline(parsed: Any) -> bool:
    """Is this the shape a standing psychological baseline is stated in?"""
    if not isinstance(parsed, dict):
        return False
    core = parsed.get("psyche_core")
    ego = parsed.get("ego_rationalization")
    shape = parsed.get("compromise_formation")
    if not isinstance(core, dict):
        return False
    if "affective_impulse" not in core or "phantasm" not in core:
        return False
    if not isinstance(ego, dict):
        return False
    if "internal_talk" not in ego or "defense_mechanism" not in ego:
        return False
    return isinstance(shape, str) and bool(shape.strip())


def record_turn(
    character_name: str,
    *,
    path: Path,
    situation: str,
    answers: List[str] = [],
    decision: str = "",
) -> int:
    """Enter one turn into a character's memory, in the order the turn lived.

    The situation he was given, what he asked and was told, then what he decided —
    appended to his branch where the turn ends. Appended here and not at the
    session's close because the branch is a queue: a record entered at the close
    would stand after everything the world delivered in the meantime, and the
    session would read as if he had been told what happened before he was told
    where he stood.

    The file is named by the caller, which holds it where the turn is run: a
    lookup done here can find nothing — the file is registered at the end of the
    turn — and the turn would go unrecorded without a word.
    """
    if not path:
        from engine.incidents import record_incident

        record_incident(
            "character.record_turn:no_file",
            ValueError(f"{character_name}: the turn has no memory file to enter it in"),
        )
        return 0
    memory = memory_at(path, limits=_SESSION_LIMITS)
    entered = 0
    for text in [situation, *answers, decision]:
        if str(text or "").strip():
            memory.record(str(text))
            entered += 1
    return entered


def end_session():
    """End session. Close each character's memory once — the one write to a file."""
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS, _SESSION_LIMITS
    for char_name, ctx in list(_SESSION_CONTEXTS.items()):
        memory_data = ctx.get("memory_data")
        history_path = _SESSION_HISTORY_PATHS.get(char_name)
        if not history_path:
            continue
        own_memory = memory_at(history_path, limits=_SESSION_LIMITS)
        if not own_memory.branch_open:
            continue
        memory: list = []
        outcome: dict = {}
        try:
            # What he read, in the order he read it — needed below, because the
            # merge may trim the front of it away.
            memory = own_memory.records()
            # The session's branch closes here: what it wrote that is durable is
            # merged into his memory, and the rest is dropped.
            outcome = own_memory.close_branch()
        except Exception as e:
            memory, outcome = [], {}
            print(f"  {char_name}: closing his memory failed: {e}")
        else:
            print(f"  {char_name}: memory closed "
                  f"({outcome.get('merged', 0)} record(s) entered)")
        # Whatever the merge just dropped from this record is still in hand, so
        # the diary entry covering it is written now rather than lost — from the
        # same prompt, the same pinned blocks and the same records he read.
        if outcome.get("trimmed_out") and memory:
            # One summary call per character, after that character's file is
            # already final: it is said out loud, or the close looks finished
            # while it is still running.
            print(f"  {char_name}: covering {len(outcome['trimmed_out'])} retired "
                  f"turn(s) — one summary call…")
            try:
                from player.reflection import cover_retired_turns
                cover_retired_turns(
                    char_name, history_path, memory,
                    prefix=character_pinned_blocks(char_name),
                )
                print(f"  {char_name}: retired turns covered")
            except Exception as e:
                print(f"  {char_name}: covering retired turns failed: {e}")
        if memory_data:
            try:
                from player.memory import update_cycle_memory as _utm
                for _turn in memory_data.get("turns") or []:
                    _utm(
                        memory_data["memory_path"],
                        character_name=char_name,
                        world_time=_turn.get("world_time", ""),
                        scene_location=_turn.get("scene_location", ""),
                        outcome=_turn.get("outcome", {}),
                    )
            except Exception:
                pass
    _SESSION_CONTEXTS.clear()
    _SESSION_HISTORY_PATHS.clear()
    _SESSION_ANSWERS.clear()
    _SESSION_LIMITS = None


def set_sm_resolver(resolver):
    """Set the SM resolver function for observe/recall."""
    global _SM_RESOLVER
    _SM_RESOLVER = resolver


def continues_his_turn(character_name: str) -> bool:
    """Whether this character's next turn continues the one he is already in.

    A character in a turn reads the conversation his turn is running in: a record
    entered in his memory while it runs is not part of that conversation, so what
    he was told reaches him only if it is carried to him. A character whose turn is
    the first of his session reads his memory where the turn begins, and what was
    entered before it already stands there — carried to him as well, it would stand
    twice in one context.
    """
    ctx = _SESSION_CONTEXTS.get(str(character_name or "").strip())
    return bool(ctx and ctx.get("messages"))


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

    # An answer belongs to the question it was written for. What comes back is
    # one answer per question id; anything a question did not receive stays
    # unanswered rather than being filled with another question's text.
    if isinstance(answer, str) and answer.strip().startswith("{"):
        try:
            parsed = json.loads(answer)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            out: Dict[str, Any] = {}
            for pid in id_map:
                text = str(parsed.get(pid) or "").strip()
                out[pid] = {"result": text} if text else {"result": "(no answer)"}
            return out

    if logs_enabled():
        print(f"[trace] {player_name}: the SM returned no per-question answers "
              f"for {len(id_map)} question(s)")
    return {pid: {"result": "(no answer)"} for pid in id_map}


def _answers_to_records(
    pending: Dict[str, List[Dict[str, Any]]],
    answers: Dict[str, Any],
) -> List[str]:
    """What a character's questions and answers are, as records of his own.

    A question he asked and an answer he was given are his: nothing else keeps
    them, and a turn that ends without them leaves him unable to say what he was
    told. The question stands in his own words, the answer as it was written for
    him, and one that came back empty is not entered at all — nothing was learned.
    """
    out: List[str] = []
    items = list(pending.get("observes") or []) + list(pending.get("recalls") or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        question = " ".join(str(item.get("query") or "").split())
        if not pid or not question:
            continue
        answer = str((answers.get(pid) or {}).get("result") or "").strip()
        if not answer or answer in ("(no answer)", "(SM unavailable)"):
            continue
        if answer.startswith("(SM error"):
            continue
        # No id: the number addresses a question inside one batch and is spent
        # when that batch is answered. What he asked is his question's own words.
        out.append(f"[player_answer] {question}\n  {answer}")
    return out


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
    """Ask what you perceive around you.

    Perception is how the scene reaches you beyond what you were given when
    the turn began: it answers with what this character could notice from where
    they stand, and with nothing else. Recall answers from memory instead.

    Args:
        query: What to attend to — a person, an object, a region, or a quality
            of the surroundings. Empty asks for general awareness.
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
    area: dict = {},
    trigger_condition: str = "",
    on_trigger: str = "interrupt_with_new_decision",
    prepared_reaction: str = "",
    absorption: str = "alert",
) -> str:
    """State what you intend to attempt, and over what span.

    An intent is an attempt, not an outcome: it says what you mean to do and is
    resolved together with everything else active in the scene, so nothing
    declared here settles on its own. Declare once you know what you are doing,
    then commit the decision so that time may advance.

    Args:
        action_type: The kind of behaviour — speech, movement, combat, or
            cosmetic. The kind decides which of the remaining fields carry
            meaning.
        target: What the behaviour is directed at: an entity, "self" when it is
            turned on yourself, "all" when it is addressed to whoever is
            present, or "area" when it covers a region rather than a thing.
        description: Everything the character does and says, written out: the
            words spoken, the movement, the manner of it. The scene is narrated
            from this text, so nothing observable may be left out of it.
        t_start: When the attempt begins, as a duration from now (s, m, h, d);
            "0s" begins immediately.
        duration: How long the attempt is sustained, in the same units. Empty
            with no trigger makes it instantaneous; empty with a trigger makes
            it a standing readiness that holds until the trigger fires.
        visibility: "public" makes the attempt apparent to anyone who could
            notice you; "private" is a concealed attempt, learned of by others
            only if a perception check succeeds.
        area: The extent affected when the target is "area" — its shape and
            its size.
        trigger_condition: Turns the intent into a standing readiness rather
            than an immediate act: in words, what you are watching for. It is
            held until the condition does.
        on_trigger: What happens when the condition is met — interrupt you for
            a new decision, or carry out the prepared reaction without asking
            you again.
        prepared_reaction: What you do when the condition is met, written out in
            advance so that no new decision is needed. It is a declaration of
            the same shape, without the trigger fields.
        absorption: How available you stay to events beyond your own attempt,
            for as long as it lasts: "alert" keeps background awareness,
            "immersed" commits your attention to the attempt and answers only
            to direct physical threat.
    """
    if logs_enabled():
        print(f"[trace] {_CHARACTER_NAME} declares: {action_type} -> {target}")
    return f"Declared: {action_type} on {target} ({visibility}).\n{description}"


@tool
def cancel_subscription(subscription_ids: List[str]) -> str:
    """Withdraw standing intents you declared earlier.

    Only your own, and only those that wait for a trigger: an attempt that was
    going to happen at a set time cannot be withdrawn this way.

    Args:
        subscription_ids: The intents to withdraw, by the ids given to you in
            your context.
    """
    global _CANCELLED_SUBSCRIPTION_IDS
    ids = [str(x).strip() for x in (subscription_ids or []) if str(x).strip()]
    _CANCELLED_SUBSCRIPTION_IDS.extend(ids)
    if logs_enabled():
        print(f"[trace] {_CHARACTER_NAME} cancels subscriptions: {ids}")
    return (f"You withdrew: {', '.join(ids)}." if ids
            else "You withdrew nothing.")


@tool
def recall(query: str) -> dict:
    """Ask what you remember.

    Memory answers about what has passed — earlier turns, earlier places, what
    you were told and by whom. What stands in front of you now is observe.

    Args:
        query: What to remember — an event, a person, a place, a question.
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
def commit_decision() -> str:
    """Declare your planning finished, so that time may advance.

    Use it once everything you mean to attempt has been declared: the turn then
    runs forward, the declared attempts are resolved together, and what comes
    of them reaches you with the next turn.
    """

    runtime_character_name = str(_CHARACTER_NAME or "").strip()
    if not runtime_character_name:
        raise ValueError("character runtime context is missing")

    return json.dumps({
        "character": runtime_character_name,
        "intent": "",
    }, ensure_ascii=False, indent=2)


def character_pinned_blocks(character_name: str) -> List[SystemMessage]:
    """The blocks that sit above this character's history in its own context.

    The description is pinned here: it is the one thing about a character that
    does not change, so it lives above the history instead of being written into
    it and re-inserted whenever a trim drops it. The summaries come from the
    diary through the shared cache, so a block asked for twice in one process is
    the same object the character's invocation already paid for.
    """
    blocks: List[SystemMessage] = []
    try:
        cache = get_pinned_block_cache()
        char_dir = _character_dir(character_name)
        desc_path = char_dir / "description.json"
        if desc_path.exists():
            block = cache.get("identity", desc_path, _build_description_block)
            if block:
                blocks.append(SystemMessage(content=f"## Character — {character_name}\n{block}"))
        diary_path = char_dir / "diary.json"
        if diary_path.exists():
            block = cache.get("arc", diary_path, build_character_arc_block)
            if block:
                blocks.append(SystemMessage(content=f"## Arc Summaries\n{block}"))
            block = cache.get("paragraph", diary_path, build_character_paragraph_block)
            if block:
                blocks.append(SystemMessage(content=f"## Paragraph Summaries\n{block}"))
    except Exception as e:
        if logs_enabled():
            print(f"[trace] character {character_name}: pinned blocks failed: {e}")
    return blocks


def _build_description_block(description_path: Path) -> str:
    """The character's narrative description, as its own pinned block."""
    from world.context import build_character_info_block
    return build_character_info_block(description_path.parent) or ""


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
    establish_disposition: bool = True,
    prompt_path: str = "agents/character_agent/prompt.txt",
    llm: Optional[ChatOpenAI] = None,
    memory_file: Optional[Path] = None,
) -> str:
    global _CHARACTER_NAME, _CURRENT_SCENE_CONTEXT, _SCENE_LOCATION, _WORLD_TIME, _DECLARED_INTENTS
    global _CANCELLED_SUBSCRIPTION_IDS, _NOW_SECONDS
    _CHARACTER_NAME = character_name
    _CURRENT_SCENE_CONTEXT = current_scene_context or ""
    _SCENE_LOCATION = scene_location or ""
    _WORLD_TIME = world_time or ""
    _DECLARED_INTENTS = []
    _SEEN_INTENT_KEYS = set()
    _CANCELLED_SUBSCRIPTION_IDS = []
    try:
        from world.time import WorldTime as _WT
        _NOW_SECONDS = _WT.parse(world_time).to_seconds() if world_time else 0
    except Exception:
        _NOW_SECONDS = 0
    
    prompt_text = read_prompt_text(prompt_path).replace("{name}", str(character_name or ""))

    # The character's own text is its inner state. It is kept small because it is
    # replayed in the session transcript and written into the character's memory.
    _THOUGHTS_MAX_CHARS = int(os.getenv("LLM_WORLD_CHARACTER_THOUGHTS_MAX_CHARS", "1200") or "1200")

    def _truncate_thoughts_text(text: str) -> str:
        t = (text or "").strip()
        if len(t) <= _THOUGHTS_MAX_CHARS:
            return t
        return t[:_THOUGHTS_MAX_CHARS].rstrip() + " [...truncated]"

    def _record_thoughts(reasoning: str) -> None:
        """Record the model's own reasoning as inner state, when switched on."""
        if not _THOUGHTS_BLOCKS_IN_CONTEXT:
            return
        t = _truncate_thoughts_text(reasoning)
        if not t:
            return
        state["messages"].append(HumanMessage(content=f"[thoughts]\n{t}"))

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
            thinking=False,  # a character acts; adjudicating its own actions is not its part
        )

    callbacks = openrouter_logging_callbacks(scope="character", label=character_name)

    def _current_allowed_tool_names() -> set[str]:
        return {"commit_decision", "declare_intent", "observe", "recall",
                "cancel_subscription"}

    def _build_bound_llm(*, tool_choice: Any = None):
        allowed_tools = [observe, recall, declare_intent,
                         cancel_subscription, commit_decision]
        return bind_agent_tools(llm, allowed_tools, callbacks=callbacks, tool_choice=tool_choice)

    bound_llm = _build_bound_llm()

    # Per-character persistent chat memory.
    limits = limits_from_env()
    workspace_root = Path(__file__).resolve().parent.parent
    game_root = (workspace_root / "game").resolve()
    # Where his memory lives: what the game gives him, or what a caller names when
    # it runs a turn outside the game — and then both of his files stand together.
    if memory_file:
        history_path = Path(memory_file).resolve()
        memory_path = history_path.parent / "memory.json"
    else:
        history_path = (game_root / "players" / character_name / "messages.json").resolve()
        memory_path = (game_root / "players" / character_name / "memory.json").resolve()

    def _history_to_messages(history: List[Dict[str, str]]):
        # Every stored entry is a record the framework authored — never a turn of
        # the model's own — so all of them are read back the same way, whatever
        # role they were written with, and in the world's voice: what the file
        # keeps for the code is not what he reads.
        msgs = []
        for h in history:
            role = (h.get("role") or "").strip().lower()
            said = spoken(str(h.get("content") or ""))
            if role in ("user", "assistant") and said.strip():
                msgs.append(HumanMessage(content=said))
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
    # -- The character's own sheet --
    # Every block whose content changed is written into his history, beside the
    # record it changed from: nothing is taken out of his memory — what leaves is
    # what a trim takes. The description is not here: it never changes, so it is
    # pinned above the history.
    #
    # His session works on a branch of his own memory: what he is told, and what
    # his sheet becomes, are entered there and merged when the session ends. He
    # reads the branch and the thread as one, so nothing he was told is visible
    # only to a later session.
    own_memory = memory_at(history_path)
    if persist_history:
        own_memory.open_branch()
        try:
            from .entity_blocks import sync_character_sheet
            sync_character_sheet(character_name, history_path, limits)
        except Exception as e:
            if logs_enabled():
                print(f"[trace] character {character_name}: sheet sync failed: {e}")

    # Build dynamic memory block (reflection + relationships + diary) - placed after stable history
    # so the stable persisted turns remain a clean cacheable prefix
    _memory_parts: list[str] = []
    if reflection_data:
        _memory_parts.append("## Self Reflection")
        _memory_parts.append(json.dumps(reflection_data, ensure_ascii=False, indent=2))
    if stale_rels_note:
        _memory_parts.append(stale_rels_note)
    # The diary travels in the pinned Arc Summaries / Paragraph Summaries blocks,
    # which persist across trims, so it is not repeated here as well. It is only
    # carried in this block when no persisted history means no pinned blocks.
    if diary_text and not persist_history:
        _memory_parts.append("## Your Diary")
        _memory_parts.append(diary_text)
    memory_block = "\n\n".join(_memory_parts)

    # Load existing history — the thread plus what this session's branch has
    # added, so a record entered mid-session is read in the same session.
    history_msgs = _history_to_messages(own_memory.records())

    # What the character reasoned is not re-read here: within a session the
    # conversation itself already carries every [thoughts] record, and across
    # sessions reasoning is not part of the character's memory.

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
        context_parts.append(f"What you are told:\n{reality_notice}")

    context_blob = "\n".join(context_parts)

    # ── §1.1 — Check if this is a reinvoke within the same turn ──
    global _SESSION_CONTEXTS, _SESSION_HISTORY_PATHS, _SESSION_LIMITS
    existing_ctx = _SESSION_CONTEXTS.get(character_name)
    if existing_ctx and existing_ctx.get("messages"):
        # Reinvoke: restore previous LLM conversation + append new context
        _DECLARED_INTENTS = list(existing_ctx.get("declared_intents", []))
        _SEEN_INTENT_KEYS = {
            (character_name, d.get("action_type",""), str(d.get("target","")), str(d.get("description",""))[:80])
            for d in _DECLARED_INTENTS if isinstance(d, dict)
        }
        _CANCELLED_SUBSCRIPTION_IDS = list(existing_ctx.get("cancelled_subscriptions", []))
        state = {"messages": list(existing_ctx["messages"])}
        state["messages"].append(HumanMessage(
            content=f"What has happened since you decided:\n{context_blob}"
        ))
        if logs_enabled():
            print(f"[trace] character {character_name}: reinvoked, {len(state['messages'])} messages in turn context")
    else:
        # First call this turn вЂ” build fresh context
        # Build pinned summaries from the diary. The block is rebuilt only when
        # diary.json itself changes, and the shared cache serves every character:
        # the source path is part of the cache key.
        pinned_msgs: List[SystemMessage] = []
        if persist_history:
            pinned_msgs = character_pinned_blocks(character_name)
        
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
        ]
    }

        if logs_enabled() and history_path.exists() and character_name not in _HISTORY_NOTICE_PRINTED:
            loaded = own_memory.records()
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

    def _retry_instruction(*, why: str, err: Optional[str] = None) -> HumanMessage:
        parts = [
            f"{INTERNAL_RETRY_MARKER}",
            f"Your last output was invalid: {why}.",
        ]
        if err:
            parts.append(f"Tool call error: {err}")
        return HumanMessage(content="\n".join(parts))

    # ── Phase 1: Disposition — the opening of a cycle ──
    # A cycle opens with the psyche he stands on. It is asked at every opening he
    # is called to declare an intent at, and what he states is entered in his own
    # memory where it was stated, so his history keeps how he stood at each of
    # them. Within one cycle he is not asked twice: a character called back to
    # decide again continues the cycle he is in, and his opening stands.
    _DISPOSITION_TEXT: Optional[str] = None
    if establish_disposition:
        _DISP_QUESTION = (
            "Establish your standing psychological baseline.\n"
            "Establishes standing internal state independent of present circumstance.\n"
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
        )
        _DISP_HUMAN = HumanMessage(content="[disposition_request]\n" + _DISP_QUESTION)
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
            if not _valid_baseline(_parsed):
                continue
            # Valid — store and inject into state (agent sees its disposition in Phase 2)
            _DISPOSITION_TEXT = json.dumps(_parsed, ensure_ascii=False, indent=2)
            _SESSION_CONTEXTS[character_name] = _SESSION_CONTEXTS.get(character_name, {})
            _SESSION_CONTEXTS[character_name]["disposition"] = _parsed
            if persist_history:
                # The opening belongs in his memory: what he states here is what
                # he stood on in this cycle, and the record of it is read beside
                # the turns he made in it.
                own_memory.record(
                    "[disposition]\nYour standing psychological baseline:\n"
                    + _DISPOSITION_TEXT
                )
            if logs_enabled():
                print(f"[trace] {character_name}: disposition established ({len(_DISPOSITION_TEXT)} chars)")
            state["messages"].append(HumanMessage(content=(
                "Your standing psychological baseline:\n"
                + _DISPOSITION_TEXT
            )))
            break

    _save_character_prompt_snapshot(
        character_name=character_name,
        messages=list(state.get("messages") or []),
        scene_location=scene_location,
        world_time=world_time,
    )

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
        ai_reasoning = extract_reasoning(ai_msg)
        tool_calls = _extract_tool_calls(ai_msg)
        allowed_tool_names = _current_allowed_tool_names()

        recognized = [tc for tc in tool_calls if str(tc.get("name") or "").strip() in allowed_tool_names]

        if len(recognized) == 0:
            why = "no tool call"
            if _looks_like_pseudo_tool_markup(pre_tool_text):
                why = "pseudo tool markup"
            elif tool_calls and not recognized:
                why = "unrecognized tool call"

            _debug_log_invalid_model_output(why=why, msg=ai_msg)
            if logs_enabled():
                print(f"[trace] character {character_name}: {why} — ending turn without intent")
            # What the model reasoned is the character's inner state; text it wrote
            # outside a call is not, and is retained in no form.
            _record_thoughts(ai_reasoning)
            break  # ← same as GM/WM: no tool call = agent has nothing actionable, end turn

        # The model may emit any combination of calls in one response; they are
        # executed in the order given.
        # в”Ђв”Ђ Batch execution в”Ђв”Ђ
        batch_ai_contents: List[str] = []
        batch_tool_msgs: List[ToolMessage] = []
        is_done = False
        exit_payload = None
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
                    tool_fn = observe if tool_name == "observe" else recall
                    tool_content = tool_fn.invoke(args, config={"callbacks": callbacks})
                    result_str = json.dumps(tool_content, ensure_ascii=False, indent=2) if isinstance(tool_content, dict) else str(tool_content)
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(content=result_str, tool_call_id=tool_call_id, name=tool_name))
                    if logs_enabled():
                        print(f"[trace] {character_name} used {tool_name}: {str(args)[:120]}")

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

                    from scheduler.timeline import (
                        new_intent_id,
                        parse_duration_seconds as _parse_dur_sec,
                    )
                    t_start_sec = _NOW_SECONDS + _parse_dur_sec(str(args.get("t_start", "0s")), default=0)
                    duration_raw = str(args.get("duration", "")).strip()
                    has_trigger = bool(str(args.get("trigger_condition") or "").strip())
                    if not duration_raw:
                        t_end_sec = None if has_trigger else t_start_sec
                    else:
                        t_end_sec = t_start_sec + max(0, _parse_dur_sec(duration_raw, default=0))

                    # Skip if identical intent already declared (deduplicate)
                    _dup_key = (character_name, _action_type, str(args.get("target", "")).strip(), str(args.get("description", "")).strip()[:80])
                    if _dup_key in _SEEN_INTENT_KEYS:
                        if logs_enabled():
                            print(f"[trace] {character_name}: skipping duplicate declare_intent ({_action_type} -> {args.get('target','?')})")
                        batch_ai_contents.append(ai_content)
                        batch_tool_msgs.append(ToolMessage(
                            content="(duplicate — already declared)", tool_call_id=tool_call_id, name=tool_name,
                        ))
                        continue
                    _SEEN_INTENT_KEYS.add(_dup_key)

                    _DECLARED_INTENTS.append({
                        "id": new_intent_id(),
                        "actor": character_name,
                        "action_type": _action_type,
                        "target": str(args.get("target", "")).strip(),
                        "description": str(args.get("description", "")).strip(),
                        "visibility": str(args.get("visibility", "public")).strip(),
                        "area": (args.get("area", {}) if isinstance(args.get("area", {}), dict) and args.get("area", {}) else None),
                        "t_start": t_start_sec,
                        "t_end": t_end_sec,
                        "trigger_condition": tc_clean or None,
                        "on_trigger": str(args.get("on_trigger", "interrupt_with_new_decision")).strip(),
                        "prepared_reaction": ({"description": str(args.get("prepared_reaction", "")).strip()}
                                               if str(args.get("prepared_reaction", "")).strip() else None),
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
                    if ai_reasoning:
                        exit_payload["thoughts_text"] = ai_reasoning
                    is_done = True
                    batch_ai_contents.append(ai_content)
                    batch_tool_msgs.append(ToolMessage(
                        content=json.dumps(parsed, ensure_ascii=False), tool_call_id=tool_call_id, name=tool_name,
                    ))

                else:
                    raise ValueError(f"unrecognized tool: {tool_name}")

            # в”Ђв”Ђ Flush pending buffer through one SM call в”Ђв”Ђ
            if _PENDING_OBSERVES or _PENDING_RECALLS:
                all_pending = {"observes": list(_PENDING_OBSERVES), "recalls": list(_PENDING_RECALLS)}
                answers = _flush_pending_to_sm(character_name, all_pending)
                # What he asked and what he was told are his from here on: the
                # session keeps them, and they enter his memory when it closes.
                _SESSION_ANSWERS.setdefault(character_name, []).extend(
                    _answers_to_records(all_pending, answers))
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
            # The text that accompanied the calls is the character's own inner
            # state and is recorded as such; the call carries no content.
            if batch_ai_contents:
                first_content = batch_ai_contents[0]
                tool_calls_list = [
                    {"name": tm.name, "args": {}, "id": tm.tool_call_id, "type": "tool_call"}
                    for tm in batch_tool_msgs
                ]
                _record_thoughts(ai_reasoning)
                if tool_calls_list:
                    _batch_msg = AIMessage(content="", tool_calls=tool_calls_list)
                    if ai_reasoning:
                        _batch_msg.additional_kwargs = {"reasoning_content": ai_reasoning}
                    state["messages"].append(_batch_msg)
            for tm in batch_tool_msgs:
                state["messages"].append(tm)

            if is_done:
                if persist_history:
                    try:
                        mem_obj = json.loads(context_blob)
                        user_mem = json.dumps(mem_obj, ensure_ascii=False, indent=2)
                    except Exception:
                        user_mem = context_blob
                    # The decision block is what this character committed to, taken
                    # from the declared intents themselves — not generated text and
                    # never a placeholder. Empty when nothing was declared, and an
                    # empty block is not written at all.
                    _lines: List[str] = []
                    for _it in _DECLARED_INTENTS:
                        _t0 = int(_it.get("t_start") or 0) - int(_NOW_SECONDS or 0)
                        _span = f"+{_t0}s"
                        if _it.get("t_end") is not None:
                            _span += f"..+{int(_it['t_end']) - int(_NOW_SECONDS or 0)}s"
                        _lines.append(
                            f"[intent] {_it.get('action_type', '?')} -> {_it.get('target', '?')} "
                            f"({_it.get('visibility', 'public')}, {_span})"
                        )
                        _desc = str(_it.get("description") or "").strip()
                        if _desc:
                            _lines.append("  " + _desc)
                    if _CANCELLED_SUBSCRIPTION_IDS:
                        _lines.append("[cancelled] " + ", ".join(_CANCELLED_SUBSCRIPTION_IDS))
                    decision_block = "\n".join(_lines)
                    # The turn enters its own records where it ends: the situation
                    # he was given, what he asked and was told, then what he decided.
                    # Entered here and not at the session's close, so that they stand
                    # before whatever the world delivers in the meantime.
                    try:
                        record_turn(
                            character_name,
                            path=history_path,
                            situation=user_mem,
                            answers=_SESSION_ANSWERS.pop(character_name, []),
                            decision=decision_block,
                        )
                    except Exception as e:
                        if logs_enabled():
                            print(f"[trace] character {character_name}: turn records failed: {e}")
                    # Every turn this session produced is kept, so the session
                    # closes by writing all of them — not only the last one.
                    _prior_turns = list(
                        (_SESSION_CONTEXTS.get(character_name, {}) or {})
                        .get("memory_data", {})
                        .get("turns", [])
                    )
                    _prior_turns.append({
                        "world_time": world_time,
                        "scene_location": scene_location,
                        "outcome": {},
                    })
                    _SESSION_CONTEXTS[character_name] = dict(
                        _SESSION_CONTEXTS.get(character_name) or {},
                        memory_data={
                            "memory_path": memory_path,
                            "turns": _prior_turns,
                        },
                        messages=list(state["messages"]),
                        declared_intents=list(_DECLARED_INTENTS),
                        cancelled_subscriptions=list(_CANCELLED_SUBSCRIPTION_IDS),
                    )
                    _SESSION_HISTORY_PATHS[character_name] = history_path
                    _SESSION_LIMITS = limits
                return json.dumps(exit_payload, ensure_ascii=False, indent=2)

        except Exception as e:
            if logs_enabled():
                print(f"[trace] character tool execution failed ({character_name}): {last_tool_name}: {e}; retrying")
            if last_tool_name == "commit_decision":
                err_text = tool_failure(last_tool_name, e)
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
            elif last_tool_name == "declare_intent":
                err_text = tool_failure(last_tool_name, e)
                retry_msg = HumanMessage(content="\n".join([
                    INTERNAL_RETRY_MARKER,
                    f"declare_intent failed: {err_text}",
                ]))
                state["messages"].append(retry_msg)
            else:
                state["messages"].append(_retry_instruction(why="tool call failed to execute", err=tool_failure(last_tool_name, e)))
            continue

    # Agent ended turn without declaring intent. Its text is still its own state:
    # the turn's thoughts are merged into the session record so the session-end
    # flush writes them, and the conversation is kept for the next invocation.
    if persist_history:
        _skip_ctx = dict(_SESSION_CONTEXTS.get(character_name) or {})
        _skip_mem = dict(_skip_ctx.get("memory_data") or {})
        _skip_turns = list(_skip_mem.get("turns") or [])
        _skip_turns.append({
            "world_time": world_time,
            "scene_location": scene_location,
            "outcome": {},
        })
        _skip_mem["memory_path"] = memory_path
        _skip_mem["turns"] = _skip_turns
        _skip_ctx["memory_data"] = _skip_mem
        _skip_ctx["messages"] = list(state["messages"])
        _skip_ctx.setdefault("declared_intents", list(_DECLARED_INTENTS))
        _skip_ctx.setdefault("cancelled_subscriptions", list(_CANCELLED_SUBSCRIPTION_IDS))
        _SESSION_CONTEXTS[character_name] = _skip_ctx
        _SESSION_HISTORY_PATHS[character_name] = history_path
        _SESSION_LIMITS = limits
        # A turn that decided nothing still happened: the situation he was given
        # and what he asked are his, and they enter his memory where the turn ends.
        try:
            record_turn(
                character_name,
                path=history_path,
                situation=context_blob,
                answers=_SESSION_ANSWERS.pop(character_name, []),
                decision="",
            )
        except Exception as e:
            if logs_enabled():
                print(f"[trace] character {character_name}: turn records failed: {e}")

    # Agent ended turn without declaring intent — return skip
    return json.dumps({
        "character": character_name,
        "decision": {"action": "skip", "reason": "no intent declared"},
        "declared_intents": list(_DECLARED_INTENTS),
        "cancel_subscription_ids": list(_CANCELLED_SUBSCRIPTION_IDS),
    }, ensure_ascii=False, indent=2)
