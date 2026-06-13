"""Eustasy — global sea-level computation.

Sea level changes due to:
  1. Tectonic: ocean basin volume change (mid-ocean ridge spreading)
  2. Glacio-eustatic: water locked in ice sheets
  3. Thermosteric: thermal expansion of seawater

Output: sea_level_offset (metres above present level).
Applied by flooding/draining coastal cells.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set

import h3 as _h3


# ── Sea-level component amplitudes ──────────────────────────────────
_TECTONIC_SL = 200.0    # max amplitude from ridge volume [m]
_GLACIAL_SL = 120.0     # max from ice sheets (full glacial) [m]
_THERMAL_SL = 1.0       # per °C global mean [m/°C]


def compute_sea_level_offset(
    mean_ocean_crustal_age: float,
    ice_volume_fraction: float = 0.0,
    global_temp_anomaly: float = 0.0,
    ridge_spread_rate: float = 0.5,
) -> float:
    """Compute global sea-level offset from tectonic + cryo + thermal.

    Args:
        mean_ocean_crustal_age: Mean age of oceanic crust [Myr].
        ice_volume_fraction: Fraction of maximum ice volume (0..1).
        global_temp_anomaly: Global mean temp anomaly [°C].
        ridge_spread_rate: Sea-floor spreading rate [deg/Myr].

    Returns:
        Sea-level offset in metres (positive = higher sea level).
    """
    # Tectonic: younger crust → more buoyant ridges → higher sea level
    # Reference: 0 at 100 Myr mean age
    tectonic = _TECTONIC_SL * (100.0 - mean_ocean_crustal_age) / 100.0
    tectonic = max(-_TECTONIC_SL, min(_TECTONIC_SL, tectonic))

    # Glacial: ice on land → lower sea level
    glacial = -_GLACIAL_SL * ice_volume_fraction

    # Thermal: warmer → expansion → higher sea level
    thermal = _THERMAL_SL * global_temp_anomaly

    return tectonic + glacial + thermal


def apply_sea_level(
    cells,
    sea_level_offset: float,
    original_is_ocean: Dict[str, bool],
) -> int:
    """Flood or drain cells based on sea-level offset.

    Args:
        cells: List of CellData objects (mutated in place).
        sea_level_offset: Sea-level change in world units (metres).
        original_is_ocean: Dict of h3_id -> original is_ocean value.

    Returns:
        Number of cells that changed status.
    """
    changes = 0
    for c in cells:
        # Geological type 0 = oceanic, >0 = land
        orig_ocean = original_is_ocean.get(c.h3_id, c.geological_type == 0)
        is_now_ocean = (c.elevation_mean <= sea_level_offset)

        if is_now_ocean and c.geological_type != 0:
            # Flood
            c.geological_type = 0
            changes += 1
        elif not is_now_ocean and c.geological_type == 0 and not orig_ocean:
            # Emerge (was land, flooded, now exposed)
            # Set to continental shelf
            c.geological_type = 1
            changes += 1

    return changes


def compute_mean_ocean_age(
    cells,
) -> float:
    """Compute mean crustal age of all oceanic cells.

    Args:
        cells: List of CellData objects.

    Returns:
        Mean age in Myr.
    """
    ages = [c.crustal_age_myr for c in cells if c.geological_type == 0]
    if not ages:
        return 100.0
    return sum(ages) / len(ages)
