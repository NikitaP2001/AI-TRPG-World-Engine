"""Run one full game session: init + 3 cycles."""
import sys, os, time, json, io, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# This script starts a FRESH game: it clears game/ and loads a preset. It takes
# no arguments, and it has none to give: an option passed here is not a request
# it can honour, and ignoring one in silence — `--next`, meant for
# run_session.py — clears the campaign while its owner expects it to continue.
# So it refuses before anything at all is touched. Continuing an existing
# campaign, or returning to an earlier point of it, is run_session.py.
_unexpected = list(sys.argv[1:])
if _unexpected:
    sys.stderr.write(
        f"_run_session: this script starts a fresh game and takes no arguments "
        f"({', '.join(_unexpected)} given); nothing has been touched.\n"
        f"To continue the campaign: python run_session.py --next\n"
        f"To see or return to its points: python run_session.py --revert [N]\n"
    )
    raise SystemExit(2)

# ── Capture all output to file with proper UTF-8 encoding ──
# Bypasses PowerShell pipe encoding issues by writing from Python directly.
from datetime import datetime
_output_file = open(f"session_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                    "w", encoding="utf-8", buffering=1)
_original_stdout = sys.stdout
_original_stderr = sys.stderr

# The run's record is the file, written here and synchronously; the console is a
# convenience, and a convenience must not be able to hold the run (see
# exit_cleanly: a console that stops draining blocks whoever writes to it).
from exit_cleanly import ConsoleWriter  # noqa: E402

_console = ConsoleWriter(_original_stdout.buffer)

class _Tee:
    """Write to both console (utf-8) and file."""
    def write(self, text):
        _output_file.write(text)
        _output_file.flush()
        _console.write(text.encode("utf-8", errors="replace"))
    def flush(self):
        _output_file.flush()
sys.stdout = _Tee()
sys.stderr = _Tee()

from dotenv import load_dotenv
load_dotenv(override=True)
os.environ["LLM_WORLD_STREAM_ECHO"] = "1"

import shutil
from pathlib import Path
game = Path("game")
if game.exists():
    for item in list(game.iterdir()):
        if item.is_dir(): shutil.rmtree(str(item))
        else: item.unlink()
else:
    game.mkdir(parents=True)

from console_app import GameOrchestrator
app = GameOrchestrator()
app._cmd_load_preset("test")

t0 = time.time()
print(f"\n{'='*60}")
print("SESSION START")
print(f"{'='*60}")
try:
    app.run_full_session()
except Exception as e:
    print(f"\nEXCEPTION: {e}")
    import traceback; traceback.print_exc()

elapsed = time.time() - t0
print(f"time={elapsed:.1f}s")

print(f"\n{'='*60}")
print("CYCLES DONE — CLOSING THE SESSION")
print(f"{'='*60}")

# End session once — merges every memory, and covers what the merges dropped.
# This is the part that still runs after the last record is in the file: the
# scene manager's memory, then each character's, each reporting itself.
app.end_session()

# The session is closed and everything it wrote is on disk. From here the only
# task left is to stop — and a stop that waits on a console pipe, a thread or a
# library's teardown can wait forever, so it is bounded and, when it still does
# not come, written down instead of guessed at (exit_cleanly).
from exit_cleanly import arm_exit_probe, finish  # noqa: E402

arm_exit_probe(seconds=15)

# The session is over. A process that keeps running now would hold the caller
# waiting for work that will never happen again, so anything still alive is
# named before the process ends rather than left to be discovered by killing it.
alive = [t for t in threading.enumerate() if t is not threading.main_thread()]
for t in alive:
    print(f"  still running after the close: {t.name} (daemon={t.daemon})")

print(f"\n{'='*60}")
print("PROCESS EXIT")
print(f"{'='*60}")

sys.stdout = _original_stdout
sys.stderr = _original_stderr
try:
    _output_file.flush()
    _output_file.close()
except Exception:
    pass

# The record of the run is the closed file above; the console gets a moment, not
# a vote. The flush that used to stand here had no bound: a console that stops
# draining holds the process in that single call for good, before the exit below
# is ever reached.
_dropped = _console.drain(2.0)
if _dropped:
    _original_stderr.write(f"\n[console] {_dropped} line(s) dropped — the console "
                           f"could not keep up; the run's file has all of them\n")
    _original_stderr.flush()
finish(0)
