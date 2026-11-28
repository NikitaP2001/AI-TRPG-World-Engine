from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .json_pointer import set_at_pointer, remove_at_pointer
from .time import WorldDuration, WorldTime

from .io import (
    _json_error_snippet,
    _parse_value,
    _read_json,
    _write_json,
    ensure_file_under_storage_limit,
    StorageLimitExceeded,
)
from .migrations import (
    ensure_character_description_fields,
    migrate_world_awareness_record,
    migrate_world_initialization_record,
)


# ---------------------------------------------------------------------------
# Character storage — limits and LRU eviction moved to _json_backend.py
# ---------------------------------------------------------------------------


@dataclass
class WorldPaths:
    root: Path

    @property
    def info_json(self) -> Path:
        return self.root / "info.json"

    @property
    def timeline_json(self) -> Path:
        return self.root / "timeline.json"


class World:
    """Owns persistent world state under game/world/.*json.

    No filesystem tools are exposed to the LLM; all changes happen through named tools.
    """

    # info.json keys
    K_NAME = "name"
    K_TIME = "time"
    K_LOCATIONS = "locations"
    K_LAST_LOCATION = "last_location"
    K_STORY_ARCS = "story_arcs"
    K_PLAYERS = "players"

    # character list entry keys in info.json
    CK_NAME = "name"
    CK_LOCATION = "location"

    # What a character's place reads as when the world holds none for him. It is
    # not a place: nothing may be staged in it, and a session waits rather than
    # narrating a scene nobody is standing in.
    NO_LOCATION = "no location specified"

    DEFAULT_TIME = "Y0000-01-01 00:00"

    def __init__(self, *, workspace_root: Optional[Path] = None) -> None:
        # This module lives under world/, so default workspace root is the parent folder.
        workspace = (workspace_root or Path(__file__).resolve().parent.parent).resolve()
        self._workspace = workspace
        self._game_root = (workspace / "game").resolve()
        self._paths = WorldPaths(root=(self._game_root / "world").resolve())
    @property
    def game_root(self) -> Path:
        return self._game_root

    @property
    def paths(self) -> WorldPaths:
        return self._paths

    def ensure_initialized(self) -> None:
        self._paths.root.mkdir(parents=True, exist_ok=True)

        # NOTE: story.json is no longer created here — each agent has its own
        # {agent}_story.json managed by world.agent_story module.
        # locations.json is also no longer created — locations live in
        # entity_store/entities.json with type="location".
        if not self._paths.timeline_json.exists():
            _write_json(self._paths.timeline_json, {
                "intents": [],
                "resolved_history": [],
                "cursor": 0,
                "horizon": 0,
            })

        # Ensure characters have required fields in their description.json.
        self._ensure_character_description_fields()

        # One-time migration: a world built before the initialization record
        # existed gets the record it implies, so it is not built again.
        self._migrate_world_initialization_record()

        # The same, for the world's opening: a world whose characters were already
        # told where they stand is not opened again after a restart.
        self._migrate_world_awareness_record()

        # Sync location parent→sublocations hierarchy in entity store.
        self._ensure_location_sublocations_field()

        # Ensure info.json exists and is in sync with current characters.
        if not self._paths.info_json.exists():
            info = {
                self.K_NAME: "",
                self.K_TIME: self.DEFAULT_TIME,
                self.K_LOCATIONS: [],
                self.K_LAST_LOCATION: "",
                self.K_STORY_ARCS: ["Ongoing Arc"],
                self.K_PLAYERS: self._build_character_entries(),
            }
            _write_json(self._paths.info_json, info)
        else:
            self._sync_info_characters()

    def _character_dir(self, name: str) -> Path:
        if not name:
            raise ValueError("Character name is required")
        return (self._game_root / "players" / name).resolve()

    def _character_description_path(self, name: str) -> Path:
        return self._character_dir(name) / "description.json"

    def list_character_names(self) -> List[str]:
        chars_dir = (self._game_root / "players").resolve()
        if not chars_dir.exists():
            return []
        return sorted([p.name for p in chars_dir.iterdir() if p.is_dir()], key=str.lower)

    def _ensure_character_description_fields(self) -> None:
        ensure_character_description_fields(game_root=self._game_root)

    def _migrate_world_initialization_record(self) -> None:
        migrate_world_initialization_record(game_root=self._game_root)

    def _migrate_world_awareness_record(self) -> None:
        migrate_world_awareness_record(game_root=self._game_root)

    def _build_character_entries(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for name in self.list_character_names():
            desc = self.get_character_description(name)
            location = (desc.get("location") or "").strip() if isinstance(desc, dict) else ""
            # Fallback: location may have been stripped into _stripped.json
            if not location:
                try:
                    stripped_path = self._character_dir(name) / "_stripped.json"
                    if stripped_path.exists():
                        stripped = _read_json(stripped_path)
                        loc = stripped.get("location", "")
                        if loc:
                            location = str(loc).strip()
                except Exception:
                    pass
            out.append(
                {
                    self.CK_NAME: name,
                    self.CK_LOCATION: location if location else self.NO_LOCATION,
                }
            )
        return out

    def _sync_info_characters(self) -> None:
        info = self.get_info()
        if not isinstance(info, dict):
            return
        info[self.K_PLAYERS] = self._build_character_entries()
        if self.K_TIME not in info:
            info[self.K_TIME] = self.DEFAULT_TIME
        _write_json(self._paths.info_json, info)

    def get_info(self) -> Dict[str, Any]:
        return _read_json(self._paths.info_json)

    def get_world_time(self) -> WorldTime:
        info = self.get_info()
        return WorldTime.parse(str(info.get(self.K_TIME, self.DEFAULT_TIME)))

    def set_world_time(self, new_time: str) -> None:
        t = WorldTime.parse(new_time)
        info = self.get_info()
        info[self.K_TIME] = t.to_string()
        _write_json(self._paths.info_json, info)

    def get_locations(self) -> Dict[str, Any]:
        """Return locations as {id: data} dict from entity store.

        All locations are stored in entity_store/entities.json with type="location".
        Returns a dict of {id: entity_dict} for backward-compatible iteration.
        """
        from world_manager.entity_core import _get_backend
        backend = _get_backend()
        entities = backend.query(type_filter="location", include_templates=False, limit=200)
        result = {}
        for e in entities:
            eid = e.get("id", "unknown")
            result[eid] = e
        return result

    def get_players(self) -> List[Dict[str, str]]:
        """Return player list from entity store: [{name, location}].

        "Player" = entity whose id matches a game/players/{id}/ folder.
        Entity type is irrelevant — it's an orchestration fact,
        not an entity-store concept.

        Falls back to info.json if entity store has no matching entities yet
        (startup path — before GM populates them).
        """
        try:
            from world_manager.entity_core import _get_backend
            players_root = (self._game_root / "players")
            result = []
            # Phase 1: entities already in store
            backend = _get_backend()
            entities = backend.query(include_templates=False, limit=200)
            seen = set()
            for e in entities:
                name = e.get("id", "").strip()
                if not name or not (players_root / name).is_dir():
                    continue
                seen.add(name.lower())
                position = e.get("position", {}) or {}
                loc = position.get("inside", position.get("near", ""))
                if not loc:
                    coords = position.get("coords")
                    if coords:
                        loc = f"coords:{coords[0]},{coords[1]}"
                result.append({
                    "name": name,
                    "location": loc if loc else self.NO_LOCATION,
                })
            # Phase 2: player folders without entity records yet (startup)
            if players_root.is_dir():
                for pdir in sorted(players_root.iterdir()):
                    if not pdir.is_dir():
                        continue
                    name = pdir.name
                    if name.lower() in seen:
                        continue
                    result.append({
                        "name": name,
                        "location": self.NO_LOCATION,
                    })
            return result
        except Exception:
            pass
        # Fallback to old info.json
        info = self.get_info()
        return info.get(self.K_PLAYERS, []) if isinstance(info, dict) else []

    def get_story(self) -> Any:
        """Read legacy story.json (no longer written to, exists for backward compat)."""
        legacy = self._paths.root / "story.json"
        if legacy.exists():
            return _read_json(legacy)
        return []

    def _ensure_location_sublocations_field(self) -> None:
        """Sync sublocations list via entity store (idempotent)."""
        from world_manager.entity_core import query_entities, set_entity as _se
        try:
            locations = query_entities(type_filter="location", include_templates=False, limit=200)
        except Exception:
            return
        # Build parent→children map
        children_of: Dict[str, List[str]] = {}
        for loc in locations:
            eid = loc.get("id", "")
            parent = str(loc.get("parent_location") or "").strip()
            if parent and parent != eid:
                children_of.setdefault(parent, []).append(eid)
        # Update each parent's sublocations
        for loc in locations:
            eid = loc.get("id", "")
            expected = sorted(set(children_of.get(eid, [])))
            current = sorted(loc.get("sublocations") or [])
            if current != expected:
                _se(id=eid,
                    facts=loc.get("facts", ""),
                    sublocations=expected)

    def get_location(self, name: str) -> Dict[str, Any]:
        from world_manager.entity_core import get_entity
        entity = get_entity(name)
        if entity is None:
            raise KeyError(f"Unknown location: {name}")
        return entity

    def non_player_entities(self, limit: int = 200) -> Dict[str, Any]:
        """Return every entity the players do not control, as {id: record}.

        "Player" is an orchestration fact, not a store concept: an entity the
        players control is one whose id matches a game/players/{id}/ folder.
        Everything else is returned as the store holds it — places, factions,
        deities, creatures and people alike, each with its own type.

        limit: how many the store is asked for. A world larger than this is read
        through the query tools, which answer with counts and a bounded slice.
        """
        try:
            from world_manager.entity_core import _get_backend
            from pathlib import Path as _P
            backend = _get_backend()
            players_root = _P("game") / "players"
            return {
                str(e.get("id")): e
                for e in backend.query(include_templates=False, limit=limit)
                if e.get("id") and not (players_root / str(e.get("id"))).is_dir()
            }
        except Exception:
            return {}

    def _get_character_description_raw(self, name: str) -> Dict[str, Any]:
        p = self._character_description_path(name)
        if not p.exists():
            raise FileNotFoundError(f"Character not found: {name}")
        data = _read_json(p)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid description.json for character: {name}")
        return data

    def get_character_description(self, name: str) -> Dict[str, Any]:
        data = self._get_character_description_raw(name)

        # NOTE: _stripped.json merge-back removed — non-core fields (location,
        # equipment, skills, status, appearance) now live in entity store only.
        # After GM entity initialization, strip_character_description_to_core()
        # keeps only general/race/personality in description.json.

        return data

    CORE_DESCRIPTION_FIELDS = frozenset({"general", "race", "personality"})

    @classmethod
    def strip_character_description_to_core(cls, *, game_root: Path, name: str) -> bool:
        """Strip description.json to core fields only.

        After GM initializes a character entity in the entity store (gurps,
        state, skills, equipment, position), the description.json becomes a
        narrative-only document. This removes location, status, appearance,
        equipment, skills — keeping only general, race, personality.

        Stripped fields are saved to _stripped.json for debugging/backward compat.
        """
        desc_path = (game_root / "players" / name / "description.json").resolve()
        if not desc_path.exists():
            return False
        try:
            data = _read_json(desc_path)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False

        # Check if already stripped
        existing_keys = set(data.keys())
        if existing_keys.issubset(cls.CORE_DESCRIPTION_FIELDS):
            return False  # nothing to do

        # Save stripped fields for backward compat
        stripped = {k: v for k, v in data.items() if k not in cls.CORE_DESCRIPTION_FIELDS}
        if stripped:
            _write_json(desc_path.with_name("_stripped.json"), stripped)

        # Write core only
        core = {k: data[k] for k in cls.CORE_DESCRIPTION_FIELDS if k in data}
        _write_json(desc_path, core)
        return True

    def update_character_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        p = self._character_description_path(name)
        parsed = _parse_value(value)

        data = self._get_character_description_raw(name)
        set_at_pointer(data, pointer, parsed, create_missing=False)
        # except for auto-updating fields
        if "/location" not in pointer:
            serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            ensure_file_under_storage_limit(
                p,
                hard_limit_bytes=13 * 1024,
                target_limit_kb=10,
                storage_kind="Character description",
                pointer=pointer,
                new_size_bytes=len(serialized.encode("utf-8")),
            )
        _write_json(p, data)
        self._sync_info_characters()
        return data

    def add_character_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        p = self._character_description_path(name)
        parsed = _parse_value(value)

        data = self._get_character_description_raw(name)
        set_at_pointer(data, pointer, parsed, create_missing=True)
        if "/location" not in pointer:
            serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            ensure_file_under_storage_limit(
                p,
                hard_limit_bytes=13 * 1024,
                target_limit_kb=10,
                storage_kind="Character description",
                pointer=pointer,
                new_size_bytes=len(serialized.encode("utf-8")),
            )
        _write_json(p, data)
        self._sync_info_characters()
        return data

    def delete_character_json(self, *, name: str, pointer: str) -> Dict[str, Any]:
        p = self._character_description_path(name)

        data = self._get_character_description_raw(name)
        remove_at_pointer(data, pointer)
        _write_json(p, data)
        self._sync_info_characters()
        return data

    # ------------------------------------------------------------------
    # Scene state (ephemeral — no scene.json, design doc §6: no scene
    # boundaries, no declared_intents reset, no acted flags).
    # Kept as in-memory fallback for gradual migration.
    # ------------------------------------------------------------------

    _scene_cache: Dict[str, Any] = {"state": ""}

    def get_scene(self) -> Dict[str, Any]:
        return self._scene_cache

    # ------------------------------------------------------------------
    # Timeline store
    def get_timeline(self) -> Dict[str, Any]:
        if not self._paths.timeline_json.exists():
            return {"intents": [], "resolved_history": [], "cursor": 0, "horizon": 0}
        data = _read_json(self._paths.timeline_json)
        if not isinstance(data, dict):
            return {"intents": [], "resolved_history": [], "cursor": 0, "horizon": 0}
        data.setdefault("intents", [])
        data.setdefault("resolved_history", [])
        data.setdefault("cursor", 0)
        data.setdefault("horizon", 0)
        return data

    def set_timeline(self, timeline: Dict[str, Any]) -> None:
        _write_json(self._paths.timeline_json, timeline)
