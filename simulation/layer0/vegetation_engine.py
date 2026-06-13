"""VegetationEngine — continuous time-evolving PFT/ecosystem dynamics.

Recomputes PFT suitability from current climate fields periodically,
allowing biomes to migrate, succeed, and respond to climate change.

Usage:
    engine = VegetationEngine(ws, h3_ids)
    engine.advance(dt_myr=0.01)
    canopy = ws.field("canopy_density")(35.0, 120.0)
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

from ..world_state import WorldState


class VegetationEngine:
    """Time-evolving PFT/ecosystem dynamics — continuous fields."""

    def __init__(self, ws: WorldState, h3_ids: List[str]):
        self._ws = ws
        self._h3_ids = list(h3_ids)
        self._rng = random.Random(42)

    def advance(self, dt_myr: float) -> None:
        """Recompute PFT suitability and update vegetation fields.

        Uses current temperature/precipitation fields from WS.
        Writes canopy_density, biomass_kgm2, vegetation_cover.
        """
        # Skip if climate fields not available
        if not self._ws.has_field("temperature") or not self._ws.has_field("precipitation"):
            return

        temp_f = self._ws.field("temperature")
        precip_f = self._ws.field("precipitation")

        import h3 as _h3
        from .plant_registry import PFT_REGISTRY
        from .vegetation import compute_vegetation_cell, _classify_dominant

        canopy_data: Dict[str, float] = {}
        biomass_data: Dict[str, float] = {}
        cover_data: Dict[str, str] = {}

        # Sample at H3 centroids using continuous climate fields
        soil_f = None
        if self._ws.has_field("soil_fertility"):
            soil_f = self._ws.field("soil_fertility")

        for hid in self._h3_ids:
            latlng = _h3.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]

            temp = temp_f(lat, lon)
            precip = precip_f(lat, lon)
            fertility = soil_f(lat, lon) if soil_f else 0.3

            if temp is None or precip is None:
                continue

            # Use generation-time vegetation physiology to compute
            # continuous canopy/biomass from current climate
            result = compute_vegetation_cell(
                temp=temp, precip=precip,
                soil_fertility=fertility,
                is_ocean=False,
            )

            canopy = max(0.0, min(1.0, result.get("canopy_density", 0.0)))
            biomass = max(0.0, result.get("biomass_kgm2", 0.0))

            # Classify to human-readable cover
            pft_composition = result.get("pft_composition", {})
            dominant = max(pft_composition, key=lambda k: pft_composition.get(k, 0)) if pft_composition else "barren"
            cover = _classify_dominant(dominant, biomass)

            # Smooth transition (avoid abrupt jumps)
            current_canopy = self._ws.get_discrete("canopy_density").get(hid, canopy)
            current_biomass = self._ws.get_discrete("biomass_kgm2").get(hid, biomass)
            blend = min(1.0, dt_myr * 10.0)
            canopy_data[hid] = current_canopy * (1.0 - blend) + canopy * blend
            biomass_data[hid] = current_biomass * (1.0 - blend) + biomass * blend
            cover_data[hid] = cover

        # Register results as continuous fields + discrete data
        if canopy_data:
            self._ws.set_field("canopy_density", canopy_data, mutable=True)
            self._ws.get_discrete("canopy_density").update(canopy_data)
            self._ws.set_field("biomass_kgm2", biomass_data, mutable=True)
            self._ws.get_discrete("biomass_kgm2").update(biomass_data)
        if cover_data:
            self._ws.get_discrete("vegetation_cover").update(cover_data)
