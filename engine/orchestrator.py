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
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from world import World, WorldTime
from world.time import WorldTime as _WorldTime
from scene_manager import SceneManager
from world_manager import WorldManager
from gm.game_master import GameMaster, parse_game_master_json
from gm.history_injector import GMHistoryInjector
from gm.injection_builders import build_world_meta
from gm.injection_models import InjectionProfile, InjectionRule, InjectionEngine
from gm.tools import reset_cycle_lock
from engine.gm_context import GMContextManager

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
        self._scene_bootstrapped = False
        if not Orchestrator._startup_scene_cleanup_done:
            Orchestrator._startup_scene_cleanup_done = True

        self.limits = limits_from_env()
        self.max_cycles = gm_max_cycles_from_env()
        self._prev_intent_keys: set = set()

        # ── GM ──
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        self._game_master = GameMaster(history_path=self.gm_master_history_path)
        self._gm_injector = GMHistoryInjector(
            game_master=self._game_master,
            gm_history_path=self.gm_master_history_path,
            history_limits=self.limits,
        )
        self._gm_ctx = GMContextManager(gm_injector=self._gm_injector, world=self.world)

        # ── SM ──
        self._sm = SceneManager(
            history_path=(self.world.game_root / "scene_manager_messages.json").resolve(),
        )

        # ── WM ──
        self.wm_messages_path = (self.world.game_root / "world_master_messages.json").resolve()
        self._wm = WorldManager(
            history_path=self.wm_messages_path,
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

        # ── Session management ──
        self._session_active = False

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
                for name in self.world.list_character_names():
                    desc = self.world.get_character_description(name)
                    if isinstance(desc, dict):
                        characters.append(desc)
            except Exception:
                pass
            plot_data = {}
            try:
                from world.io import read_json
                # Prefer the original preset plot over the game copy
                workspace_root = self.world.game_root.parent
                for candidate in [
                    workspace_root / "setups" / "plot.json",
                    self.world.game_root / "setups" / "plot.json",
                ]:
                    if candidate.exists():
                        raw = read_json(candidate)
                        if isinstance(raw, dict) and raw.get("init", "").strip():
                            plot_data = raw
                            break
            except Exception:
                pass
            ws = self._wm.create_world_setting(characters=characters, plot=plot_data)
            if ws:
                self._wm.save_world_setting(ws)
                initial_time = str(ws.get("initial_world_time") or "").strip()
                if initial_time:
                    self.world.set_world_time(initial_time)
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
        """Provide awareness updates to all characters via SM bootstrap.

        Runs once per session before the normal cycle. Resolves active groups,
        invokes SM with a notice listing characters needing awareness, SM calls
        update_awareness for each. Orchestrator parses awareness markers and
        writes them to player messages.json.
        """
        if self._scene_bootstrapped:
            return False

        groups = self._sm.resolve_active_groups()
        if not groups:
            print("  ℹ️  No active groups to bootstrap")
            return False

        # Filter out groups with no real location (characters without location set)
        groups = [g for g in groups if g["location"] and g["location"] != "no location specified"]
        if not groups:
            print("  ℹ️  No characters with a valid location to bootstrap")
            return False

        # Build pending list for validation
        pending_chars = []
        for g in groups:
            for cname in g.get("character_names", []):
                pending_chars.append((cname, g["location"]))

        # Verify locations exist
        from world_manager.entity_core import get_entity as _ge
        for g in groups:
            loc = g["location"]
            if _ge(loc) is None:
                raise ValueError(
                    f"Location '{loc}' not found in entity store. "
                    f"GM must create this location before starting a session."
                )

        print(f"  🎬 SM bootstrap: {len(pending_chars)} character(s) need awareness "
              f"across {len(groups)} location(s)")

        # Set close validation hook: all pending chars must get update_awareness
        from world_manager.tool_registry import set_close_validation_hook

        def _validate_bootstrap_close():
            """Check that all pending characters received update_awareness."""
            if not pending_chars:
                return None
            updated = set()
            for msg in self._sm._session_messages:
                content = str(getattr(msg, "content", "") or "")
                for m in re.findall(r'\[awareness:([^\]]+)\]', content):
                    for name in m.split(","):
                        updated.add(name.strip())
            missing = [name for name, _ in pending_chars if name not in updated]
            if missing:
                return (
                    "Bootstrap incomplete — characters still need awareness:\n"
                    + "\n".join(f"  - {name}" for name in missing)
                )
            return None

        set_close_validation_hook(_validate_bootstrap_close)

        try:
            # Run SM bootstrap
            self._sm.run_bootstrap(groups)
        finally:
            set_close_validation_hook(None)

        # Parse awareness markers from SM session messages and write to players
        from memory_store import append_message, limits_from_env
        limits = limits_from_env()

        awareness_texts: Dict[str, str] = {}
        for msg in self._sm._session_messages:
            content = str(getattr(msg, "content", "") or "")
            for m in re.findall(r'\[awareness:([^\]]+)\]\s*(.*)', content):
                names_part = m[0].strip()
                text = m[1].strip()
                for name in names_part.split(","):
                    name = name.strip()
                    if name and text:
                        if name in awareness_texts:
                            awareness_texts[name] += "\n\n" + text
                        else:
                            awareness_texts[name] = text

        if awareness_texts:
            for pname, text in awareness_texts.items():
                if not text.strip():
                    continue
                pfile = self.world.game_root / "players" / pname / "messages.json"
                append_message(pfile, role="assistant", content=text.strip(), limits=limits)
            print(f"  🎬 Awareness delivered to {len(awareness_texts)} character(s)")

        self._scene_bootstrapped = True
        return True

    # ── Cycle finalization ──

    def finalize_cycle(self, end_session: bool = False) -> bool:
        """Run the full resolution cycle. end_session=True publishes narration."""
        return self._do_finalize_cycle(end_session=end_session)

    def _do_finalize_cycle(self, end_session: bool = False) -> bool:
        try:
            chars_data = self.world.get_players()
            if not chars_data:
                return False
        except Exception:
            return False

        location = str(getattr(self.world.get_world_time(), 'to_string', lambda: '')()) if hasattr(self, 'world') else ""
        start_time = self.world.get_world_time().to_string()

        def _char_plans_from(players_list):
            out = []
            for ch in players_list:
                if not isinstance(ch, dict):
                    continue
                out.append({
                    "name": str(ch.get("name") or ""),
                    "intent": "(active)",
                    "thoughts": "",
                })
            return out

        char_plans = _char_plans_from(chars_data)
        timeline_store = self.world.get_timeline()
        all_intents = timeline_store.get("intents") or []

        # ── Compute intent status markers ──
        current_intent_keys: set = set()
        for d in all_intents:
            actor = str(d.get("actor", ""))
            atype = str(d.get("action_type", ""))
            desc = str(d.get("description", ""))[:80]
            current_intent_keys.add((actor, atype, desc))

        intent_statuses: Dict[str, str] = {}
        for d in all_intents:
            actor = str(d.get("actor", ""))
            atype = str(d.get("action_type", ""))
            desc = str(d.get("description", ""))[:80]
            k = (actor, atype, desc)
            if k not in self._prev_intent_keys:
                intent_statuses[k] = "new"
            else:
                intent_statuses[k] = "active"

        payload = {
            "character_plans": char_plans,
            "location": location,
            "cycle_start_time": start_time,
            "npcs": [],
            "declared_intents": all_intents,
            "intent_statuses": intent_statuses,
        }
        context_text = f"Location: {location}\nTime: {start_time}\nCharacters: {[p['name'] for p in char_plans]}"

        from scheduler.timeline import (
            Intent, SegmentScheduler, parse_duration_seconds, format_resolved_history_for_sm,
        )
        from world.time import WorldTime as _WT

        try:
            start_sec = _WT.parse(start_time).to_seconds()
        except Exception:
            start_sec = self.world.get_world_time().to_seconds()

        payload["cycle_start_seconds"] = start_sec

        carried_dicts = timeline_store.get("intents") or []
        now_sec = max(start_sec, self.world.get_world_time().to_seconds())
        player_names = [str(c.get("name", "")) for c in chars_data if isinstance(c, dict)]
        MAX_RESOLUTION_ROUNDS = 10
        all_actors = list(player_names)

        def _awareness_fn(observer, subject):
            return True

        def _dict_to_intent(d, *, anchor_sec=0):
            if not isinstance(d, dict) or not str(d.get("actor") or "").strip():
                return None
            if "_t_start_raw" in d or "_duration_raw" in d:
                d = dict(d)
                t_start = anchor_sec + parse_duration_seconds(d.pop("_t_start_raw", "0s"), default=0)
                duration_raw = d.pop("_duration_raw", "")
                t_end = None if not duration_raw else t_start + max(0, parse_duration_seconds(duration_raw, default=0))
                d["t_start"] = t_start
                d["t_end"] = t_end
            return Intent.from_dict(d)

        # Merge SM-authored intents (NPC intents, player intent corrections)
        from scene_manager.sm_tools import get_sm_intents
        sm_intents = get_sm_intents()

        intents_pool = []
        for d in carried_dicts:
            i = _dict_to_intent(d, anchor_sec=now_sec)
            if i is not None and i.status == "active":
                intents_pool.append(i)
        for d in all_intents:
            i = _dict_to_intent(d, anchor_sec=start_sec)
            if i is not None:
                intents_pool.append(i)
        for d in sm_intents:
            i = _dict_to_intent(d, anchor_sec=start_sec)
            if i is not None:
                # If this is a patch/modify, apply to matching intent in pool
                if d.get("_patched") and d.get("id"):
                    target_id = d["id"]
                    for existing in intents_pool:
                        if existing.id == target_id:
                            for k, v in d.items():
                                if k not in ("id", "_patched", "_origin"):
                                    setattr(existing, k, v)
                            break
                else:
                    intents_pool.append(i)

        horizon_cap = 86400
        horizon = now_sec + horizon_cap
        seen_action_ids = set()
        cycle_resolved_history = []
        npc_trigger_notes = []
        round_num = 0
        char_reinvokes = {}

        while round_num < MAX_RESOLUTION_ROUNDS:
            round_num += 1
            sched = SegmentScheduler(
                intents_pool, now=now_sec, horizon=horizon,
                perception_gate=self._sm.perception_check,
                awareness_fn=_awareness_fn, all_actors=all_actors,
                seen_action_ids=seen_action_ids,
            )
            result = sched.run()
            cycle_resolved_history.extend(result.resolved_history)
            seen_action_ids.update(e.intent_id for e in result.resolved_history if e.kind == "action" and e.intent_id)
            for w in result.warnings:
                pass
            intents_pool = result.carry_over_intents
            now_sec = result.next_cursor
            char_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a in player_names}
            npc_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a not in player_names}
            for a, notes in npc_reinvokes.items():
                npc_trigger_notes.extend(f"{a}: {n}" for n in notes)
            if not char_reinvokes:
                break
            reinvoke_time_str = _WT.from_seconds(now_sec).to_string()
            newly_declared_before = list(timeline_store.get("intents") or [])
            for cname, notes in char_reinvokes.items():
                combined_ctx = "\n".join(f"- {n}" for n in notes) or "New information available."
                try:
                    from cycle_runner import _run_single_player
                    _run_single_player(
                        app=self, player_name=cname, scene_data={},
                        location=location, start_time=start_time,
                        npcs=[], players=player_names,
                        gm_notice=combined_ctx,
                        world_time_override=reinvoke_time_str,
                    )
                except Exception as e:
                    print(f"  ✗ Re-invoke error for {cname}: {e}")
            timeline_store = self.world.get_timeline()
            newly_declared_after = list(timeline_store.get("intents") or [])
            fresh = newly_declared_after[len(newly_declared_before):]
            for d in fresh:
                i = _dict_to_intent(d, anchor_sec=now_sec)
                if i is not None:
                    intents_pool.append(i)

        if round_num >= MAX_RESOLUTION_ROUNDS:
            raise RuntimeError(
                f"SegmentScheduler: exhausted {MAX_RESOLUTION_ROUNDS} rounds at {location}"
            )

        consecutive_zero = int(timeline_store.get("consecutive_zero", 0))
        if now_sec <= start_sec and not cycle_resolved_history:
            consecutive_zero += 1
            if consecutive_zero >= 5:
                pass
            now_sec = start_sec + 30
        else:
            consecutive_zero = 0

        # Update statuses from resolved history (distinguish kind)
        for entry in cycle_resolved_history:
            actor = str(getattr(entry, "actor", ""))
            atype = str(getattr(entry, "action_type", ""))
            desc = str(getattr(entry, "description", ""))[:80]
            k = (actor, atype, desc)
            kind = str(getattr(entry, "kind", "action"))
            if kind == "prepared_reaction":
                intent_statuses[k] = "triggered"
            elif kind == "timeout":
                intent_statuses[k] = "timed_out"
            else:
                intent_statuses[k] = "completed"

        # Mark cancelled intents
        for d in all_intents:
            if str(d.get("status") or "").strip() == "cancelled":
                k = (str(d.get("actor","")), str(d.get("action_type","")), str(d.get("description",""))[:80])
                intent_statuses[k] = "cancelled"

        self._prev_intent_keys = current_intent_keys

        if cycle_resolved_history:
            self._sm.add_resolved_segments_to_buffer(cycle_resolved_history)
        buffer_ready = self._sm.is_session_buffer_ready() or self._sm.is_session_buffer_force()

        if not cycle_resolved_history and not char_reinvokes and not npc_trigger_notes:
            try:
                self.world.set_timeline({
                    "intents": [i.to_dict() for i in intents_pool],
                    "resolved_history": timeline_store.get("resolved_history", []),
                    "consecutive_zero": consecutive_zero,
                    "cursor": now_sec,
                })
            except Exception:
                pass
            return True

        if not buffer_ready and not end_session:
            try:
                self.world.set_timeline({
                    "intents": [i.to_dict() for i in intents_pool],
                    "resolved_history": (timeline_store.get("resolved_history", []) +
                                         [e.to_dict() for e in cycle_resolved_history])[-500:],
                    "consecutive_zero": consecutive_zero,
                    "cursor": now_sec,
                })
            except Exception:
                pass
            return True

        duration_sec = now_sec - start_sec
        cycle_end_time_str = _WT.from_seconds(now_sec).to_string()
        self.world.set_world_time(cycle_end_time_str)

        try:
            prior_history = timeline_store.get("resolved_history") or []
            merged_history = (prior_history + [e.to_dict() for e in cycle_resolved_history])[-500:]
            self.world.set_timeline({
                "intents": [i.to_dict() for i in intents_pool],
                "resolved_history": merged_history,
                "consecutive_zero": consecutive_zero,
                "cursor": now_sec,
            })
        except Exception:
            pass

        char_plans = _char_plans_from(chars_data)
        payload["character_plans"] = char_plans
        carry_over = timeline_store.get("intents") or []
        active_carry = [i for i in carry_over if isinstance(i, dict) and str(i.get("status") or "active") == "active"]
        payload["declared_intents"] = active_carry
        timeline_text = format_resolved_history_for_sm(cycle_resolved_history, start_sec)
        if npc_trigger_notes:
            timeline_text += "\n\nNPC/world triggers:\n" + "\n".join(f"  - {n}" for n in npc_trigger_notes)
        payload["timeline_text"] = timeline_text
        payload["computed_duration"] = f"{duration_sec}s"
        payload["cycle_end_time"] = cycle_end_time_str

        return True

    def run_full_session(self) -> None:
        """Run cycles until session buffer is ready, then end with narration."""
        for _ in range(50):
            run_cycle(self, None)
            if self._sm.is_session_buffer_ready() or self._sm.is_session_buffer_force():
                break
        self.end_session()

    def run_cycle(self, user_msg: Optional[HumanMessage] = None) -> None:
        """Execute one resolution cycle: bootstrap → characters → finalize."""
        run_cycle(self, user_msg)

    def begin_session(self) -> None:
        """Start a new session: SM + character contexts (§1.1)."""
        try:
            self._sm.begin_session()
        except Exception:
            pass
        try:
            from player.agent import begin_session
            begin_session()
        except Exception:
            pass
        self._session_active = True

    def end_session(self) -> None:
        """End session: publish narration, save to disk, clear memory."""
        # Publish final narration using the accumulated session context.
        # Do NOT call finalize_cycle — the scheduler already ran each cycle.
        # Building a fresh payload from the timeline would lose reinvoke intents.
        try:
            wt = self.world.get_world_time()
            time_str = wt.to_string() if hasattr(wt, 'to_string') else str(wt)
            context_text = f"Location: {time_str}\nTime: {time_str}"
            payload = {
                "character_plans": [],
                "location": time_str,
                "cycle_start_time": time_str,
                "declared_intents": [],
            }
            self._sm.run_narration(payload=payload, context_text=context_text)
        except Exception:
            pass
        try:
            self._sm.end_session()
        except Exception:
            pass
        try:
            from player.agent import end_session
            end_session()
        except Exception:
            pass
        try:
            from player.agent import set_sm_resolver
            set_sm_resolver(None)
        except Exception:
            pass
        self._session_active = False

    # ── Job registration ──

    def _register_scheduled_jobs(self) -> None:
        register_all_jobs(self._scheduler, self)

    # ── Scheduled job handlers (called by tick scheduler) ──

    def _run_character_reflection(self, *, character_name: str) -> None:
        """Run reflection for a character (scheduled job)."""
        try:
            from player.reflection import run_reflection
            run_reflection(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] reflection error for {character_name}: {e}")

    def _run_character_diary(self, *, character_name: str) -> None:
        """Run diary update for a character (scheduled job)."""
        try:
            from player.reflection import run_diary
            run_diary(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] diary error for {character_name}: {e}")

    def _run_character_review(self, *, character_name: str) -> None:
        """Run relationship review for a character (scheduled job)."""
        try:
            from player.relationship_review import run_relationship_review
            run_relationship_review(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] review error for {character_name}: {e}")

    # ── Clear cycle temp dir ──

    def _clear_cycle_temp_dir(self) -> None:
        tmp = self.world.game_root / "tmp_cycle"
        if tmp.exists():
            import shutil
            shutil.rmtree(str(tmp))
        tmp.mkdir(parents=True, exist_ok=True)


# Backward-compatible alias
GameOrchestrator = Orchestrator

# Backward-compatible method aliases
Orchestrator._cmd_load_preset = Orchestrator.load_preset
Orchestrator._maybe_create_world_setting = Orchestrator.ensure_world_setting
