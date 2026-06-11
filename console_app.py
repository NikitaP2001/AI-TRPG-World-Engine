"""Game orchestrator for the LLM World simulation.

This module orchestrates two LLM agents:

1. Game Master (gm/game_master.py):
    - Narrative agent for world planning, scene descriptions, turn narration
    - Uses ReAct loop with tools to create/manage world state
    - Maintains persistent history in game/game_master_messages.json

2. Scene Manager (scene_manager/):
    - Scene description, character execution, turn narration
    - Has its own message history and prompt log output

Composed modules:
  engine/story_tracker.py  — StoryTracker: story progress queries (turns, paragraphs, arc)
  engine/gm_context.py     — GMContextManager: GM history bootstrap, scene-pick entity injection

Schedule (scheduler/ package):
  scheduler/core.py  — TickScheduler, Job, Trigger types (TurnCount, ParagraphCount, etc.)
  scheduler/jobs.py  — Single registry of ALL scheduled tasks (the full schedule at a glance)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

from gm.full_history import gm_max_turns_from_env, load_full_gm_messages, save_full_gm_messages, trim_full_gm_messages
from gm.history_injector import GMHistoryInjector
from gm.game_master import (
    GameMaster,
    parse_game_master_json,
)
from world.context import build_game_master_context_block as build_game_master_context, build_game_master_qa_context
from gm.tools import (
    reset_turn_lock,
    is_context_changed,
    signal_context_changed,
)
from gm.injection_models import InjectionProfile, InjectionRule, InjectionEngine
from gm.injection_builders import (
    build_world_meta,
)
from scheduler import TickScheduler, Job, TurnCount, ParagraphCount, MemoryEntryCount
from scheduler.jobs import register_all_jobs
from character.agent import run_character_agent, set_scene_manager_for_characters
from character.reflection import run_reflection
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
from world import World, _json_error_snippet
from world.time import WorldTime
# scene.context imports removed — auto-advance no longer calls GM for scene picking
from turn_runner import run_turn

from engine.gm_context import GMContextManager
from scene_manager import SceneManager
from world_manager import WorldManager

# Import extracted utilities
from console_app_utils import (
    _TURN_RECAPS_FILENAME,
    _GUI_STREAM_INPUT_LOCK,
    append_turn_recap,
    backup_slug,
    is_invalid_gm_text_output,
    is_transient_assistant_error_message,
    strip_tool_error_pairs,
    log_pseudo_tool_markup_event,
)

class GameOrchestrator:
    _startup_scene_cleanup_done: bool = False
    _auto_scene_pause: bool = True

    def __init__(self) -> None:
        self.world = World()
        self.world.ensure_initialized()
        if not GameOrchestrator._startup_scene_cleanup_done:
            self._discard_interrupted_scene_state()
            GameOrchestrator._startup_scene_cleanup_done = True

        self.limits = limits_from_env()
        self.max_turns = gm_max_turns_from_env()
        self._rag_search: Optional[Any] = None

        # Game Master (narrative agent) - stores history in game_master_messages.json
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()

        self._init_plot_injected_this_run: bool = False

        
        # Game Master (narrative agent) - maintains roleplay identity with persistent history
        self._game_master = GameMaster(history_path=self.gm_master_history_path)
        self._gm_injector = GMHistoryInjector(
            game_master=self._game_master,
            gm_history_path=self.gm_master_history_path,
            history_limits=self.limits,
        )
        self._gm_ctx = GMContextManager(gm_injector=self._gm_injector, world=self.world)
        
        # Set up Game Master for character Q&A
        set_scene_manager_for_characters(self._sm)

        # Paragraph summarization handled by SM (run_summary_task) via scheduler

        # ── Composed modules ──────────────────────────────────────────
        from engine.story_tracker import StoryTracker

        self._story_tracker = StoryTracker(
            story_json_path=self.world.paths.story_json,
        )



        self._sm = SceneManager(
            history_path=(self.world.game_root / "scene_manager_messages.json").resolve(),
        )

        # ── World Manager (highest-level agent) ──────────────────────
        self._wm = WorldManager(
            history_path=self.gm_master_history_path,
        )
        # Check if setting exists (no LLM call during startup).
        # Actual creation is deferred to _maybe_create_world_setting() called from run_turn.
        self._world_setting_ready = bool(self._wm.load_world_setting())


        # ── Injection engines (profile-based, ready for multi-agent expansion) ──
        # Each agent type gets its own InjectionProfile and InjectionEngine.
        # The existing HistoryInjector instances (^ above) still handle current
        # injection logic. The engines below are additive — they can be used
        # alongside or as a replacement in future refactors.
        self._engines: Dict[str, InjectionEngine] = {}

        # GM engine — mirrors the injections done by self._gm_injector
        _gm_loader = lambda: trim_history(load_history(self.gm_master_history_path), limits=self.limits)
        self._engines["gm"] = InjectionEngine(
            profile=InjectionProfile("game_master", [
                InjectionRule("[world_snapshot:world_meta]", build_world_meta, scope="world", priority=100),
            ]),
            history_loader=_gm_loader,
            delta_injector=self._game_master.inject_delta,
        )


        # ── Unified job scheduler ──
        self._scheduler = TickScheduler(
            state_path=self.world.game_root / "world" / "scheduler_state.json",
        )

        # Register periodic jobs.
        self._register_scheduled_jobs()

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

    def _maybe_create_world_setting(self) -> bool:
        """Create the world setting block on-demand (called from run_turn, not startup).
        Returns True if setting is ready, False if creation failed (will retry later).
        """
        if self._world_setting_ready:
            return True
        try:
            # Gather characters + plot info
            characters = []
            try:
                info = self.world.get_info()
                raw_chars = info.get("characters") if isinstance(info, dict) else []
                if isinstance(raw_chars, list):
                    for c in raw_chars:
                        if isinstance(c, dict):
                            name = str(c.get("name") or "").strip()
                            if name:
                                desc = {}
                                try:
                                    desc = self.world.get_character_description(name)
                                except Exception:
                                    pass
                                if isinstance(desc, dict):
                                    characters.append(desc)
            except Exception:
                pass

            plot = {}
            try:
                from init.plot import load_plot
                plot = load_plot()
            except Exception:
                pass

            setting = self._wm.create_world_setting(
                characters=characters,
                plot=plot,
            )
            if setting:
                self._wm.save_world_setting(setting)
                self._world_setting_ready = True
                if logs_enabled():
                    print(f"[trace] World setting created")
                return True
            else:
                if logs_enabled():
                    print("[trace] World setting creation returned empty — blocking turn")
                return False
        except Exception as e:
            if logs_enabled():
                print(f"[trace] World setting creation error: {e} — blocking turn")
            return False

    # ======================================================================
    # Job scheduler
    # ======================================================================

    def _register_scheduled_jobs(self) -> None:
        """Register all periodic jobs via the single registry in scheduler/jobs.py."""
        register_all_jobs(self._scheduler, self)

    def _run_character_reflection(self, character_name: str) -> None:
        """Run reflection for one character (scheduler job callback)."""
        try:
            from character.reflection import run_reflection
            try:
                char_desc = self.world.get_character_description(character_name)
            except Exception:
                char_desc = {}
            run_reflection(character_name=character_name, character_description=char_desc)
        except Exception as e:
            import logging
            logging.exception(f"[scheduler] reflection failed for {character_name}: {e}")
            raise

    def _run_character_diary(self, character_name: str) -> None:
        """Run diary for one character (scheduler job callback)."""
        try:
            from character.reflection import run_diary
            run_diary(character_name=character_name)
        except Exception as e:
            import logging
            logging.exception(f"[scheduler] diary failed for {character_name}: {e}")
            raise

    def _run_character_review(self, character_name: str) -> None:
        """Run relationship review for one character (scheduler job callback)."""
        try:
            from character.relationship_review import run_relationship_review
            run_relationship_review(
                character_name=character_name,
                world=self.world,
            )
        except Exception as e:
            import logging
            logging.exception(f"[scheduler] review failed for {character_name}: {e}")
            raise

    def story_progress_and_last_text(self) -> Tuple[int, int, str]:
        """Return (turns_in_ongoing_paragraph, finalized_paragraphs, last_narration_text)."""
        return self._story_tracker.full_progress()

    def _scheduler_tick(self) -> None:
        """Call scheduler.tick() with current game counters."""
        try:
            turns, paras, _ = self.story_progress_and_last_text()
        except Exception:
            turns, paras = 0, 0

        try:
            char_names = self.world.list_character_names()
        except Exception:
            char_names = []

        self._scheduler.tick(
            turn_count=turns,
            paragraph_count=paras,
            world=self.world,
            character_names=char_names,
        )

