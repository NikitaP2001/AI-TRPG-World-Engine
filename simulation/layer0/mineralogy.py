"""Mineralogy — mineral registry and ore deposit generation.

Feature-based system: each ore deposit is a Feature in the FeatureStore
with properties: grade, volume, depth, formation mechanism, rarity.

Supports:
  - 30+ real minerals (GTNH-style: coal → uranium → platinum)
  - 5 formation types (sedimentary, magmatic, hydrothermal, metamorphic, placer)
  - Extensible registry for fantasy ores (wM-style)
  - Context-aware generation (tectonic, lithologic, age constraints)

Usage:
    from simulation.layer0.mineralogy import generate_all_ores, register_ore_type
    ores = generate_all_ores(cells, lithology, tectonics, feature_store, rng)
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from shapely.geometry import Point, Polygon, LineString
from shapely.ops import unary_union

from .cell_model import CellData
from .feature_store import Feature


# ======================================================================
# Mineral definitions
# ======================================================================


@dataclass
class MineralDef:
    """One mineral species."""
    name: str
    formula: str = ""
    density_gcm3: float = 2.5
    hardness_mohs: float = 5.0
    category: str = "mineral"  # mineral, ore_metal, ore_gem, ore_rare, ore_energy, fantasy
    value: float = 1.0          # economic value index (1-100)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "formula": self.formula,
            "density": self.density_gcm3, "hardness": self.hardness_mohs,
            "category": self.category, "value": self.value,
        }


# ── Mineral registry ─────────────────────────────────────────────

MINERAL_REGISTRY: Dict[str, MineralDef] = {
    # Energy minerals
    "coal": MineralDef("Coal", "C", density_gcm3=1.35, hardness_mohs=2, category="ore_energy", value=3),
    "lignite": MineralDef("Lignite", "C", density_gcm3=1.2, hardness_mohs=1.5, category="ore_energy", value=2),
    "anthracite": MineralDef("Anthracite", "C", density_gcm3=1.6, hardness_mohs=2.5, category="ore_energy", value=5),
    "uraninite": MineralDef("Uraninite", "UO2", density_gcm3=10.6, hardness_mohs=5.5, category="ore_energy", value=50),
    "thorite": MineralDef("Thorite", "ThSiO4", density_gcm3=6.7, hardness_mohs=5, category="ore_energy", value=30),

    # Iron group
    "hematite": MineralDef("Hematite", "Fe2O3", density_gcm3=5.26, hardness_mohs=5.5, category="ore_metal", value=4),
    "magnetite": MineralDef("Magnetite", "Fe3O4", density_gcm3=5.15, hardness_mohs=5.5, category="ore_metal", value=5),
    "goethite": MineralDef("Goethite", "FeO(OH)", density_gcm3=4.3, hardness_mohs=5, category="ore_metal", value=3),
    "siderite": MineralDef("Siderite", "FeCO3", density_gcm3=3.9, hardness_mohs=4, category="ore_metal", value=3),
    "pyrite": MineralDef("Pyrite", "FeS2", density_gcm3=5.0, hardness_mohs=6, category="mineral", value=1),

    # Base metals
    "chalcopyrite": MineralDef("Chalcopyrite", "CuFeS2", density_gcm3=4.2, hardness_mohs=3.5, category="ore_metal", value=12),
    "bornite": MineralDef("Bornite", "Cu5FeS4", density_gcm3=5.1, hardness_mohs=3, category="ore_metal", value=15),
    "galena": MineralDef("Galena", "PbS", density_gcm3=7.6, hardness_mohs=2.5, category="ore_metal", value=10),
    "sphalerite": MineralDef("Sphalerite", "ZnS", density_gcm3=4.0, hardness_mohs=3.5, category="ore_metal", value=8),
    "pentlandite": MineralDef("Pentlandite", "(Fe,Ni)9S8", density_gcm3=4.8, hardness_mohs=3.5, category="ore_metal", value=20),

    # Light metals
    "bauxite": MineralDef("Bauxite", "Al2O3·H2O", density_gcm3=2.5, hardness_mohs=2, category="ore_metal", value=6),
    "cassiterite": MineralDef("Cassiterite", "SnO2", density_gcm3=6.9, hardness_mohs=6.5, category="ore_metal", value=25),
    "wolframite": MineralDef("Wolframite", "(Fe,Mn)WO4", density_gcm3=7.5, hardness_mohs=5, category="ore_metal", value=35),
    "molybdenite": MineralDef("Molybdenite", "MoS2", density_gcm3=4.7, hardness_mohs=1.5, category="ore_metal", value=30),

    # Rare / Speciality metals
    "ilmenite": MineralDef("Ilmenite", "FeTiO3", density_gcm3=4.7, hardness_mohs=5.5, category="ore_rare", value=15),
    "rutile": MineralDef("Rutile", "TiO2", density_gcm3=4.2, hardness_mohs=6, category="ore_rare", value=20),
    "chromite": MineralDef("Chromite", "FeCr2O4", density_gcm3=4.5, hardness_mohs=5.5, category="ore_rare", value=15),
    "cobaltite": MineralDef("Cobaltite", "(Co,Fe)AsS", density_gcm3=6.3, hardness_mohs=5.5, category="ore_rare", value=40),
    "columbite": MineralDef("Columbite", "(Fe,Mn)Nb2O6", density_gcm3=5.2, hardness_mohs=6, category="ore_rare", value=45),
    "tantalite": MineralDef("Tantalite", "(Fe,Mn)Ta2O6", density_gcm3=8.2, hardness_mohs=6.5, category="ore_rare", value=55),

    # Precious metals
    "native_gold": MineralDef("Gold", "Au", density_gcm3=19.3, hardness_mohs=2.5, category="ore_metal", value=80),
    "native_silver": MineralDef("Silver", "Ag", density_gcm3=10.5, hardness_mohs=2.5, category="ore_metal", value=40),
    "native_platinum": MineralDef("Platinum", "Pt", density_gcm3=21.4, hardness_mohs=4, category="ore_rare", value=90),
    "osmiridium": MineralDef("Osmiridium", "Os,Ir", density_gcm3=22.0, hardness_mohs=6.5, category="ore_rare", value=95),

    # Gemstones (industrial)
    "diamond": MineralDef("Diamond", "C", density_gcm3=3.5, hardness_mohs=10, category="ore_gem", value=60),
    "corundum": MineralDef("Corundum", "Al2O3", density_gcm3=3.9, hardness_mohs=9, category="ore_gem", value=20),

    # Industrial minerals
    "calcite": MineralDef("Calcite", "CaCO3", density_gcm3=2.7, hardness_mohs=3, category="mineral", value=1),
    "fluorite": MineralDef("Fluorite", "CaF2", density_gcm3=3.2, hardness_mohs=4, category="mineral", value=3),
    "barite": MineralDef("Barite", "BaSO4", density_gcm3=4.5, hardness_mohs=3, category="mineral", value=4),
    "apatite": MineralDef("Apatite", "Ca5(PO4)3", density_gcm3=3.2, hardness_mohs=5, category="mineral", value=2),
    "graphite": MineralDef("Graphite", "C", density_gcm3=2.2, hardness_mohs=1, category="ore_energy", value=3),
    "kyanite": MineralDef("Kyanite", "Al2SiO5", density_gcm3=3.6, hardness_mohs=6.5, category="ore_gem", value=8),

    # Gangue (waste rock)
    "quartz": MineralDef("Quartz", "SiO2", density_gcm3=2.65, hardness_mohs=7, category="mineral", value=0),
    "feldspar": MineralDef("Feldspar", "KAlSi3O8", density_gcm3=2.56, hardness_mohs=6, category="mineral", value=0),
    "mica": MineralDef("Mica", "KAl2(AlSi3O10)", density_gcm3=2.8, hardness_mohs=2.5, category="mineral", value=0),
    "olivine": MineralDef("Olivine", "(Mg,Fe)2SiO4", density_gcm3=3.3, hardness_mohs=6.5, category="mineral", value=0),
}


# ======================================================================
# Ore vein / deposit definition
# ======================================================================


@dataclass
class OreFormation:
    """Formation rules for one ore/vein type.

    Controls where, how deep, and how large deposits form.
    """
    name: str
    primary_ore: str                    # main mineral key
    secondary_ores: List[str] = field(default_factory=list)
    gangue: List[str] = field(default_factory=list)

    # Formation constraints
    formation_type: str = "hydrothermal"  # sedimentary/magmatic/hydrothermal/metamorphic/placer
    host_rocks: List[str] = field(default_factory=lambda: ["granite", "diorite"])
    depth_range: Tuple[float, float] = (100, 1500)  # meters
    grade_range: Tuple[float, float] = (0.1, 0.5)    # concentration

    # Geological context
    required_geo_types: List[int] = field(default_factory=lambda: [2, 3, 5])
    required_age_min: float = 0.0
    required_age_max: float = 5000.0
    required_tectonic: Optional[str] = None  # convergent/divergent/intraplate
    min_crustal_thickness: float = 0.0

    # Abundance
    rarity: float = 0.005    # probability per qualifying cell (0-1)
    vein_volume_range: Tuple[float, float] = (1000, 50000)  # m³
    vein_shape: str = "vein"  # vein/layer/pipe/scattered

    # Override function (for fantasy ores with custom logic)
    custom_test: Optional[Callable] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "primary": self.primary_ore,
            "secondary": self.secondary_ores,
            "gangue": self.gangue,
            "formation": self.formation_type,
            "host_rocks": self.host_rocks,
            "depth_range": list(self.depth_range),
            "grade_range": list(self.grade_range),
            "rarity": self.rarity,
        }


# ── Ore formation registry ───────────────────────────────────────

ORE_REGISTRY: Dict[str, OreFormation] = {}

def _init_default_ores():
    """Populate ORE_REGISTRY with default formations."""
    global ORE_REGISTRY
    ORE_REGISTRY = {}

    # ── Sedimentary deposits ──
    _reg("sedimentary_coal", OreFormation(
        "Coal", "coal", ["lignite"], ["shale", "sandstone"],
        formation_type="sedimentary", host_rocks=["shale", "sandstone", "conglomerate"],
        depth_range=(50, 1000), grade_range=(0.3, 0.8),
        required_geo_types=[1, 2], rarity=0.02,
        vein_volume_range=(50000, 500000), vein_shape="layer",
    ))
    _reg("sedimentary_anthracite", OreFormation(
        "Anthracite", "anthracite", ["graphite"], ["shale", "quartzite"],
        formation_type="metamorphic", host_rocks=["schist", "slate", "gneiss"],
        depth_range=(500, 3000), grade_range=(0.2, 0.6),
        required_geo_types=[2, 3], rarity=0.005,
        vein_volume_range=(10000, 100000), vein_shape="layer",
    ))
    _reg("sedimentary_bauxite", OreFormation(
        "Bauxite", "bauxite", [], ["clay", "sandstone"],
        formation_type="sedimentary", host_rocks=["sandstone", "limestone", "shale"],
        depth_range=(10, 200), grade_range=(0.3, 0.6),
        required_geo_types=[2, 5], rarity=0.01,
        vein_volume_range=(10000, 200000), vein_shape="layer",
    ))
    _reg("sedimentary_evaporite", OreFormation(
        "Salt/Evaporite", "calcite", ["fluorite", "barite"], ["evaporite"],
        formation_type="sedimentary", host_rocks=["evaporite", "limestone", "dolomite"],
        depth_range=(50, 500), grade_range=(0.5, 0.9),
        required_geo_types=[1, 2], rarity=0.015,
        vein_volume_range=(50000, 500000), vein_shape="layer",
    ))

    # ── Magmatic deposits ──
    _reg("magmatic_magnetite", OreFormation(
        "Magnetite", "magnetite", ["hematite", "ilmenite"], ["gabbro", "pyroxene"],
        formation_type="magmatic", host_rocks=["gabbro", "basalt", "diorite"],
        depth_range=(200, 3000), grade_range=(0.2, 0.6),
        required_geo_types=[0, 2, 3], rarity=0.008,
        vein_volume_range=(50000, 500000), vein_shape="massive",
    ))
    _reg("magmatic_chromite", OreFormation(
        "Chromite", "chromite", ["magnetite"], ["peridotite", "olivine"],
        formation_type="magmatic", host_rocks=["peridotite", "gabbro", "dunite"],
        depth_range=(500, 5000), grade_range=(0.1, 0.4),
        required_geo_types=[0], rarity=0.003,
        vein_volume_range=(10000, 100000), vein_shape="massive",
        min_crustal_thickness=5.0,
    ))
    _reg("magmatic_ilmenite", OreFormation(
        "Ilmenite", "ilmenite", ["magnetite", "rutile"], ["gabbro", "anorthosite"],
        formation_type="magmatic", host_rocks=["gabbro", "basalt", "anorthosite"],
        depth_range=(200, 2000), grade_range=(0.1, 0.4),
        required_geo_types=[0, 2], rarity=0.005,
        vein_volume_range=(20000, 200000), vein_shape="massive",
    ))
    _reg("magmatic_nickel", OreFormation(
        "Pentlandite", "pentlandite", ["chalcopyrite", "magnetite"], ["norite", "gabbro"],
        formation_type="magmatic", host_rocks=["gabbro", "norite", "peridotite"],
        depth_range=(500, 3000), grade_range=(0.1, 0.3),
        required_geo_types=[0, 2], rarity=0.002,
        vein_volume_range=(10000, 100000), vein_shape="massive",
    ))
    _reg("magmatic_platinum", OreFormation(
        "Platinum", "native_platinum", ["osmiridium", "chromite", "pentlandite"],
        ["peridotite", "pyroxenite"],
        formation_type="magmatic", host_rocks=["peridotite", "dunite", "pyroxenite"],
        depth_range=(1000, 5000), grade_range=(0.01, 0.05),
        required_geo_types=[0], rarity=0.001,
        vein_volume_range=(1000, 50000), vein_shape="disseminated",
        required_age_min=500,
    ))

    # ── Hydrothermal deposits ──
    _reg("hydrothermal_gold", OreFormation(
        "Gold Vein", "native_gold", ["chalcopyrite", "pyrite", "galena"],
        ["quartz", "calcite"],
        formation_type="hydrothermal", host_rocks=["granite", "diorite", "gneiss", "schist"],
        depth_range=(200, 2000), grade_range=(0.001, 0.05),
        required_geo_types=[2, 3, 6], rarity=0.003,
        vein_volume_range=(500, 20000), vein_shape="vein",
    ))
    _reg("hydrothermal_silver", OreFormation(
        "Silver Vein", "native_silver", ["galena", "sphalerite"],
        ["quartz", "calcite", "barite"],
        formation_type="hydrothermal", host_rocks=["granite", "rhyolite", "andesite"],
        depth_range=(100, 1500), grade_range=(0.01, 0.15),
        required_geo_types=[2, 3, 4], rarity=0.004,
        vein_volume_range=(1000, 30000), vein_shape="vein",
    ))
    _reg("hydrothermal_copper", OreFormation(
        "Copper Vein", "chalcopyrite", ["bornite", "pyrite", "native_silver"],
        ["quartz", "calcite"],
        formation_type="hydrothermal", host_rocks=["granite", "diorite", "andesite", "basalt"],
        depth_range=(100, 2000), grade_range=(0.02, 0.15),
        required_geo_types=[2, 3, 4, 6], rarity=0.005,
        vein_volume_range=(5000, 100000), vein_shape="vein",
    ))
    _reg("hydrothermal_lead_zinc", OreFormation(
        "Lead-Zinc Vein", "galena", ["sphalerite", "native_silver"],
        ["dolomite", "calcite", "barite"],
        formation_type="hydrothermal", host_rocks=["limestone", "dolomite", "sandstone"],
        depth_range=(100, 800), grade_range=(0.05, 0.2),
        required_geo_types=[1, 2], rarity=0.003,
        vein_volume_range=(5000, 50000), vein_shape="vein",
    ))
    _reg("hydrothermal_tin", OreFormation(
        "Tin Vein", "cassiterite", ["wolframite", "molybdenite"],
        ["quartz", "topaz"],
        formation_type="hydrothermal", host_rocks=["granite", "rhyolite", "gneiss"],
        depth_range=(300, 3000), grade_range=(0.02, 0.1),
        required_geo_types=[2, 3], rarity=0.002,
        vein_volume_range=(2000, 50000), vein_shape="vein",
    ))
    _reg("hydrothermal_uranium", OreFormation(
        "Uranium Vein", "uraninite", ["thorite", "pyrite"],
        ["quartz", "fluorite", "barite"],
        formation_type="hydrothermal", host_rocks=["granite", "rhyolite", "conglomerate"],
        depth_range=(500, 3000), grade_range=(0.001, 0.05),
        required_geo_types=[2, 3, 5], rarity=0.002,
        vein_volume_range=(500, 15000), vein_shape="vein",
        required_age_min=500,
    ))
    _reg("hydrothermal_fluorite", OreFormation(
        "Fluorite", "fluorite", ["barite", "calcite"], ["quartz"],
        formation_type="hydrothermal", host_rocks=["granite", "limestone", "dolomite"],
        depth_range=(50, 1000), grade_range=(0.2, 0.6),
        required_geo_types=[2, 3, 4], rarity=0.01,
        vein_volume_range=(5000, 50000), vein_shape="vein",
    ))

    # ── Metamorphic deposits ──
    _reg("metamorphic_graphite", OreFormation(
        "Graphite", "graphite", ["anthracite"], ["schist", "gneiss"],
        formation_type="metamorphic", host_rocks=["schist", "gneiss", "marble"],
        depth_range=(500, 3000), grade_range=(0.1, 0.4),
        required_geo_types=[3], rarity=0.005,
        vein_volume_range=(10000, 100000), vein_shape="layer",
    ))
    _reg("metamorphic_marble", OreFormation(
        "Marble", "calcite", ["graphite", "garnet"], ["quartz", "mica"],
        formation_type="metamorphic", host_rocks=["limestone", "dolomite"],
        depth_range=(200, 3000), grade_range=(0.6, 0.95),
        required_geo_types=[2, 3], rarity=0.02,
        vein_volume_range=(100000, 1000000), vein_shape="massive",
    ))
    _reg("metamorphic_kyanite", OreFormation(
        "Kyanite", "kyanite", ["corundum", "sillimanite"], ["quartz", "mica"],
        formation_type="metamorphic", host_rocks=["schist", "gneiss"],
        depth_range=(1000, 5000), grade_range=(0.05, 0.2),
        required_geo_types=[3], rarity=0.002,
        vein_volume_range=(1000, 20000), vein_shape="lens",
    ))
    _reg("metamorphic_diamond", OreFormation(
        "Diamond Pipe", "diamond", ["olivine", "pyrope"], ["peridotite", "kimberlite"],
        formation_type="metamorphic", host_rocks=["peridotite", "kimberlite", "lamproite"],
        depth_range=(3000, 10000), grade_range=(0.0001, 0.001),
        required_geo_types=[5], rarity=0.0003,
        vein_volume_range=(100, 5000), vein_shape="pipe",
        required_age_min=1000,
        min_crustal_thickness=35.0,
    ))

    # ── Placer deposits ──
    _reg("placer_gold", OreFormation(
        "Placer Gold", "native_gold", ["cassiterite", "diamond"], ["quartz", "sand"],
        formation_type="placer", host_rocks=["regolith", "sandstone", "conglomerate"],
        depth_range=(0, 50), grade_range=(0.001, 0.02),
        required_geo_types=[2, 3, 5], rarity=0.002,
        vein_volume_range=(500, 10000), vein_shape="scattered",
    ))
    _reg("placer_tin", OreFormation(
        "Placer Tin", "cassiterite", ["ilmenite", "monazite"], ["quartz", "sand"],
        formation_type="placer", host_rocks=["regolith", "sandstone"],
        depth_range=(0, 30), grade_range=(0.01, 0.1),
        required_geo_types=[2, 3], rarity=0.003,
        vein_volume_range=(1000, 20000), vein_shape="scattered",
    ))


def _reg(key: str, formation: OreFormation) -> None:
    ORE_REGISTRY[key] = formation


# ======================================================================
# Context evaluation
# ======================================================================


class OreContext:
    """Geological context for ore formation evaluation."""
    def __init__(self, cell: CellData, lithology: list, tectonics):
        self.geo_type = cell.geological_type
        self.age_myr = getattr(cell, 'crustal_age_myr', 100.0)
        self.crustal_thick_km = getattr(cell, 'crustal_thickness_km', 35.0)
        self.thermal_gradient = getattr(cell, 'thermal_gradient', 25.0)
        self.boundary_type = getattr(cell, 'boundary_type', 'intraplate')
        self.elevation = getattr(cell, 'elevation_mean', 0.0)
        self.lithology = lithology
        self.h3_id = cell.h3_id

    def matches(self, formation: OreFormation) -> bool:
        """Check if this cell's context matches formation rules."""
        if formation.custom_test is not None:
            return formation.custom_test(self)

        if self.geo_type not in formation.required_geo_types:
            return False
        if self.age_myr < formation.required_age_min:
            return False
        if self.age_myr > formation.required_age_max:
            return False
        if self.crustal_thick_km < formation.min_crustal_thickness:
            return False
        if formation.required_tectonic and self.boundary_type != formation.required_tectonic:
            return False

        # Check host rock match (any layer)
        if formation.host_rocks:
            for layer in self.lithology:
                if layer.rock_type in formation.host_rocks:
                    return True
            return False

        return True


# ======================================================================
# Ore generation
# ======================================================================


def generate_ore_deposit(
    h3_id: str,
    lat: float, lon: float,
    formation: OreFormation,
    rng: random.Random,
    existing_nearby: List[Feature] = None,
) -> Feature:
    """Create a single ore deposit Feature from a formation rule.

    Generates realistic cross-cutting vein geometry:
    - If existing deposits are nearby, offset from THEM (not from cell centroid)
      to guarantee cross-cutting on multi-deposit cells.
    - Shape varies by vein_shape type (elongate for veins, irregular for stockworks)
    - Random rotation so multiple deposits on one cell cross-cut naturally
    """
    primary = formation.primary_ore
    primary_def = MINERAL_REGISTRY.get(primary)

    # Randomise grade within range
    g_min, g_max = formation.grade_range
    grade = g_min + rng.random() * (g_max - g_min)

    # Randomise volume
    v_min, v_max = formation.vein_volume_range
    volume = v_min * (v_max / v_min) ** rng.random()

    # Randomise depth
    d_top = formation.depth_range[0] + rng.random() * (formation.depth_range[1] - formation.depth_range[0])
    d_bot = d_top + volume ** (1/3) * rng.uniform(0.5, 2.0)

    # ── Position ───────────────────────────────────────────────
    # If there's already a deposit nearby, offset from IT to guarantee
    # cross-cutting (they'll partially overlap). Otherwise use cell centroid
    # with a small jitter.
    if existing_nearby:
        ref = existing_nearby[0].geometry.centroid if existing_nearby[0].geometry else None
        if ref is not None:
            # Small offset from existing deposit — guarantees overlap
            base_lon, base_lat = ref.x, ref.y
            offset_lat = rng.uniform(-0.08, 0.08)
            offset_lon = rng.uniform(-0.08, 0.08)
        else:
            base_lat, base_lon = lat, lon
            offset_lat = rng.uniform(-0.1, 0.1)
            offset_lon = rng.uniform(-0.1, 0.1)
    else:
        base_lat, base_lon = lat, lon
        # Small jitter so adjacent-cell deposits don't align perfectly
        offset_lat = rng.uniform(-0.05, 0.05)
        offset_lon = rng.uniform(-0.05, 0.05)

    center = Point(base_lon + offset_lon, base_lat + offset_lat)

    # ── Shape generation by vein type ──────────────────────────────
    base_radius = max(0.08, (volume ** (1/3)) / 3000)

    if formation.vein_shape == "vein":
        # Elongate ellipse: narrow vein with random strike
        length = base_radius * rng.uniform(3.0, 8.0)
        width = base_radius * rng.uniform(0.3, 0.6)
        angle = rng.uniform(0, 180)
        # Build ellipse via buffer of a line
        dx = length * math.cos(math.radians(angle))
        dy = length * math.sin(math.radians(angle))
        line = LineString([
            (center.x - dx, center.y - dy),
            (center.x + dx, center.y + dy),
        ])
        poly = line.buffer(width)

    elif formation.vein_shape == "massive":
        # Irregular blob: circle with radial noise
        n_pts = rng.randint(10, 16)
        angles = sorted([rng.random() * 2 * math.pi for _ in range(n_pts)])
        radii = [base_radius * rng.uniform(0.6, 1.4) for _ in range(n_pts)]
        pts = [
            (center.x + radii[i] * math.cos(angles[i]),
             center.y + radii[i] * math.sin(angles[i]))
            for i in range(n_pts)
        ]
        if len(pts) >= 3:
            poly = Polygon(pts)
        else:
            poly = center.buffer(base_radius)

    elif formation.vein_shape == "layer":
        # Broad tabular body: large regular ellipse
        a = base_radius * rng.uniform(2.0, 4.0)
        b = base_radius * rng.uniform(1.5, 3.0)
        angle = rng.uniform(0, 90)
        poly = _ellipse(center, a, b, angle)

    elif formation.vein_shape == "pipe":
        # Narrow vertical pipe: small tight circle
        poly = center.buffer(base_radius * rng.uniform(0.3, 0.6))

    elif formation.vein_shape == "lens":
        # Lenticular: moderate ellipse
        a = base_radius * rng.uniform(1.5, 3.0)
        b = base_radius * rng.uniform(0.5, 1.0)
        angle = rng.uniform(0, 180)
        poly = _ellipse(center, a, b, angle)

    elif formation.vein_shape == "scattered":
        # Placer-style: cluster of small pockets
        n_pockets = rng.randint(3, 7)
        pockets = []
        for _ in range(n_pockets):
            px = center.x + rng.uniform(-base_radius, base_radius)
            py = center.y + rng.uniform(-base_radius, base_radius)
            pr = base_radius * rng.uniform(0.15, 0.4)
            pockets.append(Point(px, py).buffer(pr))
        poly = unary_union(pockets) if pockets else center.buffer(base_radius)

    elif formation.vein_shape == "disseminated":
        # Very sparse: many tiny specks
        n_specks = rng.randint(8, 20)
        specks = []
        for _ in range(n_specks):
            sx = center.x + rng.uniform(-base_radius * 2, base_radius * 2)
            sy = center.y + rng.uniform(-base_radius * 2, base_radius * 2)
            sr = base_radius * rng.uniform(0.05, 0.15)
            specks.append(Point(sx, sy).buffer(sr))
        poly = unary_union(specks) if specks else center.buffer(base_radius * 0.5)

    else:
        # Fallback: simple circle
        poly = center.buffer(base_radius)

    return Feature(
        type="ore_deposit",
        name=f"{formation.name}",
        geometry=poly,
        properties={
            "primary_ore": primary,
            "secondary_ores": formation.secondary_ores,
            "gangue": formation.gangue,
            "grade": round(grade, 4),
            "volume_m3": int(volume),
            "depth_top_m": int(d_top),
            "depth_bottom_m": int(d_bot),
            "formation_type": formation.formation_type,
            "vein_shape": formation.vein_shape,
            "density_gcm3": primary_def.density_gcm3 if primary_def else 2.5,
            "value_index": primary_def.value if primary_def else 1,
        },
    )


def _ellipse(center: Point, a: float, b: float, angle_deg: float) -> Polygon:
    """Create an ellipse polygon centered at *center* with semi-axes a, b
    rotated by *angle_deg* degrees."""
    n = 24
    pts = []
    cos_a = math.cos(math.radians(angle_deg))
    sin_a = math.sin(math.radians(angle_deg))
    for i in range(n):
        theta = 2 * math.pi * i / n
        x = a * math.cos(theta)
        y = b * math.sin(theta)
        # Rotate
        rx = center.x + x * cos_a - y * sin_a
        ry = center.y + x * sin_a + y * cos_a
        pts.append((rx, ry))
    return Polygon(pts)


def generate_all_ores(
    cells: List[CellData],
    lithology_map: Dict[str, list],
    tectonics,
    feature_store,
    seed: int = 42,
) -> int:
    """Generate ore deposits for all cells. Returns count placed.

    Multiple formations can trigger on the same cell (cross-cutting veins).
    When a formation places a deposit, there's an elevated chance that
    other qualifying formations also place deposits on the same cell
    with different offsets, shapes, and rotations.
    """
    # Ensure default ores are registered
    if not ORE_REGISTRY:
        _init_default_ores()

    rng = random.Random(seed + 333)
    count = 0

    # Pre-compute qualifying formations per cell
    for cell in cells:
        litho = lithology_map.get(cell.h3_id, [])
        ctx = OreContext(cell, litho, tectonics)

        # Collect ALL formations that match this cell
        qualifying = []
        for key, formation in ORE_REGISTRY.items():
            if ctx.matches(formation):
                qualifying.append((key, formation))

        if not qualifying:
            continue

        ll = h3_to_latlng(cell.h3_id)

        # Track deposits already placed on this cell for cross-cutting
        cell_deposits: List[Feature] = []
        boost = 1.0

        for key, formation in qualifying:
            effective_rarity = min(1.0, formation.rarity * boost)
            if rng.random() < effective_rarity:
                # Secondary veins get volume penalty (later events are smaller)
                vol_scale = 1.0
                if cell_deposits:
                    vol_scale = rng.uniform(0.3, 0.7)
                deposit = generate_ore_deposit(
                    cell.h3_id, ll[0], ll[1], formation, rng,
                    existing_nearby=cell_deposits if cell_deposits else None,
                )
                # Scale volume for cross-cutting veins
                props = dict(deposit.properties)
                props["volume_m3"] = int(props["volume_m3"] * vol_scale)
                deposit.properties = props
                feature_store.add_feature(deposit)
                count += 1
                cell_deposits.append(deposit)
                # After first deposit, further formations on this cell
                # get a big boost (hydrothermal cascade)
                boost = 10.0

    return count


def h3_to_latlng(h3_id: str) -> tuple:
    import h3
    return h3.cell_to_latlng(h3_id)


# ======================================================================
# Public API for custom ore registration
# ======================================================================


def register_ore_type(name: str, formation: OreFormation) -> None:
    """Register a new ore type (incl. fantasy ores via wM).

    Example:
        register_ore_type('fantasy_mithril', OreFormation(
            'Mithril Vein', 'mithril', ['silver', 'platinum'],
            ['granite', 'gneiss', 'diorite'],
            formation_type='magmatic',
            depth_range=(1000, 4000),
            rarity=0.0005,
            grade_range=(0.05, 0.3),
            required_geo_types=[2, 3, 5],
        ))
    """
    ORE_REGISTRY[name] = formation


def get_mineral(name: str) -> Optional[MineralDef]:
    return MINERAL_REGISTRY.get(name)


def register_mineral(name: str, mineral: MineralDef) -> None:
    """Register a new mineral species for fantasy ores."""
    MINERAL_REGISTRY[name] = mineral
