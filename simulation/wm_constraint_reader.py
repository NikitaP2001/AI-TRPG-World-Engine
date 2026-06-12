"""WM Constraint Reader — loads authored constraints from game/wm_state/*.json.

Transforms WM tool outputs (set_world_orientation, alter_feature,
define_world_concept, define_faction) into structured objects that
the Layer 0 generator can consume at each pipeline stage.

Usage:
    from simulation.wm_constraint_reader import load_constraints

    wmc = load_constraints()
    params = wmc.to_generation_params()
    # Pass wm_constraints dict to generate_world(wm_constraints=...)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h3

# ======================================================================
# Constants
# ======================================================================

_WM_PATH = Path("game") / "wm_state"

# Map WM size_preset to approximate radius in degrees
_SIZE_PRESET_RADIUS: Dict[str, float] = {
    "tiny": 0.5,
    "small": 1.0,
    "medium": 2.0,
    "large": 5.0,
    "massive": 10.0,
}

# ======================================================================
# Feature type alias map — WM types → generator internal types
# ======================================================================
# WM uses alter_feature(feature_type=...) with open-ended strings.
# This map translates them to the fixed set the generator understands.
#
# Valid WM feature types (from tool_docs_data.py):
#   continent, elevation_feature, water_body, river, terrain_cover,
#   climate_zone, geological_zone, ambient_material_zone, void_zone,
#   mountain_pass, fault_line, settlement, ruin, underground_region,
#   physics_override
#
# Plus observed real-world usage: region, elevation

# Features that affect L0 elevation (spatial constraint injection):
_FT_CONTINENT = {"continent", "region", "landmass"}
_FT_MOUNTAIN = {"mountain_range", "elevation_feature", "elevation", "peak"}
_FT_RIVER = {"river"}  # also water_body with properties.type="river"
_FT_LAKE = {"lake"}     # also water_body with properties.type in ("lake", "sea", "ocean")
_FT_TERRAIN = {"terrain_cover", "terrain"}
_FT_CLIMATE = {"climate_zone", "climate"}

# All recognised types (for validation):
_RECOGNISED_FT = (
    _FT_CONTINENT | _FT_MOUNTAIN | _FT_RIVER | _FT_LAKE
    | _FT_TERRAIN | _FT_CLIMATE
    | {"settlement", "ruin", "underground_region", "mountain_pass",
       "fault_line", "geological_zone", "ambient_material_zone",
       "void_zone", "physics_override"}
)


# ======================================================================
# Helper: read WM JSON files
# ======================================================================


def _read_json(name: str, default: Any = None) -> Any:
    p = _WM_PATH / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _feat_radius(feat: dict) -> float:
    """Estimate feature radius from size_preset or explicit radius."""
    preset = (feat.get("size_preset") or "").strip().lower()
    if preset in _SIZE_PRESET_RADIUS:
        return _SIZE_PRESET_RADIUS[preset]
    radius = feat.get("size_radius", 0.0) or 0.0
    if radius > 0:
        return float(radius)
    return 2.0  # default medium


# ======================================================================
# WMConstraints — aggregated view of all WM state files
# ======================================================================


@dataclass
class WMConstraints:
    """Aggregated constraints from ALL WM state files.

    Populated by load_constraints(). Each field corresponds to one
    pipeline stage in generator.py.
    """

    # ── From world_orientation.json ──────────────────────────────────
    orientation: Dict[str, Any] = field(default_factory=dict)
    # Keys: planet_radius, axial_tilt, global_temperature_offset,
    #       global_precipitation_modifier, solar_intensity,
    #       atmospheric_density, ocean_temperature, tectonic_activity,
    #       ambient_rare_materials, climate_drift_rate, world_name,
    #       tech_level_default

    # ── From features.json (raw) ─────────────────────────────────────
    features: List[dict] = field(default_factory=list)

    # ── Spatial constraints for elevation injection ──────────────────
    continent_constraints: List[dict] = field(default_factory=list)
    mountain_constraints: List[dict] = field(default_factory=list)
    lake_constraints: List[dict] = field(default_factory=list)

    # ── Spatial constraints for hydrology injection ──────────────────
    river_constraints: List[dict] = field(default_factory=list)
    climate_zone_constraints: List[dict] = field(default_factory=list)
    terrain_cover_constraints: List[dict] = field(default_factory=list)

    # ── Validation ──────────────────────────────────────────────────
    unknown_feature_types: List[str] = field(default_factory=list)

    # ── From world_concepts.json ─────────────────────────────────────
    concepts: List[dict] = field(default_factory=list)
    fauna_species: List[dict] = field(default_factory=list)
    flora_pft: List[dict] = field(default_factory=list)
    ore_types: List[dict] = field(default_factory=list)
    settlement_types: List[dict] = field(default_factory=list)
    canon_constraints: List[dict] = field(default_factory=list)
    existence_types: List[dict] = field(default_factory=list)

    # ── From factions.json ───────────────────────────────────────────
    factions: List[dict] = field(default_factory=list)

    # ── From entities.json ──────────────────────────────────────────
    entities: List[dict] = field(default_factory=list)

    # ── From player_start.json ──────────────────────────────────────
    player_start: Dict[str, Any] = field(default_factory=dict)

    # ── Pre-computed elevation modifiers ─────────────────────────────
    # {h3_id: forced_elevation} — set after resolve_to_cells()
    continent_elevation: Dict[str, float] = field(default_factory=dict)
    mountain_elevation: Dict[str, float] = field(default_factory=dict)
    lake_elevation: Dict[str, float] = field(default_factory=dict)
    # River bed: lower elevation to create valley
    river_bed_elevation: Dict[str, float] = field(default_factory=dict)
    # Ridge: raise elevation to create watershed
    ridge_elevation: Dict[str, float] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Return True if no constraints were loaded."""
        return (
            not self.orientation
            and not self.features
            and not self.concepts
            and not self.factions
        )

    def to_generation_params(self) -> dict:
        """Convert world_orientation to kwargs for GenerationParams()."""
        o = self.orientation
        return {
            "planet_radius": o.get("planet_radius", 1.0),
            "axial_tilt": o.get("axial_tilt", 23.5),
            "tectonic_activity": o.get("tectonic_activity", 0.5),
            "global_temperature_offset": o.get("global_temperature_offset", 0.0),
            "global_precipitation_modifier": o.get("global_precipitation_modifier", 1.0),
            "solar_intensity": o.get("solar_intensity", 1.0),
            "atmospheric_density": o.get("atmospheric_density", 1.0),
            "num_plates": _tectonic_activity_to_plates(o.get("tectonic_activity", 0.5)),
            "seed": 42,  # overridden by finalize_world_generation world_seed
        }

    def to_generator_constraints(self) -> dict:
        """Build the dict passed as wm_constraints to generate_world()."""
        return {
            "continent_elevation": self.continent_elevation,
            "mountain_elevation": self.mountain_elevation,
            "lake_elevation": self.lake_elevation,
            "river_bed_elevation": self.river_bed_elevation,
            "ridge_elevation": self.ridge_elevation,
            "ore_types": self.ore_types,
            "fauna_species": self.fauna_species,
            "flora_pft": self.flora_pft,
            "features": self.features,
            "factions": self.factions,
            "entities": self.entities,
            "player_start": self.player_start,
            "settlement_types": self.settlement_types,
        }


def _tectonic_activity_to_plates(activity: float) -> int:
    """Map tectonic_activity (0-1) to plate count (4-14)."""
    return max(4, min(14, int(4 + activity * 10)))


# ======================================================================
# Spatial resolution: feature descriptions → H3 cell sets
# ======================================================================


def resolve_to_cells(
    feat: dict,
    all_h3_ids: List[str],
) -> set:
    """Convert a WM feature's location fields into a set of H3 cell IDs.

    Priority:
      1. outline_vertices → shapely polygon containment
      2. location_absolute → point buffer with feature radius
      3. location_region_hint → no resolution (returns empty set)

    Args:
        feat: A feature dict from features.json.
        all_h3_ids: Full list of H3 cell IDs for the world.

    Returns:
        Set of h3_id strings covered by this feature.
    """
    cells: set = set()

    # Method 1: outline_vertices → polygon containment
    outline = feat.get("outline_vertices") or []
    if len(outline) >= 3:
        pts = [
            (v.get("lon", 0.0), v.get("lat", 0.0))
            for v in outline
            if "lat" in v and "lon" in v
        ]
        if len(pts) >= 3:
            from shapely.geometry import Point, Polygon
            poly = Polygon(pts)
            if poly.is_valid and poly.area > 0:
                for h3_id in all_h3_ids:
                    latlng = h3.cell_to_latlng(h3_id)
                    pt = Point(latlng[1], latlng[0])
                    if poly.contains(pt):
                        cells.add(h3_id)
                return cells

    # Method 2: location_absolute → point buffer
    loc = feat.get("location_absolute") or {}
    if "lat" in loc and "lon" in loc:
        from shapely.geometry import Point
        radius = _feat_radius(feat)
        center = Point(float(loc["lon"]), float(loc["lat"]))
        for h3_id in all_h3_ids:
            latlng = h3.cell_to_latlng(h3_id)
            pt = Point(latlng[1], latlng[0])
            if center.distance(pt) <= radius:
                cells.add(h3_id)
        return cells

    # Method 3: location_region_hint — can't resolve without context
    return cells


def compute_river_mods(
    river_feat: dict,
    all_h3_ids: List[str],
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute elevation modifiers for a river constraint.

    Returns:
        (river_bed_map, ridge_map) where each is {h3_id: forced_elevation}.
    """
    river_bed: Dict[str, float] = {}
    ridge: Dict[str, float] = {}

    outline = river_feat.get("outline_vertices") or []
    if len(outline) < 2:
        return river_bed, ridge

    from shapely.geometry import LineString, Point

    pts = [
        (v.get("lon", 0.0), v.get("lat", 0.0))
        for v in outline
        if "lat" in v and "lon" in v
    ]
    if len(pts) < 2:
        return river_bed, ridge

    line = LineString(pts)
    river_width = _feat_radius(river_feat) * 0.3
    if river_width < 0.1:
        river_width = 0.3
    ridge_width = river_width * 3.0

    for h3_id in all_h3_ids:
        latlng = h3.cell_to_latlng(h3_id)
        pt = Point(latlng[1], latlng[0])
        dist = line.distance(pt)
        if dist < river_width:
            river_bed[h3_id] = -0.15  # below sea level
        elif dist < ridge_width:
            ridge[h3_id] = 0.25  # raised watershed

    return river_bed, ridge


# ======================================================================
# Main load function
# ======================================================================


def load_constraints() -> WMConstraints:
    """Load all WM constraints from game/wm_state/*.json.

    Call this BEFORE generate_world() to get both the params
    and the spatial elevation modifiers.
    """
    c = WMConstraints()

    # 1. Orientation
    c.orientation = _read_json("world_orientation.json") or {}

    # 2. Features — group by type (with alias resolution)
    raw_features = _read_json("features.json") or []
    c.features = raw_features
    for feat in raw_features:
        ft = (feat.get("feature_type") or "").strip().lower()

        # Resolve aliases
        if ft in _FT_CONTINENT:
            c.continent_constraints.append(feat)
        elif ft in _FT_MOUNTAIN:
            c.mountain_constraints.append(feat)
        elif ft in _FT_RIVER:
            c.river_constraints.append(feat)
        elif ft in _FT_LAKE:
            c.lake_constraints.append(feat)
        elif ft in _FT_TERRAIN:
            c.terrain_cover_constraints.append(feat)
        elif ft in _FT_CLIMATE:
            c.climate_zone_constraints.append(feat)
        elif ft == "water_body":
            # water_body is ambiguous — check properties.type
            props = feat.get("properties") or {}
            wtype = (props.get("type") or "").strip().lower()
            if wtype == "river":
                c.river_constraints.append(feat)
            else:
                # sea, lake, ocean, bay, or unspecified → lake depression
                c.lake_constraints.append(feat)
        elif ft in _RECOGNISED_FT:
            # Known type that doesn't affect L0 elevation — store but
            # don't add to any constraint group (settlement, ruin, etc.)
            pass
        else:
            # Unknown type — record warning
            if ft not in c.unknown_feature_types:
                c.unknown_feature_types.append(ft)

    # 3. Concepts — group by type
    raw_concepts = _read_json("world_concepts.json") or []
    c.concepts = raw_concepts
    for ct in raw_concepts:
        ctype = (ct.get("concept_type") or "").strip()
        if ctype == "fauna_species":
            c.fauna_species.append(ct)
        elif ctype == "flora_pft":
            c.flora_pft.append(ct)
        elif ctype == "ore_type":
            c.ore_types.append(ct)
        elif ctype == "settlement_type":
            c.settlement_types.append(ct)
        elif ctype == "canon_constraint":
            c.canon_constraints.append(ct)
        elif ctype == "existence_type":
            c.existence_types.append(ct)

    # 4. Factions
    c.factions = _read_json("factions.json") or []

    # 5. Entities
    c.entities = _read_json("entities.json") or []

    # 6. Player start
    c.player_start = _read_json("player_start.json") or {}

    return c


def resolve_all_spatial(
    c: WMConstraints,
    all_h3_ids: List[str],
) -> None:
    """Resolve spatial constraints to elevation modifiers.

    Must be called AFTER h3_ids are known (i.e. after params.derive()
    but before generate_world()). Modifies the WMConstraints in place.
    """
    # Continents
    for feat in c.continent_constraints:
        anchor = (feat.get("anchor_strength") or "").strip()
        if anchor == "fleeting":
            continue
        cells = resolve_to_cells(feat, all_h3_ids)
        for h in cells:
            c.continent_elevation[h] = max(c.continent_elevation.get(h, 0.0), 0.2)

    # Mountains
    for feat in c.mountain_constraints:
        cells = resolve_to_cells(feat, all_h3_ids)
        for h in cells:
            c.mountain_elevation[h] = max(c.mountain_elevation.get(h, 0.0), 0.6)

    # Lakes — depression in elevation
    for feat in c.lake_constraints:
        cells = resolve_to_cells(feat, all_h3_ids)
        for h in cells:
            c.lake_elevation[h] = min(c.lake_elevation.get(h, 0.0), -0.2)

    # Rivers — valley + ridge
    for feat in c.river_constraints:
        bed, ridge = compute_river_mods(feat, all_h3_ids)
        c.river_bed_elevation.update(bed)
        c.ridge_elevation.update(ridge)
