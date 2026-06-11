"""Biomes — continuous vegetation classification from PFT model.

Replaces old threshold-based classification with values derived from the
continuous PFT vegetation model. BIOME_REGISTRY stores reference values
for each class, but actual canopy/biomass/litter are computed per-cell
from the plant_registry model.

The biome classification is a thin human-readable label on top of the
continuous PFT composition — it does NOT drive the simulation.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from .base import Feature
from ..fields import FieldRegistry

# ======================================================================
# Biome registry (human-readable labels only)
# ======================================================================

BIOME_REGISTRY: Dict[str, dict] = {
    "ice_desert":       {"name": "Ice Desert", "canopy": 0.0, "biomass": 0.0, "litterfall": 0.0},
    "arctic_tundra":    {"name": "Arctic Tundra", "canopy": 0.1, "biomass": 0.3, "litterfall": 0.005},
    "alpine_tundra":    {"name": "Alpine Tundra", "canopy": 0.05, "biomass": 0.2, "litterfall": 0.003},
    "taiga":            {"name": "Taiga", "canopy": 0.6, "biomass": 8.0, "litterfall": 0.04},
    "boreal_forest":    {"name": "Boreal Forest", "canopy": 0.7, "biomass": 12.0, "litterfall": 0.05},
    "temperate_coniferous": {"name": "Temperate Coniferous", "canopy": 0.8, "biomass": 20.0, "litterfall": 0.07},
    "temperate_deciduous":  {"name": "Temperate Deciduous", "canopy": 0.85, "biomass": 18.0, "litterfall": 0.10},
    "temperate_rainforest": {"name": "Temperate Rainforest", "canopy": 0.9, "biomass": 30.0, "litterfall": 0.12},
    "mediterranean":    {"name": "Mediterranean", "canopy": 0.4, "biomass": 5.0, "litterfall": 0.04},
    "subtropical_forest": {"name": "Subtropical Forest", "canopy": 0.85, "biomass": 25.0, "litterfall": 0.11},
    "savanna":          {"name": "Savanna", "canopy": 0.3, "biomass": 4.0, "litterfall": 0.03},
    "dry_forest":       {"name": "Dry Tropical Forest", "canopy": 0.5, "biomass": 10.0, "litterfall": 0.06},
    "rainforest":       {"name": "Rainforest", "canopy": 0.95, "biomass": 45.0, "litterfall": 0.15},
    "monsoon_forest":   {"name": "Monsoon Forest", "canopy": 0.8, "biomass": 22.0, "litterfall": 0.09},
    "steppe":           {"name": "Steppe", "canopy": 0.15, "biomass": 1.5, "litterfall": 0.015},
    "forest_steppe":    {"name": "Forest-Steppe", "canopy": 0.3, "biomass": 3.0, "litterfall": 0.025},
    "shrubland":        {"name": "Shrubland", "canopy": 0.25, "biomass": 2.0, "litterfall": 0.02},
    "grassland":        {"name": "Grassland", "canopy": 0.1, "biomass": 1.0, "litterfall": 0.02},
    "mangrove":         {"name": "Mangrove", "canopy": 0.5, "biomass": 8.0, "litterfall": 0.08},
    "desert":           {"name": "Desert", "canopy": 0.02, "biomass": 0.1, "litterfall": 0.002},
    "semi_desert":      {"name": "Semi-Desert", "canopy": 0.05, "biomass": 0.3, "litterfall": 0.003},
    "wetland_veg":      {"name": "Wetland", "canopy": 0.3, "biomass": 5.0, "litterfall": 0.06},
}


def classify_biome(
    temp: float,
    precip: float,
    soil_moisture: float,
    soil_fertility: float,
    elevation: float,
    water_table: float,
    is_ocean: bool = False,
    canopy_density: float = 0.0,
    biomass_kgm2: float = 0.0,
) -> str:
    """Classify biome from continuous field values and PFT model outputs.

    Uses continuous values where available, falls back to centroid logic.
    """
    if is_ocean:
        return "ice_desert" if temp < 0.08 else "desert"

    if elevation > 0.6 and temp < 0.15:
        return "alpine_tundra"

    if water_table < 0.3 and soil_moisture > 0.7:
        return "wetland_veg"

    if biomass_kgm2 > 0:
        return _classify_from_continuous(temp, precip, biomass_kgm2, canopy_density,
                                         soil_fertility, elevation)

    return _classify_centroid(temp, precip, soil_fertility, elevation)


def _classify_from_continuous(
    temp: float, precip: float, biomass: float, canopy: float,
    fertility: float, elevation: float,
) -> str:
    if temp < 0.08:
        return "ice_desert" if biomass < 0.1 else "arctic_tundra"
    if temp < 0.20:
        if precip < 0.12:
            return "arctic_tundra"
        if biomass < 2.0:
            return "tundra"
        return "taiga" if canopy > 0.3 else "forest_steppe"
    if precip < 0.08 or biomass < 0.3:
        return "desert" if biomass < 0.2 else "semi_desert"
    if temp < 0.40:
        if precip > 0.60 and canopy > 0.6:
            return "temperate_rainforest"
        if biomass < 3.0:
            return "grassland" if precip > 0.15 else "steppe"
        if canopy > 0.5:
            return "temperate_deciduous" if precip > 0.35 else "temperate_coniferous"
        return "forest_steppe"
    if temp < 0.55:
        if precip > 0.55:
            return "temperate_deciduous"
        if biomass > 5.0:
            return "mediterranean" if precip < 0.30 else "subtropical_forest"
        return "shrubland" if precip < 0.20 else "grassland"
    if precip > 0.65 and biomass > 20:
        return "rainforest"
    if precip > 0.45:
        return "monsoon_forest" if biomass > 15 else "subtropical_forest"
    if precip > 0.25:
        return "dry_forest" if canopy > 0.3 else "savanna"
    if precip > 0.10:
        return "savanna" if biomass > 1.0 else "shrubland"
    return "desert"


def _classify_centroid(temp: float, precip: float,
                        soil_fertility: float, elevation: float) -> str:
    if temp < 0.08:
        return "ice_desert"
    if temp < 0.18:
        if precip < 0.10:
            return "arctic_tundra"
        return "taiga" if soil_fertility > 0.10 else "tundra"
    if temp < 0.35:
        if precip < 0.10:
            return "steppe"
        if precip < 0.25:
            return "grassland"
        return "boreal_forest" if precip > 0.40 else "forest_steppe"
    if temp < 0.55:
        if precip < 0.08:
            return "desert"
        if precip < 0.15:
            return "shrubland"
        if precip < 0.30:
            return "grassland"
        return "temperate_deciduous" if precip > 0.40 else "mediterranean"
    if precip < 0.08:
        return "desert"
    if precip < 0.15:
        return "shrubland"
    if precip < 0.30:
        return "savanna"
    if precip < 0.55:
        return "dry_forest" if soil_fertility > 0.15 else "savanna"
    return "rainforest" if precip > 0.70 else "monsoon_forest"


# ======================================================================
# Biome Feature — thin wrapper, PFT model drives simulation
# ======================================================================


class BiomeRegion(Feature):
    """A contiguous region of a single biome type — for visualization only.

    Actual vegetation dynamics are driven by the continuous PFT model.
    """

    def __init__(self, polygon, biome_key: str, feature_id: str = "",
                 canopy_density: float = 0.0, biomass_kgm2: float = 0.0):
        if not feature_id:
            import uuid
            feature_id = f"biome_{uuid.uuid4().hex[:8]}"
        info = BIOME_REGISTRY.get(biome_key, {})
        super().__init__(
            feature_id=feature_id,
            name=info.get("name", biome_key),
            geometry=polygon,
            feature_type="biome",
            props={
                "biome_key": biome_key,
                "canopy_density": canopy_density or info.get("canopy", 0.5),
                "biomass_kgm2": biomass_kgm2 or info.get("biomass", 1.0),
                "litterfall_rate": info.get("litterfall", 0.02),
            },
        )

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Minimal effects — PFT model handles vegetation dynamics."""
        if self.geometry is None:
            return
        centroid = self.centroid()
        if centroid is None:
            return
        clat, clon = centroid
        canopy = self.props.get("canopy_density", 0.5)
        if canopy > 0.3:
            wt_f = fields.get_mutable("water_table_depth")
            wt_f.add_persistent(clat, clon, radius_deg=2.0,
                                strength=canopy * 0.02 * dt * 0.3)


# ======================================================================
# Sample biomes grid
# ======================================================================


def sample_biomes(fields: FieldRegistry, lat_step: float = 1.0,
                  lon_step: float = 1.0) -> Dict[str, list]:
    import numpy as np
    elev_f = fields.get("elevation_mean")
    temp_f = fields.get("temperature")
    precip_f = fields.get("precipitation")
    soil_f = fields.get("soil_fertility")
    sm_f = fields.get("soil_moisture")
    wt_f = fields.get("water_table_depth")
    lats = np.arange(-89.5, 90.0, lat_step)
    lons = np.arange(-179.5, 180.0, lon_step)
    result: Dict[str, list] = {}
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            el = elev_f.base_only(float(lat), float(lon))
            is_ocean = el < -0.01
            temp = temp_f.base_only(float(lat), float(lon))
            precip = precip_f.base_only(float(lat), float(lon))
            soil = soil_f.base_only(float(lat), float(lon))
            sm = sm_f(float(lat), float(lon))
            wt = wt_f(float(lat), float(lon))
            bk = classify_biome(temp=temp, precip=precip, soil_moisture=sm,
                                soil_fertility=soil, elevation=el,
                                water_table=wt, is_ocean=is_ocean)
            result.setdefault(bk, []).append((float(lon), float(lat)))
    return result
