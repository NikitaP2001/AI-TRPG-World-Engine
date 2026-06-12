"""Layer 0 — CLI entry point."""
from __future__ import annotations
import argparse
import time
from pathlib import Path
from .cell_model import GenerationParams
from ..generator import generate_world
from .generator import save_cells_parquet
from .renderer import render_textures
from .tile_renderer import generate_all_tiles


def main() -> None:
    p = argparse.ArgumentParser(description="Layer 0 — World Generation")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--planet-radius", type=float, default=1.0)
    p.add_argument("--tectonic-activity", type=float, default=0.5)
    p.add_argument("--num-plates", type=int, default=8)
    p.add_argument("--roughness", type=float, default=0.6)
    p.add_argument("--axial-tilt", type=float, default=23.5)
    p.add_argument("--output-dir", type=str, default="game/simulation",
                   help="Output directory for generated world data")
    p.add_argument("--tiles", action="store_true", help="Generate tile pyramid")
    p.add_argument("--texture-size", type=int, default=8192,
                   help="Texture width in pixels (height=width/2, default: 8192)")
    a = p.parse_args()

    params = GenerationParams(
        planet_radius=a.planet_radius,
        tectonic_activity=a.tectonic_activity, num_plates=a.num_plates,
        roughness=a.roughness,
        axial_tilt=a.axial_tilt, seed=a.seed,
    )
    print("=" * 50)
    print("Layer 0 — World Generation")
    print("=" * 50)
    print(f"  planet_radius:      {params.planet_radius}")
    print(f"  tectonic_activity:  {params.tectonic_activity}")
    print(f"  num_plates:         {params.num_plates}")
    print(f"  roughness:          {params.roughness}")
    print(f"  axial_tilt:         {params.axial_tilt}")
    print(f"  seed:               {params.seed}")
    print()

    t0 = time.time()
    cells, feature_store, flow_acc = generate_world(params)
    t1 = time.time()
    print(f"  Generation took {t1 - t0:.1f}s")
    print(f"  Features: {feature_store.count}")

    out = Path(a.output_dir)
    save_cells_parquet(cells, out / "cells.parquet")
    # Save feature store
    feature_store.save_json(str(out / "features.json"))

    # Write to SQLite (for time-aware simulation)
    try:
        from ..world_db import WorldDB
        db = WorldDB(str(out / "world.sqlite"))
        db.set_params(
            seed=str(a.seed),
            axial_tilt=str(a.axial_tilt),
            solar_constant="1361.0",
            num_plates=str(a.num_plates),
            planet_radius=str(a.planet_radius),
            tectonic_activity=str(a.tectonic_activity),
        )
        db.init_time(tick=0, year=0, day_of_year=172.0, hour=12.0)
        db.save_cells(cells)
        db.save_features(feature_store)
        db.close()
        print(f"  [DB] Saved to {out / 'world.sqlite'}")
    except Exception as e:
        print(f"  [DB] SQLite save skipped: {e}")

    render_textures(cells, out / "textures", width=a.texture_size, height=a.texture_size // 2,
                    flow_accum=flow_acc, feature_store=feature_store)

    if a.tiles:
        t2 = time.time()
        n = generate_all_tiles(cells, out, flow_accum=flow_acc)
        print(f"  Tiles took {time.time() - t2:.1f}s ({n} tiles)")

    print(f"\nDone! Textures: {out / 'textures' / 'planet_color.png'}")


if __name__ == "__main__":
    main()
