"""Layer 1 — Continuous Field Registry.

Every physical quantity is a continuous field (lat, lon) → value.
Fields are backed by KDTree-IDW interpolation from cell data,
with a per-tick effect stack for feature-induced mutations.

KDTree-IDW gives O(log N) queries at meter precision anywhere on the sphere.
Effects accumulate as (lat, lon, radius, strength) tuples and fall off
with inverse-squared distance, so features never touch grid cells
directly — they write field effects, and fields interpolate continuously.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree


# ======================================================================
# Spherical distance (haversine) for effect falloff
# ======================================================================


def _haversine_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in degrees between two lat/lon points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    a = max(0.0, min(1.0, a))
    return math.degrees(2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


# ======================================================================
# Continuous field from cell data (immutable base)
# ======================================================================


class ContinuousField:
    """Evaluate a cell field at ANY (lat, lon) via KDTree-IDW.

    Same as simulation.layer0.contouring.ContinuousField but self-contained
    here to avoid circular imports.
    """

    def __init__(self, tree: cKDTree, values: np.ndarray):
        self._tree = tree
        self._values = values

    @classmethod
    def from_cells(cls, cells: List, attribute: str) -> "ContinuousField":
        """Build from a list of objects with .h3_id and attribute."""
        import h3
        points = []
        vals = []
        for c in cells:
            latlng = h3.cell_to_latlng(c.h3_id)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            points.append([
                math.cos(lat_r) * math.cos(lon_r),
                math.sin(lat_r),
                math.cos(lat_r) * math.sin(lon_r),
            ])
            vals.append(getattr(c, attribute, 0.0))
        tree = cKDTree(np.array(points, dtype=np.float64))
        return cls(tree, np.array(vals, dtype=np.float64))

    def __call__(self, lat: float, lon: float) -> float:
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        px = math.cos(lat_r) * math.cos(lon_r)
        py = math.sin(lat_r)
        pz = math.cos(lat_r) * math.sin(lon_r)

        dists, idxs = self._tree.query([px, py, pz], k=3)
        if np.any(dists < 1e-15):
            return float(self._values[idxs[0]])
        w = 1.0 / (dists + 1e-15)
        return float(np.average(self._values[idxs], weights=w))


# ======================================================================
# Mutable field with per-tick effect stack
# ======================================================================


class MutableField:
    """A continuous field that features can modify each tick.

    Base value comes from a ContinuousField (Layer 0 data).
    Feature effects are accumulated as (lat, lon, radius, strength)
    and applied with Gaussian-squared falloff.

    Usage:
        mf = MutableField(base_field)
        mf.add_effect(35.0, 120.0, radius=2.0, strength=-0.3)
        val = mf(35.5, 120.5)  # reads base + nearby effects
        mf.clear_effects()      # called at end of tick
    """

    def __init__(self, base: ContinuousField, default: float = 0.0):
        self._base = base
        self._default = default
        self._effects: List[Tuple[float, float, float, float]] = []
        # Optional: persistent modifications that survive clear_effects
        self._persistent: List[Tuple[float, float, float, float]] = []

    def add_effect(self, lat: float, lon: float,
                   radius_deg: float, strength: float) -> None:
        """Register a temporary effect for this tick."""
        self._effects.append((lat, lon, radius_deg, strength))

    def add_persistent(self, lat: float, lon: float,
                       radius_deg: float, strength: float) -> None:
        """Register a permanent modification (survives ticks)."""
        self._persistent.append((lat, lon, radius_deg, strength))

    def clear_effects(self) -> None:
        """Clear temporary effects (call at end of each tick)."""
        self._effects.clear()

    def __call__(self, lat: float, lon: float) -> float:
        if self._base is not None:
            val = self._base(lat, lon)
        else:
            val = self._default

        for elat, elon, radius, strength in self._effects:
            d = _haversine_deg(lat, lon, elat, elon)
            if d < radius:
                # Quadratic falloff: full at center, zero at radius
                w = (1.0 - d / radius) ** 2
                val += strength * w

        for elat, elon, radius, strength in self._persistent:
            d = _haversine_deg(lat, lon, elat, elon)
            if d < radius:
                w = (1.0 - d / radius) ** 2
                val += strength * w

        return val


# ======================================================================
# Field registry
# ======================================================================


class FieldRegistry:
    """Holds all continuous fields for the simulation.

    Read-only fields (elevation, temperature, precip, soil) are
    wrapped in BaseField for fast direct access.
    Mutable fields (water_table, soil_moisture, biomass) have
    per-tick effect stacks.
    """

    def __init__(self):
        self._base_fields: Dict[str, ContinuousField] = {}
        self._mutable_fields: Dict[str, MutableField] = {}

    def register_base(self, name: str, field: ContinuousField) -> None:
        """Register a read-only field (elevation, temp, precip, soil)."""
        self._base_fields[name] = field

    def register_mutable(self, name: str, field: MutableField) -> None:
        """Register a mutable field (water_table, biomass, etc.)."""
        self._mutable_fields[name] = field

    def get(self, name: str) -> "FieldAccessor":
        """Get a field accessor. Auto-detects base vs mutable."""
        if name in self._base_fields:
            return FieldAccessor(self._base_fields[name], None)
        if name in self._mutable_fields:
            mf = self._mutable_fields[name]
            return FieldAccessor(mf._base if mf._base is not None else None, mf)
        raise KeyError(f"Unknown field: {name}")

    def has(self, name: str) -> bool:
        return name in self._base_fields or name in self._mutable_fields

    def clear_all_effects(self) -> None:
        """Clear temporary effects from all mutable fields (end of tick)."""
        for f in self._mutable_fields.values():
            f.clear_effects()

    def get_mutable(self, name: str) -> MutableField:
        """Direct access to MutableField for adding effects."""
        return self._mutable_fields[name]

    @classmethod
    def from_cells(cls, cells: List) -> "FieldRegistry":
        """Build a standard registry from Layer 0 cells."""
        import h3
        import numpy as np
        reg = cls()
        for attr in ("elevation_mean", "temperature", "precipitation"):
            base = ContinuousField.from_cells(cells, attr)
            reg.register_base(attr, base)

        # Soil fertility is mutable (biomes modify it via litterfall)
        soil_base = ContinuousField.from_cells(cells, "soil_fertility")
        reg.register_mutable("soil_fertility", MutableField(soil_base))

        # ── Wind components (from cell prevailing_wind tuple) ────────────
        # Build shared KDTree for wind and texture fields
        pts = []
        for c in cells:
            latlng = h3.cell_to_latlng(c.h3_id)
            lat_r = math.radians(latlng[0])
            lon_r = math.radians(latlng[1])
            pts.append([
                math.cos(lat_r) * math.cos(lon_r),
                math.sin(lat_r),
                math.cos(lat_r) * math.sin(lon_r),
            ])
        tree = cKDTree(np.array(pts, dtype=np.float64))

        wind_u_vals = np.array([
            c.prevailing_wind[0] if c.prevailing_wind else 0.0 for c in cells
        ], dtype=np.float64)
        wind_v_vals = np.array([
            c.prevailing_wind[1] if c.prevailing_wind else 0.0 for c in cells
        ], dtype=np.float64)
        reg.register_base("wind_u", ContinuousField(tree, wind_u_vals))
        reg.register_base("wind_v", ContinuousField(tree, wind_v_vals))

        # ── Soil texture fields (for K_sat estimation) ──────────────────
        clay_vals = np.array([c.clay_content for c in cells], dtype=np.float64)
        sand_vals = np.array([c.sand_content for c in cells], dtype=np.float64)
        reg.register_base("clay_content", ContinuousField(tree, clay_vals))
        reg.register_base("sand_content", ContinuousField(tree, sand_vals))

        # Mutable fields (start with defaults, built by features)
        from numpy import zeros, float64
        dummy = zeros(1, dtype=float64)
        reg.register_mutable("water_table_depth", MutableField(
            ContinuousField.from_cells(cells, "elevation_mean") if cells else None,
            default=10.0
        ))
        reg.register_mutable("soil_moisture", MutableField(None, default=0.3))
        reg.register_mutable("biomass", MutableField(None, default=0.0))
        reg.register_mutable("canopy_density", MutableField(None, default=0.0))
        reg.register_mutable("sediment_flux", MutableField(None, default=0.0))
        return reg


class FieldAccessor:
    """Wrapper for field access. Delegates to base or mutable field."""

    def __init__(self, base_field: Optional[ContinuousField],
                 mutable_field: Optional[MutableField]):
        self._base = base_field
        self._mutable = mutable_field

    def __call__(self, lat: float, lon: float) -> float:
        if self._mutable is not None:
            return self._mutable(lat, lon)
        if self._base is not None:
            return self._base(lat, lon)
        return 0.0

    def base_only(self, lat: float, lon: float) -> float:
        """Read only the base value (bypass effects). Use for fields
        that should not have feature effects applied (elevation)."""
        if self._base is not None:
            return self._base(lat, lon)
        return 0.0
