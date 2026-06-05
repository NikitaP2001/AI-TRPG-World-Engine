"""Unified tick-based job scheduler for periodic game maintenance.

Replaces hardcoded N-turn logic spread across story.py, console_app.py,
and character/reflection.py with a single scheduler.  Every tick checks
all registered jobs and fires those whose trigger conditions are met.

Trigger types:
  TurnCount        — every N turns since last fire
  ParagraphCount   — every N paragraphs since last fire
  MemoryEntryCount — every N memory entries for a specific character
  WallClock        — every N seconds of real time
  WorldTime        — at an exact world datetime
  AfterJob         — once after another job completes

"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Trigger types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnCount:
    """Fire every N turns since last execution."""
    interval: int
    offset: int = 0


@dataclass(frozen=True)
class ParagraphCount:
    """Fire every N paragraphs since last execution."""
    interval: int


@dataclass(frozen=True)
class MemoryEntryCount:
    """Fire every N memory entries for a specific character."""
    interval: int
    character: str


@dataclass(frozen=True)
class WallClock:
    """Fire every N seconds of real time."""
    interval_sec: float


@dataclass(frozen=True)
class WorldTime:
    """Fire at exact world time (YYYY-MM-DD HH:MM or similar)."""
    at: str


@dataclass(frozen=True)
class AfterJob:
    """Fire once after another job completes."""
    after: str


Trigger = Union[TurnCount, ParagraphCount, MemoryEntryCount, WallClock, WorldTime, AfterJob]


# ---------------------------------------------------------------------------
# Job definition
# ---------------------------------------------------------------------------

JobFn = Callable[[], Any]


@dataclass
class Job:
    """One scheduled task.

    Fields:
        id:          Unique job identifier (e.g. ``"paragraph_summary"``).
        run:         Callable to execute. Called with no arguments.
        trigger:     Condition that determines when this job fires.
        depends_on:  Job IDs that must have completed before this one runs.
        priority:    Higher = runs earlier within the same tick.
        timeout_sec: Max wall-clock seconds before the job is considered hung.
        retry_on_fail: If True, the job is eligible to re-fire on the next tick.
    """
    id: str
    run: JobFn
    trigger: Trigger
    depends_on: List[str] = field(default_factory=list)
    priority: int = 0
    timeout_sec: float = 30.0
    retry_on_fail: bool = True


# ---------------------------------------------------------------------------
# Per-job persistent state
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    """Persisted per-job counters and status."""
    last_fired_at_turn: int = 0
    last_fired_at_para: int = 0
    last_fired_at_entry: int = 0
    last_fired_wall: float = 0.0
    last_world_time: str = ""
    status: str = "idle"        # idle | running | completed | failed
    consecutive_failures: int = 0
    skipped: bool = False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class TickScheduler:
    """Unified scheduler for periodic game jobs.

    Usage::

        scheduler = TickScheduler(state_path)
        scheduler.register(job1, job2, ...)
        # After each turn:
        scheduler.tick(turn_count=info["turns"], para_count=info["paragraphs"],
                       world=world, character_names=[...])
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path.resolve()
        self._jobs: Dict[str, Job] = {}
        self._state: Dict[str, JobState] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, *jobs: Job) -> None:
        for j in jobs:
            self._jobs[j.id] = j
            if j.id not in self._state:
                self._state[j.id] = JobState()

    @property
    def all_jobs(self) -> Dict[str, Job]:
        return dict(self._jobs)

    @property
    def all_states(self) -> Dict[str, JobState]:
        self._load_state()
        return dict(self._state)

    def job_state(self, job_id: str) -> Optional[JobState]:
        self._load_state()
        return self._state.get(job_id)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self._state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            for jid, js in data.items():
                if jid in self._state:
                    for k, v in js.items():
                        if hasattr(self._state[jid], k):
                            setattr(self._state[jid], k, v)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                jid: {
                    "last_fired_at_turn": js.last_fired_at_turn,
                    "last_fired_at_para": js.last_fired_at_para,
                    "last_fired_at_entry": js.last_fired_at_entry,
                    "last_fired_wall": js.last_fired_wall,
                    "last_world_time": js.last_world_time,
                    "status": js.status,
                    "consecutive_failures": js.consecutive_failures,
                    "skipped": js.skipped,
                }
                for jid, js in self._state.items()
            }
            self._state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    @dataclass
    class TickContext:
        turn_count: int
        paragraph_count: int
        character_entry_counts: Dict[str, int]
        world_time_str: str = ""

    def _build_context(
        self,
        *,
        turn_count: int,
        paragraph_count: int,
        world: Any = None,
        character_names: List[str] = None,
    ) -> TickContext:
        entry_counts: Dict[str, int] = {}
        if character_names and world is not None:
            for name in character_names:
                try:
                    from character.reflection import count_memory_entries
                    entry_counts[name] = count_memory_entries(name) or 0
                except Exception:
                    entry_counts[name] = 0

        world_time = ""
        if world is not None:
            try:
                wt = world.get_world_time()
                world_time = wt.to_string() if hasattr(wt, "to_string") else str(wt)
            except Exception:
                pass

        return TickScheduler.TickContext(
            turn_count=turn_count,
            paragraph_count=paragraph_count,
            character_entry_counts=entry_counts,
            world_time_str=world_time,
        )

    # ------------------------------------------------------------------
    # Trigger evaluation
    # ------------------------------------------------------------------

    def _trigger_fired(self, job: Job, ctx: TickContext, js: JobState) -> bool:
        t = job.trigger

        if isinstance(t, TurnCount):
            return (ctx.turn_count - js.last_fired_at_turn) >= t.interval

        if isinstance(t, ParagraphCount):
            return (ctx.paragraph_count - js.last_fired_at_para) >= t.interval

        if isinstance(t, MemoryEntryCount):
            entry_count = ctx.character_entry_counts.get(t.character, 0)
            return (entry_count - js.last_fired_at_entry) >= t.interval

        if isinstance(t, WallClock):
            return (time.time() - js.last_fired_wall) >= t.interval_sec

        if isinstance(t, WorldTime):
            # Fire once when world time reaches or passes the target.
            # Prevent re-fire by checking last_world_time.
            if js.last_world_time:
                return False  # already fired for this target
            return ctx.world_time_str >= t.at

        if isinstance(t, AfterJob):
            dep_state = self._state.get(t.after)
            if dep_state is None:
                return False
            return dep_state.status == "completed"

        return False

    def _update_counters(self, job: Job, ctx: TickContext, js: JobState) -> None:
        t = job.trigger
        if isinstance(t, TurnCount):
            js.last_fired_at_turn = ctx.turn_count
        elif isinstance(t, ParagraphCount):
            js.last_fired_at_para = ctx.paragraph_count
        elif isinstance(t, MemoryEntryCount):
            js.last_fired_at_entry = ctx.character_entry_counts.get(t.character, 0)
        elif isinstance(t, WallClock):
            js.last_fired_wall = time.time()
        elif isinstance(t, WorldTime):
            js.last_world_time = ctx.world_time_str
        elif isinstance(t, AfterJob):
            pass  # no counter to advance

    # ------------------------------------------------------------------
    # Tick: evaluate all jobs, run those that are due
    # ------------------------------------------------------------------

    def tick(
        self,
        *,
        turn_count: int,
        paragraph_count: int,
        world: Any = None,
        character_names: List[str] = None,
    ) -> List[str]:
        """Evaluate all jobs and execute those whose triggers have fired.

        Returns a list of job IDs that were executed this tick.
        """
        self._load_state()

        ctx = self._build_context(
            turn_count=turn_count,
            paragraph_count=paragraph_count,
            world=world,
            character_names=character_names,
        )

        # Sort jobs: priority descending; then by trigger complexity
        sorted_jobs = sorted(
            self._jobs.values(),
            key=lambda j: (-j.priority, j.id),
        )

        fired: List[str] = []

        for job in sorted_jobs:
            js = self._state[job.id]

            # Skip if already running or recently completed
            if js.status == "running":
                continue
            if js.status == "completed" and not job.retry_on_fail:
                continue

            # Check dependencies (all must be completed or absent)
            deps_met = all(
                self._state.get(did) is None
                or self._state[did].status == "completed"
                for did in job.depends_on
            )
            if not deps_met:
                continue

            # Check trigger
            if not self._trigger_fired(job, ctx, js):
                continue

            # ── Execute ──
            js.status = "running"
            self._update_counters(job, ctx, js)
            self._save_state()

            try:
                result = job.run()
                js.status = "completed"
                js.consecutive_failures = 0
                fired.append(job.id)
            except Exception as e:
                js.status = "failed"
                js.consecutive_failures += 1
                if not job.retry_on_fail:
                    js.skipped = True
                # Log but don't crash the tick
                import logging
                logging.exception(f"[scheduler] job {job.id} failed: {e}")

            self._save_state()

        return fired

    def reset(self, job_id: Optional[str] = None) -> None:
        """Reset one or all jobs to idle (next tick re-evaluates triggers)."""
        if job_id:
            if job_id in self._state:
                self._state[job_id] = JobState()
        else:
            for jid in self._state:
                self._state[jid] = JobState()
        self._save_state()
