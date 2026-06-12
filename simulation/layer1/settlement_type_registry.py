"""Settlement Type Registry — footprint coefficients for R19.

Each settlement_type registered via define_world_concept(concept_type="settlement_type")
provides the coefficients for how a settlement modifies surrounding L1 fields.

No defaults pre-registered — every world's settlement types are WM-defined.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SettlementTypeDef:
    """Footprint coefficients for a settlement category (R19).

    Applied per L1 step by SettlementFootprint for every
    define_faction.settlements[] entry with matching settlement_type,
    scaled by population_share and settlement_tier.
    """

    # ── Identity ─────────────────────────────────────────────────────
    name: str

    # ── Footprint coefficients ───────────────────────────────────────
    # Most are 0 for settlement_types that don't produce that footprint.
    # e.g. lich_tower: near-zero everything except hazard_modifier

    deforestation_factor: float = 0.0
    # -canopy_density / -biomass per tick within control_radius

    hunting_factor: float = 0.0
    # -population_density[huntable species] per tick within control_radius

    soil_modification_factor: float = 0.0
    # +/- soil_fertility within farmland_ring (smaller than control_radius).
    # Positive for cultivating types, negative for extractive ones.

    water_table_factor: float = 0.0
    # +/- water_table_depth within control_radius.
    # Negative for mining/tunneling (drains water table).

    hazard_modifier: float = 0.0
    # +/- hazard_level within control_radius.
    # Positive for "unnatural" types (lich_tower).
    # Slightly negative for garrisoned fortresses (clears hazards).

    population_suppression_factor: float = 0.0
    # -population_density[ALL species] — repels wildlife regardless of hunting.
    # For undead, extreme magical activity.

    ambient_material_extraction: List[str] = field(default_factory=list)
    # material_ids (from set_world_orientation ambient_rare_materials)
    # that this settlement_type depletes locally.

    recovery_rate_modifier: float = 1.0
    # Multiplier on how fast abandoned footprints recover.
    # water_table_factor effects typically have near-0 recovery regardless.

    # ── Internal ─────────────────────────────────────────────────────
    farmland_radius_fraction: float = 0.3
    # Farmland ring = control_radius × this fraction. Default 0.3.

    description: str = ""


# ── Registry ───────────────────────────────────────────────────────

SETTLEMENT_TYPE_REGISTRY: Dict[str, SettlementTypeDef] = {}


def register_settlement_type(type_id: str, defn: SettlementTypeDef) -> None:
    SETTLEMENT_TYPE_REGISTRY[type_id] = defn


def get_settlement_type_ids() -> List[str]:
    return list(SETTLEMENT_TYPE_REGISTRY.keys())
