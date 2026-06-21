"""World Database — SQLite-backed persistence for simulation state.

Schema:
  world_params  — static generation parameters (axial_tilt, seed, ...)
  world_time    — current simulation time (single row, id=1)
  features      — spatial features (rivers, lakes, biomes, ...)
  fauna_populations — fauna density per cell per species
  field_data    — continuous field samples (lat, lon, value)
  cell_discrete — numeric discrete cell data
  cell_discrete_text — string discrete cell data

NOTE: Cell data is stored via WorldState (field_data/cell_discrete tables),
NOT via a legacy cells table. Use world_state_db.save_world_state() /
load_world_state() instead of save_cells()/load_cells().
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

CREATE TABLE IF NOT EXISTS fauna_populations (
    h3_id        TEXT NOT NULL,
    species_id   TEXT NOT NULL,
    density      REAL DEFAULT 0.0,
    lat          REAL DEFAULT 0.0,
    lon          REAL DEFAULT 0.0,
    updated_at_tick INTEGER DEFAULT 0,
    PRIMARY KEY (h3_id, species_id)
);

CREATE INDEX IF NOT EXISTS idx_fauna_species ON fauna_populations(species_id);
"""

# ======================================================================
# WorldDB class
# ======================================================================


class WorldDB:
    """SQLite-backed world database.

    Usage:
        db = WorldDB('game/simulation/world.sqlite')
        db.save_features(feature_store)
        fs = db.load_features()
        t = db.get_time()
        db.set_time(tick=10, day_of_year=172.0, hour=12.0)
        # Cell data is saved via world_state_db.save_world_state()
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")   # faster writes
        self.conn.execute("PRAGMA synchronous=NORMAL")  # safe enough
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        self._feature_store_cache: Optional[FeatureStore] = None

    def close(self) -> None:
        self.conn.close()

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

    # ── Fauna Populations ────────────────────────────────────────────

    def save_fauna_populations(self, rows: List[Dict[str, Any]]) -> None:
        """Bulk upsert fauna population rows."""
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(
            """INSERT OR REPLACE INTO fauna_populations
               (h3_id, species_id, density, lat, lon, updated_at_tick)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (r["h3_id"], r["species_id"], float(r.get("density", 0.0)),
                 float(r.get("lat", 0.0)), float(r.get("lon", 0.0)),
                 int(r.get("updated_at_tick", 0)))
                for r in rows
            ],
        )
        self.conn.commit()

    def load_fauna_populations(self, species_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load fauna population rows.

        Args:
            species_id: If provided, filter to one species. None = all.

        Returns:
            List of dicts with h3_id, species_id, density, updated_at_tick.
        """
        import h3 as _h3
        if species_id:
            cur = self.conn.execute(
                "SELECT * FROM fauna_populations WHERE species_id=? AND density>0",
                (species_id,),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM fauna_populations WHERE density>0"
            )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            # Resolve lat/lon from h3_id for emergence/display
            try:
                latlng = _h3.cell_to_latlng(d["h3_id"])
                d["lat"] = latlng[0]
                d["lon"] = latlng[1]
            except Exception:
                d["lat"] = 0.0
                d["lon"] = 0.0
            rows.append(d)
        return rows

    # ── Convenience ─────────────────────────────────────────────────

    def cell_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM cells")
        return cur.fetchone()["n"]

    def feature_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS n FROM features WHERE is_active=1")
        return cur.fetchone()["n"]

    def __repr__(self) -> str:
        return f"WorldDB({self.path!r}, cells={self.cell_count()}, features={self.feature_count()})"
