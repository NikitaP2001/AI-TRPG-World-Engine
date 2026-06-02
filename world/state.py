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
)
from .migrations import (
    ensure_character_description_fields,
    ensure_character_metadata_fields,
    ensure_story_schema_v2,
    ensure_scene_schema_v2,
    migrate_character_last_acted_to_metadata,
)
from .story import append_turn_to_story as _append_turn_to_story


@dataclass
class WorldPaths:
    root: Path

    @property
    def info_json(self) -> Path:
        return self.root / "info.json"

    @property
    def story_json(self) -> Path:
        return self.root / "story.json"

    @property
    def locations_json(self) -> Path:
        return self.root / "locations.json"

    @property
    def npc_json(self) -> Path:
        return self.root / "npc.json"

    @property
    def scene_json(self) -> Path:
        return self.root / "scene.json"


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
    K_CHARACTERS = "characters"

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
        # Optional callable(world_context, ongoing_paragraph) -> {"name": str, "summary": str}
        self._summarizer: Optional[Any] = None

    def set_summarizer(self, summarizer: Any) -> None:
        """Set the paragraph summarizer callable (typically backed by the Game Master)."""
        self._summarizer = summarizer

    @property
    def game_root(self) -> Path:
        return self._game_root

    @property
    def paths(self) -> WorldPaths:
        return self._paths

    def ensure_initialized(self) -> None:
        self._paths.root.mkdir(parents=True, exist_ok=True)

        # Ensure core JSON files exist.
        if not self._paths.locations_json.exists():
            _write_json(self._paths.locations_json, {})
        if not self._paths.npc_json.exists():
            _write_json(self._paths.npc_json, {})
        if not self._paths.story_json.exists():
            _write_json(
                self._paths.story_json,
                [
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
                ],
            )
        if not self._paths.scene_json.exists():
            _write_json(self._paths.scene_json, {"state": ""})
        else:
            # Scene duration is finalized at gm_output_turn time; do not enforce turn_duration on persisted scenes.
            pass

        # Ensure scene schema fields exist (state, acted, etc.) and migrate legacy fields.
        ensure_scene_schema_v2(scene_json=self._paths.scene_json)

        # Ensure characters have required fields in their description.json.
        self._ensure_character_description_fields()

        # Ensure characters have required fields in their metadata.json.
        self._ensure_character_metadata_fields()

        # One-time migration: move last_acted out of description.json.
        self._migrate_character_last_acted_to_metadata()

        # Ensure story.json schema is in the expected (v2) format.
        self._ensure_story_schema_v2()

        # Remove legacy plot_seed turns (old format) and persist init plot as
        # a proper first paragraph so it survives through paragraph summaries.
        self._remove_legacy_plot_seed_turns()
        self._ensure_init_plot_as_first_paragraph()

        # Ensure every location entry has a sublocations_names field.
        self._ensure_location_sublocations_field()

        # Ensure info.json exists and is in sync with current characters.
        if not self._paths.info_json.exists():
            info = {
                self.K_NAME: "",
                self.K_TIME: self.DEFAULT_TIME,
                self.K_LOCATIONS: [],
                self.K_LAST_LOCATION: "",
                self.K_STORY_ARCS: ["Ongoing Arc"],
                self.K_CHARACTERS: self._build_character_entries(),
            }
            _write_json(self._paths.info_json, info)
        else:
            self._sync_info_characters()

    def _read_init_plot_json(self) -> Optional[Dict[str, Any]]:
        """Best-effort read of init/plot.json (or legacy init/characters/plot.json)."""

        init_plot_path = (self._workspace / "init" / "plot.json").resolve()
        alt_plot_path = (self._workspace / "init" / "characters" / "plot.json").resolve()
        path = init_plot_path if init_plot_path.exists() else alt_plot_path
        if not path.exists():
            return None

        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None

        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _remove_legacy_plot_seed_turns(self) -> None:
        """Remove legacy plot_seed turns previously persisted into story.json."""
        try:
            arcs = _read_json(self._paths.story_json)
        except Exception:
            return
        if not isinstance(arcs, list) or not arcs:
            return

        changed = False
        for i, arc in enumerate(arcs):
            if not isinstance(arc, dict):
                continue

            ongoing = arc.get("ongoing_paragraph")
            if isinstance(ongoing, dict):
                turns = ongoing.get("turns")
                if isinstance(turns, list):
                    filtered = [
                        t for t in turns
                        if not (isinstance(t, dict) and str(t.get("kind") or "").strip() == "plot_seed")
                    ]
                    if len(filtered) != len(turns):
                        ongoing["turns"] = filtered
                        arc["ongoing_paragraph"] = ongoing
                        changed = True

            paragraphs = arc.get("paragraphs")
            if isinstance(paragraphs, list):
                for p in paragraphs:
                    if not isinstance(p, dict):
                        continue
                    p_turns = p.get("turns")
                    if not isinstance(p_turns, list):
                        continue
                    filtered_p = [
                        t for t in p_turns
                        if not (isinstance(t, dict) and str(t.get("kind") or "").strip() == "plot_seed")
                    ]
                    if len(filtered_p) != len(p_turns):
                        p["turns"] = filtered_p
                        changed = True

            arcs[i] = arc

        if changed:
            try:
                _write_json(self._paths.story_json, arcs)
            except Exception:
                return

    def _ensure_init_plot_as_first_paragraph(self) -> None:
        """Persist init/plot.json as the very first paragraph in story.json (once).

        Unlike the old plot_seed approach which injected a special turn kind,
        this creates a real summarized paragraph so the plot text survives
        through paragraph summaries and remains visible in context forever.
        Idempotent: skips if a paragraph named 'Initial Plot' already exists.
        """

        plot = self._read_init_plot_json()
        if not isinstance(plot, dict):
            return

        init_text = str(plot.get("init") or "").strip()
        if not init_text:
            return

        try:
            arcs = _read_json(self._paths.story_json)
        except Exception:
            return
        if not isinstance(arcs, list) or not arcs:
            return

        # Find the oldest arc (last element) to prepend the paragraph there.
        oldest_arc = arcs[-1]
        if not isinstance(oldest_arc, dict):
            return

        paras = oldest_arc.get("paragraphs")
        if not isinstance(paras, list):
            paras = []
            oldest_arc["paragraphs"] = paras

        # Idempotency: if first paragraph is already the plot, skip.
        if paras and isinstance(paras[0], dict) and str(paras[0].get("name") or "").strip() == "Initial Plot":
            return

        plot_paragraph = {
            "name": "Initial Plot",
            "start_time": "",
            "end_time": "",
            "locations": [],
            "characters": [],
            "npcs": [],
            "summary": init_text,
            "turn_count": 0,
            "turns": [],
        }
        paras.insert(0, plot_paragraph)
        oldest_arc["paragraphs"] = paras
        arcs[-1] = oldest_arc

        try:
            _write_json(self._paths.story_json, arcs)
        except Exception:
            return

    def _character_dir(self, name: str) -> Path:
        if not name:
            raise ValueError("Character name is required")
        return (self._game_root / "characters" / name).resolve()

    def _character_description_path(self, name: str) -> Path:
        return self._character_dir(name) / "description.json"

    def _character_metadata_path(self, name: str) -> Path:
        return self._character_dir(name) / "metadata.json"

    def list_character_names(self) -> List[str]:
        chars_dir = (self._game_root / "characters").resolve()
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
        info[self.K_CHARACTERS] = self._build_character_entries()
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
        return _read_json(self._paths.locations_json)

    def get_story(self) -> Any:
        return _read_json(self._paths.story_json)

    def _ensure_story_schema_v2(self) -> None:
        ensure_story_schema_v2(story_json=self._paths.story_json)

    def append_turn_to_story(
        self,
        *,
        narration: str,
        start_time: str,
        end_time: str,
        location: str,
        characters: List[str],
        npcs: List[str],
    ) -> None:
        """Append one turn to the ongoing paragraph; summarize every 10 turns into a paragraph."""

        _append_turn_to_story(
            story_json=self._paths.story_json,
            workspace=self._workspace,
            narration=narration,
            start_time=start_time,
            end_time=end_time,
            location=location,
            characters=characters,
            npcs=npcs,
            summarizer=self._summarizer,
        )

    def _ensure_location_sublocations_field(self) -> None:
        """Backfill and normalize location hierarchy fields on existing entries."""
        try:
            locs = self.get_locations()
        except Exception:
            return
        changed = self._sync_location_hierarchy_fields(locs)
        if changed:
            _write_json(self._paths.locations_json, locs)

    def _sync_location_hierarchy_fields(self, locs: Dict[str, Any]) -> bool:
        """Ensure `parent_location` and `sublocations_names` are mutually consistent.

        Rules:
        - Every location has `parent_location` (str) and `sublocations_names` (list).
        - If a child has `parent_location` pointing to an existing location, that parent
          includes the child in `sublocations_names`.
        - If parent changes or is removed, parent sublocation lists are rebuilt accordingly.
        """

        if not isinstance(locs, dict):
            return False

        changed = False
        location_names: List[str] = [str(k) for k, v in locs.items() if isinstance(v, dict)]
        desired_children: Dict[str, List[str]] = {nm: [] for nm in location_names}

        # Ensure required fields and compute desired parent -> children mapping.
        # Also collect any referenced parent names that don't exist as records yet.
        missing_parents: List[str] = []
        for child_name in location_names:
            entry = locs.get(child_name)
            if not isinstance(entry, dict):
                continue

            if "parent_location" not in entry:
                entry["parent_location"] = ""
                changed = True
            if "sublocations_names" not in entry:
                entry["sublocations_names"] = []
                changed = True

            parent_name = str(entry.get("parent_location") or "").strip()
            if not parent_name or parent_name == child_name:
                continue
            if parent_name in desired_children:
                if child_name not in desired_children[parent_name]:
                    desired_children[parent_name].append(child_name)
            else:
                # Parent is referenced but has no location record — create a stub.
                if parent_name not in missing_parents:
                    missing_parents.append(parent_name)

        # Create minimal stub records for referenced-but-missing parents.
        for parent_name in missing_parents:
            locs[parent_name] = {
                "name": parent_name,
                "summary": "",
                "details": "",
                "last_active": "",
                "parent_location": "",
                "sublocations_names": [],
            }
            location_names.append(parent_name)
            desired_children[parent_name] = []
            changed = True

        # Re-run the child assignment now that stub parents exist.
        for child_name in location_names:
            entry = locs.get(child_name)
            if not isinstance(entry, dict):
                continue
            parent_name = str(entry.get("parent_location") or "").strip()
            if parent_name and parent_name in desired_children and parent_name != child_name:
                if child_name not in desired_children[parent_name]:
                    desired_children[parent_name].append(child_name)

        # Normalize each parent's sublocations_names to match current parent links.
        for parent_name in location_names:
            entry = locs.get(parent_name)
            if not isinstance(entry, dict):
                continue

            current_raw = entry.get("sublocations_names")
            current_list: List[str] = []
            if isinstance(current_raw, list):
                for x in current_raw:
                    sx = str(x).strip()
                    if sx and sx not in current_list:
                        current_list.append(sx)

            expected_list = desired_children.get(parent_name, [])
            if current_list != expected_list:
                entry["sublocations_names"] = expected_list
                changed = True

        return changed

    def get_location(self, name: str) -> Dict[str, Any]:
        locs = self.get_locations()
        if name not in locs:
            raise KeyError(f"Unknown location: {name}")
        return locs[name]

    def create_location(
        self,
        *,
        name: str,
        summary: str,
        details: str,
        parent_location: str = "",
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("Location name is required")

        locs = self.get_locations()
        if name in locs:
            return locs[name]

        parent = str(parent_location or "").strip()

        locs[name] = {
            "name": name,
            "summary": summary or "",
            "details": details or "",
            "last_active": "",
            "parent_location": parent,
            "sublocations_names": [],
        }
        self._sync_location_hierarchy_fields(locs)
        _write_json(self._paths.locations_json, locs)

        info = self.get_info()
        if name not in info.get(self.K_LOCATIONS, []):
            info.setdefault(self.K_LOCATIONS, []).append(name)
            _write_json(self._paths.info_json, info)

        return locs[name]

    def update_location_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Update a JSON field in locations.json using JSON Pointer (no container creation)."""

        parsed = _parse_value(value)

        locs = self.get_locations()
        if name not in locs:
            raise KeyError(f"Unknown location: {name}")

        loc = locs.get(name)
        if not isinstance(loc, dict):
            raise ValueError(f"Invalid location record: {name}")

        set_at_pointer(loc, pointer, parsed, create_missing=False)
        locs[name] = loc
        self._sync_location_hierarchy_fields(locs)
        _write_json(self._paths.locations_json, locs)
        return loc

    def add_location_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Add/create a JSON field in locations.json using JSON Pointer (creates missing containers)."""

        parsed = _parse_value(value)

        locs = self.get_locations()
        if name not in locs:
            raise KeyError(f"Unknown location: {name}")

        loc = locs.get(name)
        if not isinstance(loc, dict):
            raise ValueError(f"Invalid location record: {name}")

        set_at_pointer(loc, pointer, parsed, create_missing=True)
        locs[name] = loc
        self._sync_location_hierarchy_fields(locs)
        _write_json(self._paths.locations_json, locs)
        return loc

    def delete_location_json(self, *, name: str, pointer: str) -> Dict[str, Any]:
        """Delete a JSON field in locations.json using JSON Pointer."""

        locs = self.get_locations()
        if name not in locs:
            raise KeyError(f"Unknown location: {name}")

        loc = locs.get(name)
        if not isinstance(loc, dict):
            raise ValueError(f"Invalid location record: {name}")

        remove_at_pointer(loc, pointer)
        locs[name] = loc
        self._sync_location_hierarchy_fields(locs)
        _write_json(self._paths.locations_json, locs)
        return loc

    def get_npcs(self) -> Dict[str, Any]:
        return _read_json(self._paths.npc_json)

    def get_npc(self, name: str) -> Dict[str, Any]:
        npcs = self.get_npcs()
        if name not in npcs:
            raise KeyError(f"Unknown NPC: {name}")
        npc = npcs.get(name)
        if not isinstance(npc, dict):
            raise ValueError(f"Invalid NPC record: {name}")
        return npc

    def create_npc(self, *, name: str, location: str, current_state: str, description: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("NPC name is required")

        # NPC names must not collide with character names.
        # Compare case-insensitively to avoid ambiguous overlaps on case-insensitive filesystems.
        try:
            for existing in self.list_character_names():
                if str(existing).strip().lower() == name.lower():
                    raise ValueError(
                        f"NPC name '{name}' conflicts with existing character '{existing}'. Choose a different NPC name."
                    )
        except ValueError:
            raise
        except Exception:
            # If character scanning fails for any reason, do not block NPC creation.
            pass

        location = (location or "").strip()
        if not location:
            raise ValueError("NPC location is required")

        # Validate location exists.
        _ = self.get_location(location)

        npcs = self.get_npcs()
        if name in npcs:
            raise ValueError(f"NPC already exists: {name}. Use update_npc to modify it.")

        npcs[name] = {
            "name": name,
            "location": location,
            "current_state": current_state or "",
            "description": description or "",
            "details": "",
            "last_acted": "never",
        }
        _write_json(self._paths.npc_json, npcs)
        return npcs[name]

    def update_npc_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Update a JSON field in npc.json using JSON Pointer (no container creation)."""

        parsed = _parse_value(value)

        npcs = self.get_npcs()
        if name not in npcs:
            raise KeyError(f"Unknown NPC: {name}")

        npc = npcs.get(name)
        if not isinstance(npc, dict):
            raise ValueError(f"Invalid NPC record: {name}")

        set_at_pointer(npc, pointer, parsed, create_missing=False)
        npcs[name] = npc
        _write_json(self._paths.npc_json, npcs)
        return npc

    def add_npc_json(self, *, name: str, pointer: str, value: str) -> Dict[str, Any]:
        """Add/create a JSON field in npc.json using JSON Pointer (creates missing containers)."""

        parsed = _parse_value(value)

        npcs = self.get_npcs()
        if name not in npcs:
            raise KeyError(f"Unknown NPC: {name}")

        npc = npcs.get(name)
        if not isinstance(npc, dict):
            raise ValueError(f"Invalid NPC record: {name}")

        set_at_pointer(npc, pointer, parsed, create_missing=True)
        npcs[name] = npc
        _write_json(self._paths.npc_json, npcs)
        return npc

    def delete_npc_json(self, *, name: str, pointer: str) -> Dict[str, Any]:
        """Delete a JSON field in npc.json using JSON Pointer."""

        npcs = self.get_npcs()
        if name not in npcs:
            raise KeyError(f"Unknown NPC: {name}")

        npc = npcs.get(name)
        if not isinstance(npc, dict):
            raise ValueError(f"Invalid NPC record: {name}")

        remove_at_pointer(npc, pointer)
        npcs[name] = npc
        _write_json(self._paths.npc_json, npcs)
        return npc

    def delete_location(self, name: str) -> None:
        """Remove a location from locations.json entirely."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Location name is required")
        locations = self.get_locations()
        if name not in locations:
            raise KeyError(f"Unknown location: {name}")
        del locations[name]
        _write_json(self._paths.locations_json, locations)

    def delete_npc(self, name: str) -> None:
        """Remove an NPC from npc.json entirely."""
        name = (name or "").strip()
        if not name:
            raise ValueError("NPC name is required")
        npcs = self.get_npcs()
        if name not in npcs:
            raise KeyError(f"Unknown NPC: {name}")
        del npcs[name]
        _write_json(self._paths.npc_json, npcs)

    def set_npc_last_acted(self, *, name: str, last_acted: str) -> Dict[str, Any]:
        """Set an NPC's last_acted timestamp in npc.json (runtime-managed)."""

        # Validate NPC exists.
        _ = self.get_npc(name)

        raw = str(last_acted or "").strip()
        if not raw or raw.lower() == "never":
            normalized = "never"
        else:
            normalized = WorldTime.parse(raw).to_string()

        npcs = self.get_npcs()
        npc = npcs.get(name)
        if not isinstance(npc, dict):
            raise ValueError(f"Invalid NPC record: {name}")

        npc["last_acted"] = normalized
        npcs[name] = npc
        _write_json(self._paths.npc_json, npcs)
        return npc

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

        return data

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

    def get_scene(self) -> Dict[str, Any]:
        return _read_json(self._paths.scene_json)

    def set_scene(self, scene: Dict[str, Any]) -> None:
        _write_json(self._paths.scene_json, scene)

    def clear_scene(self) -> None:
        _write_json(self._paths.scene_json, {"state": ""})
