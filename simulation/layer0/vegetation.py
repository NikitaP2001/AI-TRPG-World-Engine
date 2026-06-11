"""Layer 0 — Continuous Vegetation Model.

Replaces old discrete biome classification with a PFT-based continuous model.
For each cell, computes:
  - Per-PFT suitability, equilibrium biomass, and canopy density
  - Total canopy density (0–1 continuous)
  - Total biomass (kg/m²)
  - Litterfall rate (from biomass × PFT litterfall rates)
  - Human-readable vegetation_cover string (for backward compat)

The model uses 20+ Plant Functional Types defined in plant_registry.py,
each with a bell-shaped climate response curve, growth parameters,
and material properties (timber quality, fruit yield, etc.).

Design:
  plant_registry.PlantDef.suitability(temp, precip, fertility, ph) → 0–1
  plant_registry.PlantDef.equilibrium_biomass(...) → kg/m²
  vegetation.compute_vegetation_cell() → dict with canopy, biomass, litter, PFT mix
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .cell_model import CellData
from .plant_registry import PFT_REGISTRY, PlantDef


# ======================================================================
# Continuous vegetation computation
# ======================================================================


def compute_vegetation_cell(
    temp: float,
    precip: float,
    soil_fertility: float,
    soil_ph: float = 7.0,
    water_table_depth: float = 0.5,
    is_ocean: bool = False,
    day_of_year: float = 172.0,
) -> dict:
    """Compute continuous vegetation state for one cell.

    Returns dict:
        canopy_density:  float  0–1 total canopy cover
        biomass_kgm2:    float  total above-ground biomass
        litterfall_rate: float  annual litterfall (fraction)
        pft_composition: dict   PFT name → biomass_kgm2
        dominant_pft:    str    name of highest-biomass PFT
        vegetation_cover: str   human-readable class (backward compat)
    """
    if is_ocean:
        return {
            "canopy_density": 0.0,
            "biomass_kgm2": 0.0,
            "litterfall_rate": 0.0,
            "pft_composition": {},
            "dominant_pft": "",
            "vegetation_cover": "barren",
        }

    if soil_fertility < 0.02:
        return {
            "canopy_density": 0.0,
            "biomass_kgm2": 0.0,
            "litterfall_rate": 0.0,
            "pft_composition": {},
            "dominant_pft": "",
            "vegetation_cover": "barren",
        }

    # Compute suitability and equilibrium biomass for every PFT
    # Use a competition model: PFTs share space based on suitability
    pft_data: List[Tuple[str, float, float, float]] = []  # name, suit, biomass, canopy
    total_canopy = 0.0
    total_biomass = 0.0
    total_litter = 0.0

    for name, pft in PFT_REGISTRY.items():
        suit = pft.suitability(temp, precip, soil_fertility, soil_ph)
        if suit < 0.05:
            continue

        # Equilibrium biomass for this PFT in pure stand
        eq_biomass = pft.equilibrium_biomass(temp, precip, soil_fertility, soil_ph)
        if eq_biomass <= 0:
            continue

        # Seasonal growth factor
        gf = pft.growth_factor(temp, precip, day_of_year)

        # Competition: reduce biomass by total canopy (light competition)
        # Shade-tolerant PFTs less affected
        competition_factor = math.exp(-total_canopy * (1.0 - pft.shade_tolerance * 0.7))
        biomass = eq_biomass * gf * competition_factor
        if biomass < 0.01:
            continue

        canopy = pft.canopy_from_biomass(biomass)
        litter = pft.litterfall_rate * biomass * gf

        pft_data.append((name, biomass, canopy, litter))
        total_biomass += biomass
        total_canopy = 1.0 - math.prod(1.0 - c for _, _, c, _ in pft_data)  # probalistic
        total_litter += litter

    # Cap canopy at 1.0
    total_canopy = min(1.0, total_canopy)

    # Dominant PFT
    pft_composition = {n: b for n, b, _, _ in pft_data}
    dominant = max(pft_data, key=lambda x: x[1])[0] if pft_data else ""

    # Human-readable vegetation_cover from dominant PFT + total biomass
    veg_class = _classify_dominant(dominant, total_biomass, total_canopy,
                                   temp, precip, soil_fertility)

    # PFT-weighted interception coefficient (P1.7)
    total_bio = sum(pft_composition.values()) or 1e-6
    interception_coeff = 0.15  # default fallback
    if total_bio > 0:
        weighted = 0.0
        from .plant_registry import PFT_REGISTRY, pft_interception_coefficient
        for name, biomass in pft_composition.items():
            pft = PFT_REGISTRY.get(name)
            if pft:
                weighted += pft_interception_coefficient(pft) * (biomass / total_bio)
        interception_coeff = min(0.40, max(0.05, weighted))

    return {
        "canopy_density": total_canopy,
        "biomass_kgm2": total_biomass,
        "litterfall_rate": total_litter / max(1e-6, total_biomass) if total_biomass > 0 else 0.0,
        "pft_composition": pft_composition,
        "dominant_pft": dominant,
        "vegetation_cover": veg_class,
        "interception_coefficient": interception_coeff,  # P1.7
    }


# ======================================================================
# Human-readable classification (backward compat)
# ======================================================================

# Maps PFT family → dominant vegetation class
_FAMILY_TO_VEG = {
    "conifer": "taiga",
    "deciduous": "forest",
    "evergreen": "forest",
    "grass": "grassland",
    "shrub": "shrubland",
    "succulent": "desert",
    "moss": "tundra",
    "herb": "grassland",
}


def _classify_dominant(dominant_pft: str, total_biomass: float,
                       canopy: float, temp: float, precip: float,
                       fertility: float) -> str:
    """Derive a human-readable vegetation_cover string from continuous state."""
    if not dominant_pft or total_biomass < 0.05:
        if fertility < 0.06:
            return "barren"
        return "desert"

    pft = PFT_REGISTRY.get(dominant_pft)
    if pft is None:
        return "grassland"

    family = pft.family
    leaf = pft.leaf_type

    # Special cases
    if dominant_pft == "mangrove_red":
        return "mangrove"
    if dominant_pft == "moss_tundra" or dominant_pft == "heath_alpine":
        return "tundra"
    if dominant_pft == "succulent_cactus":
        return "desert"
    if dominant_pft == "bamboo_giant":
        return "forest"

    # Tree-based classes
    if pft.growth_form == "tree":
        if total_biomass > 25 and precip > 0.55 and temp > 0.55:
            return "rainforest"
        if temp < 0.18:
            return "taiga"
        if leaf == "deciduous" and temp > 0.50 and precip < 0.50:
            return "savanna"
        if canopy > 0.5:
            return "forest"
        return "woodland"

    # Grass-based
    if family == "grass":
        if total_biomass > 1.5:
            return "savanna" if temp > 0.45 else "grassland"
        return "grassland"

    # Shrub-based
    if family == "shrub":
        if total_biomass < 0.5 or precip < 0.10:
            return "desert"
        return "shrubland"

    # Herb / pioneer
    if family == "herb":
        return "grassland"

    return "grassland"


# ======================================================================
# Top-level assignment for generator
# ======================================================================


def assign_vegetation(
    cells: List[CellData],
    ocean_set: set,
    iterations: int = 3,
) -> None:
    """Assign continuous vegetation to all land cells.

    Replaces old discrete string-based system.
    Stores on each cell:
      cell.vegetation_cover  — human-readable string (backward compat)
      cell.canopy_density    — 0–1 continuous
      cell.biomass_kgm2     — total above-ground biomass

    The detailed PFT composition is available via cell.pft_composition
    if needed (future use: forestry, agriculture).
    """
    for iteration in range(iterations):
        for cell in cells:
            is_ocean = cell.h3_id in ocean_set

            result = compute_vegetation_cell(
                temp=cell.temperature,
                precip=cell.precipitation,
                soil_fertility=cell.soil_fertility,
                soil_ph=getattr(cell, 'soil_ph', 7.0),
                water_table_depth=cell.water_table_depth,
                is_ocean=is_ocean,
            )

            # Store backward-compat string
            cell.vegetation_cover = result["vegetation_cover"]

            # Store continuous fields
            cell.canopy_density = result["canopy_density"]
            cell.biomass_kgm2 = result["biomass_kgm2"]
            cell.interception_coefficient = result["interception_coefficient"]  # P1.7

            # Litterfall → organic matter → soil fertility feedback
            if not is_ocean:
                litter = result["litterfall_rate"]
                cell.organic_matter = min(1.0, cell.organic_matter + litter)

                # Soil fertility = mineral base + organic contribution
                mineral_base = 0.08  # base mineral fertility
                cell.soil_fertility = min(1.0, mineral_base + cell.organic_matter * 0.6)

                # Vegetation stabilizes soil depth
                cell.soil_depth = max(cell.soil_depth, 0.02)


# ======================================================================
# Vegetation → soil feedback (litterfall)
# ======================================================================


def vegetation_soil_feedback(cell: CellData) -> float:
    """Compute organic matter addition from current vegetation (continuous)."""
    # Use canopy_density and biomass directly instead of string lookup
    canopy = getattr(cell, 'canopy_density', 0.0)
    biomass = getattr(cell, 'biomass_kgm2', 0.0)
    if canopy <= 0 or biomass <= 0:
        return 0.0
    # Litterfall scales with biomass, ~5-15% per year
    return min(0.15, biomass * 0.008)


# ======================================================================
# Backward-compatible wrapper for terrain mask extraction
# ======================================================================


def vegetation_potential(
    soil_fertility: float,
    temperature: float,
    precipitation: float,
    is_ocean: bool = False,
) -> str:
    """Legacy wrapper — derives a discrete vegetation class for terrain masking.

    Uses the continuous PFT model internally, returns a human-readable string.
    """
    result = compute_vegetation_cell(
        temp=temperature,
        precip=precipitation,
        soil_fertility=soil_fertility,
        is_ocean=is_ocean,
    )
    return result["vegetation_cover"]


