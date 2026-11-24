"""Playing a session from the GUI, without the window waiting on it.

The engine is played exactly as it is played from the command line — the same
`run_session.py`, the same points, the same session — because it is the same
program: this runs it as a child process and watches what it says. Nothing about
the game is duplicated here, and a session that ends badly cannot take the window
with it.

What the watcher keeps is what a running session can be asked: which phase it is
in, which agent is working, what the cycle reached, how full the session's buffer
is, who has already had his turn, and which tools have been called. All of it is
read from the session's own output, so the window shows what happened rather than
what was expected.

A session can also be stopped, and stopping is two acts: the child is ended, and
the point the run took is laid back over the game. A session left half-played is
not a state anything may be built on — a cycle that never resolved, an intent
pool nobody closed — so the window offers no way to leave one behind. Both acts
happen on their own threads: the frame only asks what has arrived.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE = Path(__file__).resolve().parent.parent
POINTS = WORKSPACE / "saves" / "points"

#: How long a stopped session is given to end on its own before it is killed.
_STOP_GRACE = 20.0


def _points_on_disk() -> List[int]:
    """The numbers of the points this game keeps, oldest first.

    A point is a number with its manifest beside it: a folder whose manifest was
    never written is a copy that did not finish, and there is nothing complete in
    it to return to.
    """
    try:
        return sorted(int(p.name) for p in POINTS.iterdir()
                      if p.is_dir() and p.name.isdigit()
                      and (p / "point.json").exists())
    except Exception:
        return []


def _revert_argv(number: int) -> List[str]:
    """The game's own way back to a point, as a child of this window."""
    return [sys.executable, "-u", str(WORKSPACE / "run_session.py"),
            "--revert", str(number)]

#: What one line of the session's output says, in the reader's words. The
#: engine's lines are what they are; naming them for the window is this
#: module's business, and a line nothing here recognises is kept as it stands.
_CYCLE = re.compile(r"^\s*cycle (\d+)\b.*buffer (\d+)\s*/\s*(\d+)")
_PLAYING = re.compile(r"^SESSION — world time (.+?), session (\d+)")
_CLOSED = re.compile(r"^SESSION CLOSED")
_INVOKING = re.compile(r"^\s*Invoking (\d+) player\(s\) without active intent: (.*)$")
_REVIEW = re.compile(r"^\s*SM resolve cycle: (\d+) player\(s\), (\d+) intent\(s\)")
_REVIEW_BEGUN = re.compile(r"^\s*\[SM resolve_cycle\] start — (\d+) intents, (\d+) players")
_REVIEW_CLOSED = re.compile(r"^\s*\[SM resolve_cycle\] done")
_DELIVERED = re.compile(r"^\s*Knowledge delivered: (\d+) piece")
_BOOTSTRAP = re.compile(r"^\s*SM bootstrap: (\d+) character\(s\)")
_CHAR_HISTORY = re.compile(r"^\[trace\] character history loaded: (.+?) \(msgs=(\d+)\)")
_DISPOSITION = re.compile(r"^\[trace\] (.+?): disposition established")
_DECLARES = re.compile(r"^\[trace\] (.+?) declares?: (.+)$")
_DECLARED = re.compile(r"^\[trace\] (.+?) declared: (.+)$")
_CALLED = re.compile(r"^\[trace\] character called tool (\w+)")
_USED = re.compile(r"^\[trace\] (.+?) used (\w+): (.*)$")
_REACT = re.compile(r"^\[trace\] react_loop: calling (\w+)")
_REINVOKED = re.compile(r"^\[trace\] (.+?): reinvoked")
_OBSERVES = re.compile(r"^\[trace\] (.+?) observes?: (.*)$")
_RECALLS = re.compile(r"^\[trace\] (.+?) recalls?: (.*)$")
_EVENT = re.compile(r"^\s*\[event_log\] (\w+): (.*)$")
_NARRATIVE = re.compile(r"^\s*\[SM session_narrative\] recorded — (\d+) characters")
_CLOSING = re.compile(r"^\s*Closing the memories")
_MEMORY_CLOSED = re.compile(r"^\s*(.+?): memory closed \((\d+) record")
_COLD = re.compile(r"^\[cycle\] cold-path waking (gm|wm)(?:.*?): (.*)$")
_FAILED = re.compile(r"^(EXCEPTION|Traceback|\[cycle\] .*failed)")
_SKIP = re.compile(r"^\[DEBUG\]|^$|^=+$|^\{|^\s+\"|^\s*[}\]]+,?\s*$")

_AGENT_NAMES = {"gm": "Мастер мира (GM)", "wm": "Хранитель мира (WM)",
                "sm": "Мастер сцены (SM)"}


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class SessionRun:
    """One run of the game's own session command, watched as it happens."""

    def __init__(self, argv: List[str], *, title: str = "") -> None:
        self.argv = list(argv)
        self.title = title or " ".join(self.argv)
        self.state = "idle"          # idle → running → done | failed
        self.started_at = 0.0
        self.finished_at = 0.0
        self.exit_code: Optional[int] = None
        self.log: List[str] = []     # clean phrases, oldest first
        self.other: List[str] = []   # lines nothing here names, for a look
        self.trouble: List[str] = []  # whatever said it went wrong
        self.who: str = ""           # the agent working right now
        self.phase: str = "ожидание"
        self.cycle: int = 0
        self.buffer: tuple = (0, 0)
        self.acted: Dict[str, str] = {}
        self.tools: List[str] = []
        self.world_time: str = ""
        self.session_number: str = ""
        self.log_path: str = ""
        self.cancelled = False         # stopped from the window, not by itself
        self.point_before = 0          # the last point standing when this run began
        self.reverted_to: Optional[int] = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._cancel: Optional[threading.Thread] = None
        self._ended = False

    # ── running it ───────────────────────────────────────────────────

    def start(self) -> None:
        """Start the child process and the thread that reads what it says."""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(WORKSPACE)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # The point this run will take is the one after the last standing now:
        # that is the state a stop has to put back.
        standing = _points_on_disk()
        self.point_before = standing[-1] if standing else 0
        self._process = subprocess.Popen(
            [sys.executable, "-u", *self.argv],
            cwd=str(WORKSPACE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.state = "running"
        self.started_at = time.time()
        self.phase = "запуск"
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._queue.put(line.rstrip("\n"))
        self._queue.put("")  # the end of the stream

    # ── what it said since the last look ─────────────────────────────

    def poll(self) -> None:
        """Take everything the session has said since the last frame.

        Nothing here waits: the window asks what has arrived and draws. Waiting
        on the child — for its exit, for a file, for anything — belongs to the
        threads that started it.
        """
        while True:
            try:
                line = self._queue.get_nowait()
            except queue.Empty:
                break
            if line == "":
                self._ended = True
                continue
            self._take(line)
        if self._ended:
            self._finish()

    def _finish(self) -> None:
        code = None
        if self._process is not None:
            code = self._process.poll()
        self.exit_code = code
        if self.cancelled:
            # The stop owns this run's ending: it is ending the child and putting
            # the game back, and it says what came of it when it is done.
            return
        if code is None:
            return  # not reaped yet; the next frame asks again
        self.finished_at = time.time()
        self.state = "done" if code == 0 else "failed"
        if self.state == "failed" and code:
            self.trouble.append(f"Сессия завершилась с кодом {code}.")
        self.phase = "сессия закрыта" if self.state == "done" else "остановлено"

    def stop(self) -> None:
        """Stop the session and put back the point it started from.

        A session left half-played is not a state the game can stand on: a cycle
        that never resolved, an intent pool nobody closed, records of an hour the
        world did not reach. So stopping is two acts and not one — end the child,
        then lay the point this run took back over the game — and both happen off
        the drawing thread, because the window has to keep drawing while it
        happens.
        """
        if self.state != "running" or self._cancel is not None:
            return
        self.cancelled = True
        self.state = "stopping"
        self.phase = "останавливаю сессию"
        self._say("Останавливаю сессию: она не будет доиграна.")
        self._cancel = threading.Thread(target=self._stop_and_revert, daemon=True)
        self._cancel.start()

    def _stop_and_revert(self) -> None:
        process = self._process
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=_STOP_GRACE)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
        target = next((n for n in _points_on_disk() if n > self.point_before), None)
        if target is None:
            # Stopped before its point was taken: nothing was written over the
            # game, and there is nothing to put back.
            self.phase = "сессия остановлена"
            self._say("Точка не успела взяться — игра не тронута.")
            self.state = "stopped"
            self.finished_at = time.time()
            return
        self.phase = f"возвращаю игру к точке {target}"
        self._say(f"Возвращаю игру к точке {target} — к той, с которой сессия началась.")
        ok, said = self._revert(target)
        self._say(said)
        if ok:
            self.reverted_to = target
            self.phase = f"игра возвращена к точке {target}"
            self.state = "stopped"
        else:
            self.phase = f"возврат к точке {target} не удался"
            self.trouble.append(said)
            self.state = "failed"
        self.finished_at = time.time()

    def _revert(self, number: int) -> "tuple[bool, str]":
        """Run the game's own return to a point, and report what it said."""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(WORKSPACE)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            done = subprocess.run(
                _revert_argv(number), cwd=str(WORKSPACE), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=300,
            )
        except Exception as error:  # noqa: BLE001 — said, not swallowed
            return False, f"Возврат к точке {number} не пошёл: {error}"
        if done.returncode == 0:
            return True, f"Игра возвращена к точке {number}."
        tail = [line for line in (done.stdout or "").splitlines() if line.strip()][-1:]
        return False, (f"Возврат к точке {number} завершился кодом {done.returncode}"
                       + (": " + " ".join(tail) if tail else ""))

    @property
    def elapsed(self) -> float:
        end = self.finished_at or (time.time() if self.running else 0.0)
        return max(0.0, end - self.started_at) if self.started_at else 0.0

    @property
    def running(self) -> bool:
        """Is this run still the window's to watch — playing or stopping."""
        return self.state in ("running", "stopping")

    def buffer_share(self) -> float:
        """How full the session's buffer is, of what closes the session."""
        held, limit = self.buffer
        return min(1.0, held / limit) if limit else 0.0

    # ── reading one line ─────────────────────────────────────────────

    def _say(self, phrase: str) -> None:
        self.log.append(phrase)
        if len(self.log) > 500:
            del self.log[:100]

    def _take(self, line: str) -> None:
        if _PLAYING.match(line):
            found = _PLAYING.match(line)
            self.world_time, self.session_number = found.group(1), found.group(2)
            self._say(f"Сессия {self.session_number} — мир на {self.world_time}.")
            return
        if _CLOSED.match(line):
            self.phase = "сессия закрыта"
            return
        if _BOOTSTRAP.match(line):
            found = _BOOTSTRAP.match(line)
            self.phase = "открытие мира"
            self._say(f"Мир открывается: {found.group(1)} персонажам даётся то, "
                      f"что они видят.")
            return
        if _INVOKING.match(line):
            found = _INVOKING.match(line)
            names = [n.strip() for n in found.group(2).split(",") if n.strip()]
            self.phase = "планирование"
            for name in names:
                self.acted.setdefault(name, "вызван")
            self._say("Вызваны: " + ", ".join(names) + ".")
            return
        if _REVIEW.match(line):
            found = _REVIEW.match(line)
            self.phase = "разбор заявок"
            self._say(f"Мастер сцены разбирает заявки: {found.group(1)} персонаж(ей), "
                      f"{found.group(2)} намерени(й).")
            return
        if _REVIEW_BEGUN.match(line):
            self.phase = "разбор заявок"
            self.who = "Мастер сцены (SM)"
            return
        if _REVIEW_CLOSED.match(line):
            self._say("Разбор закончен.")
            return
        if _DELIVERED.match(line):
            found = _DELIVERED.match(line)
            self.phase = "отдача знания"
            self._say(f"Персонажам отдано то, что они узнали: {found.group(1)} шт.")
            return
        if _CYCLE.match(line):
            found = _CYCLE.match(line)
            self.cycle = int(found.group(1))
            self.buffer = (int(found.group(2)), int(found.group(3)))
            self.phase = "цикл идёт"
            return
        if _CHAR_HISTORY.match(line):
            found = _CHAR_HISTORY.match(line)
            self.who = found.group(1)
            self.acted[found.group(1)] = "читает себя"
            return
        if _DISPOSITION.match(line):
            found = _DISPOSITION.match(line)
            self.who = found.group(1)
            self.acted[found.group(1)] = "думает"
            return
        if _DECLARES.match(line):
            found = _DECLARES.match(line)
            self.who = found.group(1)
            self.acted[found.group(1)] = "объявляет"
            self._say(f"{found.group(1)}: {_short(found.group(2), 110)}")
            return
        if _DECLARED.match(line):
            found = _DECLARED.match(line)
            self.acted[found.group(1)] = "объявил"
            self._say(f"{found.group(1)} объявил: {_short(found.group(2), 110)}")
            return
        if _CALLED.match(line):
            found = _CALLED.match(line)
            self.tools.append(f"{self.who or 'персонаж'}: {found.group(1)}")
            return
        if _USED.match(line):
            found = _USED.match(line)
            self.tools.append(f"{found.group(1)}: {found.group(2)}")
            return
        if _REACT.match(line):
            found = _REACT.match(line)
            self.tools.append(f"{self.who or 'агент'}: {found.group(1)}")
            return
        if _REINVOKED.match(line):
            found = _REINVOKED.match(line)
            self.acted[found.group(1)] = "вызван снова"
            self._say(f"{found.group(1)} вызван снова — он узнал что-то новое.")
            return
        if _OBSERVES.match(line):
            found = _OBSERVES.match(line)
            self.acted[found.group(1)] = "смотрит"
            self._say(f"{found.group(1)} смотрит: {_short(found.group(2), 110)}")
            return
        if _RECALLS.match(line):
            found = _RECALLS.match(line)
            self.acted[found.group(1)] = "вспоминает"
            self._say(f"{found.group(1)} вспоминает: {_short(found.group(2), 110)}")
            return
        if _EVENT.match(line):
            found = _EVENT.match(line)
            self._say(f"Событие записано: {_short(found.group(2), 110)}")
            return
        if _NARRATIVE.match(line):
            found = _NARRATIVE.match(line)
            self.phase = "запись рассказа о сессии"
            self._say(f"Мастер сцены записал рассказ о сессии ({found.group(1)} симв.).")
            return
        if _CLOSING.match(line):
            self.phase = "закрытие памяти"
            self._say("Сессия закрывается: записи входят в память.")
            return
        if _MEMORY_CLOSED.match(line):
            found = _MEMORY_CLOSED.match(line)
            self._say(f"Память закрыта: {found.group(1)} — {found.group(2)} записей.")
            return
        if _COLD.match(line):
            found = _COLD.match(line)
            self._say(f"Пробуждён {_AGENT_NAMES.get(found.group(1), found.group(1))}: "
                      f"{found.group(2)}")
            return
        if _FAILED.match(line):
            self.trouble.append(line.strip())
            return
        if _SKIP.match(line):
            return
        # A line nothing here knows: kept aside, so a reader can see what was
        # said without the live view having to carry it.
        self.other.append(line)
        if len(self.other) > 200:
            del self.other[:50]
