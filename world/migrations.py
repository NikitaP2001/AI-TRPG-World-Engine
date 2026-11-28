from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .io import _read_json, _write_json
from .time import WorldDuration, WorldTime


def _read_records(path: Path) -> List[Dict[str, Any]]:
    """A memory file as the list of records it is; empty when it is anything else.

    ``_read_json`` answers a mapping, and an agent's memory is a list — reading it
    with the wrong shape would silently say "nothing was ever told".
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            rows = json.load(handle)
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def migrate_world_initialization_record(*, game_root: Path) -> None:
    """Give a world built before the record existed the record it implies.

    ``world/initialized.json`` is what tells a restarted process that the world's
    startup step is behind it. Worlds built by an earlier version left no such
    record, and without this they would be built a second time on the next start.

    The record is written only on evidence that the world was in fact built: a
    world setting, entities in the store, and a startup call in an agent's own
    history. A world that is merely starting satisfies none of these.
    """
    root = Path(game_root)
    marker = root / "world" / "initialized.json"
    if marker.exists():
        return
    if not (root / "world_setting.json").exists():
        return

    try:
        raw = _read_json(root / "entity_store" / "entities.json")
    except Exception:
        return
    store = raw.get("entities", raw) if isinstance(raw, dict) else {}
    if not isinstance(store, dict) or not store:
        return

    startup_calls = (
        (root / "world_master_messages.json", "[call_wm: initialize_world]"),
        (root / "game_master_messages.json", "[call_gm: initialize]"),
    )
    built = False
    for history, needle in startup_calls:
        try:
            rows = _read_json(history)
        except Exception:
            continue
        if isinstance(rows, list) and any(needle in str(r.get("content") or "") for r in rows):
            built = True
            break
    if not built:
        return

    payload: Dict[str, Any] = {"migrated": True}
    try:
        info = _read_json(root / "world" / "info.json")
        if isinstance(info, dict) and info.get("time"):
            payload["world_time"] = str(info["time"])
    except Exception:
        pass
    payload["entities"] = len(store)
    try:
        _write_json(marker, payload)
    except Exception:
        pass


def migrate_world_awareness_record(*, game_root: Path) -> None:
    """Give a world opened before the record existed the record it implies.

    ``world/awareness.json`` is what tells a restarted process that the world's
    characters have already been given their opening, so a session that follows
    does not tell them where they stand a second time. Worlds opened by an earlier
    version left no such record, and without this they would be opened again on
    the next start.

    The record is written only on evidence that the opening was in fact given:
    something was told to a character and stands in his memory, or the scene
    manager holds a perception of its own. A world that is merely starting has
    neither.
    """
    root = Path(game_root)
    marker = root / "world" / "awareness.json"
    if marker.exists():
        return

    def _told() -> bool:
        players = root / "players"
        if players.is_dir():
            for entry in sorted(players.iterdir()):
                rows = _read_records(entry / "messages.json")
                if any("[account]" in str(r.get("content") or "") for r in rows):
                    return True
        rows = _read_records(root / "scene_manager_messages.json")
        return any("[perception →" in str(r.get("content") or "") for r in rows)

    if not _told():
        return

    payload: Dict[str, Any] = {"migrated": True}
    try:
        info = _read_json(root / "world" / "info.json")
        if isinstance(info, dict) and info.get("time"):
            payload["world_time"] = str(info["time"])
    except Exception:
        pass
    try:
        payload["characters"] = sorted(
            p.name for p in (root / "players").iterdir() if p.is_dir()
        )
    except Exception:
        pass
    try:
        _write_json(marker, payload)
    except Exception:
        pass


def ensure_character_description_fields(*, game_root: Path) -> None:
    """Ensure required keys exist in each character's description.json.

    This is a conservative migration: it only fills missing fields and never
    overwrites user/seed values.
    """

    chars_dir = (game_root / "players").resolve()
    if not chars_dir.exists():
        return

    names = sorted([p.name for p in chars_dir.iterdir() if p.is_dir()], key=str.lower)

    for name in names:
        desc_path = (chars_dir / name / "description.json").resolve()
        if not desc_path.exists():
            continue

        try:
            data = _read_json(desc_path)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        changed = False

        if "location" not in data:
            data["location"] = ""  # empty = unspecified
            changed = True
        # Debris from a field the framework no longer keeps: what a character
        # last acted is not part of the world model any more.
        if "last_acted" in data:
            data.pop("last_acted", None)
            changed = True

        # Remove legacy knowledge list from description.json (now handled by character diary).
        if "knowledge" in data:
            data.pop("knowledge", None)
            changed = True

        # Strip fields that now live in separate files (state.json, skills.json, equipment.json)
        for redundant_field in ["skills", "equipment", "stats", "gurps", "status"]:
            if redundant_field in data:
                data.pop(redundant_field, None)
                changed = True

        # Strip location from description.json — the entity store is the only
        # place a position is authoritative. Location in description.json is
        # only a narrative starting hint.
        if "location" in data and data["location"]:
            data.pop("location", None)
            changed = True

        # Default/normalize character status.
        # Target schema: {"health": "alive|wounded|badly_wounded|critical|dead"}
        def _normalize_health(raw: Any) -> str:
            s = str(raw or "").strip().lower()
            if s in {"dead", "deceased", "killed"}:
                return "dead"
            if s in {"critical", "fatal", "fatally_wounded", "mortally_wounded"}:
                return "critical"
            if s in {"badly_wounded", "severely_wounded", "severe", "grave"}:
                return "badly_wounded"
            if s in {"wounded", "injured", "hurt"}:
                return "wounded"
            # healthy / undead / normal / unspecified -> alive
            return "alive"

        status = data.get("status")
        if status is None:
            data["status"] = {"health": "alive"}
            changed = True
        elif isinstance(status, dict):
            old_health = status.get("health")
            normalized = _normalize_health(old_health)
            new_status = {"health": normalized}
            if status != new_status:
                data["status"] = new_status
                changed = True
        else:
            # Legacy string status (e.g., "undead", "healthy", etc.)
            data["status"] = {"health": _normalize_health(status)}
            changed = True

        if changed:
            try:
                _write_json(desc_path, data)
            except Exception:
                pass

        # Ensure per-character memory log exists.
        mem_path = (chars_dir / name / "memory.json").resolve()
        if not mem_path.exists():
            try:
                _write_json(mem_path, [])
            except Exception:
                pass
