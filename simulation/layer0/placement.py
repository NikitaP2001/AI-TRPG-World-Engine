"""Layer 0 — Coordinate system and feature/entity placement API.

Three placement methods (design doc § Coordinate System):
  1. ABSOLUTE — geographic lat/lon
  2. RELATIVE — polar offset from a known reference
  3. CONTEXTUAL — loose placement by containment or region hint

All resolve to H3 cell IDs internally. Raw H3 IDs are never exposed
to callers.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import h3

from .cell_model import CellData, WorldOrientation
from .feature_store import FeatureStore, Feature


# ======================================================================
# Exceptions
# ======================================================================


class PlacementError(Exception):
    """Raised when a placement cannot be resolved."""


class ConflictError(PlacementError):
    """Raised when fixed anchors conflict."""


# ======================================================================
# Location specifiers (input types)
# ======================================================================


@dataclass
class AbsoluteLocation:
    """Method 1 — absolute lat/lon/alt."""
    lat: float        # -90 to +90
    lon: float        # -180 to +180
    alt: Optional[float] = None  # null = surface level


@dataclass
class RelativeLocation:
    """Method 2 — polar offset from a reference point."""
    from_ref: str      # feature_id, entity_id, or reserved string
    bearing: float     # degrees clockwise from north (0-360)
    distance: float    # world units (same unit as planet_radius)
    alt_offset: Optional[float] = None


@dataclass
class ContextualLocation:
    """Method 3 — loose placement."""
    inside: Optional[str] = None          # contained within named feature
    near: Optional[str | List[str]] = None  # adjacent to named feature(s)
    region_hint: Optional[str] = None     # natural language region


# ======================================================================
# Anchor strength
# ======================================================================

AnchorStrength = str  # "suggestion" | "preferred" | "fixed"


# ======================================================================
# Feature size specifiers
# ======================================================================


@dataclass
class SizeSpec:
    preset: Optional[str] = None  # tiny | small | medium | large | massive
    width: Optional[float] = None
    length: Optional[float] = None
    radius: Optional[float] = None
    height: Optional[float] = None
    depth: Optional[float] = None
    shape_description: Optional[str] = None


# ======================================================================
# FeatureDefinition (input to place_feature)
# ======================================================================


@dataclass
class LayerEffects:
    terrain_type_tags: Optional[List[str]] = None
    elevation_modifier: Optional[float] = None
    soil_fertility_modifier: Optional[float] = None
    hazard_modifier: Optional[float] = None
    special_resource_overrides: Optional[Dict[str, float]] = None
    water_body_type_override: Optional[int] = None


@dataclass
class GenerationBehaviour:
    anchor_strength: AnchorStrength = "preferred"
    subdivision_depth: Optional[int] = None
    layer_effects: Optional[LayerEffects] = None


@dataclass
class FeatureDefinition:
    """Schema for place_feature()."""
    type: str
    name: Optional[str] = None

    # Location — provide exactly one
    absolute: Optional[AbsoluteLocation] = None
    relative: Optional[RelativeLocation] = None
    inside: Optional[str] = None
    near: Optional[str | List[str]] = None
    region_hint: Optional[str] = None

    # Size
    size: Optional[SizeSpec] = None

    # Properties (open key-value)
    properties: Dict[str, Any] = field(default_factory=dict)

    # Relationships
    relationships: Dict[str, Any] = field(default_factory=dict)

    # Generation behaviour
    generation: GenerationBehaviour = field(default_factory=GenerationBehaviour)


# ======================================================================
# FeatureResult (output of place_feature)
# ======================================================================


@dataclass
class ResolvedLocation:
    center: Tuple[float, float, float]  # (x, y, z) in world coords
    lat: float
    lon: float
    alt: float
    h3_cells: List[str]
    resolution_levels: List[int]


@dataclass
class LayerEffectsApplied:
    cells_modified: int = 0
    fields_written: List[str] = field(default_factory=list)


@dataclass
class RelationshipsDetected:
    contained_by: Optional[str] = None
    contains: List[str] = field(default_factory=list)
    adjacent_to: List[str] = field(default_factory=list)
    hydrological_connections: List[str] = field(default_factory=list)


@dataclass
class FeatureResult:
    feature_id: str
    name: str
    resolved_location: ResolvedLocation
    parent_feature_id: Optional[str] = None
    subdivisions_created: int = 0
    layer_effects_applied: LayerEffectsApplied = field(default_factory=LayerEffectsApplied)
    relationships_detected: RelationshipsDetected = field(default_factory=RelationshipsDetected)
    warnings: List[str] = field(default_factory=list)


# ======================================================================
# EntityPlacement (input to place_entity)
# ======================================================================


@dataclass
class EntityPlacement:
    entity_id: Optional[str] = None
    archetype_id: Optional[str] = None
    is_player: bool = False

    # Location — same three methods
    absolute: Optional[AbsoluteLocation] = None
    relative: Optional[RelativeLocation] = None
    inside: Optional[str] = None
    near: Optional[str] = None

    facing: Optional[float] = None


@dataclass
class EntityPlacementResult:
    entity_id: str
    resolved_location: ResolvedLocation
    player_start_registered: bool = False
    containing_features: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ======================================================================
# Feature Registry
# ======================================================================


class FeatureRegistry:
    """Stores placed features and reserved reference points."""

    def __init__(self) -> None:
        self._features: Dict[str, FeatureResult] = {}
        self._reserved: Dict[str, ResolvedLocation] = {}

    def register(self, result: FeatureResult) -> None:
        self._features[result.feature_id] = result
        if result.name:
            self._features[result.name] = result

    def register_reserved(self, name: str, location: ResolvedLocation) -> None:
        self._reserved[name] = location

    def get_location(self, ref: str) -> Optional[ResolvedLocation]:
        """Look up a reference by feature_id, name, or reserved string."""
        if ref in self._reserved:
            return self._reserved[ref]
        feat = self._features.get(ref)
        if feat:
            return feat.resolved_location
        return None

    def has(self, ref: str) -> bool:
        return ref in self._reserved or ref in self._features

    @property
    def all_features(self) -> List[FeatureResult]:
        return list(self._features.values())

    def clear(self) -> None:
        self._features.clear()
        self._reserved.clear()


# ======================================================================
# Coordinate resolution
# ======================================================================

_RESERVED_NAMES = {"player_start", "world_center", "north_pole", "south_pole"}


def _latlng_to_h3(lat: float, lon: float, resolution: int) -> str:
    """Convert lat/lon to the nearest H3 cell at the given resolution."""
    return h3.latlng_to_cell(lat, lon, resolution)


def _great_circle_distance(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Haversine distance in radians."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2
    return 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _bearing_to_destination(
    lat: float, lon: float, bearing_deg: float, distance_rad: float,
) -> Tuple[float, float]:
    """Given start (lat, lon), bearing (deg clockwise from N), distance (radians),
    return destination (lat, lon) in degrees."""
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    brng = math.radians(bearing_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(distance_rad) +
                     math.cos(lat1) * math.sin(distance_rad) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(distance_rad) * math.cos(lat1),
                              math.cos(distance_rad) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))


def resolve_absolute(
    loc: AbsoluteLocation,
    orientation: WorldOrientation,
    resolution: int = 2,
) -> ResolvedLocation:
    """Resolve absolute lat/lon to an H3 cell."""
    lat = max(-90.0, min(90.0, loc.lat))
    lon = (loc.lon + 180.0) % 360.0 - 180.0  # normalise to -180..180
    alt = loc.alt if loc.alt is not None else 0.0

    h3_cell = _latlng_to_h3(lat, lon, resolution)

    # World-coordinate centre (unit sphere)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    r = orientation.planet_radius + alt
    x = r * math.cos(lat_r) * math.cos(lon_r)
    y = r * math.sin(lat_r)
    z = r * math.cos(lat_r) * math.sin(lon_r)

    return ResolvedLocation(
        center=(x, y, z),
        lat=lat, lon=lon, alt=alt,
        h3_cells=[h3_cell],
        resolution_levels=[resolution],
    )


def resolve_relative(
    loc: RelativeLocation,
    orientation: WorldOrientation,
    registry: FeatureRegistry,
    resolution: int = 2,
) -> ResolvedLocation:
    """Resolve polar offset from a reference point."""
    ref_loc = registry.get_location(loc.from_ref)
    if ref_loc is None:
        # Check reserved names that are not in the registry yet
        if loc.from_ref == "north_pole":
            ref_loc = ResolvedLocation(
                center=(0, orientation.planet_radius, 0),
                lat=90.0, lon=0.0, alt=0.0,
                h3_cells=[_latlng_to_h3(90.0, 0.0, resolution)],
                resolution_levels=[resolution],
            )
        elif loc.from_ref == "south_pole":
            ref_loc = ResolvedLocation(
                center=(0, -orientation.planet_radius, 0),
                lat=-90.0, lon=0.0, alt=0.0,
                h3_cells=[_latlng_to_h3(-90.0, 0.0, resolution)],
                resolution_levels=[resolution],
            )
        elif loc.from_ref == "world_center":
            ref_loc = ResolvedLocation(
                center=(0, 0, 0), lat=0.0, lon=0.0, alt=0.0,
                h3_cells=[_latlng_to_h3(0.0, 0.0, resolution)],
                resolution_levels=[resolution],
            )
        else:
            raise PlacementError(f"Unknown reference: '{loc.from_ref}'")

    # Distance in radians on the sphere
    distance_rad = loc.distance / orientation.planet_radius if orientation.planet_radius > 0 else 0.0
    dest_lat, dest_lon = _bearing_to_destination(
        ref_loc.lat, ref_loc.lon, loc.bearing, distance_rad,
    )

    alt = ref_loc.alt + (loc.alt_offset if loc.alt_offset is not None else 0.0)
    abs_loc = AbsoluteLocation(lat=dest_lat, lon=dest_lon, alt=alt)
    return resolve_absolute(abs_loc, orientation, resolution)


def resolve_contextual(
    loc: ContextualLocation,
    orientation: WorldOrientation,
    registry: FeatureRegistry,
    resolution: int = 2,
) -> ResolvedLocation:
    """Resolve contextual placement (inside, near, region_hint)."""
    warnings: List[str] = []

    if loc.inside:
        ref = registry.get_location(loc.inside)
        if ref:
            # Place at the same location as the containing feature
            return ResolvedLocation(
                center=ref.center,
                lat=ref.lat, lon=ref.lon, alt=ref.alt,
                h3_cells=ref.h3_cells[:1],  # take the first cell
                resolution_levels=[resolution],
            )
        warnings.append(f"Container '{loc.inside}' not found — falling back to world_center")

    if loc.near:
        refs = [loc.near] if isinstance(loc.near, str) else loc.near
        for ref_name in refs:
            ref_loc = registry.get_location(ref_name)
            if ref_loc:
                # Place at a small random offset from the reference
                rng = random.Random(hash(ref_name) & 0xFFFFFFFF)
                bearing = rng.random() * 360.0
                dist_rad = math.radians(rng.random() * 5.0)  # 0-5 degree offset
                dest_lat, dest_lon = _bearing_to_destination(
                    ref_loc.lat, ref_loc.lon, bearing, dist_rad,
                )
                return resolve_absolute(
                    AbsoluteLocation(lat=dest_lat, lon=dest_lon, alt=ref_loc.alt),
                    orientation, resolution,
                )
        warnings.append(f"None of the 'near' references found — falling back")

    if loc.region_hint:
        # Simple heuristic: place at equator as a fallback
        pass

    # Fallback: equator at reference meridian
    fallback = resolve_absolute(AbsoluteLocation(lat=0.0, lon=orientation.reference_meridian), orientation, resolution)
    fallback.warnings.extend(warnings)
    return fallback


# ======================================================================
# Public API
# ======================================================================

_ID_COUNTER = 0


def _next_id(prefix: str = "feat") -> str:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return f"{prefix}_{_ID_COUNTER:06d}"


def place_feature(
    definition: FeatureDefinition,
    orientation: WorldOrientation,
    registry: FeatureRegistry,
    resolution: int = 2,
) -> FeatureResult:
    """Place a terrain feature at the resolved location.

    Returns a FeatureResult with resolved location and metadata.
    """
    warnings: List[str] = []

    # Resolve location
    if definition.absolute:
        resolved = resolve_absolute(definition.absolute, orientation, resolution)
    elif definition.relative:
        resolved = resolve_relative(definition.relative, orientation, registry, resolution)
    elif definition.inside or definition.near or definition.region_hint:
        resolved = resolve_contextual(
            ContextualLocation(inside=definition.inside, near=definition.near,
                               region_hint=definition.region_hint),
            orientation, registry, resolution,
        )
    else:
        raise PlacementError("No location specifier provided")

    # Generate ID
    fid = _next_id()
    name = definition.name or f"{definition.type}_{fid}"

    # Check for conflicts
    if definition.generation.anchor_strength == "fixed":
        for existing in registry.all_features:
            if existing.resolved_location.h3_cells[0] == resolved.h3_cells[0]:
                raise ConflictError(
                    f"Fixed anchor '{name}' conflicts with '{existing.name}' at cell {resolved.h3_cells[0]}"
                )

    result = FeatureResult(
        feature_id=fid,
        name=name,
        resolved_location=resolved,
        warnings=warnings,
    )

    registry.register(result)
    return result


def place_entity(
    placement: EntityPlacement,
    orientation: WorldOrientation,
    registry: FeatureRegistry,
    resolution: int = 2,
) -> EntityPlacementResult:
    """Place an entity or player at the resolved location."""
    warnings: List[str] = []
    player_start_registered = False

    # Resolve location
    if placement.absolute:
        resolved = resolve_absolute(placement.absolute, orientation, resolution)
    elif placement.relative:
        resolved = resolve_relative(placement.relative, orientation, registry, resolution)
    elif placement.inside or placement.near:
        resolved = resolve_contextual(
            ContextualLocation(inside=placement.inside, near=placement.near),
            orientation, registry, resolution,
        )
    else:
        raise PlacementError("No location specifier provided")

    eid = placement.entity_id or _next_id("ent")

    # First player placement registers "player_start"
    if placement.is_player and not registry.has("player_start"):
        registry.register_reserved("player_start", resolved)
        player_start_registered = True

    return EntityPlacementResult(
        entity_id=eid,
        resolved_location=resolved,
        player_start_registered=player_start_registered,
        warnings=warnings,
    )


# ======================================================================
# Continent placement
# ======================================================================


@dataclass
class ContinentDefinition:
    """Definition for place_continent()."""
    name: str
    vertices: List[Tuple[float, float]]   # [(lat, lon), ...] in order
    anchor_strength: str = "fixed"        # always fixed for continents


def place_continent(
    definition: ContinentDefinition,
    orientation: WorldOrientation,
    feature_store: FeatureStore,
    cells: List[CellData],
) -> Feature:
    """Place a continent polygon and classify cells as continental/oceanic.

    All cells inside the polygon get geological_type = 2 (continental).
    All cells outside get geological_type = 0 (oceanic).
    Boundary cells (within one cell width of the polygon edge) get
    geological_type = 1 (continental shelf).

    Returns the created Feature.
    """
    import h3
    from shapely.geometry import Point as SPoint

    # Create polygon
    polygon = FeatureStore.make_polygon([definition.vertices])

    # Classify cells
    cell_side = orientation.cell_side_length
    boundary_dist_deg = math.degrees(cell_side / orientation.planet_radius) if orientation.planet_radius > 0 else 1.0

    for cell in cells:
        latlng = h3.cell_to_latlng(cell.h3_id)
        pt = SPoint(latlng[1], latlng[0])

        if polygon.contains(pt):
            cell.geological_type = 2  # continental
        elif polygon.distance(pt) < boundary_dist_deg:
            cell.geological_type = 1  # continental shelf
        else:
            cell.geological_type = 0  # oceanic

    # Create feature
    feature = feature_store.add_feature(Feature(
        type="continent",
        name=definition.name,
        geometry=polygon,
        anchor_strength=definition.anchor_strength,
        properties={"cell_count": sum(1 for c in cells if c.geological_type == 2)},
    ))

    # Sync cells
    for cell in cells:
        feature_store.sync_cell(cell)

    print(f"[Continent] '{definition.name}': "
          f"{sum(1 for c in cells if c.geological_type == 2)} continental, "
          f"{sum(1 for c in cells if c.geological_type == 0)} oceanic cells")
    return feature
