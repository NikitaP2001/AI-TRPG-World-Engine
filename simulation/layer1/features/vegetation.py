"""Vegetation — continuous PFT-based biomass and canopy simulation.

Moved from Layer 0 (static generation) to Layer 1 (continuous via TimeEngine).

Each tick:
  1. Read temperature, precipitation, soil_fertility from fields
  2. For each sampled grid point, compute PFT equilibrium
  3. Apply dynamic growth: current += rate * (equilibrium - current) * dt
  4. Write per-PFT biomass and canopy_density as persistent field effects
  5. Update soil organic matter via litterfall feedback

Uses the same PFT registry (23 PFTs, bell curves) as the original generator.
"""
from __future__ import annotations

import math
import uuid
from typing import Dict, List

import numpy as np

from ...layer0.plant_registry import PFT_REGISTRY
from ...layer0.vegetation import compute_vegetation_cell
from .base import Feature
from ..fields import FieldRegistry


# Sampling resolution (degrees). Lighter than groundwater (5° → 2° for ecology).
_LAT_STEP = 2.0
_LON_STEP = 2.0


class Vegetation(Feature):
    """Continuous vegetation model as a Layer 1 feature.

    Reads climate + soil fields, computes PFT composition,
    writes canopy_density, biomass, and interception_coefficient
    as persistent field effects.
    """

    def __init__(self, feature_id: str = ""):
        if not feature_id:
            feature_id = f"vegetation_{uuid.uuid4().hex[:8]}"
        super().__init__(
            feature_id=feature_id,
            name="Vegetation",
            geometry=None,
            feature_type="vegetation",
            props={},
        )

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Compute vegetation from current climate + soil fields — dynamic PFT model.

        For each grid point:
          1. Compute PFT equilibrium (what COULD grow here)
          2. Read current PFT biomass from field (what IS here)
          3. Apply dynamic growth: current += rate * (equilibrium - current) * dt
          4. Write updated per-PFT biomass and canopy as persistent effects
        """
        temp_f = fields.get("temperature")
        precip_f = fields.get("precipitation")
        soil_f = fields.get("soil_fertility")
        elev_f = fields.get("elevation_mean")

        canopy_f = fields.get_mutable("canopy_density")
        soil_mut = fields.get_mutable("soil_fertility")

        # Pre-fetch all PFT MutableFields
        pft_mf: Dict[str, "MutableField"] = {}
        for pft_name in PFT_REGISTRY:
            try:
                pft_mf[pft_name] = fields.get_mutable(f"pft_biomass[{pft_name}]")
            except KeyError:
                pass

        lats = np.arange(-88.0, 90.0, _LAT_STEP)
        lons = np.arange(-178.0, 180.0, _LON_STEP)
        nl, nlo = len(lats), len(lons)

        # Pre-sample base fields
        temp_g = np.zeros((nl, nlo))
        precip_g = np.zeros((nl, nlo))
        soil_g = np.zeros((nl, nlo))
        elev_g = np.zeros((nl, nlo))

        for i in range(nl):
            for j in range(nlo):
                lat, lon = float(lats[i]), float(lons[j])
                temp_g[i, j] = temp_f.base_only(lat, lon)
                precip_g[i, j] = precip_f.base_only(lat, lon)
                soil_g[i, j] = soil_f.base_only(lat, lon)
                elev_g[i, j] = elev_f.base_only(lat, lon)

        # Compute water_depth from elevation: ocean depth where below sea level
        # (lake depth added via water_depth field if available)
        water_depth_g = np.maximum(0.0, -elev_g)

        # Compute vegetation per grid point
        for i in range(nl):
            for j in range(nlo):
                lat, lon = float(lats[i]), float(lons[j])

                water_depth = float(water_depth_g[i, j])

                temp = float(temp_g[i, j])
                precip = float(precip_g[i, j])
                fertility = float(soil_g[i, j])

                result = compute_vegetation_cell(
                    temp=temp,
                    precip=precip,
                    soil_fertility=fertility,
                    elevation=float(elev_g[i, j]),
                    water_depth=water_depth,
                )

                eq_canopy = result["canopy_density"]
                eq_pft_comp = result["pft_composition"]
                litter = result["litterfall_rate"]

                # --- Dynamic growth: approach equilibrium ---
                # current += growth_rate * (equilibrium - current) * dt
                # growth_rate per tick = PFT.growth_rate / 365 * dt
                total_canopy = 0.0
                for pft_name, eq_biomass in eq_pft_comp.items():
                    mf = pft_mf.get(pft_name)
                    if mf is None:
                        continue
                    # Read current biomass from field base
                    current = 0.0
                    if mf._base is not None:
                        try:
                            current = max(0.0, float(mf._base(lat, lon)))
                        except Exception:
                            current = 0.0
                    pft_def = PFT_REGISTRY.get(pft_name)
                    growth_per_tick = (pft_def.growth_rate / 365.0 * dt) if pft_def else 0.02
                    # Clamp growth so we don't exceed equilibrium in one tick
                    growth_frac = min(1.0, growth_per_tick)
                    new_biomass = current + growth_frac * (eq_biomass - current)
                    new_biomass = max(0.0, new_biomass)
                    if new_biomass > 0.001:
                        mf.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                          strength=new_biomass)
                        # Compute canopy from this PFT's biomass
                        canopy = pft_def.canopy_from_biomass(new_biomass) if pft_def else 0.0
                        total_canopy = 1.0 - (1.0 - total_canopy) * (1.0 - canopy)

                total_canopy = min(1.0, total_canopy)

                # Write canopy_density
                if total_canopy > 0.01:
                    canopy_f.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                            strength=total_canopy)

                # Soil fertility feedback from litterfall (slow)
                if litter > 0.001:
                    om_delta = litter * 0.1 * min(1.0, dt)
                    soil_mut.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                            strength=om_delta)

    def should_dissolve(self, fields: FieldRegistry) -> bool:
        return False  # vegetation is global, never dissolves
