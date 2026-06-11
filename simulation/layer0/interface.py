"""Layer 0 — Formal read-only interface for higher simulation layers.

Higher layers (L1: ecology, L2: economics, L2.5: social norms, entity sim)
read from Layer 0 through this API. They never write directly — mutation
goes through the long-cycle tick system or the event bus.

Design doc § Interface with Higher Layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import h3
import pyarrow.parquet as pq

from .cell_model import CellData
from .feature_store import FeatureStore, Feature
from .subdivision import SubdivisionManager


# ======================================================================
# Query result types
# ======================================================================


@dataclass
class CellQuery:
    """Result of a single cell lookup."""
    cell: Optional[CellData]
    found: bool = False


@dataclass
class RegionQuery:
    """Result of a region query."""
    cells: List[CellData] = field(default_factory=list)
    count: int = 0


# ======================================================================
# Layer0API — read-only access to condition vectors
# ======================================================================


class Layer0API:
    """Read-only interface to Layer 0 condition vectors.

    All higher layers access world state exclusively through this API.
    Mutation is only possible via the long-cycle tick system or by
    reloading from a new Parquet file.

    Usage:
      api = Layer0API.from_parquet("data/planet_test/cells.parquet")
      cell = api.get_cell("82186ffffffffff")
      region = api.query_region(lat_min=10, lat_max=20, lon_min=-10, lon_max=10)
      for c in region.cells:
          print(c.elevation_mean, c.climate_class, c.soil_fertility)
    """

    def __init__(self, cells: List[CellData], feature_store: Optional[FeatureStore] = None) -> None:
        self._cells = cells
        self._by_id: Dict[str, CellData] = {c.h3_id: c for c in cells}
        self._feature_store = feature_store or FeatureStore()
        # Build spatial index by H3 parent for faster region queries
        self._by_parent: Dict[str, List[CellData]] = {}
        for c in cells:
            try:
                parent = h3.cell_to_parent(c.h3_id, c.resolution - 1) if c.resolution > 0 else c.h3_id
            except Exception:
                parent = c.h3_id
            self._by_parent.setdefault(parent, []).append(c)

    @classmethod
    def from_parquet(
        cls,
        path: Path,
        feature_store: Optional[FeatureStore] = None,
    ) -> Layer0API:
        """Load cells from a Parquet file written by generator.save_cells_parquet()."""
        table = pq.read_table(path)
        cells: List[CellData] = []
        for i in range(len(table)):
            cells.append(CellData(
                h3_id=str(table.column("h3_id")[i].as_py()),
                resolution=table.column("resolution")[i].as_py(),
                elevation_mean=float(table.column("elevation_mean")[i].as_py()),
                geological_type=table.column("geological_type")[i].as_py(),
                temperature=float(table.column("temperature")[i].as_py()),
                precipitation=float(table.column("precipitation")[i].as_py()),
                soil_fertility=float(table.column("soil_fertility")[i].as_py()),
                hazard_level=float(table.column("hazard_level")[i].as_py()),
                tectonic_stress=float(table.column("tectonic_stress")[i].as_py()),
                climate_class=str(table.column("climate_class")[i].as_py()),
            ))
        return cls(cells)

    # ── Single cell lookup ───────────────────────────────────────────

    def get_cell(self, h3_id: str) -> CellQuery:
        """Look up a single cell by its H3 ID."""
        cell = self._by_id.get(h3_id)
        return CellQuery(cell=cell, found=cell is not None)

    def get_cell_at(self, lat: float, lon: float, resolution: int = 2) -> CellQuery:
        """Look up the cell containing a geographic coordinate."""
        h3_id = h3.latlng_to_cell(lat, lon, resolution)
        return self.get_cell(h3_id)

    # ── Entity location resolution ───────────────────────────────────

    def get_entity_cell(self, entity_h3_id: str) -> CellQuery:
        """Resolve an entity's location to a cell.

        If the exact cell ID is not found (e.g. entity is at a finer
        resolution), walks up the H3 parent chain until a known cell
        is found.
        """
        query = self.get_cell(entity_h3_id)
        if query.found:
            return query

        # Walk up parents
        cur = entity_h3_id
        for _ in range(4):  # max 4 levels up
            try:
                cur = h3.cell_to_parent(cur, h3.get_resolution(cur) - 1)
            except Exception:
                break
            query = self.get_cell(cur)
            if query.found:
                return query

        return CellQuery(cell=None, found=False)

    # ── Region query ─────────────────────────────────────────────────

    def query_region(
        self,
        lat_min: float = -90.0,
        lat_max: float = 90.0,
        lon_min: float = -180.0,
        lon_max: float = 180.0,
    ) -> RegionQuery:
        """Return all cells whose centroids fall within the lat/lon box."""
        result: List[CellData] = []
        for cell in self._cells:
            latlng = h3.cell_to_latlng(cell.h3_id)
            lat, lon = latlng[0], latlng[1]
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                result.append(cell)
        return RegionQuery(cells=result, count=len(result))

    def query_parent(self, parent_h3_id: str) -> RegionQuery:
        """Return all cells whose parent (at res-1) matches."""
        cells = self._by_parent.get(parent_h3_id, [])
        return RegionQuery(cells=cells, count=len(cells))

    # ── Field accessors (convenience) ────────────────────────────────

    def get_field(self, field: str, h3_id: str) -> Optional[float]:
        """Read a single numeric field from a cell."""
        cell = self._by_id.get(h3_id)
        if cell is None:
            return None
        return getattr(cell, field, None)

    def query_by_field(
        self,
        field: str,
        min_val: float = 0.0,
        max_val: float = 1.0,
    ) -> RegionQuery:
        """Return cells where a numeric field is within [min_val, max_val]."""
        result: List[CellData] = []
        for cell in self._cells:
            val = getattr(cell, field, None)
            if val is not None and min_val <= val <= max_val:
                result.append(cell)
        return RegionQuery(cells=result, count=len(result))

    # ── Metadata ─────────────────────────────────────────────────────

    @property
    def cell_count(self) -> int:
        return len(self._cells)

    @property
    def all_cells(self) -> List[CellData]:
        return self._cells

    def climate_summary(self) -> Dict[str, int]:
        """Count cells per Köppen-Geiger class."""
        counts: Dict[str, int] = {}
        for cell in self._cells:
            cls = cell.climate_class or "unknown"
            counts[cls] = counts.get(cls, 0) + 1
        return dict(sorted(counts.items()))

    # ── Feature store integration ────────────────────────────────────

    @property
    def feature_store(self) -> FeatureStore:
        return self._feature_store

    def get_features_at(self, lat: float, lon: float) -> List[Feature]:
        """All features containing this point, most specific first."""
        return self._feature_store.at_point(lat, lon)

    def get_features_by_type(self, type_name: str) -> List[Feature]:
        return self._feature_store.get_features_by_type(type_name)

    def get_features_in_region(
        self, lat_min: float, lat_max: float,
        lon_min: float, lon_max: float,
    ) -> List[Feature]:
        """Features intersecting the lat/lon bounding box."""
        from shapely.geometry import box
        bbox = box(lon_min, lat_min, lon_max, lat_max)
        return self._feature_store.intersect(bbox)

    def feature_summary(self) -> Dict[str, int]:
        """Count features by type."""
        counts: Dict[str, int] = {}
        for f in self._feature_store.all_active:
            counts[f.type] = counts.get(f.type, 0) + 1
        return dict(sorted(counts.items()))
