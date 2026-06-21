"""Layer 1 — Causal Simulation Engine.

Orchestrates per-tick execution for all features:
  1. For each feature: compute_effects → push field deltas
  2. After all features processed: fields process accumulated deltas
  3. For each feature: update_geometry (optional)
  4. Remove dissolved features
  5. Check emergence conditions → spawn new features
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from .fields import FieldRegistry
from .features.base import Feature


class SimEngine:
    """Orchestrates causal simulation ticks.

    Usage:
        engine = SimEngine(fields)
        engine.add_feature(my_lake)
        engine.add_feature(my_wetland)
        for tick in range(100):
            engine.step(dt=1.0)
    """

    def __init__(self, fields: FieldRegistry):
        self._fields = fields
        self._features: Dict[str, Feature] = {}
        self._tick_count: int = 0
        # Emergence rules: (feature_type, condition_fn, spawn_fn)
        self._emergence_rules: list = []

    @property
    def fields(self) -> FieldRegistry:
        return self._fields

    @property
    def features(self) -> List[Feature]:
        return list(self._features.values())

    def add_feature(self, feature: Feature) -> None:
        """Register a feature for simulation."""
        self._features[feature.feature_id] = feature

    def remove_feature(self, feature_id: str) -> None:
        """Unregister a feature."""
        self._features.pop(feature_id, None)

    def get_feature(self, feature_id: str) -> Optional[Feature]:
        return self._features.get(feature_id)

    def get_features_by_type(self, feature_type: str) -> List[Feature]:
        return [f for f in self._features.values()
                if f.feature_type == feature_type]

    def add_emergence_rule(self, condition_fn, spawn_fn) -> None:
        """Add an emergence rule checked each tick.

        condition_fn(fields, tick) → list of spawn_data dicts
        spawn_fn(spawn_data) → Feature or None
        """
        self._emergence_rules.append((condition_fn, spawn_fn))

    def step(self, dt: float = 1.0) -> None:
        """Run one simulation tick — two-phase for inter-feature correctness.

        Order:
          1. ALL features compute_effects (read fields, store deltas locally)
          2. Clear ALL persistent effects (fresh slate for commit)
          3. ALL features commit_effects (write deltas to fields simultaneously)
          4. Clear temporary effects from fields
          5. All features update geometry
          6. Remove dissolved features
          7. Check emergence rules → spawn new features
        """
        features = list(self._features.values())

        # Phase 0: Age increment
        for feature in features:
            feature._age_ticks += 1

        # Phase 1: Clear ALL persistent effects — fresh slate
        for mf in self._fields._mutable_fields.values():
            mf.clear_persistent()

        # Phase 2: ALL features compute + write effects sequentially
        # Each feature reads the field state (which now includes previous
        # features' writes) and writes its own persistent effects.
        # Lake raises WT → Groundwater sees raised WT → correct.
        for feature in features:
            feature.compute_effects(self._fields, dt)
            feature.update_geometry(self._fields, dt)

        # Phase 3: Clear temporary effects (persistent survive for next tick)
        self._fields.clear_all_effects()

        # Phase 3b: Bake persistent effects into _base so they survive next
        # tick's clear_persistent().  For "replace" fields (PFT, canopy),
        # rebuild _base from persistent points.  For "accumulate" fields
        # (water, soil), this is handled by the caller (_run_l1_step).
        import math as _m
        for _name, _mf in self._fields._mutable_fields.items():
            if not _mf._persistent:
                continue
            if _name.startswith("pft_biomass[") or _name in ("canopy_density",):
                # Replace _base with persistent snapshot (Vegetation writes
                # the complete new state each tick, not a delta).
                _pts = []
                _vals = []
                for _lat, _lon, _rad, _str in _mf._persistent:
                    _lr = _m.radians(_lat)
                    _lor = _m.radians(_lon)
                    _pts.append([_m.cos(_lr)*_m.cos(_lor), _m.sin(_lr), _m.cos(_lr)*_m.sin(_lor)])
                    _vals.append(_str)
                if _vals:
                    import numpy as _np
                    from scipy.spatial import cKDTree as _cKD
                    from ..layer0.contouring import ContinuousField as _CF
                    _mf._base = _CF(_cKD(_np.array(_pts, dtype=_np.float64)),
                                    _np.array(_vals, dtype=_np.float64))
                _mf._persistent.clear()

        # Phase 4: Remove dissolved features
        for fid, feature in list(self._features.items()):
            if feature.should_dissolve(self._fields):
                self._features.pop(fid, None)

        # Phase 5: Emergence
        for condition_fn, spawn_fn in self._emergence_rules:
            spawn_data_list = condition_fn(self._fields, self._tick_count)
            for data in spawn_data_list:
                new_feature = spawn_fn(data)
                if new_feature is not None:
                    self.add_feature(new_feature)

        self._tick_count += 1

    def run(self, num_ticks: int, dt: float = 1.0,
            progress_cb=None) -> None:
        """Run multiple ticks with optional progress callback."""
        for t in range(num_ticks):
            self.step(dt)
            if progress_cb:
                progress_cb(t, num_ticks, self)
