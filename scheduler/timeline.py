"""Segment-based intent scheduler — implementation of docs/scene_design.md §3-§5.

This is the deterministic engine described as missing in
docs/scene_design_implementation_plan.md: everything here is pure/structural
code. The only two hooks that may call out to the Scene Manager are
``perception_gate`` (§3.3/§8, private-visibility matches) and
``schema_semantic_check`` (§3.1/§8, optional — not wired by default).

Not covered yet (see the implementation plan doc):
- Full location connectivity/line-of-sight awareness (§2). ``perceivers()``
  currently uses a simple same-location rule; callers may pass a richer
  ``awareness_fn`` to extend this without changing the scheduler itself.
- ``true_intent`` / facade divergence for concealed actions (§10).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Duration parsing — tolerant of "0"/"0s" (WorldDuration.parse_user_input
# rejects zero durations, but "starts now" / "instant" are valid here).
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?\s*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def parse_duration_seconds(text: Optional[str], *, default: int = 0) -> int:
    """Parse a duration string like '0s', '5m', '2h' into seconds.

    Unlike ``WorldDuration.parse_user_input``, zero and empty input are valid
    (meaning "now" / "instant") since intents commonly declare ``t_start:
    "0s"``.
    """
    raw = str(text or "").strip()
    if not raw:
        return default
    m = _DURATION_RE.match(raw)
    if not m:
        return default
    value = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return value * _UNIT_SECONDS.get(unit, 1)


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

_VALID_ACTION_TYPES = {"speech", "movement", "combat", "cosmetic"}
_VALID_ON_TRIGGER = {"interrupt_with_new_decision", "apply_prepared_reaction"}


def new_intent_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Intent:
    actor: str
    action_type: str
    target: str
    description: str = ""
    area: Optional[Dict[str, Any]] = None
    t_start: int = 0
    t_end: Optional[int] = None  # None = persistent subscription (§3.2)
    is_hostile: bool = False
    visibility: str = "public"
    detection_difficulty: Optional[int] = None
    trigger_condition: Optional[Dict[str, Any]] = None
    trigger_priority: int = 0
    on_trigger: str = "interrupt_with_new_decision"
    prepared_reaction: Optional[Dict[str, Any]] = None
    on_timeout: Optional[Dict[str, Any]] = None
    id: str = field(default_factory=new_intent_id)
    status: str = "active"  # active | completed | cancelled | resolved_by_trigger
    origin_cycle: int = 0
    absorption: str = "alert"  # "alert" | "immersed" (§3.6)

    @property
    def is_persistent(self) -> bool:
        return self.t_end is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "actor": self.actor,
            "action_type": self.action_type,
            "target": self.target,
            "description": self.description,
            "area": self.area,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "is_hostile": self.is_hostile,
            "visibility": self.visibility,
            "detection_difficulty": self.detection_difficulty,
            "trigger_condition": self.trigger_condition,
            "trigger_priority": self.trigger_priority,
            "on_trigger": self.on_trigger,
            "prepared_reaction": self.prepared_reaction,
            "on_timeout": self.on_timeout,
            "status": self.status,
            "origin_cycle": self.origin_cycle,
            "absorption": self.absorption,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Intent":
        return Intent(
            actor=str(d.get("actor") or ""),
            action_type=str(d.get("action_type") or ""),
            target=str(d.get("target") or ""),
            description=str(d.get("description") or ""),
            area=d.get("area") or None,
            t_start=int(d.get("t_start") or 0),
            t_end=(int(d["t_end"]) if d.get("t_end") is not None else None),
            is_hostile=bool(d.get("is_hostile") or False),
            visibility=str(d.get("visibility") or "public"),
            detection_difficulty=(int(d["detection_difficulty"]) if d.get("detection_difficulty") is not None else None),
            trigger_condition=d.get("trigger_condition") or None,
            trigger_priority=int(d.get("trigger_priority") or 0),
            on_trigger=str(d.get("on_trigger") or "interrupt_with_new_decision"),
            prepared_reaction=d.get("prepared_reaction") or None,
            on_timeout=d.get("on_timeout") or None,
            id=str(d.get("id") or new_intent_id()),
            status=str(d.get("status") or "active"),
            origin_cycle=int(d.get("origin_cycle") or 0),
            absorption=str(d.get("absorption") or "alert"),
        )


def instantiate_sub_intent(spec: Dict[str, Any], *, actor: str, t_start: int) -> Intent:
    """Build a new Intent from a ``prepared_reaction``/``on_timeout`` sub-schema."""
    duration_raw = spec.get("duration") or spec.get("interval")
    t_end: Optional[int]
    if spec.get("persistent") or (isinstance(duration_raw, str) and not duration_raw.strip()):
        t_end = None
    elif duration_raw is None:
        t_end = t_start  # instant
    else:
        t_end = t_start + max(0, parse_duration_seconds(str(duration_raw)))

    return Intent(
        actor=str(spec.get("actor") or actor),
        action_type=str(spec.get("action_type") or "speech"),
        target=str(spec.get("target") or "all"),
        description=str(spec.get("description") or ""),
        area=spec.get("area") or None,
        t_start=t_start,
        t_end=t_end,
        is_hostile=bool(spec.get("is_hostile") or False),
        visibility=str(spec.get("visibility") or "public"),
        detection_difficulty=spec.get("detection_difficulty"),
        trigger_condition=spec.get("trigger_condition") or None,
        trigger_priority=int(spec.get("trigger_priority") or 0),
        on_trigger=str(spec.get("on_trigger") or "interrupt_with_new_decision"),
        prepared_reaction=spec.get("prepared_reaction") or None,
        on_timeout=None,
        origin_cycle=0,
    )


# ---------------------------------------------------------------------------
# §3.1 — structural schema validation
# ---------------------------------------------------------------------------

def validate_schema(intent: Intent) -> List[str]:
    """Structural (field-level) §3.1 checks. Returns a list of violations."""
    violations: List[str] = []
    at = intent.action_type

    if at not in _VALID_ACTION_TYPES:
        violations.append(f"unknown action_type '{at}'")

    if at == "cosmetic":
        if intent.is_hostile:
            violations.append("cosmetic intents may not set is_hostile=true")
        if intent.target == "area" or intent.area:
            violations.append("cosmetic intents may not target 'area'")

    if at == "combat" and intent.visibility == "private" and intent.detection_difficulty is None:
        violations.append("private combat intents require detection_difficulty")

    if at in ("speech", "movement") and intent.area:
        violations.append(f"{at} intents do not use 'area'")

    if intent.visibility not in ("public", "private"):
        violations.append(f"invalid visibility '{intent.visibility}'")

    if intent.on_trigger not in _VALID_ON_TRIGGER:
        violations.append(f"invalid on_trigger '{intent.on_trigger}'")

    return violations


# ---------------------------------------------------------------------------
# §4 — resolved history entries
# ---------------------------------------------------------------------------

@dataclass
class ResolvedEntry:
    t_start: int
    t_end: int
    actor: str
    action_type: str
    target: str
    description: str
    visibility: str
    kind: str  # action | prepared_reaction | timeout | schema_violation
    perceivers: List[str] = field(default_factory=list)
    note: str = ""
    intent_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "t_start": self.t_start,
            "t_end": self.t_end,
            "actor": self.actor,
            "action_type": self.action_type,
            "target": self.target,
            "description": self.description,
            "visibility": self.visibility,
            "kind": self.kind,
            "perceivers": list(self.perceivers),
            "note": self.note,
            "intent_id": self.intent_id,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ResolvedEntry":
        return ResolvedEntry(
            t_start=int(d.get("t_start") or 0),
            t_end=int(d.get("t_end") or 0),
            actor=str(d.get("actor") or ""),
            action_type=str(d.get("action_type") or ""),
            target=str(d.get("target") or ""),
            description=str(d.get("description") or ""),
            visibility=str(d.get("visibility") or "public"),
            kind=str(d.get("kind") or "action"),
            perceivers=list(d.get("perceivers") or []),
            note=str(d.get("note") or ""),
            intent_id=str(d.get("intent_id") or ""),
        )


def format_resolved_history_for_sm(entries: List[ResolvedEntry], base_sec: int) -> str:
    """Render resolved-history entries (§4 step 6) as compact text for SM narration."""
    if not entries:
        return ""
    lines = ["Resolved timeline (this cycle, chronological):"]
    for e in entries:
        rel_start = e.t_start - base_sec
        rel_end = e.t_end - base_sec
        span = f"+{rel_start}s" if rel_start == rel_end else f"+{rel_start}s..+{rel_end}s"
        tag = "" if e.kind == "action" else f" [{e.kind}]"
        vis = "" if e.visibility == "public" else " (private)"
        line = f"  [{span}] {e.actor} {e.action_type} -> {e.target}{vis}{tag}: {e.description[:160]}"
        if e.note:
            line += f"  ({e.note})"
        lines.append(line)
    return "\n".join(lines)


@dataclass
class SchedulerResult:
    resolved_history: List[ResolvedEntry]
    reinvoke: Dict[str, List[str]]
    carry_over_intents: List[Intent]
    completed_intents: List[Intent]
    warnings: List[str]
    next_cursor: int


PerceptionGate = Callable[[Intent, str], bool]
"""``(matched_intent, observer_actor) -> perceived``. Only called for
private-visibility structural matches (§3.3/§8)."""

AwarenessFn = Callable[[str, str], bool]
"""``(observer_actor, subject_actor) -> aware``. Used to compute
``ResolvedEntry.perceivers`` (§2). Defaults to same-scene = aware."""


MAX_SEGMENT_ITERATIONS = 200


def _target_matches(cand_target: str, target_match: str, subscriber_actor: str) -> bool:
    tm = (target_match or "").strip()
    if not tm or tm.lower() == "any":
        return True
    if tm.lower() == "self":
        return cand_target.strip().lower() == subscriber_actor.strip().lower()
    return cand_target.strip().lower() == tm.strip().lower()


def _structural_match(candidate: Intent, trigger_condition: Dict[str, Any], subscriber_actor: str) -> bool:
    tc_actor = str(trigger_condition.get("actor") or "any").strip()
    tc_action = str(trigger_condition.get("action_type") or "any").strip()
    tc_target = str(trigger_condition.get("target_match") or "any")

    if tc_actor.lower() != "any" and tc_actor.strip().lower() != candidate.actor.strip().lower():
        return False
    if tc_action.lower() != "any" and tc_action.strip().lower() != candidate.action_type.strip().lower():
        return False
    if not _target_matches(candidate.target, tc_target, subscriber_actor):
        return False
    return True


class SegmentScheduler:
    """Deterministic §4 segment-resolution engine.

    Usage::

        sched = SegmentScheduler(intents, now=t0, horizon=t1,
                                  perception_gate=sm.perception_check)
        result = sched.run()
    """

    def __init__(
        self,
        intents: List[Intent],
        *,
        now: int,
        horizon: int,
        perception_gate: Optional[PerceptionGate] = None,
        awareness_fn: Optional[AwarenessFn] = None,
        all_actors: Optional[List[str]] = None,
        seen_action_ids: Optional[set] = None,
    ) -> None:
        self.intents: List[Intent] = list(intents)
        self.now = int(now)
        self.horizon = max(int(horizon), int(now))
        self.perception_gate = perception_gate
        self.awareness_fn = awareness_fn or (lambda observer, subject: True)
        self.all_actors = list(all_actors or [])
        self._seen_action_ids = set(seen_action_ids or ())

    # -- helpers ------------------------------------------------------

    def _is_active(self, intent: Intent, seg_start: int) -> bool:
        if intent.status in ("completed", "cancelled", "resolved_by_trigger"):
            return False
        if intent.t_start > seg_start:
            return False
        if intent.t_end is not None and intent.t_end <= seg_start:
            return False
        return True

    def _next_boundary(self, intents: List[Intent], cursor: int) -> Optional[int]:
        candidates = set()
        for i in intents:
            if i.status in ("completed", "cancelled", "resolved_by_trigger"):
                continue
            if i.t_start > cursor:
                candidates.add(min(i.t_start, self.horizon))
            if i.t_end is not None and i.t_end > cursor:
                candidates.add(min(i.t_end, self.horizon))
        candidates = {c for c in candidates if c > cursor}
        if candidates:
            return min(candidates)
        if cursor < self.horizon and any(self._is_active(i, cursor) for i in intents):
            return self.horizon
        return None

    def _perceivers(self, intent: Intent) -> List[str]:
        observers = [a for a in self.all_actors if a != intent.actor] or [intent.actor]
        return [o for o in observers if self.awareness_fn(o, intent.actor)]

    # -- main loop ------------------------------------------------------

    def run(self) -> SchedulerResult:
        resolved: List[ResolvedEntry] = []
        reinvoke: Dict[str, List[str]] = {}
        warnings: List[str] = []

        intents = self.intents
        for intent in intents:
            violations = validate_schema(intent)
            for v in violations:
                warnings.append(f"{intent.actor}: {v}")

        recorded_action_ids: set = set(self._seen_action_ids)
        cursor = self.now
        iterations = 0

        while cursor < self.horizon and iterations < MAX_SEGMENT_ITERATIONS:
            iterations += 1
            boundary = self._next_boundary(intents, cursor)
            if boundary is None:
                break
            seg_start, seg_end = cursor, boundary
            active = [i for i in intents if self._is_active(i, seg_start)]

            if not active:
                cursor = seg_end
                continue

            # Record plain (non-triggered) actions once, when they start.
            for intent in active:
                if intent.t_start == seg_start and intent.id not in recorded_action_ids:
                    recorded_action_ids.add(intent.id)
                    resolved.append(ResolvedEntry(
                        t_start=intent.t_start,
                        t_end=intent.t_end if intent.t_end is not None else seg_end,
                        actor=intent.actor, action_type=intent.action_type,
                        target=intent.target, description=intent.description,
                        visibility=intent.visibility, kind="action",
                        perceivers=self._perceivers(intent),
                        intent_id=intent.id,
                    ))

            # §4 step 2 — structural join
            matches: List[Tuple[Intent, Intent]] = []
            for sub in active:
                if not sub.trigger_condition:
                    continue
                for cand in active:
                    if cand is sub or cand.actor == sub.actor:
                        continue
                    if _structural_match(cand, sub.trigger_condition, sub.actor):
                        matches.append((sub, cand))

            # §4 step 3 — perception gate (private visibility only)
            passed: List[Tuple[Intent, Intent]] = []
            for sub, cand in matches:
                if cand.visibility == "private":
                    ok = self.perception_gate(cand, sub.actor) if self.perception_gate else False
                    if not ok:
                        continue
                passed.append((sub, cand))

            # §3.4 / §4 step 4 — ascending trigger_priority
            passed.sort(key=lambda pair: pair[0].trigger_priority)

            fired_sub_ids: set = set()
            for sub, cand in passed:
                if sub.prepared_reaction:
                    new_intent = instantiate_sub_intent(sub.prepared_reaction, actor=sub.actor, t_start=seg_start)
                    intents.append(new_intent)
                    resolved.append(ResolvedEntry(
                        t_start=seg_start, t_end=new_intent.t_end if new_intent.t_end is not None else seg_start,
                        actor=new_intent.actor, action_type=new_intent.action_type,
                        target=new_intent.target, description=new_intent.description,
                        visibility=new_intent.visibility, kind="prepared_reaction",
                        perceivers=self._perceivers(new_intent),
                        note=f"prepared reaction to {cand.actor}'s {cand.action_type}",
                    ))
                elif sub.on_trigger == "interrupt_with_new_decision":
                    reinvoke.setdefault(sub.actor, []).append(
                        f"Triggered by {cand.actor}'s {cand.action_type} -> {cand.target}: "
                        f"{cand.description[:120]}"
                    )
                # Persistent subscriptions remain active after firing (§3.2, §4 step 4).
                if not sub.is_persistent:
                    fired_sub_ids.add(sub.id)

            for sub_id in fired_sub_ids:
                for i in intents:
                    if i.id == sub_id:
                        i.status = "resolved_by_trigger"

            # §4 step 5 — timeouts for bounded intents ending this segment, unmatched
            matched_sub_ids = {sub.id for sub, _ in passed}
            for intent in active:
                if intent.t_end == seg_end and intent.id not in matched_sub_ids and intent.status == "active":
                    if intent.on_timeout:
                        new_intent = instantiate_sub_intent(intent.on_timeout, actor=intent.actor, t_start=seg_end)
                        intents.append(new_intent)
                        resolved.append(ResolvedEntry(
                            t_start=seg_end, t_end=new_intent.t_end if new_intent.t_end is not None else seg_end,
                            actor=new_intent.actor, action_type=new_intent.action_type,
                            target=new_intent.target, description=new_intent.description,
                            visibility=new_intent.visibility, kind="timeout",
                            perceivers=self._perceivers(new_intent),
                        ))
                    intent.status = "completed"

            cursor = seg_end

        carry_over = [
            i for i in intents
            if i.status == "active" and (i.is_persistent or (i.t_end is not None and i.t_end > self.horizon))
        ]
        completed = [i for i in intents if i.status == "completed"]

        return SchedulerResult(
            resolved_history=resolved,
            reinvoke=reinvoke,
            carry_over_intents=carry_over,
            completed_intents=completed,
            warnings=warnings,
            next_cursor=cursor,
        )
