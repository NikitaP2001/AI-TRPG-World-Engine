"""How an invocation opens.

An agent that does not know when it is cannot say what has happened in between,
and it cannot judge how long something took, what is overdue, or whether two
things it reads belong to the same hour. So the moment is stated on every
invocation opening, by one place, and nowhere else: an agent that has to
remember to state it will one day forget, and this is the class of fact nobody
notices missing until it matters.

It is a date and nothing around it. A loud block costs the same tokens as a
quiet line and holds attention worse; what the agent needs is the stamp, in the
same form the records it reads carry.
"""

from __future__ import annotations

from typing import Iterable, List

from world.clock import now_string


def opening(parts: Iterable[str], *, moment: str = "") -> str:
    """An invocation's opening message, dated by the world's own clock.

    Whatever the caller decided to say is kept, in its order and its words, and
    the date stands under the first line — the line that says why the agent was
    called — because the reason is what it reads first and the moment is what
    it needs next.

    ``moment`` is for a caller that already knows the world time it means: a
    cycle that stands at a moment of its own states that one, and the clock is
    only asked when nobody did. With no world at all (a fresh start, a scratch
    directory) nothing is added and the opening is exactly what was passed.
    """
    lines: List[str] = [str(p) for p in parts if str(p or "").strip()]
    stamp = str(moment or "").strip() or now_string()
    if not stamp:
        return "\n".join(lines)
    date = f"Time: {stamp}"
    if not lines:
        return date
    return "\n".join([lines[0], date, *lines[1:]])
