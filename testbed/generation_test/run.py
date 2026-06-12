#!/usr/bin/env python3
"""World generation test — caches the DB so subsequent runs skip generation.

First run generates the world into a local SQLite DB.  Later runs skip
generation and go straight to verification, unless --force is given.

Usage:
    cd b:\src\llm_world
    .venv\Scripts\python testbed\generation_test\run.py
    .venv\Scripts\python testbed\generation_test\run.py --force   # regenerate
    .venv\Scripts\python testbed\generation_test\run.py --view    # 3D viewer after pass
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Paths ──────────────────────────────────────────────────────────
TEST_DB = TEST_DIR / "world.sqlite"
TEST_DB_JOURNAL = TEST_DIR / "world.sqlite-journal"
CONFIG_PATH = TEST_DIR / "config.json"
WM_STATE_DIR = TEST_DIR / "wm_state"


# ======================================================================
# Verifier
# ======================================================================

class Verifier:
    """Collect verification results."""
    def __init__(self, name: str):
        self.name = name
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.warnings: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed.append(label)
            status = "✓"
        else:
            self.failed.append(label)
            status = "✗"
        msg = f"  {status}  {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)

    def check_eq(self, label: str, got, expected, detail: str = "") -> None:
        self.check(label, got == expected, detail or f"got={got}, expected={expected}")

    def check_in(self, label: str, item, collection, detail: str = "") -> None:
        self.check(label, item in collection, detail or f"'{item}' not found")

    def check_gt(self, label: str, got, minimum: float, detail: str = "") -> None:
        self.check(label, got > minimum, detail or f"got={got}, need >{minimum}")

    def summary(self) -> bool:
        total = len(self.passed) + len(self.failed)
        print(f"\n  {'='*50}")
        print(f"  {self.name}: {len(self.passed)}/{total} passed")
        if self.failed:
            print(f"  FAILED:")
            for f in self.failed:
                print(f"    ✗  {f}")
        for w in self.warnings:
            print(f"  ⚠  {w}")
        print(f"  {'='*50}")
        return len(self.failed) == 0


# ======================================================================
# Load config
# ======================================================================

def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    print(f"[Config] {config['name']} — {config['description']}")
    return config


# ======================================================================
# Phase 1 — Load + verify WM constraints
# ======================================================================

def verify_constraints(v: Verifier, config: dict):
    """Load constraints from local wm_state/ and verify counts."""
    print("\n[Phase 1] Loading WM constraints...")

    v.check("wm_state dir exists", WM_STATE_DIR.is_dir())
    required = ["world_orientation.json", "features.json", "world_concepts.json",
                 "factions.json", "entities.json", "player_start.json"]
    for fname in required:
        v.check(f"  {fname}", (WM_STATE_DIR / fname).exists())

    import simulation.wm_constraint_reader as wcr
    orig = wcr._WM_PATH
    wcr._WM_PATH = WM_STATE_DIR
    try:
        wmc = wcr.load_constraints()
        v.check("load_constraints()", True)
    except Exception as e:
        v.check(f"load_constraints() failed: {e}", False)
        return None
    finally:
        wcr._WM_PATH = orig

    exp = config["expected"]
    v.check_eq("world_name", wmc.orientation.get("world_name"), exp["world_name"])
    v.check_eq("planet_radius", wmc.orientation.get("planet_radius"), exp["planet_radius"])

    if wmc.unknown_feature_types:
        for uft in wmc.unknown_feature_types:
            v.warnings.append(f"Unknown feature_type '{uft}'")

    return wmc


# ======================================================================
# Phase 2 — Verify WM features
# ======================================================================

def verify_features(v: Verifier, wmc, config: dict):
    """Verify features/factions/entities/concepts match expectations."""
    print("\n[Phase 2] Verifying WM-authored features...")
    exp = config["expected"]

    ef = exp["features"]
    v.check_eq("continents", len(wmc.continent_constraints), ef["continent"])
    v.check_eq("mountains", len(wmc.mountain_constraints), ef["mountain"])
    v.check_eq("rivers", len(wmc.river_constraints), ef["river"])
    v.check_eq("lakes", len(wmc.lake_constraints), ef["lake"])
    v.check_eq("total features", len(wmc.features), ef["total"])

    feat_names = [f.get("name", "") for f in wmc.features]
    for name in exp["feature_names"]:
        v.check_in(f"feature '{name}'", name, feat_names)

    v.check_eq("factions", len(wmc.factions), exp["faction_count"])
    fac_names = [f.get("name", "") for f in wmc.factions]
    for name in exp["faction_names"]:
        v.check_in(f"faction '{name}'", name, fac_names)

    v.check_eq("entities", len(wmc.entities), exp["entity_count"])
    ent_names = [e.get("display_name", "") for e in wmc.entities]
    for name in exp["entity_names"]:
        v.check_in(f"entity '{name}'", name, ent_names)

    ec = exp["concepts"]
    exist_types = [c for c in wmc.concepts if c.get("concept_type") == "existence_type"]
    sett_types = [c for c in wmc.concepts if c.get("concept_type") == "settlement_type"]
    v.check_eq("existence types", len(exist_types), ec["existence_types"])
    v.check_eq("settlement types", len(sett_types), ec["settlement_types"])


# ======================================================================
# Phase 3 — Generate or reuse world
# ======================================================================

def generate_or_reuse(v: Verifier, config: dict, force: bool):
    """Generate world into local DB, or reuse existing one."""
    print("\n[Phase 3] World generation...")

    if TEST_DB.exists() and not force:
        size_mb = TEST_DB.stat().st_size / (1024 * 1024)
        print(f"  Using cached DB ({size_mb:.1f} MB) — {TEST_DB.name}")
        v.check("cached DB", True, f"{size_mb:.1f} MB")
        _ensure_db_schema()
        return

    if force:
        print("  --force: regenerating world...")

    # Clean old DB
    for p in [TEST_DB, TEST_DB_JOURNAL]:
        if p.exists():
            p.unlink()

    from simulation.layer0.cell_model import GenerationParams
    from simulation.generator import generate_world
    import simulation.wm_constraint_reader as wcr
    import h3, random as _random

    seed = config.get("world_seed", 42)

    params = GenerationParams(
        planet_radius=6371.0, axial_tilt=23.5,
        tectonic_activity=0.5, seed=seed,
    )
    params.derive()
    print(f"  H3 res={params.h3_resolution}")

    # All H3 IDs (full planet, world_extent removed)
    _rng = _random.Random(seed)
    all_ids = []
    for r in list(h3.get_res0_cells()):
        all_ids.extend(h3.cell_to_children(r, params.h3_resolution))
    _rng.shuffle(all_ids)
    print(f"  Cells: {len(all_ids)}")

    # Load + resolve constraints from local wm_state
    orig = wcr._WM_PATH
    wcr._WM_PATH = WM_STATE_DIR
    try:
        wmc = wcr.load_constraints()
        wcr.resolve_all_spatial(wmc, all_ids)
        wm_c = wmc.to_generator_constraints()
    finally:
        wcr._WM_PATH = orig

    print(f"  Constraints: {len(wmc.continent_constraints)} continents, "
          f"{len(wmc.mountain_constraints)} mountains, "
          f"{len(wmc.river_constraints)} rivers, "
          f"{len(wmc.lake_constraints)} lakes")

    # Generate
    t0 = time.time()
    try:
        cells, fs, fa = generate_world(params=params, wm_constraints=wm_c)
    except Exception as e:
        import traceback
        v.warnings.append(f"Generation error: {e}")
        v.warnings.append(traceback.format_exc())
        return

    t1 = time.time()
    elapsed = t1 - t0

    v.check(f"generated {len(cells)} cells", len(cells) > 0, f"{elapsed:.1f}s")

    # Save to local DB
    from simulation.world_db import WorldDB
    db = WorldDB(str(TEST_DB))
    db.save_cells(cells)
    db.save_features(fs)
    db.set_params(**{k: str(v) for k, v in params.__dict__.items()
                     if not k.startswith("_")})
    db.init_time(year=1492)
    db.close()

    size_kb = TEST_DB.stat().st_size // 1024
    v.check("DB saved", True, f"{size_kb} KB at {TEST_DB}")


def _ensure_db_schema():
    """Ensure a cached DB has all required tables (in case schema changed)."""
    from simulation.world_db import WorldDB
    db = WorldDB(str(TEST_DB))
    db.close()
    # WorldDB.__init__ runs auto-migration


# ======================================================================
# Phase 4 — Verify generated world
# ======================================================================

def verify_generated(v: Verifier):
    """Verify L0-L1 data in the local DB."""
    print("\n[Phase 4] Verifying generated world (L0-L1)...")

    if not TEST_DB.exists():
        v.warnings.append("No DB found — skipping")
        return

    from simulation.world_db import WorldDB
    db = WorldDB(str(TEST_DB))

    cells = db.load_cells()
    v.check_gt("cells count", len(cells), 0)
    if cells:
        land = sum(1 for c in cells if not c.get("is_ocean", 1))
        v.check_gt("land cells", land, 0, f"{land}/{len(cells)}")

        temps = [c.get("temperature_norm", 0) for c in cells]
        mean_t = sum(temps) / len(temps)
        v.check("temperature populated", mean_t > 0.01, f"mean={mean_t:.3f}")

        canopy = sum(1 for c in cells if c.get("canopy_density", 0) > 0.01)
        v.check_gt("vegetation", canopy, 0, f"{canopy} cells")

    fs = db.get_feature_store()
    if fs and fs.all_active:
        v.check_gt("features in store", len(fs.all_active), 0)

    t = db.get_time()
    if t:
        v.check("world time set", t.get("tick", -1) >= 0, f"Y{t.get('year', '?')}")

    db.close()


# ======================================================================
# Viewer
# ======================================================================

def launch_viewer():
    """Launch the OpenGL planet viewer pointing at our test world.

    Exports the SQLite DB to a Parquet file first (the viewer needs Parquet).
    """
    parquet_path = TEST_DIR / "cells.parquet"
    if not parquet_path.exists():
        try:
            from simulation.world_db import WorldDB
            from simulation.layer0.generator import save_cells_parquet
            db = WorldDB(str(TEST_DB))
            cells = db.load_cells_as_celldata()
            db.close()
            save_cells_parquet(cells, parquet_path)
            print(f"  Exported parquet: {parquet_path}")
        except Exception as e:
            print(f"  Cannot export parquet: {e}")
            print(f"  View manually with: python opengl_app\\main.py <parquet>")
            return

    viewer = PROJECT_ROOT / "opengl_app" / "main.py"
    if not viewer.exists():
        print(f"  Viewer not found: {viewer}")
        return
    cmd = [sys.executable, str(viewer), str(parquet_path)]
    print(f"  Launching: {' '.join(cmd)}")
    subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))


# ======================================================================
# Main
# ======================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="World generation test with DB caching",
    )
    parser.add_argument("--force", action="store_true",
                        help="Force regeneration even if DB exists")
    parser.add_argument("--view", action="store_true",
                        help="Launch OpenGL viewer after successful tests")
    args = parser.parse_args()

    config = load_config()

    v = Verifier(config["name"])

    # ── Phase 1 ──
    wmc = verify_constraints(v, config)
    if wmc is None:
        v.summary()
        sys.exit(1)

    # ── Phase 2 ──
    verify_features(v, wmc, config)

    # ── Phase 3 ──
    generate_or_reuse(v, config, args.force)

    # ── Phase 4 ──
    verify_generated(v)

    # ── Summary ──
    ok = v.summary()
    print()

    if ok and args.view:
        launch_viewer()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
