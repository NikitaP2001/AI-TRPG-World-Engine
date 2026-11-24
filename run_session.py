"""Run one session of the ongoing game, with numbered points to come back to.

The game is one folder — ``game/`` — and this script never resets it. What it
keeps is the states the campaign passed through, so a session can be played,
judged, and left behind without the campaign being lost:

    python run_session.py --next              take a point, then play a session
    python run_session.py --take              take a point and stop
    python run_session.py --revert            list the points of this game
    python run_session.py --revert 3          go back to point 3, and stop
    python run_session.py --revert 3 --next   go back to point 3 and play on

A point is the whole game as it stood before a session: ``saves/points/0003/``
holds a copy of ``game/`` and a manifest naming the world time it was taken at.
Numbers only grow and nothing is overwritten — a state left behind by a rewind
is kept under a new number and the listing shows it as the branch abandoned.

The one rule the tool follows: **no state is replaced without being kept
first.** Taking a point before playing, and taking one more before a rewind,
are the same rule seen twice.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

WORKSPACE = Path(__file__).resolve().parent
GAME = WORKSPACE / "game"
POINTS = WORKSPACE / "saves" / "points"
LINE = POINTS / "line.json"
LOGS = WORKSPACE / "logs" / "sessions"

#: What a run is doing at this moment, rather than what the game is. A point
#: that carried them would restore a half-step of somebody else's cycle.
SCRATCH = {"tmp_cycle", "tmp_turn"}


# ── the game as it stands ────────────────────────────────────────────────

def _read_json(path: Path) -> Dict:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def world_time(game: Optional[Path] = None) -> str:
    """The moment the world stands at, as the game writes it down."""
    root = game or GAME
    return str(_read_json(root / "world" / "info.json").get("time") or "?")


def session_count(game: Optional[Path] = None) -> int:
    """How many sessions this world has lived, by its own count."""
    root = game or GAME
    try:
        return int(_read_json(root / "scene_manager_meta.json").get("session_count") or 0)
    except Exception:
        return 0


def _copy_game(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if item.name in SCRATCH:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(target), dirs_exist_ok=True)
        else:
            shutil.copy2(str(item), str(target))


def _has_game() -> bool:
    return (GAME / "world" / "info.json").exists()


# ── the points ───────────────────────────────────────────────────────────

def points() -> List[Dict]:
    """Every point of this game, oldest first, by number."""
    if not POINTS.is_dir():
        return []
    found: List[Dict] = []
    for entry in POINTS.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        manifest = _read_json(entry / "point.json")
        found.append({
            "point": int(entry.name),
            "dir": entry,
            "world_time": str(manifest.get("world_time") or "?"),
            "taken_at": str(manifest.get("taken_at") or "?"),
            "session_count": manifest.get("session_count"),
            "label": str(manifest.get("label") or ""),
        })
    return sorted(found, key=lambda item: item["point"])


def take_point(label: str = "") -> Dict:
    """Write the game as it stands now under the next number."""
    if not _has_game():
        raise SystemExit("no game to keep: game/world/info.json is missing")
    existing = points()
    number = existing[-1]["point"] + 1 if existing else 1
    target = POINTS / f"{number:04d}"
    if target.exists():
        raise SystemExit(f"point {number} already stands at {target}")
    _copy_game(GAME, target / "game")
    manifest = {
        "point": number,
        "taken_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "world_time": world_time(),
        "session_count": session_count(),
        "label": label,
    }
    (target / "point.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"point {number} kept — world time {manifest['world_time']}, "
          f"session {manifest['session_count']}"
          + (f", {label}" if label else ""))
    return manifest


def show_points() -> None:
    found = points()
    if not found:
        print(f"no points yet ({POINTS})")
        print("`--next` keeps one before it plays; `--take` keeps one and stops.")
        return
    line_at = _read_json(LINE).get("at")
    line_at = int(line_at) if isinstance(line_at, int) else None
    print(f"{'#':>4}  {'world time':<22} {'taken':<20} {'session':>7}  note")
    for item in found:
        note = item["label"]
        if line_at is not None:
            if item["point"] == line_at:
                note = "the line ends here" + (f" — {note}" if note else "")
            elif item["point"] > line_at:
                note = "abandoned branch" + (f" — {note}" if note else "")
        taken = item["taken_at"][:19].replace("T", " ")
        print(f"{item['point']:>4}  {item['world_time']:<22} {taken:<20} "
              f"{str(item['session_count']):>7}  {note}".rstrip())


def restore(number: int, label: str = "") -> None:
    """Put the game back to a point, keeping the state being left behind.

    The state on disk is not what the campaign is going to be — it is what the
    last session made of it, and a reader may still want it. So it is kept
    under a new number before the point is laid over it, and the listing says
    which branch went unplayed.
    """
    found = {item["point"]: item for item in points()}
    if number not in found:
        available = ", ".join(str(p["point"]) for p in points()) or "none"
        raise SystemExit(f"no point {number} in this game (points: {available})")
    source = found[number]["dir"] / "game"
    if not source.is_dir():
        raise SystemExit(f"point {number} holds no game folder: {source}")
    left_label = f"left behind when returning to point {number}"
    if label:
        left_label = f"{left_label} — {label}"
    left = take_point(left_label) if _has_game() else None
    for item in sorted(GAME.iterdir()):
        if item.is_dir():
            shutil.rmtree(str(item))
        else:
            item.unlink()
    _copy_game(source, GAME)
    LINE.parent.mkdir(parents=True, exist_ok=True)
    LINE.write_text(json.dumps({
        "at": number,
        "reverted_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "left_as": left["point"] if left else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"back at point {number} — world time {world_time()}, "
          f"session {session_count()}")
    if left:
        print(f"what stood on disk is kept as point {left['point']}")


# ── playing ──────────────────────────────────────────────────────────────

def play(after_point: Optional[int] = None) -> int:
    """Play one session on the game that is on disk.

    Nothing is reset and no preset is loaded: the world on disk is the world
    that continues, and the gates the framework keeps for itself
    (`initialized.json`) stop a world that exists from being built twice.
    """
    from dotenv import load_dotenv

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOGS.mkdir(parents=True, exist_ok=True)
    name = f"{stamp}_point{after_point:04d}.txt" if after_point else f"{stamp}.txt"
    output = open(str(LOGS / name), "w", encoding="utf-8", buffering=1)
    console_out, console_err = sys.stdout, sys.stderr

    # The record of the run is the file; the console is a convenience, and a
    # console that stops draining must not be able to hold the run (the same
    # reason _run_session.py writes through ConsoleWriter).
    from exit_cleanly import ConsoleWriter

    console = ConsoleWriter(console_out.buffer)

    class _Tee:
        def write(self, text):
            output.write(text)
            output.flush()
            console.write(text.encode("utf-8", errors="replace"))

        def flush(self):
            output.flush()

    sys.stdout = _Tee()
    sys.stderr = _Tee()

    load_dotenv(override=True)
    os.environ["LLM_WORLD_STREAM_ECHO"] = "1"

    from console_app import GameOrchestrator

    code = 0
    print(f"\n{'=' * 60}")
    print(f"SESSION — world time {world_time()}, session {session_count()}")
    if after_point:
        print(f"continuing from point {after_point}")
    print(f"log: {LOGS / name}")
    print(f"{'=' * 60}")
    try:
        app = GameOrchestrator()
        app.run_full_session()
        app.end_session()
    except Exception as error:  # noqa: BLE001 — the report is the record
        code = 1
        import traceback

        print(f"\nEXCEPTION: {error}")
        traceback.print_exc()
    print(f"\n{'=' * 60}")
    print(f"SESSION CLOSED — world time {world_time()}, session {session_count()}")
    print(f"{'=' * 60}")

    # Everything the session had to say is in the closed file. From here the
    # only task left is to stop, and a stop that waits on a console pipe or on
    # somebody's non-daemon thread can wait for good — so it is bounded, and
    # where the process still stands, that is written down rather than guessed
    # at (exit_cleanly).
    from exit_cleanly import arm_exit_probe, finish

    arm_exit_probe(seconds=15)
    import threading

    alive = [t for t in threading.enumerate() if t is not threading.current_thread()]
    for thread in alive:
        print(f"  still running after the close: {thread.name} (daemon={thread.daemon})")

    sys.stdout, sys.stderr = console_out, console_err
    try:
        output.flush()
        output.close()
    except Exception:
        pass
    dropped = console.drain(2.0)
    if dropped:
        console_err.write(f"\n[console] {dropped} line(s) dropped — the console "
                          f"could not keep up; the run's file has all of them\n")
        console_err.flush()
    finish(code)
    return code


# ── the command ──────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_session",
        description="Play the next session of the game, or go back to a point of it.",
        epilog="A fresh game is _run_session.py: it clears game/ and loads a preset, "
               "and takes no arguments. This script never clears anything.",
    )
    parser.add_argument(
        "--next", action="store_true",
        help="keep a point and play one session",
    )
    parser.add_argument(
        "--take", action="store_true",
        help="keep a point of the game as it stands and stop",
    )
    parser.add_argument(
        "--revert", nargs="?", const=-1, type=int, metavar="N",
        help="with a number, put the game back to point N; alone, list the points",
    )
    parser.add_argument(
        "--label", default="", metavar="TEXT",
        help="a note to write on the point this call keeps",
    )
    args = parser.parse_args(argv)

    if args.revert is None and not args.next and not args.take:
        parser.print_help()
        return 0

    if args.take and (args.next or args.revert is not None):
        parser.error("--take is a point and a stop: combine it with neither "
                     "--next nor --revert")

    if args.revert == -1:
        show_points()
        return 0

    keep_before_play = args.next
    if args.revert is not None and args.revert >= 0:
        restore(args.revert, args.label)
        # The point restored *is* the state the session starts from, so a
        # session asked to play now needs no second copy of it.
        keep_before_play = False

    if args.take:
        take_point(args.label)
        return 0

    if keep_before_play:
        kept = take_point(args.label)
        return play(after_point=kept["point"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
