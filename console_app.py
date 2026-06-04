"""Console application runner for the LLM World simulation.

This module orchestrates two separate LLM agents:

1. Storage Assistant (gm/operator.py):
    - ReAct agent with tool access (scene management, world updates)
    - Maintains persistent conversation history in game/storage_assistant_messages.json
    - Uses atomic marker-based context deltas in persistent history
   - Prompt: agents/storage_assistant/prompt.txt
   - Scope: "storage_assistant"

2. Game Master (gm/game_master.py):
    - Narrative-only agent for creative writing tasks (world seed, scene descriptions, turn narration)
    - Maintains persistent history in game/game_master_messages.json
    - Uses build_game_master_context_block() without iteration mechanics
   - Prompt: agents/game_master/prompt.txt
   - Scope: "game_master"
   
The Storage Assistant runs the simulation loop, while Game Master generates narrative content.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

from gm.full_history import gm_max_turns_from_env, load_full_gm_messages, save_full_gm_messages, trim_full_gm_messages
from gm.operator import StorageAssistantFactory
from gm.history_injector import GMHistoryInjector, HistoryInjector
from gm.game_master import (
    GameMaster,
    parse_game_master_json,
    build_game_master_context,
    build_game_master_qa_context,
)
from gm.tools import (
    gm_allowed_tools,
    gm_tools_for_current_context,
    reset_turn_lock,
    is_context_changed,
    signal_context_changed,
    is_scene_request_pending,
    clear_scene_request,
)
from character.agent import run_character_agent, set_game_master_for_characters
from character.reflection import needs_reflection, run_reflection
from memory_store import HistoryLimits, approx_token_count, limits_from_env, trim_history, load_history
from backup_storylines import record_turn_snapshot
from openrouter_langchain_logging import logs_enabled, stream_path
from stream_watchdog import (
    InvalidGMOutputError,
    StreamWatchdog,
    get_detected_invalid_pattern,
    clear_detected_invalid_pattern,
    _clear_watchdog_abort,
    log_retry_with_correction,
)
from bootstrap import initialize_game_dir
from webui.override_state import OverrideStore
from scene import Scene
from world import World, WorldDuration, _json_error_snippet
from world.time import WorldTime
from scene.context import (
    build_focused_scene_context,
    collect_story_turns_newest_first,
    estimate_scene_start_time_for_history,
    extract_scene_result_from_narration,
    find_reusable_scene_description,
    story_turn_fingerprint,
)
from turn_runner import run_turn


# ---------------------------------------------------------------------------
# Turn recap persistence (for WebUI display of character thoughts/actions)
# ---------------------------------------------------------------------------
_TURN_RECAPS_FILENAME = "turn_recaps.jsonl"
_GUI_STREAM_INPUT_LOCK = threading.Lock()


def _append_turn_recap(game_root: Path, result: Dict[str, Any]) -> None:
    """Append a turn recap dict as a single JSON line to turn_recaps.jsonl.

    The WebUI reads this file to display per-character thoughts and actions
    on each turn card.  We keep it separate from SA messages so we don't
    inject orphaned ToolMessages that would confuse the SA's ReAct loop.
    """
    try:
        recaps_path = game_root / _TURN_RECAPS_FILENAME
        with open(recaps_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:
        pass


class ConsoleApp:
    _startup_scene_cleanup_done: bool = False

    def __init__(self) -> None:
        self.world = World()
        self.world.ensure_initialized()
        if not ConsoleApp._startup_scene_cleanup_done:
            self._discard_interrupted_scene_state()
            ConsoleApp._startup_scene_cleanup_done = True

        self.limits = limits_from_env()
        self.max_turns = gm_max_turns_from_env()

        # Background reflection workers (non-blocking for scene progression).
        raw_ref_workers = (os.getenv("LLM_WORLD_REFLECTION_WORKERS") or "").strip()
        try:
            reflection_workers = int(raw_ref_workers) if raw_ref_workers else 2
        except Exception:
            reflection_workers = 2
        reflection_workers = max(1, min(reflection_workers, 8))
        self._reflection_executor = ThreadPoolExecutor(
            max_workers=reflection_workers,
            thread_name_prefix="reflection",
        )
        self._reflection_jobs_lock = threading.Lock()
        self._reflection_jobs: set[str] = set()
        self._rag_search: Optional[Any] = None
        
        # Storage Assistant (ReAct agent with tools) - stores history in storage_assistant_messages.json
        self.storage_assistant_history_path = (
            self.world.game_root / "storage_assistant_messages.json"
        ).resolve()
        
        # Game Master (narrative agent) - stores history in game_master_messages.json
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        
        # Migrate old history filenames if needed.
        legacy_gm_history = (self.world.game_root / "gm_full_messages.json").resolve()
        legacy_operator_history = (self.world.game_root / "game_operator_messages.json").resolve()
        if legacy_gm_history.exists() and not self.storage_assistant_history_path.exists():
            try:
                legacy_gm_history.replace(self.storage_assistant_history_path)
            except Exception:
                pass
        if legacy_operator_history.exists() and not self.storage_assistant_history_path.exists():
            try:
                legacy_operator_history.replace(self.storage_assistant_history_path)
            except Exception:
                pass

        legacy_prev = legacy_gm_history.with_name("gm_full_messages.prev.json")
        legacy_operator_prev = legacy_operator_history.with_name("game_operator_messages.prev.json")
        new_prev = self.storage_assistant_history_path.with_name("storage_assistant_messages.prev.json")
        if legacy_prev.exists() and not new_prev.exists():
            try:
                legacy_prev.replace(new_prev)
            except Exception:
                pass
        if legacy_operator_prev.exists() and not new_prev.exists():
            try:
                legacy_operator_prev.replace(new_prev)
            except Exception:
                pass
        # Used for one-time init/plot.json bootstrapping: only on the first-ever run.
        self.gm_history_preexists = self.storage_assistant_history_path.exists()
        self._init_plot_injected_this_run: bool = False

        self._gm_factory = StorageAssistantFactory()
        
        # Game Master (narrative agent) - maintains roleplay identity with persistent history
        self._game_master = GameMaster(history_path=self.gm_master_history_path)
        self._gm_injector = GMHistoryInjector(
            game_master=self._game_master,
            gm_history_path=self.gm_master_history_path,
            history_limits=self.limits,
        )
        
        # Set up Game Master for character Q&A
        set_game_master_for_characters(self._game_master, self.world)

        # Wire GM as paragraph summarizer so story.py uses the GM instead of a separate agent
        self.world.set_summarizer(self._gm_summarizer)
        
        self.state: Dict[str, Any] = {"messages": []}
        self._sa_injector = HistoryInjector(
            history_loader=self._load_active_sa_history,
            delta_injector=self._inject_sa_delta,
        )

        # Live streaming: always write to logs/stream.txt; optionally echo to console.
        raw_stream = os.getenv("LLM_WORLD_STREAM_ECHO")
        if raw_stream is None or not raw_stream.strip():
            # Default: stream echo ON (still optional to toggle via /stream).
            self.stream_echo = True
        else:
            self.stream_echo = raw_stream.strip().lower() in {"1", "true", "yes", "y", "on"}
        self._apply_stream_env()

    def _apply_stream_env(self) -> None:
        os.environ["LLM_WORLD_STREAM_ECHO"] = "1" if self.stream_echo else "0"

    def _load_active_gm_history(self) -> List[Dict[str, str]]:
        try:
            msgs = load_history(self.gm_master_history_path)
            return trim_history(msgs, limits=self.limits)
        except Exception:
            return []

    def _load_active_sa_history(self) -> List[Any]:
        try:
            msgs = list(self.state.get("messages") or [])
            return trim_full_gm_messages(msgs, limits=self.limits, max_turns=self.max_turns)
        except Exception:
            return list(self.state.get("messages") or [])

    def _inject_sa_delta(self, content: str) -> None:
        body = str(content or "").strip()
        if not body:
            return
        msgs = list(self.state.get("messages") or [])
        msgs.append(HumanMessage(content=body))
        self.state = {"messages": msgs}

    def _gm_history_contains(self, marker: str) -> bool:
        return self._gm_injector.history_contains(marker)

    def _ensure_gm_bootstrap(self) -> None:
        """Inject atomic world-state anchor messages into GM history on first run.

        Replaces the old single [world_snapshot:world] monolith with smaller,
        logically separate messages so each can be cached and updated independently.

        Backwards compatibility: if the old [world_snapshot:world] is already
        present (existing game), the legacy message is left untouched and no new
        anchors are injected.
        """
        try:
            # Legacy path: already bootstrapped with the old monolithic message.
            if self._gm_history_contains("[world_snapshot:world]"):
                return

            # 1. World metadata: name, time, location names, NPC names.
            self._gm_injector.ensure_world_meta(world=self.world)

            # 2. Per-character descriptions (one message per character).
            #    Reuse the [player_description:NAME] marker so scene-pick
            #    injection won't duplicate them.
            for name in self.world.list_character_names():
                if not name:
                    continue

                self._gm_injector.ensure_character_description(world=self.world, name=str(name))

            # 3. Story summaries recovery pass: re-inject trimmed-out paragraph/arc deltas.
            self._gm_injector.ensure_story_summaries(world=self.world)
        except Exception:
            pass

    def _maybe_inject_gm_entity_description(self, marker: str, content: str) -> None:
        if not marker or not content:
            return
        self._gm_injector.inject_if_absent(marker=marker, content=content)

    def _maybe_inject_gm_scene_pick_context(
        self,
        selected_location: str,
        selected_characters: List[str],
        selected_npcs: List[str],
    ) -> None:
        if selected_location:
            self._gm_injector.ensure_location_description(world=self.world, location=selected_location)
        for name in selected_characters:
            if not name:
                continue
            self._gm_injector.ensure_character_description(world=self.world, name=str(name))
        for name in selected_npcs:
            if not name:
                continue
            self._gm_injector.ensure_npc_description(world=self.world, name=str(name))

    def _maybe_inject_gm_scene_description_context(
        self,
        location: str,
        descriptions: Dict[str, str],
        selected_characters: List[str],
        selected_npcs: List[str],
        combined: str = "",
    ) -> None:
        if location:
            text_for_context = combined
            if not text_for_context and descriptions:
                text_for_context = "\n\n".join(
                    f"[{name}]\n{text}"
                    for name, text in descriptions.items()
                    if text
                )
            if text_for_context:
                self._maybe_inject_gm_entity_description(
                    f"[scene_description:{location}]",
                    f"Location: {location}\n\n{text_for_context}",
                )
        self._maybe_inject_gm_scene_pick_context(location, selected_characters, selected_npcs)

    def _load_story_arc0(self) -> Dict[str, Any]:
        try:
            raw = self.world.paths.story_json.read_text(encoding="utf-8")
            arcs = json.loads(raw)
            if not isinstance(arcs, list) or not arcs:
                return {}
            arc0 = arcs[0]
            return arc0 if isinstance(arc0, dict) else {}
        except Exception:
            return {}

    def story_progress(self) -> Tuple[int, int]:
        """Return (turns_count, paragraphs_count) for arc[0]."""
        arc0 = self._load_story_arc0()
        paragraphs_count = 0
        paragraphs = arc0.get("paragraphs") if isinstance(arc0, dict) else None
        if isinstance(paragraphs, list):
            paragraphs_count = len(paragraphs)

        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        if not isinstance(ongoing, dict):
            return (0, paragraphs_count)
        turns = ongoing.get("turns")
        if not isinstance(turns, list):
            turns = []
        return (len(turns), paragraphs_count)

    def story_progress_and_last_text(self) -> Tuple[int, int, str]:
        arc0 = self._load_story_arc0()
        paragraphs = arc0.get("paragraphs") if isinstance(arc0, dict) else None
        paragraphs_list = paragraphs if isinstance(paragraphs, list) else []

        paragraphs_count = len(paragraphs_list)

        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        if not isinstance(ongoing, dict):
            return (0, paragraphs_count, "")
        turns = ongoing.get("turns")
        turns_list = turns if isinstance(turns, list) else []

        turns_count = len(turns_list)

        # Prefer the latest turn narration; if the buffer was just summarized,
        # fall back to the latest paragraph's last turn narration, then its summary.
        last_text = ""
        if turns_list:
            last_turn = turns_list[-1] if isinstance(turns_list[-1], dict) else {}
            last_text = str((last_turn or {}).get("narration") or "")
        elif paragraphs_list:
            last_para = paragraphs_list[-1]
            if isinstance(last_para, dict):
                para_turns = last_para.get("turns") if isinstance(last_para.get("turns"), list) else []
                if para_turns:
                    last_turn = para_turns[-1] if isinstance(para_turns[-1], dict) else {}
                    last_text = str((last_turn or {}).get("narration") or "")
                if not last_text:
                    last_text = str(last_para.get("summary") or "")
            else:
                last_text = str(last_para or "")

        return (turns_count, paragraphs_count, last_text)

    def _scene_is_active(self) -> bool:
        try:
            scene = self.world.get_scene()
            return bool(isinstance(scene, dict) and scene.get("state") == "active")
        except Exception:
            return False

    def _discard_interrupted_scene_state(self) -> None:
        """Drop persisted in-progress scene state from previous interrupted runs."""

        try:
            scene = self.world.get_scene()
        except Exception:
            return

    def _abort_active_scene_turn(self, *, reason: str) -> None:
        """Abort the current in-progress scene turn and clear scene state."""

        try:
            self.world.clear_scene()
        except Exception:
            pass

        try:
            clear_scene_request()
        except Exception:
            pass

        try:
            signal_context_changed()
        except Exception:
            pass

        print(f"Error: turn aborted and cleared due to character failure ({reason})")

        if logs_enabled():
            print(f"[trace] scene turn aborted: {reason}")

        scene_state = str((scene or {}).get("state") or "").strip() if isinstance(scene, dict) else ""
        if scene_state != "active":
            return

        try:
            self.world.clear_scene()
            if logs_enabled():
                print(f"[trace] dropped interrupted scene state from previous run (state={scene_state})")
        except Exception:
            return

    def _locations_count(self) -> int:
        try:
            locs = self.world.get_locations()
            return len(locs) if isinstance(locs, dict) else 0
        except Exception:
            return 0

    def _progress_snapshot(self) -> Tuple[int, bool, int, int, int]:
        """Return a tuple of (time_s, scene_active, turns, paragraphs, locations_count)."""

        time_s = self.world.get_world_time().to_seconds()
        scene_active = self._scene_is_active()
        turns, paragraphs, _ = self.story_progress_and_last_text()
        locs_count = self._locations_count()
        return (time_s, scene_active, turns, paragraphs, locs_count)

    def _did_story_progress(
        self,
        *,
        before_snapshot: Tuple[int, bool, int, int, int],
        after_snapshot: Tuple[int, bool, int, int, int],
    ) -> bool:
        """Return True when story advanced by at least one turn or paragraph."""

        try:
            return (int(after_snapshot[2]) > int(before_snapshot[2])) or (
                int(after_snapshot[3]) > int(before_snapshot[3])
            )
        except Exception:
            return False

    def _needs_world_seed(self) -> bool:
        # Fallback guard: if seed bootstrap marker is already present in SA history,
        # do not request another world seed even if gm_bootstrap.json failed to persist.
        try:
            for msg in list(self.state.get("messages") or []):
                content = str(getattr(msg, "content", "") or "")
                if "[bootstrap_world_seed_v1]" in content:
                    if logs_enabled():
                        print("[trace] world_seed: skipped (bootstrap marker already present in history)")
                    return False
        except Exception:
            pass

        try:
            gm_bootstrap = self._read_gm_bootstrap()
            if bool(gm_bootstrap.get("world_seed_injected")):
                if logs_enabled():
                    print("[trace] world_seed: skipped (already injected)")
                return False
        except Exception:
            pass

        try:
            locs = self.world.get_locations()
            if isinstance(locs, dict) and locs:
                if logs_enabled():
                    print(f"[trace] world_seed: skipped (locations={len(locs)})")
                return False
        except Exception:
            pass

        arc0 = self._load_story_arc0()
        paragraphs = arc0.get("paragraphs") if isinstance(arc0, dict) else None
        if isinstance(paragraphs, list):
            # Ignore the "Initial Plot" paragraph that is always prepended on init.
            real_paragraphs = [p for p in paragraphs if not (isinstance(p, dict) and str(p.get("name") or "").strip() == "Initial Plot")]
            if real_paragraphs:
                if logs_enabled():
                    print("[trace] world_seed: skipped (paragraphs exist)")
                return False

        ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0, dict) else None
        turns_list = ongoing.get("turns") if isinstance(ongoing, dict) else None
        if not isinstance(turns_list, list) or not turns_list:
            if logs_enabled():
                print("[trace] world_seed: eligible (no turns)")
            return True

        # If the only content is a plot_seed turn, treat the story as empty.
        if all(isinstance(t, dict) and str(t.get("kind") or "") == "plot_seed" for t in turns_list):
            if logs_enabled():
                print("[trace] world_seed: eligible (plot_seed only)")
            return True

        if logs_enabled():
            print("[trace] world_seed: skipped (story already progressed)")
        return False

    def _maybe_run_world_seed(self) -> bool:
        needs_full_seed = self._needs_world_seed()
        time_recovery_only = False

        if not needs_full_seed:
            # Recovery mode: if full seed is no longer eligible but world time is
            # still the default placeholder, request only authoritative seed time.
            try:
                gm_bootstrap = self._read_gm_bootstrap()
                if bool(gm_bootstrap.get("world_seed_time_applied")):
                    return False
            except Exception:
                pass

            try:
                current_time = self.world.get_world_time().to_string()
            except Exception:
                current_time = ""

            if current_time == "Y0000-01-01 00:00:00":
                time_recovery_only = True
                if logs_enabled():
                    print("[trace] world_seed: recovery mode (time-only; full seed skipped)")
            else:
                return False

        try:
            gm_bootstrap = self._read_gm_bootstrap()
            if bool(gm_bootstrap.get("world_seed_injected")) and not time_recovery_only:
                return False
        except Exception:
            pass

        if logs_enabled():
            print("[trace] world_seed: invoking Game Master")

        ctx = build_game_master_context(self.world)
        payload = {"character_names": self.world.list_character_names()}
        seed = self._game_master.run_world_seed(payload=payload, context_text=ctx)
        seed_text = str(seed.get("seed_text") or "").strip()
        if not seed_text and not time_recovery_only:
            print("Error: WORLD_SEED did not provide any text")
            return False

        # WORLD_SEED time must come from tool parameter, not free text.
        world_time_raw = str(seed.get("world_time") or "").strip()
        if not world_time_raw:
            print(
                "Error: WORLD_SEED must provide world_time as gm_world_seed_result parameter. "
                "Seed not applied."
            )
            return False

        # Accept missing leading 'Y' and normalize to canonical world time format.
        if world_time_raw[:1].isdigit() and len(world_time_raw) >= 5 and world_time_raw[4:5] == "-":
            world_time_raw = f"Y{world_time_raw}"

        try:
            wt = WorldTime.parse(world_time_raw)
        except Exception as e:
            print(f"Error: invalid WORLD TIME in seed: {e}")
            return False

        if wt.year <= 1:
            print(
                "Error: WORLD TIME must be meaningful and cannot use bootstrap-like years 0000/0001. "
                "Use a datetime that matches the plot and setting."
            )
            return False

        # Apply canonicalized world time.
        self.world.set_world_time(wt.to_string())

        if time_recovery_only:
            self._set_gm_bootstrap_flag(
                {
                    "world_seed_time_applied": True,
                    "world_seed_time_only_at": datetime.now(timezone.utc).isoformat(),
                    "world_seed_time_value": wt.to_string(),
                }
            )
            return True

        marker = "[bootstrap_world_seed_v1]"

        # Inject seed into Storage Assistant history for tool-driven setup.
        seed_msg = (
            f"{marker}\n"
            "WORLD_SEED (from Game Master; authoritative).\n"
            f"WORLD_TIME_APPLIED: {wt.to_string()}\n"
            "Use this to create locations and assign character locations. Do not invent extra locations.\n\n"
            f"{seed_text}\n"
        )

        self.state = {"messages": [AIMessage(content=seed_msg)] + list(self.state.get("messages") or [])}
        self.save_gm_history()

        self._set_gm_bootstrap_flag(
            {
                "world_seed_injected": True,
                "world_seed_version": 1,
                "world_seed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        return True

    def _gm_summarizer(self, world_context: str, ongoing_paragraph: Dict[str, Any]) -> Dict[str, Any]:
        """Use the Game Master to summarize a paragraph (called by story.py every 10 turns)."""
        if logs_enabled():
            print("[trace] GM summarizing paragraph...")

        # Collect existing paragraph names so the GM doesn't repeat them.
        existing_names: list[str] = []
        existing_arc_names: list[str] = []
        do_arc_summary = False
        paragraphs: list = []
        try:
            story_data = self.world.get_story()
            if isinstance(story_data, list) and story_data:
                arc0 = story_data[0] if isinstance(story_data[0], dict) else {}
                paragraphs = arc0.get("paragraphs") if isinstance(arc0.get("paragraphs"), list) else []
                next_paragraph_number = len(paragraphs) + 1
                do_arc_summary = (next_paragraph_number % 10 == 0)
                for arc in story_data:
                    if not isinstance(arc, dict):
                        continue
                    nm = str(arc.get("name") or "").strip()
                    if nm and nm not in existing_arc_names:
                        existing_arc_names.append(nm)
                for p in paragraphs:
                    n = str(p.get("name") or "").strip()
                    if n:
                        existing_names.append(n)
        except Exception:
            pass

        self._ensure_gm_bootstrap()
        # GM handles paragraph-level summary only; arc summarization is routed to
        # the standalone summarizer agent for a clean, history-free context.
        raw = self._game_master.run_task(
            task="PARAGRAPH_SUMMARY",
            payload={
                "ongoing_paragraph": ongoing_paragraph,
                "existing_paragraph_names": existing_names,
            },
            context_text="",
        )

        # Parse JSON from GM output
        parsed = parse_game_master_json(raw)
        name = str(parsed.get("name") or "").strip()
        summary = str(parsed.get("summary") or "").strip()

        if not name:
            name = "Summary"
        if not summary:
            summary = raw if raw else "(failed to summarize paragraph)"

        # Arc summarization: standalone summarizer agent with full paragraph context.
        arc_name = ""
        arc_summary = ""
        if do_arc_summary:
            from summarizer_agent import summarize_arc
            ongoing_turns = ongoing_paragraph.get("turns") if isinstance(ongoing_paragraph.get("turns"), list) else []
            current_para = {
                "name": name,
                "summary": summary,
                "start_time": str(ongoing_paragraph.get("start_time") or "").strip(),
                "end_time": str((ongoing_turns[-1] or {}).get("end_time") or "").strip() if ongoing_turns else "",
                "locations": list(ongoing_paragraph.get("locations") or []),
                "characters": list(ongoing_paragraph.get("characters") or []),
                "npcs": list(ongoing_paragraph.get("npcs") or []),
            }
            arc_paragraphs = [p for p in paragraphs if isinstance(p, dict)] + [current_para]
            try:
                arc_result = summarize_arc(
                    paragraphs=arc_paragraphs,
                    existing_arc_names=existing_arc_names,
                )
                arc_name = str(arc_result.get("arc_name") or "").strip()
                arc_summary = str(arc_result.get("arc_summary") or "").strip()
            except Exception:
                pass
            if not arc_name:
                arc_name = "Arc"
            if not arc_summary:
                arc_summary = summary

        if logs_enabled():
            print(f"[trace] GM paragraph summary: {name!r} ({len(summary)} chars)")
            if arc_name:
                print(f"[trace] Arc summary: {arc_name!r} ({len(arc_summary)} chars)")

        out = {
            "name": name,
            "summary": summary,
        }
        if do_arc_summary:
            out["arc_name"] = arc_name
            out["arc_summary"] = arc_summary

        # Inject completed paragraph summary into GM history as a delta message so
        # future task calls learn about it without needing the full context blob.
        if name and summary and name != "Summary":
            try:
                locations = ongoing_paragraph.get("locations") if isinstance(ongoing_paragraph.get("locations"), list) else []
                characters = ongoing_paragraph.get("characters") if isinstance(ongoing_paragraph.get("characters"), list) else []
                npcs_list = ongoing_paragraph.get("npcs") if isinstance(ongoing_paragraph.get("npcs"), list) else []
                para_start_time = str(ongoing_paragraph.get("start_time") or "").strip()
                para_end_time = ""
                turns_for_time = ongoing_paragraph.get("turns") if isinstance(ongoing_paragraph.get("turns"), list) else []
                if turns_for_time:
                    para_end_time = str((turns_for_time[-1] or {}).get("end_time") or "").strip()
                self._gm_injector.inject_paragraph_summary(
                    name=name,
                    summary=summary,
                    start_time=para_start_time,
                    end_time=para_end_time,
                    locations=list(locations),
                    characters=list(characters),
                    npcs=list(npcs_list),
                )

                if do_arc_summary and arc_name and arc_summary:
                    # Aggregate locations, characters, and NPCs across all 10 arc paragraphs.
                    arc_all_paras = [p for p in paragraphs if isinstance(p, dict)] + [
                        {"name": name, "locations": locations, "characters": characters, "npcs": npcs_list}
                    ]
                    agg_locs: list[str] = []
                    agg_chars: list[str] = []
                    agg_npcs: list[str] = []
                    para_names: list[str] = []
                    arc_start_time = ""
                    arc_end_time = ""
                    for p in arc_all_paras:
                        pn = str(p.get("name") or "").strip()
                        if pn:
                            para_names.append(pn)
                        p_start = str(p.get("start_time") or "").strip()
                        p_end = str(p.get("end_time") or "").strip()
                        if p_start and not arc_start_time:
                            arc_start_time = p_start
                        if p_end:
                            arc_end_time = p_end
                        for loc in (p.get("locations") or []):
                            s = str(loc).strip()
                            if s and s not in agg_locs:
                                agg_locs.append(s)
                        for ch in (p.get("characters") or []):
                            s = str(ch).strip()
                            if s and s not in agg_chars:
                                agg_chars.append(s)
                        for npc in (p.get("npcs") or []):
                            s = str(npc).strip()
                            if s and s not in agg_npcs:
                                agg_npcs.append(s)
                    self._gm_injector.inject_arc_summary(
                        arc_name=arc_name,
                        arc_summary=arc_summary,
                        start_time=arc_start_time,
                        end_time=arc_end_time,
                        paragraph_names=para_names,
                        locations=agg_locs,
                        characters=agg_chars,
                        npcs=agg_npcs,
                    )
            except Exception:
                pass

        return out


    def _maybe_run_scene_description(self) -> bool:
        if not is_scene_request_pending():
            return False

        # If a scene is already active, clear stale request and continue.
        try:
            cur_scene = self.world.get_scene()
            if isinstance(cur_scene, dict) and str(cur_scene.get("state") or "").strip() == "active":
                clear_scene_request()
                return False
        except Exception:
            pass

        scene: Dict[str, Any] = {}

        try:
            scene = self.world.get_scene()
        except Exception:
            scene = {}

        if isinstance(scene, dict) and str(scene.get("state") or "").strip() == "active":
            clear_scene_request()
            return False

        self._ensure_gm_bootstrap()

        character_time_overview: list[dict] = []
        world_time_of_day: str = ""
        try:
            from world.time import WorldTime

            wt = self.world.get_world_time()
            wt_sec = wt.to_seconds()
            world_time_of_day = wt.time_of_day()
            info = self.world.get_info()
            chars = info.get("characters") if isinstance(info, dict) else []

            rows: list[tuple[int, dict]] = []
            for ch in (chars if isinstance(chars, list) else []):
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "?").strip() or "?"
                loc = str(ch.get("location") or "unknown").strip() or "unknown"
                la = str(ch.get("last_acted") or "").strip()
                if not la or la == "never":
                    rows.append(
                        (
                            10 ** 18,  # never acted = highest priority, sort first
                            {
                                "name": name,
                                "last_acted": "never",
                                "location": loc,
                            },
                        )
                    )
                    continue
                try:
                    la_wt = WorldTime.parse(la)
                    delta = wt_sec - la_wt.to_seconds()
                except Exception:
                    rows.append(
                        (
                            10 ** 18,  # parse error = treat as never acted
                            {
                                "name": name,
                                "last_acted": la or "unknown",
                                "location": loc,
                            },
                        )
                    )
                    continue
                delta = max(int(delta), 0)
                rows.append(
                    (
                        delta,
                        {
                            "name": name,
                            "last_acted": la,
                            "location": loc,
                        },
                    )
                )
            rows.sort(key=lambda x: x[0], reverse=True)

            # Compute turns_without_attention: turns since character last participated.
            try:
                recaps_path = self.world.game_root / _TURN_RECAPS_FILENAME
                recap_lines: list[str] = []
                if recaps_path.exists():
                    with open(recaps_path, encoding="utf-8") as _rf:
                        recap_lines = [_l.strip() for _l in _rf if _l.strip()]
                total_turns = len(recap_lines)
                last_seen: dict[str, int] = {}
                for _i, _line in enumerate(recap_lines):
                    try:
                        _rec = json.loads(_line)
                        _sr = _rec.get("scene_recap") if isinstance(_rec, dict) else None
                        _chars = _sr.get("characters") if isinstance(_sr, dict) else []
                        if isinstance(_chars, list):
                            for _ch in _chars:
                                if isinstance(_ch, dict):
                                    _cname = str(_ch.get("name") or "").strip()
                                    if _cname:
                                        last_seen[_cname] = _i
                        elif isinstance(_chars, dict):
                            for _cname in _chars:
                                last_seen[str(_cname)] = _i
                    except Exception:
                        pass
                for _, _row in rows:
                    _nm = _row.get("name", "")
                    _row["turns_without_attention"] = (
                        total_turns - (last_seen[_nm] + 1) if _nm in last_seen else total_turns
                    )
            except Exception:
                pass

            character_time_overview = [row[1] for row in rows]
        except Exception:
            pass

        # Scene bootstrap hints from pending scene state (if any).
        catch_up = scene.get("catch_up") if isinstance(scene.get("catch_up"), dict) else None
        selected_characters = scene.get("initiative_order") if isinstance(scene.get("initiative_order"), list) else []
        selected_characters = [str(x).strip() for x in selected_characters if str(x).strip()]
        selected_location = str(scene.get("location") or "").strip()
        selected_npcs = scene.get("npcs") if isinstance(scene.get("npcs"), list) else []
        selected_npcs = [str(x).strip() for x in selected_npcs if str(x).strip()]
        scene_time_shift = "0"

        # Enrich GM prompt with targeted context (if location/participants already preselected).
        descriptions: Dict[str, str] = {}
        if False:  # scene description reuse disabled
            pass

        if not descriptions:
            focused_ctx = ""
            missing_locations: list[str] = []
            missing_npcs: list[str] = []
            scene_start_time_hint = ""
            if selected_characters and selected_location:
                focused_ctx, missing_locations, missing_npcs = build_focused_scene_context(
                    self.world,
                    selected_characters=selected_characters,
                    selected_location=selected_location,
                    selected_npcs=selected_npcs,
                )
                scene_start_time_hint = estimate_scene_start_time_for_history(
                    self.world,
                    selected_characters=selected_characters,
                    scene_time_shift=scene_time_shift,
                )

            payload = {
                "location": selected_location,
                "character_names": selected_characters,
                "npcs": selected_npcs,
                "time_shift": scene_time_shift,
                "turn_start_time": scene_start_time_hint,
                "character_time_overview": character_time_overview,
                "world_time_of_day": world_time_of_day,
                "missing_locations": missing_locations,
                "missing_npcs": missing_npcs,
            }
            if catch_up:
                payload["catch_up"] = catch_up

            # Pass only the focused (scene-specific) context; world state lives in GM history.
            ctx = focused_ctx.strip() if focused_ctx else ""
            plan_ctx = self._read_world_facts_context()
            if plan_ctx:
                ctx = (ctx + "\n\n" + plan_ctx).strip()

            max_scene_desc_retries = 5
            _desc_combined: str = ""
            for _sd_attempt in range(max_scene_desc_retries):
                try:
                    result = self._game_master.run_scene_description(payload=payload, context_text=ctx)
                except KeyboardInterrupt:
                    # Defensive: some streaming paths may surface callback aborts here.
                    if logs_enabled():
                        print(
                            f"[trace] GM scene_description attempt {_sd_attempt + 1}/{max_scene_desc_retries} raised KeyboardInterrupt; retrying"
                        )
                    continue
                except Exception as e:  # noqa: BLE001
                    if logs_enabled():
                        print(
                            f"[trace] GM scene_description attempt {_sd_attempt + 1}/{max_scene_desc_retries} raised {type(e).__name__}: {e}; retrying"
                        )
                    continue
                selected_characters = [
                    str(x).strip()
                    for x in (result.get("character_names") if isinstance(result.get("character_names"), list) else [])
                    if str(x).strip()
                ]
                selected_location = str(result.get("location") or "").strip()
                selected_npcs = [
                    str(x).strip()
                    for x in (result.get("scene_npc") if isinstance(result.get("scene_npc"), list) else [])
                    if str(x).strip()
                ]
                scene_time_shift = str(result.get("time_shift") or "0").strip() or "0"

                # Catch-up hard rule: force the behind character as the only participant.
                if isinstance(catch_up, dict) and catch_up.get("character"):
                    behind = str(catch_up.get("character") or "").strip()
                    if behind:
                        selected_characters = [behind]
                    if not selected_location:
                        selected_location = str(catch_up.get("original_location") or "").strip()

                if not selected_characters or not selected_location:
                    if logs_enabled():
                        print(
                            f"[trace] GM scene_description attempt {_sd_attempt + 1}/{max_scene_desc_retries} missing scene pick fields; retrying"
                        )
                    continue

                result_descs = result.get("descriptions", {})
                if not isinstance(result_descs, dict):
                    result_descs = {}
                missing_players = [c for c in selected_characters if c not in result_descs]
                if result_descs and not missing_players:
                    descriptions = result_descs
                    _desc_combined = result.get("combined", "") if isinstance(result, dict) else ""
                    self._append_gui_stream_scene_pick(
                        selected_location=selected_location,
                        selected_characters=selected_characters,
                        selected_npcs=selected_npcs,
                        time_shift=scene_time_shift,
                    )
                    self._maybe_inject_gm_scene_pick_context(
                        selected_location=selected_location,
                        selected_characters=selected_characters,
                        selected_npcs=selected_npcs,
                    )
                    break
                if logs_enabled():
                    if missing_players:
                        print(f"[trace] GM scene_description attempt {_sd_attempt + 1}/{max_scene_desc_retries} missing players {missing_players}; retrying")
                    else:
                        print(f"[trace] GM scene_description attempt {_sd_attempt + 1}/{max_scene_desc_retries} returned empty; retrying")

        if not descriptions:
            print(f"Error: Game Master did not provide scene descriptions after {max_scene_desc_retries} attempts")
            return True

        self._maybe_inject_gm_scene_description_context(
            location=selected_location,
            descriptions=descriptions,
            selected_characters=selected_characters,
            selected_npcs=selected_npcs,
            combined=_desc_combined,
        )

        # Auto-start scene immediately after GM description.
        try:
            # Pre-start healing/alignment so strict Scene.start does not fail on
            # newly introduced location/NPC names or stale character locations.
            try:
                self.world.get_location(selected_location)
            except Exception:
                self.world.create_location(
                    name=selected_location,
                    summary="Auto-created placeholder location",
                    details="Created automatically by orchestration because selected scene location did not exist yet.",
                )

            for cname in selected_characters:
                desc = self.world.get_character_description(cname)
                cur_loc = str(desc.get("location") or "").strip() if isinstance(desc, dict) else ""
                if not cur_loc or cur_loc != selected_location:
                    self.world.add_character_json(
                        name=cname,
                        pointer="/location",
                        value=json.dumps(selected_location, ensure_ascii=False),
                    )

            # Timeline sync for multi-character scenes: align to the most advanced
            # selected timestamp to keep one shared scene time anchor.
            if len(selected_characters) > 1:
                from world.time import WorldTime

                parsed_times: Dict[str, Optional[WorldTime]] = {}
                for cname in selected_characters:
                    try:
                        meta = self.world.get_character_metadata(cname)
                        raw = str(meta.get("last_acted") or "").strip()
                        if raw and raw.lower() != "never":
                            parsed_times[cname] = WorldTime.parse(raw)
                        else:
                            parsed_times[cname] = None
                    except Exception:
                        parsed_times[cname] = None

                known = [t for t in parsed_times.values() if t is not None]
                if known:
                    max_time = max(known, key=lambda t: t.to_seconds())
                    max_time_s = max_time.to_seconds()
                    max_time_str = max_time.to_string()
                    for cname in selected_characters:
                        cur_t = parsed_times.get(cname)
                        if cur_t is None or cur_t.to_seconds() < max_time_s:
                            try:
                                self.world.set_character_last_acted(name=cname, last_acted=max_time_str)
                            except Exception:
                                pass

            # Heal NPCs: create missing entries and align location.
            npcs_map: Dict[str, Any] = {}
            try:
                loaded_npcs = self.world.get_npcs()
                if isinstance(loaded_npcs, dict):
                    npcs_map = loaded_npcs
            except Exception:
                npcs_map = {}

            healed_npcs: List[str] = []
            for npc_name in selected_npcs:
                entry = npcs_map.get(npc_name)
                if not isinstance(entry, dict):
                    try:
                        self.world.create_npc(
                            name=npc_name,
                            location=selected_location,
                            current_state="",
                            description="Auto-created placeholder NPC",
                        )
                        healed_npcs.append(npc_name)
                        continue
                    except Exception:
                        continue

                npc_loc = str(entry.get("location") or "").strip()
                if not npc_loc or npc_loc != selected_location:
                    try:
                        self.world.update_npc_json(
                            name=npc_name,
                            pointer="/location",
                            value=json.dumps(selected_location, ensure_ascii=False),
                        )
                    except Exception:
                        continue
                healed_npcs.append(npc_name)

            from scene import Scene
            scene_obj = Scene(world=self.world)
            combined_scene_desc = _desc_combined or "\n\n".join(
                f"[{name}]\n{text}" for name, text in descriptions.items() if text
            ).strip()
            scene_obj.start(
                character_names=selected_characters,
                location=selected_location,
                npc_names=healed_npcs,
                scene_description=combined_scene_desc,
                player_descriptions=descriptions,
                time_shift=scene_time_shift,
            )
            clear_scene_request()
            if logs_enabled():
                print(
                    f"[trace] scene auto-started at '{selected_location}' with "
                    f"{selected_characters}"
                )

            # World planning: GM pre-establishes hidden facts before knowing player intents.
            try:
                plan_ctx = build_game_master_context(self.world)
                print("[trace] world_planning: starting...")
                world_facts = self._game_master.run_world_planning(context_text=plan_ctx)
                if world_facts:
                    print("[trace] world_planning: facts received, injecting into SA")
                    self._inject_sa_delta(f"[world_facts]\n{world_facts}")
                else:
                    print("[trace] world_planning: GM returned no facts")
            except Exception as e:
                print(f"[trace] world_planning error: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"Error: failed to auto-start scene after description: {e}")
            return True

        return True

    def _has_unprocessed_turn_complete(self) -> bool:
        """Check if the last [turn_complete] message hasn't been acted on by the SA yet.

        Returns True when the most recent HumanMessage contains [turn_complete]
        and there is no subsequent AI message with tool calls (meaning the SA
        hasn't had a chance to persist facts yet).
        """
        msgs = list(self.state.get("messages") or [])
        # Walk backwards: find the last HumanMessage with [turn_complete].
        found_turn_complete = False
        for m in reversed(msgs):
            t = getattr(m, "type", "")
            if t in {"ai", "assistant"}:
                # SA already responded after the turn_complete.
                if found_turn_complete:
                    return False
                # An AI message before we found turn_complete — keep searching.
                continue
            if t == "tool":
                # Tool result after the turn_complete means SA processed it.
                if found_turn_complete:
                    return False
                continue
            if t == "human":
                content = str(getattr(m, "content", "") or "")
                if "[turn_complete]" in content:
                    found_turn_complete = True
                    continue
                # Some other human message after [turn_complete] — SA got input.
                if found_turn_complete:
                    return False
        return found_turn_complete
    def _auto_execute_characters_in_scene(self) -> bool:
        """Automatically execute character agents for an active scene.
        
        Characters are invoked one-by-one in initiative order. Each makes a
        single decision and is immediately marked as acted.
        
        If a character is armed for human override (via the web UI), execution
        pauses: a ``pending_prompt`` is written so the UI can show the decision
        form, and this method returns immediately.  On the next call, if a
        ``pending_decision`` is available for that character, it is consumed
        instead of calling the LLM agent.
        
        Returns True if any character was executed, False otherwise.
        """
        try:
            scene = self.world.get_scene()
        except Exception:
            return False

        if not (isinstance(scene, dict) and scene.get("state") == "active"):
            return False

        chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
        if not chars:
            return False

        # Find characters who haven't acted yet.
        remaining = [
            name for name, entry in chars.items()
            if not (entry if isinstance(entry, dict) else {}).get("acted")
        ]
        if not remaining:
            return False

        # Get initiative order (or use remaining list order)
        initiative = scene.get("initiative_order") if isinstance(scene.get("initiative_order"), list) else []
        if not initiative:
            initiative = list(chars.keys())

        scene_obj = Scene(world=self.world)

        # Load web-UI override state once.
        ov_store = OverrideStore(str(self.world.game_root.parent))
        armed_name = ov_store.armed_character()

        # Execute each character in initiative order
        executed_any = False
        for character_name in initiative:
            if character_name not in remaining:
                continue

            if logs_enabled():
                print(f"[trace] auto-executing character: {character_name}")

            current_scene_context = self._build_character_input(scene, character_name)
            self._append_gui_stream_character_input(character_name=character_name, body=current_scene_context)
            loc_name = str(scene.get("location") or "").strip()
            # Use the scene's start_time for memory keying so that each scene
            # gets its own memory entry even when the global clock hasn't moved
            # (e.g. another character's timeline is ahead).
            world_time_str = str(scene.get("start_time") or "") or self.world.get_world_time().to_string()

            # ── Override check ──────────────────────────────────────────
            if armed_name and armed_name == character_name:
                pending_dec = ov_store.get_pending_decision()
                if pending_dec and pending_dec.character_name == character_name:
                    # Human submitted a decision — consume it.
                    action = pending_dec.intent
                    thoughts = pending_dec.thoughts
                    gm_answers = []
                    output_source = "human_override"
                    raw_model_output = ""
                    ov_store.consume_pending_decision()
                    if logs_enabled():
                        print(f"[trace] using human override for {character_name}")
                else:
                    # No decision yet — write the prompt and pause.
                    ov_store.set_pending_prompt(
                        character_name=character_name,
                        scene_location=loc_name,
                        world_time=world_time_str,
                        character_input=current_scene_context,
                    )
                    if logs_enabled():
                        print(f"[trace] armed override for {character_name}; pausing for human input")
                    return executed_any  # Pause; the auto-advance loop will break on pending_prompt
            else:
                # ── Normal LLM agent path ───────────────────────────────
                try:
                    char_desc = self.world.get_character_description(character_name)
                except Exception:
                    char_desc = ""

                try:
                    decision_json = run_character_agent(
                        character_name=character_name,
                        character_description=char_desc,
                        current_scene_context=current_scene_context,
                        scene_location=loc_name,
                        world_time=world_time_str,
                        require_decision=False,
                        persist_history=False,
                    )
                except Exception as e:
                    if logs_enabled():
                        print(f"[trace] character agent error for {character_name}: {e}")
                    self._abort_active_scene_turn(reason=f"{character_name}: {e}")
                    raise RuntimeError(
                        f"Character '{character_name}' failed after retries; current turn was cancelled."
                    )

                output_source = "model"
                raw_model_output = str(decision_json or "").strip()

                # Parse output
                try:
                    parsed = json.loads(decision_json)
                except Exception:
                    parsed = {"raw": decision_json}

                if not isinstance(parsed, dict):
                    parsed = {"raw": decision_json}

                decision = parsed.get("decision") if isinstance(parsed.get("decision"), dict) else {}
                action = str((decision or {}).get("intent") or "").strip()
                thoughts = str((decision or {}).get("thoughts") or "").strip()
                gm_answers = parsed.get("gm_answers") if isinstance(parsed.get("gm_answers"), list) else []
                gm_answers = [str(x).strip() for x in gm_answers if str(x).strip()]
                thoughts_text = str(parsed.get("thoughts_text") or "").strip()
                if thoughts_text:
                    thoughts = thoughts_text


            # ── Mark acted (common path) ────────────────────────────────
            try:
                gm_answers_text = ""
                if isinstance(gm_answers, list) and gm_answers:
                    clipped = []
                    for a in gm_answers:
                        s = str(a or "").strip()
                        if not s:
                            continue
                        clipped.append(s)
                    if clipped:
                        gm_answers_text = "\n\n".join(clipped)

                scene_obj.mark_character_acted(
                    character_name,
                    last_decision=action,
                    last_thoughts=thoughts,
                    last_gm_answers=gm_answers_text,
                    character_input=current_scene_context,
                    output_source=output_source,
                )
                executed_any = True

                if logs_enabled():
                    print(f"[trace] {character_name} acted: {action}")

                # If all characters acted, signal context change
                if scene_obj.all_characters_ended():
                    signal_context_changed()
                    if logs_enabled():
                        print("[trace] all characters acted; context changed")
                    break

                # Refresh scene so subsequent characters see prior actions.
                try:
                    scene = self.world.get_scene()
                except Exception:
                    pass

            except Exception as e:
                if logs_enabled():
                    print(f"[trace] failed to mark {character_name} as acted: {e}")
                self._abort_active_scene_turn(reason=f"mark_character_acted failed for {character_name}: {e}")
                raise RuntimeError(
                    f"Character '{character_name}' could not be marked as acted; current turn was cancelled."
                )

        return executed_any

    def _append_gui_stream_character_input(self, *, character_name: str, body: str) -> None:
        """Append character input payload to stream log for WebUI inspection."""
        text = str(body or "").strip()
        if not text:
            text = "(empty)"
        clip_limit = 6000
        if len(text) > clip_limit:
            text = text[:clip_limit].rstrip() + "..."

        ts = datetime.now(timezone.utc).isoformat()
        payload = f"\n[character:{character_name}:current_scene_context] {ts}\n{text}\n"
        try:
            p = stream_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with _GUI_STREAM_INPUT_LOCK:
                with p.open("a", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
        except Exception:
            pass

    def _append_gui_stream_scene_pick(
        self,
        *,
        selected_location: str,
        selected_characters: List[str],
        selected_npcs: List[str],
        time_shift: str,
    ) -> None:
        """Append SCENE_PICK order details to stream log for WebUI inspection."""
        loc = str(selected_location or "").strip() or "(empty)"
        chars = [str(x).strip() for x in (selected_characters or []) if str(x).strip()]
        npcs = [str(x).strip() for x in (selected_npcs or []) if str(x).strip()]
        shift = str(time_shift or "").strip() or "0"

        ts = datetime.now(timezone.utc).isoformat()
        payload = (
            f"\n[scene_pick] {ts}\n"
            f"location: {loc}\n"
            f"character_order: {', '.join(chars) if chars else '(none)'}\n"
            f"npcs: {', '.join(npcs) if npcs else '(none)'}\n"
            f"time_shift: {shift}\n"
        )
        try:
            p = stream_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with _GUI_STREAM_INPUT_LOCK:
                with p.open("a", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
        except Exception:
            pass

    def _collect_recent_gm_outputs(self, *, max_messages: int = 3, max_chars: int = 2200) -> List[str]:
        """Collect recent GM assistant outputs to seed character perception context."""
        snippets: List[str] = []
        try:
            rows = load_full_gm_messages(self.gm_master_history_path)
        except Exception:
            rows = []

        if not isinstance(rows, list):
            rows = []

        for row in reversed(rows):
            if len(snippets) >= max(1, int(max_messages)):
                break
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or row.get("type") or "").strip().lower()
            if role != "assistant":
                continue
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            snippets.append(content)

        if not snippets:
            return []

        snippets.reverse()
        remaining = max(200, int(max_chars))
        out: List[str] = []
        for text in snippets:
            t = text
            if len(t) > 900:
                t = t[:900].rstrip() + "..."
            item = t.strip()
            if len(item) > remaining:
                if remaining <= 3:
                    break
                item = item[: remaining - 3].rstrip() + "..."
            if item:
                out.append(item)
                remaining -= len(item) + 1
            if remaining <= 0:
                break
        return out

    def _build_character_input(
        self,
        scene: Dict[str, Any],
        character_name: str,
    ) -> str:
        """Build plain current scene context for a character turn."""
        player_descs = scene.get("player_descriptions")
        if isinstance(player_descs, dict) and character_name in player_descs:
            current_scene_description = str(player_descs[character_name] or "").strip()
        else:
            current_scene_description = str(scene.get("scene_description") or "").strip()
        return current_scene_description

    def _finalize_turn_if_ready(self) -> bool:
        try:
            scene = self.world.get_scene()
        except Exception:
            return False

        if not (isinstance(scene, dict) and scene.get("state") == "active"):
            return False

        chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
        if not chars:
            return False

        if not all(bool((entry or {}).get("acted") is True) for entry in chars.values()):
            return False

        initiative = scene.get("initiative_order") if isinstance(scene.get("initiative_order"), list) else []
        if not initiative:
            initiative = list(chars.keys())

        scene_obj = Scene(world=self.world)
        ov_store = OverrideStore(str(self.world.game_root.parent))

        def _build_plans_from_scene(scene_characters: Dict[str, Any]) -> List[Dict[str, str]]:
            _plans: List[Dict[str, str]] = []
            for _name in initiative:
                _entry = scene_characters.get(_name) if isinstance(scene_characters.get(_name), dict) else {}
                _plans.append(
                    {
                        "name": str(_name),
                        "intent": str(_entry.get("last_decision") or ""),
                        "thoughts": str(_entry.get("last_thoughts") or ""),
                        "gm_answers": str(_entry.get("last_gm_answers") or ""),
                    }
                )
            return _plans

        def _build_payload(scene_obj_dict: Dict[str, Any], scene_plans: List[Dict[str, str]]) -> Dict[str, Any]:
            return {
                "location": str(scene_obj_dict.get("location") or "").strip(),
                "scene_description": str(scene_obj_dict.get("scene_description") or "").strip(),
                "turn_start_time": str(scene_obj_dict.get("start_time") or "").strip(),
                "character_plans": scene_plans,
                "npcs": scene_obj_dict.get("npcs") if isinstance(scene_obj_dict.get("npcs"), list) else [],
            }

        plans = _build_plans_from_scene(chars)
        round_history: List[Dict[str, Any]] = []
        round_counter = 0

        payload = _build_payload(scene, plans)

        self._ensure_gm_bootstrap()
        narration = ""
        narrations: Dict[str, str] = {}
        turn_duration = ""
        MAX_CORRECTION_ROUNDS = 5
        correction_counts: Dict[str, int] = {}

        while True:
            round_counter += 1
            round_history.append(
                {
                    "type": "plans",
                    "round": round_counter,
                    "character_plans": [dict(p) for p in plans],
                }
            )
            payload["turn_round_history"] = list(round_history)

            # GM knows world state from its persistent history; payload has all scene details.
            gm_result = self._game_master.run_turn_narration(
                payload=payload,
                context_text=self._read_world_facts_context(),
            )

            result_type = str(gm_result.get("type") or "").strip().lower()
            if result_type == "correction":
                corrected_name = str(gm_result.get("character_name") or "").strip()
                turn_insight = str(gm_result.get("turn_insight") or "").strip()
                if not corrected_name or corrected_name not in chars:
                    print(f"Error: Game Master requested correction for unknown character: {corrected_name}")
                    return False
                if not turn_insight:
                    print("Error: Game Master correction has empty turn_insight")
                    return False

                # Per-character correction limit
                correction_counts[corrected_name] = correction_counts.get(corrected_name, 0) + 1
                current_count = correction_counts[corrected_name]

                round_history.append(
                    {
                        "type": "correction",
                        "round": round_counter,
                        "character_name": corrected_name,
                        "turn_insight": turn_insight,
                    }
                )

                if current_count >= MAX_CORRECTION_ROUNDS:
                    # Hard safety: if GM still corrects after final_decision was already applied,
                    # abort the turn — prevents infinite loops.
                    if current_count >= MAX_CORRECTION_ROUNDS + 2:
                        print(
                            f"Error: {corrected_name} already exhausted corrections; "
                            f"GM issued correction #{current_count}. "
                            "Aborting turn to prevent infinite correction loop."
                        )
                        return False

                    # Character exhausted all corrections — GM's word is final.
                    # Mark exhausted in scene, set GM's ruling as the decision.
                    if logs_enabled():
                        print(f"[trace] {corrected_name} exhausted {MAX_CORRECTION_ROUNDS} corrections; applying GM final ruling")

                    try:
                        scene = self.world.get_scene()
                        chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
                        entry = chars.get(corrected_name) if isinstance(chars.get(corrected_name), dict) else {}
                        entry["corrections_exhausted"] = True
                        entry["last_decision"] = f"[gm_final_decision] {turn_insight}"
                        entry["last_thoughts"] = ""
                        chars[corrected_name] = entry
                        scene["characters"] = chars
                        self.world.set_scene(scene)
                    except Exception:
                        pass

                    round_history.append({
                        "type": "final_decision",
                        "round": round_counter,
                        "character_name": corrected_name,
                        "gm_final_ruling": turn_insight,
                    })

                    try:
                        scene = self.world.get_scene()
                    except Exception:
                        return False
                    chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
                    plans = _build_plans_from_scene(chars)
                    payload = _build_payload(scene, plans)
                    continue

                entry = chars.get(corrected_name) if isinstance(chars.get(corrected_name), dict) else {}
                loc_name = str(scene.get("location") or "").strip()
                world_time_str = str(scene.get("start_time") or "") or self.world.get_world_time().to_string()
                prev_intent = str(entry.get("last_decision") or "").strip()
                current_scene_context = str(entry.get("character_input") or "").strip()
                if not current_scene_context:
                    current_scene_context = self._build_character_input(scene, corrected_name)

                armed_name = ov_store.armed_character()
                if armed_name and armed_name == corrected_name:
                    pending_dec = ov_store.get_pending_decision()
                    if pending_dec and pending_dec.character_name == corrected_name:
                        action = str(pending_dec.intent or "").strip()
                        thoughts = str(pending_dec.thoughts or "").strip()
                        try:
                            scene_obj.update_character_intent(
                                corrected_name,
                                last_decision=action,
                                last_thoughts=thoughts,
                                last_gm_answers="",
                                output_source="human_override",
                            )
                            ov_store.consume_pending_decision()
                        except Exception as e:
                            print(f"Error: failed to apply human correction decision for {corrected_name}: {e}")
                            return False

                        try:
                            scene = self.world.get_scene()
                        except Exception:
                            return False
                        chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
                        plans = _build_plans_from_scene(chars)
                        payload = _build_payload(scene, plans)
                        round_history.append(
                            {
                                "type": "replan",
                                "round": round_counter,
                                "character_name": corrected_name,
                                "intent": action,
                                "thoughts": thoughts,
                            }
                        )
                        continue

                    ov_store.set_pending_prompt(
                        character_name=corrected_name,
                        scene_location=loc_name,
                        world_time=world_time_str,
                        character_input=current_scene_context,
                        gm_reality_notice=turn_insight,
                        current_intent=prev_intent,
                    )
                    return False

                try:
                    char_desc = self.world.get_character_description(corrected_name)
                except Exception:
                    char_desc = ""

                try:
                    decision_json = run_character_agent(
                        character_name=corrected_name,
                        character_description=char_desc,
                        current_scene_context=current_scene_context,
                        scene_location=loc_name,
                        world_time=world_time_str,
                        gm_reality_notice=turn_insight,
                        previous_intent=prev_intent,
                        require_decision=False,
                        persist_history=False,
                    )
                except Exception as e:
                    if logs_enabled():
                        print(f"[trace] correction re-plan error for {corrected_name}: {e}")
                    return False

                try:
                    parsed = json.loads(decision_json)
                except Exception:
                    parsed = {"raw": decision_json}

                if not isinstance(parsed, dict):
                    parsed = {"raw": decision_json}

                decision = parsed.get("decision") if isinstance(parsed.get("decision"), dict) else {}
                action = str((decision or {}).get("intent") or "").strip()
                thoughts = str((decision or {}).get("thoughts") or "").strip()
                gm_answers = parsed.get("gm_answers") if isinstance(parsed.get("gm_answers"), list) else []
                gm_answers = [str(x).strip() for x in gm_answers if str(x).strip()]
                thoughts_text = str(parsed.get("thoughts_text") or "").strip()
                if thoughts_text:
                    thoughts = thoughts_text

                gm_answers_text = ""
                if gm_answers:
                    gm_answers_text = "\n\n".join(gm_answers)

                try:
                    scene_obj.update_character_intent(
                        corrected_name,
                        last_decision=action,
                        last_thoughts=thoughts,
                        last_gm_answers=gm_answers_text,
                        output_source="model",
                    )
                except Exception as e:
                    print(f"Error: failed to update corrected intent for {corrected_name}: {e}")
                    return False

                try:
                    scene = self.world.get_scene()
                except Exception:
                    return False
                chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
                plans = _build_plans_from_scene(chars)
                payload = _build_payload(scene, plans)
                round_history.append(
                    {
                        "type": "replan",
                        "round": round_counter,
                        "character_name": corrected_name,
                        "intent": action,
                        "thoughts": thoughts,
                    }
                )
                continue

            narration = str(gm_result.get("narration") or "").strip()
            narrations = gm_result.get("narrations") if isinstance(gm_result.get("narrations"), dict) else {}
            turn_duration = str(gm_result.get("duration") or "").strip()
            _new_world_facts = str(gm_result.get("world_facts") or "").strip()
            if _new_world_facts:
                self._write_world_facts(_new_world_facts)

            if not narration:
                print("Error: Game Master did not provide turn narration")
                return False

            if not turn_duration:
                print("Error: Game Master did not provide turn duration")
                return False

            break

        # Persist legacy combined turn text: Scene description + Actions + GM narration.
        scene_description_for_turn = str(payload.get("scene_description") or "").strip()
        actions_lines: List[str] = []
        for p in plans:
            nm = str(p.get("name") or "").strip()
            act = str(p.get("intent") or "").strip()
            if nm and act:
                actions_lines.append(f"{nm}: {act}")

        combined_parts: List[str] = []
        if scene_description_for_turn:
            combined_parts.append("Scene description:\n" + scene_description_for_turn)
        if actions_lines:
            combined_parts.append("Actions:\n" + "\n".join(actions_lines))
        if narration:
            combined_parts.append("Outcome:\n" + narration)

        combined_narration = "\n\n".join([x for x in combined_parts if str(x).strip()]).strip()
        if not combined_narration:
            combined_narration = narration

        # Finalize the turn with the Game Master's narration
        try:
            result = scene_obj.end_with_gm_output(
                narration=combined_narration,
                location=payload["location"],
                turn_duration=turn_duration,
            )
            if logs_enabled():
                print(
                    f"[trace] turn finalized with narration ({len(combined_narration)} chars, duration={turn_duration})"
                )
        except Exception as e:  # noqa: BLE001
            print(f"Error: turn finalization failed: {e}")
            return False

        # Share the final narration with all character participants as a GM message.
        try:
            from memory_store import append_message, limits_from_env, load_history
            from character.memory import update_turn_memory

            limits = limits_from_env()
            workspace_root = Path(__file__).resolve().parent
            game_root = (workspace_root / "game").resolve()

            turn_world_time = str(scene.get("start_time") or "") or self.world.get_world_time().to_string()
            scene_location = str(scene.get("location") or "").strip()

            def _last_user_message_text(path: Path) -> str:
                try:
                    hist = load_history(path)
                except Exception:
                    return ""
                if not isinstance(hist, list):
                    return ""
                for row in reversed(hist):
                    if not isinstance(row, dict):
                        continue
                    role = str(row.get("role") or "").strip().lower()
                    if role == "user":
                        return str(row.get("content") or "").strip()
                return ""

            for name in initiative:
                nm = str(name)
                entry = chars.get(nm) if isinstance(chars.get(nm), dict) else {}
                action = str(entry.get("last_decision") or "")
                thoughts = str(entry.get("last_thoughts") or "")
                gm_answers_text = str(entry.get("last_gm_answers") or "").strip()

                msg_path = (game_root / "characters" / nm / "messages.json").resolve()
                mem_path = (game_root / "characters" / nm / "memory.json").resolve()

                character_input_text = str(entry.get("character_input") or "").strip()
                if not character_input_text:
                    character_input_text = self._build_character_input(scene, nm)
                user_mem = json.dumps(
                    {
                        "scene_location": scene_location,
                        "world_time": turn_world_time,
                        "current_scene_context": character_input_text,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                if user_mem:
                    # If scene context was reused from previous result, user context can
                    # duplicate the latest stored user/outcome content. Skip this repeat.
                    skip_user_mem = False
                    last_user = _last_user_message_text(msg_path)
                    if last_user:
                        if user_mem.strip() == last_user:
                            skip_user_mem = True
                        elif character_input_text and character_input_text.strip() == last_user:
                            skip_user_mem = True

                    if skip_user_mem:
                        if logs_enabled():
                            print(f"[trace] skipped duplicate current_scene_context history for {nm}")
                    else:
                        append_message(msg_path, role="user", content=user_mem, limits=limits)

                assistant_parts: List[str] = []
                if action:
                    assistant_parts.append("Intent:\n" + action)
                assistant_mem = "\n\n".join(assistant_parts).strip()
                if assistant_mem:
                    append_message(msg_path, role="assistant", content=assistant_mem, limits=limits)

                is_override = (str(entry.get("output_source") or "").strip() == "human_override")
                ts_thoughts = f"[{turn_world_time}] {thoughts}" if (turn_world_time and thoughts.strip()) else thoughts
                update_turn_memory(
                    mem_path,
                    character_name=nm,
                    world_time=turn_world_time,
                    scene_location=scene_location,
                    thoughts_to_add=[ts_thoughts] if thoughts.strip() else [],
                    outcome={
                        "intent": action,
                        "thoughts": thoughts,
                    },
                    is_override=is_override,
                )

            # Character history: per-player personalized narration.
            # Falls back to the combined narration if a per-player entry is missing.
            header_parts = []
            if turn_world_time:
                header_parts.append(f"time: {turn_world_time}")
            if turn_duration:
                header_parts.append(f"duration: {turn_duration}")
            header = "\n".join(header_parts)
            for name in initiative:
                nm = str(name)
                player_narration = str(narrations.get(nm) or narration).strip()
                char_narration = ("Events:\n" + player_narration).strip() if player_narration else ""
                if header and char_narration:
                    turn_output_with_duration = f"{header}\n\n{char_narration}"
                elif header:
                    turn_output_with_duration = header
                else:
                    turn_output_with_duration = char_narration
                msg_path = (game_root / "characters" / nm / "messages.json").resolve()
                append_message(msg_path, role="user", content=turn_output_with_duration, limits=limits)
        except Exception:
            pass

        # Inject a structured post-turn message into SA history.
        # From the SA's perspective it called run_scene and now receives
        # the turn outcome so it knows exactly what to persist.
        try:
            # Write turn recap to a dedicated file for the WebUI to read.
            # (We do NOT inject into SA messages — an orphaned ToolMessage
            # would confuse the SA's LLM and cause infinite loops.)
            _append_turn_recap(self.world.game_root, result)

            # Hidden per-turn snapshot for storyline backup/check-in UI.
            try:
                recap = result.get("scene_recap") if isinstance(result.get("scene_recap"), dict) else {}
                record_turn_snapshot(
                    Path(__file__).resolve().parent,
                    start_time=str(recap.get("start_time") or result.get("start_time") or ""),
                    end_time=str(result.get("new_time") or ""),
                    location=str(result.get("location") or ""),
                )
            except Exception:
                pass

            new_time = str(result.get("new_time") or "")
            char_names = [str(p.get("name") or "") for p in plans if str(p.get("name") or "").strip()]
            action_summary = "; ".join(
                f"{p['name']}: {p['intent']}" for p in plans
                if str(p.get("name") or "").strip() and str(p.get("intent") or "").strip()
            )
            thought_lines: List[str] = []
            for p in plans:
                nm = str(p.get("name") or "").strip()
                th = str(p.get("thoughts") or "").strip()
                if not nm or not th:
                    continue
                thought_lines.append(f"{nm}: {th}")
            thought_summary = "\n".join(thought_lines)
            notes_block = (
                f"\nCharacter notes (may include GM Q&A):\n{thought_summary}"
                if thought_summary
                else ""
            )
            gm_answer_lines: List[str] = []
            for p in plans:
                nm = str(p.get("name") or "").strip()
                qa = str(p.get("gm_answers") or "").strip()
                if not nm or not qa:
                    continue
                gm_answer_lines.append(f"{nm}:\n{qa}")

            msgs = list(self.state.get("messages") or [])
            if gm_answer_lines:
                gm_answers_msg = HumanMessage(content=(
                    "[gm_answers] Character answers from Game Master this turn:\n"
                    + "\n\n".join(gm_answer_lines)
                ))
                msgs.append(gm_answers_msg)

            # Build storage-oversize notices for character description files.
            _storage_notices: List[str] = []
            try:
                _TARGET_KB = 10
                _TARGET_BYTES = _TARGET_KB * 1024
                for _char_name in self.world.list_character_names():
                    _desc_path = self.world.game_root / "characters" / _char_name / "description.json"
                    try:
                        _sz = _desc_path.stat().st_size
                        if _sz > _TARGET_BYTES:
                            _sz_kb = (_sz + 1023) // 1024
                            _storage_notices.append(
                                f"[storage_notice] Character '{_char_name}' description.json is {_sz_kb}KB"
                                f" (limit {_TARGET_KB}KB). PRIORITY: delete stale/irrelevant fields from"
                                f" this file before running run_scene. Use delete_character_path to remove"
                                f" obsolete keys (old location intel, spent observations, resolved sub-plots)."
                            )
                    except Exception:
                        pass
            except Exception:
                pass

            _notices_prefix = ("\n".join(_storage_notices) + "\n\n") if _storage_notices else ""
            turn_msg = HumanMessage(content=(
                _notices_prefix
                + f"[turn_complete] Scene at '{payload['location']}' finished."
                f" Time: {new_time}. Duration: {turn_duration}."
                f"\nCharacters: {', '.join(char_names)}."
                f"\nActions: {action_summary}"
                f"{notes_block}"
                f"\nNarration:\n{narration}"
            ))
            msgs.append(turn_msg)
            self.state = {"messages": msgs}
            self.save_gm_history()
        except Exception:
            pass

        # One-turn override ends only when the whole turn is finalized.
        try:
            if ov_store.armed_character():
                ov_store.disarm()
        except Exception:
            pass

        return True

    def _scene_all_characters_ended(self) -> bool:
        try:
            scene = self.world.get_scene()
            if not (isinstance(scene, dict) and scene.get("state") == "active"):
                return False
            chars = scene.get("characters")
            if not isinstance(chars, dict) or not chars:
                return False
            return all(bool((entry or {}).get("acted") is True) for entry in chars.values())
        except Exception:
            return False

    def _last_tool_call_index(self, tool_name: str) -> int:
        msgs = list(self.state.get("messages") or [])
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            try:
                if getattr(m, "type", "") != "tool":
                    continue
                if str(getattr(m, "name", "") or "").strip() == str(tool_name or "").strip():
                    return i
            except Exception:
                continue
        return -1

    def _auto_advance_until_turn_finalized(
        self,
        *,
        before_time_s: int,
        max_steps: Optional[int] = None,
        before_snapshot: Optional[Tuple[int, bool, int, int, int]] = None,
    ) -> None:
        # Prevent the console from hanging forever if the model never finalizes a turn.
        # This can happen when a tool call keeps failing or the model loops.
        if max_steps is None:
            raw = (os.getenv("LLM_WORLD_AUTO_ADVANCE_MAX_STEPS") or "").strip()
            if raw:
                try:
                    max_steps = int(raw)
                except Exception:
                    max_steps = 200
            else:
                max_steps = 200
        try:
            max_steps_i = int(max_steps)
        except Exception:
            max_steps_i = 200
        max_steps_i = max(1, min(max_steps_i, 5000))

        # If repeated ticks cause no observable world progress, stop early with diagnostics.
        # This prevents /continue from feeling "hung" when the model outputs non-tool chatter
        # or loops without advancing time or finalizing a turn.
        raw_no_progress = (os.getenv("LLM_WORLD_NO_PROGRESS_MAX_STEPS") or "").strip()
        if raw_no_progress:
            try:
                no_progress_max = int(raw_no_progress)
            except Exception:
                no_progress_max = 30
        else:
            no_progress_max = 30
        no_progress_max = max(3, min(int(no_progress_max), 5000))

        # Determine what "finalized" means for this waiting loop.
        # We consider a turn finalized when:
        # - the scene is no longer active (scene cleared)
        # - story progressed (a turn appended and/or a paragraph summarized)
        #
        # Note: world time may legitimately stay unchanged when resolving a
        # lagging character timeline that ends before/at current global time.
        if before_snapshot is None:
            try:
                before_snapshot = self._progress_snapshot()
            except Exception:
                before_snapshot = (int(before_time_s), self._scene_is_active(), 0, 0, 0)

        (_t0, _scene0, turns0, paras0, _locs0) = before_snapshot

        def _is_finalized() -> bool:
            try:
                snap = self._progress_snapshot()
                _time_s, scene_active, turns, paras, _locs = snap
                if bool(scene_active):
                    return False
                # "turns" can reset to 0 when a paragraph is summarized; count either.
                if int(turns) > int(turns0) or int(paras) > int(paras0):
                    return True
                return False
            except Exception:
                return False

        last_progress = before_snapshot
        no_progress_steps = 0

        steps = 0
        while True:
            self.world.ensure_initialized()
            # Web UI one-turn override handshake: if a prompt is pending, pause
            # auto-advance so the user can submit the character decision.
            try:
                ov_path = (self.world.game_root / "user_inputs" / "override_state.json").resolve()
                if ov_path.exists():
                    raw_ov = ov_path.read_text(encoding="utf-8")
                    ov = json.loads(raw_ov)
                    if isinstance(ov, dict) and ov.get("pending_prompt") is not None:
                        break
            except Exception:
                pass
            if _is_finalized():
                # Before breaking, let the SA process the [turn_complete]
                # message to persist facts from the narration.  The SA gets
                # one invocation with full maintenance tools (no scene exists).
                # It may then request the next scene via run_scene when ready.
                if self._has_unprocessed_turn_complete():
                    if logs_enabled():
                        print("[trace] auto-advance: turn finalized but SA has unprocessed [turn_complete]; running SA once")
                    self.invoke_once(None)
                # One scene = one /continue click.  Always stop here.
                if logs_enabled():
                    print("[trace] auto-advance: scene complete; stopping")
                break
            steps += 1
            if logs_enabled():
                print(f"[trace] auto-advance: step {steps}, calling invoke_once")
            if steps > max_steps_i:
                raise RuntimeError(
                    "Auto-advance exceeded max steps without finalizing a turn. "
                    "This usually means the GM is stuck in a tool-error loop. "
                    "Try again, or enable tracing and inspect logs/stream.txt."
                )
            self.invoke_once(None)
            if logs_enabled():
                print(f"[trace] auto-advance: invoke_once returned; checking progress")

            # Auto-execute characters if there's an active scene
            char_executed = self._auto_execute_characters_in_scene()
            if char_executed and logs_enabled():
                print("[trace] auto-advance: executed character agents")

            # If all characters completed planning, finalize turn with Game Master
            if self._finalize_turn_if_ready():
                if logs_enabled():
                    print("[trace] auto-advance: turn finalized with Game Master narration")
                # Let the SA persist facts before breaking.
                if self._has_unprocessed_turn_complete():
                    if logs_enabled():
                        print("[trace] auto-advance: running SA to process [turn_complete]")
                    self.invoke_once(None)
                # One scene = one /continue click.  Always stop here.
                if logs_enabled():
                    print("[trace] auto-advance: scene complete; stopping")
                break

            # Detect a stall: no progress snapshot changes for N ticks.
            snap = self._progress_snapshot()
            if snap == last_progress:
                no_progress_steps += 1
            else:
                no_progress_steps = 0
                last_progress = snap

            if no_progress_steps >= no_progress_max:
                # Build a compact diagnostic payload for the user.
                last_ai = ""
                last_tool = ""
                last_tool_name = ""
                for m in reversed(self.state.get("messages") or []):
                    t = getattr(m, "type", "")
                    if not last_ai and t in {"ai", "assistant"}:
                        last_ai = str(getattr(m, "content", "") or "").strip()
                    if not last_tool and t == "tool":
                        last_tool = str(getattr(m, "content", "") or "").strip()
                        last_tool_name = str(getattr(m, "name", "") or "").strip()
                    if last_ai and last_tool:
                        break

                def _clip(s: str, n: int = 400) -> str:
                    s = (s or "").strip()
                    if len(s) <= n:
                        return s
                    return s[:n].rstrip() + " …"

                raise RuntimeError(
                    "Auto-advance made no world progress for too many ticks. "
                    "This usually means the GM is not calling tools (or is stuck repeating). "
                    f"Last assistant text: {_clip(last_ai) or '[none]'} | "
                    f"Last tool: {last_tool_name or '[none]'} {_clip(last_tool) or ''}"
                )
            if logs_enabled() and steps % 10 == 0:
                print(f"[trace] auto-advancing... steps={steps}")

    def _load_session_state(self, *, show_history_stats: bool) -> None:
        self.load_gm_history()
        self.maybe_inject_initial_character_cards()

        if show_history_stats and logs_enabled() and list(self.state.get("messages") or []):
            msgs = list(self.state.get("messages") or [])
            approx_tokens = 0
            for m in msgs:
                approx_tokens += approx_token_count(str(getattr(m, "content", "") or ""))

            max_hist = int(self.limits.max_history_tokens)
            max_ctx = int(self.limits.model_max_context_tokens)
            pct_hist = int(round((approx_tokens / max_hist) * 100.0)) if max_hist > 0 else 0

            print(
                f"[trace] storage assistant history loaded (msgs={len(msgs)}) "
                f"| hist~{approx_tokens}/{max_hist} tok (~{pct_hist}%) "
                f"| model_ctx~{max_ctx} tok"
            )

    def _maybe_bootstrap_plot_and_autoadvance(self) -> None:
        """If init/plot.json is injected, auto-advance until a turn finalizes, then print output."""

        before_time_s = self.world.get_world_time().to_seconds()
        self._init_plot_injected_this_run = False

        plot_err = self.maybe_inject_init_plot_as_first_user_command()
        if plot_err:
            print(f"Startup error: {plot_err}")
            print("Tip: fix init/plot.json or delete it to skip plot bootstrapping.")
            return

        if not self._init_plot_injected_this_run:
            return

        try:
            before_snapshot = self._progress_snapshot()
            self._auto_advance_until_turn_finalized(
                before_time_s=int(before_time_s),
                max_steps=None,
                before_snapshot=before_snapshot,
            )
        except KeyboardInterrupt:
            print("\n(interrupted; auto-advance stopped)")

        after_time_s = self.world.get_world_time().to_seconds()
        turn_finalized = after_time_s > before_time_s
        # Always print something after an injected first command so users see the game start.
        self._print_latest_output(is_continue=True, turn_finalized=turn_finalized)

    def _handle_stream_command(self, low: str) -> bool:
        if not low.startswith("/stream"):
            return False

        parts = low.split()
        if len(parts) >= 2 and parts[1] in {"on", "1", "true"}:
            self.stream_echo = True
        elif len(parts) >= 2 and parts[1] in {"off", "0", "false"}:
            self.stream_echo = False
        else:
            self.stream_echo = not self.stream_echo

        self._apply_stream_env()
        mode = "ON (echoing to this console)" if self.stream_echo else "OFF (tail logs/stream.txt)"
        print(f"(stream {mode})")
        return True

    def _clear_turn_temp_dir(self) -> None:
        """Clear per-turn temporary workspace (best-effort)."""
        try:
            tmp_dir = (self.world.game_root / "tmp_turn").resolve()
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _run_pending_reflections(self) -> None:
        """Run self-reflection for any characters that are due.

        Called at the START of a turn (before SA runs) so it never blocks
        turn finalisation.  Failures are logged but never propagate.

        Override turns are skipped: if a character is currently armed for
        human override their turn will be driven by a player, so AI
        reflection is suppressed.  The staleness counter is unaffected, so
        reflection fires normally on the first subsequent agent turn.
        """
        try:
            scene = self.world.get_scene()
            if isinstance(scene, dict) and str(scene.get("state") or "").strip() == "active":
                return
        except Exception:
            pass

        # Find which character (if any) is armed for human override this turn.
        # Skipping reflection on override turns is disabled by default
        # (set SKIP_REFLECTION_ON_OVERRIDE=1 to re-enable).
        skip_reflection_on_override = os.getenv("SKIP_REFLECTION_ON_OVERRIDE", "0").strip() in ("1", "true", "yes")
        armed_name: Optional[str] = None
        if skip_reflection_on_override:
            try:
                ov_store = OverrideStore(str(self.world.game_root.parent))
                armed_name = ov_store.armed_character()
            except Exception:
                pass

        try:
            char_names = self.world.list_character_names()
        except Exception:
            return
        for cname in char_names:
            try:
                if armed_name and armed_name == cname:
                    if logs_enabled():
                        print(f"[trace] skipping reflection for {cname}: armed for human override")
                    continue
                if needs_reflection(cname):
                    with self._reflection_jobs_lock:
                        if cname in self._reflection_jobs:
                            continue
                        self._reflection_jobs.add(cname)

                    if logs_enabled():
                        print(f"[trace] {cname} is due for self-reflection (queued pre-turn)")

                    def _job(name: str) -> None:
                        try:
                            try:
                                char_desc = self.world.get_character_description(name)
                            except Exception:
                                char_desc = {}
                            run_reflection(character_name=name, character_description=char_desc)
                        except Exception as e:  # noqa: BLE001
                            if logs_enabled():
                                print(f"[trace] reflection run failed for {name}: {e}")
                        finally:
                            with self._reflection_jobs_lock:
                                self._reflection_jobs.discard(name)

                    self._reflection_executor.submit(_job, cname)
            except Exception as e:
                if logs_enabled():
                    print(f"[trace] reflection check/run failed for {cname}: {e}")

    def _run_gm_interaction(self, *, user_text: str, debug_trace: bool) -> None:
        raw = (user_text or "").strip()
        parts = raw.split()
        low0 = parts[0].lower() if parts else ""

        is_continue = low0 == "/continue"
        continue_turns = 1
        if is_continue:
            if len(parts) >= 2:
                try:
                    continue_turns = int(parts[1])
                except Exception:
                    print("Error: /continue expects an optional integer, e.g. /continue 3")
                    return
            if continue_turns <= 0:
                print("Error: /continue count must be >= 1")
                return
            # Safety guard against accidental runaway loops.
            continue_turns = min(continue_turns, 50)

        # /continue is a console-only command. The GM should not see it, and the console
        # should not stream intermediate model chatter during unattended advancement.
        prev_stream_echo: Optional[bool] = None
        if is_continue:
            prev_stream_echo = bool(self.stream_echo)
            if self.stream_echo:
                self.stream_echo = False
                self._apply_stream_env()

        try:
            for i in range(int(continue_turns)):
                # Run pending reflections at the START of each turn so
                # multi-turn /continue and paragraph auto-continue both
                # keep checking all characters continuously.
                self._run_pending_reflections()

                self._clear_turn_temp_dir()
                before_time = self.world.get_world_time().to_seconds()
                before_snapshot = self._progress_snapshot()

                try:
                    # First, process the user's intent (or a pure tick for /continue).
                    if not is_continue and i == 0:
                        self.invoke_once(HumanMessage(content=user_text))
                    else:
                        self.invoke_once(None)

                    # Then, keep ticking until a turn finalizes.
                    # This keeps the console from returning to the "You>" prompt mid-turn.
                    self._auto_advance_until_turn_finalized(
                        before_time_s=int(before_time),
                        max_steps=200,
                        before_snapshot=before_snapshot,
                    )
                except KeyboardInterrupt:
                    # Allow the user to regain control if the model never finalizes.
                    print("\n(interrupted; auto-advance stopped)")
                    return
                except Exception as e:  # noqa: BLE001
                    print(f"Error: {e}")
                    return

                after_time = self.world.get_world_time().to_seconds()
                # Keep this aligned with _auto_advance_until_turn_finalized.
                after_snap = self._progress_snapshot()
                turn_finalized = self._did_story_progress(
                    before_snapshot=before_snapshot,
                    after_snapshot=after_snap,
                )
                self._print_latest_output(is_continue=is_continue, turn_finalized=turn_finalized)

                if debug_trace and logs_enabled() and turn_finalized:
                    print(self.gm_memory_usage_line())

                # If the model didn't advance time / finalize a turn, don't spin further.
                if not turn_finalized:
                    break
        finally:
            if prev_stream_echo is not None and bool(self.stream_echo) != bool(prev_stream_echo):
                self.stream_echo = bool(prev_stream_echo)
                self._apply_stream_env()

    def _dispatch_user_input(self, *, user_text: str, debug_trace: bool) -> bool:
        """Handle a single console input line.

        Returns True if the main loop should exit.
        """

        low = user_text.lower()
        if low in {"/exit", "/quit"}:
            return True

        if self._handle_stream_command(low):
            return False

        # Backup/restore are console-only commands (never shown to the GM).
        if low.startswith("/backup"):
            parts = user_text.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("Error: /backup expects a name, e.g. /backup my_save")
                return False
            name = parts[1].strip()
            try:
                self.backup_game(name)
                print(f"(backup saved: backups/{self._backup_slug(name)})")
            except Exception as e:  # noqa: BLE001
                print(f"Error: {e}")
            return False

        if low.startswith("/restore"):
            parts = user_text.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("Error: /restore expects a name, e.g. /restore my_save")
                return False
            name = parts[1].strip()
            try:
                self.restore_game(name)
                print(f"(restored backup: backups/{self._backup_slug(name)})")

                # Mirror startup behavior after a restore.
                self._load_session_state(show_history_stats=False)
                self._maybe_bootstrap_plot_and_autoadvance()
            except Exception as e:  # noqa: BLE001
                print(f"Error: {e}")
            return False

        if low == "/reset":
            self.reset_game()
            print("(game reset: deleted game/ and reinitialized from init/)")

            # Mirror startup behavior after a hard reset.
            self._load_session_state(show_history_stats=False)
            self._maybe_bootstrap_plot_and_autoadvance()
            return False

        if low == "/reset_chat":
            self.reset_conversation()
            print("(conversation reset: cleared storage assistant history + bootstrap flags)")
            return False

        if low.startswith("/ask_gm"):
            question = user_text[len("/ask_gm"):].strip()
            if not question:
                print("Usage: /ask_gm <your question>")
                return False
            try:
                from gm.game_master import build_game_master_qa_context
                ctx = build_game_master_qa_context(self.world)
                answer = self._game_master.ask_ephemeral(question=question, context_text=ctx)
                print(f"\n[GM] {answer}\n")
            except Exception as e:  # noqa: BLE001
                print(f"Error asking GM: {e}")
            return False

        if low.startswith("/rag"):
            raw = user_text[len("/rag"):].strip()
            force_reindex = False
            if raw.startswith("--reindex"):
                force_reindex = True
                raw = raw[len("--reindex"):].strip()

            if not raw:
                print("Usage: /rag [--reindex] <question>")
                return False

            try:
                from rag_poc import StoryEmbeddingSearch

                if self._rag_search is None:
                    self._rag_search = StoryEmbeddingSearch(Path(__file__).resolve().parent)

                out = self._rag_search.search(raw, top_k=6, force_rebuild=force_reindex)
                gpu = out.get("gpu") if isinstance(out.get("gpu"), dict) else {}
                idx = out.get("index") if isinstance(out.get("index"), dict) else {}
                timing = out.get("timings_s") if isinstance(out.get("timings_s"), dict) else {}
                hits = out.get("hits") if isinstance(out.get("hits"), list) else []

                print(
                    "\n[RAG] GPU check: "
                    f"cuda={gpu.get('cuda')} | device={gpu.get('device_name')} | "
                    f"expected='{gpu.get('expected_substr')}' | match={gpu.get('expected_match')}"
                )
                print(
                    "[RAG] Index: "
                    f"rebuilt={idx.get('rebuilt')} | chunks={idx.get('chunks')} | "
                    f"timings(s)={timing}"
                )

                if not hits:
                    print("[RAG] No hits found.\n")
                    return False

                for row in hits:
                    rank = int(row.get("rank") or 0)
                    score = float(row.get("score") or 0.0)
                    source = str(row.get("source") or "story")
                    text = str(row.get("text") or "").strip().replace("\n", " ")
                    if len(text) > 420:
                        text = text[:420].rstrip() + " ..."
                    print(f"\n#{rank} score={score:.4f} | {source}\n{text}")
                print()
            except Exception as e:  # noqa: BLE001
                print(f"Error running /rag: {e}")
            return False

        self._run_gm_interaction(user_text=user_text, debug_trace=debug_trace)
        return False

    @staticmethod
    def _backup_slug(name: str) -> str:
        """Normalize user-provided backup names to a safe folder name."""

        raw = (name or "").strip()
        if not raw:
            raise ValueError("Backup name is required")

        out = []
        for ch in raw:
            if ch.isalnum() or ch in {"-", "_"}:
                out.append(ch)
            elif ch.isspace():
                out.append("_")
            # drop everything else (slashes, dots, etc.)
        slug = "".join(out).strip("_")
        if not slug:
            raise ValueError("Backup name must contain letters/numbers")
        return slug[:64]

    def backup_game(self, name: str) -> None:
        """Save a snapshot of the entire game/ folder under backups/<name>/game/."""

        workspace = Path(__file__).resolve().parent
        game_root = (workspace / "game").resolve()
        if not game_root.exists():
            raise RuntimeError("game/ folder does not exist")

        slug = self._backup_slug(name)
        backups_root = (workspace / "backups").resolve()
        dest_root = (backups_root / slug).resolve()
        dest_game = (dest_root / "game").resolve()

        # Safety: ensure dest_root stays under backups_root.
        if backups_root not in dest_game.parents:
            raise RuntimeError("Invalid backup destination")

        if dest_root.exists():
            raise RuntimeError(f"Backup already exists: backups/{slug}")

        dest_root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(game_root, dest_game)

        # Small manifest for humans.
        try:
            meta = {
                "name": str(name),
                "slug": slug,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (dest_root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    def restore_game(self, name: str) -> None:
        """Restore game/ folder from backups/<name>/game/."""

        workspace = Path(__file__).resolve().parent
        slug = self._backup_slug(name)
        backups_root = (workspace / "backups").resolve()
        src_root = (backups_root / slug).resolve()
        src_game = (src_root / "game").resolve()

        if not src_game.exists():
            raise RuntimeError(f"Backup not found: backups/{slug}")

        game_root = (workspace / "game").resolve()

        # Remove or rename existing game/ folder.
        try:
            if game_root.exists():
                shutil.rmtree(game_root)
        except Exception:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup = (workspace / f"game_old_{ts}").resolve()
                if game_root.exists() and not backup.exists():
                    game_root.rename(backup)
            except Exception as e:
                raise RuntimeError(f"Failed to replace game/ folder: {e}")

        shutil.copytree(src_game, game_root)

        # Recreate runtime objects so paths/flags are consistent.
        self.world = World(workspace_root=workspace)
        self.world.ensure_initialized()
        self.limits = limits_from_env()
        self.max_turns = gm_max_turns_from_env()
        self.storage_assistant_history_path = (
            self.world.game_root / "storage_assistant_messages.json"
        ).resolve()
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        self.gm_history_preexists = self.storage_assistant_history_path.exists()
        self._gm_factory = StorageAssistantFactory()
        self.state = {"messages": []}

        try:
            reset_turn_lock()
        except Exception:
            pass

    @staticmethod
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
            "parameterinvfunction_calls",
            "invfunction_calls",
            "<tool_call",
            "</tool_call",
            "<function=",
            "</function>",
            "<parameter=",
            # Additional malformed variants seen in logs
            "give_word_to",  # partial tool name in markup context
            "<invoke name=\"give_word",
            "<invoke name=\"start_scene",
            "<invoke name=\"run_scene",
            "<invoke name=\"gm_output",
            "<invoke name=\"create_",
            "<invoke name=\"update_",
        ]
        
        # Check XML-style pseudo markup first
        if any(p in t for p in patterns):
            return True
        
        # Check for JSON-formatted pseudo tool calls
        # Pattern: {"function": "tool_name", "parameters": {...}}
        if '"function"' in t and '"parameters"' in t:
            # Likely JSON tool attempt
            import re
            # Look for {"function": "known_tool_name"
            tool_pattern = r'"function"\s*:\s*"(start_scene|run_scene|create_npc|update_character|create_location|update_location)'
            if re.search(tool_pattern, t, re.IGNORECASE):
                return True
        
        # Check for JSON data dumps that look like tool outputs/summaries
        # Detect ```json blocks anywhere in text, or JSON objects with scene/tool-like fields
        output_indicators = ['"narration"', '"turn_duration"', '"location"', '"characters"', '"state"', '"npcs"', '"acted"', '"_context_notice"']
        count = sum(1 for ind in output_indicators if ind in t)
        
        # If text contains ```json anywhere, it's likely dumping JSON
        if '```json' in t and count >= 2:
            return True
        
        # If text has many scene/tool indicators, it's dumping internal state
        if count >= 3:
            return True
        
        return False

    @staticmethod
    def _is_invalid_gm_text_output(text: str) -> bool:
        """Check if GM text output is invalid (pseudo tool markup or too verbose)."""
        if ConsoleApp._looks_like_pseudo_tool_markup(text):
            return True
        
        # Word count check — SA should communicate via tool calls, not text
        stripped = (text or "").strip()
        if stripped:
            word_count = len(stripped.split())
            if word_count > 50:
                return True
        
        return False

    @staticmethod
    def _is_tool_error_message(text: str) -> bool:
        s = str(text or "").strip().lower()
        if not s:
            return False
        if s.startswith("error:"):
            return True
        return any(
            p in s
            for p in [
                "not available in the current context",
                "context changed during this invocation",
                "is not a valid tool",
                "turn already finalized",
                "please fix your mistakes",
                "not all characters ended",
                "is not valid right now",
            ]
        )

    @staticmethod
    def _is_transient_assistant_error_message(text: str) -> bool:
        s = str(text or "").strip().lower()
        if not s.startswith("error:"):
            return False
        return any(
            p in s
            for p in [
                "connection error",
                "connect",
                "timeout",
                "timed out",
                "refused",
                "unreachable",
                "temporarily unavailable",
                "upstream",
                "provider",
                "bad gateway",
            ]
        )

    @staticmethod
    def _strip_tool_error_pairs(messages: List[Any]) -> List[Any]:
        """Drop stale tool error messages and their corresponding AI tool calls."""

        dropped_tool_call_ids: set[str] = set()
        for m in messages:
            try:
                if getattr(m, "type", "") != "tool":
                    continue
                if not ConsoleApp._is_tool_error_message(getattr(m, "content", "")):
                    continue
                tc_id = str(getattr(m, "tool_call_id", "") or "").strip()
                if tc_id:
                    dropped_tool_call_ids.add(tc_id)
            except Exception:
                continue

        if not dropped_tool_call_ids:
            return list(messages)

        cleaned: List[Any] = []
        for m in messages:
            try:
                t = getattr(m, "type", "")

                if t == "tool":
                    tc_id = str(getattr(m, "tool_call_id", "") or "").strip()
                    if tc_id in dropped_tool_call_ids:
                        continue
                    cleaned.append(m)
                    continue

                if t in {"ai", "assistant"}:
                    content = str(getattr(m, "content", "") or "")
                    tool_calls = getattr(m, "tool_calls", None)
                    if not isinstance(tool_calls, list) or not tool_calls:
                        ak = getattr(m, "additional_kwargs", None) or {}
                        if isinstance(ak, dict):
                            tc2 = ak.get("tool_calls")
                            if isinstance(tc2, list):
                                tool_calls = tc2
                    if not isinstance(tool_calls, list):
                        tool_calls = []

                    if tool_calls:
                        filtered_calls = [
                            tc for tc in tool_calls
                            if str((tc or {}).get("id", "") or "").strip() not in dropped_tool_call_ids
                        ]
                        if filtered_calls != tool_calls:
                            if not filtered_calls and not content.strip():
                                continue
                            m = AIMessage(
                                content=content,
                                tool_calls=filtered_calls,
                                additional_kwargs=getattr(m, "additional_kwargs", {}) or {},
                            )
                    cleaned.append(m)
                    continue

                cleaned.append(m)
            except Exception:
                cleaned.append(m)

        return cleaned

    @staticmethod
    def _extract_pseudo_tool_markup_syntax(text: str) -> Tuple[str, Optional[str]]:
        s = text or ""
        lower = s.lower()

        candidates = [
            lower.find("<function_calls"),
            lower.find("<functioninvoke"),
            lower.find("<invoke"),
        ]
        starts = [i for i in candidates if i >= 0]
        start = min(starts) if starts else -1
        if start < 0:
            return ("<function_calls> ... </function_calls>", None)

        end = -1
        # Prefer capturing a full <function_calls>...</function_calls> block when present.
        end_fc = lower.find("</function_calls>", start)
        if end_fc >= 0:
            end = end_fc + len("</function_calls>")
        else:
            # Fallback: capture through a single invoke block.
            end_inv = lower.find("</invoke>", start)
            if end_inv >= 0:
                end = end_inv + len("</invoke>")

        if end < 0:
            end = min(len(s), start + 1600)

        snippet = s[start:end].strip()

        tool_name: Optional[str] = None
        m = re.search(r"(?:<invoke|<functioninvoke)\s+name=[\"']([^\"']+)[\"']", snippet, flags=re.IGNORECASE)
        if m:
            tool_name = str(m.group(1) or "").strip() or None

        def _shorten(v: str) -> str:
            return (v or "").strip()

        # Keep structure but avoid dumping huge parameter bodies into the retry message.
        snippet = re.sub(
            r"(<parameter[^>]*>)([\s\S]*?)(</parameter>)",
            lambda mm: mm.group(1) + _shorten(mm.group(2)) + mm.group(3),
            snippet,
            flags=re.IGNORECASE,
        )

        # IMPORTANT: escape '<' and '>' so the retry message doesn't itself contain
        # pseudo tool markup patterns that models might copy/paste.
        snippet = snippet.replace("<", "&lt;").replace(">", "&gt;")

        return (snippet, tool_name)

    @staticmethod
    def _log_pseudo_tool_markup_event(text: str) -> None:
        if not logs_enabled():
            return
        try:
            p = Path("logs/malformed_tool_markup.jsonl")
            p.parent.mkdir(parents=True, exist_ok=True)
            # Keep logs game-focused by default; raw model text is optional.
            include_snippet = (os.getenv("LLM_WORLD_LOG_PSEUDO_TOOL_SNIPPETS") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "on",
            }
            _snippet, tool_name = ConsoleApp._extract_pseudo_tool_markup_syntax(text or "")
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "pseudo_tool_markup_detected",
                **({"tool": tool_name} if tool_name else {}),
                **({"snippet": (text or "")} if include_snippet else {}),
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:
            pass

    def load_gm_history(self) -> None:
        msgs = []
        if self.storage_assistant_history_path.exists():
            msgs = load_full_gm_messages(self.storage_assistant_history_path)
        # Defensive: if an older version accidentally persisted console commands
        # (e.g. "/continue 2") as user messages, drop them on load.
        filtered: List[Any] = []
        for m in msgs:
            try:
                if getattr(m, "type", "") in {"human", "user"}:
                    c = str(getattr(m, "content", "") or "").strip().lower()
                    if (
                        c.startswith("/continue")
                        or c.startswith("/reset")
                        or c.startswith("/reset_chat")
                        or c.startswith("/stream")
                        or c.startswith("/backup")
                        or c.startswith("/restore")
                    ):
                        continue
                if getattr(m, "type", "") in {"ai", "assistant"}:
                    c = str(getattr(m, "content", "") or "")
                    tcs = getattr(m, "tool_calls", None) or []
                    if not tcs and self._is_transient_assistant_error_message(c):
                        continue
                filtered.append(m)
            except Exception:
                filtered.append(m)

            # Remove stale retry/error tool chatter so SA keeps meaningful action history.
            filtered = self._strip_tool_error_pairs(filtered)

        # If persisted history is clearly off-topic/corrupted (e.g., spammy assistant text
        # with repeated tool errors and no user input), auto-clear it. The game state lives
        # in storage; corrupted chat history just anchors the model into nonsense.
        try:
            ai_msgs = [m for m in filtered if getattr(m, "type", "") in {"ai", "assistant"}]
            human_msgs = [m for m in filtered if getattr(m, "type", "") in {"human", "user"}]
            tool_msgs = [m for m in filtered if getattr(m, "type", "") == "tool"]

            tool_errors = 0
            for tm in tool_msgs:
                content_low = str(getattr(tm, "content", "") or "").lower()
                if content_low.strip().startswith("error:"):
                    tool_errors += 1

            ai_preview = "\n".join(
                [str(getattr(m, "content", "") or "") for m in ai_msgs[:3]]
            ).lower()
            looks_like_offtopic = any(
                k in ai_preview
                for k in [
                    "web scraping",
                    "books to scrape",
                    "analysis.ipynb",
                    "books_data.csv",
                    "project completion",
                ]
            )

            looks_like_corrupt = (
                len(human_msgs) == 0
                and len(ai_msgs) >= 3
                and len(tool_msgs) >= 3
                and tool_errors >= 3
                and looks_like_offtopic
            )

            if looks_like_corrupt:
                if logs_enabled():
                    print("[trace] storage assistant history appears corrupted/off-topic; clearing persisted history")
                self.state = {"messages": []}
                try:
                    if self.storage_assistant_history_path.exists():
                        self.storage_assistant_history_path.unlink()
                except Exception:
                    pass
                self.gm_history_preexists = False
                return
        except Exception:
            pass

        self.state = {"messages": filtered}

    def save_gm_history(self) -> None:
        try:
            msgs = list(self.state.get("messages") or [])
            # Never persist console commands as part of the GM's conversational memory.
            cleaned: List[Any] = []
            for m in msgs:
                try:
                    if getattr(m, "type", "") in {"human", "user"}:
                        c = str(getattr(m, "content", "") or "").strip().lower()
                        if (
                            c.startswith("/continue")
                            or c.startswith("/reset")
                            or c.startswith("/reset_chat")
                            or c.startswith("/stream")
                            or c.startswith("/backup")
                            or c.startswith("/restore")
                        ):
                            continue

                    # Drop empty assistant placeholders that carry no tool calls.
                    # Keep assistant messages with tool_calls even if textual content is empty.
                    if getattr(m, "type", "") in {"ai", "assistant"}:
                        c = str(getattr(m, "content", "") or "").strip()
                        tcs = getattr(m, "tool_calls", None) or []
                        if not c and not tcs:
                            continue
                        if not tcs and self._is_transient_assistant_error_message(c):
                            continue

                    cleaned.append(m)
                except Exception:
                    cleaned.append(m)

            # Drop stale tool errors and orphaned failed tool-calls before persistence.
            cleaned = self._strip_tool_error_pairs(cleaned)

            # Persist a trimmed history, and also trim in-memory state so the next
            # invocation cannot exceed model context due to unbounded growth.
            trimmed = trim_full_gm_messages(cleaned, limits=self.limits, max_turns=self.max_turns)

            # Safety net: keep a one-step backup of the last persisted history.
            # This prevents permanent loss if a process starts with an empty/failed
            # load and then overwrites the file.
            try:
                if self.storage_assistant_history_path.exists():
                    prev = self.storage_assistant_history_path.with_name(
                        "storage_assistant_messages.prev.json"
                    )
                    shutil.copy2(self.storage_assistant_history_path, prev)
            except Exception:
                pass

            save_full_gm_messages(self.storage_assistant_history_path, trimmed)
            self.state = {"messages": list(trimmed)}
        except Exception:
            pass

    def _ensure_sa_bootstrap(self) -> None:
        """Ensure SA history uses atomic, marker-based context messages.

        Backward compatibility: if legacy SA bootstrap blobs are present,
        keep that history untouched to avoid duplicate context copies.
        """
        msgs_before = list(self.state.get("messages") or [])

        # Legacy SA bootstrap present: skip atomic injection in this history.
        if (
            self._sa_injector.history_contains("[world_snapshot:locations]")
            or self._sa_injector.history_contains("[world_snapshot:characters]")
            or self._sa_injector.history_contains("[world_snapshot:npcs]")
        ):
            return

        try:
            self._sa_injector.ensure_world_meta(world=self.world)

            for loc_name in sorted((self.world.get_locations() or {}).keys()):
                self._sa_injector.ensure_location_description(world=self.world, location=str(loc_name))

            for ch_name in self.world.list_character_names():
                self._sa_injector.ensure_character_description(world=self.world, name=str(ch_name))

            for npc_name in sorted((self.world.get_npcs() or {}).keys()):
                self._sa_injector.ensure_npc_description(world=self.world, name=str(npc_name))

            self._sa_injector.ensure_story_summaries(world=self.world)
        except Exception:
            return

        msgs_after = list(self.state.get("messages") or [])
        if len(msgs_after) != len(msgs_before):
            self.save_gm_history()

    def reset_conversation(self) -> None:
        """Reset GM conversation state for this session and on disk.

        This clears the persisted GM chat history and bootstrap markers so that
        first-run injections (character cards / init plot) can re-run if applicable.
        It does NOT reset the world/story state.
        """

        # Clear in-memory messages.
        self.state = {"messages": []}

        # Clear persisted storage assistant history.
        try:
            if self.storage_assistant_history_path.exists():
                self.storage_assistant_history_path.unlink()
        except Exception:
            pass

        # Clear bootstrap flags (character cards / init plot).
        try:
            self._write_gm_bootstrap({})
        except Exception:
            pass

        # Treat as "no history existed" for init plot injection logic.
        self.gm_history_preexists = False

        # Defensive: clear any per-invocation tool lock.
        try:
            reset_turn_lock()
        except Exception:
            pass

    def reset_game(self) -> None:
        """Hard reset: delete game/ and re-seed from init/.

        This resets the entire simulation state (world/story/scene/characters/NPCs) by
        removing the persisted game directory and running the normal init bootstrap.
        """

        workspace = Path(__file__).resolve().parent
        game_root = (workspace / "game").resolve()

        # Try to delete the whole game directory.
        try:
            if game_root.exists():
                shutil.rmtree(game_root)
        except Exception:
            # If deletion fails (Windows file locks, etc.), fall back to renaming.
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup = (workspace / f"game_old_{ts}").resolve()
                if game_root.exists() and not backup.exists():
                    game_root.rename(backup)
            except Exception:
                # As a last resort, keep the old folder.
                pass

        # Recreate game/ from init/ and ensure world core files exist.
        initialize_game_dir(init_root="init", game_root="game")

        # Recreate runtime objects so paths/flags are consistent.
        self.world = World(workspace_root=workspace)
        self.world.ensure_initialized()
        self.limits = limits_from_env()
        self.max_turns = gm_max_turns_from_env()
        self.storage_assistant_history_path = (
            self.world.game_root / "storage_assistant_messages.json"
        ).resolve()
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        self.gm_history_preexists = False
        self._gm_factory = StorageAssistantFactory()
        self.state = {"messages": []}

        try:
            reset_turn_lock()
        except Exception:
            pass

    def add_character_to_active_game(self, name: str, description_data: Dict[str, Any]) -> None:
        """Add a newly-created character to the active game and notify both agents.

        Steps:
        1. Write description.json to game/characters/<name>/ (skip if already exists).
        2. Call ensure_initialized() so info.json is synced with the new entry.
        3. Inject a [new_character:<name>] HumanMessage into SA history so the SA
           has the character data in its context window on the very next turn.
        4. Inject the same announcement into GM history via inject_delta() so the GM
           is aware without needing to look it up from a tool call.
        """
        name = (name or "").strip()
        if not name:
            return

        # 1. Copy description to game/characters/<name>/
        game_char_dir = (self.world.game_root / "characters" / name).resolve()
        try:
            game_char_dir.mkdir(parents=True, exist_ok=True)
            desc_path = game_char_dir / "description.json"
            if not desc_path.exists():
                desc_path.write_text(
                    json.dumps(description_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            return

        # 2. Sync info.json (adds character to the characters list)
        try:
            self.world.ensure_initialized()
        except Exception:
            pass

        # 3+4. Build announcement and inject into both histories
        marker = f"[new_character:{name}]"
        desc_json = json.dumps(description_data, ensure_ascii=False, indent=2)
        announcement = (
            f"{marker}\n"
            f"NEW CHARACTER ADDED: {name}\n"
            f"A new character has just been added to the game world.\n"
            f"Description:\n{desc_json}"
        )

        # Inject into SA history (LangChain message format)
        try:
            sa_msgs = load_full_gm_messages(self.storage_assistant_history_path)
            already = any(marker in str(getattr(m, "content", "") or "") for m in sa_msgs)
            if not already:
                sa_msgs.append(HumanMessage(content=announcement))
                save_full_gm_messages(self.storage_assistant_history_path, sa_msgs)
        except Exception:
            pass

        # Inject into GM history
        try:
            if not self._gm_history_contains(marker):
                self._game_master.inject_delta(announcement)
        except Exception:
            pass

    def _gm_bootstrap_path(self) -> Path:
        return (self.world.game_root / "world" / "gm_bootstrap.json").resolve()

    def _read_gm_bootstrap(self) -> Dict[str, Any]:
        """Load bootstrap markers from a dedicated file (not info.json).

        Also migrates legacy `info.json["gm_bootstrap"]` the first time we see it.
        """

        # One-time migration from legacy location.
        try:
            info = self.world.get_info()
            if isinstance(info, dict) and isinstance(info.get("gm_bootstrap"), dict):
                legacy = dict(info.get("gm_bootstrap") or {})
                # Write to new location.
                if legacy:
                    self._write_gm_bootstrap(legacy)
                # Remove from info.json to avoid prompt leakage.
                try:
                    del info["gm_bootstrap"]
                except Exception:
                    info.pop("gm_bootstrap", None)
                try:
                    self.world.paths.info_json.write_text(
                        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        except Exception:
            pass

        try:
            p = self._gm_bootstrap_path()
            if not p.exists():
                return {}
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_gm_bootstrap(self, data: Dict[str, Any]) -> None:
        try:
            p = self._gm_bootstrap_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(data or {}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(p)
        except Exception:
            # Best-effort only.
            pass

    def _read_world_facts_context(self) -> str:
        """World facts are history-backed; no extra context block needed."""
        return ""

    def _write_world_facts(self, facts: str) -> None:
        """Persist world facts for SA; GM stores them in TURN_NARRATION assistant history."""
        try:
            text = str(facts or "").strip()
            if not text:
                return

            # SA consumes in-memory self.state["messages"] during the active session.
            # Inject here so world_facts is visible immediately on the next SA invocation.
            # save_gm_history() persists the updated state later in the turn-finalization flow.
            self._inject_sa_delta(f"[world_facts]\n{text}")
        except Exception:
            pass

    # Backward-compatible aliases kept to avoid rename regressions.
    def _read_gm_thoughts_context(self) -> str:
        return self._read_world_facts_context()

    def _write_gm_thoughts(self, thoughts: str) -> None:
        self._write_world_facts(thoughts)

    def _set_gm_bootstrap_flag(self, updates: Dict[str, Any]) -> None:
        try:
            gm_bootstrap = self._read_gm_bootstrap()
            gm_bootstrap.update(updates)
            self._write_gm_bootstrap(gm_bootstrap)
        except Exception:
            pass

    def maybe_inject_initial_character_cards(self) -> None:
        # This feature injects ALL character cards as a persistent AI message.
        # Default is OFF to avoid bloating history and to ensure only active-scene
        # participant details are injected (via the per-invocation GM context).
        enabled = (os.getenv("LLM_WORLD_INJECT_ALL_CHARACTER_CARDS") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        if not enabled:
            return

        marker = "[bootstrap_character_cards_v1]"

        gm_bootstrap = self._read_gm_bootstrap()
        if bool(gm_bootstrap.get("character_cards_injected")):
            return

        # If marker exists in persisted history, treat it as already injected.
        for m in (self.state.get("messages") or []):
            try:
                if marker in str(getattr(m, "content", "") or ""):
                    return
            except Exception:
                continue

        turns_count, paragraphs_count = self.story_progress()
        if turns_count != 0 or paragraphs_count != 0:
            return

        names = self.world.list_character_names()
        cards: List[Dict[str, Any]] = []
        for name in names:
            try:
                desc = self.world.get_character_description(name)
                cards.append({"name": name, "description": desc})
            except Exception as e:  # noqa: BLE001
                cards.append({"name": name, "error": str(e)})

        payload = json.dumps(cards, ensure_ascii=False, indent=2)
        bootstrap_text = (
            f"{marker}\n"
            "REFERENCE ONLY (pre-game bootstrap): Full character cards are provided below so you do not need to call tools to fetch them.\n"
            "Treat this as static background reference; do not roleplay this message and do not assume players have seen it.\n\n"
            "=== Character Cards (JSON) ===\n"
            f"{payload}\n"
        )

        self.state = {"messages": [AIMessage(content=bootstrap_text)] + list(self.state.get("messages") or [])}
        self._set_gm_bootstrap_flag(
            {
                "character_cards_injected": True,
                "version": 1,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.save_gm_history()

    def invoke_once(self, user_msg: Optional[HumanMessage]) -> None:
        run_turn(self, user_msg)

    def maybe_inject_init_plot_as_first_user_command(self) -> Optional[str]:
        """Inject init/plot.json once as an automatic first user command.

        Returns an error string if malformed, otherwise None.
        """

        if self.gm_history_preexists:
            return None

        # Backward/alternate location support: some users place plot.json under init/characters.
        # Prefer init/plot.json when both exist.
        init_plot_path = Path("init") / "plot.json"
        alt_plot_path = Path("init") / "characters" / "plot.json"
        if not init_plot_path.exists():
            init_plot_path = alt_plot_path
        if not init_plot_path.exists():
            return None

        turns_count, paragraphs_count = self.story_progress()
        if turns_count != 0 or paragraphs_count != 0:
            return None

        gm_bootstrap = self._read_gm_bootstrap()
        if bool(gm_bootstrap.get("init_plot_injected")):
            return None

        

        try:
            raw = init_plot_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return (
                f"Failed to read {init_plot_path} as UTF-8. "
                "Tip: save it as UTF-8 (no BOM) and ensure it is valid JSON."
            )

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            snippet = _json_error_snippet(text=raw, lineno=int(getattr(e, "lineno", 1) or 1))
            return (
                "Malformed plot.json.\n"
                f"File: {init_plot_path}\n"
                f"Error: {e.msg} (line {e.lineno}, column {e.colno})\n"
                "Common causes: trailing commas, comments, unquoted keys/strings.\n"
                "Context:\n"
                f"{snippet}"
            )

        plot_payload = json.dumps(parsed, ensure_ascii=False, indent=2)
        plot_message = (
            "[init_plot_v1]\n"
            f"This is the initial plot seed (loaded automatically from {init_plot_path}). "
            "Treat it as the very first player-provided instruction that starts the game.\n\n"
            "=== plot.json (JSON) ===\n"
            f"{plot_payload}\n"
        )

        if logs_enabled():
            print(f"[trace] injecting {init_plot_path} as first user command")

        self.invoke_once(HumanMessage(content=plot_message))
        self._init_plot_injected_this_run = True

        self._set_gm_bootstrap_flag(
            {
                "init_plot_injected": True,
                "init_plot_version": 1,
                "init_plot_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return None

    def gm_memory_usage_line(self) -> str:
        msgs = list(self.state.get("messages") or [])
        approx_tokens = 0
        for m in msgs:
            approx_tokens += approx_token_count(str(getattr(m, "content", "") or ""))
        return (
            f"[trace] gm memory usage: msgs={len(msgs)} "
            f"approx_tokens~{approx_tokens}/{self.limits.max_history_tokens} "
            f"max_turns={self.max_turns}"
        )

    def _print_latest_output(self, *, is_continue: bool, turn_finalized: bool) -> None:
        _, _, last_story_text = self.story_progress_and_last_text()

        # /continue is intended to be a silent "tick". If no turn finalized,
        # do not print any free-form assistant text (it can look like the model
        # is responding to the console command itself).
        if is_continue and not turn_finalized:
            return

        # If streaming echo is enabled, the model text was already printed token-by-token.
        # Only emit the structured recap on finalize; avoid duplicating the assistant text.
        if self.stream_echo and not turn_finalized:
            return

        # When a turn finalized, print the narration from the world state
        # (story.json contains the narrative after turn finalization).
        if turn_finalized:
            narration = (last_story_text or "").strip()
            if narration:
                print("\n" + narration.strip() + "\n")
                return

        if (is_continue or turn_finalized) and last_story_text.strip():
            print("\n" + last_story_text.strip() + "\n")
            return

        last_content = ""
        for m in reversed(self.state.get("messages") or []):
            t = getattr(m, "type", "")
            if t in {"ai", "assistant"}:
                last_content = getattr(m, "content", "") or ""
                if last_content:
                    break
        print("\n" + (last_content or "").strip() + "\n")

    def run(self) -> int:
        print("LLM World (console) — Storage Assistant (LangGraph ReAct)")
        print("Commands: /continue, /reset, /reset_chat, /backup, /restore, /stream, /exit")
        print("  - /continue [N] runs N turns without prompting (default N=1)")
        print("  - /backup <name> saves a snapshot of game/ to backups/<name>/")
        print("  - /restore <name> restores game/ from backups/<name>/")
        print("(Auto-advance runs until a turn finalizes; press Ctrl+C to interrupt.)")
        print("(Tip: open a second terminal and run: Get-Content .\\logs\\stream.txt -Wait)")

        self._apply_stream_env()

        self._load_session_state(show_history_stats=True)

        # Keep console output game-focused by default.
        debug_trace = (os.getenv("LLM_WORLD_DEBUG_TRACE") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
        if debug_trace:
            print(
                f"(GM memory cap ~{self.limits.max_history_tokens} tokens; configurable via LLM_WORLD_MODEL_CONTEXT_TOKENS and LLM_WORLD_HISTORY_FRACTION)"
            )

        self._maybe_bootstrap_plot_and_autoadvance()

        while True:
            try:
                user_text = input("You> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_text:
                continue

            should_exit = self._dispatch_user_input(user_text=user_text, debug_trace=debug_trace)
            if should_exit:
                break

        return 0
