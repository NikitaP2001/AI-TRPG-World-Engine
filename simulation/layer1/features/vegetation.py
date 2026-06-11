"""Vegetation — continuous PFT-based biomass and canopy simulation.

Moved from Layer 0 (static generation) to Layer 1 (continuous via TimeEngine).

Each tick:
  1. Read temperature, precipitation, soil_fertility from fields
  2. For each sampled grid point, compute PFT suitability → canopy_density → biomass
  3. Write canopy_density and biomass as mutable field effects
  4. Update soil organic matter via litterfall feedback

Uses the same PFT registry (24 PFTs, bell curves) as the original generator.
"""
from __future__ import annotations

import math
import uuid
from typing import List

import numpy as np

from ...layer0.plant_registry import PFT_REGISTRY, pft_interception_coefficient
from ...layer0.vegetation import compute_vegetation_cell, _classify_dominant
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
        """Compute vegetation from current climate + soil fields.

        Samples at ~2° resolution, computes PFT equilibrium for each
        point, writes canopy_density and biomass as persistent effects.
        """
        temp_f = fields.get("temperature")
        precip_f = fields.get("precipitation")
        soil_f = fields.get("soil_fertility")
        elev_f = fields.get("elevation_mean")

        canopy_f = fields.get_mutable("canopy_density")
        biomass_f = fields.get_mutable("biomass")
        soil_mut = fields.get_mutable("soil_fertility")

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

        # Compute vegetation per grid point
        for i in range(nl):
            for j in range(nlo):
                lat, lon = float(lats[i]), float(lons[j])

                if elev_g[i, j] < -0.01:
                    continue  # ocean

                temp = float(temp_g[i, j])
                precip = float(precip_g[i, j])
                fertility = float(soil_g[i, j])

                result = compute_vegetation_cell(
                    temp=temp,
                    precip=precip,
                    soil_fertility=fertility,
                    is_ocean=False,
                )

                canopy = result["canopy_density"]
                biomass = result["biomass_kgm2"]
                litter = result["litterfall_rate"]

                # Write persistent effects
                if canopy > 0.01:
                    canopy_f.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                            strength=canopy)
                    biomass_f.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                             strength=biomass)

                # Soil fertility feedback from litterfall (slow)
                if litter > 0.001:
                    om_delta = litter * 0.1 * min(1.0, dt)
                    soil_mut.add_persistent(lat, lon, radius_deg=_LAT_STEP * 1.5,
                                            strength=om_delta)

    def should_dissolve(self, fields: FieldRegistry) -> bool:
        return False  # vegetation is global, never dissolves
