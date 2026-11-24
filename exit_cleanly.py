"""Ending a one-shot run: the console may slow it down but never holds it.

A run that writes every line to a console is at the mercy of that console: a pipe
nobody drains, a terminal that has fallen behind, a task whose output is no longer
read — the write itself blocks, and it blocks *inside* the game, wherever the next
print happens to be. The same is true of the last moment: a process that has
finished its work can still refuse to die because the flush that would close the
console never returns.

Three pieces, each doing one thing:

* ``ConsoleWriter`` — console output through a bounded queue in a daemon thread.
  The file a run keeps is written synchronously and is always complete; the
  console is a convenience, so it may lag or drop lines and never blocks the run.
* ``dump_thread_stacks`` / ``arm_exit_probe`` — where every thread stands, and the
  same written down if the process is *still* alive N seconds after the session
  closed. Armed only once nothing but stopping is left, so a long summary call
  cannot make it speak; a normal end takes the probe with it and leaves no file.
* ``finish`` — give the console a bounded moment to drain, then end the process
  with ``os._exit``. Everything the run had to write is already on disk through
  closed files, and skipping the interpreter's teardown cannot lose it.

Why ``os._exit`` rather than ``sys.exit``: an atexit handler, a non-daemon thread
or a blocked flush is exactly what is being escaped, and ``sys.exit`` runs all of
them.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional


class ConsoleWriter:
    """Console output that cannot hold the caller.

    ``write`` returns as soon as the line is queued. A console that keeps up sees
    every line; one that does not costs the oldest lines, and the count is
    reported rather than hidden.
    """

    def __init__(self, target, *, limit: int = 4000) -> None:
        self._target = target
        self._limit = max(1, int(limit))
        self._condition = threading.Condition()
        self._queue: deque = deque()
        self._dropped = 0
        self._thread = threading.Thread(target=self._pump, name="console", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._condition:
            return self._dropped

    def write(self, text) -> None:
        if not text:
            return
        with self._condition:
            while len(self._queue) >= self._limit:
                # The console is behind. The run's record is the file, so the
                # oldest line here is the cheapest thing to lose.
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(text)
            self._condition.notify()

    def flush(self) -> None:
        """Nothing to do: the pump writes each line as it takes it."""

    def drain(self, seconds: float = 2.0) -> int:
        """Wait at most *seconds* for the console to catch up. Returns drops."""
        deadline = time.time() + max(0.0, float(seconds))
        with self._condition:
            while self._queue and time.time() < deadline:
                self._condition.wait(0.05)
            return self._dropped

    def _pump(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                text = self._queue.popleft()
            try:
                self._target.write(text)
                self._target.flush()
            except Exception:
                pass
            finally:
                with self._condition:
                    self._condition.notify_all()


def dump_thread_stacks(path) -> Path:
    """Write down every thread and the frames it is standing in."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    names = {t.ident: t.name for t in threading.enumerate()}
    lines = [f"# thread stacks: {datetime.now().isoformat()}",
             f"# threads: {', '.join(sorted(names.values()))}", ""]
    for ident, frame in sys._current_frames().items():
        lines.append(f"--- {names.get(ident, ident)}")
        lines.append("".join(traceback.format_stack(frame)))
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def arm_exit_probe(seconds: float = 15.0, logs_dir=None) -> threading.Thread:
    """Write the stacks if this process is still alive *seconds* from now.

    Returns the probe thread, for a caller that wants to watch it; nothing has to
    be joined — it is a daemon, and a normal end of the process takes it with it.
    """
    where = Path(logs_dir) if logs_dir else Path("logs")
    target = where / f"_exit_hang_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    def _watch() -> None:
        time.sleep(max(0.0, float(seconds)))
        try:
            dump_thread_stacks(target)
            # The console may be exactly what is blocked, so the file is the
            # record; the line is best effort only.
            sys.stderr.write(f"\n[exit] still alive {seconds:.0f}s after the close — "
                             f"stacks written to {target}\n")
        except Exception:
            pass

    thread = threading.Thread(target=_watch, name="exit-probe", daemon=True)
    thread.start()
    return thread


def finish(code: int = 0, grace_seconds: float = 2.0) -> None:
    """End the process, after at most *grace_seconds* of flushing the console."""
    def _drain() -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass

    worker = threading.Thread(target=_drain, name="exit-flush", daemon=True)
    worker.start()
    worker.join(max(0.0, float(grace_seconds)))
    os._exit(int(code))
