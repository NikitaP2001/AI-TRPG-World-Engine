"""Fauna — continuous population dynamics for one species.

One Fauna instance per registered fauna_species. Each tick:
  1. Sample habitat suitability (terrestrial/aquatic/aerial branching)
  2. Compute demographics (logistic growth, food-adequacy-dependent)
  3. Apply predation (per-prey efficiency from dict, Lotka-Volterra)
  4. Migration redistribution toward higher-suitability cells

Writes population_density[species_id] as a persistent field effect.
"""
from __future__ import annotations

import math
import uuid
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import Feature
from ..fauna_registry import FAUNA_REGISTRY, FaunaSpeciesDef, get_hazard_weight
from ..fields import FieldRegistry

# Sampling resolution (degrees). Same as Vegetation.
_LAT_STEP = 2.0
_LON_STEP = 2.0

# Below this density, species is considered absent.
_EPSILON = 1e-8

# Predation one-step lag fraction.
_PREDATION_DT_FRAC = 0.5


class Fauna(Feature):
    """One species' population dynamics as a Layer 1 feature.

    Supports three habitat types:
      terrestrial — suitability from vegetation canopy + climate + elevation
      aquatic     — water cells only, suitability from water temp + plankton
      aerial      — wide suitability, climate-limited, higher migration
      amphibious  — min(land_suit, water_suit), can use either food source

    Diet is per-prey: diet_sources maps {prey_id: efficiency}.

    One instance per registered species.
    """

    def __init__(
        self,
        species_id: str,
        resolution_deg: float = 2.0,
        feature_id: str = "",
    ):
        if not feature_id:
            feature_id = f"fauna_{species_id}"
        self.species_id = species_id
        self.species_def: FaunaSpeciesDef = FAUNA_REGISTRY[species_id]
        self._resolution = resolution_deg
        self._sample_cache: Dict[Tuple[float, float], float] = {}
        super().__init__(
            feature_id=feature_id,
            name=self.species_def.name,
            geometry=None,
            feature_type="fauna",
            props={"species_id": species_id},
        )

    # ── Sampling grid ──────────────────────────────────────────────

    def _sample_points(self) -> List[Tuple[float, float]]:
        """Generate sampling grid points at configured resolution."""
        points = []
        r = self._resolution
        lat = -90.0 + r / 2
        while lat < 90.0:
            lon_step = r / max(1.0, math.cos(math.radians(lat)))
            lon = -180.0 + lon_step / 2
            while lon < 180.0:
                points.append((lat, lon))
                lon += lon_step
            lat += r
        return points

    # ── Habitat suitability ────────────────────────────────────────

    def _is_water(self, lat: float, lon: float, fields: FieldRegistry) -> bool:
        """Check if point is ocean/water (elevation < 0 or is_ocean flag)."""
        try:
            elev_f = fields.get("elevation_mean")
            elev = elev_f(lat, lon)
            return elev < 0.0
        except Exception:
            return False

    def _suitability(self, lat: float, lon: float, fields: FieldRegistry) -> float:
        """Compute habitat suitability (0.0–1.0). Branches by habitat_type."""
        sp = self.species_def
        cache_key = (round(lat, 2), round(lon, 2))
        if cache_key in self._sample_cache:
            return self._sample_cache[cache_key]

        suit = 1.0

        if sp.habitat_type == "aquatic":
            suit = self._suitability_aquatic(lat, lon, fields)
        elif sp.habitat_type == "aerial":
            suit = self._suitability_aerial(lat, lon, fields)
        elif sp.habitat_type == "amphibious":
            land_s = self._suitability_terrestrial(lat, lon, fields)
            water_s = self._suitability_aquatic(lat, lon, fields)
            suit = max(land_s, water_s) * 0.5 + min(land_s, water_s) * 0.5
        else:
            suit = self._suitability_terrestrial(lat, lon, fields)

        # Apply modifiers (for all habitat types)
        for expr in sp.habitat_suitability_modifiers:
            suit = self._eval_modifier(expr, lat, lon, fields, suit)

        suit = max(0.0, min(1.0, suit))
        self._sample_cache[cache_key] = suit
        return suit

    def _suitability_terrestrial(
        self, lat: float, lon: float, fields: FieldRegistry,
    ) -> float:
        """Terrestrial suitability: vegetation + climate + elevation."""
        sp = self.species_def

        # Water cells are unsuitable for terrestrial species
        if self._is_water(lat, lon, fields):
            return 0.0

        suit = 1.0

        # Soft biome match via canopy density
        canopy_f = fields.get("canopy_density")
        suit *= 0.3 + 0.7 * max(0.0, canopy_f(lat, lon))

        # Climate
        temp_f = fields.get("temperature")
        temp = max(0.0, min(1.0, temp_f(lat, lon)))
        precip_f = fields.get("precipitation")
        precip = max(0.0, min(1.0, precip_f(lat, lon)))

        # Temperature extremes penalty
        if temp < 0.05:
            suit *= max(0.0, temp / 0.05)
        if temp > 0.95:
            suit *= max(0.0, (1.0 - temp) / 0.05)

        # Precipitation extremes penalty
        if precip < 0.02:
            suit *= max(0.0, precip / 0.02)

        return suit

    def _suitability_aquatic(
        self, lat: float, lon: float, fields: FieldRegistry,
    ) -> float:
        """Aquatic suitability: only in water, depends on temp + plankton."""
        if not self._is_water(lat, lon, fields):
            return 0.0

        temp_f = fields.get("temperature")
        temp = max(0.0, min(1.0, temp_f(lat, lon)))

        # Water temperature tolerance (most fish 0.1-0.9 norm range)
        if temp < 0.05 or temp > 0.95:
            return 0.0
        suit = 1.0 - abs(temp - 0.5) * 1.5  # peak at 0.5 norm (~17.5°C)
        suit = max(0.1, suit)

        # Plankton availability bonus
        try:
            plankton_f = fields.get("plankton_density")
            plankton = max(0.0, plankton_f(lat, lon))
            suit *= 0.5 + 0.5 * min(1.0, plankton / 0.1)
        except KeyError:
            pass

        return max(0.0, min(1.0, suit))

    def _suitability_aerial(
        self, lat: float, lon: float, fields: FieldRegistry,
    ) -> float:
        """Aerial suitability: wide, limited only by extreme climate."""
        temp_f = fields.get("temperature")
        temp = max(0.0, min(1.0, temp_f(lat, lon)))
        precip_f = fields.get("precipitation")

        # Aerial species tolerate wider range
        if temp < 0.01 or temp > 0.99:
            return 0.0
        suit = 1.0
        if temp < 0.03:
            suit *= temp / 0.03
        if temp > 0.97:
            suit *= (1.0 - temp) / 0.03

        # Prey availability matters (nesting sites via canopy for arboreal)
        try:
            canopy_f = fields.get("canopy_density")
            suit *= 0.5 + 0.5 * max(0.0, canopy_f(lat, lon))
        except KeyError:
            pass

        return max(0.0, min(1.0, suit))

    def _eval_modifier(
        self, expr: str, lat: float, lon: float,
        fields: FieldRegistry, current_suit: float,
    ) -> float:
        """Evaluate habitat_suitability_modifier expression."""
        try:
            if "->" not in expr:
                return current_suit
            condition, action = expr.split("->", 1)
            condition = condition.strip()
            action = action.strip()

            field_name = ""
            if "L0.cell[" in condition:
                start = condition.index("[") + 1
                end = condition.index("]")
                field_name = condition[start:end]

            field_val = 0.0
            if field_name == "elevation_mean":
                elev_f = fields.get("elevation_mean")
                field_val = elev_f(lat, lon) if elev_f else 0.0
            elif field_name == "temperature":
                t_f = fields.get("temperature")
                field_val = t_f(lat, lon) if t_f else 0.5
            elif field_name == "precipitation":
                p_f = fields.get("precipitation")
                field_val = p_f(lat, lon) if p_f else 0.5
            else:
                try:
                    f_acc = fields.get(field_name)
                    field_val = f_acc(lat, lon) if f_acc else 0.0
                except KeyError:
                    return current_suit

            for op in [">=", "<=", ">", "<", "=="]:
                if op in condition:
                    parts = condition.split(op)
                    threshold_str = parts[-1].strip()
                    try:
                        threshold = float(threshold_str)
                    except ValueError:
                        return current_suit

                    cond_true = False
                    if op == ">=":
                        cond_true = field_val >= threshold
                    elif op == "<=":
                        cond_true = field_val <= threshold
                    elif op == ">":
                        cond_true = field_val > threshold
                    elif op == "<":
                        cond_true = field_val < threshold
                    elif op == "==":
                        cond_true = abs(field_val - threshold) < 1e-6

                    if cond_true:
                        if "*=" in action:
                            factor_str = action.split("*=")[-1].strip()
                            factor = float(factor_str)
                            current_suit *= factor
                        elif "=" in action:
                            val_str = action.split("=")[-1].strip()
                            val = float(val_str)
                            current_suit = val
                    break
        except Exception:
            pass
        return current_suit

    # ── Food availability ──────────────────────────────────────────

    def _food_availability(
        self, lat: float, lon: float, density: float,
        fields: FieldRegistry,
    ) -> float:
        """Compute food adequacy (0.0–2.0). Uses dict-based per-source efficiency.

        diet_sources keys:
          "biomass"        — generic grazing (herbivore generalist)
          species_id       — hunt specific fauna species
          "plankton"       — aquatic filter feeding
          "scavenge"       — carrion/scavenging (from death events)
          flora_pft_id     — specific plant type grazing
        """
        sp = self.species_def
        sources = sp.diet_sources
        if not sources:
            return 1.0

        total_food = 0.0

        for source_id, efficiency in sources.items():
            if source_id == "biomass" and sp.diet in ("herbivore", "omnivore"):
                biomass_f = fields.get("biomass")
                if biomass_f:
                    total_food += biomass_f(lat, lon) * efficiency

            elif source_id == "plankton":
                try:
                    plank_f = fields.get("plankton_density")
                    plank = max(0.0, plank_f(lat, lon))
                    total_food += plank * efficiency
                except KeyError:
                    pass

            elif source_id == "scavenge":
                # Scavenging: depends on local death rate of all species
                # Simplified: proportional to total fauna density
                total_food += density * 0.01 * efficiency

            elif source_id in FAUNA_REGISTRY:
                # Hunt a specific fauna species
                pop_fn = f"population_density[{source_id}]"
                try:
                    prey_f = fields.get(pop_fn)
                    if prey_f:
                        prey_density = max(0.0, prey_f(lat, lon))
                        total_food += prey_density * efficiency
                except KeyError:
                    pass

            else:
                # Try as PFT name (direct biomass grazing)
                biomass_f = fields.get("biomass")
                if biomass_f:
                    total_food += biomass_f(lat, lon) * efficiency * 0.1

        # Add plankton consumption for aquatic species
        if sp.plankton_consumption_rate > 0:
            try:
                plank_f = fields.get("plankton_density")
                plank = max(0.0, plank_f(lat, lon))
                total_food += plank * sp.plankton_consumption_rate
            except KeyError:
                pass

        if total_food <= 0:
            return 0.0

        subsistence = density * 0.1
        if subsistence <= 0:
            return 1.0

        return min(2.0, total_food / subsistence)

    # ── Core tick logic ────────────────────────────────────────────

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Compute population update: demographics + predation + migration."""
        sp = self.species_def
        pop_field_name = f"population_density[{self.species_id}]"

        self._sample_cache.clear()

        try:
            pop_mf = fields.get_mutable(pop_field_name)
        except KeyError:
            return

        points = self._sample_points()

        # ── Phase 1: Density deltas ────────────────────────────────
        deltas: Dict[Tuple[float, float], float] = {}

        for lat, lon in points:
            density = max(0.0, pop_mf(lat, lon))
            if density < _EPSILON:
                deltas[(lat, lon)] = 0.0
                continue

            suit = self._suitability(lat, lon, fields)
            carrying_capacity = sp.population_density_max * suit

            food = self._food_availability(lat, lon, density, fields)

            birth_rate = sp.base_birth * food * (1.0 - density / max(carrying_capacity, _EPSILON))
            death_rate = sp.base_death * (1.0 + max(0.0, 1.0 - food))

            delta = density * (birth_rate - death_rate) * dt
            deltas[(lat, lon)] = delta

        # ── Phase 2: Predation writeback ───────────────────────────
        # Consume prey population_density fields using per-prey efficiency
        for source_id, efficiency in sp.diet_sources.items():
            if source_id == "biomass" or source_id == "plankton" or source_id == "scavenge":
                continue  # not a prey species
            if source_id not in FAUNA_REGISTRY:
                continue  # not a fauna species (maybe PFT name)

            prey_field_name = f"population_density[{source_id}]"
            try:
                prey_mf = fields.get_mutable(prey_field_name)
            except KeyError:
                continue

            for lat, lon in points:
                predator_density = max(0.0, pop_mf(lat, lon))
                if predator_density < _EPSILON:
                    continue
                prey_density = max(0.0, prey_mf(lat, lon))
                if prey_density < _EPSILON:
                    continue

                consumption = predator_density * efficiency * prey_density * _PREDATION_DT_FRAC * dt
                if consumption > 0:
                    prey_mf.add_effect(lat, lon, radius_deg=self._resolution, strength=-consumption)

        # ── Phase 3: Write persistent effects ──────────────────────
        for lat, lon in points:
            delta = deltas.get((lat, lon), 0.0)
            if abs(delta) > _EPSILON:
                pop_mf.add_persistent(lat, lon, radius_deg=self._resolution * 0.6, strength=delta)

        # ── Phase 4: Migration ─────────────────────────────────────
        self._migrate(pop_mf, fields, points, dt)

    def _migrate(
        self,
        pop_mf,
        fields: FieldRegistry,
        points: List[Tuple[float, float]],
        dt: float,
    ) -> None:
        """Redistribute population toward higher-suitability neighbors."""
        sp = self.species_def
        if sp.migration_rate <= 0:
            return

        # Aerial species have wider migration
        mig_rate = sp.migration_rate
        if sp.habitat_type == "aerial":
            mig_rate *= 3.0  # birds cover more ground
        elif sp.habitat_type == "aquatic":
            mig_rate *= 0.3  # fish constrained by water

        suit_map: Dict[Tuple[float, float], float] = {}
        for lat, lon in points:
            suit_map[(lat, lon)] = self._suitability(lat, lon, fields)

        for lat, lon in points:
            density = max(0.0, pop_mf(lat, lon))
            if density < _EPSILON:
                continue

            current_suit = suit_map.get((lat, lon), 0.0)

            # Find best neighbor in 8 directions
            best_suit = current_suit
            best_pt = None
            for dlat, dlon in [
                (self._resolution, 0), (-self._resolution, 0),
                (0, self._resolution), (0, -self._resolution),
                (self._resolution, self._resolution),
                (-self._resolution, self._resolution),
                (self._resolution, -self._resolution),
                (-self._resolution, -self._resolution),
            ]:
                nl = lat + dlat
                if nl > 90:
                    nl = 180 - nl
                elif nl < -90:
                    nl = -180 - nl
                npt = (nl, lon + dlon)
                if npt in suit_map:
                    ns = suit_map[npt]
                    if ns > best_suit:
                        best_suit = ns
                        best_pt = npt

            if best_pt is None or best_suit <= current_suit:
                continue

            delta_suit = best_suit - current_suit
            migrate_frac = min(mig_rate, mig_rate * delta_suit) * dt
            flow = density * migrate_frac

            if flow > _EPSILON:
                pop_mf.add_persistent(lat, lon, radius_deg=self._resolution * 0.6, strength=-flow)
                bl, bn = best_pt
                pop_mf.add_persistent(bl, bn, radius_deg=self._resolution * 0.6, strength=flow)
