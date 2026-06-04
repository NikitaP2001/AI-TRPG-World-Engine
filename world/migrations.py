from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .io import _read_json, _write_json
from .time import WorldDuration, WorldTime


def _latest_character_last_acted_from_story(*, story_json: Path) -> Dict[str, str]:
    """Best-effort map of character -> latest end_time found in story turns."""

    out: Dict[str, str] = {}

    try:
        arcs = _read_json(story_json)
    except Exception:
        return out

    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        return out

    arc0 = arcs[0]

    def _consider_turn(turn: Any) -> None:
        if not isinstance(turn, dict):
            return

        raw_end = str(turn.get("end_time") or "").strip()
        if not raw_end:
            return

        try:
            end_norm = WorldTime.parse(raw_end).to_string()
            end_sec = WorldTime.parse(end_norm).to_seconds()
        except Exception:
            return

        chars = turn.get("characters")
        if not isinstance(chars, list):
            return

        for c in chars:
            name = str(c or "").strip()
            if not name:
                continue

            prev = str(out.get(name) or "").strip()
            if not prev:
                out[name] = end_norm
                continue

            try:
                if end_sec > WorldTime.parse(prev).to_seconds():
                    out[name] = end_norm
            except Exception:
                out[name] = end_norm

    paragraphs = arc0.get("paragraphs") if isinstance(arc0.get("paragraphs"), list) else []
    for p in paragraphs:
        if not isinstance(p, dict):
            continue
        turns = p.get("turns") if isinstance(p.get("turns"), list) else []
        for t in turns:
            _consider_turn(t)

    ongoing = arc0.get("ongoing_paragraph") if isinstance(arc0.get("ongoing_paragraph"), dict) else {}
    ongoing_turns = ongoing.get("turns") if isinstance(ongoing.get("turns"), list) else []
    for t in ongoing_turns:
        _consider_turn(t)

    return out


def migrate_scene_turn_duration(*, scene_json: Path) -> None:
    """Migrate legacy scene fields to current schema.

    Legacy: turn_duration_minutes
    Current: turn_duration (WorldDuration string)
    """

    try:
        scene = _read_json(scene_json)
    except Exception:
        return

    if not isinstance(scene, dict) or scene.get("active") is not True:
        return

    if "turn_duration" in scene:
        return

    legacy = scene.get("turn_duration_minutes")
    if legacy is None:
        return

    try:
        scene["turn_duration"] = WorldDuration.from_minutes(int(legacy)).to_string()
        scene.pop("turn_duration_minutes", None)
        _write_json(scene_json, scene)
    except Exception:
        return


def ensure_character_description_fields(*, game_root: Path) -> None:
    """Ensure required keys exist in each character's description.json.

    This is a conservative migration: it only fills missing fields and never
    overwrites user/seed values.
    """

    chars_dir = (game_root / "characters").resolve()
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
        # NOTE: last_acted is stored in metadata.json (auto-updated by runtime), not description.json.
        if "last_acted" in data:
            data.pop("last_acted", None)
            changed = True

        # Remove legacy knowledge list from description.json (now handled by character diary).
        if "knowledge" in data:
            data.pop("knowledge", None)
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


def ensure_character_metadata_fields(*, game_root: Path) -> None:
    """Ensure required keys exist in each character's metadata.json.

    metadata.json is reserved for auto-updated runtime fields that should not be
    directly edited by the LLM (e.g., last_acted).
    """

    chars_dir = (game_root / "characters").resolve()
    if not chars_dir.exists():
        return

    names = sorted([p.name for p in chars_dir.iterdir() if p.is_dir()], key=str.lower)
    story_fallback = _latest_character_last_acted_from_story(story_json=(game_root / "world" / "story.json").resolve())

    for name in names:
        meta_path = (chars_dir / name / "metadata.json").resolve()

        try:
            data = _read_json(meta_path) if meta_path.exists() else {}
        except Exception:
            data = {}

        if not isinstance(data, dict):
            data = {}

        changed = False

        raw = str(data.get("last_acted") or "").strip()
        if not raw:
            recovered = str(story_fallback.get(name) or "").strip()
            data["last_acted"] = recovered if recovered else "never"
            changed = True
        elif raw.lower() == "never":
            recovered = str(story_fallback.get(name) or "").strip()
            if recovered:
                data["last_acted"] = recovered
                changed = True
        else:
            try:
                normalized = WorldTime.parse(raw).to_string()
                if normalized != raw:
                    data["last_acted"] = normalized
                    changed = True
            except Exception:
                recovered = str(story_fallback.get(name) or "").strip()
                data["last_acted"] = recovered if recovered else "never"
                changed = True

        if changed or not meta_path.exists():
            try:
                _write_json(meta_path, data)
            except Exception:
                pass


def migrate_character_last_acted_to_metadata(*, game_root: Path) -> None:
    """Move last_acted from description.json into metadata.json (one-time migration).

    Keeps reads backward-compatible by copying the value if present, then removing
    it from description.json.
    """

    chars_dir = (game_root / "characters").resolve()
    if not chars_dir.exists():
        return

    names = sorted([p.name for p in chars_dir.iterdir() if p.is_dir()], key=str.lower)
    story_fallback = _latest_character_last_acted_from_story(story_json=(game_root / "world" / "story.json").resolve())

    for name in names:
        desc_path = (chars_dir / name / "description.json").resolve()
        meta_path = (chars_dir / name / "metadata.json").resolve()

        try:
            desc = _read_json(desc_path) if desc_path.exists() else {}
        except Exception:
            desc = {}
        if not isinstance(desc, dict):
            desc = {}

        try:
            meta = _read_json(meta_path) if meta_path.exists() else {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        migrated_val = str(desc.get("last_acted") or "").strip()
        changed_desc = False
        changed_meta = False

        if migrated_val:
            # Prefer explicit metadata if already present.
            meta_existing = str(meta.get("last_acted") or "").strip()
            if (not meta_existing) or meta_existing.lower() == "never":
                meta["last_acted"] = migrated_val
                changed_meta = True
            desc.pop("last_acted", None)
            changed_desc = True

        # Ensure defaults exist.
        meta_raw = str(meta.get("last_acted") or "").strip()
        if not meta_raw:
            recovered = str(story_fallback.get(name) or "").strip()
            meta["last_acted"] = recovered if recovered else "never"
            changed_meta = True
        elif meta_raw.lower() == "never":
            recovered = str(story_fallback.get(name) or "").strip()
            if recovered:
                meta["last_acted"] = recovered
                changed_meta = True
        else:
            try:
                normalized = WorldTime.parse(meta_raw).to_string()
                if normalized != meta_raw:
                    meta["last_acted"] = normalized
                    changed_meta = True
            except Exception:
                recovered = str(story_fallback.get(name) or "").strip()
                meta["last_acted"] = recovered if recovered else "never"
                changed_meta = True

        if "last_acted" not in meta:
            meta["last_acted"] = "never"
            changed_meta = True

        if changed_meta:
            try:
                _write_json(meta_path, meta)
            except Exception:
                pass
        if changed_desc:
            try:
                _write_json(desc_path, desc)
            except Exception:
                pass


def ensure_story_schema_v2(*, story_json: Path) -> None:
    """Migrate story.json to v2 schema if needed.

    v2 schema:
    arcs[0] = {
      name,
      paragraphs: [ {name,start_time,end_time,locations,characters,npcs,summary,turn_count} ... ],
      ongoing_paragraph: {start_time, turns:[turn...], locations, characters, npcs}
    }
    """

    try:
        arcs = _read_json(story_json)
    except Exception:
        return

    if not isinstance(arcs, list) or not arcs or not isinstance(arcs[0], dict):
        arcs = [
            {
                "name": "Ongoing Arc",
                "paragraphs": [],
                "ongoing_paragraph": {
                    "start_time": "",
                    "turns": [],
                    "locations": [],
                    "characters": [],
                    "npcs": [],
                },
            }
        ]
        _write_json(story_json, arcs)
        return

    arc0: Dict[str, Any] = arcs[0]
    changed = False

    paras = arc0.get("paragraphs")

    # Migrate legacy paragraphs list[str] to list[paragraph_obj]
    if isinstance(paras, list) and paras and isinstance(paras[0], str):
        new_paras: List[Dict[str, Any]] = []
        for i, p in enumerate(paras):
            new_paras.append(
                {
                    "name": f"Legacy Paragraph {i+1}",
                    "start_time": "",
                    "end_time": "",
                    "locations": [],
                    "characters": [],
                    "npcs": [],
                    "summary": str(p),
                    "turn_count": 0,
                }
            )
        arc0["paragraphs"] = new_paras
        changed = True
    elif not isinstance(paras, list):
        arc0["paragraphs"] = []
        changed = True

    if "summaries" in arc0:
        arc0.pop("summaries", None)
        changed = True

    ongoing = arc0.get("ongoing_paragraph")
    if not isinstance(ongoing, dict):
        arc0["ongoing_paragraph"] = {
            "start_time": "",
            "turns": [],
            "locations": [],
            "characters": [],
            "npcs": [],
        }
        changed = True
    else:
        for k, default in (
            ("start_time", ""),
            ("turns", []),
            ("locations", []),
            ("characters", []),
            ("npcs", []),
        ):
            if k not in ongoing:
                ongoing[k] = default
                changed = True
        arc0["ongoing_paragraph"] = ongoing

    arcs[0] = arc0
    if changed:
        _write_json(story_json, arcs)


def ensure_scene_schema_v2(*, scene_json: Path) -> None:
    """Ensure scene.json uses the v2 schema (state field, acted, no iteration sub-object)."""

    try:
        scene = _read_json(scene_json)
    except Exception:
        return

    if not isinstance(scene, dict):
        return

    changed = False

    # Migrate legacy booleans → state field.
    if "state" not in scene:
        if scene.get("active") is True:
            scene["state"] = "active"
        else:
            scene["state"] = ""
        changed = True

    # Remove legacy boolean flags.
    for legacy_key in ("active", "pending"):
        if legacy_key in scene:
            del scene[legacy_key]
            changed = True

    if "scene_description" not in scene:
        scene["scene_description"] = ""
        changed = True

    chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
    if isinstance(chars, dict):
        for name, entry in chars.items():
            e = entry if isinstance(entry, dict) else {}
            # Migrate planning_complete / ended → acted.
            if "acted" not in e:
                if "planning_complete" in e:
                    e["acted"] = bool(e.pop("planning_complete"))
                elif "ended" in e:
                    e["acted"] = bool(e.pop("ended"))
                else:
                    e["acted"] = False
                changed = True
            # Remove legacy fields.
            for old_key in ("planning_complete", "ended", "open_questions", "observations"):
                if old_key in e:
                    del e[old_key]
                    changed = True
            # Ensure current fields exist.
            if "last_decision" not in e:
                e["last_decision"] = ""
                changed = True
            if "last_thoughts" not in e:
                e["last_thoughts"] = ""
                changed = True
            if "corrections_exhausted" not in e:
                e["corrections_exhausted"] = False
                changed = True
            chars[name] = e
        scene["characters"] = chars

    if "initiative_order" not in scene:
        scene["initiative_order"] = list(chars.keys()) if isinstance(chars, dict) else []
        changed = True

    # Remove the legacy iteration sub-object.
    if "iteration" in scene:
        del scene["iteration"]
        changed = True

    if changed:
        _write_json(scene_json, scene)
