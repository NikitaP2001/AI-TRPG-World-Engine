from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from world.io import _read_json as _locked_read_json, _write_json as _locked_write_json


def _read_json(path: Path) -> Any:
    return _locked_read_json(path)


def _write_json(path: Path, data: Any) -> None:
    _locked_write_json(path, data)


def ensure_memory_file(path: Path) -> None:
    if path.exists():
        return
    _write_json(path, [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_turn_memory(
    path: Path,
    *,
    character_name: str,
    world_time: str,
    scene_location: str,
    thoughts_to_add: Optional[List[str]] = None,
    outcome: Optional[Dict[str, Any]] = None,
    is_override: bool = False,
) -> None:
    """Append thoughts and/or set outcome for the current turn.

    A "turn" is keyed by (world_time, scene_location).
    This stays stable even when scene duration is finalized later by the GM.

    The file format is a list of turn entries in chronological order.
    """

    ensure_memory_file(path)

    try:
        data = _read_json(path)
    except Exception:
        data = []

    if not isinstance(data, list):
        data = []

    key = f"{world_time}|{scene_location}"
    ts = _now_iso()

    entry: Dict[str, Any]
    if data and isinstance(data[-1], dict) and str(data[-1].get("turn_key") or "") == key:
        entry = data[-1]
    else:
        entry = {
            "turn_key": key,
            "meta": {
                "character": character_name,
                "world_time": world_time,
                "scene_location": scene_location,
                "created_at": ts,
                "updated_at": ts,
            },
            "thoughts": [],
            "outcome": None,
        }
        data.append(entry)

    meta = entry.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("character", character_name)
    meta.setdefault("world_time", world_time)
    meta.setdefault("scene_location", scene_location)
    meta["updated_at"] = ts
    if is_override:
        meta["override"] = True
    entry["meta"] = meta

    thoughts = entry.get("thoughts")
    if not isinstance(thoughts, list):
        thoughts = []

    for t in thoughts_to_add or []:
        s = str(t or "").strip()
        if s:
            thoughts.append(s)

    entry["thoughts"] = thoughts

    if outcome is not None:
        # We never overwrite an existing outcome unless explicitly asked.
        if entry.get("outcome") is None:
            entry["outcome"] = outcome
        else:
            # If an outcome already exists, keep the first one but update metadata.
            pass

    _write_json(path, data)
