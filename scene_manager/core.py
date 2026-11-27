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

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from history_pipeline import memory_at
from record_voice import spoken
from openrouter_langchain_logging import _append_jsonl, logs_dir, logs_enabled
from openrouter_llm import bind_agent_tools, build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from world import World, WorldDuration, WorldTime, build_game_master_context_block
from engine.change_summary import ChangeCollector, append_change_record
from engine.history_meta import HistoryMeta
from engine.incidents import phase_guard
from engine.tool_errors import tool_failure
from scene_manager.awareness import knowledge_label

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
            # OpenAI-raw shape some providers leave in additional_kwargs:
            # {"id": ..., "function": {"name": ..., "arguments": "..."}}
            if not str(tc.get("name") or "").strip():
                fn = tc.get("function")
                if isinstance(fn, dict) and str(fn.get("name") or "").strip():
                    fn_args = fn.get("arguments")
                    norm_args: Any = {}
                    if isinstance(fn_args, dict):
                        norm_args = fn_args
                    elif isinstance(fn_args, str) and fn_args.strip():
                        try:
                            norm_args = json.loads(fn_args)
                        except Exception:
                            norm_args = {"_raw": fn_args}
                    norm: Dict[str, Any] = {
                        "name": str(fn.get("name")).strip(),
                        "args": norm_args if isinstance(norm_args, dict) else {"_raw": norm_args},
                        "type": "tool_call",
                    }
                    tc_id = tc.get("id")
                    if tc_id:
                        norm["id"] = tc_id
                    return norm

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


def _log_qa_raw(
    *,
    player: str,
    attempt: int,
    out: Any,
    raw_count: int,
    tcs: List[Dict[str, Any]],
) -> None:
    """Best-effort dump of the raw model output for a QA invocation.

    Written only on anomalies — extraction found no tool calls, extraction
    diverged from the raw message, or the message carries invalid tool
    calls — so the file stays small. Never raises.
    """
    try:
        invalid = getattr(out, "invalid_tool_calls", None) or []
        if tcs and int(raw_count) == len(tcs) and not invalid:
            return

        content = str(getattr(out, "content", "") or "")
        additional = getattr(out, "additional_kwargs", {}) or {}

        def _names(items: Any) -> List[str]:
            names: List[str] = []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        names.append(str(it.get("name") or ""))
                    else:
                        names.append(str(getattr(it, "name", "") or ""))
            return names

        ak_filtered: Dict[str, Any] = {}
        if isinstance(additional, dict):
            for key in ("tool_calls", "function_call"):
                if key in additional:
                    ak_filtered[key] = additional.get(key)

        def _clip(value: Any, limit: int = 600) -> str:
            text = value if isinstance(value, str) else repr(value)
            return text[:limit]

        _append_jsonl(
            logs_dir() / "sm_raw_outputs.jsonl",
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "scope": "scene_manager",
                "label": "resolve_player_question",
                "player": str(player or ""),
                "attempt": int(attempt),
                "out_class": type(out).__name__,
                "raw_getattr_count": int(raw_count),
                "extract_count": len(tcs),
                "raw_names": _names(getattr(out, "tool_calls", None) or []),
                "extract_names": [
                    str(tc.get("name") or "") for tc in tcs if isinstance(tc, dict)
                ],
                "invalid_tool_calls": _clip(invalid),
                "additional_kwargs": _clip(ak_filtered),
                "content_len": len(content),
                "content_sha1": hashlib.sha1(
                    content.encode("utf-8", errors="replace")
                ).hexdigest()[:12],
                "content_head": content[:300],
            },
        )
    except Exception:
        pass


def _thoughts_blocks_enabled() -> bool:
    """Whether the model's reasoning is also entered as its own message."""
    import os

    return (os.getenv("LLM_WORLD_THOUGHTS_BLOCKS", "0") or "0").strip() == "1"


def _append_thoughts(messages: List, out: Any) -> None:
    """Record the model's own reasoning as its own entry, when switched on.

    Off by default: the reasoning already travels inside the response message, and a
    separate entry would duplicate it. Kept switchable for inspecting what a manager
    deliberates about.
    """
    if not _thoughts_blocks_enabled():
        return
    from openrouter_llm import extract_reasoning

    reasoning = extract_reasoning(out)
    if reasoning:
        messages.append(HumanMessage(content=f"[thoughts]\n{reasoning}"))


def _traffic_message(out: Any, tool_calls: List[Dict[str, Any]]) -> AIMessage:
    """An assistant message that carries its own reasoning, as the backend requires."""
    from openrouter_llm import extract_reasoning

    msg = AIMessage(content="", tool_calls=tool_calls)
    reasoning = extract_reasoning(out)
    if reasoning:
        msg.additional_kwargs = {"reasoning_content": reasoning}
    return msg


def _gm_answer_text(result: Any) -> str:
    """The Game Master's answer as text, not as the dump of the call that carried it."""
    if not isinstance(result, dict):
        return str(result or "").strip() or "No answer."
    for key in ("exit_args", "thinking", "answer", "content"):
        value = result.get(key)
        if isinstance(value, dict):
            for inner in ("answer", "content", "text"):
                text = str(value.get(inner) or "").strip()
                if text:
                    return text
            continue
        text = str(value or "").strip()
        if text:
            return text
    return "No answer."


def _normalised_calls(tool_calls: List[Dict[str, Any]], *, fallback: str) -> List[Dict[str, Any]]:
    """The calls of one response, each with an id its tool result can answer.

    A response is one message however many calls it carries, so the calls are
    read once here and answered once each: an assistant message whose calls are
    only partly answered is not a transcript the backend accepts.
    """
    out: List[Dict[str, Any]] = []
    for i, tc in enumerate(tool_calls or []):
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or "").strip()
        if not name:
            continue
        args = tc.get("args")
        out.append({
            "id": str(tc.get("id") or f"{name}_{fallback}_{i}"),
            "name": name,
            "args": args if isinstance(args, dict) else {},
        })
    return out


def _append_tool_rejection(messages: List, out: Any, error_text: str) -> None:
    """Record a rejected response without leaving an assistant turn behind.

    A response that carried calls is re-entered as those calls answered with the
    error. A response that carried no calls leaves only the error itself — the
    prose the model produced is never committed.
    """
    tcs = getattr(out, "tool_calls", None) or []
    if not isinstance(tcs, list):
        tcs = []
    if not tcs:
        ak = getattr(out, "additional_kwargs", {}) or {}
        tcs2 = ak.get("tool_calls") if isinstance(ak, dict) else None
        if isinstance(tcs2, list):
            tcs = tcs2
    if not tcs:
        messages.append(HumanMessage(content=error_text))
        return

    norm: List[Dict[str, Any]] = []
    for i, tc in enumerate(tcs):
        if isinstance(tc, dict):
            tc_id = str(tc.get("id") or "").strip()
            tc_name = str(tc.get("name") or "tool").strip()
            raw_args = tc.get("args")
        else:
            tc_id = str(getattr(tc, "id", "") or "").strip()
            tc_name = str(getattr(tc, "name", "") or "tool").strip()
            raw_args = getattr(tc, "args", None)
        if not tc_id:
            tc_id = f"synthetic_tc_{i}"
        norm.append({
            "id": tc_id,
            "name": tc_name,
            "args": raw_args if isinstance(raw_args, dict) else {},
        })

    messages.append(_traffic_message(out, norm))
    for tc in norm:
        messages.append(ToolMessage(content=error_text, tool_call_id=tc["id"], name=tc["name"]))


#: Why the cycle's default advance is what it is, in the world's own terms. The
#: kinds are the ones the scheduler reads a pool by: where an attempt ends, where
#: one begins, where one ends and another begins at the same moment, the pause a
#: scene that changes nowhere is given, and a scene with nothing under way, over
#: which there is nothing to move time.
_TIME_ADVANCE_REASONS = {
    "end": "an attempt reaches its end",
    "start": "an attempt begins",
    "both": "an attempt reaches its end where another begins",
    "pause": "nothing comes due before then, so time is let forward",
    "still": "nothing is under way",
}


def _format_intent_interval(di: Dict[str, Any], now_seconds: Optional[int] = None) -> str:
    """Render an intent's timing for display.

    Shows relative times when now_seconds is provided, else falls back
    to raw t_start/t_end.
    """
    t_start = di.get("t_start")
    t_end = di.get("t_end")

    if t_start is None:
        return str(di.get("interval", "")).strip()

    if now_seconds is not None:
        relative = int(t_start) - int(now_seconds)
        if relative <= 0:
            elapsed = -relative
            if t_end is not None and int(t_end) <= int(now_seconds):
                return "completed"
            if t_end is not None:
                remaining = int(t_end) - int(now_seconds)
                return f"in progress (+{elapsed}s elapsed, {remaining}s remaining)"
            return f"in progress (+{elapsed}s elapsed, persistent)"
        else:
            if t_end is not None:
                duration = int(t_end) - int(t_start)
                return f"in {relative}s, for {duration}s"
            return f"in {relative}s, persistent"

    # Fallback: raw display
    if t_end is None:
        return f"+{int(t_start)}s (persistent)"
    return f"+{int(t_start)}s..+{int(t_end)}s"


#: How an attempt is marked, by whether the review has seen it before.
_STATUS_MARKS = {"new": "new", "active": "already seen", "completed": "ended"}


def _format_intent_lines(
    di: Dict[str, Any],
    *,
    statuses: Dict[Any, str],
    marked: Any,
    knowledge_ids: Dict[str, str],
    now_seconds: Optional[int] = None,
) -> List[str]:
    """One attempt as the agent reads it: what it is, what it says, when, under which ids.

    The id stands beside it, because the id is what the review acts by; the
    description stands whole, because a description cut mid-sentence is a
    decision made blind; the attempt time stops at on its own is marked where
    that is what it is; and the id it is known as knowledge stands under it,
    because an attempt whose id is not stated is one this agent cannot tell
    anyone about or ask after.
    """
    actor = di.get("actor", "?")
    atype = di.get("action_type", "?")
    target = di.get("target", "?")
    desc = str(di.get("description", "")).strip()
    key = (actor, atype, str(di.get("description", ""))[:80])
    status = statuses.get(key, "new")
    vis = di.get("visibility", "public")
    absorp = di.get("absorption", "alert")
    mark = _STATUS_MARKS.get(status, "new")
    ident = str(di.get("id") or "?")
    block = [f"Attempt {ident} ({mark}) — {actor}: {atype}, aimed at {target} "
             f"({vis}, {absorp})"]
    if desc:
        block.append(f"  {desc}")
    timing = _format_intent_interval(di, now_seconds=now_seconds)
    if timing:
        block.append(f"  timing: {timing}")
    if ident in {str(x) for x in (marked or [])}:
        # Where time stops on its own, said on the attempt that brings it there.
        block[-1] += " * current default for time advance"
    knowledge_id = str(knowledge_ids.get(ident) or "").strip()
    if knowledge_id:
        block.append(f"  {knowledge_label(knowledge_id)}")
    return block


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


def _format_position(pos: dict) -> str:
    """Format a position dict to a short string like 'tavern @ (+3,0,0)'."""
    if not pos or not isinstance(pos, dict):
        return "?"
    inside = str(pos.get("inside") or "").strip()
    near = str(pos.get("near") or "").strip()
    coords = pos.get("coords")
    offset = pos.get("offset")
    if inside:
        if offset and isinstance(offset, (list, tuple)) and len(offset) >= 2:
            return f"{inside} @ ({offset[0]},{offset[1]},{offset[2] if len(offset) > 2 else 0})"
        return inside
    if near:
        return f"near {near}"
    if coords and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return f"({coords[0]},{coords[1]})"
    return str(pos.get("id") or "?")


# Tool names that change entity state — results are logged to persistent history
ENTITY_CHANGING_TOOLS = frozenset({
    "adjust_entity", "create_event", "set_effect", "remove_effect",
    "declare_intent", "modify_intent",
})


def _answers_with_a_decision(args: Dict[str, Any]) -> bool:
    """Does this call manage an attempt, and take its owner's decision as the answer?

    Only where the hand that can call him back is there: in a review, managing an
    attempt is answered inside the same invocation. Elsewhere it is a request the
    orchestrator applies when the phase ends, and the call answers itself.
    """
    from scene_manager.sm_tools import has_callback_resolver

    action = str((args or {}).get("action") or "").strip().lower()
    return action in ("remove", "interrupt") and has_callback_resolver()


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
        # Where this scene manager reports what a session changed: the memory of
        # the Game Master, the authority above it. Set by the orchestrator.
        self._peer_history_path: Optional[Path] = None
        # What the session in front of it changed, collected from the tools that
        # ran. Rebuilt when a session begins.
        self._changes = ChangeCollector()
        # Where a Game Master invoked from here reports what it changed. Set by
        # the orchestrator: without it those invocations would leave no trace of
        # their effect anywhere but in the Game Master's own memory.
        self._gm_peer_history: Optional[Path] = None
        self._session_context_buffer: List[Dict[str, Any]] = []
        # Thresholds: session buffer content before forcing publish
        self._session_buffer_publish_chars: int = 3000    # ~600-900 tokens
        self._session_buffer_force_chars: int = 60000     # ~15-20K tokens session safety cap
        # ── 4-layer context architecture ──
        # Layer 1a: never changes — created once here
        self._immutable_prefix: List[SystemMessage] = [
            SystemMessage(content=self._prompt_text),
        ]
        # Layer 2: read through the pipeline (thread + the session's open branch)
        self._hot_buffer: List[BaseMessage] = []
        # Records the conversation itself carries, because this session's own tool
        # calls produced them. Reading those back from the branch would put the
        # same intent, and the same perception, in front of the scene manager
        # twice in one context.
        self._carried: set = set()
        # The attempts this session's reviews have read, by id: an attempt is
        # entered where it is read, and a cycle that only carries it on does not
        # restate it.
        self._attempts_read: set = set()
        # The cycle's default time advance as it was last stated here, as
        # (seconds, kind). The estimate is read off the pool, and the pool moves
        # while the cycle runs; stating it again only when what stands under it
        # has changed is what keeps a repeated statement from reading as news.
        self._stated_advance: Optional[Tuple[int, str]] = None
        # Entity tracking: IDs referenced via tools during current session
        self._tracked_entity_ids: set = set()
        # The session's narrative (set by run_session_summary, read by end_session)
        self._session_narrative: str = ""
        # The cycles this session has run: the first and the last, in the world's
        # own numbering. A session is a stretch of them, and the closing account
        # is asked for as the events that stretch covers.
        self._cycle_first: int = 0
        self._cycle_last: int = 0
        # Shared LLM instance (created once for prefix caching)
        self._llm: Any = None
        # Second client, for the one phase that forces a tool call
        self._llm_forced: Any = None
        self._world = World()
        self._meta = HistoryMeta(
            (self._history_path.parent / "scene_manager_meta.json")
            if self._history_path else Path("scene_manager_meta.json"),
        )

    def begin_session(self) -> None:
        """Start a new session: what stands where, entered into its own branch."""
        from scene_manager.sm_tools import reset_sm_intents
        reset_sm_intents()
        # The ledger belongs to one session: what the previous session produced
        # is not what this one knows, and a piece of knowledge from it can no
        # longer be given to anyone.
        from scene_manager.awareness import reset_ledger
        reset_ledger()
        # A session's change outline covers that session, and its conversation
        # carries nothing of the last one.
        self._changes = ChangeCollector()
        self._hot_buffer.clear()
        self._carried = set()
        self._tracked_entity_ids.clear()
        self._stated_advance = None
        self._attempts_read = set()
        self._cycle_first = 0
        self._cycle_last = 0

        # The session works on a branch of the SM's memory: what it writes while
        # the session runs accumulates there and is merged when the session ends.
        if self._history_path:
            memory_at(self._history_path).open_branch()

        # The count of sessions is the framework's own bookkeeping and stays in its
        # metadata; the record the session opens with states the situation.
        self._meta.increment_session()

        # The session's opening states the scene: the places the players stand in,
        # and what stands in each of them. Said here and nowhere else — once per
        # session, in the record every invocation of that session reads — because
        # the alternative is the same index in front of the agent as many times as
        # the session has phases.
        #
        # What is still running is not restated here. It is state, and state is
        # read where it is decided on: the review of declared intents states every
        # one of them, with its id and how far it has got, at each cycle. What the
        # previous session took and left standing is written down as part of that
        # session's own account, when it closes.
        roster = self._scene_roster()
        user_msg = "[session_begin]"
        if roster:
            user_msg += "\n" + roster

        # Save to persistent history
        self._record_history(user_msg)

    def end_session(self) -> None:
        """End the session: enter its account, then merge the branch.

        Nothing here writes the file — the session's records are entered as the
        session lives them, and the one merge at its close is what puts them
        on disk.
        """
        if self._history_path:
            narrative = str(self._session_narrative).strip()
            self._session_narrative = ""

            # Time range
            try:
                wt = self._world.get_world_time()
                time_str = wt.to_string() if hasattr(wt, 'to_string') else str(wt)
            except Exception:
                time_str = "?"

            # Nothing is gathered here. Everything the session did was entered
            # where it stood — the attempts the review read, what each came to,
            # what was asked and answered — and this close enters only what the
            # close itself is: the end of the session, with its own account of it.
            # The stretch of cycles it covered is stated with it: the account that
            # follows is an account of those cycles, and a reader that does not
            # know which ones cannot match it to anything it can go and look at.
            parts = [f"[session_end]\ntime: {time_str}"]
            first = int(getattr(self, "_cycle_first", 0) or 0)
            last = int(getattr(self, "_cycle_last", 0) or 0)
            if first and last:
                parts[0] += (f"\ncycles: {first}–{last}" if last > first
                             else f"\ncycles: {first}")
            if narrative:
                parts.append(narrative)
            self._record_history("\n\n".join(parts))

            # What the session accumulated has been written down; a new session
            # starts with an empty buffer, so what it measures is the session in
            # front of it rather than every session so far.
            self._session_context_buffer.clear()

        # The session's branch closes here: what the session wrote that is durable
        # is merged into the thread, and the rest is dropped. Covering happens
        # where the trim does — here — while those records are still in the SM's
        # own context.
        if self._history_path:
            hist = memory_at(self._history_path)
            memory = self._memory_as_read()
            outcome = hist.close_branch()
            print(f"  scene manager: memory closed "
                  f"({outcome.get('merged', 0)} record(s) entered)")
            self._cover_retired(
                dropped=outcome.get("trimmed_out") or [], memory=memory,
            )

        # What the session changed in the world, in outline, goes to the authority
        # above: the Game Master reads it after the session, the same way this
        # agent reads what the World Manager changed. A session that changed
        # nothing sends nothing.
        append_change_record(self._peer_history_path, self._changes.build("sm"))

        self._hot_buffer.clear()
        self._tracked_entity_ids.clear()
        self._stated_advance = None

    def _record_history(self, content: str) -> None:
        """Enter one record into SM history.

        The record goes through the pipeline, which owns the branch: while the
        session runs, what is durable accumulates in memory and is merged into
        the file by the one merge at the session's close. Nothing is written to
        disk before that merge — the file an interrupted session leaves behind is
        the file the previous session closed, not half of this one.

        Covering a trim happens where the trim does: at the merge. What the merge
        dropped is reported by the pipeline, and those records are still in the
        SM's own context at that point, so the paragraph about them is written
        from here rather than lost (see gm/game_master.py for the same step).

        This is the session's memory of an act, not what a character can be
        told of it: a piece of knowledge is entered where the act is entered
        (``_log_entity_change``), so that one act is one piece of knowledge
        under one id, whether the session reads it here or hands it on.
        """
        if not self._history_path:
            return
        text = str(content or "").strip()
        if not text:
            return
        memory_at(self._history_path).record(text)

    def _memory_as_read(self) -> List[Dict[str, str]]:
        """The thread as the SM reads it.

        What is already merged, then what the open branch has added — the order
        the file will have once the branch merges. Read through the pipeline,
        because the file on disk is only half of that while a session runs: read
        from disk directly and the SM loses sight of everything it wrote this
        session, which is what "the session wrote nothing durable until the
        close" turns into if the reading side is not treated the same way.
        """
        if not self._history_path:
            return []
        return memory_at(self._history_path).records()

    def _kind_in_play(self, entity_id: str, store_type: str, players: set) -> str:
        """What a thing is to this session: one of the world's players, or its own kind.

        The type vocabulary belongs to the world — `set_entity` reserves no type and
        matches the string exactly — so what the world calls a thing is passed
        through as written and never read for meaning. The one thing added here is
        the fact the framework owns and the world does not: whether this id is one
        of the campaign's players (`game/players/`), which is what decides whether
        the scene manager is asked for its intent or narrates for it.
        """
        if entity_id in players:
            return "player"
        return store_type or "?"

    def _scene_roster(self) -> str:
        """Where the active players stand, and what stands there with them.

        An index, not a snapshot: entity ids are matched exactly, so a reader who
        has no list of them can only name what it already knows and guess at the
        rest — each guess a call, each failure a record left in its history. What
        is here is the id and whether it is one of the players; everything else
        about it is read by naming that id.

        A session is not one place. The players may share one room tonight and
        stand in three towns by morning, so what an invocation is given is every
        place a player is in, each with what is in it. Listing the world instead
        buries them; listing only the busiest place hides the rest of the session
        from the one agent that can see it all.
        """
        places = self._occupied_places()
        if not places:
            return ""
        try:
            players = {str(p.get("name") or "").strip()
                       for p in self._world.get_players() if isinstance(p, dict)}
        except Exception:
            players = set()
        present = self._present_map([place for place, _names in places], players=players)
        blocks: List[str] = []
        for place, names in places:
            here = present.get(place) or []
            block = [place]
            if here:
                for entity_id, kind in here[:25]:
                    block.append(f"  {entity_id} [{kind}]")
                if len(here) > 25:
                    block.append(f"  ... {len(here) - 25} more present")
            else:
                # The world says they are here and the store holds nothing in the
                # place. Said plainly rather than dropped: a place a character
                # stands in belongs to the session whether or not the world has
                # furnished it yet.
                for name in names:
                    block.append(f"  {name} [player]")
            blocks.append("\n".join(block))
        return ("[Locations and entities near the active players]\n"
                + "\n\n".join(blocks))

    def _occupied_places(self) -> List[Tuple[str, List[str]]]:
        """The places characters stand in, the busiest first, then by name."""
        try:
            players = [p for p in self._world.get_players() if isinstance(p, dict)]
        except Exception:
            return []
        groups: Dict[str, List[str]] = {}
        for player in players:
            name = str(player.get("name") or "").strip()
            place = str(player.get("location") or "").strip()
            if name and place:
                groups.setdefault(place, []).append(name)
        return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))

    def _present_map(
        self, places: Sequence[str], *, players: Optional[set] = None,
    ) -> Dict[str, List[Tuple[str, str]]]:
        """What stands in each of these places, from one reading of the store.

        Membership is a fact the position carries — an entity is in a place when
        its position names it, or names something that is in it (what is perched
        on a shoulder is in the room the shoulder is in). Nothing else is
        membership: an entity without a place, or with a place elsewhere, is not
        in this scene, however close the world happens to put it.
        """
        wanted = {str(place).strip() for place in places if str(place).strip()}
        if not wanted:
            return {}
        players = set(players or ())
        try:
            from world_manager.entity_store import query_page
            page = query_page(include_templates=False, limit=500)
        except Exception:
            return {}
        items = page.get("entities") if isinstance(page, dict) else page
        found: Dict[str, Dict[str, Tuple[str, str]]] = {}
        beside: List[Tuple[str, str, Tuple[str, str]]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("id") or "").strip()
            if not entity_id:
                continue
            position = item.get("position") if isinstance(item.get("position"), dict) else {}
            entry = (
                entity_id,
                self._kind_in_play(entity_id, str(item.get("type") or ""), players),
            )
            inside = str(position.get("inside") or "").strip()
            if inside in wanted:
                found.setdefault(inside, {})[entity_id] = entry
                continue
            near = str(position.get("near") or "").strip()
            if near:
                beside.append((near, entity_id, entry))
        # A thing beside something in a place is in that place; a chain of such
        # settles in a pass or two, and a cycle settles by adding nothing.
        for _pass in range(4):
            added = False
            for near, entity_id, entry in beside:
                for held in found.values():
                    if near in held and entity_id not in held:
                        held[entity_id] = entry
                        added = True
            if not added:
                break
        return {place: list(held.values()) for place, held in found.items()}



    def _awareness_changes(self) -> str:
        """What changed in the session's awareness, for the start of an invocation.

        Asked once, at the beginning of a phase. Asked after each record instead,
        every character would be stated again for every record — a new record
        changes every character's combination at once, which is the one thing
        this is arranged not to do.
        """
        from scene_manager.awareness import ledger
        try:
            players = [str(p.get("name") or "") for p in self._world.get_players()
                       if isinstance(p, dict)]
        except Exception:
            players = []
        return ledger().changes([n for n in players if n])

    def _turn_header(self) -> str:
        """What a phase is given when it begins: what changed in the session's
        awareness since the phase before it.

        Once per phase, not once per record: asked after each record instead, every
        character would be stated again for every record — a new record changes
        every character's combination at once, which is the one thing this is
        arranged not to do.

        Where the players stand, and what stands with them, is not here: that is
        stated once, in ``[session_begin]``, which every invocation of the session
        reads in its history. Restating it in each phase's own message would put
        the same index in front of the agent as many times as the session has
        phases.
        """
        return self._awareness_changes()

    def _cover_retired(self, *, dropped, memory) -> None:
        """Turn what a merge dropped into a paragraph, while it is still in context."""
        if not self._history_path:
            return
        try:
            from engine.history_retirement import pending_retirement, retire
            from world.agent_story import append_paragraph

            if not dropped and not pending_retirement(self._history_path):
                return

            root = self._history_path.parent

            def _write(name, summary):
                append_paragraph(
                    summaries_path=root / "sm_summaries.json",
                    story_path=root / "sm_story.json",
                    name=name or "What came before",
                    summary=summary,
                )

            print("  scene manager: covering what left the memory — one summary call...")
            if retire(
                self._history_path,
                dropped=dropped,
                prefix=self._pinned_summaries(),
                history_messages=memory,
                prompt_text=self._prompt_text,
                temperature=self._temperature,
                scope="scene_manager",
                write=_write,
                kind="paragraph",
                label="sm_history_retirement",
            ):
                self._meta.append_entry("auto_injection", "[sm_history_retirement]")
                print("  scene manager: retired records covered")
                if logs_enabled():
                    print("[trace] SM covered retired history")
        except Exception as e:
            print(f"  scene manager: covering retired records failed: {e}")

    def _get_llm(self):
        """Lazy-create the shared LLM (created once for prefix caching)."""
        if self._llm is None:
            from openrouter_llm import build_openrouter_chat_llm
            self._llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-scene-manager",
                max_tokens=4000,
                parallel_tool_calls=True,
                thinking=True,  # adjudication is deliberation
                effort="low",  # a request: the backend accepts the word
            )
        return self._llm

    def _get_llm_forced(self):
        """A second client for the one phase that forces a tool call.

        Letting the backend keep the model's reasoning apart and forcing a call are
        mutually exclusive, so that phase cannot share the main client. The phase
        answers with a single boolean, so its budget stays small.
        """
        if self._llm_forced is None:
            from openrouter_llm import build_openrouter_chat_llm
            self._llm_forced = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-scene-manager-forced",
                max_tokens=2000,
                parallel_tool_calls=True,
                thinking=False,
            )
        return self._llm_forced

    def _assemble_context(self) -> List[BaseMessage]:
        """Build full LLM context: immutable prefix + pinned summaries + persistent history + hot buffer."""
        # Layer 1b: dynamic pinned summaries (rebuilt only on trim via PinnedBlockCache)
        pinned = self._pinned_summaries()
        # Layer 2: convert persisted dicts to BaseMessage. What this session
        # entered is not repeated here: the conversation below already carries it
        # — the call that produced it and the result that came back — and reading
        # it back as a record would put the same intent and the same perception in
        # front of the scene manager twice in one context. Only what stands in the
        # file from earlier sessions is read here, and it reads as it was written:
        # an id belongs to the moment a piece of knowledge was entered, and that
        # moment is a tool call, not a line of history.
        from scene_manager.awareness import ledger

        from history_kinds import payload as _payload

        carried = getattr(self, "_carried", set())
        history: List[BaseMessage] = []
        for m in self._memory_as_read():
            role = str(m.get("role") or "").strip()
            content = str(m.get("content") or "")
            if _payload(content) in carried:
                continue
            # Stored entries are records the framework authored, so they are read
            # back as records whatever role they were written with — and in the
            # world's voice: the markers the file keeps are the code's, and an
            # agent reading its own memory does not need to see them.
            if role in ("user", "assistant"):
                said = spoken(content)
                if said.strip():
                    history.append(HumanMessage(content=said))
        return self._immutable_prefix + pinned + history + self._hot_buffer

    # ── Entity tracking ───────────────────────────────────────────────

    def _track_entity_refs(self, tool_name: str, args: dict) -> None:
        """Extract entity IDs from tool call args and add to tracked set.
        
        Called after every tool invocation (read-only or changing).
        Tracks actor, target, entity_id, name fields.
        """
        if not isinstance(args, dict):
            return
        for key in ("entity_id", "name", "actor", "target", "character_name", "character_names"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                self._tracked_entity_ids.add(val.strip())
            elif isinstance(val, (list, tuple)):
                for v in val:
                    if isinstance(v, str) and v.strip():
                        self._tracked_entity_ids.add(v.strip())
            elif isinstance(val, dict):
                eid = val.get("entity_id") or val.get("id") or val.get("name") or ""
                if isinstance(eid, str) and eid.strip():
                    self._tracked_entity_ids.add(eid.strip())

    def _log_entity_change(self, tool_name: str, args: dict, result_str: str) -> str:
        """Log an entity-changing tool call; return the record id it took.

        Called only for tools in ENTITY_CHANGING_TOOLS after successful execution.
        The id comes back so the caller can put it in the tool's own result: a
        declaration or an event that nobody can name afterwards cannot be stated
        as perceived by anyone, and the number is what such a statement takes.
        """
        if not self._history_path:
            return ""
        if not isinstance(args, dict):
            return ""
        # Build a short summary of what changed
        desc_parts: List[str] = [f"[entity] {tool_name}"]
        for key in ("entity_id", "name", "actor", "target", "character_name"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                desc_parts.append(f"  {key}: {val.strip()}")
        # Add key action-specific fields
        if tool_name == "adjust_entity":
            for k in ("hp", "fp", "er", "alter_condition", "alter_injury",
                      "alter_item", "alter_ability", "alter_skill",
                      "position", "current_state"):
                v = args.get(k)
                if v is not None and v != "" and v != {}:
                    desc_parts.append(f"  {k}: {str(v)}")
        elif tool_name == "create_event":
            desc = str(args.get("description") or "").strip()
            if desc:
                desc_parts.append(f"  description: {desc}")
            # What the occurrence involved and where it was placed, when the call
            # named them: a record of an event that says neither leaves a reader
            # with something that happened to nobody, nowhere.
            involved = args.get("entity_ids")
            if isinstance(involved, (list, tuple)):
                names = [str(v).strip() for v in involved if str(v).strip()]
                if names:
                    desc_parts.append("  entities: " + ", ".join(names))
            elif isinstance(involved, str) and involved.strip():
                desc_parts.append(f"  entities: {involved.strip()}")
            place = args.get("position")
            if isinstance(place, dict) and place:
                desc_parts.append(
                    "  position: " + json.dumps(place, ensure_ascii=False)
                )
        elif tool_name == "declare_intent":
            desc = str(args.get("description") or "").strip()
            if desc:
                desc_parts.append(f"  description: {desc}")
        msg = "\n".join(desc_parts)
        if not msg.strip():
            return ""
        # The record is entered on the session's branch like every other one:
        # the file is written once, when the session closes. The change itself
        # is already in the world — the tool wrote it there — so nothing about
        # the world waits for this record.
        self._record_history(msg)
        # The conversation itself carries the record of the call, so it is not
        # read back from the branch as well within this session.
        self._carried_by_the_conversation(msg)
        # What a character can be told is not the call but what the call made. A
        # declaration makes an attempt, and the attempt is what the session
        # resolves, what its perceivers are marked on, and what the phase in
        # front of the resolution lists — so the id handed back here names the
        # attempt, and it is the same id the attempt is entered under when it
        # stands in the pool. Any other call is the scene manager's own
        # bookkeeping: it stands in its memory and carries no address, because a
        # record of a call is not something a character can be given.
        if tool_name != "declare_intent":
            return ""
        from scene_manager.awareness import attempt_piece, ledger
        text = attempt_piece(args.get("actor"), args.get("action_type"),
                             args.get("target"), args.get("description")) or msg
        book = ledger()
        return book.id_of(text) or book.register(kind="action", content=text)

    def _record_what_it_told(self, tool_name: str, args: dict, result: str) -> None:
        """Enter what this session told a character, in its own memory.

        The act stands in the ledger — who came to know what, under which id — and
        in the conversation, as the call and the result it got. What is kept here
        is the session manager's own account of it: a session that ends has to be
        able to read what it told whom, or the next session reads a scene where
        nobody was ever told anything.

        The record takes no id of its own. The knowledge it names already carries
        one, and the same words under two ids would let two readers reach them as
        two different things.
        """
        if not self._history_path or tool_name != "add_perception":
            return
        if str(result or "").strip().upper().startswith("ERROR"):
            return
        name = str(args.get("player") or "").strip()
        perception = str(args.get("perception") or "").strip()
        if not name or not perception:
            # Knowledge named by its ids alone is not written down here: the ids
            # are the session's own names, and a session that has closed mints
            # them again for other words. What a character was given stands in
            # his own memory, as the account of the words themselves.
            return
        text = f"[perception → {name}]\n{perception}"
        # The act is in front of the agent as the call that produced it, so it is
        # not read back from the branch as well within this session.
        self._record_history(text)
        self._carried_by_the_conversation(text)

    def _carried_by_the_conversation(self, text: str) -> None:
        """Remember that this session's own conversation already carries a record.

        A record written because a tool call produced it is in front of the agent
        as that call and its result; reading it back from the branch as well would
        state the same thing twice in one context.
        """
        carried = getattr(self, "_carried", None)
        if carried is None:
            carried = self._carried = set()
        carried.add(str(text))

    def _get_sm_tools(self):
        """Complete unified tool set for ALL SM phases.

        All tools are always available. The agent chooses which to use based
        on the system prompt and context. Has tools for entity reads/writes,
        scene management, GM escalation, and session summary.
        """
        from scene_manager.tools import (
            get_entity, get_entity_facts, get_entity_state, get_location_info,
            query_entities, adjust_entity,
            create_event, query_events, close_invocation,
            set_effect, remove_effect,
            escalate_to_gm,
            manage_intent, add_perception,
            answer_player_question,
        )
        from scene_manager.sm_tools import declare_intent, modify_intent, write_session_narrative
        return [
            escalate_to_gm,
            get_entity, get_entity_facts, get_entity_state, get_location_info,
            query_entities, adjust_entity,
            create_event, query_events, close_invocation,
            set_effect, remove_effect,
            manage_intent, add_perception,
            declare_intent, modify_intent,
            answer_player_question, write_session_narrative,
        ]

    @phase_guard("perception_check")
    def perception_check(self, matched_intent: Any, observer: str) -> bool:
        """Р’В§3.3 / Р’В§8 perception gate РІР‚вЂќ narrow structured query, NOT open narrative judgment.

        Called by the segment scheduler (``scheduler/timeline.py``) only for structural
        matches on a ``visibility == "private"`` intent. ``matched_intent`` is a
        ``scheduler.timeline.Intent``. Returns True if ``observer`` perceives the
        concealed action.
        """
        actor = str(getattr(matched_intent, "actor", "") or "").strip()
        description = str(getattr(matched_intent, "description", "") or "").strip()

        @tool
        def perception_result(detected: bool) -> str:
            """Report whether the observer notices the concealed action.

            Args:
                detected: True if the observer would plausibly notice this action
                    given the scene.
            """
            return "ok"

        human_msg = (
            "Perception check РІР‚вЂќ structural query, answer with the tool only.\n"
            f"Observer: {observer}\n"
            f"Concealed actor: {actor}\n"
            f"Concealed action: {description[:200]}\n"
        )
        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="perception_check")
        bound = bind_agent_tools(
            self._get_llm_forced(), [perception_result],
            callbacks=callbacks, tool_choice="required",
        )

        messages = self._assemble_context() + [HumanMessage(content=human_msg)]
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
        """Ask the Game Master, and keep the asking.

        The one way this agent reaches the authority, whatever phase is running: the
        question is stated to it as a question from the scene manager, and what it
        answers comes back as what it said.
        """
        from gm.game_master import GameMaster
        # A fresh instance reads its history from disk, and reports what it
        # changes into the same memory any other Game Master invocation reports
        # into: an invocation is an invocation, whoever asked for it.
        gm = GameMaster(
            history_path=self._history_path.parent / "game_master_messages.json"
            if self._history_path else None,
        )
        if self._gm_peer_history is not None:
            gm.set_peer_history(self._gm_peer_history)
        return gm.call_gm(
            notice=notice,
            invoked_as="you receive new scene manager question",
            context=context,
        )

    def ask_the_gm(self, question: str, context: str = "") -> str:
        """Ask the Game Master, and keep the asking.

        The one way this agent reaches the authority, whatever phase of a session
        is running: the question and the scene state it has to fit go to the Game
        Master, and what it answers comes back as what it said. Question and
        answer are then entered as the session's record of the exchange — the
        asking is this agent's own act, no character knows of it, and the record
        is entered where the asking was made.

        Args:
            question: The fact needed.
            context: The scene state the answer has to fit, if any.
        """
        asked = str(question or "").strip()
        scene_state = str(context or "").strip()
        if scene_state:
            asked = f"{asked}\n{scene_state}"
        gm_answer = _gm_answer_text(self.call_gm(notice=asked))
        self._note_exchange("exchange", f"question: {asked}\nanswer: {gm_answer}")
        return gm_answer

    def set_gm_peer_history(self, path: Optional[Path]) -> None:
        """Where a Game Master invoked from here reports what it changed."""
        self._gm_peer_history = Path(path) if path else None

    def set_peer_history(self, path: Optional[Path]) -> None:
        """Where this scene manager reports what a session changed."""
        self._peer_history_path = Path(path) if path else None

    @staticmethod
    def _form_player_request(player_name: str, questions: str) -> str:
        is_observe = "[observe" in questions
        is_recall = "[recall" in questions

        if not (is_observe or is_recall):
            raise RuntimeError("unexpected question format")

        request_kind = " and ".join(
            [k for k, present in (("observe", is_observe), ("recall", is_recall)) if present]
        )
        # Pure data: who asks, and what is asked. The delivery contract belongs to
        # the tool alone — nothing here restates it.
        return (
            f"Player \"{player_name}\" sends the following {request_kind} request:\n"
            f"{questions}"
        )


    @phase_guard("resolve_player_question")
    def resolve_player_question(
        self,
        *,
        player_name: str,
        questions: str,
    ) -> str:
        """Answer a player's questions, one answer per question.

        Every question carries an id, and what comes back is one answer for each
        of them: a question nobody answered stays unanswered rather than being
        filled with the text written for another question.
        """
        from openrouter_llm import openrouter_logging_callbacks

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="resolve_player_question")

        bound = bind_agent_tools(self._get_llm(), self._get_sm_tools(), callbacks=callbacks)

        task_msg = self._form_player_request(player_name, questions)
        header = self._turn_header()
        if header:
            task_msg += "\n" + header

        # Append to hot buffer (Layer 3)
        self._hot_buffer.append(HumanMessage(content=task_msg))

        asked: List[str] = []
        question_text: Dict[str, str] = {}
        for line in questions.splitlines():
            m = re.match(r'\[(observe|recall):([^\]]+)\]\s*(.*)$', line.strip())
            if m:
                asked.append(m.group(2))
                question_text[m.group(2)] = m.group(3).strip()

        answered: Dict[str, str] = {}

        def _record() -> None:
            # The id is the address of a question inside one batch and is spent
            # with it; what is written down names the question by its own words.
            for pid in asked:
                self._note_exchange(
                    "qa",
                    f"{player_name}: {question_text.get(pid, '')}\n"
                    f"Answer: {answered.get(pid, '(no answer)')[:500]}",
                )

        for _attempt in range(10):
            pending = [pid for pid in asked if pid not in answered]
            if not pending:
                _record()
                return json.dumps(answered, ensure_ascii=False)

            try:
                out = bound.invoke(self._assemble_context())
            except Exception:
                if _attempt == 9:
                    raise
                continue

            _append_thoughts(self._hot_buffer, out)
            raw_tcs = getattr(out, "tool_calls", None) or []
            tcs = _extract_tool_calls(out)
            _log_qa_raw(
                player=player_name,
                attempt=_attempt,
                out=out,
                raw_count=len(raw_tcs),
                tcs=tcs,
            )
            if not tcs:
                # A rejected response leaves nothing behind: the text is not
                # committed, and the notice states only that no call was made.
                text = str(getattr(out, "content", "") or "").strip()
                if text and logs_enabled():
                    print(f"[trace] SM resolve_player_question: free text rejected "
                          f"({len(text)} chars, attempt {_attempt})")
                self._hot_buffer.append(HumanMessage(
                    content="[error] No tool call was made — nothing reached the player. "
                            "Still unanswered: " + ", ".join(pending) + "."
                ))
                continue

            # Process all tool calls of this response (parallel_tool_calls=True
            # support). The response is echoed once, whole: the calls share the
            # reasoning they were thought with, and a message per call would carry
            # that reasoning into the context once for every call.
            calls = _normalised_calls(tcs, fallback=f"qa{_attempt}")
            self._hot_buffer.append(_traffic_message(out, calls))

            for call in calls:
                name = str(call["name"])
                args = dict(call["args"])
                tc_id = str(call["id"])

                if name == "answer_player_question":
                    question_id = str(args.get("question_id") or "").strip()
                    answer_text = str(args.get("answer") or "").strip()
                    note = ""
                    if question_id not in pending:
                        note = ("ERROR: question_id must be one of the questions still "
                                "waiting for an answer: " + ", ".join(pending) + ".")
                    elif not answer_text:
                        note = "ERROR: answer is empty — provide what the player perceives."
                    if note:
                        self._hot_buffer.append(ToolMessage(
                            content=note, tool_call_id=tc_id, name=name,
                        ))
                        continue
                    answered[question_id] = answer_text
                    self._hot_buffer.append(ToolMessage(
                        content=f"[answer recorded: {question_id}]",
                        tool_call_id=tc_id, name=name,
                    ))
                    continue

                if name == "escalate_to_gm" and not str(args.get("question") or "").strip():
                    # A call that named no question is about the ones on the table:
                    # the authority is asked what the characters asked.
                    args["question"] = questions

                _qa_tool_map = {t.name: t for t in self._get_sm_tools()}
                fn = _qa_tool_map.get(name)
                if fn:
                    try:
                        tool_result = fn.invoke(args)
                        result_str = json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else str(tool_result)
                        # Track entity references and log changes
                        self._track_entity_refs(name, args)
                        self._changes.record(name, args, tool_result)
                        if name in ENTITY_CHANGING_TOOLS:
                            record_id = self._log_entity_change(name, args, result_str)
                            if record_id:
                                result_str = f"{result_str}\n{knowledge_label(record_id)}"
                        self._record_what_it_told(name, args, result_str)
                    except Exception as e:
                        result_str = tool_failure(name, e)
                    self._hot_buffer.append(ToolMessage(content=result_str, tool_call_id=tc_id, name=name))
                    continue

            still = [pid for pid in asked if pid not in answered]
            if still:
                self._hot_buffer.append(HumanMessage(
                    content="[waiting for an answer] " + ", ".join(still)
                ))

        unanswered = [pid for pid in asked if pid not in answered]
        _record()
        self._note_exchange(
            "qa",
            f"{player_name}: unanswered after 10 attempts: {', '.join(unanswered)}",
        )
        raise RuntimeError(
            f"resolve_player_question: unanswered for player '{player_name}': "
            f"{', '.join(unanswered)}"
        )

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
            npcs_data = self._world.non_player_entities()
            if isinstance(npcs_data, dict):
                for nname, ndata in npcs_data.items():
                    if not isinstance(ndata, dict):
                        continue
                    position = ndata.get("position") or {}
                    inside = position.get("inside", "") if isinstance(position, dict) else ""
                    if inside == location:
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
                    info = self._world.get_info()
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

    def resolve_active_groups(self) -> List[Dict[str, Any]]:
        """Return all location groups with characters and NPCs.

        Each group: {"location": str, "character_names": [str], "npc_names": [str]}
        Returns empty list if no groups found.
        """
        try:
            chars = self._world.get_players()
            if not chars:
                return []

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
                return []

            groups = []
            for loc, char_names in location_groups.items():
                all_at_loc = self.get_entities_at_location(loc)
                all_chars = list(dict.fromkeys(char_names + all_at_loc["players"]))
                groups.append({
                    "location": loc,
                    "character_names": all_chars,
                    "npc_names": all_at_loc["npcs"],
                })
            return groups
        except Exception as e:
            if logs_enabled():
                print(f"[trace] resolve_active_groups error: {e}")
            return []

    def _build_resolve_message(self, payload: Dict[str, Any]) -> str:
        """Build the human message for the resolve cycle.

        What the players' planning ended in, and what this cycle will do to the
        clock if the review leaves it alone: the moment it stands at,
        how far time moves by default, and what that advance is estimated by. The
        estimate is read from the pool as it stands here, and the attempt
        standing at that moment is marked where it is listed — the review is what
        decides the advance, so what it is offered is what its own pool amounts
        to rather than a promise about what the pool will become.

        Each attempt is also listed under the id it is known by as knowledge: the
        id it is handed to a character by, and the id that stands at its
        resolution. An attempt whose id is not stated here is one the scene
        manager cannot tell anyone about or ask after.
        """
        declared = payload.get("declared_intents") or []
        statuses = payload.get("intent_statuses") or {}
        now_sec = payload.get("cycle_start_seconds")
        player_names = payload.get("player_names") or []
        advance = payload.get("time_advance") or {}
        marked = {str(ident) for ident in (advance.get("by") or [])}
        knowledge_ids = payload.get("knowledge_ids") or {}

        lines: List[str] = []
        cycle = int(getattr(self, "_cycle_no", 0) or 0)
        lines.append(f"Cycle {cycle} — resolve stage." if cycle else "Resolve stage.")
        lines.append("Player planning cycle ended.")

        if declared:
            lines.append("Declared intents:")
            for di in declared:
                lines.extend(_format_intent_lines(
                    di, statuses=statuses, marked=marked,
                    knowledge_ids=knowledge_ids, now_seconds=now_sec,
                ))

        time_str = str(payload.get("cycle_start_time") or "").strip()
        if time_str:
            lines.append(f"Current moment of time: {time_str}")
        if advance:
            lines.extend(self._time_advance_lines(advance))

        header = self._turn_header()
        if header:
            lines.append(header)
        return "\n".join(lines).strip()

    @phase_guard("run_resolve_cycle")
    def run_resolve_cycle(
        self,
        *,
        payload: Dict[str, Any],
    ) -> None:
        """SM resolve cycle — review intents, declare NPC intents, modify/correct.

        Called every cycle after characters, before SegmentScheduler.
        Always ends with close_invocation. Rejects free text.
        """
        n_intents = len(payload.get("declared_intents", []))
        n_players = len(payload.get("player_names", []))
        print(f"[SM resolve_cycle] start — {n_intents} intents, {n_players} players")

        # What this review reads is entered where it is read: the attempts
        # standing before it, in the order the pool holds them, each with how far
        # it has got. What comes of them is entered where it comes to pass.
        self.note_attempts_read(
            payload.get("declared_intents") or [],
            now_seconds=payload.get("cycle_start_seconds"),
        )

        from scene_manager.sm_tools import reset_sm_intents
        reset_sm_intents()

        # The cycle this review belongs to, counted through the world: it is what
        # the phase is called in the memory, and what its stretch is known by
        # afterwards, without a label to mark it.
        self._cycle_no = self._meta.increment_cycle()
        if not self._cycle_first:
            self._cycle_first = self._cycle_no
        self._cycle_last = self._cycle_no

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="resolve_cycle")
        human_msg = self._build_resolve_message(payload)
        self._hot_buffer.append(HumanMessage(content=human_msg))

        bound_llm = bind_agent_tools(self._get_llm(), self._get_sm_tools(), callbacks=callbacks)

        # The phase ends when the SM closes the invocation. Writing one awareness
        # block per character is as legitimate as writing them in one call, so the
        # work itself is not budgeted: only calls that yield nothing usable are
        # counted, and they bound a stuck model rather than the work.
        stall_limit = 10
        stalls = 0
        call_no = 0
        while True:
            call_no += 1
            try:
                out = bound_llm.invoke(self._assemble_context())
            except Exception as exc:
                print(f"  [SM resolve_cycle] LLM error, retrying: {exc}")
                stalls += 1
                if stalls >= stall_limit:
                    raise ValueError(
                        f"SM resolve_cycle: {stall_limit} consecutive calls failed"
                    )
                continue

            _append_thoughts(self._hot_buffer, out)
            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                stalls += 1
                print(f"  [SM resolve_cycle] no tool call, retrying ({stalls}/{stall_limit})")
                _append_tool_rejection(
                    self._hot_buffer, out,
                    "ERROR: Use declare_intent, modify_intent, manage_intent, "
                    "escalate_to_gm, or close_invocation."
                )
                if stalls >= stall_limit:
                    raise ValueError(
                        f"SM resolve_cycle: {stall_limit} consecutive calls carried no tool call"
                    )
                continue
            stalls = 0

            # One response, one message: every call it carried rides in it with the
            # reasoning it came with (see _normalised_calls).
            calls = _normalised_calls(tool_calls, fallback=f"resolve_{call_no}")
            response_at = len(self._hot_buffer)
            self._hot_buffer.append(_traffic_message(out, calls))

            def _execute(name: str, args: Dict[str, Any], tc_id: str) -> None:
                """Run one call of this response and answer it."""
                tool_map = {t.name: t for t in self._get_sm_tools()}
                fn = tool_map.get(name)
                if fn:
                    try:
                        result = fn.invoke(args)
                        result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
                        self._track_entity_refs(name, args)
                        self._changes.record(name, args, result)
                        if name in ENTITY_CHANGING_TOOLS:
                            record_id = self._log_entity_change(name, args, result_str)
                            if record_id:
                                result_str = f"{result_str}\n{knowledge_label(record_id)}"
                        self._record_what_it_told(name, args, result_str)
                    except Exception as e:
                        result_str = tool_failure(name, e)
                    self._hot_buffer.append(ToolMessage(content=result_str, tool_call_id=tc_id, name=name))
                else:
                    _append_tool_rejection(
                        self._hot_buffer, out,
                        f"ERROR: Unknown tool {name!r}. "
                        "Use declare_intent, modify_intent, manage_intent, "
                        "escalate_to_gm, or close_invocation."
                    )

            deferred: List[Tuple[str, Dict[str, Any], str]] = []
            for call in calls:
                name = str(call["name"])
                args = dict(call["args"])
                tc_id = str(call["id"])

                if name == "close_invocation":
                    # Defer close until ALL sibling tool calls in this batch are
                    # executed. With parallel_tool_calls=True, DeepSeek may emit
                    # close_invocation FIRST followed by declare_intent calls —
                    # returning here would silently drop them.
                    continue

                if name == "manage_intent" and _answers_with_a_decision(args):
                    # Cutting an attempt calls its owner back, and what he decides
                    # is this call's answer. Answered after the calls it was
                    # batched with, so nothing of another call stands between the
                    # response and its results.
                    deferred.append((name, args, tc_id))
                    continue

                # Execute any other tool
                _execute(name, args, tc_id)

            for name, args, tc_id in deferred:
                # The answer is a whole phase of its own: a character called back
                # in the middle of a review may ask what he perceives, and that
                # question is answered by this same agent. So the response being
                # answered is set aside while the call runs — a conversation whose
                # last turn carries a call nothing has answered yet cannot be
                # sent, and a question asked on top of it would be refused — and
                # it is put back with the answer under it when the call returns.
                saved = list(self._hot_buffer[response_at:])
                del self._hot_buffer[response_at:]
                try:
                    _execute(name, args, tc_id)
                finally:
                    tail = list(self._hot_buffer[response_at:])
                    # The answer is what the call wrote last. Whatever was said
                    # while it ran — a question this agent answered — stands in
                    # the record of the exchange, not between a call and its
                    # answer: the response is answered by what it asked for.
                    answer = tail[-1:] if tail and isinstance(tail[-1], ToolMessage) else []
                    self._hot_buffer[response_at:] = saved + answer

            # After executing all sibling tools, answer the closing call itself. The
            # answer has to come last — the siblings it was batched with are part of
            # the same response — but it has to come: a transcript whose last
            # assistant turn carries a call nothing answers is not one the backend
            # accepts, and every request made on it afterwards is refused.
            close_calls = [c for c in calls
                           if str(c.get("name") or "").strip() == "close_invocation"]
            if close_calls:
                for call in close_calls:
                    self._hot_buffer.append(ToolMessage(
                        content='{"status": "invocation_closed"}',
                        tool_call_id=str(call.get("id") or ""),
                        name="close_invocation",
                    ))
                print(f"  [SM resolve_cycle] done — close_invocation")
                return

    def note_attempts_read(
        self, intents: List[Dict[str, Any]], now_seconds: Optional[int] = None,
    ) -> None:
        """Enter what the review of a cycle reads, where it reads it.

        The attempts standing before the review are what the cycle is decided
        from, and the scene manager reads them here — entered in the order the
        pool holds them, each under the id it is acted by and with how far it has
        got. An attempt already read stands as it was read: a cycle that only
        carries it on does not restate it, and what comes of it is entered where it
        comes to pass.

        Only for actors that are characters: what the scene manager declared for
        an NPC is already recorded as the declaration it made.
        """
        try:
            players = {str(p.get("name") or "") for p in self._world.get_players()
                       if isinstance(p, dict)}
        except Exception:
            players = set()
        read = getattr(self, "_attempts_read", None)
        if read is None:
            read = self._attempts_read = set()
        for intent in intents or []:
            if not isinstance(intent, dict):
                continue
            ident = str(intent.get("id") or "").strip()
            actor = str(intent.get("actor") or "").strip()
            desc = str(intent.get("description") or "").strip()
            if not ident or not actor or not desc or actor not in players:
                continue
            if ident in read:
                continue
            read.add(ident)
            line = (f"[accepted] [{ident}] {actor}: {intent.get('action_type') or '?'} → "
                    f"{intent.get('target') or '?'}\n  {desc}")
            timing = _format_intent_interval(intent, now_seconds=now_seconds)
            if timing:
                line += f"\n  timing: {timing}"
            self._record_history(line)

    def note_resolved(self, entries: List[Any]) -> None:
        """Enter what a round brought to pass, where it resolved.

        The attempt's own record stands where the review read it, and it is not
        said again here: what a round adds is that the attempt reached its end and
        who was there to notice that. So the record names the attempt by the id it
        stood under and carries those who could see it, and no text of the attempt
        stands a second time in the memory — an attempt written out again where it
        resolved would read as if it happened then.

        Entered here, where the round reached that moment, and not gathered at the
        session's close: a group written at the close would land after everything
        that happened later.

        Only for actors that are characters: what an NPC did is recorded as what
        was declared for it.
        """
        try:
            players = {str(p.get("name") or "") for p in self._world.get_players()
                       if isinstance(p, dict)}
        except Exception:
            return
        for entry in entries or []:
            if str(getattr(entry, "kind", "action") or "action") != "action":
                continue
            actor = str(getattr(entry, "actor", "") or "").strip()
            # The attempt is named by the id it stood under, and an action entry
            # carries one: the scheduler sets it where the attempt ends.
            ident = str(getattr(entry, "intent_id", "") or "").strip()
            if not actor or not ident or actor not in players:
                continue
            line = f"[accepted] [{ident}] — resolved"
            seen = [str(n) for n in (getattr(entry, "perceivers", None) or [])
                    if str(n).strip()]
            if seen:
                line += "\n  perceived by: " + ", ".join(seen)
            self._record_history(line)

    #: How a record of an exchange opens, by the kind of exchange it was.
    _EXCHANGE_MARKERS = {
        "qa": "[exchange:character → scene_manager]",
        "exchange": "[exchange:scene_manager → game_master]",
    }

    def _note_exchange(self, kind: str, content: str) -> None:
        """Enter an exchange: what was asked, and what came back.

        Both directions: a character's question with the answer it got, and the
        question this agent put to the Game Master with the answer that came back.
        Neither survives the session anywhere else, and they are the only record of
        what was actually asked.

        Entered where the exchange is made, not gathered at the session's close.
        The file is still written once, at the close — the record goes onto the
        session's branch — but a record entered at the close lands after every
        record of what happened later in the session, and a memory whose order is
        not the order the session lived tells its reader something untrue about
        when things were said.
        """
        text = str(content or "").strip()
        if not text:
            return
        self._session_context_buffer.append({"type": kind, "content": text})
        marker = self._EXCHANGE_MARKERS.get(str(kind or ""))
        if not marker or not self._history_path:
            return
        record = f"{marker}\n  " + text
        self._record_history(record)
        # The conversation carries the same question and the same answer, as the
        # call and the result it got; read back from the branch as well, the same
        # exchange would stand in front of this agent twice in one context.
        self._carried_by_the_conversation(record)

    def _build_session_summary_message(self, payload: Dict[str, Any]) -> str:
        """Build the human message that asks for the session's narrative.

        Asked for by the stretch of cycles it covers, not by the word "session":
        a cycle is a thing the agent has counted through, seen in the phase it was
        called in, and can go and look at; a session is the framework's own unit
        and names nothing in the world.
        """
        first = int(getattr(self, "_cycle_first", 0) or 0)
        last = int(getattr(self, "_cycle_last", 0) or 0)
        if first and last and last > first:
            return (f"Cycles {first}–{last} have passed. Write immersive narration "
                    "in novelic tone covering new story part.")
        if first and last:
            return (f"Cycle {first} has passed. Write immersive narration "
                    "in novelic tone covering new story part.")
        return "Write immersive narration in novelic tone covering new story part."

    @phase_guard("run_session_summary")
    def run_session_summary(
        self,
        *,
        payload: Dict[str, Any],
        context_text: str,
    ) -> Dict[str, Any]:
        """Run the session's close: one narrative of the session's events.

        Returns ``{"narrative": str}`` on success, ``{}`` on failure.
        """
        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="session_summary")
        human_msg = self._build_session_summary_message(payload)
        ctx_note = _context_for_narration(context_text)
        if ctx_note.strip():
            human_msg += "\n\n---\n" + ctx_note.strip()

        self._hot_buffer.append(HumanMessage(content=human_msg))

        bound_llm = bind_agent_tools(self._get_llm(), self._get_sm_tools(), callbacks=callbacks)

        from scene_manager.sm_tools import (
            clear_session_narrative,
            get_session_narrative,
            write_session_narrative as _wsn,
        )

        # The account belongs to one session: a new close starts from nothing,
        # never from what the previous session left behind.
        clear_session_narrative()

        attempts = 0
        while attempts < 6:
            attempts += 1
            try:
                out = bound_llm.invoke(self._assemble_context())
            except Exception as exc:
                print(f"  [SM session_narrative] LLM error, retrying: {exc}")
                continue

            _append_thoughts(self._hot_buffer, out)
            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                print(f"  [SM session_narrative] no tool call, retrying (attempt {attempts})")
                _append_tool_rejection(
                    self._hot_buffer, out,
                    "ERROR: You MUST call write_session_narrative."
                )
                continue

            tc0 = tool_calls[0]
            name = str(tc0.get("name") or "").strip()
            args = tc0.get("args") if isinstance(tc0, dict) else {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}

            if name != "write_session_narrative":
                print(f"  [SM session_narrative] unexpected tool {name!r}, retrying")
                _append_tool_rejection(
                    self._hot_buffer, out,
                    "ERROR: Use write_session_narrative."
                )
                continue

            result = _wsn.invoke({"narrative": args.get("narrative")})
            if str(result).startswith("ERROR"):
                # An empty narrative is the one fault worth naming: the session is
                # not recorded without it, so it is asked for again.
                print(f"  [SM session_narrative] rejected: {result}")
                _append_tool_rejection(self._hot_buffer, out, f"ERROR: {result}")
                continue

            narrative = get_session_narrative()
            if not narrative:
                _append_tool_rejection(
                    self._hot_buffer, out,
                    "ERROR: The narrative was not recorded. Write it again."
                )
                continue

            print(f"  [SM session_narrative] recorded — {len(narrative)} characters "
                  f"in {attempts} attempt(s)")
            self._session_narrative = narrative
            return {"type": "session_narrative", "narrative": narrative}

        print(f"  [SM session_narrative] FAILED — nothing recorded in {attempts} attempt(s)")
        return {}

    # ── Session buffer methods (used by run_full_session) ──

    def session_buffer_chars(self) -> int:
        """Return estimated character count of accumulated session context."""
        return sum(len(str(e.get("content", ""))) for e in self._session_context_buffer)

    def is_session_buffer_ready(self) -> bool:
        """Check if session context buffer has crossed the publication threshold."""
        return self.session_buffer_chars() >= self._session_buffer_publish_chars

    def session_buffer_threshold(self) -> int:
        """How much of the session's buffer is enough to close the session."""
        return self._session_buffer_publish_chars

    def is_session_buffer_force(self) -> bool:
        """Check if session safety cap is reached (force publish regardless)."""
        return self.session_buffer_chars() >= self._session_buffer_force_chars

    @phase_guard("run_bootstrap")
    def run_bootstrap(self) -> None:
        """Run SM bootstrap — enter what the characters in the scene perceive.

        Invoked once per world, before the normal cycle. What stands in the scene
        is stated in the session's own opening (``[session_begin]``), and what the
        phase is for is stated by its tools. What the phase message states is the
        one thing the opening does not: that the session has begun and nobody in
        the scene has been given anything of it yet — the state the close is
        judged on, and the difference between an opening and a cycle.
        """
        from openrouter_llm import openrouter_logging_callbacks

        callbacks = openrouter_logging_callbacks(scope="scene_manager", label="run_bootstrap")

        bound = bind_agent_tools(self._get_llm(), self._get_sm_tools(), callbacks=callbacks)

        # Where the scene stands is the session's opening, one record back, and
        # the scope of the phase is where it is checked. What is new here is the
        # session's own beginning and the fact that the scene holds nothing yet:
        # a label alone would leave the agent to work out what it was called for.
        human_msg = (
            "The scene opens for the first time. No character in the scene has "
            "been given anything of it yet."
        )
        header = self._turn_header()
        if header:
            human_msg += "\n" + header

        self._hot_buffer.append(HumanMessage(content=human_msg))

        # Same rule as the resolve cycle: the work is not budgeted, only calls
        # that yield nothing usable are. A close refused by the gate that guards
        # this phase is such a call: the opening is not complete.
        stall_limit = 10
        refusal_limit = 3
        stalls = 0
        refusals = 0
        call_no = 0
        close_after = False
        while True:
            call_no += 1
            try:
                out = bound.invoke(self._assemble_context())
            except Exception as exc:
                if logs_enabled():
                    print(f"[trace] SM run_bootstrap: LLM error, retrying: {exc}")
                stalls += 1
                if stalls >= stall_limit:
                    raise RuntimeError(
                        f"SM run_bootstrap: {stall_limit} consecutive calls failed"
                    )
                continue

            _append_thoughts(self._hot_buffer, out)
            tool_calls = _extract_tool_calls(out)
            if not tool_calls:
                stalls += 1
                _append_tool_rejection(
                    self._hot_buffer, out,
                    "ERROR: Use close_invocation when done, or read what you need "
                    "about the place and enter what each character perceives of it."
                )
                if stalls >= stall_limit:
                    raise RuntimeError(
                        f"SM run_bootstrap: {stall_limit} consecutive calls carried no tool call"
                    )
                continue
            stalls = 0

            # One response, one message: every call it carried rides in it with the
            # reasoning it came with (see _normalised_calls).
            calls = _normalised_calls(tool_calls, fallback=f"bootstrap_{call_no}")
            self._hot_buffer.append(_traffic_message(out, calls))

            # Execute all tool calls of this response
            tool_map = {t.name: t for t in self._get_sm_tools()}
            for call in calls:
                name = str(call["name"])
                args = dict(call["args"])
                tc_id = str(call["id"])

                if name == "close_invocation":
                    # Answered once the calls it was batched with are done, and
                    # answered by the tool rather than here: whether the opening is
                    # complete is the tool's to say.
                    continue

                fn = tool_map.get(name)
                if fn:
                    try:
                        result = fn.invoke(args)
                        result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
                        self._track_entity_refs(name, args)
                        self._changes.record(name, args, result)
                        if name in ENTITY_CHANGING_TOOLS:
                            record_id = self._log_entity_change(name, args, result_str)
                            if record_id:
                                result_str = f"{result_str}\n{knowledge_label(record_id)}"
                        self._record_what_it_told(name, args, result_str)
                    except Exception as e:
                        result_str = tool_failure(name, e)
                else:
                    result_str = f"Unknown tool: {name}"

                self._hot_buffer.append(ToolMessage(content=result_str, tool_call_id=tc_id, name=name))

            close_calls = [c for c in calls
                           if str(c.get("name") or "").strip() == "close_invocation"]
            if close_calls:
                closer = tool_map.get("close_invocation")
                if closer is None:
                    # Named rather than reported as a refusal three times over: a
                    # surface without the closing tool would be blamed on the
                    # characters, who are not the ones missing anything.
                    raise RuntimeError(
                        "SM run_bootstrap: the tool set holds no close_invocation"
                    )
                for call in close_calls:
                    try:
                        # The closing call names nothing: whatever a stale habit
                        # puts in its arguments, ending the invocation is all it
                        # does.
                        result = closer.invoke({})
                    except Exception as e:
                        result = {"status": "error", "error": tool_failure("close_invocation", e)}
                    if isinstance(result, dict) and result.get("status") == "error":
                        refusals += 1
                        self._hot_buffer.append(ToolMessage(
                            content=f"ERROR: {result.get('error') or 'close refused'}",
                            tool_call_id=str(call.get("id") or ""),
                            name="close_invocation",
                        ))
                        continue
                    self._hot_buffer.append(ToolMessage(
                        content=json.dumps(result, ensure_ascii=False)
                                if isinstance(result, dict) else str(result),
                        tool_call_id=str(call.get("id") or ""),
                        name="close_invocation",
                    ))
                    close_after = True
                if not close_after and refusals >= refusal_limit:
                    raise RuntimeError(
                        f"SM run_bootstrap: {refusal_limit} closes refused — the "
                        "characters were left knowing nothing of the scene they "
                        "stand in"
                    )

            if close_after:
                return

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
        """Run paragraph summary from SM's own context. Every 10 sessions.

        Writes to sm_story.json + sm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_paragraph

            from engine.summarizer import SummaryRunner

            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="scene_manager",
            )
            last_ref = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[sm_paragraph_summary_task]\n"
                "Summarize the story since \"{last_ref}\".\n"
                "Below are the events since then (auto-injected context excluded):\n\n"
                "{refs}\n\n"
                "Write a paragraph with a short title (3-10 words) and 5-10 sentence summary.\n"
                "Respond with JSON: {\"name\": \"short title\", \"summary\": \"your summary\"}.",
                last_ref=last_ref,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-scene-manager-paragraph",
                label="sm_paragraph_summary",
                extra_pinned=self._pinned_summaries(),
            )
            if not result:
                return
            pname, summary = result
            pname = pname or "Session Summary"
            root = self._history_path.parent
            append_paragraph(
                summaries_path=root / "sm_summaries.json",
                story_path=root / "sm_story.json",
                name=pname,
                summary=summary,
            )
            self._meta.append_entry("auto_injection", f"[sm_paragraph:{pname}]")
            self._meta.mark_summarized(pname)
            self._meta.increment_paragraph()
            if logs_enabled():
                print(f"[trace] SM paragraph summary: '{pname}' ({len(summary)} chars)")
        except Exception as e:
            import logging
            logging.exception(f"[scheduler] SM paragraph_summary failed: {e}")

    def _run_arc_summary(self) -> None:
        """Run arc summary from SM's own context. Every 10 paragraphs.

        Writes to sm_story.json + sm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_arc

            from engine.summarizer import SummaryRunner

            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="scene_manager",
            )
            last_ref = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[sm_arc_summary_task]\n"
                "Since arc summary after \"{last_ref}\", "
                "10 new paragraphs have been summarized.\n"
                "Write an arc-level summary covering all these paragraphs.\n"
                "Respond with JSON: {\"name\": \"arc title\", \"summary\": \"your summary\"}.",
                last_ref=last_ref,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-scene-manager-arc",
                label="sm_arc_summary",
                extra_pinned=self._pinned_summaries(),
            )
            if not result:
                return
            arc_name, arc_summary = result
            arc_name = arc_name or "Arc Summary"
            root = self._history_path.parent
            append_arc(
                summaries_path=root / "sm_summaries.json",
                story_path=root / "sm_story.json",
                name=arc_name,
                summary=arc_summary,
            )
            self._meta.append_entry("auto_injection", f"[sm_arc:{arc_name}]")
            self._meta.mark_summarized(arc_name)
            if logs_enabled():
                print(f"[trace] SM arc summary: '{arc_name}' ({len(arc_summary)} chars)")
        except Exception as e:
            import logging
            logging.exception(f"[scheduler] SM arc_summary failed: {e}")

    def inject_delta(self, content: str) -> None:
        """Inject a standalone world-state delta message into SM history."""
        if not content or not content.strip():
            return
        if not self._history_path:
            return
        self._record_history(content.strip())
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
                   .add_all_player_descriptions()
                   .add_sm_summaries()
                   .add_active_characters()
                   .build_persistent()
            )
        except Exception:
            return []

    def _time_advance_lines(self, advance: Dict[str, Any]) -> List[str]:
        """The cycle's default advance and what it is estimated by, as stated.

        Stating it is what records it: the estimate is read off the pool, so what
        was last said of it is what a later statement of it is compared against.
        """
        from scheduler.timeline import format_duration

        seconds = int(advance.get("seconds") or 0)
        kind = str(advance.get("kind") or "")
        self._stated_advance = (seconds, kind)
        out = ["World time advance default after resolve cycle: "
               + format_duration(seconds)]
        reason = _TIME_ADVANCE_REASONS.get(kind)
        if reason:
            out.append(f"Estimated by - {reason}")
        return out

    def state_advance(self, advance: Dict[str, Any]) -> List[str]:
        """The cycle's default advance as it now stands, for a hand to pass on.

        A decision taken in the middle of a review moves where time would stop if
        nothing else changed, and the review is told of it in the answer to the
        call that took the decision — not afterwards, when nothing is in front of
        it to decide by. Stated only when it moved: an advance already standing
        under the pool is not news, and saying it again makes the answer longer
        without telling the review anything.
        """
        if not isinstance(advance, dict) or not advance:
            return []
        standing = (int(advance.get("seconds") or 0), str(advance.get("kind") or ""))
        if standing == getattr(self, "_stated_advance", None):
            return []
        return self._time_advance_lines(advance)

    def intents_changed_lines(
        self,
        player_name: str,
        intents: List[Dict[str, Any]],
        knowledge_ids: Optional[Dict[str, str]] = None,
        *,
        now_seconds: Optional[int] = None,
        marked: Any = None,
    ) -> List[str]:
        """What a character's attempts now hold, as the review reads an attempt.

        The same form the phase lists its pool in — the id it is acted by, the
        description whole, how far it has got, the id it is known as knowledge
        by — and only that character's attempts: what changed is his, and the
        rest of the pool stands where the review already read it.
        """
        name = str(player_name or "").strip()
        rows = [d for d in (intents or []) if isinstance(d, dict)]
        if not name or not rows:
            return []
        out = [f"Player intents changed. Intents list of player {name}:"]
        for di in rows:
            out.extend(_format_intent_lines(
                di, statuses={}, marked=marked or set(),
                knowledge_ids=knowledge_ids or {}, now_seconds=now_seconds,
            ))
        return out

    def note_intents_changed(
        self,
        player_name: str,
        intents: List[Dict[str, Any]],
        knowledge_ids: Optional[Dict[str, str]] = None,
        *,
        now_seconds: Optional[int] = None,
    ) -> None:
        """State that a character's attempts changed while the cycle ran.

        A character called back mid-cycle decides with no review in front of him:
        what he decided stands in the pool from that moment, and this states it —
        for that character, and only for what changed — under the id each attempt
        is acted by and the id each is known as knowledge by. It is what the pool
        holds for him, not a review of it: nothing here asks for a decision.

        Said, not recorded. The attempts stand in the timeline, which is where a
        later reader meets them.
        """
        name = str(player_name or "").strip()
        rows = [d for d in (intents or []) if isinstance(d, dict)]
        if not name or not rows:
            return
        lines = self.intents_changed_lines(
            name, rows, knowledge_ids, now_seconds=now_seconds,
        )
        self._hot_buffer.append(HumanMessage(content="\n".join(lines)))

    def note_time_advance(self, advance: Dict[str, Any]) -> None:
        """State the cycle's default advance again, once it is no longer that.

        The estimate is read off the pool in front of the cycle, and the pool
        moves while the cycle runs: a call-back puts an attempt in it, and a
        decision taken on the pool takes one out or cuts it short, so where time
        would stop if nothing else changed moves with it. Said only when what
        stands under it has changed.
        """
        if not isinstance(advance, dict) or not advance:
            return
        standing = (int(advance.get("seconds") or 0), str(advance.get("kind") or ""))
        if standing == getattr(self, "_stated_advance", None):
            return
        self._hot_buffer.append(
            HumanMessage(content="\n".join(self._time_advance_lines(advance)))
        )

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
