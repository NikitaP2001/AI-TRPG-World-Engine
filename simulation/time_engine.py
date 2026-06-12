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

        # Break into hourly steps for temperature updates
        n_steps = max(1, int(total_hours))
        dt_hours = total_hours / n_steps

        for _ in range(n_steps):
            self._step(dt_hours)
            time = self.db.get_time()

        # ── L1 daily step: run causal features for total days ──
        total_days = max(1, int(days + hours / 24 + minutes / 1440))
        self._run_l1_step(dt_days=total_days)

        # ── Long-cycle processes at intervals ──
        self._accumulated_days += total_days
        self._run_long_cycle()

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
        self._update_temperature(new_day, new_hour)

    def _update_temperature(self, day_of_year: float, hour: float) -> None:
        """Recompute temperature for every cell using instant_temperature()."""
        cells = self.db.load_cells()
        if not cells:
            return

        params = self.db.get_params()
        axial_tilt = float(params.get("axial_tilt", "23.44"))

        updates = []
        for c in cells:
            lat = c["lat"]
            lon = c["lon"]
            elev = c["elevation"]
            is_ocean = bool(c["is_ocean"])
            coastal = False
            if not is_ocean and abs(lat) < 60:
                coastal = True

            t_c = instant_temperature(
                lat_deg=lat, elevation=elev,
                is_ocean=is_ocean, coastal=coastal,
                day_of_year=day_of_year, hour=hour,
                axial_tilt=axial_tilt,
            )
            t_norm = min(1.0, (t_c + 5.0) / 45.0)
            updates.append((t_c, t_norm, c["h3_id"]))

        cur = self.db.conn.cursor()
        cur.executemany(
            "UPDATE cells SET temperature_c=?, temperature_norm=?, "
            "updated_at_tick=COALESCE(updated_at_tick,0)+1 WHERE h3_id=?",
            updates,
        )
        self.db.conn.commit()

    def _run_l1_step(self, dt_days: float) -> None:
        """Run one L1 tick: lakes, groundwater, wetlands, vegetation,
        fauna, settlement footprints, emergence.

        Loads current state from SQLite, builds FieldRegistry,
        runs SimEngine features, saves back.
        """
        # No L1 to run if sub-daily advance
        if dt_days < 0.5:
            return

        # 1. Load cells as CellData + features from SQLite
        cells = self.db.load_cells_as_celldata()
        if not cells:
            return

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

        # 3. Load fauna population_density fields from DB into MutableFields
        fauna_rows_all = self.db.load_fauna_populations()
        fauna_by_species: dict = {}
        for row in fauna_rows_all:
            sid = row["species_id"]
            if sid not in fauna_by_species:
                fauna_by_species[sid] = []
            fauna_by_species[sid].append(row)

        import h3 as _h3_fauna
        for species_id, rows in fauna_by_species.items():
            field_name = f"population_density[{species_id}]"
            if fields.has(field_name):
                try:
                    mf = fields.get_mutable(field_name)
                except KeyError:
                    continue
                # Set field values via sample points
                for r in rows:
                    try:
                        latlng = _h3_fauna.cell_to_latlng(r["h3_id"])
                        lat, lon = float(latlng[0]), float(latlng[1])
                        density = max(0.0, float(r.get("density", 0.0)))
                        if density > 1e-8:
                            mf.add_persistent(lat, lon, radius_deg=2.0, strength=density)
                    except Exception:
                        pass

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

        # 6. Compute plankton_density from water temperature (for aquatic species)
        import h3 as _h3_plank
        plank_mf = None
        try:
            plank_mf = fields.get_mutable("plankton_density")
        except KeyError:
            pass
        if plank_mf is not None:
            for cell in cells:
                latlng = _h3_plank.cell_to_latlng(cell.h3_id)
                lat, lon = float(latlng[0]), float(latlng[1])
                elev_f = fields.get("elevation_mean")
                elev = elev_f(lat, lon) if elev_f else 0.0
                if elev >= 0:
                    continue  # land — no plankton
                temp_f = fields.get("temperature")
                temp = max(0.0, min(1.0, temp_f(lat, lon))) if temp_f else 0.5
                # Plankton blooms at moderate temps (0.3-0.7 norm = ~8-26°C)
                plank = 0.0
                if 0.1 < temp < 0.9:
                    plank = math.sin(math.pi * (temp - 0.1) / 0.8) * 0.15
                if plank > 1e-6:
                    plank_mf.add_persistent(lat, lon, radius_deg=2.0, strength=plank)

        # 7. Add Fauna features (one per registered species)
        for species_id in get_species_ids():
            engine.add_feature(Fauna(species_id))

        # 7. Run for dt_days
        engine.step(dt=float(dt_days))

        # 8. Propagate mutable field state back to CellData
        import h3 as _h3_prop
        wt_f = fields.get("water_table_depth")
        canopy_f = fields.get("canopy_density")
        biomass_f = fields.get("biomass")
        soil_mut = fields.get("soil_fertility")
        for cell in cells:
            latlng = _h3_prop.cell_to_latlng(cell.h3_id)
            lat, lon = float(latlng[0]), float(latlng[1])
            cell.water_table_depth = max(0.0, wt_f(lat, lon))
            cell.canopy_density = max(0.0, min(1.0, canopy_f(lat, lon)))
            cell.biomass_kgm2 = max(0.0, biomass_f(lat, lon))
            cell.soil_fertility = max(0.0, min(1.0, soil_mut(lat, lon)))

        # 9. Apply settlement footprint — accumulating deltas to CellData
        for feat in engine.features:
            if feat.feature_type == "settlement_footprint":
                sf = feat
                sf.apply_to_cells(cells, dt=float(dt_days))

        # 10. Save fauna population_density fields to DB
        fauna_save_rows = []
        for species_id in get_species_ids():
            field_name = f"population_density[{species_id}]"
            try:
                pop_mf = fields.get_mutable(field_name)
            except KeyError:
                continue
            current_tick = 0
            time = self.db.get_time()
            if time:
                current_tick = time.get("tick", 0)

            # Apply settlement hunting/suppression deltas
            for sf_feat in engine.features:
                if sf_feat.feature_type != "settlement_footprint":
                    continue
                deltas = sf_feat.props.get("_hunting_deltas", {})
                if not deltas:
                    continue
                # Write hunting deltas to population_density fields
                for h3_id_str, delta in deltas.items():
                    try:
                        latlng = _h3_prop.cell_to_latlng(h3_id_str)
                        lat, lon = float(latlng[0]), float(latlng[1])
                        pop_mf.add_persistent(lat, lon, radius_deg=2.0, strength=delta)
                    except Exception:
                        pass

            # Sample field to get per-cell densities
            for cell in cells:
                latlng = _h3_prop.cell_to_latlng(cell.h3_id)
                lat, lon = float(latlng[0]), float(latlng[1])
                density = max(0.0, pop_mf(lat, lon))
                if density > 1e-6:
                    fauna_save_rows.append({
                        "h3_id": cell.h3_id,
                        "species_id": species_id,
                        "density": density,
                        "updated_at_tick": current_tick,
                    })

        self.db.save_fauna_populations(fauna_save_rows)

        # 11. Check fauna emergence → feed WM notification queue
        emergence_events = check_fauna_emergence(
            fauna_save_rows,
            current_tick=time.get("tick", 0) if time else 0,
        )
        if emergence_events:
            for ev in emergence_events:
                print(f"  [Emergence] {ev['faction_name']} ({ev['faction_id']}) "
                      f"— {ev['total_population']:.0f} individuals")

        # 12. Compute encounter_probability → hazard_level feedback
        # encounter_probability = Σ(pop_density/density_max × hazard_weight)
        # for carnivore/dangerous species, added to hazard_level
        from .layer1.fauna_registry import get_hazard_weight
        for cell in cells:
            latlng = _h3_prop.cell_to_latlng(cell.h3_id)
            lat, lon = float(latlng[0]), float(latlng[1])
            encounter = 0.0
            for species_id in get_species_ids():
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
            # Blend encounter probability into hazard_level (small additive)
            cell.hazard_level = max(0.0, min(1.0, cell.hazard_level + encounter * 0.05))

        # 13. Save updated cells to SQLite
        self.db.save_cells(cells)

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
        """Return a summary dict of current world state."""
        cells = self.db.load_cells()
        temps = [c["temperature_c"] for c in cells] if cells else [0]
        return {
            "tick": time.get("tick", 0),
            "year": time.get("year", 0),
            "day_of_year": time.get("day_of_year", 0),
            "hour": time.get("hour", 0),
            "temp_min": min(temps) if temps else 0,
            "temp_max": max(temps) if temps else 0,
            "temp_mean": sum(temps) / len(temps) if temps else 0,
        }

    # ── Long-cycle processes (slow planetary changes) ──────────────

    def _run_long_cycle(self) -> None:
        """Run long-cycle planetary processes at configurable intervals.

        Climate drift: ~100d — random walk temperature/precipitation.
        Resource flux: ~365d — Gray-Scott resource evolution.
        Geological events: ~1000d — tectonic stress → earthquakes.
        """
        acc = self._accumulated_days

        if acc < _CLIMATE_DRIFT_INTERVAL:
            return  # nothing to do yet

        cells = self.db.load_cells_as_celldata()
        if not cells:
            return

        # Climate drift at ~100d intervals
        if acc >= _CLIMATE_DRIFT_INTERVAL:
            from .layer0.long_cycle import _drift_climate
            n = _drift_climate(cells, drift_rate=0.003, rng=self._rng)
            if n > 0:
                print(f"  [LongCycle] climate drift: {n} cells changed class")
            self._accumulated_days -= _CLIMATE_DRIFT_INTERVAL

        # Resource evolution at ~365d intervals
        if acc >= _RESOURCE_EVOLVE_INTERVAL:
            from .layer0.long_cycle import _evolve_resources
            from .layer0.resources import default_resource_types
            rtypes = default_resource_types()
            _evolve_resources(cells, rtypes, steps=5, rng=self._rng)
            self._accumulated_days -= _RESOURCE_EVOLVE_INTERVAL

        # Geological events at ~1000d intervals
        if acc >= _GEOLOGICAL_EVENT_INTERVAL:
            from .layer0.long_cycle import _check_geological_events
            events = _check_geological_events(
                cells, stress_accumulation_rate=0.01,
                event_threshold=0.8, rng=self._rng,
            )
            if events:
                print(f"  [LongCycle] {len(events)} geological event(s)")
            self._accumulated_days -= _GEOLOGICAL_EVENT_INTERVAL

        # Save any cell modifications from long-cycle
        self.db.save_cells(cells)


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
