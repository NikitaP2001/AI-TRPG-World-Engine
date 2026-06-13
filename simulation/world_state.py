"""WorldState — unified continuous world state.

Replaces CellData as the primary simulation data container.
Holds:
  - ContinuousFields (elevation, temperature, precipitation, etc.)
  - FeatureStore (rivers, lakes, biomes, tectonic plates, etc.)
  - World time
  - Parameters

Cells (H3) are only used as a SAMPLING grid for visualization.
All physics work on continuous fields evaluable at any (lat, lon).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .layer0.feature_store import Feature, FeatureStore
from .layer1.fields import (
    ContinuousField,
    FieldAccessor,
    FieldRegistry,
    MutableField,
)


# ======================================================================
# WorldState
# ======================================================================


class WorldState:
    """Unified continuous world state.

    Usage:
        ws = WorldState()
        ws.set_field("elevation", data)
        ws.field("temperature")(35.0, 120.0)  # evaluates at any point
        ws.features.all_active  # spatial features
        ws.time  # {"tick": ..., "year": ..., "day_of_year": ..., "hour": ...}
    """

    def __init__(self):
        self._fields = FieldRegistry()
        self._features = FeatureStore()
        self._time: dict = {
            "tick": 0,
            "year": 0,
            "day_of_year": 0.0,
            "hour": 0.0,
        }
        self._params: Dict[str, str] = {}
        self._discrete_fields: Dict[str, Dict[str, float]] = {}
        """"Discrete" cell-attributed data — crustal_age for each cell,
        useful for operations that need fast per-cell lookups.
        These are transient and rebuilt on load.
        """

    # ── Properties ───────────────────────────────────────────────

    @property
    def fields(self) -> FieldRegistry:
        return self._fields

    @property
    def features(self) -> FeatureStore:
        return self._features

    @features.setter
    def features(self, fs: FeatureStore) -> None:
        self._features = fs

    @property
    def time(self) -> dict:
        return self._time

    @time.setter
    def time(self, t: dict) -> None:
        self._time = dict(t)

    @property
    def params(self) -> Dict[str, str]:
        return self._params

    @params.setter
    def params(self, p: Dict[str, str]) -> None:
        self._params = dict(p)

    def get_param(self, key: str, default: str = "") -> str:
        return self._params.get(key, default)

    # ── Field access ──────────────────────────────────────────────

    def set_field(self, name: str, data, mutable: bool = False) -> None:
        """Register a continuous field.

        Args:
            name: Field name (e.g. "elevation", "temperature").
            data: Either a ContinuousField, a dict[h3_id → value],
                  or a list of (lat, lon, value) tuples.
            mutable: If True, creates a MutableField (can be modified by features).
        """
        if isinstance(data, ContinuousField):
            cf = data
        elif isinstance(data, dict):
            # Build from h3_id → value dict
            cf = self._build_field_from_dict(data)
        else:
            raise TypeError(f"Unsupported data type: {type(data)}")

        if mutable:
            mf = MutableField(cf) if cf is not None else MutableField(None)
            self._fields.register_mutable(name, mf)
        else:
            self._fields.register_base(name, cf)

    def field(self, name: str) -> FieldAccessor:
        """Get a field evaluator at any (lat, lon)."""
        return self._fields.get(name)

    def set_discrete(self, name: str, data: Dict[str, float]) -> None:
        """Register a discrete (per-cell) value map.

        These are NOT interpolated — they store exact values per H3 cell.
        Used for things like crustal_age that are cell-attribute data
        but needed by continuous field operations.
        """
        self._discrete_fields[name] = dict(data)

    def get_discrete(self, name: str) -> Dict[str, float]:
        """Get or create a discrete per-cell field (mutable)."""
        if name not in self._discrete_fields:
            self._discrete_fields[name] = {}
        return self._discrete_fields[name]

    # ── Build helpers ─────────────────────────────────────────────

    @staticmethod
    def _build_field_from_dict(data: Dict[str, float]) -> ContinuousField:
        """Build a ContinuousField from h3_id → value dict."""
        import h3 as _h3
        points = []
        values = []
        for hid, val in data.items():
            if val is None:
                continue
            latlng = _h3.cell_to_latlng(hid)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            points.append([
                math.cos(lat_r) * math.cos(lon_r),
                math.sin(lat_r),
                math.cos(lat_r) * math.sin(lon_r),
            ])
            values.append(float(val))
        if not points:
            raise ValueError("Empty data dict")
        tree = __import__("scipy.spatial", fromlist=["cKDTree"]).cKDTree(
            np.array(points, dtype=np.float64)
        )
        return ContinuousField(tree, np.array(values, dtype=np.float64))

    def has_field(self, name: str) -> bool:
        return self._fields.has(name)

    # ── Ephemeral CellData (for legacy layer0 functions) ─────────

    def to_celldata(self, h3_ids: List[str]) -> "List[CellData]":
        """Build ephemeral CellData objects from continuous fields.

        These are NOT saved to DB — they're only for legacy layer0
        functions that still operate on CellData. The fields remain
        the source of truth.
        """
        from .layer0.cell_model import CellData
        import h3 as _h3

        cells = []
        for hid in h3_ids:
            c = CellData(h3_id=hid, resolution=2)
            latlng = _h3.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]

            # Continuous fields
            for attr, fname in [
                ("elevation_mean", "elevation"),
                ("temperature", "temperature"),
                ("precipitation", "precipitation"),
                ("soil_fertility", "soil_fertility"),
                ("crustal_age_myr", "crustal_age"),
                ("crustal_thickness_km", "crustal_thickness"),
                ("thermal_gradient", "thermal_gradient"),
                ("sediment_thickness", "sediment_thickness"),
                ("porosity", "porosity"),
                ("bulk_density", "bulk_density"),
                ("cementation", "cementation"),
                ("sea_level_offset", "sea_level_offset"),
            ]:
                try:
                    setattr(c, attr, self.field(fname)(lat, lon))
                except KeyError:
                    pass

            # Discrete fields (all soil, vegetation, climate attrs)
            for attr, dname in [
                ("plate_id", "plate_id"),
                ("geological_type", "geological_type"),
                ("boundary_type", "boundary_type"),
                ("distance_to_boundary", "distance_to_boundary"),
                ("water_table_depth", "water_table_depth"),
                ("canopy_density", "canopy_density"),
                ("biomass_kgm2", "biomass_kgm2"),
                ("soil_fertility", "soil_fertility"),
                ("hazard_level", "hazard_level"),
                ("snowpack_mm", "snowpack_mm"),
                ("ice_thickness_m", "ice_thickness_m"),
                # Soil texture & chemistry
                ("clay_content", "clay_content"),
                ("sand_content", "sand_content"),
                ("silt_content", "silt_content"),
                ("soil_ph", "soil_ph"),
                ("cation_exchange", "cation_exchange"),
                ("organic_matter", "organic_matter"),
                ("soil_depth", "soil_depth"),
                # Vegetation
                ("interception_coefficient", "interception_coefficient"),
                # Climate
                ("precip_seasonality", "precip_seasonality"),
                ("runoff_ratio", "runoff_ratio"),
                ("effective_precip", "effective_precip"),
                # String fields (vegetation_cover, climate_class, bedrock_class)
                ("vegetation_cover", "vegetation_cover"),
                ("climate_class", "climate_class"),
                ("bedrock_class", "bedrock_class"),
            ]:
                val = self.get_discrete(dname).get(hid)
                if val is not None:
                    setattr(c, attr, val)
                elif attr in ("vegetation_cover",):
                    setattr(c, attr, "barren")
                elif attr in ("climate_class",):
                    setattr(c, attr, "")
                elif attr in ("bedrock_class",):
                    setattr(c, attr, "unknown")

            cells.append(c)
        return cells

    # ── H3 sampler (for GUI) ──────────────────────────────────────

    def sample_cells(self, h3_ids: List[str]) -> List[dict]:
        """Sample continuous fields at H3 cell centroids.

        This replaces load_cells() for the GUI — cells are computed
        on-the-fly from continuous fields, not stored.
        """
        import h3 as _h3
        rows = []
        for hid in h3_ids:
            latlng = _h3.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]
            row = {"h3_id": hid, "lat": lat, "lon": lon}

            # Sample continuous fields
            for fname in ("elevation", "temperature", "precipitation"):
                try:
                    row[fname] = self.field(fname)(lat, lon)
                except KeyError:
                    row[fname] = 0.0

            # Sample discrete fields
            for dname, dmap in self._discrete_fields.items():
                row[dname] = dmap.get(hid, 0.0)

            # Geological type from discrete or fallback
            gtype = self.get_discrete("geological_type").get(hid, 2)
            row["geological_type"] = int(gtype)
            row["is_ocean"] = 1 if gtype == 0 else 0

            rows.append(row)
        return rows
