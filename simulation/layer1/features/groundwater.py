"""Groundwater — multi-layer aquifer system with springs.

Causal chain:
  precip + canopy_interception → net_precip
  net_precip × infiltration_capacity → recharge
  recharge - evapotranspiration → water_table change
  water_table intersects surface → spring feature
  water_table near river → baseflow contribution

Physics:
  - Infiltration from soil texture + lithology porosity
  - Evapotranspiration from Penman-Monteith (climate module)
  - Specific yield from lithology (not magic 2.0)
  - Multi-layer aquifers separated by aquitards
  - Springs generated when water table > surface

Time integration:
  - day_of_year and hour affect ET (Penman uses instant temp)
  - Seasonal recharge cycle (winter recharge, summer ET)

Fields read:
  elevation, precipitation, temperature, wind
  soil_fertility, clay_content, sand_content
  lithology (via db)

Fields written:
  water_table_depth — lowered by ET, raised by recharge
  spring features — added to feature store when water table breaches
"""
from __future__ import annotations

import math
import uuid
from typing import Dict, List, Optional, Tuple

from shapely.geometry import Point

from .base import Feature
from ..fields import FieldRegistry


def _infiltration_capacity(clay: float, sand: float, canopy: float) -> float:
    """Infiltration capacity (0-1 fraction of precip that can infiltrate).

    Uses continuous K_sat from texture with direct linear interpolation (P2.1/P2.5).
    Sandy soils infiltrate nearly all; clay soils runoff more.
    Canopy intercepts then slowly releases.
    """
    k_sat_ms = _k_sat_from_texture(clay, sand) / 86400.0  # m/day → m/s
    # Direct interpolation: 1e-7 (clay) → 0, 1e-4 (sand) → 1
    infil = max(0.0, min(1.0, (k_sat_ms - 1e-7) / (1e-4 - 1e-7)))
    infil *= 0.7 + 0.3 * (1.0 - canopy)
    return min(1.0, max(0.05, infil))


def _k_sat_from_texture(clay: float, sand: float) -> float:
    """Estimate K_sat [m/day] from clay/sand fractions directly."""
    silt = 1.0 - clay - sand
    # Empirical: logKsat = a + b*clay + c*sand based on USDA data
    log_k = -2.0 + 3.0 * sand - 2.0 * clay
    return 10.0 ** max(-3.0, min(1.5, log_k))


def _specific_yield_from_texture(clay: float, sand: float) -> float:
    """Estimate specific yield (0-1) from clay/sand fractions."""
    return max(0.02, min(0.35, 0.35 - 0.3 * clay + 0.15 * sand))


def _specific_yield(clay: float, sand: float) -> float:
    """Drainable porosity: how much water releases per unit water table drop.
    
    Uses continuous texture estimate (replaces broken USDA dict lookup)."""
    return _specific_yield_from_texture(clay, sand)


# ======================================================================
# Aquifer layer descriptor
# ======================================================================


class AquiferLayer:
    """One aquifer or aquitard layer from lithology."""

    def __init__(self, rock_type: str, depth_top: float, depth_bottom: float,
                 porosity: float, permeability: float):
        self.rock_type = rock_type
        self.depth_top = depth_top
        self.depth_bottom = depth_bottom
        self.porosity = porosity
        self.permeability = permeability  # Darcy
        self.pressure_head: float = 0.0  # m above layer top
        self.saturation: float = 0.5     # 0-1

    @property
    def thickness(self) -> float:
        return self.depth_bottom - self.depth_top

    @property
    def is_aquifer(self) -> bool:
        return self.permeability > 0.01 and self.porosity > 0.05


# ======================================================================
# Spring Feature
# ======================================================================


class Spring(Feature):
    """A spring where groundwater emerges at the surface."""

    def __init__(self, lat: float, lon: float,
                 flow_rate: float,
                 temperature: float = 10.0,
                 source_depth: float = 10.0):
        super().__init__(
            feature_id=f"spring_{uuid.uuid4().hex[:8]}",
            name=f"Spring ({flow_rate:.1f} L/s)",
            geometry=Point(lon, lat),
            feature_type="spring",
            props={
                "flow_rate_ls": flow_rate,       # L/s
                "temperature_c": temperature,     # °C
                "source_depth_m": source_depth,   # m
                "perennial": True,
            },
        )

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Springs are passive — they record their existence only."""
        pass


# ======================================================================
# Groundwater system
# ======================================================================


class Groundwater(Feature):
    """Multi-layer groundwater system with time-aware dynamics.

    Singleton feature (one per world). Integrates with:
      - Soil texture for infiltration / specific yield
      - Lithology for aquifer layers
      - Climate (Penman ET via instant_temperature)
      - Time engine (day_of_year, hour)
      - Feature store (springs)
    """

    def __init__(self, feature_id: str = "groundwater_global"):
        super().__init__(
            feature_id=feature_id,
            name="Groundwater",
            geometry=None,
            feature_type="groundwater",
            props={},
        )
        self._grid: Dict[str, dict] = {}  # h3_id → state
        self._springs: List[Spring] = []

    # ── Main tick ──────────────────────────────────────────────────

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Update water table for all land cells — numpy vectorized."""
        import numpy as np
        from ...layer0.climate import potential_evap_mm_day, norm_to_c

        wt = fields.get_mutable("water_table_depth")
        elev_f = fields.get("elevation_mean")
        precip_f = fields.get("precipitation")
        temp_f = fields.get("temperature")
        soil_f = fields.get("soil_fertility")

        # Note: persistent effects are cleared centrally by SimEngine
        # before commit_effects. No manual wt._persistent.clear() needed.

        # Sample grid at 5° resolution (35×72 = 2520 points)
        step = 5
        lats = np.arange(-85, 86, step, dtype=np.float64)
        lons = np.arange(-175, 180, step, dtype=np.float64)
        nl, nlo = len(lats), len(lons)

        # ── 1. Pre-sample all fields into numpy arrays (single KDTree pass each) ──
        elev_g = np.zeros((nl, nlo))
        precip_g = np.zeros((nl, nlo))
        temp_g = np.zeros((nl, nlo))
        soil_g = np.zeros((nl, nlo))

        for i in range(nl):
            for j in range(nlo):
                lat, lon = lats[i], lons[j]
                elev_g[i, j] = elev_f.base_only(lat, lon)
                precip_g[i, j] = precip_f.base_only(lat, lon)
                temp_g[i, j] = temp_f.base_only(lat, lon)
                soil_g[i, j] = soil_f.base_only(lat, lon)

        # ── 2. Vectorized computations ──
        land = elev_g >= -0.01  # boolean mask

        # Soil texture estimates
        clay_est = 0.1 + soil_g * 0.4
        sand_est = 0.5 - soil_g * 0.3

        # Infiltration capacity
        log_k = -2.0 + 3.0 * sand_est - 2.0 * clay_est
        k_sat = 10.0 ** np.clip(log_k, -3.0, 1.5)
        infil = 0.1 + 0.9 * np.log(1.0 + k_sat * 10.0) / np.log(101)
        infil = np.clip(infil * 0.85, 0.05, 1.0)

        # Specific yield
        sy = np.clip(0.35 - 0.3 * clay_est + 0.15 * sand_est, 0.02, 0.35)

        # Temperature
        temp_c = norm_to_c(temp_g)

        # Penman ET (vectorized per grid point)
        abs_lat = np.abs(lats)[:, None] * np.ones((1, nlo))  # broadcast
        wind_ms = 5.0 + 2.0 * abs_lat / 90.0
        rh = 0.4 + 0.4 * (1.0 - abs_lat / 90.0)
        solar = 300.0 * np.maximum(0.05, np.cos(np.radians(abs_lat)))

        # Apply Penman per land cell (still needs loop — but only over land mask)
        recharge = np.where(land, precip_g * infil * dt, 0.0)
        evap = np.zeros_like(elev_g)

        for i in range(nl):
            for j in range(nlo):
                if land[i, j]:
                    penman_mm = potential_evap_mm_day(
                        float(temp_c[i, j]), float(rh[i, j]),
                        float(wind_ms[i, j]), float(solar[i, j]),
                    )
                    evap[i, j] = penman_mm / 100.0 * dt * 0.65

        net = recharge - evap

        # Water table change
        dwl = net / np.maximum(sy, 0.01)
        dwl = np.clip(dwl, -5.0, 5.0)

        # Current water table at grid points
        wt_current = np.zeros((nl, nlo))
        for i in range(nl):
            for j in range(nlo):
                if land[i, j]:
                    wt_current[i, j] = wt(float(lats[i]), float(lons[j]))

        wt_new = np.clip(wt_current - dwl, 0.0, 50.0)
        delta = wt_new - wt_current

        # ── 3. Write persistent effects ──
        self._springs.clear()
        radius_deg = float(step) * 0.6  # ~3° radius for 5° grid

        for i in range(nl):
            for j in range(nlo):
                if not land[i, j]:
                    continue
                d = delta[i, j]
                if abs(d) > 0.001:
                    wt.add_persistent(float(lats[i]), float(lons[j]),
                                      radius_deg=radius_deg, strength=d)

                # Spring generation
                if wt_new[i, j] < 1.0 and elev_g[i, j] > 0.01:
                    # Estimate slope from grid neighbours
                    s = 0.5
                    if i > 0 and i < nl - 1:
                        s = abs(elev_g[i+1, j] - elev_g[i-1, j]) * 0.5
                    if s > 0.1:
                        flow = max(0.1, (1.0 - wt_new[i, j]) * precip_g[i, j] * 10.0)
                        self._springs.append(Spring(
                            float(lats[i]), float(lons[j]),
                            flow_rate=flow, temperature=float(temp_c[i, j]),
                            source_depth=wt_new[i, j],
                        ))

        self.props["spring_count"] = len(self._springs)

    # ── Utility ────────────────────────────────────────────────────

    def set_time(self, day_of_year: float, hour: float) -> None:
        """Set current time for time-aware ET calculation."""
        self._day_of_year = day_of_year
        self._hour = hour

    def get_springs(self) -> List[Spring]:
        """Return and clear spring list."""
        springs = list(self._springs)
        self._springs.clear()
        return springs
