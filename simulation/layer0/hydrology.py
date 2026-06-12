"""Layer 0 — Continuous Hydrology Model.

Runoff = f(soil texture, slope, canopy density, precipitation intensity).
D8 flow accumulation uses effective_precip = precip × runoff_ratio.

Physics: USDA soil texture → saturated hydraulic conductivity (K_sat).
         Canopy density → interception + reduced surface runoff.
         Slope → overland flow velocity (Manning's n).
         Snowpack → meltwater delay.

Time-aware: day_of_year for snow accumulation/melt, precipitation intensity.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

import h3
import numpy as np

from .cell_model import CellData
from .climate import norm_to_c


# ======================================================================
# USDA soil texture → hydraulic properties
# ======================================================================

# ── Continuous K_sat from soil texture (P2.1, Rosetta-подобная PTF) ──
# Устаревшие USDA-словари сохранены для обратной совместимости
_K_SAT: Dict[str, float] = {
    "clay": 0.01, "silty_clay": 0.05, "silty_clay_loam": 0.1,
    "silt_loam": 0.3, "loam": 0.5, "sandy_loam": 1.0,
    "loamy_sand": 3.0, "sand": 10.0,
    "default": 0.5,
}


def _k_sat_continuous(clay: float, sand: float) -> float:
    """Непрерывная оценка K_sat [m/day] из фракций (P2.1).

    Rosetta-подобная PTF: ln(K_sat) = f(sand, clay).
    Калибрована на средние по USDA классам:
      sand→10, loamy_sand→3, sandy_loam→1, loam→0.5, clay→0.01
    """
    silt = 1.0 - clay - sand
    ln_k = -0.5 + 3.5 * sand - 2.0 * clay + 0.5 * silt
    return math.exp(max(-4.6, min(2.3, ln_k)))  # clamp ~0.01..10 m/day


def _texture_class(clay: float, sand: float) -> str:
    """USDA soil texture triangle classification from clay/sand fractions.
    Сохранён для обратной совместимости (groundwater.py)."""
    silt = 1.0 - clay - sand
    if clay > 0.40:
        return "clay"
    if clay > 0.27:
        if silt > 0.40:
            return "silty_clay"
        return "silty_clay_loam" if silt > 0.20 else "clay"
    if silt > 0.80:
        return "silt_loam"
    if sand > 0.85:
        return "sand"
    if sand > 0.70:
        return "loamy_sand"
    if sand > 0.50:
        return "sandy_loam"
    if clay > 0.07:
        return "loam" if silt > 0.40 else "silt_loam"
    return "silt_loam"


# Canopy interception fraction by density (P1.7)
def _canopy_interception(canopy: float, interception_coeff: float = 0.15) -> float:
    """Fraction of precip intercepted by canopy (never reaches ground).

    Uses PFT-specific interception coefficient (P1.7):
      conifers (LAI≈6) → ~21% interception at full canopy
      deciduous (LAI≈4.5) → ~13%
      grass (LAI≈2.5) → ~5%
    """
    return interception_coeff * canopy


def _infiltration_capacity(clay: float, sand: float, canopy: float) -> float:
    """Fraction of net precipitation that can infiltrate (0-1).

    Использует непрерывную K_sat из текстуры (P2.1) с прямой
    интерполяцией в m/s (P2.5): infil = clamp((K_sat - 1e-7)/(1e-4 - 1e-7)).
    Canopy замедляет поступление (перехваченная вода капает медленно).
    """
    k_sat_ms = _k_sat_continuous(clay, sand) / 86400.0  # m/day → m/s
    # Прямая интерполяция: 1e-7 (clay) → 0, 1e-4 (sand) → 1 (P2.5)
    infil = max(0.0, min(1.0, (k_sat_ms - 1e-7) / (1e-4 - 1e-7)))
    # Canopy: корневые каналы немного увеличивают инфильтрацию
    infil *= (0.8 + 0.2 * canopy)
    return max(0.05, min(1.0, infil))


def _overland_flow_velocity(slope: float, canopy: float, runoff: float) -> float:
    """Overland flow velocity [m/s] from Manning's equation approximation.

    v = (1/n) * R^(2/3) * S^(1/2)
    Simplified: n = Manning's n from surface roughness, R ≈ depth ∝ runoff
    """
    # Manning's n: barren=0.05, grass=0.15, forest=0.30
    n_manning = 0.05 + 0.25 * canopy
    # Depth proxy: more runoff = deeper flow
    depth = max(0.001, runoff * 0.1)
    # Slope in m/m from our normalized slope
    slope_mm = max(0.001, slope * 0.5)
    v = (1.0 / n_manning) * (depth ** (2.0/3.0)) * (slope_mm ** 0.5)
    return max(0.001, v)


# ======================================================================
# Snowpack model
# ======================================================================


def compute_snowmelt(
    temp_c: float,
    snowpack_mm: float,
    day_of_year: float,
    elevation: float,
) -> Tuple[float, float]:
    """Compute snow accumulation and melt.

    Args:
        temp_c: Current temperature [degC].
        snowpack_mm: Current snow water equivalent [mm].
        day_of_year: 0-365 for solar radiation effect.

    Returns:
        (melt_mm, new_snowpack_mm)
    """
    # Snow accumulation when temp < 0°C
    if temp_c < 0.0:
        return (0.0, snowpack_mm)  # no melt, accumulation from precip handled elsewhere

    # Degree-day melt model
    # Melt factor: higher in spring (more solar radiation)
    spring_factor = max(0.5, math.sin(math.radians(day_of_year * 360 / 365 - 90)))
    dd_factor = 2.0 + 4.0 * spring_factor  # mm/day/°C, 2-6 mm/day/°C

    melt_mm = dd_factor * temp_c
    melt_mm = min(melt_mm, snowpack_mm)  # can't melt more than available
    new_snowpack = snowpack_mm - melt_mm

    return (melt_mm, new_snowpack)


# ======================================================================
# Runoff ratio model — physics-based
# ======================================================================


# Canopy density lookup (fallback when L1 biome not available)
_VEG_CANOPY_FALLBACK = {
    "rainforest": 0.95, "forest": 0.80, "taiga": 0.60,
    "savanna": 0.30, "grassland": 0.10, "shrubland": 0.25,
    "tundra": 0.05, "desert": 0.02, "barren": 0.00,
}


def compute_runoff_ratio(
    clay_content: float,
    sand_content: float,
    soil_depth: float,
    slope: float,
    precipitation: float,
    canopy: float = 0.0,                  # continuous 0-1 canopy density
    vegetation_cover: str = "",            # fallback for canopy lookup
    interception_coeff: float = 0.15,      # PFT-weighted interception (P1.7)
    temp_c: float = 15.0,                 # for snow/rain distinction
    snowpack_mm: float = 0.0,             # current snowpack [mm]
    day_of_year: float = 172.0,           # for snowmelt calculation
) -> float:
    """Compute fraction of precipitation that becomes surface runoff (0-1).

    Physics:
      1. Rain vs snow: if temp_c < 0, precip accumulates as snow → no immediate runoff
      2. Canopy interception: reduces net_precip reaching ground (PFT-specific, P1.7)
      3. Infiltration: from USDA texture class
      4. Saturation excess: when soil is saturated, all excess runs off
      5. Snowmelt: degree-day model adds meltwater to runoff
    """
    # Resolve canopy if not directly provided
    if canopy <= 0.0 and vegetation_cover:
        canopy = _VEG_CANOPY_FALLBACK.get(vegetation_cover, 0.0)

    # 1. Rain vs snow
    if temp_c < -1.0:
        return 0.0
    elif temp_c < 2.0:
        rain_fraction = (temp_c + 1.0) / 3.0
    else:
        rain_fraction = 1.0

    net_precip = precipitation * rain_fraction

    # 2. Canopy interception (P1.7: PFT-specific coefficient)
    interception = _canopy_interception(canopy, interception_coeff)
    net_precip *= (1.0 - interception)

    if net_precip <= 0.0:
        return 0.0

    # 3. Infiltration from USDA texture
    infil = _infiltration_capacity(clay_content, sand_content, canopy)

    # 4. Soil depth storage: deeper = more capacity before saturation
    storage_capacity = min(2.0, soil_depth * 3.0)
    saturation_factor = min(1.0, net_precip / max(0.01, storage_capacity))
    # High saturation → more runoff
    infil_effective = infil * (1.0 - saturation_factor * 0.5)

    # 5. Slope: steeper = faster overland flow = less time to infiltrate
    slope_factor = 1.0 / (1.0 + slope * 5.0)

    # 6. Snowmelt contribution
    melt_mm, _ = compute_snowmelt(temp_c, snowpack_mm, day_of_year, 0)
    melt_factor = min(1.0, melt_mm / max(0.01, net_precip * 100.0))

    # Composite: runoff = 1 - (infiltration * time_for_infiltration)
    runoff = 1.0 - infil_effective * slope_factor
    runoff = max(0.0, min(1.0, runoff))
    runoff += melt_factor * 0.1  # melt adds to runoff
    runoff = min(1.0, runoff)

    return runoff


def compute_runoff_for_cells(
    cells: List[CellData],
    precipitation: Dict[str, float],
    ocean_set: Set[str],
    day_of_year: float = 172.0,
) -> None:
    """Compute runoff_ratio and effective_precip for all cells (in-place)."""
    for cell in cells:
        h = cell.h3_id
        if h in ocean_set:
            cell.runoff_ratio = 0.0
            cell.effective_precip = 0.0
            continue

        temp_norm = getattr(cell, 'temperature', 0.5)
        temp_c = norm_to_c(temp_norm)
        # Use continuous canopy_density (P1.6/P1.4)
        canopy = getattr(cell, 'canopy_density', 0.0) or _VEG_CANOPY_FALLBACK.get(cell.vegetation_cover or "barren", 0.0)

        cell_interception = getattr(cell, 'interception_coefficient', 0.15)
        runoff = compute_runoff_ratio(
            clay_content=cell.clay_content,
            sand_content=cell.sand_content,
            soil_depth=cell.soil_depth,
            slope=cell.slope[0] if cell.slope else 0.0,
            precipitation=precipitation.get(h, 0.5),
            canopy=canopy,
            interception_coeff=cell_interception,
            temp_c=temp_c,
            snowpack_mm=0.0,
            day_of_year=day_of_year,
        )
        cell.runoff_ratio = runoff
        cell.effective_precip = precipitation.get(h, 0.5) * runoff


# ======================================================================
# Weighted D8 flow accumulation
# ======================================================================


def compute_flow_accum_weighted(
    h3_ids: List[str],
    flow_dir: Dict[str, int],
    weights: Dict[str, float],
) -> Dict[str, float]:
    """Compute flow accumulation using per-cell weights instead of uniform 1.0.

    Each cell contributes its weight (e.g., effective_precip) to the total
    flow accumulation of downstream cells.

    Args:
        h3_ids: All cell IDs in the grid.
        flow_dir: Dict[h3_id] → neighbour index (0-5) or -1 (sink) or -2 (ocean).
        weights: Dict[h3_id] → weight contribution (e.g., effective_precip).

    Returns:
        Dict[h3_id] → accumulated flow (sum of upstream weights).
    """
    # Build flow_to mapping
    flow_to: Dict[str, str] = {}
    for h in h3_ids:
        d = flow_dir.get(h, -1)
        if d >= 0:
            nh_list = h3.grid_ring(h, 1) or []
            if d < len(nh_list):
                nh = nh_list[d]
                flow_to[h] = nh if nh in h3_ids else ""
            else:
                flow_to[h] = ""
        else:
            flow_to[h] = ""  # sink or ocean terminus

    # Build upstream map
    upstream: Dict[str, list] = {h: [] for h in h3_ids}
    for h in h3_ids:
        target = flow_to.get(h)
        if target and target in upstream:
            upstream[target].append(h)

    # Stack-based accumulation
    acc: Dict[str, float] = {h: weights.get(h, 0.0) for h in h3_ids}
    visited: set = set()

    for h in h3_ids:
        if h in visited:
            continue
        stack = [h]
        while stack:
            cur = stack[-1]
            if cur in visited:
                stack.pop()
                continue
            target = flow_to.get(cur)
            if not target or target in visited:
                visited.add(cur)
                stack.pop()
                for up in upstream.get(cur, []):
                    if up not in visited:
                        acc[cur] += acc.get(up, 0.0)
            elif target in stack:
                # Cycle detected — break it
                visited.add(cur)
                stack.pop()
            else:
                if target not in visited:
                    stack.append(target)
                else:
                    visited.add(cur)
                    stack.pop()
                    for up in upstream.get(cur, []):
                        if up not in visited:
                            acc[cur] += acc.get(up, 0.0)

    return acc
