"""Layer 0 — Geology and Mineralogy Model.

Mineral composition profiles per bedrock type, assigned from
tectonic plate context (geological_type). Used by the weathering
and soil formation model (Stage 3).

Design doc § Stage 1 (Tectonic Structure) output — geological_type
and mineral composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .cell_model import CellData

# ======================================================================
# Mineral Profile — composition of one bedrock type
# ======================================================================


@dataclass
class MineralProfile:
    """Mineral and chemical composition of a bedrock type.

    All nutrient values are normalized 0.0-1.0 fractions.
    'weatherability' controls how fast the rock breaks down (Goldich series).
    """

    name: str                         # bedrock_class key
    weatherability: float = 0.5       # 0-1, Goldich dissolution series
    nutrient_n: float = 0.0           # Nitrogen content (usually 0 in bedrock)
    nutrient_p: float = 0.02          # Phosphorus content
    nutrient_k: float = 0.03          # Potassium content
    calcium: float = 0.04             # Calcium content
    magnesium: float = 0.02           # Magnesium content
    silica: float = 0.50              # Silica (SiO2) content
    clay_potential: float = 0.3       # Tendency to weather to clay
    ph_initial: float = 7.0           # Initial pH of weathered product
    cation_exchange: float = 5.0      # Base CEC in cmol/kg

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "weatherability": self.weatherability,
            "nutrient_n": self.nutrient_n,
            "nutrient_p": self.nutrient_p,
            "nutrient_k": self.nutrient_k,
            "calcium": self.calcium,
            "magnesium": self.magnesium,
            "silica": self.silica,
            "clay_potential": self.clay_potential,
            "ph_initial": self.ph_initial,
            "cation_exchange": self.cation_exchange,
        }

    @staticmethod
    def from_dict(d: dict) -> "MineralProfile":
        return MineralProfile(
            name=d.get("name", "unknown"),
            weatherability=d.get("weatherability", 0.5),
            nutrient_n=d.get("nutrient_n", 0.0),
            nutrient_p=d.get("nutrient_p", 0.02),
            nutrient_k=d.get("nutrient_k", 0.03),
            calcium=d.get("calcium", 0.04),
            magnesium=d.get("magnesium", 0.02),
            silica=d.get("silica", 0.50),
            clay_potential=d.get("clay_potential", 0.3),
            ph_initial=d.get("ph_initial", 7.0),
            cation_exchange=d.get("cation_exchange", 5.0),
        )


# ======================================================================
# Mineral profile database
# ======================================================================

MINERAL_PROFILES: Dict[str, MineralProfile] = {
    "oceanic_basalt": MineralProfile(
        name="oceanic_basalt",
        weatherability=0.30,     # Basalt weathers moderately (Ca-feldspar)
        nutrient_n=0.0,          # No nitrogen in basalt
        nutrient_p=0.02,         # Low phosphorus (no apatite)
        nutrient_k=0.03,         # Low potassium (K-feldspar rare in MORB)
        calcium=0.08,            # High calcium (Ca-feldspar, pyroxene)
        magnesium=0.06,          # High magnesium (olivine, pyroxene)
        silica=0.48,             # ~48% SiO2 (mafic)
        clay_potential=0.25,     # Weathers to smectite clay
        ph_initial=8.0,          # Alkaline (basalt)
        cation_exchange=15.0,    # Smectite clay = high CEC
    ),
    "continental_granite": MineralProfile(
        name="continental_granite",
        weatherability=0.45,     # Granite weathers moderately
        nutrient_n=0.0,
        nutrient_p=0.05,         # Moderate P (apatite accessory mineral)
        nutrient_k=0.08,         # High K (K-feldspar, biotite)
        calcium=0.02,            # Low calcium (plagioclase)
        magnesium=0.01,          # Low magnesium
        silica=0.70,             # ~70% SiO2 (felsic)
        clay_potential=0.40,     # Weathers to kaolinite
        ph_initial=6.5,          # Slightly acidic
        cation_exchange=8.0,     # Kaolinite = low CEC
    ),
    "orogenic_granite": MineralProfile(
        name="orogenic_granite",
        weatherability=0.50,     # Freshly uplifted = moderate-high
        nutrient_n=0.0,
        nutrient_p=0.06,         # Higher P (more accessory minerals in orogenic)
        nutrient_k=0.07,
        calcium=0.03,
        magnesium=0.02,
        silica=0.65,
        clay_potential=0.35,
        ph_initial=6.5,
        cation_exchange=10.0,
    ),
    "sedimentary": MineralProfile(
        name="sedimentary",
        weatherability=0.70,     # Sedimentary rocks weather fastest
        nutrient_n=0.01,         # Some inherited organics
        nutrient_p=0.07,         # Higher phosphorus
        nutrient_k=0.06,
        calcium=0.06,            # Variable (limestone = high)
        magnesium=0.03,
        silica=0.45,
        clay_potential=0.55,     # Often clay-rich already
        ph_initial=7.2,          # Slightly alkaline (carbonate buffer)
        cation_exchange=18.0,    # Clay-rich = high CEC
    ),
    "cratonic_granite": MineralProfile(
        name="cratonic_granite",
        weatherability=0.20,     # Ancient, already weathered surface
        nutrient_n=0.0,
        nutrient_p=0.01,         # Depleted by ancient weathering
        nutrient_k=0.02,         # Depleted
        calcium=0.01,
        magnesium=0.01,
        silica=0.75,             # Quartz-rich (resistant)
        clay_potential=0.30,
        ph_initial=5.5,          # Acidic (leached)
        cation_exchange=5.0,
    ),
    "basalt": MineralProfile(     # Divergent boundary / hotspot
        name="basalt",
        weatherability=0.35,
        nutrient_n=0.0,
        nutrient_p=0.03,
        nutrient_k=0.04,
        calcium=0.07,
        magnesium=0.05,
        silica=0.50,
        clay_potential=0.30,
        ph_initial=7.5,
        cation_exchange=12.0,
    ),
    "sheared_metamorphic": MineralProfile(
        name="sheared_metamorphic",
        weatherability=0.40,     # Metamorphic = variable
        nutrient_n=0.0,
        nutrient_p=0.03,
        nutrient_k=0.05,
        calcium=0.04,
        magnesium=0.03,
        silica=0.55,
        clay_potential=0.35,
        ph_initial=7.2,
        cation_exchange=10.0,
    ),
}


# ======================================================================
# Helper: lookup mineral profile
# ======================================================================


def get_mineral_profile(bedrock_class: str) -> MineralProfile:
    """Look up mineral profile by bedrock class name.

    Falls back to continental_granite for unknown types.
    """
    return MINERAL_PROFILES.get(bedrock_class, MINERAL_PROFILES["continental_granite"])


# ======================================================================
# Geological type → bedrock class mapping
# ======================================================================

# Maps geological_type int → default bedrock_class key
GEOLOGY_TO_BEDROCK: Dict[int, str] = {
    0: "oceanic_basalt",          # oceanic crust
    1: "sedimentary",             # continental shelf
    2: "sedimentary",             # continental
    3: "orogenic_granite",        # mountain belt
    4: "basalt",                  # rift valley
    5: "cratonic_granite",        # craton
    6: "sheared_metamorphic",     # fault zone
}

# Human-readable names for geological types
GEOLOGY_NAMES: Dict[int, str] = {
    0: "Oceanic Crust",
    1: "Continental Shelf",
    2: "Continental Plain",
    3: "Mountain Belt",
    4: "Rift Valley",
    5: "Craton",
    6: "Fault Zone",
}


def geology_name(gtype: int) -> str:
    """Get human-readable name for geological type."""
    return GEOLOGY_NAMES.get(gtype, "Unknown")


def bedrock_from_geology(geological_type: int) -> str:
    """Map geological_type to bedrock class key."""
    return GEOLOGY_TO_BEDROCK.get(geological_type, "continental_granite")


def assign_bedrock_classes(cells: List[CellData]) -> None:
    """Assign bedrock_class to every cell from its geological_type.

    Called after tectonic geology is established.
    """
    for cell in cells:
        cell.bedrock_class = bedrock_from_geology(cell.geological_type)
