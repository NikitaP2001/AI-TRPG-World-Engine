"""World Database — SQLite-backed persistence for simulation state.

Replaces cells.parquet + features.json with a single ACID database.
Enables time-aware incremental updates (Phase 2+).

Schema:
  world_params  — static generation parameters (axial_tilt, seed, ...)
  world_time    — current simulation time (single row, id=1)
  cells         — one row per H3 cell, static + dynamic fields
  features      — spatial features (rivers, lakes, biomes, ...)
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from shapely import wkt, STRtree
from shapely.geometry import shape, mapping

from .layer0.feature_store import Feature, FeatureStore
from .layer0.cell_model import CellData


# ======================================================================
# SQLite schema
# ======================================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS world_params (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_time (
    id          INTEGER PRIMARY KEY CHECK(id = 1),
    tick        INTEGER DEFAULT 0,
    year        INTEGER DEFAULT 0,
    day_of_year REAL    DEFAULT 0.0,
    hour        REAL    DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS cells (
    h3_id            TEXT PRIMARY KEY,
    lat              REAL NOT NULL,
    lon              REAL NOT NULL,

    -- Static fields (set once at generation)
    elevation        REAL DEFAULT 0.0,
    slope            REAL DEFAULT 0.0,
    bedrock_class    TEXT DEFAULT 'continental_granite',
    geological_type  INTEGER DEFAULT 2,
    is_ocean         INTEGER DEFAULT 0,

    -- Dynamic fields (updated on time advance)
    temperature_c       REAL DEFAULT 15.0,
    temperature_norm    REAL DEFAULT 0.5,
    precipitation_norm  REAL DEFAULT 0.5,
    wind_u              REAL DEFAULT 0.0,
    wind_v              REAL DEFAULT 0.0,
    soil_fertility      REAL DEFAULT 0.3,
    soil_depth          REAL DEFAULT 0.5,
    water_table_depth   REAL DEFAULT 5.0,
    organic_matter      REAL DEFAULT 0.0,
    vegetation_cover    TEXT DEFAULT 'grassland',
    biome_key           TEXT DEFAULT 'grassland',
    climate_class       TEXT DEFAULT '',
    canopy_density      REAL DEFAULT 0.0,
    biomass_kgm2        REAL DEFAULT 0.0,
    runoff_ratio        REAL DEFAULT 0.3,
    effective_precip    REAL DEFAULT 0.15,

    -- Deep geology
    crustal_age         REAL DEFAULT 100.0,
    crustal_thickness   REAL DEFAULT 35.0,
    thermal_gradient    REAL DEFAULT 25.0,

    -- Tracking
    updated_at_tick INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cells_lat     ON cells(lat);
CREATE INDEX IF NOT EXISTS idx_cells_is_ocean ON cells(is_ocean);

CREATE TABLE IF NOT EXISTS features (
    feature_id      TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    name            TEXT DEFAULT '',
    geometry_wkt    TEXT,       -- WKT string
    properties_json TEXT,       -- JSON blob
    is_active       INTEGER DEFAULT 1,
    updated_at_tick INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_features_type   ON features(type);
CREATE INDEX IF NOT EXISTS idx_features_active ON features(is_active);
"""

# ======================================================================
# WorldDB class
# ======================================================================


class WorldDB:
    """SQLite-backed world database.

    Usage:
        db = WorldDB('game/simulation/world.sqlite')
        db.save_cells(cell_list)
        db.save_features(feature_store)
        cells = db.load_cells()
        fs = db.load_features()
        t = db.get_time()
        db.set_time(tick=10, day_of_year=172.0, hour=12.0)
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")   # faster writes
        self.conn.execute("PRAGMA synchronous=NORMAL")  # safe enough
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._feature_store_cache: Optional[FeatureStore] = None

    def close(self) -> None:
        self.conn.close()

    # ── Schema ──────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        self._migrate_schema()

    _COLUMN_DEFAULTS: Dict[str, str] = {
        "climate_class": "TEXT DEFAULT ''",
        "canopy_density": "REAL DEFAULT 0.0",
        "biomass_kgm2": "REAL DEFAULT 0.0",
        "crustal_age": "REAL DEFAULT 100.0",
        "crustal_thickness": "REAL DEFAULT 35.0",
        "thermal_gradient": "REAL DEFAULT 25.0",
        "runoff_ratio": "REAL DEFAULT 0.3",
        "effective_precip": "REAL DEFAULT 0.15",
        "updated_at_tick": "INTEGER DEFAULT 0",
        # Missing fields that CellData has but weren't in schema
        "precip_seasonality": "REAL DEFAULT 0.3",
        "hazard_level": "REAL DEFAULT 0.0",
        "tectonic_stress": "REAL DEFAULT 0.0",
        "clay_content": "REAL DEFAULT 0.0",
        "sand_content": "REAL DEFAULT 0.0",
        "silt_content": "REAL DEFAULT 0.0",
        "soil_ph": "REAL DEFAULT 7.0",
        "cation_exchange": "REAL DEFAULT 5.0",
        "interception_coefficient": "REAL DEFAULT 0.15",
        "slope_dir": "REAL DEFAULT 0.0",
        "elevation_variance": "REAL DEFAULT 0.0",
    }

    def _migrate_schema(self) -> None:
        """Add missing columns to existing tables (schema evolution)."""
        existing = {
            r[1]
            for r in self.conn.execute("PRAGMA table_info(cells)").fetchall()
        }
        for col, decl in self._COLUMN_DEFAULTS.items():
            if col not in existing:
                try:
                    self.conn.execute(
                        f"ALTER TABLE cells ADD COLUMN {col} {decl}"
                    )
                    print(f"[WorldDB] Added missing column: {col}")
                except Exception as e:
                    print(f"[WorldDB] Could not add {col}: {e}")
        self.conn.commit()

    # ── Parameters ──────────────────────────────────────────────────

    def set_params(self, **kwargs: Any) -> None:
        """Store world generation parameters."""
        cur = self.conn.cursor()
        cur.executemany(
            "INSERT OR REPLACE INTO world_params (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in kwargs.items()],
        )
        self.conn.commit()

    def get_params(self) -> Dict[str, str]:
        """Return all params as {key: value} dict (values are strings)."""
        cur = self.conn.execute("SELECT key, value FROM world_params")
        return {row["key"]: row["value"] for row in cur.fetchall()}

    def get_param(self, key: str, default: str = "") -> str:
        cur = self.conn.execute("SELECT value FROM world_params WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    # ── Time ────────────────────────────────────────────────────────

    def init_time(self, tick: int = 0, year: int = 0,
                  day_of_year: float = 0.0, hour: float = 0.0) -> None:
        """Insert (or reset) the single world_time row."""
        self.conn.execute(
            "INSERT OR REPLACE INTO world_time (id, tick, year, day_of_year, hour) "
            "VALUES (1, ?, ?, ?, ?)",
            (tick, year, day_of_year, hour),
        )
        self.conn.commit()

    def get_time(self) -> Dict[str, Any]:
        """Return current time dict, or defaults if none set."""
        cur = self.conn.execute("SELECT * FROM world_time WHERE id=1")
        row = cur.fetchone()
        if row is None:
            return {"tick": 0, "year": 0, "day_of_year": 0.0, "hour": 0.0}
        return dict(row)

    def set_time(self, tick: int = 0, year: int = 0,
                 day_of_year: float = 0.0, hour: float = 0.0) -> None:
        self.conn.execute(
            "UPDATE world_time SET tick=?, year=?, day_of_year=?, hour=? WHERE id=1",
            (tick, year, day_of_year, hour),
        )
        self.conn.commit()

    # ── Cells ───────────────────────────────────────────────────────

    def save_cells(self, cells: List[CellData]) -> None:
        """Bulk upsert all cells into the database."""
        cur = self.conn.cursor()
        cur.executemany(
            """INSERT OR REPLACE INTO cells (
                h3_id, lat, lon,
                elevation, slope, bedrock_class, geological_type, is_ocean,
                temperature_c, temperature_norm, precipitation_norm,
                wind_u, wind_v,
                soil_fertility, soil_depth, water_table_depth,
                organic_matter, vegetation_cover, biome_key, climate_class,
                canopy_density, biomass_kgm2,
                runoff_ratio, effective_precip,
                crustal_age, crustal_thickness, thermal_gradient,
                updated_at_tick,
                precip_seasonality, hazard_level, tectonic_stress,
                clay_content, sand_content, silt_content,
                soil_ph, cation_exchange, interception_coefficient,
                slope_dir, elevation_variance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [self._cell_to_row(c) for c in cells],
        )
        self.conn.commit()

    def _cell_to_row(self, c: CellData) -> tuple:
        import h3
        latlng = h3.cell_to_latlng(c.h3_id)
        from .layer0.climate import norm_to_c
        temp_norm = getattr(c, 'temperature', 0.5)
        temp_c = norm_to_c(temp_norm)
        pw = getattr(c, 'prevailing_wind', (0.0, 0.0)) or (0.0, 0.0)
        sl = getattr(c, 'slope', (0.0, 0.0)) or (0.0, 0.0)
        return (
            c.h3_id,
            latlng[0], latlng[1],
            getattr(c, 'elevation_mean', 0.0),
            sl[0],                       # slope magnitude
            getattr(c, 'bedrock_class', 'continental_granite'),
            getattr(c, 'geological_type', 2),
            1 if getattr(c, 'elevation_mean', 0) < -0.01 else 0,
            temp_c,
            temp_norm,
            getattr(c, 'precipitation', 0.5),
            float(pw[0]), float(pw[1]),
            getattr(c, 'soil_fertility', 0.5),
            getattr(c, 'soil_depth', 0.5),
            getattr(c, 'water_table_depth', 5.0),
            getattr(c, 'organic_matter', 0.0),
            getattr(c, 'vegetation_cover', 'grassland'),
            '',
            getattr(c, 'climate_class', ''),
            getattr(c, 'canopy_density', 0.0),
            getattr(c, 'biomass_kgm2', 0.0),
            getattr(c, 'runoff_ratio', 0.5),
            getattr(c, 'effective_precip', 0.0),
            getattr(c, 'crustal_age_myr', 100.0),
            getattr(c, 'crustal_thickness_km', 35.0),
            getattr(c, 'thermal_gradient', 25.0),
            0,
            # New columns
            getattr(c, 'precip_seasonality', 0.3),
            getattr(c, 'hazard_level', 0.0),
            getattr(c, 'tectonic_stress', 0.0),
            getattr(c, 'clay_content', 0.0),
            getattr(c, 'sand_content', 0.0),
            getattr(c, 'silt_content', 0.0),
            getattr(c, 'soil_ph', 7.0),
            getattr(c, 'cation_exchange', 5.0),
            getattr(c, 'interception_coefficient', 0.15),
            sl[1],                       # slope direction (radians)
            getattr(c, 'elevation_variance', 0.0),
        )

    def load_cells(self) -> List[Dict[str, Any]]:
        """Load all cells as a list of dicts."""
        cur = self.conn.execute("SELECT * FROM cells")
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def load_cells_as_celldata(self) -> List[CellData]:
        """Load cells into CellData objects — ALL fields."""
        from .layer0.climate import norm_to_c
        rows = self.load_cells()
        cells = []
        for r in rows:
            c = CellData(h3_id=r["h3_id"], resolution=2)
            c.elevation_mean = r["elevation"]
            c.elevation_variance = r.get("elevation_variance", 0.0)
            c.slope = (r["slope"], r.get("slope_dir", 0.0)) if r["slope"] else (0.0, 0.0)
            c.bedrock_class = r["bedrock_class"]
            c.geological_type = r["geological_type"]
            # Temperature: prefer norm if available
            tn = r["temperature_norm"]
            if tn is None:
                tn = (r["temperature_c"] + 5.0) / 45.0 if r["temperature_c"] else 0.5
            c.temperature = tn
            c.precipitation = r["precipitation_norm"] or 0.5
            c.precip_seasonality = r.get("precip_seasonality", 0.3)
            c.prevailing_wind = (r.get("wind_u", 0.0), r.get("wind_v", 0.0))
            c.soil_fertility = r["soil_fertility"] or 0.5
            c.soil_depth = r["soil_depth"] or 0.5
            c.water_table_depth = r.get("water_table_depth") or 5.0
            c.organic_matter = r["organic_matter"] or 0.0
            c.vegetation_cover = r["vegetation_cover"] or "barren"
            c.climate_class = r.get("climate_class", "") or ""
            c.canopy_density = r.get("canopy_density") or 0.0
            c.biomass_kgm2 = r.get("biomass_kgm2") or 0.0
            c.runoff_ratio = r.get("runoff_ratio") or 0.5
            c.effective_precip = r.get("effective_precip") or 0.0
            c.hazard_level = r.get("hazard_level") or 0.0
            c.tectonic_stress = r.get("tectonic_stress") or 0.0
            c.clay_content = r.get("clay_content") or 0.0
            c.sand_content = r.get("sand_content") or 0.0
            c.silt_content = r.get("silt_content") or 0.0
            c.soil_ph = r.get("soil_ph") or 7.0
            c.cation_exchange = r.get("cation_exchange") or 5.0
            c.interception_coefficient = r.get("interception_coefficient") or 0.15
            c.crustal_age_myr = r.get("crustal_age") or 100.0
            c.crustal_thickness_km = r.get("crustal_thickness") or 35.0
            c.thermal_gradient = r.get("thermal_gradient") or 25.0
            cells.append(c)
        return cells

    # ── Features ────────────────────────────────────────────────────

    def save_features(self, fs: FeatureStore) -> None:
        """Write all active features to the database."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM features")  # simple: flush & rewrite
        for f in fs.all_active:
            geom_wkt = wkt.dumps(f.geometry) if f.geometry else None
            cur.execute(
                "INSERT INTO features (feature_id, type, name, "
                "geometry_wkt, properties_json, is_active, updated_at_tick) "
                "VALUES (?, ?, ?, ?, ?, 1, 0)",
                (f.feature_id, f.type, f.name, geom_wkt,
                 json.dumps(f.properties, ensure_ascii=False)),
            )
        self.conn.commit()

    def load_features(self) -> FeatureStore:
        """Load features from DB into a FeatureStore (with STRtree)."""
        fs = FeatureStore()
        cur = self.conn.execute("SELECT * FROM features WHERE is_active=1")
        for row in cur.fetchall():
            geom = wkt.loads(row["geometry_wkt"]) if row["geometry_wkt"] else None
            feat = Feature(
                type=row["type"],
                feature_id=row["feature_id"],
                name=row["name"] or "",
                geometry=geom,
                properties=json.loads(row["properties_json"] or "{}"),
            )
            fs.add_feature(feat)
        self._feature_store_cache = fs
        return fs

    def get_feature_store(self) -> Optional[FeatureStore]:
        """Return cached feature store, loading if needed."""
        if self._feature_store_cache is None:
            self._feature_store_cache = self.load_features()
        return self._feature_store_cache

    # ── Convenience ─────────────────────────────────────────────────

    def cell_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM cells")
        return cur.fetchone()["n"]

    def feature_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM features WHERE is_active=1")
        return cur.fetchone()["n"]

    def __repr__(self) -> str:
        return f"WorldDB({self.path!r}, cells={self.cell_count()}, features={self.feature_count()})"
