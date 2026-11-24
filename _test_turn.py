"""Quick test: one full cycle with state validation."""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)
os.environ["LLM_WORLD_STREAM_ECHO"] = "1"

import shutil
from pathlib import Path
game = Path("game")
if game.exists():
    for item in list(game.iterdir()):
        if item.is_dir(): shutil.rmtree(str(item))
        else: item.unlink()
else:
    game.mkdir(parents=True)

from console_app import GameOrchestrator
app = GameOrchestrator()
app._cmd_load_preset("test")

from cycle_runner import run_cycle
t0 = time.time()
try:
    run_cycle(app, None)
except Exception as e:
    print(f"\nEXCEPTION: {e}")
    import traceback; traceback.print_exc()
elapsed = time.time() - t0
print(f"time={elapsed:.1f}s")

# ── Scene state ──
s = app.world.get_scene()
if isinstance(s, dict):
    print(f"scene: state={s.get('state')} loc={s.get('location')}")
    chars = s.get("players") or {}
    acted = sum(1 for v in chars.values() if (v or {}).get("acted"))
    print(f"acted: {acted}/{len(chars)}")

# ── File sizes ──
for f in ["game_master_messages.json", "scene_manager_messages.json", "world/story.json",
          "world/timeline.json", "world/scene.json", "world/info.json"]:
    p = game / f
    print(f"{f}: {p.stat().st_size if p.exists() else 0}B")

# ── Player files ──
players_dir = game / "players"
if players_dir.exists():
    for pd in sorted(players_dir.iterdir()):
        if pd.is_dir():
            files = [f.name for f in pd.iterdir()]
            print(f"  player/{pd.name}: {files}")
            # Check description.json has stripped format
            desc = pd / "description.json"
            if desc.exists():
                d = json.loads(desc.read_text(encoding="utf-8"))
                has_persona = "general" in d or "personality" in d
                no_mechanics = "equipment" not in d and "skills" not in d
                print(f"    desc: persona={has_persona} no_mechanics={no_mechanics}")
            meta = pd / "metadata.json"
            if meta.exists():
                m = json.loads(meta.read_text(encoding="utf-8"))
                print(f"    meta: tags={'tags' in m} last_acted={'last_acted' in m}")
            stripped = pd / "_stripped.json"
            if stripped.exists():
                print(f"    _stripped: present")
