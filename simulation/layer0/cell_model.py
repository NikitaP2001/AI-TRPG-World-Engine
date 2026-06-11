"""Layer 0 — cell model, world orientation, and generation parameters.

Grid geometry follows from a single WM input (planet_radius).
All other parameters are derived. No hardcoded constants except
the base resolution ratio (planet_radius / 60), which sets the
scale at which planetary geography is meaningfully distinguishable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ======================================================================
# World Orientation — established once at initialization
# ======================================================================


@dataclass
class WorldOrientation:
    """Coordinate reference frame for the world.

    Set once at world initialization. All coordinate expressions
    (absolute lat/lon, relative offsets, contextual hints) resolve
    against this frame. Never changed after creation.
    """

    planet_radius: float                    # radius of the world body (any consistent unit)

    reference_meridian: float = 0.0         # longitude of prime meridian (degrees)
    axial_tilt: float = 23.5                # degrees — drives climate seasonality

    # ── Global climate parameters ────────────────────────────────────
    global_temperature_offset: float = 0.0       # added to every cell's temperature
    global_precipitation_modifier: float = 1.0   # multiplied with cell precipitation
    solar_intensity: float = 1.0                 # multiplies total solar energy
    atmospheric_density: float = 1.0             # temperature buffering
    ocean_temperature: float = 0.5               # base ocean surface temperature

    named_directions: Dict[str, str] = field(default_factory=lambda: {
        "north": "north",
        "south": "south",
        "east": "east",
        "west": "west",
    })

    # ── Derived grid properties ──────────────────────────────────────

    @property
    def cell_side_length(self) -> float:
        """Hex side length derived from planet radius.

        The ratio planet_radius / 60 is the scale at which climate zones,
        major terrain systems, and continental drainage basins each span
        multiple cells rather than being smeared into one or lost between two.
        """
        return self.planet_radius / 60.0

    @property
    def cell_area(self) -> float:
        """Area of a regular hexagon with side length s: A = (3√3/2)s²."""
        s = self.cell_side_length
        return 3.0 * math.sqrt(3.0) / 2.0 * s * s

    @property
    def surface_area(self) -> float:
        """Total surface area of the planet sphere."""
        return 4.0 * math.pi * self.planet_radius ** 2

    @property
    def top_level_cell_count(self) -> int:
        """Number of base-resolution cells needed to cover the sphere.

        surface_area / cell_area cancels planet_radius², yielding a
        constant ≈ 17 400 regardless of world size.
        """
        return int(round(self.surface_area / self.cell_area))


# ======================================================================
# Generation parameters — the only WM-authored inputs
# ======================================================================


@dataclass
class GenerationParams:
    """World Master inputs for Layer 0 generation.

    All grid geometry falls out from planet_radius (via WorldOrientation).
    No cell counts, resolutions, or size constants are specified here.
    """

    # ── Geometry ─────────────────────────────────────────────────────
    planet_radius: float = 1.0          # radius of the world body (any unit)
    world_extent: float = 1.0           # fraction of surface to simulate (0.0–1.0)

    # ── Terrain ──────────────────────────────────────────────────────
    tectonic_activity: float = 0.5      # 0.0–1.0 — plate count, uplift magnitude
    num_plates: int = 8                 # number of tectonic plates (3-16)
    roughness: float = 0.6              # 0.0–1.0 — fractal terrain roughness

    # ── Climate ──────────────────────────────────────────────────────
    axial_tilt: float = 23.5            # degrees — 0 = no seasons, 23.5 = Earth-like

    # ── Ocean currents (P2.6) ────────────────────────────────────────
    ocean_currents_enabled: bool = True
    ocean_wind_drag: float = 0.03        # fraction of wind → surface current
    ocean_ekman_angle: float = 45.0      # Ekman turning angle [deg]
    ocean_coastal_radius: float = 5.0    # coastal climate influence [deg]

    # ── Randomisation ────────────────────────────────────────────────
    seed: int = 42

    # ── Derived (computed once at generation start) ──────────────────
    # These are populated by _derive_params() before generation runs.
    _h3_resolution: int = 0
    _top_level_cell_count: int = 0
    _cell_side_length: float = 0.0

    def derive(self) -> None:
        """Compute derived grid properties from WM inputs."""
        orient = WorldOrientation(
            planet_radius=self.planet_radius,
            axial_tilt=self.axial_tilt,
        )
        target = int(round(orient.top_level_cell_count * self.world_extent))
        # Clamp to at least 1 cell
        target = max(1, target)
        self._top_level_cell_count = target
        self._cell_side_length = orient.cell_side_length

        # Pick H3 resolution whose total cell count ≈ target
        # Bias toward higher resolution when target falls between two levels
        # (gives better texture quality, especially near poles)
        best_res = 0
        best_diff = float("inf")
        for res in range(16):
            count = 122 * (7 ** res)
            diff = abs(count - target)
            if diff < best_diff:
                best_diff = diff
                best_res = res
        self._h3_resolution = best_res

    @property
    def h3_resolution(self) -> int:
        return self._h3_resolution

    @property
    def top_level_cell_count(self) -> int:
        return self._top_level_cell_count

    @property
    def cell_side_length(self) -> float:
        return self._cell_side_length

    @property
    def surface_area(self) -> float:
        return 4.0 * math.pi * self.planet_radius ** 2 * self.world_extent

    @property
    def cell_area(self) -> float:
        return self.surface_area / max(1, self.top_level_cell_count)


# ======================================================================
# Cell condition vector — interface to all higher layers
# ======================================================================


@dataclass
class CellData:
    """Per-cell condition vector — the complete Layer 0 output for one H3 cell.

    This is the read-only interface between Layer 0 and all higher layers.
    Stores only physics fields that are genuinely meaningful at cell resolution.
    Terrain features (rivers, forests, mountains, lakes) live in the feature store.

    Higher layers read from it; they do not write to it except through
    the long-cycle tick system.
    """

    # ── Identity ─────────────────────────────────────────────────────
    h3_id: str                          # H3 cell identifier (hex string)
    resolution: int                      # H3 resolution level (constant across grid)

    # ── Terrain physics ──────────────────────────────────────────────
    elevation_mean: float = 0.0          # mean elevation across cell area (world units)
    elevation_variance: float = 0.0      # internal elevation variance — high = complex terrain
    slope: Tuple[float, float] = (0.0, 0.0)  # gradient (magnitude, direction_radians)
    geological_type: int = 0              # 0=oceanic, 1=continental shelf,
                                          # 2=continental, 3=mountain belt,
                                          # 4=rift valley, 5=craton

    # ── Hydrology ────────────────────────────────────────────────────
    water_table_depth: float = 0.0        # depth to groundwater (world units)

    # ── Climate ──────────────────────────────────────────────────────
    temperature: float = 0.5              # 0.0–1.0 normalised annual mean
    temp_seasonal_range: float = 0.2      # warmest–coldest month difference
    precipitation: float = 0.5            # 0.0–1.0 normalised annual total
    precip_seasonality: float = 0.3       # coefficient of variation of monthly precip
    climate_class: str = ""               # Köppen-Geiger class code (e.g. "Cfb")
    prevailing_wind: Tuple[float, float] = (0.0, 0.0)
                                          # dominant surface wind (u, v)

    # ── Surface physics ──────────────────────────────────────────────
    soil_fertility: float = 0.5           # 0.0–1.0 normalised agricultural potential
    hazard_level: float = 0.0             # 0.0–1.0 baseline environmental danger

    # ── Special resources ────────────────────────────────────────────
    special_resource_flux: List[float] = field(default_factory=list)
                                          # one value per world-defined resource type

    # ── Tectonics ────────────────────────────────────────────────────
    plate_id: int = -1                    # tectonic plate assignment (-1 = unassigned)
    boundary_type: str = "intraplate"      # convergent/divergent/transform/intraplate
    distance_to_boundary: float = -1.0     # cells to nearest plate boundary

    # ── Deep geology ─────────────────────────────────────────────────
    crustal_age_myr: float = 100.0        # crustal age in million years
    crustal_thickness_km: float = 35.0    # crustal thickness in km
    thermal_gradient: float = 25.0        # geothermal gradient in °C/km

    # ── Geology ──────────────────────────────────────────────────────
    bedrock_class: str = "unknown"         # mineral profile key (e.g. "oceanic_basalt")

    # ── Soil ─────────────────────────────────────────────────────────
    soil_depth: float = 0.0               # soil depth in world units (0-2 scale)
    organic_matter: float = 0.0           # 0.0-1.0 normalized organic content
    clay_content: float = 0.0             # soil texture fraction
    sand_content: float = 0.0             # soil texture fraction
    silt_content: float = 0.0             # soil texture fraction
    soil_ph: float = 7.0                  # pH scale
    cation_exchange: float = 5.0          # CEC in cmol/kg

    # ── Vegetation ───────────────────────────────────────────────────
    vegetation_cover: str = "barren"       # barren/tundra/desert/grassland/shrubland/forest/rainforest/taiga
    canopy_density: float = 0.0            # 0–1 continuous canopy cover (NEW continuous model)
    biomass_kgm2: float = 0.0              # total above-ground biomass kg/m²
    interception_coefficient: float = 0.15  # PFT-weighted canopy interception fraction (P1.7)

    # ── Hydrology ────────────────────────────────────────────────────
    runoff_ratio: float = 0.5             # fraction of precipitation that runs off
    effective_precip: float = 0.0         # precipitation * runoff_ratio

    # ── Generation metadata ──────────────────────────────────────────
    tectonic_stress: float = 0.0
    anchor_feature_ids: List[str] = field(default_factory=list)
                                          # feature_ids of fixed anchors that constrained
                                          # this cell's generation. Long-cycle drift
                                          # does not override these.

    # ── Feature intersection cache ───────────────────────────────────
    feature_ids: List[str] = field(default_factory=list)
                                          # All feature_ids whose geometry intersects
                                          # this cell. Maintained by feature store trigger.
                                          # Read-only from simulation perspective.
