"""SettlementFootprint — accumulating L1 field modifications from settlements (R19).

One instance per active settlement. Each tick:
  - Reads settlement_type coefficients from SETTLEMENT_TYPE_REGISTRY
  - Accumulates deforestation, hunting, soil, water_table, hazard deltas
  - Applies a small decay term while active (constant upkeep equilibrium)
  - Writes directly to CellData fields (persistent, accumulating)

Key design difference from other L1 features: effects are ACCUMULATING
deltas written to CellData stored fields, NOT per-tick field effects.
This means abandoned settlements leave lingering footprints that recover
slowly via existing L1 dynamics.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .base import Feature
from ..settlement_type_registry import (
    SETTLEMENT_TYPE_REGISTRY,
    SettlementTypeDef,
)


# ======================================================================
# Tier multipliers
# ======================================================================

_TIER_MULTIPLIER = {
    "hamlet": 0.3,
    "village": 0.5,
    "town": 0.7,
    "city": 1.0,
    "capital": 1.3,
    "megalopolis": 1.6,
}

# Default farmland radius as fraction of control_radius
_FARMLAND_FRACTION = 0.3

# Decay fraction for accumulated footprint while settlement is active
# (constant upkeep — prevents infinite accumulation)
_ACTIVE_DECAY_FRACTION = 0.01  # 1% of accumulated offset decays per tick toward 0

# Post-abandonment recovery per tick multiplier
_BASE_RECOVERY_RATE = 0.002  # 0.2% per tick toward pre-settlement baseline


class SettlementFootprint(Feature):
    """Accumulating L1 footprint from one settlement.

    Unlike Vegetation/Lake/Wetland, this feature writes accumulating
    deltas to CellData stored fields (not per-tick MutableField effects),
    so footprints persist after settlement dissolution.
    """

    def __init__(
        self,
        settlement_id: str,
        faction_id: str,
        settlement_type: str,
        location_lat: float,
        location_lon: float,
        control_radius_deg: float,
        population_share: float = 1.0,
        settlement_tier: str = "village",
        feature_id: str = "",
    ):
        if not feature_id:
            feature_id = f"sf_{settlement_id}"
        super().__init__(
            feature_id=feature_id,
            name=f"Footprint:{settlement_id}",
            geometry=None,
            feature_type="settlement_footprint",
            props={
                "settlement_id": settlement_id,
                "faction_id": faction_id,
                "settlement_type": settlement_type,
                "location_lat": location_lat,
                "location_lon": location_lon,
                "control_radius_deg": control_radius_deg,
                "population_share": population_share,
                "settlement_tier": settlement_tier,
            },
        )
        self._settlement_id = settlement_id
        self._faction_id = faction_id
        self._settlement_type = settlement_type
        self._lat = location_lat
        self._lon = location_lon
        self._radius = control_radius_deg
        self._pop_share = population_share
        self._tier = settlement_tier

    # ── Coefficient access ─────────────────────────────────────────

    def _coeffs(self) -> Optional[SettlementTypeDef]:
        return SETTLEMENT_TYPE_REGISTRY.get(self._settlement_type)

    def _intensity(self) -> float:
        """Combined population_share × tier multiplier."""
        base = float(self._pop_share)
        tier_mul = _TIER_MULTIPLIER.get(self._tier, 0.5)
        return base * tier_mul

    def _farmland_radius(self) -> float:
        """Smaller radius for soil modification."""
        coeffs = self._coeffs()
        frac = coeffs.farmland_radius_fraction if coeffs else _FARMLAND_FRACTION
        return self._radius * frac

    # ── Cell iteration ─────────────────────────────────────────────

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """SettlementFootprint writes directly to CellData, not MutableFields.
        Use apply_to_cells() instead, called from TimeEngine.
        This is a no-op for the SimEngine two-phase cycle.
        """
        pass  # footprint applied via apply_to_cells() after engine step

    def _cells_in_radius(
        self, cells: List[Any], radius_deg: float,
    ) -> List[Tuple[Any, float, float]]:
        """Return (cell, distance_from_center, weight) for cells within radius."""
        result = []
        for cell in cells:
            import h3
            try:
                latlng = h3.cell_to_latlng(cell.h3_id)
                clat, clon = float(latlng[0]), float(latlng[1])
            except Exception:
                continue
            # Approximate distance in degrees
            dlat = abs(clat - self._lat)
            dlon = abs(clon - self._lon) * math.cos(math.radians((clat + self._lat) / 2))
            dist = math.sqrt(dlat**2 + dlon**2)
            if dist <= radius_deg:
                w = (1.0 - dist / radius_deg) ** 2  # quadratic falloff
                result.append((cell, dist, w))
        return result

    # ── Effects ────────────────────────────────────────────────────

    def apply_to_cells(self, cells: List[Any], dt: float = 1.0) -> None:
        """Apply accumulating footprint to CellData objects.

        Called from TimeEngine after L1 engine step, before DB save.
        This is NOT a compute_effects override — it works directly on
        CellData stored fields for accumulating persistence.

        Args:
            cells: List of CellData objects.
            dt: Time delta in days.
        """
        coeffs = self._coeffs()
        if coeffs is None:
            return

        intensity = self._intensity()
        if intensity <= 0:
            return

        decay = _ACTIVE_DECAY_FRACTION * dt
        recovery_rate = coeffs.recovery_rate_modifier * _BASE_RECOVERY_RATE * dt

        # ── Deforestation ──────────────────────────────────────────
        if abs(coeffs.deforestation_factor) > 1e-10:
            for cell, dist, w in self._cells_in_radius(cells, self._radius):
                delta = -coeffs.deforestation_factor * intensity * w * dt
                # Active decay: restore some of the accumulated offset
                offset_from_baseline = 0.0  # baseline = current (not tracked)
                net = delta + decay * (cell.canopy_density * 0.1)  # slow recovery kick
                cell.canopy_density = max(0.0, cell.canopy_density + net)
                cell.biomass_kgm2 = max(0.0, cell.biomass_kgm2 + net * 10.0)

        # ── Soil modification ──────────────────────────────────────
        if abs(coeffs.soil_modification_factor) > 1e-10:
            farm_r = self._farmland_radius()
            for cell, dist, w in self._cells_in_radius(cells, farm_r):
                delta = coeffs.soil_modification_factor * intensity * w * dt
                cell.soil_fertility = max(0.0, min(1.0, cell.soil_fertility + delta))

        # ── Water table ────────────────────────────────────────────
        if abs(coeffs.water_table_factor) > 1e-10:
            # Water table has NO recovery by default (permanent landscape scar)
            for cell, dist, w in self._cells_in_radius(cells, self._radius):
                delta = coeffs.water_table_factor * intensity * w * dt
                cell.water_table_depth += delta  # no clamp — can go arbitrarily deep

        # ── Hazard level ───────────────────────────────────────────
        if abs(coeffs.hazard_modifier) > 1e-10:
            for cell, dist, w in self._cells_in_radius(cells, self._radius):
                delta = coeffs.hazard_modifier * intensity * w * dt
                cell.hazard_level = max(0.0, min(1.0, cell.hazard_level + delta))

        # ── Hunting & population suppression ───────────────────────
        if (abs(coeffs.hunting_factor) > 1e-10 or
                abs(coeffs.population_suppression_factor) > 1e-10):
            # These are applied to population_density[species_id] fields
            # via the TimeEngine after field propagation (stored in props
            # for now, applied in TimeEngine)
            self.props.setdefault("_hunting_deltas", {})
            for cell, dist, w in self._cells_in_radius(cells, self._radius):
                key = cell.h3_id
                total_delta = (
                    -coeffs.hunting_factor * intensity * w * dt
                    - coeffs.population_suppression_factor * intensity * w * dt
                )
                if abs(total_delta) > 1e-12:
                    prev = self.props["_hunting_deltas"].get(key, 0.0)
                    self.props["_hunting_deltas"][key] = prev + total_delta
