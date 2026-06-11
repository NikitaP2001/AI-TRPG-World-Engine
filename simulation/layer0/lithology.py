"""Lithology — subsurface rock column generation.

Each cell gets a layered column of 6-8 rock types from surface to mantle.
Properties: density, porosity, permeability, hardness, thermal conductivity.
Generated from tectonic context (crust type, age, thickness, thermal gradient).

Registry is extensible — add any rock type including fantasy stone (wM-style).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cell_model import CellData


# ======================================================================
# Rock property registry
# ======================================================================


@dataclass
class RockProps:
    """Physical properties of one rock type.

    All values are in SI-like units for consistency.
    """
    name: str
    density_gcm3: float = 2.7        # bulk density [g/cm³]
    porosity: float = 0.05            # 0-1 fraction
    permeability_darcy: float = 0.01  # Darcy
    hardness_mohs: float = 5.0        # Mohs scale 1-10
    thermal_cond: float = 2.5         # W/(m·K)
    compressive_strength_mpa: float = 100.0  # MPa
    is_aquifer: bool = False          # can store groundwater
    is_aquitard: bool = False         # impedes water flow
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "density": self.density_gcm3,
            "porosity": self.porosity,
            "permeability": self.permeability_darcy,
            "hardness": self.hardness_mohs,
            "thermal_cond": self.thermal_cond,
            "strength": self.compressive_strength_mpa,
            "aquifer": self.is_aquifer,
            "aquitard": self.is_aquitard,
        }


# ── Registry ─────────────────────────────────────────────────────

ROCK_REGISTRY: Dict[str, RockProps] = {
    # Sedimentary
    "sandstone": RockProps(
        name="sandstone", density_gcm3=2.3, porosity=0.20,
        permeability_darcy=0.5, hardness_mohs=3, thermal_cond=1.7,
        compressive_strength_mpa=40, is_aquifer=True,
        description="Clastic sedimentary rock, good aquifer",
    ),
    "shale": RockProps(
        name="shale", density_gcm3=2.4, porosity=0.10,
        permeability_darcy=0.001, hardness_mohs=2.5, thermal_cond=1.5,
        compressive_strength_mpa=30, is_aquitard=True,
        description="Clay-rich sedimentary rock, aquitard",
    ),
    "limestone": RockProps(
        name="limestone", density_gcm3=2.5, porosity=0.15,
        permeability_darcy=0.1, hardness_mohs=3, thermal_cond=2.0,
        compressive_strength_mpa=60, is_aquifer=True,
        description="Carbonate sedimentary rock, karst aquifer",
    ),
    "dolomite": RockProps(
        name="dolomite", density_gcm3=2.6, porosity=0.10,
        permeability_darcy=0.05, hardness_mohs=3.5, thermal_cond=2.2,
        compressive_strength_mpa=70, is_aquifer=True,
        description="Magnesium carbonate rock",
    ),
    "conglomerate": RockProps(
        name="conglomerate", density_gcm3=2.4, porosity=0.12,
        permeability_darcy=0.3, hardness_mohs=3, thermal_cond=1.8,
        compressive_strength_mpa=50, is_aquifer=True,
        description="Coarse clastic rock",
    ),
    "evaporite": RockProps(
        name="evaporite", density_gcm3=2.2, porosity=0.05,
        permeability_darcy=0.001, hardness_mohs=2, thermal_cond=3.0,
        compressive_strength_mpa=20, is_aquitard=True,
        description="Salt/gypsum deposits, aquitard",
    ),

    # Igneous
    "granite": RockProps(
        name="granite", density_gcm3=2.7, porosity=0.01,
        permeability_darcy=0.001, hardness_mohs=6, thermal_cond=3.0,
        compressive_strength_mpa=150, is_aquitard=True,
        description="Felsic intrusive igneous rock",
    ),
    "diorite": RockProps(
        name="diorite", density_gcm3=2.8, porosity=0.008,
        permeability_darcy=0.0005, hardness_mohs=6.5, thermal_cond=2.8,
        compressive_strength_mpa=180,
        description="Intermediate intrusive igneous rock",
    ),
    "gabbro": RockProps(
        name="gabbro", density_gcm3=2.9, porosity=0.005,
        permeability_darcy=0.0001, hardness_mohs=7, thermal_cond=2.5,
        compressive_strength_mpa=200,
        description="Mafic intrusive igneous rock (lower crust)",
    ),
    "basalt": RockProps(
        name="basalt", density_gcm3=2.9, porosity=0.02,
        permeability_darcy=0.01, hardness_mohs=7, thermal_cond=2.0,
        compressive_strength_mpa=250,
        description="Mafic volcanic rock (oceanic crust)",
    ),
    "peridotite": RockProps(
        name="peridotite", density_gcm3=3.3, porosity=0.001,
        permeability_darcy=0.00001, hardness_mohs=7.5, thermal_cond=3.5,
        compressive_strength_mpa=300,
        description="Ultramafic mantle rock",
    ),
    "andesite": RockProps(
        name="andesite", density_gcm3=2.6, porosity=0.05,
        permeability_darcy=0.02, hardness_mohs=6, thermal_cond=1.8,
        compressive_strength_mpa=120,
        description="Intermediate volcanic rock",
    ),
    "rhyolite": RockProps(
        name="rhyolite", density_gcm3=2.4, porosity=0.08,
        permeability_darcy=0.03, hardness_mohs=5.5, thermal_cond=1.5,
        compressive_strength_mpa=100,
        description="Felsic volcanic rock",
    ),

    # Metamorphic
    "gneiss": RockProps(
        name="gneiss", density_gcm3=2.7, porosity=0.005,
        permeability_darcy=0.0005, hardness_mohs=6.5, thermal_cond=2.8,
        compressive_strength_mpa=180,
        description="High-grade metamorphic (granitic composition)",
    ),
    "schist": RockProps(
        name="schist", density_gcm3=2.6, porosity=0.05,
        permeability_darcy=0.02, hardness_mohs=5, thermal_cond=2.0,
        compressive_strength_mpa=80,
        description="Medium-grade metamorphic, foliated",
    ),
    "amphibolite": RockProps(
        name="amphibolite", density_gcm3=2.9, porosity=0.005,
        permeability_darcy=0.0001, hardness_mohs=6.5, thermal_cond=2.5,
        compressive_strength_mpa=200,
        description="Medium-high grade metamorphic (basaltic)",
    ),
    "marble": RockProps(
        name="marble", density_gcm3=2.6, porosity=0.005,
        permeability_darcy=0.001, hardness_mohs=3, thermal_cond=2.5,
        compressive_strength_mpa=100,
        description="Metamorphosed limestone",
    ),
    "quartzite": RockProps(
        name="quartzite", density_gcm3=2.6, porosity=0.003,
        permeability_darcy=0.0001, hardness_mohs=7, thermal_cond=3.5,
        compressive_strength_mpa=250,
        description="Metamorphosed sandstone, extremely hard",
    ),
    "slate": RockProps(
        name="slate", density_gcm3=2.7, porosity=0.01,
        permeability_darcy=0.001, hardness_mohs=5.5, thermal_cond=2.0,
        compressive_strength_mpa=120, is_aquitard=True,
        description="Low-grade metamorphic, excellent aquitard",
    ),
    "migmatite": RockProps(
        name="migmatite", density_gcm3=2.7, porosity=0.003,
        permeability_darcy=0.0001, hardness_mohs=6.5, thermal_cond=2.8,
        compressive_strength_mpa=160,
        description="Partially melted gneiss (deep crust)",
    ),

    # Regolith / surface
    "regolith": RockProps(
        name="regolith", density_gcm3=1.8, porosity=0.35,
        permeability_darcy=1.0, hardness_mohs=1, thermal_cond=0.8,
        compressive_strength_mpa=1, is_aquifer=True,
        description="Weathered surface material, unconsolidated",
    ),
}


# ======================================================================
# Lithological column — one cell's subsurface
# ======================================================================


@dataclass
class Layer:
    """One layer in a lithological column."""
    rock_type: str
    depth_top: float        # meters below surface
    depth_bottom: float
    props: RockProps = field(default_factory=lambda: RockProps(name="unknown"))

    @property
    def thickness(self) -> float:
        return self.depth_bottom - self.depth_top

    @property
    def mid_depth(self) -> float:
        return (self.depth_top + self.depth_bottom) / 2.0

    def to_dict(self) -> dict:
        return {
            "rock_type": self.rock_type,
            "depth_top": self.depth_top,
            "depth_bottom": self.depth_bottom,
        }


# ======================================================================
# Column generation
# ======================================================================


def generate_lithology(
    cell: CellData,
    rng: random.Random,
) -> List[Layer]:
    """Generate the full lithological column for one cell.

    Columns vary by tectonic setting:
      Oceanic:  regolith(0-5m)  basalt(5-2000m)  gabbro(2-5km)  peridotite(5km+)
      Continental: regolith(0-10m)  sediment/granite(10-500m)  diorite(0.5-3km)  gabbro(3-7km)  peridotite(7km+)
      Orogen:  regolith + schist/gneiss/amphibolite
      Rift:    regolith + sediment/basalt + gabbro

    Args:
        cell: CellData with tectonic fields populated.
        rng: Random number generator.

    Returns:
        List of Layer objects from surface to depth.
    """
    layers: List[Layer] = []

    depth_max = 15000.0  # down to 15 km max
    gtype = cell.geological_type
    age = getattr(cell, 'crustal_age_myr', 100.0)
    thick = getattr(cell, 'crustal_thickness_km', 35.0) * 1000.0  # to m
    is_ocean = gtype == 0

    # ── Layer 0: Regolith / Soil (0-10 m) ──
    reg_depth = 2.0 + rng.random() * 8.0
    layers.append(Layer("regolith", 0, reg_depth, ROCK_REGISTRY["regolith"]))

    # ── Layer 1: Upper crust (10 m to ~500 m) ──
    upper_base = 100.0 + rng.random() * 400.0

    if is_ocean:
        # Oceanic: basalt flows + sediments
        layers.append(Layer("basalt", reg_depth, upper_base, ROCK_REGISTRY["basalt"]))
    elif gtype == 3:
        # Mountain belt: metamorphic
        rock = rng.choice(["schist", "gneiss", "amphibolite"])
        layers.append(Layer(rock, reg_depth, upper_base, ROCK_REGISTRY[rock]))
    elif gtype == 4:
        # Rift: volcanics + sediments
        rock = rng.choice(["basalt", "rhyolite", "andesite", "conglomerate"])
        layers.append(Layer(rock, reg_depth, upper_base, ROCK_REGISTRY[rock]))
    elif gtype == 5 or age > 1500:
        # Craton: granite
        layers.append(Layer("granite", reg_depth, upper_base, ROCK_REGISTRY["granite"]))
    else:
        # Continental: mixture
        rock = rng.choice(["granite", "sandstone", "limestone", "diorite"])
        layers.append(Layer(rock, reg_depth, upper_base, ROCK_REGISTRY[rock]))

    # ── Layer 2: Middle crust (~500 m to ~3000 m) ──
    mid_base = min(depth_max, max(upper_base + 1000, 2000 + rng.random() * 2000))

    if is_ocean:
        layers.append(Layer("gabbro", upper_base, mid_base, ROCK_REGISTRY["gabbro"]))
    elif gtype == 3:
        rock = rng.choice(["gneiss", "migmatite", "amphibolite", "diorite"])
        layers.append(Layer(rock, upper_base, mid_base, ROCK_REGISTRY[rock]))
    else:
        rock = rng.choice(["granite", "diorite", "gneiss"])
        layers.append(Layer(rock, upper_base, mid_base, ROCK_REGISTRY[rock]))

    # ── Layer 3: Lower crust (~3000 m to base of crust) ──
    crust_base = min(depth_max, thick)

    if mid_base < crust_base:
        if is_ocean:
            rock = rng.choice(["gabbro", "amphibolite"])
        elif gtype == 3:
            rock = rng.choice(["migmatite", "gabbro", "amphibolite"])
        else:
            rock = rng.choice(["gabbro", "granulite"])
        rock_name = rock if rock in ROCK_REGISTRY else "gabbro"
        layers.append(Layer(rock_name, mid_base, crust_base, ROCK_REGISTRY.get(rock_name, ROCK_REGISTRY["gabbro"])))

    # ── Layer 4: Mantle (below crust) ──
    if crust_base < depth_max:
        layers.append(Layer("peridotite", crust_base, depth_max, ROCK_REGISTRY["peridotite"]))

    return layers


# ======================================================================
# Bulk generation
# ======================================================================


def generate_all_lithology(
    cells: List[CellData],
    seed: int = 42,
) -> Dict[str, List[Layer]]:
    """Generate lithological columns for all cells."""
    rng = random.Random(seed + 111)
    lithology: Dict[str, List[Layer]] = {}
    for cell in cells:
        lithology[cell.h3_id] = generate_lithology(cell, rng)
    return lithology


# ======================================================================
# Lookup
# ======================================================================


def get_rock_props(rock_type: str) -> RockProps:
    """Look up rock properties by name. Falls back to generic defaults."""
    return ROCK_REGISTRY.get(rock_type, RockProps(name=rock_type))


def register_rock_type(name: str, props: RockProps) -> None:
    """Register a new rock type (including fantasy stone)."""
    ROCK_REGISTRY[name] = props


def find_layer_at_depth(layers: List[Layer], depth_m: float) -> Optional[Layer]:
    """Find which layer contains a given depth."""
    for layer in layers:
        if layer.depth_top <= depth_m < layer.depth_bottom:
            return layer
    return None
