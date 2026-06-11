"""Layer 1 — Base Feature class.

Every feature type (river, lake, wetland, forest, etc.) extends this.
The causal engine calls:
  1. compute_effects(fields, dt) — read fields, push effects
  2. update_geometry(fields, dt) — optional shape change
  3. should_dissolve(fields) → bool — removal check

Features NEVER write cell data directly. They write field effects,
and fields handle continuous interpolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional

from shapely.geometry import Polygon as SPolygon

from ..fields import FieldRegistry


class Feature(ABC):
    """Base class for all simulated features.

    Attributes:
        feature_id: Unique string identifier.
        name: Human-readable name for display.
        geometry: Shapely geometry (Polygon, LineString, Point).
        feature_type: String type discriminator.
        props: Arbitrary properties dict.
    """

    def __init__(
        self,
        feature_id: str,
        name: str,
        geometry: Any,
        feature_type: str,
        props: Optional[dict] = None,
    ):
        self.feature_id = feature_id
        self.name = name
        self.geometry = geometry
        self.feature_type = feature_type
        self.props = props or {}
        self._age_ticks: int = 0

    @abstractmethod
    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Phase 1: Read fields, compute deltas, store locally.

        Called every tick BEFORE commit. Read-only on fields.
        Store write-deltas in self.props or instance variables.
        """

    def commit_effects(self, fields: FieldRegistry) -> None:
        """Phase 2: Write stored deltas to fields.

        Called every tick AFTER all features have computed.
        This is where add_effect() / add_persistent() calls happen.
        Default: calls compute_effects again (backward compat).
        """
        # Default: legacy mode — compute_effects does both read+write.
        # Override in features that support two-phase.
        pass

    def update_geometry(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Optionally update geometry based on field changes.

        Default: no-op. Override for expanding lakes, meandering rivers, etc.
        """

    def should_dissolve(self, fields: FieldRegistry) -> bool:
        """Return True if this feature should be removed.

        Default: never dissolves.
        """
        return False

    def tick(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Run one simulation tick."""
        self._age_ticks += 1
        self.compute_effects(fields, dt)
        self.update_geometry(fields, dt)

    def centroid(self) -> Optional[tuple]:
        """Return (lat, lon) of geometry centroid."""
        if self.geometry is None:
            return None
        c = self.geometry.centroid
        return (c.y, c.x)  # Shapely: (lon, lat)
