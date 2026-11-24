"""Console REPL for the LLM World simulation.

This module provides the interactive console interface.
All orchestration logic now lives in engine/orchestrator.py.

Entry points should import GameOrchestrator from engine.orchestrator.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# The progress lines carry characters the Windows code page cannot spell, and a
# redirected stdout would otherwise raise in the middle of a step that has
# nothing to do with printing.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from stream_watchdog import InvalidGMOutputError
from bootstrap import initialize_game_dir
from cycle_runner import run_cycle
from engine.orchestrator import Orchestrator  # ← the real implementation

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

        try:
            self._scheduler.start()
        except Exception:
            pass

        # ── Bump session counter (persistent across restarts) ──
        try:
            self._scheduler.increment_session()
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
        """Run a full session (cycles until buffer ready, then end with narration)."""
        try:
            self.run_full_session()
        except InvalidGMOutputError as e:
            print(f"\n  GM output rejected ({e}). Retrying...\n")
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        except Exception as e:
            print(f"\n  Error: {e}")
            import traceback
            traceback.print_exc()

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
    # Delegation methods (for backward compat with earlier entry points)
    # ==================================================================

    def _bootstrap_next_scene(self) -> bool:
        return self.bootstrap_scene()

    def _finalize_cycle_if_ready(self) -> bool:
        return self.finalize_cycle()

    def _maybe_create_world_setting(self) -> bool:
        return self.ensure_world_setting()

