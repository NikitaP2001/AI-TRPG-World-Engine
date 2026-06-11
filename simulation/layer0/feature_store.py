"""Layer 0 — Feature Store.

PostGIS-like in-memory vector feature store with Shapely spatial indexing.
Stores geographic primitives (rivers, forests, mountain ranges, lakes,
continents, climate zones) as real vector geometries at arbitrary precision.

Design doc § Feature Storage.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from shapely.geometry import Point as SPoint, LineString, Polygon, shape
from shapely import STRtree

from .cell_model import CellData


# ======================================================================
# JSON conversion helper
# ======================================================================


def _convert_native(obj):
    """Recursively convert numpy types to Python native types."""
    if hasattr(obj, "dtype"):
        # numpy scalar
        return obj.item()
    elif isinstance(obj, dict):
        return {k: _convert_native(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_convert_native(v) for v in obj]
    return obj


# ======================================================================
# Feature data type
# ======================================================================


@dataclass
class Feature:
    """One geographic feature in the store.

    Mirrors the PostGIS schema from the design doc.
    """
    type: str                            # open string, world-defined
    feature_id: str = ""                 # auto-generated UUID if empty
    name: Optional[str] = None
    geometry: Any = None                 # Shapely geometry (Point, LineString, Polygon)
    properties: Dict[str, Any] = field(default_factory=dict)
    layer_effects: Dict[str, Any] = field(default_factory=dict)
    anchor_strength: str = "suggestion"  # suggestion | preferred | fixed
    parent_feature_id: Optional[str] = None
    created_tick: int = 0
    dissolved_tick: Optional[int] = None  # None = currently exists

    @property
    def is_active(self) -> bool:
        return self.dissolved_tick is None

    def bounding_cells(self, resolution: int = 2) -> List[str]:
        """Return H3 cells that this feature's bounding box intersects."""
        if self.geometry is None:
            return []
        import h3
        minx, miny, maxx, maxy = self.geometry.bounds
        # Convert bounds to lat/lon and find covering H3 cells
        cells: set = set()
        # Sample along the bounding box edges
        steps = max(4, int(math.sqrt(abs(maxx - minx) * abs(maxy - miny)) * 10))
        for i in range(steps + 1):
            t = i / steps
            for lat, lon in [
                (miny + t * (maxy - miny), minx),
                (miny + t * (maxy - miny), maxx),
                (miny, minx + t * (maxx - minx)),
                (maxy, minx + t * (maxx - minx)),
            ]:
                try:
                    cells.add(h3.latlng_to_cell(lat, lon, resolution))
                except Exception:
                    pass
        return list(cells)

    def to_dict(self) -> dict:
        """Serialize feature to a JSON-compatible dict."""
        geom = None
        if self.geometry is not None:
            from shapely.geometry import mapping
            geom = mapping(self.geometry)
        # Convert numpy types to native Python for JSON serialization
        props = _convert_native(self.properties)
        effects = _convert_native(self.layer_effects)
        return {
            "type": self.type,
            "feature_id": self.feature_id,
            "name": self.name,
            "geometry": geom,
            "properties": dict(props),
            "layer_effects": dict(effects),
            "anchor_strength": self.anchor_strength,
            "parent_feature_id": self.parent_feature_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "Feature":
        """Restore feature from a dict (produced by to_dict)."""
        geom = d.get("geometry")
        if geom is not None:
            from shapely.geometry import shape
            geom = shape(geom)
        return Feature(
            type=d["type"],
            feature_id=d.get("feature_id", ""),
            name=d.get("name"),
            geometry=geom,
            properties=d.get("properties", {}),
            layer_effects=d.get("layer_effects", {}),
            anchor_strength=d.get("anchor_strength", "suggestion"),
            parent_feature_id=d.get("parent_feature_id"),
        )


# ======================================================================
# Feature Store
# ======================================================================

_SyncCallback = Callable[[str, List[str]], None]
"""Callback(feature_id, affected_cell_ids) — called when feature changes."""


class FeatureStore:
    """In-memory feature store with spatial indexing.

    Provides PostGIS-compatible spatial queries via Shapely STRtree.
    Supports cell synchronisation via optional callback.
    """

    def __init__(self, sync_callback: Optional[_SyncCallback] = None) -> None:
        self._features: Dict[str, Feature] = {}
        self._tree: Optional[STRtree] = None
        self._tree_dirty: bool = False
        self._sync_callback: Optional[_SyncCallback] = sync_callback

    # ── CRUD ─────────────────────────────────────────────────────────

    def add_feature(
        self,
        feature: Feature,
    ) -> Feature:
        """Insert a feature. Calls sync callback for affected cells."""
        if not feature.feature_id:
            feature.feature_id = str(uuid.uuid4())
        self._features[feature.feature_id] = feature
        self._tree_dirty = True

        if self._sync_callback:
            cells = feature.bounding_cells()
            self._sync_callback(feature.feature_id, cells)

        return feature

    def update_feature(
        self,
        feature_id: str,
        **kwargs: Any,
    ) -> Optional[Feature]:
        """Update feature properties. Rebuilds spatial index."""
        feat = self._features.get(feature_id)
        if feat is None:
            return None
        for k, v in kwargs.items():
            if hasattr(feat, k):
                setattr(feat, k, v)
        self._tree_dirty = True

        if self._sync_callback:
            cells = feat.bounding_cells()
            self._sync_callback(feature_id, cells)

        return feat

    def dissolve_feature(self, feature_id: str, tick: int = 0) -> Optional[Feature]:
        """Mark a feature as dissolved (soft delete)."""
        feat = self._features.get(feature_id)
        if feat is None:
            return None
        feat.dissolved_tick = tick

        if self._sync_callback:
            cells = feat.bounding_cells()
            self._sync_callback(feature_id, cells)

        return feat

    def get_feature(self, feature_id: str) -> Optional[Feature]:
        return self._features.get(feature_id)

    def get_features_by_type(self, type_name: str) -> List[Feature]:
        return [f for f in self._features.values()
                if f.type == type_name and f.is_active]

    def get_features_by_name(self, name: str) -> List[Feature]:
        return [f for f in self._features.values()
                if f.name == name and f.is_active]

    @property
    def all_active(self) -> List[Feature]:
        return [f for f in self._features.values() if f.is_active]

    @property
    def count(self) -> int:
        return len(self._features)

    # ── Spatial index ────────────────────────────────────────────────

    def _rebuild_tree(self) -> None:
        active = self.all_active
        self._tree_geoms: List[Any] = []
        self._tree_features: List[Feature] = []
        for f in active:
            if f.geometry is not None:
                self._tree_geoms.append(f.geometry)
                self._tree_features.append(f)
        if self._tree_geoms:
            self._tree = STRtree(self._tree_geoms)
        else:
            self._tree = None
        self._tree_dirty = False

    @property
    def _index(self) -> Optional[STRtree]:
        if self._tree_dirty or self._tree is None:
            self._rebuild_tree()
        return self._tree

    # ── Spatial queries ──────────────────────────────────────────────

    def _query_indices(self, geometry: Any, predicate: str = "intersects",
                       **kwargs: Any) -> List[Feature]:
        tree = self._index
        if tree is None:
            return []
        indices = tree.query(geometry, predicate=predicate, **kwargs)
        return [self._tree_features[i] for i in indices if i < len(self._tree_features)]

    def intersect(self, geometry: Any) -> List[Feature]:
        """All active features whose geometry intersects the given geometry."""
        return self._query_indices(geometry, "intersects")

    def contains(self, geometry: Any) -> List[Feature]:
        """All active features that contain the given geometry."""
        return self._query_indices(geometry, "contains")

    def within_distance(self, geometry: Any, distance_degrees: float) -> List[Feature]:
        """All active features within the given distance (in degrees)."""
        # Buffer and intersect (Shapely 2.0 compat — dwithin needs 2.1+)
        buffered = geometry.buffer(distance_degrees)
        return self._query_indices(buffered, "intersects")

    def at_point(self, lat: float, lon: float) -> List[Feature]:
        """All active features containing this lat/lon point, smallest area first."""
        pt = SPoint(lon, lat)
        results = self.intersect(pt)
        # Sort by area ascending (most specific first)
        results.sort(key=lambda f: f.geometry.area if f.geometry is not None else float("inf"))
        return results

    # ── Cell synchronisation ─────────────────────────────────────────

    def sync_cell(
        self,
        cell: CellData,
        resolution: int = 2,
    ) -> CellData:
        """Update a cell's feature_ids cache by querying all intersecting features.

        For LineString features (rivers), uses distance threshold since
        cell centroids rarely fall exactly on the line.
        """
        import h3
        latlng = h3.cell_to_latlng(cell.h3_id)
        pt = SPoint(latlng[1], latlng[0])
        # Direct intersect first
        intersecting = self.intersect(pt)
        # Also find LineString features within cell radius (~55km at H3 res 2)
        nearby = self.within_distance(pt, distance_degrees=0.5)
        for f in nearby:
            if f.geometry is not None and f.geometry.geom_type == "LineString":
                if f not in intersecting:
                    intersecting.append(f)
        cell.feature_ids = [f.feature_id for f in intersecting]
        return cell

    # ── Serialisation ───────────────────────────────────────────────

    def save_json(self, path: str) -> None:
        """Save all active features to a JSON file."""
        import json
        data = [f.to_dict() for f in self._features.values() if f.is_active]
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, ensure_ascii=False)

    @staticmethod
    def load_json(path: str) -> "FeatureStore":
        """Load features from a JSON file into a new FeatureStore."""
        import json
        store = FeatureStore()
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        for d in data:
            store.add_feature(Feature.from_dict(d))
        return store

    # ── Factory helpers ──────────────────────────────────────────────

    @staticmethod
    def make_polygon(
        lat_lon_rings: List[List[Tuple[float, float]]],
    ) -> Any:
        """Create a Shapely Polygon from lat/lon ring(s).

        First ring is the exterior, subsequent rings are holes.
        """
        rings = [[(lon, lat) for lat, lon in ring] for ring in lat_lon_rings]
        return Polygon(rings[0], rings[1:] if len(rings) > 1 else None)

    @staticmethod
    def make_linestring(
        lat_lon_points: List[Tuple[float, float]],
    ) -> Any:
        """Create a Shapely LineString from lat/lon points."""
        return LineString([(lon, lat) for lat, lon in lat_lon_points])

    @staticmethod
    def make_point(lat: float, lon: float) -> Any:
        """Create a Shapely Point from lat/lon."""
        return SPoint(lon, lat)
