#!/usr/bin/env python3
"""Testbed runner: run a scenario for N turns with timeout, then evaluate."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTBED_ROOT = REPO_ROOT / "testbed"


def _load_config(scenario_name: str) -> dict:
    path = TESTBED_ROOT / "scenarios" / scenario_name / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"Scenario config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _setup_game_dir(scenario_name: str, run_dir: Path) -> Path:
    """Copy scenario setup/ into run_dir/game/."""
    setup = TESTBED_ROOT / "scenarios" / scenario_name / "setup"
    game_dir = run_dir / "game"
    if game_dir.exists():
        shutil.rmtree(game_dir)
    shutil.copytree(setup, game_dir)
    return game_dir


def _save_turn_snapshot(run_dir: Path, turn: int):
    """Copy game/ and logs/ into run_dir/backups/turn_N."""
    backup_dir = run_dir / "backups" / f"turn_{turn:04d}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Copy game state
    game_dir = run_dir / "game"
    if game_dir.exists():
        shutil.copytree(game_dir, backup_dir / "game", dirs_exist_ok=True)

    # Copy logs
    logs_dir = REPO_ROOT / "logs"
    if logs_dir.exists():
        shutil.copytree(logs_dir, backup_dir / "logs", dirs_exist_ok=True)

    # Copy stream.txt
    stream = REPO_ROOT / "logs" / "stream.txt"
    if stream.exists():
        shutil.copy2(stream, backup_dir / "stream.txt")


def _turn_count(game_dir: Path) -> int:
    """Count completed turns from story.json."""
    story = game_dir / "world" / "story.json"
    if not story.exists():
        return 0
    try:
        arcs = json.loads(story.read_text(encoding="utf-8"))
        if not isinstance(arcs, list) or not arcs:
            return 0
        ongoing = arcs[0].get("ongoing_paragraph", {})
        turns = ongoing.get("turns", [])
        return len(turns)
    except Exception:
        return 0


def _run_turn_with_timeout(
    cmd: list[str],
    timeout_sec: int,
    cwd: Path,
    env: dict,
) -> bool:
    """Run one invoke_once command with timeout. Returns True if it completed."""

    def _target():
        try:
            subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            print(f"  [runner] Turn timed out after {timeout_sec}s", file=sys.stderr)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec + 5)  # extra grace
    return thread.is_alive() is False


def _run_scenario(scenario_name: str, max_turns: int, max_turn_sec: int, run_dir: Path):
    """Execute the main game loop."""
    game_dir = _setup_game_dir(scenario_name, run_dir)

    env = os.environ.copy()
    env["LLM_WORLD_TEST_MODE"] = "1"
    # Point game to our setup
    env["LLM_WORLD_GAME_DIR"] = str(game_dir)

    print(f"[runner] Starting scenario: {scenario_name}")
    print(f"[runner] Max turns: {max_turns}, turn timeout: {max_turn_sec}s")
    print(f"[runner] Run dir: {run_dir}")

    turn_count = 0
    stalled = 0
    stall_max = 3

    for turn_idx in range(1, max_turns + 1):
        print(f"\n[runner] === Turn {turn_idx}/{max_turns} ===")

        completed = _run_turn_with_timeout(
            cmd=[sys.executable, "-c", "from console_app import ConsoleApp; app=ConsoleApp(); app._auto_advance_until_turn_finalized()"],
            timeout_sec=max_turn_sec,
            cwd=REPO_ROOT,
            env=env,
        )

        current_turns = _turn_count(game_dir)
        if current_turns > turn_count:
            turn_count = current_turns
            stalled = 0
            print(f"[runner] Turn completed. Total: {turn_count}")
        else:
            stalled += 1
            print(f"[runner] No progress (stalled {stalled}/{stall_max})")
            if stalled >= stall_max:
                print("[runner] Too many stalled attempts — aborting")
                break

        _save_turn_snapshot(run_dir, turn_idx)

    print(f"\n[runner] Finished after {turn_count} turns (target {max_turns})")
    return turn_count


def main():
    parser = argparse.ArgumentParser(description="Testbed runner")
    parser.add_argument("scenario", help="Scenario name (folder under scenarios/)")
    parser.add_argument("--max-turns", type=int, default=None, help="Override max_turns")
    parser.add_argument("--run-id", default=None, help="Custom run ID (default: auto)")

    args = parser.parse_args()
    scenario = args.scenario
    config = _load_config(scenario)
    run_cfg = config.get("run", {})

    max_turns = args.max_turns or run_cfg.get("max_turns", 30)
    max_turn_sec = run_cfg.get("max_turn_duration_sec", 30)
    max_wall_min = run_cfg.get("max_run_wall_clock_min", 45)

    run_id = args.run_id or f"{scenario}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    results_root = TESTBED_ROOT / "report" / "results"
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    start_wall = time.time()

    try:
        turns_done = _run_scenario(scenario, max_turns, max_turn_sec, run_dir)

        wall_elapsed = time.time() - start_wall
        print(f"\n[runner] Wall clock: {wall_elapsed:.0f}s, turns: {turns_done}")

        if wall_elapsed > max_wall_min * 60:
            print(f"[runner] WARNING: exceeded max wall clock ({max_wall_min} min)")

        # Save run metadata
        meta = {
            "scenario": scenario,
            "run_id": run_id,
            "turns_completed": turns_done,
            "turns_target": max_turns,
            "wall_clock_sec": int(wall_elapsed),
            "completed": True,
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"\n[runner] Results: {run_dir}")
        print("[runner] Ready for agent evaluation.")

    except KeyboardInterrupt:
        print("\n[runner] Interrupted by user")
        sys.exit(1)


if __name__ == "__main__":
    main()
