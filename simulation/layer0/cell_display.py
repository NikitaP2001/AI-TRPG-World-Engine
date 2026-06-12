"""Layer 0 — Cell Display Grid.

Computes cell display properties from the Feature Store.
Cells are purely for display — the simulation truth is in features.

Usage:
    grid = CellDisplayGrid(feature_store, resolution=2)
    color = grid.cell_color(h3_id, view_mode="elevation")
    info = grid.cell_info(h3_id)  # {elevation, soil, vegetation, ...}
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import h3
import numpy as np
from shapely.geometry import Point as SPoint, Polygon

from .feature_store import FeatureStore


# ======================================================================
# Colour palettes (shared with planet_scene.py)
# ======================================================================

_CONTOUR_FLAT = (0.275, 0.529, 0.235)
_CONTOUR_LOW = (0.471, 0.608, 0.333)
_CONTOUR_MID = (0.725, 0.647, 0.431)
_CONTOUR_HIGH = (0.647, 0.510, 0.314)
_CONTOUR_PEAK = (0.529, 0.373, 0.216)
_CONTOUR_SNOW = (0.804, 0.784, 0.784)

_OCEAN_DEEP = (0.071, 0.125, 0.373)
_OCEAN_MID = (0.118, 0.216, 0.471)
_OCEAN_SHALLOW = (0.176, 0.314, 0.588)

_SOIL_BARREN = (0.6, 0.5, 0.4)
_SOIL_POOR = (0.7, 0.65, 0.5)
_SOIL_MODERATE = (0.6, 0.7, 0.4)
_SOIL_GOOD = (0.4, 0.65, 0.3)
_SOIL_RICH = (0.2, 0.5, 0.15)

_VEG_MAP = {
    "barren": (0.5, 0.45, 0.35),
    "desert": (0.8, 0.7, 0.45),
    "tundra": (0.55, 0.6, 0.65),
    "grassland": (0.6, 0.75, 0.4),
    "shrubland": (0.65, 0.6, 0.35),
    "savanna": (0.7, 0.65, 0.3),
    "forest": (0.2, 0.55, 0.2),
    "rainforest": (0.1, 0.4, 0.1),
    "taiga": (0.25, 0.45, 0.3),
}

_GEO_COLORS = {
    0: (0.1, 0.15, 0.4),
    1: (0.6, 0.55, 0.45),
    2: (0.5, 0.6, 0.35),
    3: (0.55, 0.35, 0.2),
    4: (0.4, 0.3, 0.5),
    5: (0.7, 0.5, 0.3),
    6: (0.5, 0.3, 0.3),
}


# ======================================================================
# Cell Display Grid
# ======================================================================


class CellDisplayGrid:
    """Cell grid for GUI display — reads from Feature Store.

    No simulation logic. Pure display projection of vector features
    onto the H3 grid for rendering.
    """

    def __init__(
        self,
        feature_store: FeatureStore,
        resolution: int = 2,
    ):
        self.feature_store = feature_store
        self.resolution = resolution
        self.h3_ids: List[str] = []

        # Build full H3 grid
        all_ids: List[str] = []
        for r0 in list(h3.get_res0_cells()):
            all_ids.extend(h3.cell_to_children(r0, resolution))
        self.h3_ids = all_ids

    # ── Hex-based feature queries (polygon intersection, not centroid) ─

    def features_at(self, h3_id: str) -> List[Any]:
        """Return all features intersecting this cell's hexagon area."""
        return self.feature_store.features_in_hex(h3_id)

    def hex_info(self, h3_id: str) -> dict:
        """Comprehensive info for a hexagon — features + inferred terrain.

        Queries features by hex boundary intersection (not centroid point).
        Returns everything the info panel needs.
        """
        features = self.features_at(h3_id)
        latlng = h3.cell_to_latlng(h3_id)

        info = {
            "h3_id": h3_id,
            "lat": latlng[0],
            "lon": latlng[1],
            "features": features,
            "features_by_type": {},
            "elevation_mean": 0.0,
            "geological_type": 0,
            "temperature": 0.5,
            "precipitation": 0.5,
            "precip_seasonality": 0.3,
            "soil_fertility": 0.02,
            "vegetation_cover": "barren",
            "is_ocean": True,
            "has_river": False,
            "has_lake": False,
            "has_road": False,
            "hazard_level": 0.0,
            "wind_u": 0.0,
            "wind_v": 0.0,
        }

        # Classify features by type
        for f in features:
            ft = f.type
            info["features_by_type"].setdefault(ft, []).append(f)
            props = f.properties or {}

            if ft == "elevation_contour":
                info["elevation_mean"] = max(info["elevation_mean"], props.get("elevation", 0.0))
            elif ft == "temperature_band":
                info["temperature"] = props.get("temperature", info["temperature"])
                info["precipitation"] = props.get("precipitation", info["precipitation"])
            elif ft == "soil_region":
                info["soil_fertility"] = props.get("soil_fertility", info["soil_fertility"])
            elif ft == "terrain_cover":
                info["vegetation_cover"] = props.get("cover_type", info["vegetation_cover"])
            elif ft == "geology_region":
                info["geological_type"] = props.get("geological_type", info["geological_type"])
            elif ft == "climate_zone":
                info["precip_seasonality"] = props.get("seasonality", info["precip_seasonality"])
            elif ft == "river":
                info["has_river"] = True
            elif ft == "lake":
                info["has_lake"] = True
            elif ft in ("ocean", "sea"):
                info["is_ocean"] = True

        info["is_ocean"] = info["geological_type"] == 0
        return info

    # ── Color queries (for rendering) ───────────────────────────────

    def cell_color(self, h3_id: str, view_mode: int = 0) -> Tuple[float, float, float]:
        """Get cell display color for the given view mode."""
        info = self.cell_info(h3_id)

        if view_mode == 0:  # Elevation
            return self._elevation_color(info)
        elif view_mode == 1:  # Soil
            return self._soil_color(info)
        elif view_mode == 2:  # Vegetation
            return self._veg_color(info)
        elif view_mode == 3:  # Geology
            return self._geo_color(info)

        return self._elevation_color(info)

    def _elevation_color(self, info: dict) -> Tuple[float, float, float]:
        el = info["elevation_mean"]
        if info.get("is_ocean", True):
            d = int(min(1.0, max(0.0, 1.0 - info.get("temperature", 0.5))) * 200)
            if d < 80:
                return _OCEAN_SHALLOW
            elif d < 160:
                return _OCEAN_MID
            return _OCEAN_DEEP
        if el > 0.80:
            return _CONTOUR_SNOW
        elif el > 0.60:
            return _CONTOUR_PEAK
        elif el > 0.45:
            return _CONTOUR_HIGH
        elif el > 0.30:
            return _CONTOUR_MID
        elif el > 0.15:
            return _CONTOUR_LOW
        return _CONTOUR_FLAT

    def _soil_color(self, info: dict) -> Tuple[float, float, float]:
        f = info.get("soil_fertility", 0.02)
        if f < 0.03:
            return _SOIL_BARREN
        elif f < 0.15:
            return _SOIL_POOR
        elif f < 0.30:
            return _SOIL_MODERATE
        elif f < 0.50:
            return _SOIL_GOOD
        return _SOIL_RICH

    def _veg_color(self, info: dict) -> Tuple[float, float, float]:
        if info.get("is_ocean", True):
            return _OCEAN_DEEP
        veg = info.get("vegetation_cover", "barren")
        return _VEG_MAP.get(veg, (0.6, 0.75, 0.4))

    def _geo_color(self, info: dict) -> Tuple[float, float, float]:
        return _GEO_COLORS.get(info.get("geological_type", 0), (0.5, 0.5, 0.5))

    # ── Bulk query (for batch rendering) ────────────────────────────

    def all_cell_info(self) -> Dict[str, dict]:
        """Return display info for every cell."""
        return {h: self.cell_info(h) for h in self.h3_ids}
