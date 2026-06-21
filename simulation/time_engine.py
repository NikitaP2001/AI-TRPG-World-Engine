"""Time Engine — advances world simulation state.

Reads current time from SQLite, updates temperature (hourly) and
L1 causal features (daily: lakes, groundwater, wetlands, vegetation),
plus long-cycle processes (climate drift, resources, tectonics) at
configurable intervals.

Usage:
    from simulation.time_engine import TimeEngine
    engine = TimeEngine("game/simulation/world.sqlite")
    engine.advance(days=7, hours=12)
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional

from .world_db import WorldDB
from .layer0.climate import (
    instant_temperature,
    _solar_declination,
    _day_length_hours,
    _diurnal_amplitude,
    _cos_zenith_angle,
)

# Long-cycle intervals [days] — no need on every advance
_CLIMATE_DRIFT_INTERVAL = 100       # random walk temp/precip
_RESOURCE_EVOLVE_INTERVAL = 365     # Gray-Scott resource flux drift
_GEOLOGICAL_EVENT_INTERVAL = 1000   # tectonic stress → earthquakes


class TimeEngine:
    """Drives world simulation forward in time.

    Each advance() call:
      1. Moves world_time forward (handles day/year rollover)
      2. Recomputes temperature for every cell (hourly, seasonal+diurnal)
      3. Runs L1 causal features (daily: lakes, groundwater, wetlands, vegetation)
      4. Runs long-cycle processes at configurable intervals
      5. Writes updated cells + features to SQLite
    """

    def __init__(self, db: WorldDB):
        self.db = db
        self._accumulated_days: float = 0.0
        self._rng = random.Random(42)
        # WorldState — continuous fields replacing CellData
        self._ws: Optional["WorldState"] = None

        # Multi-frequency counters for cryosphere + tectonics
        self._years_since_last_cryo: float = 0.0     # annual snowpack/mass balance
        self._centuries_since_last_iceflow: float = 0.0  # ice creep (~100 yr)
        self._myr_since_last_tectonics: float = 0.0  # plate motion (~0.1 Myr)
        # New counters for erosion, eustasy, diagenesis
        self._myr_since_last_erosion: float = 0.0    # landscape diffusion (~0.01 Myr)
        self._kyr_since_last_eustasy: float = 0.0    # sea level (~1 kyr)
        self._myr_since_last_diagenesis: float = 0.0 # lithosphere evolution (~0.1 Myr)
        # Soil evolution counter
        self._kyr_since_last_soil: float = 0.0       # soil formation (~1 kyr)
        self._soil_time_factor: int = 1              # incremented each soil step
        # Resource evolution counter
        self._myr_since_last_resources: float = 0.0  # ore/resource evolution (~0.1 Myr)
        # Vegetation succession counter
        self._myr_since_last_vegetation: float = 0.0 # PFT migration (~0.01 Myr)
        # Climate evolution
        self._climate_engine: Optional["ClimateEngine"] = None
        self._kyr_since_last_climate: float = 0.0    # climate drift (~1 kyr)

    # ── WorldState lazy loader ──────────────────────────────────────

    def _get_ws(self) -> "WorldState":
        """Load WorldState on demand (lazy)."""
        if self._ws is None:
            from .world_state_db import load_world_state
            self._ws = load_world_state(self.db)
        return self._ws

    def _ws_save(self) -> None:
        """Save WorldState to DB."""
        if self._ws is not None:
            from .world_state_db import save_world_state
            save_world_state(self.db, self._ws)

    def _ws_h3_ids(self) -> List[str]:
        """Get H3 cell IDs from the primary discrete field."""
        ws = self._get_ws()
        # Use elevation discrete data as source of all H3 IDs
        dmap = ws.get_discrete("elevation")
        if dmap:
            return list(dmap.keys())
        # Fallback: try other discrete fields
        for name in ("geological_type", "temperature", "precipitation"):
            dmap = ws.get_discrete(name)
            if dmap:
                return list(dmap.keys())
        return []

    # ── Public API ──────────────────────────────────────────────────

    def advance(self, days: float = 0, hours: float = 0,
                minutes: float = 0, seconds: float = 0) -> dict:
        """Advance world time and update all time-dependent fields.

        Hourly: temperature (instant_temperature with diurnal cycle).
        Daily:  L1 features (lake, groundwater, wetland, vegetation).
        Long-cycle: climate drift (~100d), resource evolution (~365d),
                    geological events (~1000d).

        Args:
            days, hours, minutes, seconds: time to advance.

        Returns:
            Dict with summary: {'tick', 'year', 'day_of_year', 'hour',
                                'temp_min', 'temp_max', 'temp_mean'}
        """
        time = self.db.get_time()
        if not time or "tick" not in time:
            self.db.init_time()
            time = self.db.get_time()

        total_hours = days * 24 + hours + minutes / 60 + seconds / 3600
        if total_hours <= 0:
            return self._summary(time)

        total_days = max(1, int(days + hours / 24 + minutes / 1440))
        total_years = total_days / 365.0

        # ── Short advance: hourly temperature (diurnal cycle) ─────
        # For long advances (> 1 year) skip the hourly loop —
        # diurnal+seasonal cycles cancel out over years, compute
        # final temperature snapshot instead.
        if total_days <= 365:
            n_steps = max(1, int(total_hours))
            dt_hours = total_hours / n_steps
            for _ in range(n_steps):
                self._step(dt_hours)
                time = self.db.get_time()
            # Compute temperature at final time (single call, not 720×)
            self._update_temperature(time["day_of_year"], time["hour"])
            # Compute seasonal precipitation for this time of year
            self._update_precipitation(time["day_of_year"])
            # Sync WS time after hourly loop
            if self._ws is not None:
                self._ws._time = dict(time)
        else:
            # Jump clock forward, compute temperature at final day+hour
            new_year = time["year"] + int(total_days // 365)
            new_day = int(total_days % 365)
            new_hour = time["hour"]
            new_tick = time["tick"] + total_days
            self.db.set_time(tick=new_tick, year=new_year,
                             day_of_year=new_day, hour=new_hour)
            self._update_temperature(new_day, new_hour)
            self._update_precipitation(new_day)
            time = self.db.get_time()
            # Sync WorldState time cache
            if self._ws is not None:
                self._ws._time = dict(time)

        # ── L1 daily step: run causal features for total days ──
        self._run_l1_step(dt_days=total_days)

        # Persist WorldState (temperature, fauna, vegetation, water, etc.)
        if self._ws is not None:
            from .world_state_db import save_world_state
            save_world_state(self.db, self._ws)
            # Sync fauna to legacy fauna_populations table for viewer
            self._sync_fauna_to_db()

        # ── Long-cycle processes at intervals ──
        self._accumulated_days += total_days
        self._run_long_cycle()

        # ── Multi-frequency Earth system processes ──────────────
        self._years_since_last_cryo += total_years
        self._centuries_since_last_iceflow += total_years
        self._myr_since_last_tectonics += total_years / 1e6
        self._myr_since_last_erosion += total_years / 1e6
        self._kyr_since_last_eustasy += total_years / 1e3
        self._myr_since_last_diagenesis += total_years / 1e6
        self._kyr_since_last_soil += total_years / 1e3
        self._myr_since_last_resources += total_years / 1e6
        self._myr_since_last_vegetation += total_years / 1e6
        self._kyr_since_last_climate += total_years / 1e3

        self._run_cryo_annual()
        self._run_iceflow_centennial()
        self._run_climate_evolution()
        self._run_erosion_geological()
        self._run_eustasy_millennial()
        self._run_diagenesis_geological()
        self._run_soil_evolution()
        self._run_resource_evolution()
        self._run_vegetation_succession()
        self._run_tectonics_geological()

        return self._summary(time)

    # ── Internal ────────────────────────────────────────────────────

    def _step(self, dt_hours: float) -> None:
        """Advance one time step and update temperature."""
        time = self.db.get_time()
        if not time:
            return

        new_hour = time["hour"] + dt_hours
        new_day = time["day_of_year"]
        new_tick = time["tick"] + 1
        new_year = time["year"]

        while new_hour >= 24:
            new_hour -= 24
            new_day += 1
        while new_hour < 0:
            new_hour += 24
            new_day -= 1
        while new_day >= 365:
            new_day -= 365
            new_year += 1
        while new_day < 0:
            new_day += 365
            new_year -= 1

        self.db.set_time(tick=new_tick, year=new_year,
                         day_of_year=new_day, hour=new_hour)
        # Temperature updated once after loop, not every hour

    def _update_temperature(self, day_of_year: float, hour: float) -> None:
        """Recompute temperature field via continuous elevation + climate model.

        Stores the result as a continuous temperature field (WorldState)
        + discrete temperature map for backward compat.
        """
        ws = self._get_ws()
        if not ws.has_field("elevation"):
            return
        elev_f = ws.field("elevation")
        axial_tilt = float(ws.get_param("axial_tilt", "23.44"))

        # Sample from discrete elevation data for all H3 cells
        elev_map = ws.get_discrete("elevation")
        sea_level = float(ws.get_param("sea_level", "0.0"))
        if not elev_map:
                return
        # Compute temperature at every sampled point
        import h3 as _h3_t
        temp_data = {}
        for hid, elev in elev_map.items():
            latlng = _h3_t.cell_to_latlng(hid)
            lat, lon = latlng[0], latlng[1]
            # Water body (ocean or lake): water_depth > 0
            water_depth = max(0.0, sea_level - elev)
            is_ocean = water_depth > 0.01
            coastal = not is_ocean and abs(lat) < 60

            t_c = instant_temperature(
                lat_deg=lat, elevation=elev,
                is_ocean=is_ocean, coastal=coastal,
                day_of_year=day_of_year, hour=hour,
                axial_tilt=axial_tilt,
            )
            from .layer0.climate import TEMP_C_MIN as _TCM, _TEMP_C_RANGE as _TCR
            t_norm = max(0.0, min(1.0, (t_c - _TCM) / _TCR))
            temp_data[hid] = t_norm

        # Store as continuous field + discrete (in-memory only)
        # DB save happens once in advance() after the loop completes
        if len(temp_data) > 10:
            ws.set_field("temperature", temp_data)
            ws.set_discrete("temperature", temp_data)

    def _update_precipitation(self, day_of_year: float) -> None:
        """Compute seasonal precipitation from ITCZ position + base climate.

        Uses the existing precip_seasonality field to modulate mean annual
        precipitation: cells closer to the ITCZ get more rain, cells farther
        get less.  The ITCZ follows the solar declination.
        """
        ws = self._get_ws()
        base_precip_map = ws.get_discrete("precipitation")
        seas_map = ws.get_discrete("precip_seasonality")
        if not base_precip_map or not seas_map:
            return
        axial_tilt = float(ws.get_param("axial_tilt", "23.44"))
        decl = _solar_declination(day_of_year, axial_tilt)
        itcz_lat = decl * 0.8  # ITCZ follows declination with ocean thermal lag

        import h3 as _h3_p
        new_precip = {}
        for hid, base_p in base_precip_map.items():
            latlng = _h3_p.cell_to_latlng(hid)
            lat = latlng[0]
            seas = seas_map.get(hid, 0.1)

            # Distance from ITCZ (degrees of latitude)
            dist = abs(lat - itcz_lat)
            # ITCZ influence: gaussian with ~15 deg half-width
            itcz_influence = math.exp(-(dist / 15.0) ** 2)
            # Seasonal anomaly: ±seas relative to baseline
            # baseline = 0.5 (when ITCZ is far), max = 0.5+seas (when ITCZ overhead)
            seasonal_factor = 1.0 + seas * (itcz_influence - 0.5) * 2.0
            p = base_p * seasonal_factor
            p = max(0.0, min(1.0, p))
            new_precip[hid] = p

        ws.set_field("precipitation", new_precip)
        ws.set_discrete("precipitation", new_precip)

    def _run_l1_step(self, dt_days: float) -> None:
        """Run one L1 tick: lakes, groundwater, wetlands, vegetation, fauna."""
        if dt_days < 0.5:
            return

        # 1. Load WorldState + features
        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        # Ephemeral CellData from WS continuous fields (for legacy FieldRegistry)
        cells = ws.to_celldata(h3_ids)

        fs = self.db.get_feature_store()
        if fs is None:
            return

        # 2. Build FieldRegistry + SimEngine
        from .layer1.fields import FieldRegistry
        from .layer1.engine import SimEngine
        from .layer1.features.groundwater import Groundwater
        from .layer1.features.lake import Lake
        from .layer1.features.wetland import Wetland
        from .layer1.features.vegetation import Vegetation
        from .layer1.features.fauna import Fauna
        from .layer1.features.settlement_footprint import SettlementFootprint
        from .layer1.fauna_registry import FAUNA_REGISTRY, get_species_ids
        from .layer1.settlement_type_registry import SETTLEMENT_TYPE_REGISTRY
        from .layer1.emergence import check_fauna_emergence

        fields = FieldRegistry.from_cells(cells)
        engine = SimEngine(fields)

        # 2a. Load MutableField _base from WS for persistence between ticks
        # This ensures canopy_density, PFT biomass, etc. retain previous state
        for _mf_name in list(fields._mutable_fields.keys()):
            if ws.has_field(_mf_name):
                try:
                    _ws_cf = ws._fields._base_fields.get(_mf_name)
                    if _ws_cf is not None:
                        _mf = fields._mutable_fields[_mf_name]
                        _mf._base = _ws_cf
                except Exception:
                    pass

        # 2b. Register PFT biomass MutableFields (populated by Vegetation feature)
        from .layer0.plant_registry import PFT_REGISTRY as _PFT_REG
        if _PFT_REG:
            for _pft_name in _PFT_REG:
                _pft_fn = f"pft_biomass[{_pft_name}]"
                if not fields.has(_pft_fn):
                    from .layer1.fields import MutableField as _MF
                    _new_mf = _MF(None, default=0.0)
                    # If WS has this field from a previous advance, use it as _base
                    if ws.has_field(_pft_fn):
                        try:
                            _ws_cf = ws._fields._base_fields.get(_pft_fn)
                            if _ws_cf is not None:
                                _new_mf._base = _ws_cf
                        except Exception:
                            pass
                    fields.register_mutable(_pft_fn, _new_mf)
                else:
                    # Field already exists — try to set _base from WS
                    try:
                        _existing_mf = fields.get_mutable(_pft_fn)
                        if _existing_mf._base is None and ws.has_field(_pft_fn):
                            _ws_cf = ws._fields._base_fields.get(_pft_fn)
                            if _ws_cf is not None:
                                _existing_mf._base = _ws_cf
                    except Exception:
                        pass

        # 3. Load fauna population_density fields into MutableFields
        # First try from continuous WS fields, fall back to fauna_populations table
        import h3 as _h3_fauna
        ws_fauna_loaded = 0
        for species_id in get_species_ids():
            field_name = f"population_density[{species_id}]"
            if not fields.has(field_name):
                continue
            try:
                mf = fields.get_mutable(field_name)
            except KeyError:
                continue
            # Try continuous WS field first
            ws_field_name = f"population_density[{species_id}]"
            if ws.has_field(ws_field_name):
                ws_fauna_loaded += 1
                # Collect points + values to build a proper ContinuousField _base
                _pts_3d = []
                _vals = []
                for cell in cells:
                    latlng = _h3_fauna.cell_to_latlng(cell.h3_id)
                    lat, lon = float(latlng[0]), float(latlng[1])
                    try:
                        density = max(0.0, ws.field(ws_field_name)(lat, lon))
                        if density > 1e-8:
                            mf.add_persistent(lat, lon, radius_deg=2.0, strength=density)
                            # Also collect for _base
                            lat_r = math.radians(lat)
                            lon_r = math.radians(lon)
                            _pts_3d.append([
                                math.cos(lat_r) * math.cos(lon_r),
                                math.sin(lat_r),
                                math.cos(lat_r) * math.sin(lon_r),
                            ])
                            _vals.append(float(density))
                    except Exception:
                        pass
                # Build _base from collected data so Fauna._load_density_from_base can read it
                if _vals:
                    import numpy as _np_ws
                    from scipy.spatial import cKDTree as _cKDTree_ws
                    from simulation.layer0.contouring import ContinuousField as _CF_ws
                    _tree_ws = _cKDTree_ws(_np_ws.array(_pts_3d, dtype=_np_ws.float64))
                    mf._base = _CF_ws(_tree_ws, _np_ws.array(_vals, dtype=_np_ws.float64))
        if ws_fauna_loaded == 0:
            # Fallback: load from legacy fauna_populations table
            fauna_rows_all = self.db.load_fauna_populations()
            fauna_by_species: dict = {}
            for row in fauna_rows_all:
                sid = row["species_id"]
                fauna_by_species.setdefault(sid, []).append(row)
            for species_id, rows in fauna_by_species.items():
                field_name = f"population_density[{species_id}]"
                if not fields.has(field_name):
                    continue
                try:
                    mf = fields.get_mutable(field_name)
                except KeyError:
                    continue
                # Build a ContinuousField as base so it survives clear_persistent()
                points_3d = []
                vals = []
                import math as _m
                for r in rows:
                    try:
                        latlng_r = _h3_fauna.cell_to_latlng(r["h3_id"])
                        lat_r = _m.radians(latlng_r[0])
                        lon_r = _m.radians(latlng_r[1])
                        points_3d.append([_m.cos(lat_r)*_m.cos(lon_r),
                                           _m.sin(lat_r),
                                           _m.cos(lat_r)*_m.sin(lon_r)])
                        vals.append(float(r.get("density", 0.0)))
                    except Exception:
                        pass
                if vals:
                    import numpy as _np2
                    from scipy.spatial import cKDTree
                    from .layer0.contouring import ContinuousField
                    tree = cKDTree(_np2.array(points_3d, dtype=_np2.float64))
                    mf._base = ContinuousField(tree, _np2.array(vals, dtype=_np2.float64))

        # 4. Add Vegetation
        engine.add_feature(Vegetation())

        # 5. Reconstruct L1 features from feature store
        has_groundwater = False
        for feat in fs.all_active:
            if feat.type == "lake" and feat.geometry is not None:
                spill = feat.properties.get("spill_elevation", 0.1)
                lake = Lake(polygon=feat.geometry, spill_elevation=spill,
                            feature_id=feat.feature_id)
                if "volume_m3" in feat.properties:
                    lake.props["volume_m3"] = feat.properties["volume_m3"]
                if "level_m" in feat.properties:
                    lake.props["level_m"] = feat.properties["level_m"]
                if "river_inflow_m3s" in feat.properties:
                    lake.props["river_inflow_m3s"] = feat.properties["river_inflow_m3s"]
                engine.add_feature(lake)

            elif feat.type == "wetland" and feat.geometry is not None:
                wtype = feat.properties.get("wetland_type", "marsh")
                wetland = Wetland(polygon=feat.geometry, wetland_type=wtype,
                                  feature_id=feat.feature_id)
                if "peat_depth" in feat.properties:
                    wetland.props["peat_depth"] = feat.properties["peat_depth"]
                engine.add_feature(wetland)

            elif feat.type == "groundwater" and not has_groundwater:
                gw = Groundwater(feature_id=feat.feature_id)
                engine.add_feature(gw)
                has_groundwater = True

        if not has_groundwater:
            engine.add_feature(Groundwater())

        # 5b. Compute water_depth grid from elevation + sea_level + lakes
        # Injected into _FIELD_GRID_DATA so fauna/plankton can use continuous water depth
        import simulation.layer1.features.fauna as _fauna_mod
        _fauna_mod._ensure_grid()
        _wd_nl = _fauna_mod._NLATS
        _wd_nlo = _fauna_mod._NLONS
        _set_wd = _fauna_mod._set_field_grid_data
        import numpy as _np
        _wd = _np.zeros((_wd_nl, _wd_nlo), dtype=_np.float64)
        _elev_f = fields.get("elevation_mean")
        if _elev_f is not None:
            _sea_level = float(ws.get_param("sea_level", "0.0"))
            for _i in range(_wd_nl):
                _lat = float(_fauna_mod._GRID_LATS[_i])
                for _j in range(_wd_nlo):
                    _lon = float(_fauna_mod._GRID_LONS[_j])
                    _el = _elev_f.base_only(_lat, _lon)
                    _wd[_i, _j] = max(0.0, _sea_level - _el)
            # Lake depth: check grid points inside lake polygons
            for _feat in fs.all_active:
                if _feat.type == "lake" and _feat.geometry is not None:
                    _lake_level = float(_feat.properties.get("spill_elevation", 0.1))
                    if _lake_level <= _sea_level:
                        continue
                    from shapely.geometry import Point as _Pt
                    for _i in range(_wd_nl):
                        _lat = float(_fauna_mod._GRID_LATS[_i])
                        for _j in range(_wd_nlo):
                            if _wd[_i, _j] > 0:
                                continue
                            _lon = float(_fauna_mod._GRID_LONS[_j])
                            if _feat.geometry.contains(_Pt(_lon, _lat)):
                                _el = _elev_f.base_only(_lat, _lon)
                                _ld = max(0.0, _lake_level - _el)
                                if _ld > _wd[_i, _j]:
                                    _wd[_i, _j] = _ld
        _set_wd("water_depth", _wd)

        # Save water_depth to WS immediately (before engine.step clears _FIELD_GRID_DATA)
        # Also register in FieldRegistry so _build_field_grid can re-sample it
        import h3 as _h3_wd
        _wd_data = {}
        for _cell in cells:
            try:
                _ll = _h3_wd.cell_to_latlng(_cell.h3_id)
                _i = int((_ll[0] + 90.0) / 2.0 + 0.5)
                _j = int((_ll[1] + 180.0) / 2.0 + 0.5)
                _i = max(0, min(89, _i))
                _j = max(0, min(179, _j))
                _v = float(_wd[_i, _j])
                if _v > 0.001:
                    _wd_data[_cell.h3_id] = _v
            except Exception:
                pass
        if _wd_data:
            try:
                ws.set_field("water_depth", _wd_data)
                # Register in FieldRegistry so _build_field_grid can sample it
                _ws_wd_cf = ws._fields._base_fields.get("water_depth")
                if _ws_wd_cf is not None:
                    fields.register_base("water_depth", _ws_wd_cf)
            except Exception:
                pass

        # 6. Compute plankton_density from water temperature (for aquatic species)
        import h3 as _h3_plank
        plank_mf = None
        try:
            plank_mf = fields.get_mutable("plankton_density")
        except KeyError:
            pass
        if plank_mf is not None:
            # Use water_depth if already computed, else fall back to elevation
            _wd_f = _fauna_mod._FIELD_GRID_DATA.get("water_depth")
            for cell in cells:
                latlng = _h3_plank.cell_to_latlng(cell.h3_id)
                lat, lon = float(latlng[0]), float(latlng[1])
                # Skip land: check if water_depth > 0, or fall back to elevation
                if _wd_f is not None:
                    _i = int((lat + 90.0) / 2.0 + 0.5)
                    _j = int((lon + 180.0) / 2.0 + 0.5)
                    _i = max(0, min(89, _i))
                    _j = max(0, min(179, _j))
                    _wd_val = float(_wd_f[_i, _j])
                    is_water = _wd_val > 0.01
                else:
                    elev_f = fields.get("elevation_mean")
                    elev = elev_f(lat, lon) if elev_f else 0.0
                    is_water = elev < 0
                if not is_water:
                    continue  # land — no plankton
                temp_f = fields.get("temperature")
                temp = max(0.0, min(1.0, temp_f(lat, lon))) if temp_f else 0.5
                # Plankton blooms at moderate temps (0.3-0.7 norm = ~8-26°C)
                plank = 0.0
                if 0.1 < temp < 0.9:
                    plank = math.sin(math.pi * (temp - 0.1) / 0.8) * 0.15
                if plank > 1e-6:
                    plank_mf.add_persistent(lat, lon, radius_deg=2.0, strength=plank)

        # 7. Clear shared env cache, add Fauna features, run
        from .layer1.features.fauna import _clear_env_cache
        _clear_env_cache()
        for species_id in get_species_ids():
            engine.add_feature(Fauna(species_id))

        # 7b. Run for dt_days
        engine.step(dt=float(dt_days))

        # 8. Propagate mutable field state back to WorldState discrete data
        import h3 as _h3_prop
        wt_f = fields.get("water_table_depth")
        canopy_f = fields.get("canopy_density")
        soil_mut = fields.get("soil_fertility")
        wt_disc = ws.get_discrete("water_table_depth")
        can_disc = ws.get_discrete("canopy_density")
        soil_disc = ws.get_discrete("soil_fertility")
        for cell in cells:
            latlng = _h3_prop.cell_to_latlng(cell.h3_id)
            lat, lon = float(latlng[0]), float(latlng[1])
            wt_disc[cell.h3_id] = max(0.0, wt_f(lat, lon))
            can_disc[cell.h3_id] = max(0.0, min(1.0, canopy_f(lat, lon)))
            soil_disc[cell.h3_id] = max(0.0, min(1.0, soil_mut(lat, lon)))

        # 9. Apply settlement footprint — accumulating deltas to WS fields
        for feat in engine.features:
            if feat.feature_type == "settlement_footprint":
                sf = feat
                sf.apply_to_cells(cells, dt=float(dt_days))
        # Sync settlement-modified cell attrs to WS discrete data
        for cell in cells:
            for attr in ("canopy_density", "soil_fertility",
                          "water_table_depth", "hazard_level"):
                val = getattr(cell, attr, None)
                if val is not None:
                    ws.get_discrete(attr)[cell.h3_id] = val

        # 10. Save fauna population_density fields as continuous fields in WS
        import h3 as _h3_fauna_save
        for species_id in get_species_ids():
            field_name = f"population_density[{species_id}]"
            try:
                pop_mf = fields.get_mutable(field_name)
            except KeyError:
                continue

            # Apply settlement hunting/suppression deltas
            for sf_feat in engine.features:
                if sf_feat.feature_type != "settlement_footprint":
                    continue
                deltas = sf_feat.props.get("_hunting_deltas", {})
                if not deltas:
                    continue
                for h3_id_str, delta in deltas.items():
                    try:
                        latlng = _h3_fauna_save.cell_to_latlng(h3_id_str)
                        lat, lon = float(latlng[0]), float(latlng[1])
                        pop_mf.add_persistent(lat, lon, radius_deg=2.0, strength=delta)
                    except Exception:
                        pass

            # Build continuous field dict from MutableField
            # Include zero entries for deep-water cells so the WS
            # ContinuousField doesn't interpolate from land neighbors.
            import simulation.layer1.features.fauna as _fauna_wd
            _wd_grid = _fauna_wd._FIELD_GRID_DATA.get("water_depth")
            field_data = {}
            for cell in cells:
                latlng = _h3_fauna_save.cell_to_latlng(cell.h3_id)
                lat, lon = float(latlng[0]), float(latlng[1])
                density = max(0.0, pop_mf(lat, lon))
                if density > 1e-6:
                    field_data[cell.h3_id] = density
                elif _wd_grid is not None:
                    # Check if this cell is in deep water — include explicit zero
                    # to prevent KDTree interpolation from land cells.
                    _i = int((lat + 90.0) / 2.0 + 0.5)
                    _j = int((lon + 180.0) / 2.0 + 0.5)
                    _i = max(0, min(89, _i))
                    _j = max(0, min(179, _j))
                    if _wd_grid[_i, _j] > 0.02:
                        field_data[cell.h3_id] = 0.0

            # Register as continuous field in WorldState
            if field_data:
                try:
                    ws.set_field(field_name, field_data)
                except Exception:
                    pass

        # 10b. Propagate PFT biomass fields from engine MutableFields to WS
        for _pft_name in _PFT_REG:
            _pft_fn = f"pft_biomass[{_pft_name}]"
            try:
                _pft_mf = fields.get_mutable(_pft_fn)
            except KeyError:
                continue
            _pft_data = {}
            for _cell in cells:
                try:
                    _ll = _h3_fauna_save.cell_to_latlng(_cell.h3_id)
                    _v = max(0.0, _pft_mf(_ll[0], _ll[1]))
                    if _v > 1e-6:
                        _pft_data[_cell.h3_id] = _v
                except Exception:
                    pass
            if _pft_data:
                try:
                    ws.set_field(_pft_fn, _pft_data)
                except Exception:
                    pass

        # 11. Build ephemeral per-cell sample for emergence check
        emergence_rows = []
        for species_id in get_species_ids():
            field_name = f"population_density[{species_id}]"
            try:
                pop_mf = fields.get_mutable(field_name)
            except KeyError:
                continue
            for cell in cells:
                latlng = _h3_prop.cell_to_latlng(cell.h3_id)
                lat, lon = float(latlng[0]), float(latlng[1])
                density = max(0.0, pop_mf(lat, lon))
                if density > 1e-6:
                    emergence_rows.append({
                        "h3_id": cell.h3_id, "species_id": species_id,
                        "density": density,
                    })
        emergence_events = check_fauna_emergence(
            emergence_rows,
            current_tick=self.db.get_time().get("tick", 0),
        )
        if emergence_events:
            for ev in emergence_events:
                print(f"  [Emergence] {ev['faction_name']} ({ev['faction_id']}) "
                      f"— {ev['total_population']:.0f} individuals")

        # 12. Compute encounter_probability -> hazard_level feedback
        from .layer1.fauna_registry import get_hazard_weight
        species_ids = list(get_species_ids())
        haz_disc = ws.get_discrete("hazard_level")
        for cell in cells:
            latlng = _h3_prop.cell_to_latlng(cell.h3_id)
            lat, lon = float(latlng[0]), float(latlng[1])
            encounter = 0.0
            for species_id in species_ids:
                sp = FAUNA_REGISTRY.get(species_id)
                if sp is None:
                    continue
                hw = get_hazard_weight(species_id)
                if hw <= 0:
                    continue
                try:
                    pop_f = fields.get(f"population_density[{species_id}]")
                    density = max(0.0, pop_f(lat, lon))
                    density_max = sp.population_density_max
                    if density_max > 0:
                        encounter += (density / density_max) * hw
                except KeyError:
                    pass
            current_haz = haz_disc.get(cell.h3_id, 0.0)
            haz_disc[cell.h3_id] = max(0.0, min(1.0, current_haz + encounter * 0.05))

        # 13. Save WorldState (fields + discrete data)
        self._ws_save()

        # 14. Update feature store with new L1 state
        for feat in engine.features:
            if feat.feature_type == "lake":
                for f in fs.all_active:
                    if f.feature_id == feat.feature_id:
                        f.properties["volume_m3"] = feat.props["volume_m3"]
                        f.properties["level_m"] = feat.props["level_m"]
                        f.properties["outflow_m3s"] = feat.props.get("outflow_m3s", 0)
                        break
            elif feat.feature_type == "wetland":
                for f in fs.all_active:
                    if f.feature_id == feat.feature_id:
                        f.properties["peat_depth"] = feat.props.get("peat_depth", 0)
                        break

        self.db.save_features(fs)

    def _summary(self, time: dict) -> dict:
        """Return a summary dict of current world state (from fields)."""
        temps = []
        ws = self._get_ws()
        if ws.has_field("temperature"):
            dmap = ws.get_discrete("temperature")
            if dmap:
                from .layer0.climate import TEMP_C_MIN as _TCM, _TEMP_C_RANGE as _TCR
                temps = [_TCM + v * _TCR for v in dmap.values()]
        return {
            "tick": time.get("tick", 0),
            "year": time.get("year", 0),
            "day_of_year": time.get("day_of_year", 0),
            "hour": time.get("hour", 0),
            "temp_min": min(temps) if temps else 0,
            "temp_max": max(temps) if temps else 0,
            "temp_mean": sum(temps) / len(temps) if temps else 0,
        }

    # ── Fauna sync to legacy table ──────────────────────────────────

    def _sync_fauna_to_db(self) -> None:
        """Sample WS continuous fauna fields → fauna_populations table.

        The viewer reads fauna_populations for the info panel.
        Without this sync, the panel would show stale data after advance().
        """
        from .layer1.fauna_registry import get_species_ids
        import h3 as _h3

        ws = self._ws
        if ws is None:
            return
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        species_ids = list(get_species_ids())
        fauna_rows = []
        for species_id in species_ids:
            field_name = f"population_density[{species_id}]"
            if not ws.has_field(field_name):
                continue
            fa = ws.field(field_name)
            for hid in h3_ids:
                latlng = _h3.cell_to_latlng(hid)
                lat, lon = float(latlng[0]), float(latlng[1])
                try:
                    density = max(0.0, fa(lat, lon))
                except Exception:
                    density = 0.0
                # Round to integer individuals when densities are small
                if density < 100 and density > 0:
                    import random as _rnd_sync
                    density = int(density) + (1 if _rnd_sync.random() < density - int(density) else 0)
                if density > 0.5:
                    fauna_rows.append({
                        "h3_id": hid, "species_id": species_id,
                        "density": density,
                        "lat": lat, "lon": lon,
                        "updated_at_tick": self.db.get_time().get("tick", 0),
                    })
        if fauna_rows:
            # Replace entire table (simpler than per-cell diff)
            self.db.conn.execute("DELETE FROM fauna_populations")
            self.db.save_fauna_populations(fauna_rows)

    # ── Long-cycle processes (continuous fields) ──────────────────

    def _run_long_cycle(self) -> None:
        """Run long-cycle processes via continuous field dicts."""
        acc = self._accumulated_days
        if acc < _CLIMATE_DRIFT_INTERVAL:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        # Get field dicts from WS (no CellData)
        temp_map = ws.get_discrete("temperature")
        precip_map = ws.get_discrete("precipitation")
        temp_range_map = ws.get_discrete("temp_seasonal_range")
        cc_map = ws.get_discrete("climate_class")

        if acc >= _CLIMATE_DRIFT_INTERVAL and temp_map:
            # Build ephemeral CellData from WS discrete maps
            from .layer0.cell_model import CellData
            _tmp_cells = []
            for hid in h3_ids:
                _tmp_cells.append(CellData(
                    h3_id=hid, resolution=2,
                    elevation_mean=0.0,
                    geological_type=2,
                    temperature=temp_map.get(hid, 0.5),
                    precipitation=precip_map.get(hid, 0.5),
                    temp_seasonal_range=temp_range_map.get(hid, 0.2),
                    climate_class=cc_map.get(hid, "") or "",
                ))
            from .layer0.long_cycle import _drift_climate
            n = _drift_climate(_tmp_cells, drift_rate=0.003, rng=self._rng)
            if n > 0:
                print(f"  [LongCycle] climate drift: {n} cells changed class")
            # Write back to WS discrete maps
            for c in _tmp_cells:
                temp_map[c.h3_id] = c.temperature
                precip_map[c.h3_id] = c.precipitation
                cc_map[c.h3_id] = c.climate_class
            self._accumulated_days -= _CLIMATE_DRIFT_INTERVAL

        if acc >= _RESOURCE_EVOLVE_INTERVAL:
            from .layer0.long_cycle import _evolve_resources
            from .layer0.resources import default_resource_types
            rtypes = default_resource_types()
            # Build ephemeral cells with resource flux (use WS data if available)
            res_cells = ws.to_celldata(h3_ids)
            _evolve_resources(res_cells, rtypes, steps=5, rng=self._rng)
            self._accumulated_days -= _RESOURCE_EVOLVE_INTERVAL

        if acc >= _GEOLOGICAL_EVENT_INTERVAL:
            from .layer0.long_cycle import _check_geological_events
            geo_cells = ws.to_celldata(h3_ids)
            events = _check_geological_events(
                geo_cells,
                stress_accumulation_rate=0.01, event_threshold=0.8, rng=self._rng,
            )
            if events:
                print(f"  [LongCycle] {len(events)} geological event(s)")
                # Write back elevation/stress/hazard changes to WS
                elev_map = ws.get_discrete("elevation")
                stress_map = ws.get_discrete("tectonic_stress")
                hazard_map = ws.get_discrete("hazard_level")
                for c in geo_cells:
                    elev_map[c.h3_id] = c.elevation_mean
                    stress_map[c.h3_id] = c.tectonic_stress
                    hazard_map[c.h3_id] = c.hazard_level
            self._accumulated_days -= _GEOLOGICAL_EVENT_INTERVAL

        # Register updated temperature/precip as continuous fields
        ws.set_field("temperature", dict(temp_map))
        ws.set_field("precipitation", dict(precip_map))
        self._ws_save()


    # ── Cryosphere: annual snowpack / mass balance ─────────────────

    def _run_cryo_annual(self) -> None:
        """Annual cryosphere update — fires every ~1 year."""
        threshold = 1.0
        if self._years_since_last_cryo < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)
        h3_ids = [c.h3_id for c in cells]
        temperature = {c.h3_id: c.temperature for c in cells}
        precipitation = {c.h3_id: c.precipitation for c in cells}
        elevation = {c.h3_id: c.elevation_mean for c in cells}
        ocean_set = {c.h3_id for c in cells if c.geological_type == 0}
        snowpack = {c.h3_id: getattr(c, 'snowpack_mm', 0.0) for c in cells}
        ice = {c.h3_id: getattr(c, 'ice_thickness_m', 0.0) for c in cells}

        try:
            from .layer0.cryosphere import CryosphereEngine
            cryo = CryosphereEngine(h3_ids, elevation, temperature,
                                     precipitation, ocean_set)
            dt_years = self._years_since_last_cryo
            new_snow, new_ice = cryo.advance(dt_years, snowpack, ice)
            self._years_since_last_cryo = 0.0

            n_ice = sum(1 for v in new_ice.values() if v > 1.0)
            if n_ice > 0:
                print(f"  [Cryo] annual: {n_ice} glacier cells")

            ws.get_discrete("snowpack_mm").update(new_snow)
            ws.get_discrete("ice_thickness_m").update(new_ice)
            self._ws_save()
        except Exception as e:
            print(f"  [Cryo] annual skipped: {e}")

    # ── Cryosphere: ice flow (centennial) ──────────────────────────

    def _run_iceflow_centennial(self) -> None:
        """Ice creep / glacier flow — every ~100 years."""
        threshold = 100.0
        if self._centuries_since_last_iceflow < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)
        h3_ids = [c.h3_id for c in cells]
        elevation = {c.h3_id: c.elevation_mean for c in cells}
        ocean_set = {c.h3_id for c in cells if c.geological_type == 0}
        ice = {c.h3_id: getattr(c, 'ice_thickness_m', 0.0) for c in cells}

        try:
            from .layer0.cryosphere.advance import _ice_flow, _calving
            dt_years = self._centuries_since_last_iceflow
            n_flow = min(max(1, int(dt_years / 10)), 10)
            for _ in range(n_flow):
                ice = _ice_flow(h3_ids, ice, elevation, ocean_set)
                ice = _calving(h3_ids, ice, ocean_set)
            self._centuries_since_last_iceflow = 0.0

            ws.get_discrete("ice_thickness_m").update(ice)
            self._ws_save()
            print(f"  [Cryo] ice flow: {sum(1 for v in ice.values() if v > 1.0)} glacier cells")
        except Exception as e:
            print(f"  [Cryo] ice flow skipped: {e}")

    # ── Climate evolution (millennial) ──────────────────────────

    def _run_climate_evolution(self) -> None:
        """Milankovitch + CO2 + albedo climate evolution."""
        threshold = 0.001  # Myr (1000 years)
        if self._kyr_since_last_climate < threshold:
            return

        ws = self._get_ws()
        if not ws.get_discrete("ice_thickness_m"):
            return

        dt_years = self._kyr_since_last_climate * 1e6

        try:
            from .layer0.climate_engine import ClimateEngine
            if self._climate_engine is None:
                self._climate_engine = ClimateEngine()
            self._climate_engine.advance(ws, dt_years=dt_years)
            self._kyr_since_last_climate = 0.0
            self._ws_save()
            print(f"  [Climate] evolved {dt_years/1000:.0f} kyr "
                  f"(CO2={self._climate_engine._co2_ppm:.0f}ppm, "
                  f"tilt={self._climate_engine._axial_tilt:.2f}°)")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [Climate] skipped: {e}")

    # ── Erosion: landscape diffusion (geological) ─────────────────

    def _run_erosion_geological(self) -> None:
        """Hillslope diffusion + sediment transport — every ~0.01 Myr."""
        threshold = 0.01
        if self._myr_since_last_erosion < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)
        h3_ids = [c.h3_id for c in cells]
        dt_myr = self._myr_since_last_erosion

        try:
            from .layer0.erosion import ErosionEngine
            from .layer0.erosion.diffusion import build_neighbour_map, compute_sediment_budget

            nmap = build_neighbour_map(h3_ids)
            ocean_set = {c.h3_id for c in cells if c.geological_type == 0}
            elev = {c.h3_id: c.elevation_mean for c in cells}

            engine = ErosionEngine(h3_ids, neighbour_map=nmap)
            new_elev = engine.advance(elev, dt_years=dt_myr * 1e6, ocean_set=ocean_set)
            self._myr_since_last_erosion = 0.0

            budget = compute_sediment_budget(elev, new_elev, ocean_set)

            # Store back to WS continuous + discrete fields
            ws.set_field("elevation", new_elev)
            el_disc = ws.get_discrete("elevation")
            sed_disc = ws.get_discrete("sediment_thickness")
            for hid in h3_ids:
                el_disc[hid] = new_elev.get(hid, elev.get(hid, 0))
                sed_disc[hid] = max(0.0, sed_disc.get(hid, 0.0)
                                    + budget.get(hid, 0.0) * 0.001)

            n_changed = sum(1 for h in h3_ids
                            if abs(new_elev.get(h, 0) - elev.get(h, 0)) > 0.001)
            if n_changed > 0:
                print(f"  [Erosion] advanced {dt_myr:.4f} Myr: {n_changed} cells changed")
            self._ws_save()
        except Exception as e:
            print(f"  [Erosion] skipped: {e}")

    # ── Eustasy: sea level (millennial) ──────────────────────────

    def _run_eustasy_millennial(self) -> None:
        """Sea-level change from tectonics + cryosphere — every ~1 kyr."""
        threshold = 0.001
        if self._kyr_since_last_eustasy < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)

        try:
            from .layer0.eustasy import (
                compute_sea_level_offset,
                apply_sea_level,
                compute_mean_ocean_age,
            )

            ice_total = sum(getattr(c, 'ice_thickness_m', 0.0) for c in cells)
            ice_fraction = min(1.0, ice_total / (len(cells) * 1000.0))
            mean_age = compute_mean_ocean_age(cells)

            temps = [c.temperature for c in cells]
            mean_temp_norm = sum(temps) / len(temps) if temps else 0.5
            from .layer0.climate import TEMP_C_MIN as _TCM, _TEMP_C_RANGE as _TCR
            mean_temp_c = _TCM + mean_temp_norm * _TCR
            temp_anomaly = mean_temp_c - 15.0

            orig_ocean = {c.h3_id: (c.geological_type == 0) for c in cells}
            offset = compute_sea_level_offset(
                mean_ocean_crustal_age=mean_age,
                ice_volume_fraction=ice_fraction,
                global_temp_anomaly=temp_anomaly,
            )

            current_offset = ws.get_discrete("sea_level_offset").get(h3_ids[0], 0.0)
            blended = current_offset * 0.9 + offset * 0.1
            n_flooded = apply_sea_level(cells, blended, orig_ocean)
            self._kyr_since_last_eustasy = 0.0

            for c in cells:
                ws.get_discrete("sea_level_offset")[c.h3_id] = blended
                ws.get_discrete("geological_type")[c.h3_id] = c.geological_type

            if abs(blended) > 0.1 or n_flooded > 0:
                print(f"  [Eustasy] offset={blended:.1f}m, {n_flooded} cells changed")
            self._ws_save()
        except Exception as e:
            print(f"  [Eustasy] skipped: {e}")

    # ── Diagenesis: lithosphere evolution (geological) ───────────

    def _run_diagenesis_geological(self) -> None:
        """Compaction, cementation, density — every ~0.1 Myr."""
        threshold = 0.1
        if self._myr_since_last_diagenesis < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)
        dt_myr = self._myr_since_last_diagenesis

        try:
            from .layer0.diagenesis import diagenesis_step
            sed = {c.h3_id: getattr(c, 'sediment_thickness', 0.0) for c in cells}

            diagenesis_step(cells, dt_myr=dt_myr,
                            sediment_thickness_change=sed, rng=self._rng)
            self._myr_since_last_diagenesis = 0.0

            for c in cells:
                ws.get_discrete("sediment_thickness")[c.h3_id] = c.sediment_thickness
                ws.get_discrete("crustal_thickness")[c.h3_id] = c.crustal_thickness_km
                for attr in ('porosity', 'cementation', 'bulk_density'):
                    if hasattr(c, attr):
                        ws.get_discrete(attr)[c.h3_id] = getattr(c, attr)

            print(f"  [Diagenesis] advanced {dt_myr:.2f} Myr")
            self._ws_save()
        except Exception as e:
            print(f"  [Diagenesis] skipped: {e}")

    # ── Soil evolution (millennial, continuous) ──────────────────

    def _run_soil_evolution(self) -> None:
        """Time-evolving soil — every ~1 kyr.

        Uses ContinuousSoil with field accessors, no CellData.
        """
        threshold = 0.001
        if self._kyr_since_last_soil < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        if not ws.has_field("temperature"):
            return

        self._soil_time_factor += 1

        try:
            from .layer0.continuous_soil import ContinuousSoil

            # Build field accessors from WS
            temp_f = ws.field("temperature")
            precip_f = ws.field("precipitation")
            elev_f = ws.field("elevation")
            canopy_f = ws.field("canopy_density") if ws.has_field("canopy_density") else None
            geo_f = lambda lat, lon: int(ws.get_discrete("geological_type").get(
                __import__("h3").latlng_to_cell(lat, lon, 2), 2))

            def _bedrock_at(lat, lon):
                import h3 as _h3_b
                hid = _h3_b.latlng_to_cell(lat, lon, 2)
                return ws.get_discrete("bedrock_class").get(hid, "continental_granite")

            def _slope_at(lat, lon):
                import h3 as _h3_s
                hid = _h3_s.latlng_to_cell(lat, lon, 2)
                return float(ws.get_discrete("slope_mag").get(hid, 0.0))

            def _canopy_at(lat, lon):
                if canopy_f is not None:
                    return canopy_f(lat, lon)
                return 0.0

            def _psec_at(lat, lon):
                import h3 as _h3_p
                hid = _h3_p.latlng_to_cell(lat, lon, 2)
                return float(ws.get_discrete("precip_seasonality").get(hid, 0.3))

            cs = ContinuousSoil(
                temperature_f=temp_f,
                precipitation_f=precip_f,
                elevation_f=elev_f,
                canopy_f=_canopy_at,
                bedrock_f=_bedrock_at,
                geo_type_f=geo_f,
                slope_f=_slope_at,
                precip_seas_f=_psec_at,
                time_factor=float(self._soil_time_factor),
            )

            cs.build_fields(ws, h3_ids)
            self._kyr_since_last_soil = 0.0
            self._ws_save()
            print(f"  [Soil] advanced (tf={self._soil_time_factor})")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  [Soil] skipped: {e}")

    # ── Resource evolution (geological) ──────────────────────────

    def _run_resource_evolution(self) -> None:
        """Evolve ore/resource concentrations — every ~0.1 Myr."""
        threshold = 0.1
        if self._myr_since_last_resources < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        dt_myr = self._myr_since_last_resources

        try:
            from .layer0.resource_engine import ResourceEngine
            engine = ResourceEngine(ws, h3_ids)
            engine.advance(dt_myr=dt_myr, rng=self._rng)
            self._myr_since_last_resources = 0.0
            self._ws_save()
            print(f"  [Resources] evolved {dt_myr:.2f} Myr: "
                  f"{engine.field_names()}")
        except Exception as e:
            print(f"  [Resources] skipped: {e}")

    # ── Vegetation succession (geological) ───────────────────────

    def _run_vegetation_succession(self) -> None:
        """PFT succession/migration in response to climate change."""
        threshold = 0.01  # Myr (10 000 years)
        if self._myr_since_last_vegetation < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        dt_myr = self._myr_since_last_vegetation

        try:
            from .layer0.vegetation_engine import VegetationEngine
            veg = VegetationEngine(ws, h3_ids)
            veg.advance(dt_myr=dt_myr)
            self._myr_since_last_vegetation = 0.0
            self._ws_save()
            print(f"  [Vegetation] succession {dt_myr:.4f} Myr")
        except Exception as e:
            print(f"  [Vegetation] skipped: {e}")

    # ── Tectonics: plate motion (geological) ───────────────────────

    def _run_tectonics_geological(self) -> None:
        """Plate motion via Euler polygon rotation — every ~0.1 Myr.

        Loads plate polygons from feature store (type='tectonic_plate'),
        rotates all vertices by Euler vector, reassigns cells via
        point-in-polygon, saves updated polygons back.
        """
        threshold = 0.1  # Myr (100 000 years)
        if self._myr_since_last_tectonics < threshold:
            return

        ws = self._get_ws()
        h3_ids = self._ws_h3_ids()
        if not h3_ids:
            return

        cells = ws.to_celldata(h3_ids)
        h3_ids = [c.h3_id for c in cells]
        dt_myr = self._myr_since_last_tectonics

        try:
            import h3 as _h3_t
            from shapely import wkt
            from .layer0.tectonics.polygon_plates import (
                assign_cells_via_polygons,
                detect_boundaries_from_polygons,
            )

            # Load plate polygons from feature store
            fs = self.db.get_feature_store()
            plate_polygons: dict = {}
            plate_motions: dict = {}
            for feat in fs.all_active:
                if feat.type != "tectonic_plate":
                    continue
                pid = feat.properties.get("plate_id", -1)
                if pid < 0:
                    continue
                if feat.geometry is not None:
                    plate_polygons[pid] = feat.geometry
                mx = float(feat.properties.get("motion_x", 0))
                my = float(feat.properties.get("motion_y", 0))
                mz = float(feat.properties.get("motion_z", 0))
                plate_motions[pid] = (mx, my, mz)

            if not plate_polygons:
                # First run: build polygons from cell assignments
                from .layer0.tectonics.polygon_plates import build_plate_polygons
                assignment = {c.h3_id: c.plate_id for c in cells}
                plate_polygons = build_plate_polygons(h3_ids, assignment)
                # Build plate_motions from cell data
                plate_ids = sorted(set(assignment.values()))
                for pid in plate_ids:
                    rng_p = random.Random(42 + pid)
                    theta = rng_p.uniform(0, 2 * math.pi)
                    phi = rng_p.uniform(0, math.pi)
                    plate_motions[pid] = (
                        math.sin(phi) * math.cos(theta),
                        math.sin(phi) * math.sin(theta),
                        math.cos(phi),
                    )

            # Reconstruct assignment from current cell data
            assignment = {c.h3_id: c.plate_id for c in cells}

            # ── 1. Euler-rotate all plate polygons ────────────────
            from .layer0.tectonics.polygon_plates import euler_rotate_polygon
            for pid, poly in plate_polygons.items():
                if poly is None:
                    continue
                omega = plate_motions.get(pid, (0, 0, 1))
                plate_polygons[pid] = euler_rotate_polygon(
                    poly, omega[0], omega[1], omega[2], dt_myr,
                )

            # ── 2. Reassign cells via point-in-polygon ────────────
            new_assignment = assign_cells_via_polygons(h3_ids, plate_polygons)
            for hid in h3_ids:
                if hid in new_assignment:
                    assignment[hid] = new_assignment[hid]

            # ── 3. Detect boundaries ───────────────────────────────
            (boundary_type, distance_to_boundary,
             _, convergence_velocity) = detect_boundaries_from_polygons(
                h3_ids, assignment, plate_polygons, plate_motions
            )

            # ── Count changes BEFORE updating cells ────────────────
            n_moved = sum(1 for c in cells
                          if assignment.get(c.h3_id, -1) != c.plate_id)
            # ── 4. Update cells from new state ─────────────────────
            for c in cells:
                c.plate_id = assignment.get(c.h3_id, c.plate_id)
                bt = boundary_type.get(c.h3_id, "intraplate")
                if bt != c.boundary_type:
                    c.boundary_type = bt
                c.distance_to_boundary = distance_to_boundary.get(c.h3_id, 999.0)
                # Update crustal age (simple ageing)
                c.crustal_age_myr += dt_myr * 1.0  # all crust ages
                # Reset age at divergent boundaries
                if bt == "divergent" and distance_to_boundary.get(c.h3_id, 999) < 2:
                    c.crustal_age_myr *= 0.3

            # ── 5. Save updated polygons to feature store ──────────
            from shapely.geometry import mapping as _mapping
            for pid, poly in plate_polygons.items():
                if poly is None:
                    continue
                # Find existing feature or create new one
                found = False
                for feat in fs.all_active:
                    if (feat.type == "tectonic_plate"
                            and feat.properties.get("plate_id") == pid):
                        feat.geometry = poly
                        found = True
                        break
                if not found:
                    omega = plate_motions.get(pid, (0, 0, 1))
                    from .layer0.feature_store import Feature
                    new_feat = Feature(
                        feature_id=f"tectonic_plate_{pid}",
                        type="tectonic_plate",
                        geometry=poly,
                        properties={
                            "plate_id": pid,
                            "motion_x": omega[0],
                            "motion_y": omega[1],
                            "motion_z": omega[2],
                        },
                    )
                    fs.add_feature(new_feat)
            self.db.save_features(fs)

            # ── 6. Sync to WorldState (no cells table) ────────────
            for c in cells:
                ws.get_discrete("plate_id")[c.h3_id] = c.plate_id
                ws.get_discrete("crustal_age")[c.h3_id] = c.crustal_age_myr
                ws.get_discrete("crustal_thickness")[c.h3_id] = c.crustal_thickness_km
                ws.get_discrete("thermal_gradient")[c.h3_id] = c.thermal_gradient
                ws.get_discrete("elevation")[c.h3_id] = c.elevation_mean
                ws.get_discrete("geological_type")[c.h3_id] = c.geological_type
                ws.get_discrete("boundary_type")[c.h3_id] = c.boundary_type
                ws.get_discrete("distance_to_boundary")[c.h3_id] = c.distance_to_boundary
            # Register elevation as continuous field
            elev_data = {c.h3_id: c.elevation_mean for c in cells}
            ws.set_field("elevation", elev_data)
            self._ws_save()

            print(f"  [Tectonics] advanced {dt_myr:.4f} Myr: {n_moved} cells changed plate")
        except Exception as e:
            print(f"  [Tectonics] skipped: {e}")


# ======================================================================
# Console command helper
# ======================================================================

_ADVANCE_HELP = """Advance world time.

Usage:
  /advance 7d          — 7 days
  /advance 12h 30m     — 12 hours 30 minutes
  /advance 1d 6h 15m   — 1 day 6 hours 15 minutes
  /advance 30m         — 30 minutes
"""


def parse_advance_args(text: str) -> dict:
    """Parse '/advance 7d 12h 30m' into {'days':7, 'hours':12, 'minutes':30}."""
    import re
    result = {"days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    text = text.strip()
    if text.startswith("/advance"):
        text = text[len("/advance"):].strip()
    for token in text.split():
        token = token.strip().lower()
        m = re.match(r"^(\d+(?:\.\d+)?)([dhms])$", token)
        if m:
            val = float(m.group(1))
            unit = m.group(2)
            if unit == "d":
                result["days"] += val
            elif unit == "h":
                result["hours"] += val
            elif unit == "m":
                result["minutes"] += val
            elif unit == "s":
                result["seconds"] += val
    return result


def format_time(time: dict) -> str:
    """Format world time for display."""
    day = int(time.get("day_of_year", 0))
    hour = time.get("hour", 0)
    h = int(hour)
    m = int((hour - h) * 60)
    y = int(time.get("year", 0))
    return f"Year {y}, Day {day}, {h:02d}:{m:02d}"
