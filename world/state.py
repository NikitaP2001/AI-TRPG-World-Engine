from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .json_pointer import get_at_pointer, set_at_pointer, remove_at_pointer
from .time import WorldDuration, WorldTime

from .io import (
    _append_jsonl,
    _json_error_snippet,
    _parse_value,
    _read_json,
    _write_json,
    ensure_file_under_storage_limit,
    StorageLimitExceeded,
)
from .migrations import (
    ensure_character_description_fields,
    ensure_character_metadata_fields,
    migrate_character_last_acted_to_metadata,
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
    def locations_json(self) -> Path:
        # Legacy path — kept for backward compat during migration, now unused.
        # Locations are stored in entity_store/entities.json with type="location".
        return self.root / "locations.json"

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
    CK_LAST_ACTED = "last_acted"

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

        # Ensure characters have required fields in their metadata.json.
        self._ensure_character_metadata_fields()

        # One-time migration: move last_acted out of description.json.
        self._migrate_character_last_acted_to_metadata()

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

    def _character_state_path(self, name: str) -> Path:
        return self._character_dir(name) / "state.json"

    def _character_skills_path(self, name: str) -> Path:
        return self._character_dir(name) / "skills.json"

    def _character_equipment_path(self, name: str) -> Path:
        return self._character_dir(name) / "equipment.json"

    def _character_metadata_path(self, name: str) -> Path:
        return self._character_dir(name) / "metadata.json"

    def list_character_names(self) -> List[str]:
        chars_dir = (self._game_root / "players").resolve()
        if not chars_dir.exists():
            return []
        return sorted([p.name for p in chars_dir.iterdir() if p.is_dir()], key=str.lower)

    def _ensure_character_description_fields(self) -> None:
        ensure_character_description_fields(game_root=self._game_root)

    def _ensure_character_metadata_fields(self) -> None:
        ensure_character_metadata_fields(game_root=self._game_root)

    def _migrate_character_last_acted_to_metadata(self) -> None:
        migrate_character_last_acted_to_metadata(game_root=self._game_root)

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
            try:
                meta = self.get_character_metadata(name)
                last_acted = str(meta.get("last_acted") or "never")
            except Exception:
                last_acted = "never"
            out.append(
                {
                    self.CK_NAME: name,
                    self.CK_LOCATION: location if location else "no location specified",
                    self.CK_LAST_ACTED: last_acted or "never",
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
        """Return player list from entity store: [{name, location, last_acted}].

        "Player" = entity whose id matches a game/players/{id}/ folder.
        Entity type is irrelevant — it's an orchestration fact,
        not an entity-store concept.

        Falls back to info.json if entity store has no matching entities yet
        (startup path — before GM populates them).
        """
        try:
            from world_manager.entity_core import _get_backend
            from pathlib import Path as _P
            players_root = _P("game") / "players"
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
                last_acted = "never"
                try:
                    meta = _read_json(self._character_dir(name) / "metadata.json")
                    last_acted = str(meta.get("last_acted", "never"))
                except Exception:
                    pass
                result.append({
                    "name": name,
                    "location": loc if loc else "no location specified",
                    "last_acted": last_acted,
                })
            # Phase 2: player folders without entity records yet (startup)
            if players_root.is_dir():
                for pdir in sorted(players_root.iterdir()):
                    if not pdir.is_dir():
                        continue
                    name = pdir.name
                    if name.lower() in seen:
                        continue
                    loc = "no location specified"
                    last_acted = "never"
                    try:
                        meta = _read_json(pdir / "metadata.json")
                        last_acted = str(meta.get("last_acted", "never"))
                    except Exception:
                        pass
                    result.append({
                        "name": name,
                        "location": loc,
                        "last_acted": last_acted,
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
                _se(id=eid, type="location",
                    facts=loc.get("facts", ""),
                    sublocations=expected)

    def get_location(self, name: str) -> Dict[str, Any]:
        from world_manager.entity_core import get_entity
        entity = get_entity(name)
        if entity is None:
            raise KeyError(f"Unknown location: {name}")
        return entity

    def get_npcs(self) -> Dict[str, Any]:
        """Return all non-player entities as {name: data} dict.

        UI helper — filters out entities whose id matches a game/players/{id}/
        folder (those are player-controlled). Not a type check — the entity
        store does not know what a "player" is.
        """
        try:
            from world_manager.entity_core import _get_backend
            from pathlib import Path as _P
            backend = _get_backend()
            entities = backend.query(include_templates=False, limit=200)
            result = {}
            for e in entities:
                eid = e.get("id", "")
                if not eid:
                    continue
                # Skip player-controlled entities
                if (_P("game") / "players" / eid).is_dir():
                    continue
                loc_data = e.get("position", {})
                loc = loc_data.get("inside", "") if isinstance(loc_data, dict) else ""
                result[eid] = {
                    "name": eid,
                    "location": loc,
                    "current_state": e.get("state", {}).get("current_state", ""),
                    "description": e.get("facts", ""),
                    "details": "",
                    "last_acted": e.get("state", {}).get("last_acted", "never"),
                }
            return result
        except Exception:
            pass
        return {}

    def get_npc(self, name: str) -> Dict[str, Any]:
        from world_manager.entity_core import get_entity as _ge
        entity = _ge(name)
        if entity is None:
            raise KeyError(f"Unknown entity: {name}")
        loc_data = entity.get("position", {})
        loc = loc_data.get("inside", "") if isinstance(loc_data, dict) else ""
        return {
            "name": name,
            "location": loc,
            "current_state": entity.get("state", {}).get("current_state", ""),
            "description": entity.get("facts", ""),
            "details": "",
            "last_acted": entity.get("state", {}).get("last_acted", "never"),
        }

    def create_npc(self, *, name: str, location: str, current_state: str, description: str) -> Dict[str, Any]:
        from world_manager.entity_core import set_entity as _se
        name = (name or "").strip()
        if not name:
            raise ValueError("NPC name is required")

        # Validate no character collision
        try:
            for existing in self.list_character_names():
                if str(existing).strip().lower() == name.lower():
                    raise ValueError(
                        f"NPC name '{name}' conflicts with existing character '{existing}'."
                    )
        except ValueError:
            raise
        except Exception:
            pass

        location = (location or "").strip()
        if not location:
            raise ValueError("NPC location is required")
        _ = self.get_location(location)

        # Check duplicate via entity store
        from world_manager.entity_core import get_entity as _ge
        if _ge(name) is not None:
            raise ValueError(f"NPC already exists: {name}. Use update methods to modify it.")

        result = _se(
            id=name, type="npc",
            facts=description or "",
            state={"current_state": current_state or "", "last_acted": "never"},
            position={"inside": location},
        )
        if result.get("status") == "error":
            raise ValueError(f"Failed to create NPC: {result.get('error')}")
        return self.get_npc(name)

    def update_npc_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Update a JSON field on an NPC entity using JSON Pointer."""
        from world_manager.entity_core import get_entity as _ge, set_entity as _se
        entity = _ge(name)
        if entity is None:
            raise KeyError(f"Unknown NPC: {name}")
        parsed = _parse_value(value)
        set_at_pointer(entity, pointer, parsed, create_missing=False)
        result = _se(id=name, type="npc",
            facts=entity.get("facts", ""),
            state=entity.get("state", {}),
            position=entity.get("position", {}),
        )
        if result.get("status") == "error":
            raise ValueError(f"Failed to update NPC: {result.get('error')}")
        return entity

    def add_npc_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Add/create a JSON field on an NPC entity using JSON Pointer."""
        from world_manager.entity_core import get_entity as _ge, set_entity as _se
        entity = _ge(name)
        if entity is None:
            raise KeyError(f"Unknown NPC: {name}")
        parsed = _parse_value(value)
        set_at_pointer(entity, pointer, parsed, create_missing=True)
        result = _se(id=name, type="npc",
            facts=entity.get("facts", ""),
            state=entity.get("state", {}),
            position=entity.get("position", {}),
        )
        if result.get("status") == "error":
            raise ValueError(f"Failed to add NPC field: {result.get('error')}")
        return entity

    def delete_npc_json(self, *, name: str, pointer: str) -> Dict[str, Any]:
        """Delete a JSON field on an NPC entity using JSON Pointer."""
        from world_manager.entity_core import get_entity as _ge, set_entity as _se
        entity = _ge(name)
        if entity is None:
            raise KeyError(f"Unknown NPC: {name}")
        remove_at_pointer(entity, pointer)
        result = _se(id=name, type="npc",
            facts=entity.get("facts", ""),
            state=entity.get("state", {}),
            position=entity.get("position", {}),
        )
        if result.get("status") == "error":
            raise ValueError(f"Failed to delete NPC field: {result.get('error')}")
        return entity

    def delete_npc(self, name: str) -> None:
        """Remove an NPC from the entity store."""
        from world_manager.entity_core import _get_backend
        name = (name or "").strip()
        if not name:
            raise ValueError("NPC name is required")
        backend = _get_backend()
        if backend.get(name) is None:
            raise KeyError(f"Unknown NPC: {name}")
        backend.delete(name, reason="deleted via delete_npc")

    def set_npc_last_acted(self, *, name: str, last_acted: str) -> Dict[str, Any]:
        """Set an NPC's last_acted timestamp via entity store."""
        from world_manager.entity_core import get_entity as _ge, set_entity as _se
        entity = _ge(name)
        if entity is None:
            raise KeyError(f"Unknown NPC: {name}")
        raw = str(last_acted or "").strip()
        normalized = "never" if not raw or raw.lower() == "never" else WorldTime.parse(raw).to_string()
        state = dict(entity.get("state", {}))
        state["last_acted"] = normalized
        result = _se(id=name, type="npc",
            facts=entity.get("facts", ""),
            state=state,
            position=entity.get("position", {}),
        )
        if result.get("status") == "error":
            raise ValueError(f"Failed to update NPC last_acted: {result.get('error')}")
        return self.get_npc(name)

    def get_character_metadata(self, name: str) -> Dict[str, Any]:
        p = self._character_metadata_path(name)
        if not p.exists():
            return {
                "last_acted": "never",
            }
        data = _read_json(p)
        if not isinstance(data, dict):
            return {
                "last_acted": "never",
            }
        if "last_acted" not in data:
            data = {**data, "last_acted": "never"}
        return data

    def set_character_last_acted(self, *, name: str, last_acted: str) -> Dict[str, Any]:
        # Validate character exists.
        _ = self._get_character_description_raw(name)

        raw = str(last_acted or "").strip()
        if not raw or raw.lower() == "never":
            normalized = "never"
        else:
            normalized = WorldTime.parse(raw).to_string()

        p = self._character_metadata_path(name)
        data = self.get_character_metadata(name)
        data["last_acted"] = normalized
        _write_json(p, data)
        self._sync_info_characters()
        return data

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

        # Inject runtime metadata fields for agent visibility, without persisting them.
        try:
            meta = self.get_character_metadata(name)
            la = str(meta.get("last_acted") or "").strip()
            if la:
                data = {**data, "last_acted": la}
        except Exception:
            pass

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

    # ------------------------------------------------------------------
    # Character state.json (GURPS runtime: hp, fp, hunger, etc.)
    # ------------------------------------------------------------------

    def get_character_state(self, name: str) -> Dict[str, Any]:
        """Return state.json or {} if not yet initialized."""
        p = self._character_state_path(name)
        try:
            return _read_json(p) if p.exists() else {}
        except Exception:
            return {}

    # NOTE: set_character_state — removed (dead code, 0 callers)

    # ------------------------------------------------------------------
    # Character skills.json (GURPS abilities: core + LRU cache)
    # ------------------------------------------------------------------

    def get_character_skills(self, name: str) -> Dict[str, Any]:
        """Return skills.json or {} if not yet initialized."""
        p = self._character_skills_path(name)
        try:
            return _read_json(p) if p.exists() else {}
        except Exception:
            return {}

    # NOTE: set_character_skills/set_character_equipment/set_character_state
    # and _strip_description_field/_strip_noncore_fields have been REMOVED.
    # These were dead code (0 external callers). Byte budgets, LRU eviction,
    # and narrative-strip mechanisms are now in _json_backend.py as generic
    # field-config, applied via _field_config on base_type templates or
    # form-based defaults — not tied to any type string.

    def get_character_equipment(self, name: str) -> Dict[str, Any]:
        """Return equipment.json or {} if not yet initialized."""
        p = self._character_equipment_path(name)
        try:
            return _read_json(p) if p.exists() else {}
        except Exception:
            return {}

    def update_character_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        p = self._character_description_path(name)
        parsed = _parse_value(value)

        # last_acted is stored in metadata.json (runtime-managed), not description.json.
        if str(pointer or "").strip() == "/last_acted":
            self.set_character_last_acted(name=name, last_acted=str(parsed))
            return self.get_character_description(name)

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

        # last_acted is stored in metadata.json (runtime-managed), not description.json.
        if str(pointer or "").strip() == "/last_acted":
            self.set_character_last_acted(name=name, last_acted=str(parsed))
            return self.get_character_description(name)

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

        # last_acted is runtime-managed in metadata.json and not deletable here.
        if str(pointer or "").strip() == "/last_acted":
            raise ValueError("/last_acted is runtime-managed and cannot be deleted via description.json")

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

    def set_scene(self, scene: Dict[str, Any]) -> None:
        self._scene_cache = dict(scene)

    def clear_scene(self) -> None:
        self._scene_cache = {"state": ""}

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

    def clear_timeline(self) -> None:
        _write_json(self._paths.timeline_json, {
            "intents": [], "resolved_history": [], "cursor": 0, "horizon": 0,
        })
