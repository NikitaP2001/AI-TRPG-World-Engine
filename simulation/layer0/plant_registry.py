"""Plant Functional Type (PFT) Registry — species definitions for continuous vegetation model.

Each PFT defines a climate envelope, growth parameters, and material properties.
The vegetation model uses these to compute continuous canopy, biomass, and litter
per cell, replacing the old discrete biome-type system.

Usage:
    from simulation.layer0.plant_registry import PFT_REGISTRY, PlantDef, compute_suitability
    for pft in PFT_REGISTRY.values():
        suit = compute_suitability(temp, precip, pft)
        biomass = pft.max_biomass_kgm2 * suit
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ======================================================================
# Plant Functional Type definition
# ======================================================================


@dataclass
class PlantDef:
    """One plant functional type (PFT) — a species or group with similar ecology."""

    # ── Identity ─────────────────────────────────────────────────────
    name: str
    common_name: str = ""
    family: str = "unknown"        # conifer, deciduous, evergreen, grass, shrub, succulent, moss
    growth_form: str = "tree"      # tree, shrub, grass, herb, moss, vine

    # ── Climate envelope (normalised 0–1) ────────────────────────────
    temp_min: float = 0.0
    temp_opt_min: float = 0.3
    temp_opt_max: float = 0.7
    temp_max: float = 1.0
    precip_min: float = 0.0
    precip_opt_min: float = 0.2
    precip_opt_max: float = 0.8
    precip_max: float = 1.0

    # ── Soil preferences ─────────────────────────────────────────────
    ph_min: float = 4.0
    ph_max: float = 8.5
    fertility_min: float = 0.0      # minimum soil_fertility to survive
    shade_tolerance: float = 0.3    # 0 = intolerant (pioneer), 1 = very tolerant

    # ── Growth ───────────────────────────────────────────────────────
    max_height_m: float = 10.0
    max_biomass_kgm2: float = 10.0      # equilibrium above-ground biomass
    max_canopy_density: float = 0.7     # 0–1 fractional cover at max biomass
    growth_rate: float = 0.1            # per-year fraction of max (0–1)
    mortality_rate: float = 0.02        # per-year background mortality

    # ── Phenology ────────────────────────────────────────────────────
    leaf_type: str = "evergreen"        # evergreen, deciduous, semi-deciduous
    dormancy_temp: float = -1.0         # below this, growth stops (-1 = never)
    dormancy_precip: float = 0.0        # below this, drought dormancy

    # ── Litter & biogeochemistry ─────────────────────────────────────
    litterfall_rate: float = 0.1        # fraction of biomass → litter per year
    litter_cn_ratio: float = 30.0       # C:N ratio (affects decomposition speed)

    # ── Material properties ──────────────────────────────────────────
    wood_density_gcm3: float = 0.5
    carbon_fraction: float = 0.47
    timber_quality: float = 0.0         # 0–1
    fruit_yield_kgm2: float = 0.0       # annual edible yield at full biomass
    medicinal: bool = False
    ornamental: bool = False

    # ── Water interception (P1.7) ────────────────────────────────────
    leaf_area_index: float = 0.0        # m²/m², 0 = auto from leaf_type
    interception_max: float = 0.0       # max fraction 0-1, 0 = auto from leaf_type

    # ── Rooting ──────────────────────────────────────────────────────
    root_depth_m: float = 1.0
    drought_tolerance: float = 0.3      # 0–1

    def suitability(self, temp: float, precip: float,
                    fertility: float = 0.5, ph: float = 7.0) -> float:
        """Combined climate + soil suitability, 0–1."""
        # Temperature response (skewed bell)
        temp_s = _bell_skew(temp, self.temp_min, self.temp_opt_min,
                            self.temp_opt_max, self.temp_max)
        # Precipitation response
        prec_s = _bell_skew(precip, self.precip_min, self.precip_opt_min,
                            self.precip_opt_max, self.precip_max)
        # Soil fertility clamp
        fert_s = 1.0 if fertility >= self.fertility_min else (
            fertility / max(1e-6, self.fertility_min) * 0.5
        )
        # pH tolerance
        if ph < self.ph_min or ph > self.ph_max:
            ph_s = 0.0
        elif ph < (self.ph_min + 1.0):
            ph_s = (ph - self.ph_min) / 1.0
        elif ph > (self.ph_max - 1.0):
            ph_s = (self.ph_max - ph) / 1.0
        else:
            ph_s = 1.0

        return temp_s * prec_s * fert_s * ph_s

    def equilibrium_biomass(self, temp: float, precip: float,
                            fertility: float = 0.5, ph: float = 7.0) -> float:
        """Above-ground biomass at climate equilibrium, kg/m²."""
        suit = self.suitability(temp, precip, fertility, ph)
        if suit < 0.05:
            return 0.0
        return self.max_biomass_kgm2 * suit ** 1.2

    def canopy_from_biomass(self, biomass: float) -> float:
        """Canopy density (0–1) for given biomass."""
        if biomass <= 0:
            return 0.0
        frac = biomass / max(1e-6, self.max_biomass_kgm2)
        return self.max_canopy_density * min(1.0, frac ** 0.7)

    def growth_factor(self, temp: float, precip: float,
                      day_of_year: float = 172.0) -> float:
        """Seasonal growth multiplier: 0 = dormant, 1 = full growth.

        Accounts for winter dormancy (temperatures below threshold)
        and drought dormancy.
        """
        if temp < self.dormancy_temp:
            return 0.0
        if precip < self.dormancy_precip:
            return 0.2  # partial dormancy during drought
        # Northern/southern hemisphere winter check using day_of_year
        return 1.0


# ======================================================================
# Bell-shaped response functions
# ======================================================================


def _bell_skew(x: float, x_min: float, x_opt_low: float,
               x_opt_high: float, x_max: float) -> float:
    """Skewed bell curve: 0 outside [x_min, x_max], 1 on [x_opt_low, x_opt_high]."""
    if x < x_min or x > x_max:
        return 0.0
    if x_opt_low <= x <= x_opt_high:
        return 1.0
    if x < x_opt_low:
        # Rising limb
        dx = x_opt_low - x_min
        if dx <= 0:
            return 1.0
        t = (x - x_min) / dx
        return t * t * (3 - 2 * t)  # smoothstep
    else:
        # Falling limb
        dx = x_max - x_opt_high
        if dx <= 0:
            return 1.0
        t = (x_max - x) / dx
        return t * t * (3 - 2 * t)


# ======================================================================
# PFT-specific water interception (P1.7)
# ======================================================================

_DEFAULT_LAI_BY_LEAF_TYPE = {
    "needleleaf":     6.0,   # хвойные — макс. перехват
    "evergreen":      5.0,   # вечнозелёные широколиственные
    "semi-deciduous": 3.5,   # полулистопадные
    "deciduous":      4.5,   # листопадные
    "grass":          2.5,   # травы
    "shrub":          1.5,   # кустарники
    "succulent":      0.8,   # суккуленты
    "moss":           2.0,   # мхи
}

_MAX_INTERCEPTION_BY_LEAF_TYPE = {
    "needleleaf":     0.35,  # хвойные: до 35%
    "evergreen":      0.25,  # вечнозелёные: до 25%
    "semi-deciduous": 0.22,  # до 22%
    "deciduous":      0.20,  # листопадные: до 20%
    "grass":          0.12,  # травы: до 12%
    "shrub":          0.15,  # кустарники: до 15%
    "succulent":      0.08,  # до 8%
    "moss":           0.25,  # мхи: до 25%
}

_RAIN_EXTINCTION_COEFF = 0.15  # k в законе Бера для дождя


def pft_interception_coefficient(pft: PlantDef) -> float:
    """PFT-specific interception coefficient 0-1.

    I_max = a * (1 - exp(-LAI * k))
    где a = max interception fraction (leaf_type),
        k = _RAIN_EXTINCTION_COEFF (0.15),
        LAI = pft.leaf_area_index or auto-detect from leaf_type
    """
    lai = pft.leaf_area_index or _DEFAULT_LAI_BY_LEAF_TYPE.get(pft.leaf_type, 4.0)
    a_max = pft.interception_max or _MAX_INTERCEPTION_BY_LEAF_TYPE.get(pft.leaf_type, 0.15)
    return a_max * (1.0 - math.exp(-lai * _RAIN_EXTINCTION_COEFF))


# ======================================================================
# PFT Registry
# ======================================================================

PFT_REGISTRY: Dict[str, PlantDef] = {}


def _init_pfts():
    """Populate PFT_REGISTRY with default plant functional types."""
    global PFT_REGISTRY
    PFT_REGISTRY = {}

    _add(PlantDef(
        name="pine_scots", common_name="Scots Pine",
        family="conifer", growth_form="tree",
        temp_min=0.05, temp_opt_min=0.15, temp_opt_max=0.45, temp_max=0.60,
        precip_min=0.10, precip_opt_min=0.20, precip_opt_max=0.60, precip_max=0.80,
        ph_min=4.5, ph_max=8.0, fertility_min=0.05,
        max_height_m=35, max_biomass_kgm2=15, max_canopy_density=0.65,
        growth_rate=0.08, mortality_rate=0.015,
        leaf_type="evergreen", dormancy_temp=0.05,
        litterfall_rate=0.06, litter_cn_ratio=50,
        wood_density_gcm3=0.52, timber_quality=0.6,
        root_depth_m=2.0, drought_tolerance=0.5,
    ))
    _add(PlantDef(
        name="spruce_norway", common_name="Norway Spruce",
        family="conifer", growth_form="tree",
        temp_min=0.02, temp_opt_min=0.10, temp_opt_max=0.35, temp_max=0.55,
        precip_min=0.15, precip_opt_min=0.25, precip_opt_max=0.70, precip_max=0.90,
        ph_min=4.0, ph_max=7.5, fertility_min=0.08,
        max_height_m=45, max_biomass_kgm2=20, max_canopy_density=0.75,
        growth_rate=0.06, mortality_rate=0.01,
        leaf_type="evergreen", dormancy_temp=0.04,
        litterfall_rate=0.05, litter_cn_ratio=60,
        wood_density_gcm3=0.42, timber_quality=0.55,
        root_depth_m=1.5, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="fir_silver", common_name="Silver Fir",
        family="conifer", growth_form="tree",
        temp_min=0.03, temp_opt_min=0.12, temp_opt_max=0.38, temp_max=0.55,
        precip_min=0.20, precip_opt_min=0.30, precip_opt_max=0.75, precip_max=0.90,
        ph_min=5.0, ph_max=7.5, fertility_min=0.10,
        max_height_m=50, max_biomass_kgm2=25, max_canopy_density=0.80,
        growth_rate=0.05, mortality_rate=0.008,
        leaf_type="evergreen", dormancy_temp=0.04,
        litterfall_rate=0.04, litter_cn_ratio=55,
        wood_density_gcm3=0.45, timber_quality=0.65,
        root_depth_m=2.0, drought_tolerance=0.25,
    ))
    _add(PlantDef(
        name="oak_english", common_name="English Oak",
        family="deciduous", growth_form="tree",
        temp_min=0.08, temp_opt_min=0.20, temp_opt_max=0.55, temp_max=0.70,
        precip_min=0.15, precip_opt_min=0.25, precip_opt_max=0.65, precip_max=0.85,
        ph_min=5.0, ph_max=8.0, fertility_min=0.12,
        max_height_m=30, max_biomass_kgm2=18, max_canopy_density=0.70,
        growth_rate=0.07, mortality_rate=0.012,
        leaf_type="deciduous", dormancy_temp=0.08,
        litterfall_rate=0.12, litter_cn_ratio=40,
        wood_density_gcm3=0.75, timber_quality=0.8,
        root_depth_m=3.0, drought_tolerance=0.4,
    ))
    _add(PlantDef(
        name="birch_silver", common_name="Silver Birch",
        family="deciduous", growth_form="tree",
        temp_min=0.02, temp_opt_min=0.08, temp_opt_max=0.45, temp_max=0.60,
        precip_min=0.10, precip_opt_min=0.15, precip_opt_max=0.55, precip_max=0.75,
        ph_min=4.0, ph_max=7.5, fertility_min=0.05,
        max_height_m=25, max_biomass_kgm2=10, max_canopy_density=0.55,
        growth_rate=0.12, mortality_rate=0.025,  # pioneer species
        leaf_type="deciduous", dormancy_temp=0.04,
        litterfall_rate=0.15, litter_cn_ratio=35,
        wood_density_gcm3=0.62, timber_quality=0.4,
        root_depth_m=1.5, drought_tolerance=0.35,
    ))
    _add(PlantDef(
        name="maple_sugar", common_name="Sugar Maple",
        family="deciduous", growth_form="tree",
        temp_min=0.06, temp_opt_min=0.15, temp_opt_max=0.50, temp_max=0.65,
        precip_min=0.15, precip_opt_min=0.25, precip_opt_max=0.60, precip_max=0.80,
        ph_min=5.0, ph_max=7.5, fertility_min=0.12,
        max_height_m=35, max_biomass_kgm2=20, max_canopy_density=0.75,
        growth_rate=0.06, mortality_rate=0.01,
        leaf_type="deciduous", dormancy_temp=0.06,
        litterfall_rate=0.10, litter_cn_ratio=45,
        wood_density_gcm3=0.68, timber_quality=0.7,
        root_depth_m=2.5, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="beech_european", common_name="European Beech",
        family="deciduous", growth_form="tree",
        temp_min=0.07, temp_opt_min=0.18, temp_opt_max=0.52, temp_max=0.68,
        precip_min=0.15, precip_opt_min=0.25, precip_opt_max=0.65, precip_max=0.85,
        ph_min=5.5, ph_max=8.0, fertility_min=0.15,
        max_height_m=40, max_biomass_kgm2=25, max_canopy_density=0.80,
        growth_rate=0.05, mortality_rate=0.008,
        leaf_type="deciduous", dormancy_temp=0.06,
        litterfall_rate=0.11, litter_cn_ratio=42,
        wood_density_gcm3=0.72, timber_quality=0.75,
        root_depth_m=2.0, drought_tolerance=0.25,
    ))
    _add(PlantDef(
        name="mahogany", common_name="Mahogany",
        family="evergreen", growth_form="tree",
        temp_min=0.50, temp_opt_min=0.60, temp_opt_max=0.90, temp_max=1.0,
        precip_min=0.35, precip_opt_min=0.50, precip_opt_max=0.95, precip_max=1.0,
        ph_min=5.0, ph_max=7.5, fertility_min=0.15,
        max_height_m=45, max_biomass_kgm2=35, max_canopy_density=0.85,
        growth_rate=0.04, mortality_rate=0.005,
        leaf_type="evergreen",
        litterfall_rate=0.08, litter_cn_ratio=50,
        wood_density_gcm3=0.85, timber_quality=0.9,
        root_depth_m=3.0, drought_tolerance=0.2,
    ))
    _add(PlantDef(
        name="teak", common_name="Teak",
        family="deciduous", growth_form="tree",
        temp_min=0.50, temp_opt_min=0.60, temp_opt_max=0.90, temp_max=1.0,
        precip_min=0.30, precip_opt_min=0.45, precip_opt_max=0.85, precip_max=1.0,
        ph_min=5.5, ph_max=8.0, fertility_min=0.12,
        max_height_m=40, max_biomass_kgm2=30, max_canopy_density=0.75,
        growth_rate=0.05, mortality_rate=0.008,
        leaf_type="deciduous",  # tropical deciduous (dry season)
        dormancy_precip=0.25,
        litterfall_rate=0.09, litter_cn_ratio=45,
        wood_density_gcm3=0.70, timber_quality=0.85,
        root_depth_m=4.0, drought_tolerance=0.5,
    ))
    _add(PlantDef(
        name="coconut_palm", common_name="Coconut Palm",
        family="evergreen", growth_form="tree",
        temp_min=0.55, temp_opt_min=0.65, temp_opt_max=0.95, temp_max=1.0,
        precip_min=0.30, precip_opt_min=0.50, precip_opt_max=0.95, precip_max=1.0,
        ph_min=5.0, ph_max=8.5, fertility_min=0.08,
        max_height_m=25, max_biomass_kgm2=8, max_canopy_density=0.35,
        growth_rate=0.10, mortality_rate=0.015,
        leaf_type="evergreen",
        litterfall_rate=0.15, litter_cn_ratio=35,
        wood_density_gcm3=0.40, timber_quality=0.2,
        fruit_yield_kgm2=2.0,  # coconuts!
        root_depth_m=1.0, drought_tolerance=0.4,
    ))
    _add(PlantDef(
        name="bamboo_giant", common_name="Giant Bamboo",
        family="grass", growth_form="grass",
        temp_min=0.40, temp_opt_min=0.55, temp_opt_max=0.90, temp_max=1.0,
        precip_min=0.30, precip_opt_min=0.45, precip_opt_max=0.95, precip_max=1.0,
        ph_min=5.0, ph_max=7.5, fertility_min=0.15,
        max_height_m=20, max_biomass_kgm2=25, max_canopy_density=0.6,
        growth_rate=0.30, mortality_rate=0.04,  # very fast, high turnover
        leaf_type="evergreen",
        litterfall_rate=0.25, litter_cn_ratio=60,
        wood_density_gcm3=0.35, timber_quality=0.5,
        root_depth_m=1.5, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="grass_temperate", common_name="Temperate Grass",
        family="grass", growth_form="grass",
        temp_min=0.02, temp_opt_min=0.08, temp_opt_max=0.55, temp_max=0.75,
        precip_min=0.05, precip_opt_min=0.10, precip_opt_max=0.60, precip_max=0.85,
        ph_min=4.5, ph_max=8.5, fertility_min=0.03,
        max_height_m=1.5, max_biomass_kgm2=1.5, max_canopy_density=0.15,
        growth_rate=0.40, mortality_rate=0.35,  # fast turnover
        leaf_type="deciduous", dormancy_temp=0.04,
        litterfall_rate=0.40, litter_cn_ratio=25,
        wood_density_gcm3=0.1, timber_quality=0.0,
        root_depth_m=0.5, drought_tolerance=0.6,
    ))
    _add(PlantDef(
        name="grass_tropical", common_name="Tropical Savanna Grass",
        family="grass", growth_form="grass",
        temp_min=0.35, temp_opt_min=0.50, temp_opt_max=0.95, temp_max=1.0,
        precip_min=0.05, precip_opt_min=0.10, precip_opt_max=0.60, precip_max=0.90,
        ph_min=5.0, ph_max=8.5, fertility_min=0.03,
        max_height_m=2.5, max_biomass_kgm2=2.0, max_canopy_density=0.2,
        growth_rate=0.50, mortality_rate=0.40,
        leaf_type="deciduous", dormancy_precip=0.05,
        litterfall_rate=0.50, litter_cn_ratio=20,
        wood_density_gcm3=0.1, timber_quality=0.0,
        root_depth_m=0.8, drought_tolerance=0.7,
    ))
    _add(PlantDef(
        name="shrub_wet", common_name="Moist Shrub",
        family="shrub", growth_form="shrub",
        temp_min=0.05, temp_opt_min=0.15, temp_opt_max=0.60, temp_max=0.80,
        precip_min=0.10, precip_opt_min=0.20, precip_opt_max=0.75, precip_max=0.95,
        ph_min=4.5, ph_max=8.0, fertility_min=0.06,
        max_height_m=3.0, max_biomass_kgm2=3.0, max_canopy_density=0.4,
        growth_rate=0.15, mortality_rate=0.05,
        leaf_type="deciduous", dormancy_temp=0.04,
        litterfall_rate=0.15, litter_cn_ratio=35,
        wood_density_gcm3=0.3, timber_quality=0.1,
        root_depth_m=1.0, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="shrub_dry", common_name="Arid Shrub",
        family="shrub", growth_form="shrub",
        temp_min=0.08, temp_opt_min=0.20, temp_opt_max=0.70, temp_max=0.95,
        precip_min=0.02, precip_opt_min=0.05, precip_opt_max=0.30, precip_max=0.50,
        ph_min=6.0, ph_max=9.0, fertility_min=0.03,
        max_height_m=2.0, max_biomass_kgm2=1.0, max_canopy_density=0.15,
        growth_rate=0.08, mortality_rate=0.04,
        leaf_type="evergreen",
        dormancy_precip=0.02,
        litterfall_rate=0.05, litter_cn_ratio=40,
        wood_density_gcm3=0.35, timber_quality=0.05,
        root_depth_m=2.5, drought_tolerance=0.9,
    ))
    _add(PlantDef(
        name="succulent_cactus", common_name="Desert Cactus",
        family="succulent", growth_form="shrub",
        temp_min=0.20, temp_opt_min=0.40, temp_opt_max=0.85, temp_max=1.0,
        precip_min=0.0, precip_opt_min=0.02, precip_opt_max=0.20, precip_max=0.40,
        ph_min=6.5, ph_max=9.0, fertility_min=0.02,
        max_height_m=5.0, max_biomass_kgm2=2.0, max_canopy_density=0.05,
        growth_rate=0.03, mortality_rate=0.01,
        leaf_type="evergreen",
        dormancy_precip=0.0,
        litterfall_rate=0.02, litter_cn_ratio=60,
        wood_density_gcm3=0.2, timber_quality=0.0,
        root_depth_m=0.3, drought_tolerance=0.95,
        fruit_yield_kgm2=0.3,
    ))
    _add(PlantDef(
        name="moss_tundra", common_name="Tundra Moss/Lichen",
        family="moss", growth_form="moss",
        temp_min=0.0, temp_opt_min=0.02, temp_opt_max=0.20, temp_max=0.35,
        precip_min=0.02, precip_opt_min=0.05, precip_opt_max=0.50, precip_max=0.80,
        ph_min=4.0, ph_max=8.0, fertility_min=0.0,
        max_height_m=0.1, max_biomass_kgm2=0.5, max_canopy_density=0.3,
        growth_rate=0.05, mortality_rate=0.04,
        leaf_type="evergreen",
        dormancy_temp=0.0,
        litterfall_rate=0.10, litter_cn_ratio=50,
        wood_density_gcm3=0.1, timber_quality=0.0,
        root_depth_m=0.05, drought_tolerance=0.4,
    ))
    _add(PlantDef(
        name="heath_alpine", common_name="Alpine Heath",
        family="shrub", growth_form="shrub",
        temp_min=0.0, temp_opt_min=0.02, temp_opt_max=0.20, temp_max=0.35,
        precip_min=0.05, precip_opt_min=0.10, precip_opt_max=0.45, precip_max=0.70,
        ph_min=4.0, ph_max=7.0, fertility_min=0.04,
        max_height_m=0.8, max_biomass_kgm2=1.0, max_canopy_density=0.25,
        growth_rate=0.06, mortality_rate=0.03,
        leaf_type="evergreen",
        dormancy_temp=0.01,
        litterfall_rate=0.08, litter_cn_ratio=45,
        wood_density_gcm3=0.2, timber_quality=0.0,
        root_depth_m=0.3, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="mangrove_red", common_name="Red Mangrove",
        family="evergreen", growth_form="tree",
        temp_min=0.45, temp_opt_min=0.55, temp_opt_max=0.90, temp_max=1.0,
        precip_min=0.25, precip_opt_min=0.40, precip_opt_max=0.95, precip_max=1.0,
        ph_min=6.0, ph_max=8.5, fertility_min=0.05,
        max_height_m=25, max_biomass_kgm2=15, max_canopy_density=0.55,
        growth_rate=0.08, mortality_rate=0.015,
        leaf_type="evergreen",
        litterfall_rate=0.14, litter_cn_ratio=40,
        wood_density_gcm3=0.65, timber_quality=0.4,
        root_depth_m=0.5, drought_tolerance=0.2,  # salt-tolerant, not drought
    ))
    _add(PlantDef(
        name="rainforest_emergents", common_name="Rainforest Emergent",
        family="evergreen", growth_form="tree",
        temp_min=0.55, temp_opt_min=0.65, temp_opt_max=0.95, temp_max=1.0,
        precip_min=0.45, precip_opt_min=0.60, precip_opt_max=1.0, precip_max=1.0,
        ph_min=4.5, ph_max=7.5, fertility_min=0.12,
        max_height_m=60, max_biomass_kgm2=50, max_canopy_density=0.7,
        growth_rate=0.03, mortality_rate=0.005,
        leaf_type="evergreen",
        litterfall_rate=0.06, litter_cn_ratio=55,
        wood_density_gcm3=0.80, timber_quality=0.7,
        root_depth_m=2.0, drought_tolerance=0.1,
    ))
    _add(PlantDef(
        name="rainforest_understory", common_name="Rainforest Understory",
        family="evergreen", growth_form="tree",
        temp_min=0.55, temp_opt_min=0.65, temp_opt_max=0.95, temp_max=1.0,
        precip_min=0.45, precip_opt_min=0.60, precip_opt_max=1.0, precip_max=1.0,
        ph_min=4.5, ph_max=7.5, fertility_min=0.08,
        max_height_m=15, max_biomass_kgm2=8, max_canopy_density=0.3,
        growth_rate=0.06, mortality_rate=0.01,
        leaf_type="evergreen", shade_tolerance=0.9,
        litterfall_rate=0.10, litter_cn_ratio=45,
        wood_density_gcm3=0.55, timber_quality=0.3,
        root_depth_m=1.0, drought_tolerance=0.1,
    ))
    _add(PlantDef(
        name="pioneer_ruderal", common_name="Pioneer Weeds",
        family="herb", growth_form="herb",
        temp_min=0.05, temp_opt_min=0.15, temp_opt_max=0.70, temp_max=0.90,
        precip_min=0.05, precip_opt_min=0.10, precip_opt_max=0.70, precip_max=0.95,
        ph_min=4.0, ph_max=8.5, fertility_min=0.02,
        max_height_m=0.5, max_biomass_kgm2=0.3, max_canopy_density=0.05,
        growth_rate=0.80, mortality_rate=0.70,
        leaf_type="deciduous", dormancy_temp=0.04,
        litterfall_rate=0.80, litter_cn_ratio=15,
        wood_density_gcm3=0.05, timber_quality=0.0,
        root_depth_m=0.2, drought_tolerance=0.3,
    ))
    _add(PlantDef(
        name="riparian_willow", common_name="Willow",
        family="deciduous", growth_form="tree",
        temp_min=0.04, temp_opt_min=0.10, temp_opt_max=0.50, temp_max=0.70,
        precip_min=0.15, precip_opt_min=0.25, precip_opt_max=0.85, precip_max=1.0,
        ph_min=5.0, ph_max=8.0, fertility_min=0.08,
        max_height_m=20, max_biomass_kgm2=12, max_canopy_density=0.6,
        growth_rate=0.15, mortality_rate=0.025,
        leaf_type="deciduous", dormancy_temp=0.04,
        litterfall_rate=0.18, litter_cn_ratio=30,
        wood_density_gcm3=0.40, timber_quality=0.3,
        root_depth_m=1.0, drought_tolerance=0.15,
    ))


def _add(plant: PlantDef):
    PFT_REGISTRY[plant.name] = plant


# ======================================================================
# Convenience
# ======================================================================


def get_pft(name: str) -> Optional[PlantDef]:
    return PFT_REGISTRY.get(name)


def register_pft(name: str, plant: PlantDef) -> None:
    """Register a custom plant type (e.g. for fantasy plants)."""
    PFT_REGISTRY[name] = plant


def compute_suitability(temp: float, precip: float,
                        fertility: float = 0.5, ph: float = 7.0) -> Dict[str, float]:
    """Return suitability (0–1) for every PFT."""
    return {
        name: pft.suitability(temp, precip, fertility, ph)
        for name, pft in PFT_REGISTRY.items()
    }


# Initialise on import
_init_pfts()
