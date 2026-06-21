"""WorldState persistence — save/load WorldState to/from SQLite.

Replaces parts of WorldDB for the cells table.
ContinuousFields are stored as (field_name, lat, lon, value) rows.
FeatureStore and params use existing WorldDB tables.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import shape, mapping
from shapely import wkt

from .world_state import WorldState
from .layer1.fields import ContinuousField
from .layer0.feature_store import Feature, FeatureStore


# ======================================================================
# Save WorldState to WorldDB
# ======================================================================


def save_world_state(db, ws: WorldState) -> None:
    """Save all WorldState data to an open WorldDB connection."""

    # ── 1. Save continuous fields ────────────────────────────────
    cur = db.conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS field_data (
            field_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (field_name, lat, lon)
        )
    """)
    cur.execute("DELETE FROM field_data")

    for fname in ("elevation", "temperature", "precipitation",
                  "soil_fertility", "soil_depth",
                  "water_table_depth", "canopy_density", "biomass_kgm2",
                  "crustal_age", "crustal_thickness", "thermal_gradient",
                  "sediment_thickness", "porosity", "bulk_density", "cementation",
                  "sea_level_offset", "hazard_level", "organic_matter",
                  "soil_ph", "cation_exchange",
                  "clay_content", "sand_content", "silt_content",
                  "interception_coefficient", "precip_seasonality"):
        if not ws.has_field(fname):
            continue
        fa = ws.field(fname)
        # Sample at a regular grid or use stored discrete points
        # For now, use the discrete field data if available
        dmap = ws.get_discrete(fname)
        if dmap:
            import h3 as _h3
            rows = []
            for hid, val in dmap.items():
                if val is None:
                    continue
                latlng = _h3.cell_to_latlng(hid)
                rows.append((fname, latlng[0], latlng[1], float(val)))
            cur.executemany(
                "INSERT OR REPLACE INTO field_data VALUES (?, ?, ?, ?)",
                rows,
            )
    # Save any additional fields (population_density[species_id], etc.)
    _saved_above = {
        "elevation", "temperature", "precipitation",
        "soil_fertility", "soil_depth",
        "water_table_depth", "canopy_density", "biomass_kgm2",
        "crustal_age", "crustal_thickness", "thermal_gradient",
        "sediment_thickness", "porosity", "bulk_density", "cementation",
        "sea_level_offset", "hazard_level", "organic_matter",
        "soil_ph", "cation_exchange",
        "clay_content", "sand_content", "silt_content",
        "interception_coefficient", "precip_seasonality",
    }
    for fname in list(ws._fields._base_fields.keys()):
        if fname in _saved_above:
            continue  # already saved above
        dmap = ws.get_discrete(fname)
        if not dmap:
            continue
        import h3 as _h3
        rows = []
        for hid, val in dmap.items():
            if val is None:
                continue
            latlng = _h3.cell_to_latlng(hid)
            rows.append((fname, latlng[0], latlng[1], float(val)))
        if rows:
            cur.executemany(
                "INSERT OR REPLACE INTO field_data VALUES (?, ?, ?, ?)",
                rows,
            )

    # ── 1b. Save discrete-only fields (integers, strings) ────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cell_discrete (
            field_name TEXT NOT NULL,
            h3_id TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (field_name, h3_id)
        )
    """)
    cur.execute("DELETE FROM cell_discrete")
    # Numeric discrete fields
    _NUMERIC_DISCRETE = (
        "geological_type", "plate_id",
        "hazard_level", "distance_to_boundary",
        "runoff_ratio", "effective_precip",
        "clay_content", "sand_content", "silt_content",
        "soil_ph", "cation_exchange", "organic_matter",
        "interception_coefficient", "precip_seasonality",
        "slope_mag", "sediment_thickness", "porosity",
        "bulk_density", "cementation", "sea_level_offset",
    )
    for dname in _NUMERIC_DISCRETE:
        dmap = ws.get_discrete(dname)
        if not dmap:
            continue
        rows = [(dname, hid, float(v))
                for hid, v in dmap.items() if v is not None]
        if rows:
            cur.executemany(
                "INSERT OR REPLACE INTO cell_discrete VALUES (?, ?, ?)", rows,
            )

    # String discrete fields
    _STRING_DISCRETE = ("boundary_type", "climate_class",
                         "bedrock_class", "vegetation_cover")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cell_discrete_text (
            field_name TEXT NOT NULL,
            h3_id TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (field_name, h3_id)
        )
    """)
    for dname in _STRING_DISCRETE:
        cur.execute("DELETE FROM cell_discrete_text WHERE field_name=?", (dname,))
        dmap = ws.get_discrete(dname)
        if not dmap:
            continue
        cur.executemany(
            "INSERT OR REPLACE INTO cell_discrete_text VALUES (?, ?, ?)",
            [(dname, hid, v) for hid, v in dmap.items()],
        )

    # ── 2. Save time (use init_time for INSERT OR REPLACE) ───────
    t = ws.time
    db.init_time(
        tick=t.get("tick", 0),
        year=t.get("year", 0),
        day_of_year=t.get("day_of_year", 0.0),
        hour=t.get("hour", 0.0),
    )

    # ── 3. Save params ───────────────────────────────────────────
    db.set_params(**ws.params)

    # ── 4. Save features ─────────────────────────────────────────
    db.save_features(ws.features)

    # ── 5. Save discrete fields as field_data rows ───────────────
    # (already done in the loop above)

    db.conn.commit()


# ======================================================================
# Load WorldState from WorldDB
# ======================================================================


def load_world_state(db) -> WorldState:
    """Load WorldState from an open WorldDB connection."""
    ws = WorldState()

    # ── 1. Load continuous fields from field_data table ──────────
    cur = db.conn.cursor()
    try:
        cur.execute("SELECT DISTINCT field_name FROM field_data")
        field_names = [r[0] for r in cur.fetchall()]
    except Exception:
        field_names = []

    for fname in field_names:
        cur.execute(
            "SELECT lat, lon, value FROM field_data WHERE field_name=?",
            (fname,),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        points = []
        values = []
        import math
        for lat, lon, val in rows:
            lat_r = math.radians(lat)
            lon_r = math.radians(lon)
            points.append([
                math.cos(lat_r) * math.cos(lon_r),
                math.sin(lat_r),
                math.cos(lat_r) * math.sin(lon_r),
            ])
            values.append(float(val))
        tree = cKDTree(np.array(points, dtype=np.float64))
        cf = ContinuousField(tree, np.array(values, dtype=np.float64))
        ws._fields.register_base(fname, cf)

    # ── 1b. Populate discrete fields from field_data ────────────
    for fname in field_names:
        cur.execute(
            "SELECT lat, lon, value FROM field_data WHERE field_name=?",
            (fname,),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        # Also store lat/lon for H3 lookup if we have cell centroids
        import h3 as _h3_fd
        dmap = ws.get_discrete(fname)
        for lat, lon, val in rows:
            try:
                hid = _h3_fd.latlng_to_cell(lat, lon, 2)
                dmap[hid] = float(val)
            except Exception:
                pass

    # ── 1c. Load discrete fields from cell_discrete table ────────
    try:
        cur.execute("SELECT DISTINCT field_name FROM cell_discrete")
        disc_names = [r[0] for r in cur.fetchall()]
        for dname in disc_names:
            cur.execute(
                "SELECT h3_id, value FROM cell_discrete WHERE field_name=?",
                (dname,),
            )
            dmap = ws.get_discrete(dname)
            for hid, val in cur.fetchall():
                dmap[hid] = float(val)
    except Exception:
        pass
    try:
        cur.execute("SELECT DISTINCT field_name FROM cell_discrete_text")
        text_names = [r[0] for r in cur.fetchall()]
        for dname in text_names:
            cur.execute(
                "SELECT h3_id, value FROM cell_discrete_text WHERE field_name=?",
                (dname,),
            )
            dmap = ws.get_discrete(dname)
            for hid, val in cur.fetchall():
                dmap[hid] = val
    except Exception:
        pass

    # ── 2. Load time ─────────────────────────────────────────────
    t = db.get_time()
    if t:
        ws._time = dict(t)

    # ── 3. Load params ───────────────────────────────────────────
    ws._params = dict(db.get_params())

    # ── 4. Load features ─────────────────────────────────────────
    fs = db.get_feature_store()
    if fs:
        ws._features = fs

    # ── 5. Rebuild discrete fields from field_data if needed ─────

    return ws


# ======================================================================
# Save generated world (post-generation pipeline)
# ======================================================================


def save_generated_world(
    db,
    cells,
    feature_store,
    params_dict: dict,
    time: Optional[dict] = None,
    fauna_fields: Optional[Dict[str, Dict[str, float]]] = None,
) -> None:
    """Save generator output as WorldState (field_data + features + params).

    Call AFTER generate_world() and BEFORE any advance() call.
    Replaces the old pattern: save_cells() + save_features() + set_params().
    """
    from .world_state import WorldState

    ws = WorldState()
    ws._params = dict(params_dict)
    ws._time = dict(time or {"tick": 0, "year": 0, "day_of_year": 0.0, "hour": 0.0})
    if feature_store:
        ws._features = feature_store

    # Build field dicts from CellData list
    field_dicts: dict = {}
    for c in cells:
        hid = c.h3_id
        for attr, fname in [
            ("elevation_mean", "elevation"),
            ("temperature", "temperature"),
            ("precipitation", "precipitation"),
            ("soil_fertility", "soil_fertility"),
            ("crustal_age_myr", "crustal_age"),
            ("crustal_thickness_km", "crustal_thickness"),
            ("thermal_gradient", "thermal_gradient"),
        ]:
            val = getattr(c, attr, None)
            if val is not None:
                field_dicts.setdefault(fname, {})[hid] = float(val)

        # Discrete fields
        for attr in ("plate_id", "geological_type", "boundary_type",
                      "water_table_depth", "canopy_density", "biomass_kgm2",
                      "hazard_level", "distance_to_boundary",
                      "runoff_ratio", "effective_precip",
                      "clay_content", "sand_content", "silt_content",
                      "soil_ph", "cation_exchange", "organic_matter", "soil_depth",
                      "interception_coefficient",
                      "precip_seasonality",
                      "bedrock_class", "vegetation_cover", "climate_class"):
            val = getattr(c, attr, None)
            if val is not None:
                try:
                    ws.get_discrete(attr)[hid] = float(val)
                except (ValueError, TypeError):
                    ws.get_discrete(attr)[hid] = val  # string
        # Slope magnitude (tuple component)
        sl = getattr(c, 'slope', (0.0, 0.0)) or (0.0, 0.0)
        ws.get_discrete("slope_mag")[hid] = float(sl[0])

    # Register continuous fields
    for fname, data in field_dicts.items():
        try:
            ws.set_field(fname, data)
            ws.get_discrete(fname).update(data)
        except Exception:
            pass

    # ── Register fauna as continuous fields ───────────────────────
    if fauna_fields:
        n_records = 0
        for sp_id, data in fauna_fields.items():
            if data:
                ws.set_field(f"population_density[{sp_id}]", data)
                n_records += len(data)
        print(f"  [Fauna] registered {len(fauna_fields)} species as continuous fields ({n_records} sample points)")

    # Save to DB
    save_world_state(db, ws)
