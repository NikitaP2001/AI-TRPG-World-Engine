from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .io import _append_jsonl, _file_lock, _read_json, _write_json
from .migrations import ensure_story_schema_v2


_ASYNC_SUMMARY_GUARD = threading.Lock()
_ASYNC_SUMMARY_RUNNING: set[str] = set()


def _async_summary_enabled() -> bool:
    raw = (os.getenv("LLM_WORLD_ASYNC_SUMMARY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _turns_fingerprint(turns: List[Dict[str, Any]]) -> str:
    try:
        payload = json.dumps(turns, ensure_ascii=False, sort_keys=True)
    except Exception:
        payload = str(turns)
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()


def _merge_unique(dst: Any, items: List[str]) -> List[str]:
    out: List[str] = []
    if isinstance(dst, list):
        out.extend([str(x) for x in dst if str(x).strip()])
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


def append_turn_to_story(
    *,
    story_json: Path,
    workspace: Path,
    narration: str,
    start_time: str,
    end_time: str,
    location: str,
    characters: List[str],
    npcs: List[str],
    summarizer: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    """Append one turn to the ongoing paragraph; summarize every 10 turns into a paragraph.
    
    Args:
        summarizer: Optional callable(world_context, ongoing_paragraph) -> {"name": str, "summary": str}.
                    If None, falls back to the separate summarizer_agent.
    """

    if _async_summary_enabled():
        _append_turn_to_story_async(
            story_json=story_json,
            workspace=workspace,
            narration=narration,
            start_time=start_time,
            end_time=end_time,
            location=location,
            characters=characters,
            npcs=npcs,
            summarizer=summarizer,
        )
        return

    try:
        arcs = _read_json(story_json)
    except Exception:
        arcs = None

    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        ensure_story_schema_v2(story_json=story_json)
        try:
            arcs = _read_json(story_json)
        except Exception:
            arcs = None
        if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
            return

    arc0: Dict[str, Any] = arcs[0]
    ongoing = arc0.get("ongoing_paragraph")
    if not isinstance(ongoing, dict):
        ongoing = {
            "start_time": "",
            "turns": [],
            "locations": [],
            "characters": [],
            "npcs": [],
        }

    if not str(ongoing.get("start_time") or "").strip():
        ongoing["start_time"] = str(start_time)

    turns = ongoing.get("turns")
    if not isinstance(turns, list):
        turns = []

    turn_entry = {
        "start_time": str(start_time),
        "end_time": str(end_time),
        "location": str(location),
        "narration": narration or "",
        "characters": list(characters or []),
        "npcs": list(npcs or []),
    }
    turns.append(turn_entry)
    ongoing["turns"] = turns

    # Participants (unique, stable order by insertion)
    def _merge_unique(dst: Any, items: List[str]) -> List[str]:
        out: List[str] = []
        if isinstance(dst, list):
            out.extend([str(x) for x in dst if str(x).strip()])
        for it in items:
            s = str(it).strip()
            if s and s not in out:
                out.append(s)
        return out

    ongoing["locations"] = _merge_unique(ongoing.get("locations"), [location])
    ongoing["characters"] = _merge_unique(ongoing.get("characters"), list(characters or []))
    ongoing["npcs"] = _merge_unique(ongoing.get("npcs"), list(npcs or []))

    # Summarize when we reached 10 turns.
    if len(turns) >= 10:
        try:
            if summarizer is not None:
                summary_out = summarizer("", ongoing)
            else:
                # Fallback to standalone summarizer agent (legacy path).
                from summarizer_agent import summarize_ongoing_paragraph
                summary_out = summarize_ongoing_paragraph(world_context="", ongoing_paragraph=ongoing)
        except Exception as e:
            summary_out = {"name": "Summary", "summary": "(failed to summarize paragraph)"}
        para_obj = {
            "name": summary_out.get("name") or "Summary",
            "start_time": str(ongoing.get("start_time") or ""),
            "end_time": str(end_time),
            "locations": ongoing.get("locations") or [],
            "characters": ongoing.get("characters") or [],
            "npcs": ongoing.get("npcs") or [],
            "summary": summary_out.get("summary") or "",
            "turn_count": len(turns),
            # Keep full turns for offline debugging / tooling (not injected to the model).
            "turns": turns,
        }

        paras = arc0.get("paragraphs")
        if not isinstance(paras, list):
            paras = []
        paras.append(para_obj)
        arc0["paragraphs"] = paras

        # Arc-level checkpoint: every 10th paragraph of the ongoing arc.
        arc_checkpoint = (len(paras) % 10 == 0)
        if arc_checkpoint:
            agg_locations: List[str] = []
            agg_characters: List[str] = []
            agg_npcs: List[str] = []
            for p in paras:
                if not isinstance(p, dict):
                    continue
                agg_locations = _merge_unique(agg_locations, list(p.get("locations") or []))
                agg_characters = _merge_unique(agg_characters, list(p.get("characters") or []))
                agg_npcs = _merge_unique(agg_npcs, list(p.get("npcs") or []))

            finalized_arc_name = str(summary_out.get("arc_name") or "").strip() or str(arc0.get("name") or "").strip() or "Arc"
            finalized_arc_summary = str(summary_out.get("arc_summary") or "").strip() or str(para_obj.get("summary") or "")

            arc0["name"] = finalized_arc_name
            arc0["summary"] = finalized_arc_summary
            arc0["paragraph_count"] = len(paras)
            if not str(arc0.get("start_time") or "").strip():
                arc0["start_time"] = str(paras[0].get("start_time") or para_obj.get("start_time") or "")
            arc0["end_time"] = str(para_obj.get("end_time") or "")
            arc0["locations"] = agg_locations
            arc0["characters"] = agg_characters
            arc0["npcs"] = agg_npcs

            # Keep finalized arc frozen (no ongoing paragraph on finalized arcs);
            # prepend a fresh ongoing arc for new paragraphs.
            arc0.pop("ongoing_paragraph", None)
            arcs[0] = arc0
            arcs.insert(
                0,
                {
                    "name": "Ongoing Arc",
                    "paragraphs": [],
                    "ongoing_paragraph": {
                        "start_time": str(end_time),
                        "turns": [],
                        "locations": [],
                        "characters": [],
                        "npcs": [],
                    },
                },
            )
        else:
            # Continue same ongoing arc until paragraph-count checkpoint.
            arc0["ongoing_paragraph"] = {
                "start_time": str(end_time),
                "turns": [],
                "locations": [],
                "characters": [],
                "npcs": [],
            }
            arcs[0] = arc0

        # Dedicated log for GM context summary updates.
        try:
            _append_jsonl(
                (workspace / "logs" / "gm_context_summary_updates.jsonl").resolve(),
                {
                    "event": "gm_context_summary_updated",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "paragraph": para_obj,
                },
            )
        except Exception:
            pass

    else:
        arc0["ongoing_paragraph"] = ongoing
        arcs[0] = arc0

    _write_json(story_json, arcs)


def _enqueue_async_summary_job(
    *,
    story_json: Path,
    workspace: Path,
    summarizer: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]],
) -> None:
    key = str(story_json.resolve())
    with _ASYNC_SUMMARY_GUARD:
        if key in _ASYNC_SUMMARY_RUNNING:
            return
        _ASYNC_SUMMARY_RUNNING.add(key)

    def _runner() -> None:
        try:
            _process_pending_summaries(
                story_json=story_json,
                workspace=workspace,
                summarizer=summarizer,
            )
        finally:
            with _ASYNC_SUMMARY_GUARD:
                _ASYNC_SUMMARY_RUNNING.discard(key)

    t = threading.Thread(target=_runner, daemon=True, name=f"summary-worker-{story_json.stem}")
    t.start()


def _append_turn_to_story_async(
    *,
    story_json: Path,
    workspace: Path,
    narration: str,
    start_time: str,
    end_time: str,
    location: str,
    characters: List[str],
    npcs: List[str],
    summarizer: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    needs_summary = False

    with _file_lock(story_json):
        try:
            arcs = _read_json(story_json)
        except Exception:
            arcs = None

        if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
            ensure_story_schema_v2(story_json=story_json)
            try:
                arcs = _read_json(story_json)
            except Exception:
                arcs = None
            if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
                return

        arc0: Dict[str, Any] = arcs[0]
        ongoing = arc0.get("ongoing_paragraph")
        if not isinstance(ongoing, dict):
            ongoing = {
                "start_time": "",
                "turns": [],
                "locations": [],
                "characters": [],
                "npcs": [],
            }

        if not str(ongoing.get("start_time") or "").strip():
            ongoing["start_time"] = str(start_time)

        turns = ongoing.get("turns")
        if not isinstance(turns, list):
            turns = []

        turn_entry = {
            "start_time": str(start_time),
            "end_time": str(end_time),
            "location": str(location),
            "narration": narration or "",
            "characters": list(characters or []),
            "npcs": list(npcs or []),
        }
        turns.append(turn_entry)
        ongoing["turns"] = turns
        ongoing["locations"] = _merge_unique(ongoing.get("locations"), [location])
        ongoing["characters"] = _merge_unique(ongoing.get("characters"), list(characters or []))
        ongoing["npcs"] = _merge_unique(ongoing.get("npcs"), list(npcs or []))

        if len(turns) >= 10:
            ongoing["_summary_pending"] = True
            needs_summary = True

        arc0["ongoing_paragraph"] = ongoing
        arcs[0] = arc0
        _write_json(story_json, arcs)

    if needs_summary:
        _enqueue_async_summary_job(
            story_json=story_json,
            workspace=workspace,
            summarizer=summarizer,
        )


def _process_pending_summaries(
    *,
    story_json: Path,
    workspace: Path,
    summarizer: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    while True:
        snapshot: Optional[Dict[str, Any]] = None

        with _file_lock(story_json):
            try:
                arcs = _read_json(story_json)
            except Exception:
                return
            if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
                return

            arc0: Dict[str, Any] = arcs[0]
            ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0.get("ongoing_paragraph"), dict) else {}
            turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
            pending = bool(ongoing.get("_summary_pending"))

            if not pending or len(turns) < 10:
                if pending:
                    ongoing.pop("_summary_pending", None)
                    arc0["ongoing_paragraph"] = ongoing
                    arcs[0] = arc0
                    _write_json(story_json, arcs)
                return

            chunk = [t for t in turns[:10] if isinstance(t, dict)]
            if len(chunk) < 10:
                return

            chunk_start = str(ongoing.get("start_time") or "") or str(chunk[0].get("start_time") or "")
            chunk_end = str(chunk[-1].get("end_time") or "")
            locs: List[str] = []
            chars: List[str] = []
            npcs: List[str] = []
            for t in chunk:
                locs = _merge_unique(locs, [str(t.get("location") or "")])
                chars = _merge_unique(chars, [str(x) for x in (t.get("characters") or []) if str(x).strip()])
                npcs = _merge_unique(npcs, [str(x) for x in (t.get("npcs") or []) if str(x).strip()])

            snapshot = {
                "fingerprint": _turns_fingerprint(chunk),
                "chunk": chunk,
                "ongoing": {
                    "start_time": chunk_start,
                    "turns": chunk,
                    "locations": locs,
                    "characters": chars,
                    "npcs": npcs,
                },
                "chunk_end_time": chunk_end,
            }

        if not snapshot:
            return

        try:
            if summarizer is not None:
                summary_out = summarizer("", snapshot["ongoing"])
            else:
                from summarizer_agent import summarize_ongoing_paragraph

                summary_out = summarize_ongoing_paragraph(
                    world_context="",
                    ongoing_paragraph=snapshot["ongoing"],
                )
        except Exception:
            summary_out = {"name": "Summary", "summary": "(failed to summarize paragraph)"}

        with _file_lock(story_json):
            try:
                arcs2 = _read_json(story_json)
            except Exception:
                return
            if not isinstance(arcs2, list) or not arcs2 or not isinstance(arcs2[0], dict):
                return

            arc0 = arcs2[0]
            ongoing2 = arc0.get("ongoing_paragraph") if isinstance(arc0.get("ongoing_paragraph"), dict) else {}
            turns2 = ongoing2.get("turns") if isinstance(ongoing2.get("turns"), list) else []
            if len(turns2) < 10:
                ongoing2.pop("_summary_pending", None)
                arc0["ongoing_paragraph"] = ongoing2
                arcs2[0] = arc0
                _write_json(story_json, arcs2)
                continue

            now_fp = _turns_fingerprint([t for t in turns2[:10] if isinstance(t, dict)])
            if now_fp != str(snapshot.get("fingerprint") or ""):
                # State changed while summary was generated; recompute on latest state.
                continue

            para_obj = {
                "name": summary_out.get("name") or "Summary",
                "start_time": str(snapshot["ongoing"].get("start_time") or ""),
                "end_time": str(snapshot.get("chunk_end_time") or ""),
                "locations": list(snapshot["ongoing"].get("locations") or []),
                "characters": list(snapshot["ongoing"].get("characters") or []),
                "npcs": list(snapshot["ongoing"].get("npcs") or []),
                "summary": summary_out.get("summary") or "",
                "turn_count": 10,
                "turns": [t for t in turns2[:10] if isinstance(t, dict)],
            }

            paras = arc0.get("paragraphs") if isinstance(arc0.get("paragraphs"), list) else []
            paras.append(para_obj)
            arc0["paragraphs"] = paras

            remaining_turns = [t for t in turns2[10:] if isinstance(t, dict)]

            arc_checkpoint = (len(paras) % 10 == 0)
            if arc_checkpoint:
                agg_locations: List[str] = []
                agg_characters: List[str] = []
                agg_npcs: List[str] = []
                for p in paras:
                    if not isinstance(p, dict):
                        continue
                    agg_locations = _merge_unique(agg_locations, list(p.get("locations") or []))
                    agg_characters = _merge_unique(agg_characters, list(p.get("characters") or []))
                    agg_npcs = _merge_unique(agg_npcs, list(p.get("npcs") or []))

                finalized_arc_name = (
                    str(summary_out.get("arc_name") or "").strip()
                    or str(arc0.get("name") or "").strip()
                    or "Arc"
                )
                finalized_arc_summary = str(summary_out.get("arc_summary") or "").strip() or str(para_obj.get("summary") or "")

                arc0["name"] = finalized_arc_name
                arc0["summary"] = finalized_arc_summary
                arc0["paragraph_count"] = len(paras)
                if not str(arc0.get("start_time") or "").strip():
                    arc0["start_time"] = str(paras[0].get("start_time") or para_obj.get("start_time") or "")
                arc0["end_time"] = str(para_obj.get("end_time") or "")
                arc0["locations"] = agg_locations
                arc0["characters"] = agg_characters
                arc0["npcs"] = agg_npcs

                arc0.pop("ongoing_paragraph", None)
                arcs2[0] = arc0

                new_ongoing = {
                    "start_time": str(remaining_turns[0].get("start_time") or para_obj.get("end_time") or "")
                    if remaining_turns
                    else str(para_obj.get("end_time") or ""),
                    "turns": remaining_turns,
                    "locations": [],
                    "characters": [],
                    "npcs": [],
                }
                for t in remaining_turns:
                    new_ongoing["locations"] = _merge_unique(new_ongoing.get("locations"), [str(t.get("location") or "")])
                    new_ongoing["characters"] = _merge_unique(new_ongoing.get("characters"), [str(x) for x in (t.get("characters") or []) if str(x).strip()])
                    new_ongoing["npcs"] = _merge_unique(new_ongoing.get("npcs"), [str(x) for x in (t.get("npcs") or []) if str(x).strip()])
                if len(remaining_turns) >= 10:
                    new_ongoing["_summary_pending"] = True

                arcs2.insert(
                    0,
                    {
                        "name": "Ongoing Arc",
                        "paragraphs": [],
                        "ongoing_paragraph": new_ongoing,
                    },
                )
            else:
                new_ongoing = {
                    "start_time": str(remaining_turns[0].get("start_time") or para_obj.get("end_time") or "")
                    if remaining_turns
                    else str(para_obj.get("end_time") or ""),
                    "turns": remaining_turns,
                    "locations": [],
                    "characters": [],
                    "npcs": [],
                }
                for t in remaining_turns:
                    new_ongoing["locations"] = _merge_unique(new_ongoing.get("locations"), [str(t.get("location") or "")])
                    new_ongoing["characters"] = _merge_unique(new_ongoing.get("characters"), [str(x) for x in (t.get("characters") or []) if str(x).strip()])
                    new_ongoing["npcs"] = _merge_unique(new_ongoing.get("npcs"), [str(x) for x in (t.get("npcs") or []) if str(x).strip()])
                if len(remaining_turns) >= 10:
                    new_ongoing["_summary_pending"] = True

                arc0["ongoing_paragraph"] = new_ongoing
                arcs2[0] = arc0

            _write_json(story_json, arcs2)

            try:
                _append_jsonl(
                    (workspace / "logs" / "gm_context_summary_updates.jsonl").resolve(),
                    {
                        "event": "gm_context_summary_updated_async",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "paragraph": para_obj,
                    },
                )
            except Exception:
                pass

