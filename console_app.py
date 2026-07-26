"""Console REPL for the LLM World simulation.

This module provides the interactive console interface.
All orchestration logic now lives in engine/orchestrator.py.

Entry points should import GameOrchestrator from engine.orchestrator.
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

from gm.full_history import gm_max_cycles_from_env, load_full_gm_messages, save_full_gm_messages, trim_full_gm_messages
from gm.history_injector import GMHistoryInjector
from gm.game_master import GameMaster, parse_game_master_json
from world.context import build_game_master_context_block as build_game_master_context, build_game_master_qa_context
from gm.tools import reset_cycle_lock, is_context_changed, signal_context_changed
from gm.injection_models import InjectionProfile, InjectionRule, InjectionEngine
from gm.injection_builders import build_world_meta
from scheduler import TickScheduler, Job, CycleCount, ParagraphCount, MemoryEntryCount
from scheduler.jobs import register_all_jobs
from player.agent import run_player_agent
from player.reflection import run_reflection
from memory_store import HistoryLimits, approx_token_count, limits_from_env, trim_history, load_history
from backup_storylines import record_turn_snapshot
from openrouter_langchain_logging import logs_enabled, stream_path
from stream_watchdog import (
    InvalidGMOutputError, StreamWatchdog, get_detected_invalid_pattern,
    clear_detected_invalid_pattern, _clear_watchdog_abort, log_retry_with_correction,
)
from bootstrap import initialize_game_dir
from scene import Scene
from world import World, WorldTime, _json_error_snippet
from world.time import WorldTime
from cycle_runner import run_cycle
from engine.gm_context import GMContextManager
from engine.orchestrator import Orchestrator  # ← the real implementation
from scene_manager import SceneManager
from world_manager import WorldManager
from console_app_utils import (
    _CYCLE_RECAPS_FILENAME, _GUI_STREAM_INPUT_LOCK,
    append_cycle_recap, backup_slug, is_invalid_gm_text_output,
    is_transient_assistant_error_message, strip_tool_error_pairs, log_pseudo_tool_markup_event,
)

# ======================================================================
# GameOrchestrator — thin delegation wrapper (legacy name kept for compat)
# ======================================================================

class GameOrchestrator(Orchestrator):
    """Console REPL orchestrator. Extends Orchestrator with REPL commands.

    All game logic is in engine.orchestrator.Orchestrator.
    This class only adds the interactive console loop and _cmd_ handlers.
    """
    _auto_scene_pause: bool = True

    # ==================================================================
    # REPL
    # ==================================================================

    def run(self) -> int:
        """Simple console REPL for the game loop.

        Handles user commands in a loop until EOF or 'quit'.
        """
        initialize_game_dir()
        self.world.ensure_initialized()

        print(f"\nLLM World — Console")
        print(f"Type 'continue' to run one cycle, 'quit' to exit.\n")

        from console_app_utils import _GUI_STREAM_INPUT_LOCK, _CYCLE_RECAPS_FILENAME

        try:
            self._scheduler.start()
        except Exception:
            pass

        while True:
            try:
                raw = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue

            args = raw.split()
            cmd = args[0].lower()

            # ── Quit ──
            if cmd in ("quit", "exit", "q"):
                break

            # ── Help ──
            elif cmd in ("help", "h", "?"):
                self._cmd_help()

            # ── Continue (run one cycle) ──
            elif cmd in ("continue", "c", ""):
                self._cmd_continue(args[1:])

            # ── Advance ──
            elif cmd == "advance":
                result = self._cmd_advance(args[1:])
                if result:
                    print(result[:2000])

            # ── Generate world ──
            elif cmd == "generate":
                result = self._cmd_generate(args[1:])
                if result:
                    print(result[:2000])

            # ── Save/load ──
            elif cmd == "save":
                self._cmd_save(args[1:] if len(args) > 1 else None)
            elif cmd == "load":
                path = args[1] if len(args) > 1 else None
                if path:
                    self._cmd_load(path)
                else:
                    print("Usage: load <path>")

            # ── Presets ──
            elif cmd == "presets":
                self._cmd_list_presets()
            elif cmd == "load_preset" or cmd == "preset":
                self._cmd_load_preset(args[1] if len(args) > 1 else "")

            # ── Stream echo toggle ──
            elif cmd == "stream":
                self.stream_echo = not self.stream_echo
                self._apply_stream_env()
                print(f"Stream echo: {'ON' if self.stream_echo else 'OFF'}")

            # ── Debug ──
            elif cmd == "debug":
                debug_mode = args[1].lower() in ("on", "1", "true") if len(args) > 1 else True
                os.environ["LLM_WORLD_DEBUG"] = "1" if debug_mode else "0"
                print(f"Debug mode: {'ON' if debug_mode else 'OFF'}")

            elif cmd == "rag":
                query = " ".join(args[1:])
                if not self._rag_search:
                    print("RAG search not available")
                    continue
                try:
                    results = self._rag_search(query)
                    print(f"RAG results: {results[:2000] if results else '(none)'}")
                except Exception as e:
                    print(f"RAG error: {e}")

            # ── Unknown ──
            else:
                print(f"Unknown command: {cmd}. Type 'help' for commands.")
        return 0

    # ==================================================================
    # Command handlers
    # ==================================================================

    def _cmd_help(self) -> None:
        print("""
Commands:
  continue, c       Run one cycle
  advance <years>   Advance world time
  generate          Generate world
  load_preset <n>   Load preset
  presets           List presets
  save <name>       Save game
  load <path>       Load game
  stream            Toggle stream echo
  debug on/off      Toggle debug mode
  help, h           Show this help
  quit, q           Exit
        """.strip())

    def _cmd_continue(self, args: List[str]) -> None:
        """Run one cycle."""
        cycle_count = 0
        max_cycles = 1
        if args:
            try:
                max_cycles = max(1, int(args[0]))
            except ValueError:
                pass

        while cycle_count < max_cycles:
            try:
                run_cycle(self, None)
                cycle_count += 1
            except InvalidGMOutputError as e:
                print(f"\n  ⚠ GM output rejected ({e}). Retrying...\n")
                continue
            except KeyboardInterrupt:
                print("\n  ⏹ Interrupted.")
                break
            except Exception as e:
                print(f"\n  ✗ Error: {e}")
                import traceback
                traceback.print_exc()
                break

    def _cmd_generate(self, args: list, debug: bool = False) -> str:
        """Generate the world using the WM generator pipeline."""
        from world_manager.tools import finalize_world_generation
        review_only = "-r" in args or "--review" in args
        try:
            result = finalize_world_generation.invoke({
                "review_only": review_only,
            })
            return json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
        except Exception as e:
            return f"Generation error: {e}"

    def _cmd_advance(self, args: list, debug: bool = False) -> str:
        """Advance the world simulation by the specified duration."""
        years_str = args[0] if args else "1"
        try:
            years = float(years_str)
        except ValueError:
            return f"Invalid years: {years_str}"
        try:
            from world_manager.entity_core import tick as engine_tick
            engine_tick(years=years)
            return f"Advanced {years} years"
        except Exception as e:
            return f"Advance error: {e}"

    def _cmd_save(self, name: Optional[str] = None) -> None:
        if not name:
            name = datetime.now(timezone.utc).strftime("save_%Y%m%d_%H%M%S")
        save_dir = self.world.game_root.parent / "saves" / name
        save_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(self.world.game_root), str(save_dir / "game"), dirs_exist_ok=True)
        print(f"Saved to {save_dir}")

    def _cmd_load(self, path: str) -> None:
        from world.io import read_json, write_json
        src = Path(path)
        if not src.exists():
            print(f"Save not found: {path}")
            return
        game_src = src / "game" if src.name != "game" else src
        if not game_src.exists():
            print(f"No game data in {path}")
            return
        shutil.rmtree(str(self.world.game_root))
        shutil.copytree(str(game_src), str(self.world.game_root), dirs_exist_ok=True)
        info_path = self.world.paths.info_json
        if info_path.exists():
            try:
                info = read_json(info_path)
                if isinstance(info, dict):
                    wt = info.get("time", "")
                    if wt:
                        print(f"Loaded: time={wt}, last_location={info.get('last_location', '?')}")
            except Exception:
                pass
        print(f"Loaded from {path}")

    def _cmd_list_presets(self) -> None:
        from world.io import read_json
        preset_dir = self.world.game_root / "setups"
        json_files = sorted(preset_dir.glob("*.json"))
        dirs = sorted(preset_dir.glob("*/"))
        all_presets = [d.name for d in dirs] + [f.stem for f in json_files if f.is_file()]
        if all_presets:
            print("Available presets:")
            for name in all_presets:
                print(f"  {name}")
        else:
            print("No presets found.")

    def _cmd_load_preset(self, name: str) -> None:
        """Load a preset — delegates to orchestrator."""
        if not name:
            self._cmd_list_presets()
            return
        super().load_preset(name)

    # ==================================================================
    # Delegation methods (for backward compat with turn_runner / tests)
    # ==================================================================

    def _bootstrap_next_scene(self) -> bool:
        return self.bootstrap_scene()

    def _finalize_cycle_if_ready(self) -> bool:
        return self.finalize_cycle()

    def _maybe_create_world_setting(self) -> bool:
        return self.ensure_world_setting()


# ======================================================================
# Standalone function for finalize logic
# (called from orchestrator.py's finalize_cycle during migration)
# ======================================================================

def _finalize_cycle_logic(orch: Orchestrator) -> bool:
    """Run the full resolution cycle — used by Orchestrator.finalize_cycle()."""
    scene = orch.world.get_scene()
    if not isinstance(scene, dict) or scene.get("state") != "active":
        return False

    chars = scene.get("players") or {}
    if not chars:
        return False

    all_acted = all((v or {}).get("acted") is True for v in chars.values())
    if not all_acted:
        return False

    location = str(scene.get("location") or "")
    start_time = str(scene.get("start_time") or "")
    npcs = scene.get("npcs") or []

    def _char_plans_from(chars_map):
        out = []
        for name, entry in chars_map.items():
            e = entry if isinstance(entry, dict) else {}
            out.append({
                "name": name,
                "intent": str(e.get("last_decision") or "").strip() or "(no decision)",
                "thoughts": str(e.get("last_thoughts") or "").strip(),
            })
        return out

    char_plans = _char_plans_from(chars)
    all_intents = scene.get("declared_intents") or []

    payload = {
        "character_plans": char_plans,
        "location": location,
        "cycle_start_time": start_time,
        "npcs": npcs,
        "declared_intents": all_intents,
    }
    context_text = f"Location: {location}\nTime: {start_time}\nCharacters: {[p['name'] for p in char_plans]}"

    if logs_enabled():
        print(f"[cycle] Resolving cycle at {location} with {len(char_plans)} characters")

    from scheduler.timeline import (
        Intent, SegmentScheduler, parse_duration_seconds, format_resolved_history_for_sm,
    )
    from world.time import WorldTime as _WT

    try:
        start_sec = _WT.parse(start_time).to_seconds()
    except Exception:
        start_sec = orch.world.get_world_time().to_seconds()

    timeline_store = orch.world.get_timeline()
    carried_dicts = timeline_store.get("intents") or []
    cursor = max(start_sec, int(timeline_store.get("cursor") or 0))

    DEFAULT_HORIZON_SECONDS = 900
    MAX_RESOLUTION_ROUNDS = 10
    all_actors = list(chars.keys()) + [str(n) for n in npcs if str(n).strip()]

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

    intents_pool = []
    for d in carried_dicts:
        i = _dict_to_intent(d, anchor_sec=cursor)
        if i is not None and i.status == "active":
            intents_pool.append(i)
    for d in all_intents:
        i = _dict_to_intent(d, anchor_sec=start_sec)
        if i is not None:
            intents_pool.append(i)

    horizon = cursor + DEFAULT_HORIZON_SECONDS
    seen_action_ids = set()
    cycle_resolved_history = []
    npc_trigger_notes = []
    round_num = 0
    char_reinvokes = {}

    while round_num < MAX_RESOLUTION_ROUNDS:
        round_num += 1
        sched = SegmentScheduler(
            intents_pool, now=cursor, horizon=horizon,
            perception_gate=orch._sm.perception_check,
            awareness_fn=_awareness_fn, all_actors=all_actors,
            seen_action_ids=seen_action_ids,
        )
        result = sched.run()
        cycle_resolved_history.extend(result.resolved_history)
        seen_action_ids.update(e.intent_id for e in result.resolved_history if e.kind == "action" and e.intent_id)
        for w in result.warnings:
            if logs_enabled():
                print(f"[trace] schema warning: {w}")

        intents_pool = result.carry_over_intents
        cursor = result.next_cursor

        char_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a in chars}
        npc_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a not in chars}
        for a, notes in npc_reinvokes.items():
            npc_trigger_notes.extend(f"{a}: {n}" for n in notes)

        if not char_reinvokes:
            break

        if logs_enabled():
            print(f"[cycle] Resolution round {round_num}: reinvoking {sorted(char_reinvokes)}")

        reinvoke_time_str = _WT.from_seconds(cursor).to_string()
        newly_declared_before = list(orch.world.get_scene().get("declared_intents") or [])

        for cname, notes in char_reinvokes.items():
            combined_ctx = "\n".join(f"- {n}" for n in notes) or "New information available."
            try:
                from cycle_runner import _run_single_player
                scene_now = orch.world.get_scene()
                _run_single_player(
                    app=orch, player_name=cname, scene_data=scene_now,
                    location=location, start_time=start_time,
                    npcs=npcs, players=list(chars.keys()),
                    gm_notice=combined_ctx,
                    world_time_override=reinvoke_time_str,
                )
            except Exception as e:
                print(f"  ✗ Re-invoke error for {cname}: {e}")

        scene = orch.world.get_scene()
        chars = scene.get("players") or chars
        newly_declared_after = list(scene.get("declared_intents") or [])
        fresh = newly_declared_after[len(newly_declared_before):]
        for d in fresh:
            i = _dict_to_intent(d, anchor_sec=cursor)
            if i is not None:
                intents_pool.append(i)

    if round_num >= MAX_RESOLUTION_ROUNDS:
        raise RuntimeError(
            f"SegmentScheduler: exhausted {MAX_RESOLUTION_ROUNDS} resolution rounds "
            f"at {location} — infinite reinvoke loop detected"
        )

    # ── Push resolved segments to cycle context buffer ──
    if cycle_resolved_history:
        orch._sm.add_resolved_segments_to_buffer(cycle_resolved_history)

    # ── Check buffer: ready to publish? ──
    buffer_ready = orch._sm.is_session_buffer_ready() or orch._sm.is_session_buffer_force()

    # ── STRUCTURAL PAUSE (no content, or not enough buffer yet) ──
    if not cycle_resolved_history and not char_reinvokes and not npc_trigger_notes:
        new_cursor = cursor + DEFAULT_HORIZON_SECONDS
        try:
            orch.world.set_timeline({
                "intents": [i.to_dict() for i in intents_pool],
                "resolved_history": timeline_store.get("resolved_history", []),
                "cursor": new_cursor,
                "horizon": new_cursor + DEFAULT_HORIZON_SECONDS,
            })
        except Exception as e:
            if logs_enabled():
                print(f"[trace] timeline persistence error: {e}")
        if logs_enabled():
            print(f"[cycle] Structural pause — T jumped +{DEFAULT_HORIZON_SECONDS}s to {new_cursor}")
        return True

    # Buffer not full yet — keep accumulating, jump T forward
    if not buffer_ready:
        new_cursor = cursor + DEFAULT_HORIZON_SECONDS
        try:
            orch.world.set_timeline({
                "intents": [i.to_dict() for i in intents_pool],
                "resolved_history": (timeline_store.get("resolved_history", []) +
                                     [e.to_dict() for e in cycle_resolved_history])[-500:],
                "cursor": new_cursor,
                "horizon": new_cursor + DEFAULT_HORIZON_SECONDS,
            })
        except Exception as e:
            if logs_enabled():
                print(f"[trace] timeline persistence error: {e}")
        if logs_enabled():
            print(f"[cycle] Buffer at {orch._sm.session_buffer_chars()} chars — not yet ready, structural pause")
        return True

    # ── PERSIST TIMELINE ──
    try:
        prior_history = timeline_store.get("resolved_history") or []
        merged_history = (prior_history + [e.to_dict() for e in cycle_resolved_history])[-500:]
        orch.world.set_timeline({
            "intents": [i.to_dict() for i in intents_pool],
            "resolved_history": merged_history,
            "cursor": cursor,
            "horizon": horizon,
        })
    except Exception as e:
        if logs_enabled():
            print(f"[trace] timeline persistence error: {e}")

    char_plans = _char_plans_from(chars)
    payload["character_plans"] = char_plans
    # Scene-declared intents + carry-over intents from timeline
    scene_intents = list(orch.world.get_scene().get("declared_intents") or [])
    carry_over = timeline_store.get("intents") or []
    active_carry = [i for i in carry_over if isinstance(i, dict) and str(i.get("status") or "active") == "active"]
    payload["declared_intents"] = scene_intents + active_carry
    timeline_text = format_resolved_history_for_sm(cycle_resolved_history, start_sec)
    if npc_trigger_notes:
        timeline_text += "\n\nNPC/world triggers:\n" + "\n".join(f"  - {n}" for n in npc_trigger_notes)
    payload["timeline_text"] = timeline_text

    # ── SM NARRATION ──
    narration_result = orch._sm.run_narration(payload=payload, context_text=context_text)

    # Clear cycle context buffer after publication
    orch._sm.clear_session_context_buffer()

    # ── Persist per-player personal narration ──
    personal_narratives = narration_result.get("personal_narration", {})
    if personal_narratives:
        from memory_store import append_message, limits_from_env
        limits = limits_from_env()
        for pname, pnarr in personal_narratives.items():
            if not pnarr.strip():
                continue
            pdir = orch.world.game_root / "players" / pname
            pdir.mkdir(parents=True, exist_ok=True)
            pfile = pdir / "messages.json"
            append_message(pfile, role="assistant", content=pnarr.strip(), limits=limits)

    narration_type = str(narration_result.get("type") or "").strip()
    if narration_type == "correction":
        print(f"  📞 SM correction: {narration_result.get('turn_insight', '')[:100]}")
        return False

    if narration_type == "end_scene":
        reason = str(narration_result.get("reason") or "").strip()
        end_narration = str(narration_result.get("narration") or "").strip()
        end_duration = str(narration_result.get("duration") or "").strip()
        next_location_hint = str(narration_result.get("next_location") or "").strip()
        if logs_enabled():
            print(f"[cycle] Scene ended: {reason}")
        if end_narration:
            print(f"\n{'='*60}\n📖 SCENE END\n{'='*60}\n{end_narration}\n{'='*60}\n")
        if next_location_hint:
            try:
                info = orch.world.get_info()
                info["next_scene_hint"] = next_location_hint
                from world.io import write_json
                write_json(orch.world.paths.info_json, info)
            except Exception:
                pass
        if end_duration:
            try:
                orch._scene.end_with_gm_output(
                    narration=end_narration, location=location,
                    cycle_duration=end_duration, keep_scene=False,
                )
            except ValueError:
                pass
        else:
            orch.world.clear_scene()
        return True

    narration = str(narration_result.get("narration") or "").strip()
    duration = str(narration_result.get("duration") or "").strip()
    if not duration:
        raise ValueError("SM narration returned no duration.")

    if narration:
        print(f"\n{'='*60}\n📖 CYCLE NARRATION\n{'='*60}\n{narration}\n{'='*60}\n")

    try:
        orch._scene.end_with_gm_output(
            narration=narration, location=location,
            cycle_duration=duration, keep_scene=True,
        )
        if logs_enabled():
            print(f"[cycle] Cycle finalized: {location}, duration={duration}, scene active")
        return True
    except ValueError as e:
        if logs_enabled():
            print(f"[cycle] Error finalizing cycle: {e}")
        return False
