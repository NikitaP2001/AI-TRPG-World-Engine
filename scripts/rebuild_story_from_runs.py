from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _safe_literal_eval(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = ast.literal_eval(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _trim(s: str, max_chars: int) -> str:
    return (s or "").strip()


def _merge_unique(dst: List[str], items: List[str]) -> List[str]:
    out = list(dst)
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


def _heuristic_summary(turns: List[Dict[str, Any]]) -> str:
    beats: List[str] = []
    for t in turns[:10]:
        loc = str(t.get("location") or "").strip()
        narr = str(t.get("narration") or "").strip()
        first_line = ""
        for line in narr.splitlines():
            if line.strip():
                first_line = line.strip()
                break
        if not first_line:
            continue
        beats.append(f"[{loc}] {first_line}" if loc else first_line)

    if not beats:
        return "A sequence of events unfolded over several turns."
    return _trim(" ".join(beats), 1600)


def rebuild_story(
    *,
    runs_jsonl: Path,
    out_json: Path,
    keep_turns_compact: bool,
    turn_narration_max_chars: int,
) -> None:
    events = _read_jsonl(runs_jsonl)

    current_scene_start_time = ""
    current_scene_characters: List[str] = []
    current_scene_npcs: List[str] = []

    last_gm_tool_call: str = ""

    pending_turn_idx: int | None = None

    turns: List[Dict[str, Any]] = []

    for ev in events:
        if ev.get("scope") != "gm":
            continue
        event_type = str(ev.get("event") or "")
        if event_type == "tool_call":
            tool = str(ev.get("tool") or "")
            last_gm_tool_call = tool

            params_raw = str(ev.get("params") or "")
            params = _safe_literal_eval(params_raw) or {}

            if tool in {"start_scene", "run_scene"}:
                current_scene_characters = [
                    str(x) for x in (params.get("character_names") or []) if str(x).strip()
                ]
                npc_names = params.get("npc_names")
                if npc_names is None:
                    current_scene_npcs = []
                elif isinstance(npc_names, list):
                    current_scene_npcs = [str(x) for x in npc_names if str(x).strip()]
                else:
                    current_scene_npcs = []
                # start_time is set on the following tool_result.
                current_scene_start_time = ""
                continue

            if tool == "gm_output_turn":
                narration = str(params.get("narration") or "")
                location = str(params.get("location") or "")
                start_time = current_scene_start_time
                turns.append(
                    {
                        "start_time": start_time,
                        # Filled from the following tool_result (new_time).
                        "end_time": "",
                        "location": location,
                        "narration": narration,
                        "characters": list(current_scene_characters),
                        "npcs": list(current_scene_npcs),
                    }
                )
                pending_turn_idx = len(turns) - 1
                continue

        if event_type == "tool_result":
            # Capture start_time from the run_scene/start_scene tool_result.
            if last_gm_tool_call in {"start_scene", "run_scene"}:
                output_raw = str(ev.get("output") or "")
                try:
                    out_obj = json.loads(output_raw)
                except Exception:
                    out_obj = None
                if isinstance(out_obj, dict):
                    st = str(out_obj.get("start_time") or "").strip()
                    if st:
                        current_scene_start_time = st
            # Capture end_time from gm_output_turn tool_result.
            if last_gm_tool_call == "gm_output_turn" and pending_turn_idx is not None:
                output_raw = str(ev.get("output") or "")
                try:
                    out_obj = json.loads(output_raw)
                except Exception:
                    out_obj = None
                if isinstance(out_obj, dict):
                    end_t = str(out_obj.get("new_time") or "").strip()
                    if end_t and 0 <= pending_turn_idx < len(turns):
                        turns[pending_turn_idx]["end_time"] = end_t
                        # After gm_output_turn the world time advances; next scene start_time will reflect that.
                        current_scene_start_time = end_t
                pending_turn_idx = None
            continue

    # Build story.json v2
    arcs: List[Dict[str, Any]] = [
        {
            "name": "Ongoing Arc",
            "paragraphs": [],
            "ongoing_paragraph": {
                "start_time": (turns[0]["start_time"] if turns else ""),
                "turns": [],
                "locations": [],
                "characters": [],
                "npcs": [],
            },
        }
    ]

    arc0 = arcs[0]
    ongoing = arc0["ongoing_paragraph"]

    def flush_paragraph(chunk: List[Dict[str, Any]]) -> None:
        if not chunk:
            return
        start = str(chunk[0].get("start_time") or "")
        end = str(chunk[-1].get("end_time") or "")

        locations: List[str] = []
        characters: List[str] = []
        npcs: List[str] = []
        for t in chunk:
            locations = _merge_unique(locations, [str(t.get("location") or "")])
            characters = _merge_unique(characters, [str(x) for x in (t.get("characters") or [])])
            npcs = _merge_unique(npcs, [str(x) for x in (t.get("npcs") or [])])

        para: Dict[str, Any] = {
            "name": "Summary",
            "start_time": start,
            "end_time": end,
            "locations": [x for x in locations if x.strip()],
            "characters": [x for x in characters if x.strip()],
            "npcs": [x for x in npcs if x.strip()],
            "summary": _heuristic_summary(chunk),
            "turn_count": len(chunk),
            "summary_source": "rebuild:heuristic",
        }

        if keep_turns_compact:
            compact: List[Dict[str, Any]] = []
            for t in chunk:
                compact.append(
                    {
                        "start_time": str(t.get("start_time") or ""),
                        "end_time": str(t.get("end_time") or ""),
                        "location": str(t.get("location") or ""),
                        "characters": list(t.get("characters") or []),
                        "npcs": list(t.get("npcs") or []),
                        "narration": _trim(str(t.get("narration") or ""), turn_narration_max_chars),
                    }
                )
            para["turns_compact"] = compact

        arc0["paragraphs"].append(para)

    # Group into paragraphs of 10 turns.
    idx = 0
    while idx + 10 <= len(turns):
        flush_paragraph(turns[idx : idx + 10])
        idx += 10

    # Remaining turns go into ongoing.
    remaining = turns[idx:]
    ongoing["turns"] = remaining
    for t in remaining:
        ongoing["locations"] = _merge_unique(ongoing.get("locations", []), [str(t.get("location") or "")])
        ongoing["characters"] = _merge_unique(ongoing.get("characters", []), [str(x) for x in (t.get("characters") or [])])
        ongoing["npcs"] = _merge_unique(ongoing.get("npcs", []), [str(x) for x in (t.get("npcs") or [])])

    out_json.write_text(json.dumps(arcs, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild story.json (best-effort) from logs/runs.jsonl")
    parser.add_argument("--runs", default="logs/runs.jsonl", help="Path to runs.jsonl")
    parser.add_argument(
        "--out",
        default="game/world/story_rebuilt.json",
        help="Output story json path (default: game/world/story_rebuilt.json)",
    )
    parser.add_argument(
        "--keep-turns-compact",
        action="store_true",
        help="Store compact turn archive in each paragraph",
    )
    parser.add_argument(
        "--turn-narration-max-chars",
        type=int,
        default=400,
        help="Max chars for compact narration",
    )

    args = parser.parse_args()

    rebuild_story(
        runs_jsonl=Path(args.runs),
        out_json=Path(args.out),
        keep_turns_compact=bool(args.keep_turns_compact),
        turn_narration_max_chars=int(args.turn_narration_max_chars),
    )


if __name__ == "__main__":
    main()
