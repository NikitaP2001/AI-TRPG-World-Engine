"""Time Engine — advances world simulation state.

Reads current time from SQLite, updates temperature (hourly) and
L1 causal features (daily: lakes, groundwater, wetlands).

Usage:
    from simulation.time_engine import TimeEngine
    engine = TimeEngine("game/simulation/world.sqlite")
    engine.advance(days=7, hours=12)
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from .world_db import WorldDB
from .layer0.climate import (
    instant_temperature,
    _solar_declination,
    _day_length_hours,
    _diurnal_amplitude,
    _cos_zenith_angle,
)


class TimeEngine:
    """Drives world simulation forward in time.

    Each advance() call:
      1. Moves world_time forward (handles day/year rollover)
      2. Recomputes temperature for every cell (hourly, seasonal+diurnal)
      3. Runs L1 causal features (daily: lakes, groundwater, wetlands)
      4. Writes updated cells + features to SQLite
    """

    def __init__(self, db: WorldDB):
        self.db = db

    # ── Public API ──────────────────────────────────────────────────

    def advance(self, days: float = 0, hours: float = 0,
                minutes: float = 0, seconds: float = 0) -> dict:
        """Advance world time and update all time-dependent fields.

        Hourly: temperature (instant_temperature with diurnal cycle).
        Daily:  L1 features (lake water balance, groundwater, wetland peat).

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
        """Run one L1 tick: lakes, groundwater, wetlands.

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

        fields = FieldRegistry.from_cells(cells)
        engine = SimEngine(fields)

        # 3. Add Vegetation (L0→L1 migration: continuous PFT updates)
        engine.add_feature(Vegetation())

        # 4. Reconstruct L1 features from feature store
        has_groundwater = False
        for feat in fs.all_active:
            if feat.type == "lake" and feat.geometry is not None:
                spill = feat.properties.get("spill_elevation", 0.1)
                lake = Lake(polygon=feat.geometry, spill_elevation=spill,
                            feature_id=feat.feature_id)
                # Restore persisted state
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

        # 5. Run for dt_days
        engine.step(dt=float(dt_days))

        # 6. Propagate mutable field state back to CellData
        canopy_f = fields.get_mutable("canopy_density")
        biomass_f = fields.get_mutable("biomass")
        soil_mut = fields.get_mutable("soil_fertility")
        import h3
        for cell in cells:
            latlng = h3.cell_to_latlng(cell.h3_id)
            lat, lon = float(latlng[0]), float(latlng[1])
            cell.canopy_density = max(0.0, min(1.0, canopy_f(lat, lon)))
            cell.biomass_kgm2 = max(0.0, biomass_f(lat, lon))
            cell.soil_fertility = max(0.0, min(1.0, soil_mut(lat, lon)))

        # 7. Save updated state back to SQLite
        self.db.save_cells(cells)

        # 9. Update feature store with new L1 state
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
