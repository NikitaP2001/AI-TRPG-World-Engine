"""SCHEDULER JOB REGISTRY — single source of truth for all agent tasks.

Every conditionally-run agent task is defined here with its trigger and
callback.  Read this file to see the full schedule at a glance.

Add / edit / remove jobs here to change what runs when.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from scheduler.core import TickScheduler, Job, TurnCount, ParagraphCount, MemoryEntryCount

if TYPE_CHECKING:
    from console_app import GameOrchestrator


def register_all_jobs(scheduler: TickScheduler, orch: "GameOrchestrator") -> None:
    """Register EVERY scheduled task.

    This is the single authority on what runs when.  All job IDs, triggers,
    dependencies, and callbacks are visible in one place.
    """

    # ═══════════════════════════════════════════════════════════════════
    # Story progression
    # ═══════════════════════════════════════════════════════════════════

    scheduler.register(
        Job("paragraph_summary", orch._sm._run_paragraph_summary,
            trigger=TurnCount(interval=10),
            priority=50,
            ),
        Job("arc_summary", orch._sm._run_arc_summary,
            trigger=ParagraphCount(interval=10),
            depends_on=["paragraph_summary"],
            priority=40,
            ),
    )

    # ═══════════════════════════════════════════════════════════════════
    # Per-character jobs: reflection → diary, relationship review
    # ═══════════════════════════════════════════════════════════════════

    try:
        from character.reflection import get_reflection_interval
        ref_interval = get_reflection_interval()
    except Exception:
        ref_interval = 10

    for cname in orch.world.list_character_names():
        nm = str(cname).strip()
        if not nm:
            continue

        scheduler.register(
            Job(f"char_reflection:{nm}",
                partial(orch._run_character_reflection, character_name=nm),
                trigger=MemoryEntryCount(interval=ref_interval, character=nm),
                priority=30,
                ),
            Job(f"char_diary:{nm}",
                partial(orch._run_character_diary, character_name=nm),
                trigger=MemoryEntryCount(interval=ref_interval, character=nm),
                depends_on=[f"char_reflection:{nm}"],
                priority=20,
                ),
            Job(f"char_review:{nm}",
                partial(orch._run_character_review, character_name=nm),
                trigger=MemoryEntryCount(interval=10, character=nm),
                priority=25,
                ),
        )
