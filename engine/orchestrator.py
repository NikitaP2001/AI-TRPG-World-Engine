"""Orchestrator — game engine orchestration layer.

Owns the resolution cycle: bootstrap scene → run characters → finalize.
Orchestrator is the single entry point for "advance the game one step."
It does NOT contain UI, REPL, or GUI code.

All agent instances (SM, GM, WM, Scene) are created here and exposed
for UI layers to inspect.

Layer 1 in the architecture — above agents, below entry points.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from world import World, WorldTime
from world.time import WorldTime as _WorldTime
from scene import Scene
from scene_manager import SceneManager
from world_manager import WorldManager
from gm.game_master import GameMaster, parse_game_master_json
from gm.history_injector import GMHistoryInjector
from gm.injection_builders import build_world_meta
from gm.injection_models import InjectionProfile, InjectionRule, InjectionEngine
from gm.tools import reset_cycle_lock
from engine.gm_context import GMContextManager
from engine.story_tracker import StoryTracker
from scheduler import TickScheduler
from scheduler.jobs import register_all_jobs
from memory_store import HistoryLimits, limits_from_env, trim_history, load_history
from openrouter_langchain_logging import logs_enabled
from bootstrap import initialize_game_dir
from gm.full_history import gm_max_cycles_from_env
from cycle_runner import run_cycle


class Orchestrator:
    """Game engine orchestrator — owns all agent instances and the cycle logic.

    Entry points (console_app, opengl_app) create one Orchestrator and call
    ``run_cycle()`` to advance the game by one step.
    """

    _startup_scene_cleanup_done: bool = False

    def __init__(self) -> None:
        self.world = World()
        self.world.ensure_initialized()
        if not Orchestrator._startup_scene_cleanup_done:
            self._discard_interrupted_scene_state()
            Orchestrator._startup_scene_cleanup_done = True

        self.limits = limits_from_env()
        self.max_cycles = gm_max_cycles_from_env()

        # ── GM ──
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        self._game_master = GameMaster(history_path=self.gm_master_history_path)
        self._gm_injector = GMHistoryInjector(
            game_master=self._game_master,
            gm_history_path=self.gm_master_history_path,
            history_limits=self.limits,
        )
        self._gm_ctx = GMContextManager(gm_injector=self._gm_injector, world=self.world)

        # ── Scene ──
        self._scene = Scene(world=self.world)

        # ── Story tracker ──
        self._story_tracker = StoryTracker(
            story_json_path=self.world.paths.story_json,
        )

        # ── SM ──
        self._sm = SceneManager(
            history_path=(self.world.game_root / "scene_manager_messages.json").resolve(),
        )

        # ── WM ──
        self._wm = WorldManager(
            history_path=self.gm_master_history_path,
        )
        self._world_setting_ready = bool(self._wm.load_world_setting())
        self._fully_initialized = False

        # ── Injection engines ──
        self._engines: Dict[str, InjectionEngine] = {}
        _gm_loader = lambda: trim_history(load_history(self.gm_master_history_path), limits=self.limits)
        self._engines["gm"] = InjectionEngine(
            profile=InjectionProfile("game_master", [
                InjectionRule("[world_snapshot:world_meta]", build_world_meta, scope="world", priority=100),
            ]),
            history_loader=_gm_loader,
            delta_injector=self._game_master.inject_delta,
        )

        # ── Scheduler ──
        self._scheduler = TickScheduler(
            state_path=self.world.game_root / "world" / "scheduler_state.json",
        )
        self._register_scheduled_jobs()

        # ── Stream echo ──
        raw_stream = os.getenv("LLM_WORLD_STREAM_ECHO")
        if raw_stream is None or not raw_stream.strip():
            self.stream_echo = True
        else:
            self.stream_echo = raw_stream.strip().lower() in {"1", "true", "yes", "y", "on"}
        self._apply_stream_env()

    # ── Properties for backward compat ──

    @property
    def _rag_search(self):
        return None

    # ── Stream ──

    def _apply_stream_env(self) -> None:
        os.environ["LLM_WORLD_STREAM_ECHO"] = "1" if self.stream_echo else "0"

    # ── World setting ──

    def ensure_world_setting(self) -> bool:
        """Create the world setting block on-demand.
        Returns True if setting is ready.
        """
        if self._world_setting_ready:
            return True
        try:
            characters = []
            try:
                info = self.world.get_info()
                raw_chars = info.get("players") if isinstance(info, dict) else []
                if isinstance(raw_chars, list):
                    characters = [c for c in raw_chars if isinstance(c, dict)]
            except Exception:
                pass
            plot_text = ""
            try:
                from world.io import read_json
                plot_path = self.world.game_root / "setups" / "plot.json"
                if plot_path.exists():
                    plot_data = read_json(plot_path)
                    if not isinstance(plot_data, dict):
                        plot_data = {}
                else:
                    plot_data = {}
            except Exception:
                plot_data = {}
            self._wm.create_world_setting(characters=characters, plot=plot_data)
            self._world_setting_ready = True
            if logs_enabled():
                print("[trace] World setting created")
            return True
        except Exception as e:
            if logs_enabled():
                print(f"[trace] World setting creation failed: {e}")
            return False

    # ── Preset loading ──

    def load_preset(self, name: str) -> None:
        """Load a preset (characters, locations, plot) from setups/{name}/ or setups/{name}.json."""
        from world.io import read_json, write_json
        import shutil
        game_root = self.world.game_root
        workspace_root = game_root.parent
        preset_dir = workspace_root / "setups"
        src_dir = preset_dir / name

        if src_dir.is_dir():
            # Load structured format: character subdirectories with description.json / metadata.json
            chars_src = src_dir / "characters"
            if chars_src.is_dir():
                for char_dir in sorted(chars_src.iterdir()):
                    if not char_dir.is_dir():
                        continue
                    char_name = char_dir.name
                    dst_dir = game_root / "players" / char_name
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for json_file in char_dir.glob("*.json"):
                        shutil.copy2(json_file, dst_dir / json_file.name)

            # Also check for legacy flat JSON files
            for f in ["characters.json", "locations.json", "plot.json"]:
                src = src_dir / f
                if src.exists():
                    data = read_json(src)
                    if f == "plot.json":
                        write_json(game_root / "setups" / f, data)

            # Copy description.json if present (shared character descriptions)
            desc_src = src_dir / "description.json"
            if desc_src.exists():
                shutil.copy2(desc_src, game_root / "setups" / "description.json")

        else:
            preset_file = preset_dir / f"{name}.json"
            if preset_file.exists():
                data = read_json(preset_file)
                chars = data.get("players", [])
                for ch_data in chars:
                    char_name = str(ch_data.get("name") or "").strip()
                    if char_name:
                        dst_dir = game_root / "players" / char_name
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        (dst_dir / "description.json").write_text(
                            json.dumps(ch_data, ensure_ascii=False, indent=2))
                locs = data.get("locations", [])
                for loc_data in locs:
                    self.world.add_location(loc_data)

        # Ensure character files are properly indexed
        try:
            from world.io import ensure_character_description_fields
            ensure_character_description_fields(game_root=game_root)
        except Exception:
            pass

        # Sync info.json so get_info() reflects loaded characters immediately
        try:
            self.world._sync_info_characters()
        except Exception:
            pass

        print(f"  ✓ Story seeded")
        info = read_json(self.world.paths.info_json)
        chars_info = info.get("players") if isinstance(info, dict) else []
        loaded_chars = [c for c in chars_info if isinstance(c, dict) and c.get("name")]
        print(f"  ✓ {len(loaded_chars)} characters loaded")
        try:
            locs = self.world.get_locations()
            print(f"  ✓ {len(locs)} location(s) created")
        except Exception:
            pass
        print(f"Preset '{name}' loaded. Use 'continue' to start.")

    # ── Scene bootstrap ──

    def bootstrap_scene(self) -> bool:
        """Check if a scene is active; if not, determine and start the next one.

        Returns True if a new scene was started, False if scene already active
        or if we couldn't determine a scene.
        """
        try:
            scene = self.world.get_scene()
            if isinstance(scene, dict) and scene.get("state") == "active":
                return False
        except Exception:
            pass

        last_loc = ""
        try:
            info = self.world.get_info()
            if isinstance(info, dict):
                last_loc = str(info.get("last_location") or "").strip()
        except Exception:
            pass

        decision = self._sm.determine_next_scene(last_scene_location=last_loc)
        decision_type = str(decision.get("type") or "").strip()

        if decision_type == "no_scene":
            print(f"  ℹ️  No scene: {decision.get('reason', 'unknown')}")
            return False

        location = str(decision.get("location") or "").strip()
        char_names = decision.get("character_names") or []
        npc_names = decision.get("npc_names") or []

        if not location or not char_names:
            print("  ℹ️  Cannot start scene: no location or characters")
            return False

        player_descriptions: Dict[str, str] = {}

        # Resolve location name to a real entity_id before passing to SM
        try:
            locs = self.world.get_locations()
            if location not in locs and locs:
                loc_lower = location.lower()
                for lid in locs:
                    if lid.lower() in loc_lower or loc_lower in lid.lower():
                        if logs_enabled():
                            print(f"[trace] Location '{location}' resolved to '{lid}'")
                        location = lid
                        break
                if location not in locs:
                    new_loc = next(iter(locs.keys()))
                    if logs_enabled():
                        print(f"[trace] Location '{location}' not found, using '{new_loc}'")
                    location = new_loc
        except Exception:
            pass

        print(f"  🎬 SM describing scene at {location}...")
        desc_result = self._sm.describe_scene(
            location=location,
            character_names=char_names,
            npc_names=npc_names,
        )
        player_descriptions = desc_result.get("personal") or {}

        # Write per-player descriptions to their messages.json
        if player_descriptions:
            from memory_store import append_message, limits_from_env
            limits = limits_from_env()
            for pname, pdesc in player_descriptions.items():
                if not pdesc.strip():
                    continue
                pfile = self.world.game_root / "players" / pname / "messages.json"
                append_message(pfile, role="assistant", content=pdesc.strip(), limits=limits)

        try:
            self._scene.start(
                character_names=char_names,
                location=location,
                npc_names=npc_names,
                scene_description="",
                player_descriptions=player_descriptions,
            )
            print(f"  🎬 Scene started: {location} ({len(char_names)} characters)")
            return True
        except ValueError as e:
            print(f"  ✗ Scene start failed: {e}")
            return False

    # ── Cycle finalization ──

    def finalize_cycle(self) -> bool:
        """Run the full resolution cycle: segment scheduler → SM narration → finalize.
        Delegates to console_app's implementation for now (migration in progress).
        """
        # Delegate to GameOrchestrator's finalize logic — avoids circular import
        # by importing at call time. Will be inlined once console_app is thinned.
        from console_app import _finalize_cycle_logic
        return _finalize_cycle_logic(self)

    # ── Run one cycle ──

    def run_cycle(self, user_msg: Optional[HumanMessage] = None) -> None:
        """Execute one resolution cycle: bootstrap → characters → finalize."""
        run_cycle(self, user_msg)

    # ── Job registration ──

    def _register_scheduled_jobs(self) -> None:
        register_all_jobs(self._scheduler, self)

    # ── Clear cycle temp dir ──

    def _clear_cycle_temp_dir(self) -> None:
        tmp = self.world.game_root / "tmp_cycle"
        if tmp.exists():
            import shutil
            shutil.rmtree(str(tmp))
        tmp.mkdir(parents=True, exist_ok=True)

    # ── Scene discard ──

    def _discard_interrupted_scene_state(self) -> None:
        """Discard scene state from a previous interrupted run."""
        try:
            scene = self.world.get_scene()
            if isinstance(scene, dict) and scene.get("state") == "active":
                self.world.clear_scene()
        except Exception:
            pass


# Backward-compatible alias
GameOrchestrator = Orchestrator

# Backward-compatible method aliases
Orchestrator._cmd_load_preset = Orchestrator.load_preset
Orchestrator._maybe_create_world_setting = Orchestrator.ensure_world_setting
