"""Copy the world on disk into saves/ before a reset.

Run: python _save_branch.py [name]
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

game = Path("game")
if not game.exists():
    print("no game/ to save")
    sys.exit(0)

name = sys.argv[1] if len(sys.argv) > 1 else "pre_reset_" + datetime.now().strftime("%Y%m%d_%H%M%S")
dest = Path("saves") / name
dest.mkdir(parents=True, exist_ok=True)
target = dest / "game"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(game, target)

files = [p for p in target.rglob("*") if p.is_file()]
print(f"saved {len(files)} files, {sum(p.stat().st_size for p in files)} bytes -> {target}")
