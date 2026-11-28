from __future__ import annotations

import os


def gm_max_cycles_from_env() -> int:
    raw = (os.getenv("LLM_WORLD_GM_MAX_CYCLES") or "").strip()
    if not raw:
        # Disabled by default. When set (>0), keeps only the last N turns
        # before token sliding-window trimming.
        return 0
    try:
        v = int(raw)
        if v <= 0:
            return 0
        return max(1, min(100, v))
    except Exception:
        return 0
