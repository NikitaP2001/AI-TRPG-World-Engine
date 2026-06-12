"""Fauna Emergence — proto-faction spawn trigger (R4, L2/L2.5 §13).

When a non-zero-emergence-threshold species' population across a contiguous
region crosses the threshold, a proto-faction (social_complexity=0.3) is
spawned with a named leader entity.

This lives in L1 because the trigger (population crossing a threshold) is
fundamentally an L1 quantity — circular for L2 to watch a faction that
doesn't exist yet.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .fauna_registry import FAUNA_REGISTRY, FaunaSpeciesDef


# Minimum cell density to count as "occupied" for contiguity
_DENSITY_THRESHOLD = 0.1

# Minimum cells in a contiguous region for emergence
_MIN_CONTIGUOUS_CELLS = 10

# Cooldown ticks before same (species_id, region) can re-emerge
_EMERGENCE_COOLDOWN_TICKS = 1000


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    a = max(0.0, min(1.0, a))
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _region_hash(cells: List[str]) -> str:
    """Stable hash from a set of cell IDs."""
    h = hashlib.md5("".join(sorted(cells)).encode())
    return h.hexdigest()[:12]


def _find_contiguous_regions(
    fauna_rows: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Group fauna_populations rows into contiguous regions.

    Uses a simple adjacency heuristic: cells within ~20km of each other
    and sharing the same species_id are considered contiguous.
    """
    # Group by species_id
    by_species: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in fauna_rows:
        density = row.get("density", 0.0)
        if density >= _DENSITY_THRESHOLD:
            by_species[row["species_id"]].append(row)

    regions: List[List[Dict[str, Any]]] = []

    for species_id, rows in by_species.items():
        if len(rows) < _MIN_CONTIGUOUS_CELLS:
            continue

        # Simple greedy clustering: cells within 20km form a region
        # This is O(n²) but n per species is small (contiguous pop regions)
        unassigned = list(rows)
        while unassigned:
            region = [unassigned.pop(0)]
            changed = True
            while changed:
                changed = False
                for r in list(unassigned):
                    for placed in region:
                        d = _haversine_km(
                            placed.get("lat", 0), placed.get("lon", 0),
                            r.get("lat", 0), r.get("lon", 0),
                        )
                        if d < 20.0:
                            region.append(r)
                            unassigned.remove(r)
                            changed = True
                            break
            if len(region) >= _MIN_CONTIGUOUS_CELLS:
                regions.append(region)

    return regions


def check_fauna_emergence(
    fauna_rows: List[Dict[str, Any]],
    current_tick: int = 0,
    existing_factions: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Check emergence conditions for all fauna species.

    Args:
        fauna_rows: List of dicts from fauna_populations table.
        current_tick: Current simulation tick.
        existing_factions: Set of faction_ids already registered. If None,
            assumed empty (all checks pass).

    Returns:
        List of emergence event dicts, each with:
            species_id: str
            region_cells: list of h3_ids
            total_population: float
            faction_id: str (auto-generated)
            template_id: str
            leader_archetype: str
            event_type: "proto_faction_emergence"
    """
    events: List[Dict[str, Any]] = []
    existing = existing_factions or set()

    regions = _find_contiguous_regions(fauna_rows)

    for region in regions:
        species_id = region[0]["species_id"]
        sp = FAUNA_REGISTRY.get(species_id)
        if sp is None:
            continue
        if sp.emergence_population_threshold <= 0:
            continue
        if not sp.social_complexity_template:
            continue

        total_pop = sum(r.get("density", 0.0) for r in region)
        if total_pop < sp.emergence_population_threshold:
            continue

        cell_ids = [r["h3_id"] for r in region if r.get("h3_id")]
        rhash = _region_hash(cell_ids)
        faction_id = f"{species_id}_tribe_{rhash}"

        if faction_id in existing:
            continue  # already a faction for this region

        events.append({
            "species_id": species_id,
            "region_cells": cell_ids,
            "total_population": total_pop,
            "faction_id": faction_id,
            "faction_name": f"{sp.name} Tribe",
            "template_id": sp.social_complexity_template,
            "leader_archetype": sp.emergence_leader_archetype,
            "event_type": "proto_faction_emergence",
            "tick": current_tick,
        })

    return events
