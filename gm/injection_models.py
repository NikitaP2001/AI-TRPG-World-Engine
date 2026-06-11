"""Injection profile system: composable rule sets for conditional context injection.

Designed to support multiple agent types (world planner, zone manager, scene manager,
GM, SA, character agents) each with their own set of injection rules.
Expands on the existing HistoryInjector by adding declarative profiles.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from engine.history_injector import HistoryInjector


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

BuilderFn = Callable[..., str]
"""Signature: (world, **kwargs) -> content_string. Pure, no side effects."""


@dataclass(frozen=True)
class InjectionRule:
    """One injectable fact with marker, builder, and metadata.

    Fields:
        marker:    Unique tag (e.g. ``[world_snapshot:world_meta]``) used
                   for deduplication and re-injection recovery.
        builder:   Pure callable that produces content string.
        scope:     Semantic scope hint: ``"world"`` | ``"zone"`` | ``"scene"`` | ``"turn"`` | ``"self"``.
        priority:  Higher = injected first within a profile.
        depends_on: List of marker strings that must be injected first.
        kwargs:    Default keyword args passed to ``builder`` (overridable at ensure time).
    """
    marker: str
    builder: BuilderFn
    scope: str = "world"
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InjectionProfile:
    """A named set of rules for one agent type.

    Profiles can be merged to compose rules
    (e.g. zone = base_world + zone_overlay).
    """
    agent_type: str
    rules: List[InjectionRule]

    def merge(self, other: InjectionProfile) -> InjectionProfile:
        """Return a new profile with rules from both, deduplicated by marker."""
        seen = {r.marker for r in self.rules}
        merged = list(self.rules)
        for r in other.rules:
            if r.marker not in seen:
                merged.append(r)
                seen.add(r.marker)
        return InjectionProfile(
            agent_type=f"{self.agent_type}+{other.agent_type}",
            rules=merged,
        )

    def for_scope(self, scope: str) -> InjectionProfile:
        """Return a new profile filtered to one scope."""
        return InjectionProfile(
            agent_type=f"{self.agent_type}/{scope}",
            rules=[r for r in self.rules if r.scope == scope],
        )


# ---------------------------------------------------------------------------
# Engine: runs a profile against a HistoryInjector
# ---------------------------------------------------------------------------

class InjectionEngine:
    """Drives one agent's injection lifecycle using a declarative profile.

    Usage::

        engine = InjectionEngine(profile, history_loader, delta_injector)
        engine.ensure(world=world)  # called on every tick
    """

    def __init__(
        self,
        profile: InjectionProfile,
        history_loader: Callable[[], List[Any]],
        delta_injector: Callable[[str], None],
    ) -> None:
        self._profile = profile
        self._injector = HistoryInjector(
            history_loader=history_loader,
            delta_injector=delta_injector,
        )
        self._sorted_rules = sorted(
            self._profile.rules,
            key=lambda r: (-r.priority, r.marker),
        )

    @property
    def agent_type(self) -> str:
        return self._profile.agent_type

    @property
    def profile(self) -> InjectionProfile:
        return self._profile

    @property
    def rules(self) -> List[InjectionRule]:
        return self._sorted_rules

    def ensure(self, *, world: Any, extra_kwargs: Optional[Dict[str, Any]] = None) -> int:
        """Run all missing rules. Returns count of injections performed."""
        count = 0
        ekw = extra_kwargs or {}

        for rule in self._sorted_rules:
            if self._injector.history_contains(rule.marker):
                continue

            # Resolve builder kwargs: default from rule + overrides from extra
            kw = dict(rule.kwargs)
            kw.update(ekw)

            try:
                body = rule.builder(world, **kw)
            except Exception:
                continue

            if not (body or "").strip():
                continue

            if self._injector.inject_if_absent(marker=rule.marker, content=body):
                count += 1

        return count

    def force_refresh(self, marker: str, *, world: Any, **kwargs: Any) -> bool:
        """Force re-injection of a single rule, bypassing presence check.

        Use this when a fact was updated mid-turn (e.g. character moved, NPC changed state).
        """
        for rule in self._sorted_rules:
            if rule.marker != marker:
                continue
            kw = dict(rule.kwargs)
            kw.update(kwargs)
            try:
                body = rule.builder(world, **kw)
            except Exception:
                return False
            if not (body or "").strip():
                return False
            # Inject without marker check — overrides previous version.
            try:
                self._injector._delta_injector(f"{marker}\n{body}")
                return True
            except Exception:
                return False
        return False

    def contains(self, marker: str) -> bool:
        """Check if marker is already in the agent's active history."""
        return self._injector.history_contains(marker)

    def inject_raw(self, content: str) -> None:
        """Inject raw content directly (no marker dedup)."""
        self._injector._delta_injector(str(content or "").strip())
