"""Fauna — continuous population dynamics for one species.

Optimized: static grid KDTree (Option A), density-compressed loops (Option B),
flat-array migration (Option C).
"""
from __future__ import annotations

import math
import uuid
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import Feature
from ..fauna_registry import FAUNA_REGISTRY, FaunaSpeciesDef, get_hazard_weight
from ..fields import FieldRegistry, ContinuousField

# ── Grid constants ─────────────────────────────────────────────────
_LAT_STEP = 2.0
_LON_STEP = 2.0
_NLATS = 90   # 180 / 2
_NLONS = 180  # 360 / 2
_GRID_SIZE = _NLATS * _NLONS  # 16200

_EPSILON = 1e-8
_PREDATION_DT_FRAC = 0.5            # kept for backward compat
_HANDLING_TIME = 0.3                # Holling Type II: handling time per prey

# ── Allometric food need by size class ────────────────────────────────
# Smaller animals need more food relative to body mass (Kleiber's law).
# Per-capita food requirement as fraction of density.
_SIZE_FOOD_NEED = {
    "micro": 0.5,      # bacteria / plankton
    "small": 0.3,      # insects, mice, small birds
    "medium": 0.15,    # rabbits, foxes, medium fish
    "large": 0.08,     # deer, wolves, large fish
    "huge": 0.04,      # moose, bears, cattle
    "legendary": 0.02, # elephants, whales
}

# ── Integer population mode ──────────────────────────────────────────
# When True, densities are rounded to whole individuals (stochastic
# rounding) after each tick.  This prevents ghost populations
# (e.g. 0.02 deer in ocean), adds realistic demographic stochasticity,
# and makes small populations countable.  Large populations (>>100)
# are unaffected.
_USE_INTEGER_POPULATIONS = True

# Demographic time-step: rates are calibrated for one call = one month.
# Do NOT multiply by engine's dt (which may be 30 for 30-day advances).
_DT = 1.0

def _stochastic_round(arr: np.ndarray) -> np.ndarray:
    """Round to nearest integer with stochastic tie-breaking.

    For each element x: floor(x) with probability 1 - frac(x),
    ceil(x) with probability frac(x).  Preserves expected value.
    """
    floor = np.floor(arr)
    frac = arr - floor
    return np.where(np.random.random(arr.shape) < frac, floor + 1.0, floor)

# ── Static grid: built ONCE, shared by all species every tick ─────
# No KVTree rebuild, no sin/cos recomputation.
_GRID_LATS: Optional[np.ndarray] = None
_GRID_LONS: Optional[np.ndarray] = None
_GRID_PTS_3D: Optional[np.ndarray] = None   # (16200, 3) spherical xyz
_GRID_TREE: Optional["cKDTree"] = None       # static KDTree from _GRID_PTS_3D

def _ensure_grid() -> None:
    """Build static sampling grid once."""
    global _GRID_LATS, _GRID_LONS, _GRID_PTS_3D, _GRID_TREE
    if _GRID_LATS is not None:
        return
    r = _LAT_STEP
    _GRID_LATS = np.arange(-90.0 + r / 2, 90.0, r)    # (90,)
    _GRID_LONS = np.arange(-180.0 + r / 2, 180.0, r)   # (180,)
    lat_r = np.radians(_GRID_LATS)[:, np.newaxis]       # (90, 1)
    lon_r = np.radians(_GRID_LONS)[np.newaxis, :]       # (1, 180)
    cos_lat = np.cos(lat_r)
    _GRID_PTS_3D = np.dstack([
        cos_lat * np.cos(lon_r),
        np.sin(lat_r) * np.ones((1, _NLONS)),
        cos_lat * np.sin(lon_r),
    ]).reshape(-1, 3)  # (16200, 3)
    from scipy.spatial import cKDTree
    _GRID_TREE = cKDTree(_GRID_PTS_3D)

def _grid_ij(lat: float, lon: float) -> Tuple[int, int]:
    """(lat, lon) → (i, j) index into grid arrays, clamped."""
    i = int((lat + 90.0 - _LAT_STEP / 2) / _LAT_STEP + 0.5)
    j = int((lon + 180.0 - _LAT_STEP / 2) / _LAT_STEP + 0.5)
    return max(0, min(_NLATS - 1, i)), max(0, min(_NLONS - 1, j))

# ── Shared field grid (rebuilt each tick) ──────────────────────────
_FIELD_GRID_DATA: Dict[str, np.ndarray] = {}

def _build_field_grid(fields: FieldRegistry) -> None:
    """Sample fields at static grid → numpy arrays.

    For base-only fields (elevation, temperature): fast KDTree path.
    For MutableFields (canopy, PFTs): evaluate at each grid point
    to capture persistent effects written by features.
    """
    global _FIELD_GRID_DATA
    _ensure_grid()
    _FIELD_GRID_DATA.clear()
    _FNAMES = ["elevation_mean", "temperature", "precipitation", "water_depth",
               "canopy_density", "plankton_density"]
    # Add PFT biomass fields from the FieldRegistry
    for fname in sorted(fields._mutable_fields.keys()):
        if fname.startswith("pft_biomass[") and fname not in _FNAMES:
            _FNAMES.append(fname)

    for fname in _FNAMES:
        try:
            fa = fields.get(fname)
        except KeyError:
            _FIELD_GRID_DATA[fname] = np.zeros((_NLATS, _NLONS), dtype=np.float64)
            continue
        try:
            base = fa._base
            mutable = fa._mutable
            if base is not None and hasattr(base, '_tree') and hasattr(base, '_values') and mutable is None:
                # Base-only field — fast KDTree path
                _, idxs = base._tree.query(_GRID_PTS_3D, k=1)
                arr = base._values[idxs.ravel()].astype(np.float64)
            elif mutable is not None:
                # MutableField — check for persistent effects
                if mutable._persistent:
                    # For PFT fields, persistent has the COMPLETE state
                    # (Vegetation writes full new_biomass, not a delta).
                    # Read persistent_only to avoid double-count with _base.
                    arr = np.zeros((_NLATS, _NLONS), dtype=np.float64)
                    for i in range(_NLATS):
                        lat = float(_GRID_LATS[i])
                        for j in range(_NLONS):
                            lon = float(_GRID_LONS[j])
                            arr[i, j] = float(mutable.persistent_only(lat, lon))
                else:
                    # No persistent — read full evaluation (base + effects)
                    arr = np.zeros((_NLATS, _NLONS), dtype=np.float64)
                    for i in range(_NLATS):
                        lat = float(_GRID_LATS[i])
                        for j in range(_NLONS):
                            lon = float(_GRID_LONS[j])
                            arr[i, j] = float(fa(lat, lon))
            else:
                arr = np.zeros(_GRID_SIZE, dtype=np.float64)
        except Exception:
            arr = np.zeros(_GRID_SIZE, dtype=np.float64)
        _FIELD_GRID_DATA[fname] = arr.reshape(_NLATS, _NLONS)

    # Compute total PFT biomass for veg_cover (sum of all PFTs)
    pft_total = np.zeros((_NLATS, _NLONS), dtype=np.float64)
    for key, grid in _FIELD_GRID_DATA.items():
        if key.startswith("pft_biomass["):
            pft_total += np.maximum(0.0, grid)
    _FIELD_GRID_DATA["pft_biomass_total"] = pft_total

def _grid_value(field: str, lat: float, lon: float, default: float = 0.0) -> float:
    """O(1) field lookup from pre-sampled grid."""
    data = _FIELD_GRID_DATA.get(field)
    if data is None:
        return default
    i, j = _grid_ij(lat, lon)
    return float(data[i, j])

def _set_field_grid_data(name: str, data: np.ndarray) -> None:
    """Inject a pre-computed grid (e.g., water_depth from _run_l1_step)."""
    _FIELD_GRID_DATA[name] = data

def _clear_env_cache() -> None:
    """Clear field grid (called at start of each tick)."""
    global _FIELD_GRID_DATA
    _FIELD_GRID_DATA.clear()


class Fauna(Feature):
    """One species' population dynamics.

    Uses a flat (NLATS × NLONS) density array updated in-place each tick.
    Static KDTree grid — no tree rebuilds. Density-compressed loops.
    Flat-array migration with index arithmetic.
    """

    def __init__(self, species_id: str, resolution_deg: float = 4.0, feature_id: str = ""):
        if not feature_id:
            feature_id = f"fauna_{species_id}"
        self.species_id = species_id
        self.species_def: FaunaSpeciesDef = FAUNA_REGISTRY[species_id]
        self._resolution = resolution_deg
        self._sample_cache: Dict[Tuple[float, float], float] = {}
        # Flat density array (replaces dict-based new_density_map)
        self._density = np.zeros((_NLATS, _NLONS), dtype=np.float64)
        # Pre-computed suitability (cached from Phase 1, reused in migration)
        self._suitability_grid = np.zeros((_NLATS, _NLONS), dtype=np.float64)
        # Starvation debt (accumulates when food < 1, decays when food > 1)
        self._starvation_debt = np.zeros((_NLATS, _NLONS), dtype=np.float64)
        _ensure_grid()
        super().__init__(
            feature_id=feature_id,
            name=self.species_def.name,
            geometry=None,
            feature_type="fauna",
            props={"species_id": species_id},
        )

    # ── Flat-array helpers ─────────────────────────────────────────

    def _load_initial_density(self, pop_mf) -> None:
        """Read initial density from MutableField into self._density."""
        for i in range(_NLATS):
            lat = float(_GRID_LATS[i])
            for j in range(_NLONS):
                lon = float(_GRID_LONS[j])
                self._density[i, j] = max(0.0, _grid_value("temperature", lat, lon))
        # Reset — the real loading happens via pop_mf._base
        self._density.fill(0.0)
        if pop_mf._base is not None:
            _, idxs = pop_mf._base._tree.query(_GRID_PTS_3D, k=1)
            vals = pop_mf._base._values[idxs.ravel()]
            self._density.flat[:] = np.maximum(0.0, vals)

    def _grid_iter_active(self):
        """Yield (i, j, lat, lon) for grid cells with density > _EPSILON."""
        ii, jj = np.where(self._density > _EPSILON)
        for i, j in zip(ii, jj):
            yield i, j, float(_GRID_LATS[i]), float(_GRID_LONS[j])

    def _grid_at(self, i: int, j: int) -> float:
        """Clamped grid access — returns density at (i,j)."""
        return self._density[max(0, min(_NLATS-1, i)), max(0, min(_NLONS-1, j))]

    # ── Sampling grid (kept for backward compat, returns (lat, lon) pairs) ──

    def _sample_points(self) -> List[Tuple[float, float]]:
        """Generate sampling grid at 2° resolution (~16K points)."""
        pts: List[Tuple[float, float]] = []
        r = self._resolution
        lat = -90.0 + r / 2
        while lat < 90.0:
            lon = -180.0 + r / 2
            while lon < 180.0:
                pts.append((lat, lon))
                lon += r
            lat += r
        return pts

    # ── Habitat suitability ────────────────────────────────────────

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

    @staticmethod
    def _fg(field: str, lat: float, lon: float, default: float = 0.0) -> float:
        """Read field from pre‑sampled grid (O(1)), fallback to 0."""
        return _grid_value(field, lat, lon, default)

    @staticmethod
    def _fg_mf(pop_mf, lat: float, lon: float) -> float:
        """Read mutable field at (lat, lon) — still KDTree but only per‑species field."""
        return max(0.0, pop_mf(lat, lon))

    @staticmethod
    def _starvation_multiplier(food: float) -> float:
        """Starvation effect on death rate — gradual.

        food=0.0 → ×2.0 death (starving, but not instant death)
        food=0.5 → ×1.5 death
        food=1.0 → ×1.0 death (maintenance)
        food≥2.0 → ×0.5 death (well-fed, reduced natural death)
        """
        if food >= 1.0:
            return 1.0 - 0.5 * min(1.0, (food - 1.0) / 1.0)
        return 1.0 + (1.0 - food)

    def _is_water(self, lat: float, lon: float, fields: FieldRegistry) -> bool:
        """Check if point is under water (depth > 0 from water_depth grid)."""
        wd = _FIELD_GRID_DATA.get("water_depth")
        if wd is not None:
            i, j = _grid_ij(lat, lon)
            return float(wd[i, j]) > 0.01
        return False

    # ── Vectorised suitability (full grid in one shot) ────────────

    def _compute_suitability_grid(self) -> np.ndarray:
        """Fill self._suitability_grid for ALL cells — fully vectorised."""
        sp = self.species_def
        elev = _FIELD_GRID_DATA.get("elevation_mean")
        temp = _FIELD_GRID_DATA.get("temperature")
        precip = _FIELD_GRID_DATA.get("precipitation")
        canopy = _FIELD_GRID_DATA.get("canopy_density")
        plankton = _FIELD_GRID_DATA.get("plankton_density")
        pft_total = _FIELD_GRID_DATA.get("pft_biomass_total")
        water_depth = _FIELD_GRID_DATA.get("water_depth")

        if sp.habitat_type == "aquatic":
            suit = self._suit_grid_aquatic(temp, plankton, water_depth)
        elif sp.habitat_type == "aerial":
            suit = self._suit_grid_aerial(temp, canopy, elev)
        elif sp.habitat_type == "amphibious":
            land_s = self._suit_grid_terrestrial(elev, temp, precip, canopy, pft_total, water_depth)
            water_s = self._suit_grid_aquatic(temp, plankton, water_depth)
            suit = np.maximum(land_s, water_s) * 0.5 + np.minimum(land_s, water_s) * 0.5
        else:
            suit = self._suit_grid_terrestrial(elev, temp, precip, canopy, pft_total, water_depth)
        self._suitability_grid = np.clip(suit, 0.0, 1.0)
        return self._suitability_grid

    def _suit_grid_terrestrial(self, elev, temp, precip, canopy, pft_total=None,
                                water_depth=None) -> np.ndarray:
        """Vectorised terrestrial suitability — vegetation + water depth.

        Suitability = veg_cover × water_penalty, where water_penalty drops
        exponentially with water depth beyond the species' wading limit.
        Ocean cells (deep water) automatically get suit → 0.
        """
        sp = self.species_def
        if elev is None or temp is None:
            return np.zeros((_NLATS, _NLONS), dtype=np.float64)
        t = np.clip(temp, 0.0, 1.0)
        p = np.clip(precip, 0.0, 1.0)
        c = np.maximum(0.0, canopy) if canopy is not None else 0.0
        # Vegetation cover: trees + herbaceous (from total PFT biomass)
        if pft_total is not None:
            b_norm = np.minimum(1.0, np.maximum(0.0, pft_total) / 0.5)
        else:
            b_norm = c  # fallback
        veg_cover = np.maximum(c, b_norm)
        suit = veg_cover.copy()

        # Water depth penalty: exponential drop beyond wading depth
        if water_depth is not None:
            wading_max = sp.wading_depth_max
            penalty = np.where(water_depth > wading_max,
                               np.exp(-(water_depth - wading_max) / max(wading_max, 0.001)),
                               1.0)
            # Deep water (ocean) → suit = 0
            penalty = np.where(water_depth > 0.5, 0.0, penalty)
            suit *= penalty
        else:
            # Fallback: elevation-based (original logic)
            suit = np.where(elev < 0.0, 0.0, veg_cover)

        # Temperature suitability — species-specific tolerances
        sp_tmin = sp.temp_min
        sp_topt_low = sp.temp_opt_low
        sp_topt_max = sp.temp_opt_max
        sp_tmax = sp.temp_max
        below_opt = np.where(t < sp_topt_low,
                             np.maximum(0.0, (t - sp_tmin) / (sp_topt_low - sp_tmin + 1e-10)),
                             1.0)
        above_opt = np.where(t > sp_topt_max,
                             np.maximum(0.0, (sp_tmax - t) / (sp_tmax - sp_topt_max + 1e-10)),
                             1.0)
        suit = suit * np.minimum(below_opt, above_opt)
        # Precipitation extremes — too little OR too much
        suit = np.where(p < 0.02, suit * np.maximum(0.0, p / 0.02), suit)
        suit = np.where(p > 0.95, suit * np.maximum(0.0, (1.0 - p) / 0.05), suit)
        # Elevation penalty — O2 partial pressure (barometric formula)
        el = np.clip(elev, -0.5, 1.5)
        # High elevation penalty: barometric O2 decline, scale height ~8.5 km
        high_mask = el > 0.15
        h_km = np.maximum(0.0, el - 0.15) * 8.0
        o2_factor = np.exp(-h_km / 8.5)
        suit = np.where(high_mask, suit * o2_factor, suit)
        return suit

    def _suit_grid_aquatic(self, temp, plankton, water_depth=None) -> np.ndarray:
        """Vectorised aquatic suitability — needs water for fish."""
        if temp is None:
            return np.zeros((_NLATS, _NLONS), dtype=np.float64)
        t = np.clip(temp, 0.0, 1.0)
        if water_depth is not None:
            has_water = (water_depth > 0.01)
        else:
            has_water = np.ones((_NLATS, _NLONS), dtype=bool)
        suit = np.where(has_water & (t >= 0.05) & (t <= 0.95),
                        1.0 - np.abs(t - 0.5) * 1.5, 0.0)
        suit = np.maximum(0.1, suit)
        if plankton is not None:
            pl = np.maximum(0.0, plankton)
            suit *= 0.5 + 0.5 * np.minimum(1.0, pl / 0.1)
        return np.clip(suit, 0.0, 1.0)

    def _suit_grid_aerial(self, temp, canopy, elev=None) -> np.ndarray:
        """Vectorised aerial suitability with elevation limit (thin air, cold)."""
        if temp is None:
            return np.ones((_NLATS, _NLONS), dtype=np.float64) * 0.5
        t = np.clip(temp, 0.0, 1.0)
        suit = np.where((t < 0.01) | (t > 0.99), 0.0, 1.0)
        suit = np.where(t < 0.03, suit * t / 0.03, suit)
        suit = np.where(t > 0.97, suit * (1.0 - t) / 0.03, suit)
        if canopy is not None:
            c = np.maximum(0.0, canopy)
            suit *= 0.5 + 0.5 * c
        # Elevation penalty — air density (lift) follows barometric formula
        if elev is not None:
            el = np.clip(elev, -0.5, 1.5)
            high_mask = el > 0.3
            h_km = np.maximum(0.0, el - 0.3) * 8.0
            rho_factor = np.exp(-h_km / 8.5)  # air density scale height
            suit = np.where(high_mask, suit * rho_factor, suit)
        return np.clip(suit, 0.0, 1.0)

    # ── Scalar suitability (kept for backward compat) ─────────────

    def _suitability_terrestrial(self, lat, lon, fields):
        return float(self._suit_grid_terrestrial(
            _FIELD_GRID_DATA.get("elevation_mean"),
            _FIELD_GRID_DATA.get("temperature"),
            _FIELD_GRID_DATA.get("precipitation"),
            _FIELD_GRID_DATA.get("canopy_density"),
            _FIELD_GRID_DATA.get("pft_biomass_total"),
            _FIELD_GRID_DATA.get("water_depth"),
        )[_grid_ij(lat, lon)])

    def _suitability_aquatic(self, lat, lon, fields):
        return float(self._suit_grid_aquatic(
            _FIELD_GRID_DATA.get("temperature"),
            _FIELD_GRID_DATA.get("plankton_density"),
            _FIELD_GRID_DATA.get("water_depth"),
        )[_grid_ij(lat, lon)])

    def _suitability_aerial(self, lat, lon, fields):
        return float(self._suit_grid_aerial(
            _FIELD_GRID_DATA.get("temperature"),
            _FIELD_GRID_DATA.get("canopy_density"),
            _FIELD_GRID_DATA.get("elevation_mean"),
        )[_grid_ij(lat, lon)])

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

    # ── Food availability (vectorised grid) ────────────────────────

    def _compute_food_grid(self, density: np.ndarray, fields: FieldRegistry) -> np.ndarray:
        """Compute food adequacy grid — PFT-specific + plankton + predation.

        PFT names in diet_sources (e.g. "grass_temperate") are read from
        pft_biomass[*] grids.  Predation (species IDs) read from population
        density fields.  Special keys: "plankton", "scavenge".
        """
        sp = self.species_def
        sources = sp.diet_sources
        food = np.zeros((_NLATS, _NLONS), dtype=np.float64)

        for source_id, efficiency in sources.items():
            if source_id == "plankton":
                pl = _FIELD_GRID_DATA.get("plankton_density")
                if pl is not None:
                    food += np.maximum(0.0, pl) * efficiency

            elif source_id == "scavenge":
                food += density * 0.01 * efficiency

            elif source_id in FAUNA_REGISTRY:
                # Predation — read prey's density array
                prey_fn = f"population_density[{source_id}]"
                try:
                    prey_mf = fields.get_mutable(prey_fn)
                except KeyError:
                    continue
                prey_arr = self._read_prey_density(prey_mf)
                food += np.maximum(0.0, prey_arr) * efficiency

            else:
                # PFT-specific food source: read from pft_biomass[source_id] grid
                pft_grid = _FIELD_GRID_DATA.get(f"pft_biomass[{source_id}]")
                if pft_grid is not None:
                    food += np.maximum(0.0, pft_grid) * efficiency

        # Plankton consumption for aquatic species
        if sp.plankton_consumption_rate > 0:
            pl = _FIELD_GRID_DATA.get("plankton_density")
            if pl is not None:
                food += np.maximum(0.0, pl) * sp.plankton_consumption_rate

        # Food adequacy: allometric scaling by size class
        per_capita_need = _SIZE_FOOD_NEED.get(sp.size_class, 0.15)
        subsist = np.maximum(density * per_capita_need, 1e-10)
        adequacy = np.where(food > 0, np.minimum(2.0, food / subsist), 0.0)
        return adequacy

    # ── Core tick logic ────────────────────────────────────────────

    def compute_effects(self, fields: FieldRegistry, dt: float = 1.0) -> None:
        """Compute population update: demographics + predation + migration.

        Optimized: flat-array density (A), density-compressed loops (B),
        index-arithmetic migration (C).
        """
        if not _FIELD_GRID_DATA:
            _build_field_grid(fields)

        sp = self.species_def
        pop_field_name = f"population_density[{self.species_id}]"
        self._sample_cache.clear()

        try:
            pop_mf = fields.get_mutable(pop_field_name)
        except KeyError:
            return

        # Load current density into self._density (flat array) from _base
        self._load_density_from_base(pop_mf)
        d = self._density  # alias

        # ── Phase 1: Suitability + Food + Demographics (ALL vectorised) ────
        suit_grid = self._compute_suitability_grid()
        food_grid = self._compute_food_grid(d, fields)

        # Carrying capacity
        cc = sp.population_density_max * suit_grid

        # Logistic birth: base_birth * food * max(0, 1 - N/K)
        # Starvation death: accumulated debt, not instant spike
        _DECAY_RATE = 0.3
        _MAX_DEBT = 3.0
        food_deficit = np.maximum(0.0, 1.0 - food_grid)
        # Debt accumulates when food<1, decays exponentially when food>1
        decay = _DECAY_RATE * (food_grid - 1.0) * (food_grid > 1.0)
        self._starvation_debt = np.clip(
            self._starvation_debt + (food_deficit * 0.5 - decay) * _DT,
            0.0, _MAX_DEBT,
        )
        starvation = 1.0 + self._starvation_debt * 0.5
        food_cap = np.minimum(food_grid, 2.0)
        birth_rate = sp.base_birth * food_cap * np.maximum(0.0, 1.0 - d / np.maximum(cc, _EPSILON))
        death_rate = sp.base_death * starvation

        delta = d * (birth_rate - death_rate) * _DT
        d[:] = np.maximum(0.0, d + delta)
        if _USE_INTEGER_POPULATIONS:
            d[:] = _stochastic_round(d)

        # ── Phase 2: Predation (vectorised: prey mass consumption) ─────────
        for source_id, efficiency in sp.diet_sources.items():
            if source_id in ("plankton", "scavenge") or source_id not in FAUNA_REGISTRY:
                continue
            prey_fn = f"population_density[{source_id}]"
            try:
                prey_mf = fields.get_mutable(prey_fn)
            except KeyError:
                continue
            prey_arr = self._read_prey_density(prey_mf)
            # Holling Type II functional response: saturates at 1/_HANDLING_TIME
            attack = efficiency * 2.0
            holling = (attack * prey_arr) / (1.0 + attack * _HANDLING_TIME * prey_arr)
            consumption = d * holling * _DT
            # Only apply where both predator and prey exist
            mask = (d > _EPSILON) & (prey_arr > _EPSILON)
            reduction = np.where(mask, np.minimum(consumption, prey_arr), 0.0)
            if _USE_INTEGER_POPULATIONS:
                reduction = _stochastic_round(reduction)
            prey_arr[mask] = np.maximum(0.0, prey_arr[mask] - reduction[mask])

        # ── Phase 4: Migration (flat-array, index arithmetic) ──────────────
        self._migrate_flat(sp, dt)

        # ── Phase 5: Write _density back to _base (static KDTree, no rebuild) ──
        pop_mf.clear_persistent()
        if _USE_INTEGER_POPULATIONS:
            self._density[:] = _stochastic_round(self._density)
        if pop_mf._base is not None:
            pop_mf._base._values = self._density.ravel().copy()
        else:
            # First init: build ContinuousField with static tree + density values
            from ..fields import ContinuousField
            pop_mf._base = ContinuousField(_GRID_TREE, self._density.ravel().copy())

    def _load_density_from_base(self, pop_mf) -> None:
        """Load density from _base → self._density, then kill fauna in deep water."""
        self._density.fill(0.0)
        if pop_mf._base is not None and pop_mf._base._tree is not None:
            if pop_mf._base._tree is _GRID_TREE or (
                hasattr(pop_mf._base, '_values') and
                len(pop_mf._base._values) == _GRID_SIZE
            ):
                self._density.flat[:] = np.maximum(0.0, pop_mf._base._values)
            else:
                _, idxs = pop_mf._base._tree.query(_GRID_PTS_3D, k=1)
                self._density.flat[:] = np.maximum(0.0, pop_mf._base._values[idxs.ravel()])
        # Kill terrestrial animals in deep water (drowning, not slow starvation)
        wdepth = _FIELD_GRID_DATA.get("water_depth")
        if wdepth is not None:
            sp = self.species_def
            if sp.habitat_type == "terrestrial" and np.any(wdepth > sp.wading_depth_max):
                self._density[wdepth > sp.wading_depth_max] = 0.0

    @staticmethod
    def _read_prey_density(prey_mf) -> np.ndarray:
        """Read prey's density as flat (NLATS, NLONS) array."""
        if prey_mf._base is not None and hasattr(prey_mf._base, '_values'):
            vals = prey_mf._base._values
            if len(vals) == _GRID_SIZE:
                return vals.reshape(_NLATS, _NLONS)
        return np.zeros((_NLATS, _NLONS), dtype=np.float64)

    def _migrate_flat(self, sp: FaunaSpeciesDef, dt: float) -> None:
        """Flat-array migration — index arithmetic, no tuple dict."""
        if sp.migration_rate <= 0:
            return
        # Migration speed by habitat: birds fast, aquatic slower, amphibious intermediate
        _HABITAT_MULT = {
            "terrestrial": 1.0,
            "aerial": 3.0,       # birds: 3x range, reasonable
            "aquatic": 0.5,      # fish: drag in 3D medium
            "amphibious": 0.8,   # both land & water capability
        }
        mig_rate = sp.migration_rate * _HABITAT_MULT.get(sp.habitat_type, 1.0)

        # Iterate all cells (migration needs empty cells too for receiving)
        for i in range(_NLATS):
            for j in range(_NLONS):
                density = float(self._density[i, j])
                if density < _EPSILON:
                    continue

                current_suit = float(self._suitability_grid[i, j])
                best_suit = current_suit
                bi, bj = i, j

                # Check 8 neighbors via index arithmetic
                wdepth = _FIELD_GRID_DATA.get("water_depth")
                for di, dj in [(-1,0), (1,0), (0,-1), (0,1), (-1,-1), (-1,1), (1,-1), (1,1)]:
                    ni, nj = i + di, j + dj
                    if ni < 0 or ni >= _NLATS:
                        ni = max(0, min(_NLATS - 1, ni))
                    if nj < 0 or nj >= _NLONS:
                        nj %= _NLONS
                    # Water barrier: skip if neighbor is too deep for this species
                    if wdepth is not None and sp.habitat_type == "terrestrial":
                        if float(wdepth[ni, nj]) > sp.wading_depth_max:
                            continue
                    ns = float(self._suitability_grid[ni, nj])
                    if ns > best_suit:
                        best_suit = ns; bi, bj = ni, nj

                if (bi, bj) == (i, j) or best_suit <= current_suit:
                    continue

                delta_suit = best_suit - current_suit
                migrate_frac = min(mig_rate, mig_rate * delta_suit) * _DT
                flow_raw = density * migrate_frac
                if flow_raw > _EPSILON:
                    if _USE_INTEGER_POPULATIONS:
                        flow = float(_stochastic_round(np.array([flow_raw]))[0])
                        if flow < 1.0:
                            continue
                    else:
                        flow = flow_raw
                    self._density[i, j] -= flow
                    self._density[bi, bj] += flow
