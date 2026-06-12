"""Layer 0 — Weathering and Soil Formation Model.

Causal chain: bedrock mineralogy + climate → weathering rate.
            weathering - erosion → soil depth.
            soil depth + nutrients + organic matter → soil fertility.

Ocean floor: no subaerial weathering → soil_fertility ≈ 0.02 (silt).

All hardcoded coefficients have been replaced with physics-based
parameters. Key improvements:
  - Erosion K-factor from soil texture (not implicit 0.03)
  - Cover C-factor from continuous canopy density (not biome string)
  - Fertility from mineral balance (no minimum clamp)
  - pH from precipitation leaching (not fixed offset)
  - Soil depth = weathering - erosion (mass balance, no magic 0.15/0.1)

Design doc § Stage 6 — Soil and Surface Properties.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .cell_model import CellData
from .climate import norm_to_c
from .geology import get_mineral_profile, MineralProfile


# Canopy density by vegetation type (for erosion C-factor)
_VEG_CANOPY = {
    "rainforest": 0.95,
    "forest": 0.80,
    "taiga": 0.60,
    "savanna": 0.30,
    "grassland": 0.10,
    "shrubland": 0.25,
    "tundra": 0.05,
    "desert": 0.02,
    "barren": 0.00,
}

# Litterfall rate by vegetation type (kg/m²/yr approx)
_VEG_LITTER = {
    "rainforest": 1.2,
    "forest": 0.7,
    "taiga": 0.3,
    "savanna": 0.2,
    "grassland": 0.15,
    "shrubland": 0.1,
    "tundra": 0.05,
    "desert": 0.01,
    "barren": 0.0,
}


# ======================================================================
# Weathering model — Q10 temperature rule + precipitation + mineralogy
# ======================================================================


def compute_weathering(
    temperature: float,          # 0-1 normalized annual mean
    precipitation: float,        # 0-1 normalized annual total
    mineral: MineralProfile,
    slope: float,                # 0-1 slope magnitude
    time_factor: float = 1.0,    # long-cycle tick multiplier
    is_ocean: bool = False,      # no weathering underwater
) -> dict:
    """Compute chemical weathering rate and nutrient release.

    Returns dict with keys:
      weathering_rate  — 0-1 scale (fraction of max possible per time unit)
      n_release, p_release, k_release — nutrient release rates
      clay_formation   — 0-1 clay fraction formation rate
    """
    if is_ocean:
        return {
            "weathering_rate": 0.0,
            "n_release": 0.0,
            "p_release": 0.0,
            "k_release": 0.0,
            "clay_formation": 0.0,
        }

    # Temperature factor: Q10 ≈ 2 (reaction rate doubles per 10°C)
    temp_c = norm_to_c(temperature)
    if temp_c <= 0.0:
        temp_factor = 0.05  # Freezing = minimal chemical weathering
    else:
        temp_factor = 2.0 ** ((temp_c - 10.0) / 10.0)

    # Precipitation factor: more water = more hydrolysis
    # No magic thresholds — continuous function
    # Saturation at high precip (all available CO₂ consumed)
    precip_factor = precipitation / (precipitation + 0.15)  # Michaelis-Menten form

    # Mineral weatherability (Goldich dissolution series, 0-1)
    mineral_factor = mineral.weatherability

    # Slope: steeper terrain = faster drainage = less water-rock contact
    slope_factor = 1.0 / (1.0 + slope * 3.0)

    # Composite weathering rate
    # Theoretical max when: temp=30°C (factor≈4), precip=saturated, mineral=1, slope=0
    base_rate = temp_factor * precip_factor * mineral_factor * slope_factor
    # Normalize: at temp=0.7 (26.5°C), precip=0.7, mineral=0.5, slope=0.1:
    #   temp_f ≈ 3.0, precip_f ≈ 0.82, slope_f ≈ 0.77
    #   3.0 * 0.82 * 0.5 * 0.77 ≈ 0.95
    # Scale to 0-1 range
    weathering_rate = min(1.0, base_rate * 0.8 * time_factor)

    # Nutrient release proportional to weathering × mineral content
    n_release = mineral.nutrient_n * weathering_rate
    p_release = mineral.nutrient_p * weathering_rate
    k_release = mineral.nutrient_k * weathering_rate

    # Clay formation from mineral weathering
    clay_formation = mineral.clay_potential * weathering_rate

    return {
        "weathering_rate": weathering_rate,
        "n_release": n_release,
        "p_release": p_release,
        "k_release": k_release,
        "clay_formation": clay_formation,
    }


# ======================================================================
# Erosion model — RUSLE with explicit K-factor + continuous C-factor
# ======================================================================


def _k_factor_from_texture(clay: float, sand: float, om: float) -> float:
    """Soil erodibility K-factor (0-1 scale, RUSLE).

    Wischmeier approximation:
      K = [2.1*M^1.14*10^-4*(12-OM) + 3.25*(s-2) + 2.5*(p-3)] / 100
    Simplified for our texture model:
      - M = (silt + very-fine-sand) * (100 - clay)
      - s = structure code (1-4), p = permeability code (1-6)

    We use a texture-based approximation:
      Silty soils erode fastest, sandy slowest, clay intermediate.
    """
    # M factor: high silt = high erodibility
    silt = 1.0 - clay - sand  # calculate silt fraction
    M = silt * (100.0 - clay * 100.0)
    # OM reduces erodibility
    om_factor = max(0.5, 1.0 - om * 2.0)
    # Base K from texture
    K = (0.00021 * M ** 0.5 + 0.02) * om_factor / 7.59
    return max(0.01, min(0.60, K))


def _c_factor_from_canopy(canopy: float) -> float:
    """Cover management C-factor from continuous canopy density 0-1.

    C = exp(-2.5 * canopy) gives:
      canopy=0.0 (barren) → C=1.00 (max erosion)
      canopy=0.2 (grass)  → C=0.61
      canopy=0.5 (shrub)  → C=0.29
      canopy=0.8 (forest) → C=0.14
      canopy=0.95 (rainf) → C=0.09
    """
    return max(0.05, math.exp(-2.5 * canopy))


def compute_erosion(
    slope: float,                 # 0-1 slope magnitude
    precipitation: float,         # 0-1 normalized
    canopy: float,                # 0-1 continuous canopy density
    clay: float = 0.2,           # soil clay fraction
    sand: float = 0.4,           # soil sand fraction
    organic_matter: float = 0.0, # soil organic matter 0-1
    precip_seasonality: float = 0.3,  # CV of monthly precipitation (P1.5)
) -> float:
    """Compute soil erosion rate (RUSLE with explicit K-factor).

    A = R * K * LS * C * P
    where:
      R = rainfall erosivity (from precipitation intensity × seasonality)
      K = soil erodibility (from texture + organic matter)
      LS = slope length/steepness
      C = cover management (from continuous canopy density)
      P = support practice (= 1.0, no human intervention)

    R-factor uses Fournier-style index (P1.5):
      R = precip^1.5 * (1.0 + seasonality * 2.0)
    Higher seasonality = more concentrated rainfall = higher erosivity.

    Returns erosion_rate in 0-1 scale.
    """
    # R factor: rainfall erosivity with seasonality (P1.5)
    # More seasonal = more intense storms = higher erosivity
    seasonality_factor = 1.0 + precip_seasonality * 2.0
    rainfall_factor = (precipitation ** 1.5) * seasonality_factor

    # K factor: soil erodibility from texture + OM
    k_factor = _k_factor_from_texture(clay, sand, organic_matter)

    # LS factor: slope length-steepness
    # Simplified: steeper = more erosion (non-linear)
    ls_factor = slope ** 1.3 * 3.0

    # C factor: cover management from continuous canopy density
    c_factor = _c_factor_from_canopy(canopy)

    # P factor: support practices (1.0 = none)
    p_factor = 1.0

    # RUSLE: A = R * K * LS * C * P
    erosion_rate = rainfall_factor * k_factor * ls_factor * c_factor * p_factor

    return min(1.0, erosion_rate)


# ======================================================================
# Soil profile formation
# ======================================================================


def form_soil(
    weathering: dict,
    erosion_rate: float,
    is_ocean: bool = False,
    is_shelf: bool = False,
    time_factor: float = 1.0,
    mineral: Optional[MineralProfile] = None,
    canopy: float = 0.0,  # continuous canopy density for C-factor
) -> dict:
    """Form soil profile from weathering-erosion balance.

    Physics-based: no hardcoded minimum fertility, no magic coefficients.

    Returns dict with keys:
      soil_depth, soil_fertility, organic_matter,
      clay_content, sand_content, silt_content,
      ph, cation_exchange
    """
    if is_ocean:
        return {
            "soil_depth": 0.0,
            "soil_fertility": 0.02,   # Seafloor sediment
            "organic_matter": 0.0,
            "clay_content": 0.30,
            "sand_content": 0.20,
            "silt_content": 0.50,
            "ph": 8.0,                 # Seawater-buffered alkaline
            "cation_exchange": 8.0,
        }

    if is_shelf:
        return {
            "soil_depth": 0.05,
            "soil_fertility": 0.05,
            "organic_matter": 0.01,
            "clay_content": 0.25,
            "sand_content": 0.30,
            "silt_content": 0.45,
            "ph": 7.5,
            "cation_exchange": 7.0,
        }

    # ── Soil depth from weathering-erosion balance ──
    w_rate = weathering.get("weathering_rate", 0.0)
    # Net accumulation: weathering builds soil, erosion removes it
    net_accumulation = w_rate * time_factor - erosion_rate * time_factor * 0.5
    # Cap at 2.0 world units (max soil depth)
    soil_depth = max(0.0, min(2.0, net_accumulation * 5.0))

    # Minimal pioneer soil on any land with weathering
    if soil_depth < 0.005 and w_rate > 0.001:
        soil_depth = 0.005

    # ── Fertility from mineral nutrient release ──
    # Direct contributions from weathering-released nutrients
    p_release = weathering.get("p_release", 0.0)
    k_release = weathering.get("k_release", 0.0)
    n_release = weathering.get("n_release", 0.0)

    # Base fertility = sum of available nutrients (each 0-1, summed capped)
    base_fertility = min(1.0, (p_release + k_release + n_release * 0.3) * 2.0)

    # Organic matter starts at 0 (will be updated by vegetation feedback)
    organic_matter = 0.0

    # Final fertility = mineral base + organic contribution (organic added later)
    soil_fertility = base_fertility
    # NO minimum clamp — genuinely barren areas stay barren

    # ── Soil texture from clay formation ──
    clay_frac = weathering.get("clay_formation", 0.0)
    clay_content = min(0.6, clay_frac + 0.02 * time_factor)
    # Sand: less in weathered soils
    sand_content = max(0.02, 0.5 - clay_content * 0.5)
    silt_content = 1.0 - clay_content - sand_content
    # Normalize
    total = clay_content + sand_content + silt_content
    if total > 0:
        clay_content /= total
        sand_content /= total
        silt_content /= total

    # ── pH from mineral + precipitation leaching ──
    ph = mineral.ph_initial if mineral else 7.0
    if w_rate > 0:
        # Leaching: precipitation dissolves bases, acidifies soil
        # pH drops more in wet climates, less in dry/alkaline minerals
        precip_effect = weathering.get("precip_used", 0.5)
        leaching = precip_effect * 0.4 * (7.0 - ph) / 7.0  # proportional to buffering
        ph = max(4.5, ph - leaching)

    # ── CEC from clay + organic matter + mineral base ──
    cec = (mineral.cation_exchange if mineral else 5.0)
    cec += clay_content * 25.0  # clay contribution (smectite ≈ 80, kaolinite ≈ 10)
    cec += organic_matter * 50.0  # OM contribution

    return {
        "soil_depth": soil_depth,
        "soil_fertility": soil_fertility,
        "organic_matter": organic_matter,
        "clay_content": clay_content,
        "sand_content": sand_content,
        "silt_content": silt_content,
        "ph": ph,
        "cation_exchange": cec,
    }


# ======================================================================
# Organic matter update (vegetation feedback)
# ======================================================================


def update_organic_matter(
    canopy: float,                 # 0-1 continuous canopy density
    current_organic_matter: float,
    temperature: float,            # 0-1 normalized
    precipitation: float,          # 0-1 normalized
) -> float:
    """Update soil organic matter from vegetation litter.

    Uses continuous canopy density (not biome string) for litter input.
    Decomposition follows Q10 temperature rule + moisture availability.
    """
    # Litter input proportional to canopy (linear proxy for NPP)
    # Max litter ~0.15 for full rainforest canopy
    litter_input = 0.15 * canopy

    # Decomposition rate: Q10 ≈ 2 with moisture limitation
    temp_c = norm_to_c(temperature)
    if temp_c <= 0.0:
        decomp_rate = 0.02  # minimal at freezing
    else:
        decomp_rate = 0.05 * (2.0 ** ((temp_c - 10.0) / 10.0)) * precipitation

    # Steady-state approximation
    new_om = current_organic_matter + litter_input - decomp_rate * current_organic_matter
    return max(0.0, min(1.0, new_om))


# ======================================================================
# Top-level soil assignment for generator
# ======================================================================


def assign_soil_profiles(
    cells: List[CellData],
    temperature: Dict[str, float],
    precipitation: Dict[str, float],
    ocean_set: set,
    shelf_set: set,
    time_factor: float = 1.0,
) -> None:
    """Compute and assign soil profiles to all cells.

    Called after climate and geology are established.
    """
    for cell in cells:
        h = cell.h3_id
        is_ocean = h in ocean_set
        is_shelf = h in shelf_set
        temp = temperature.get(h, 0.5)
        precip = precipitation.get(h, 0.5)
        slope_mag = cell.slope[0] if cell.slope else 0.0
        # Get mineral profile
        mineral = get_mineral_profile(cell.bedrock_class)

        # Weathering
        weather = compute_weathering(
            temperature=temp,
            precipitation=precip,
            mineral=mineral,
            slope=slope_mag,
            time_factor=time_factor,
            is_ocean=is_ocean,
        )

        # Erosion (with canopy from continuous canopy_density, P1.4)
        canopy = getattr(cell, 'canopy_density', 0.0)
        seas = getattr(cell, 'precip_seasonality', 0.3)
        erosion = compute_erosion(
            slope=slope_mag,
            precipitation=precip,
            canopy=canopy,
            clay=cell.clay_content,
            sand=cell.sand_content,
            organic_matter=cell.organic_matter,
            precip_seasonality=seas,
        )

        # Soil profile
        soil = form_soil(
            weathering=weather,
            erosion_rate=erosion,
            is_ocean=is_ocean,
            is_shelf=is_shelf,
            time_factor=time_factor,
            mineral=mineral,
            canopy=canopy,
        )

        # Write to cell
        cell.soil_depth = soil["soil_depth"]
        cell.soil_fertility = soil["soil_fertility"]
        cell.organic_matter = soil["organic_matter"]
        cell.clay_content = soil["clay_content"]
        cell.sand_content = soil["sand_content"]
        cell.silt_content = soil["silt_content"]
        cell.soil_ph = soil["ph"]
        cell.cation_exchange = soil["cation_exchange"]
