from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import json
import copy

from world import World, WorldDuration, WorldTime
from world.io import write_json


@dataclass
class Scene:
    """Scene lifecycle manager.

    Persisted in game/world/scene.json.

        New architecture (v2):
        - Scenes use ``state: 'active'`` while running; otherwise scene is cleared.
    - Each character entry has ``acted: bool`` instead of
      ``planning_complete``.
    - No ``iteration`` sub-object, no ``open_questions``, ``observations``,
      ``plan_submitted``, or ``planned_actions``.
    - Characters are executed one-by-one in initiative order automatically;
      no multi-round iteration logic.
    """

    world: World

    def start(
        self,
        *,
        character_names: List[str],
        location: str,
        npc_names: Optional[List[str]] = None,
        scene_description: str = "",
        player_descriptions: Optional[Dict[str, str]] = None,
        time_shift: str = "0",
    ) -> Dict[str, Any]:
        if not character_names:
            raise ValueError("character_names must not be empty")

        # Validate location exists.
        _ = self.world.get_location(location)

        existing = self.world.get_scene()
        if isinstance(existing, dict) and existing.get("state") == "active":
            # Idempotency: if the exact same scene is already active, return it.
            try:
                existing_loc = str(existing.get("location") or "")
                existing_chars = existing.get("characters")
                existing_names = list(existing_chars.keys()) if isinstance(existing_chars, dict) else []
                existing_npcs = existing.get("npcs")
                existing_npc_names = (
                    [str(x) for x in existing_npcs if str(x).strip()] if isinstance(existing_npcs, list) else []
                )
                desired_npc_names = [str(x) for x in (npc_names or []) if str(x).strip()]
                if (
                    existing_loc == location
                    and sorted([str(x) for x in existing_names]) == sorted([str(x) for x in character_names])
                    and sorted(existing_npc_names) == sorted(desired_npc_names)
                ):
                    return existing
            except Exception:
                pass
            raise ValueError("A scene is already active")
        # Validate characters and align their location if unspecified.
        # Strict mode: do NOT silently assign/teleport characters into scene locations.
        for name in character_names:
            desc = self.world.get_character_description(name)
            current_loc = str(desc.get("location") or "").strip()
            if current_loc and current_loc != location:
                raise ValueError(
                    f"Character '{name}' has location '{current_loc}', cannot start scene at '{location}'."
                )
            if not current_loc:
                raise ValueError(
                    f"Character '{name}' has no location set; set /location first before starting scene at '{location}'."
                )

        start_time = self.world.get_world_time().to_string()

        # Use the earliest last_acted among participants so the scene picks up
        # where these characters actually are in the timeline, not at global
        # world time (which may be far ahead if other characters advanced it).
        earliest_sec: Optional[int] = None
        for name in character_names:
            try:
                desc = self.world.get_character_description(name)
                la = str(desc.get("last_acted") or "").strip() if isinstance(desc, dict) else ""
                if la and la != "never":
                    la_sec = WorldTime.parse(la).to_seconds()
                    if earliest_sec is None or la_sec < earliest_sec:
                        earliest_sec = la_sec
            except Exception:
                continue
        if earliest_sec is not None:
            start_time = WorldTime.from_seconds(earliest_sec).to_string()

        # Optional passive time skip before scene start (e.g., sleep/unconscious).
        shift_text = str(time_shift or "0").strip() or "0"
        if shift_text not in {"0", "0s", "0m", "0h", "0d"}:
            try:
                shift = WorldDuration.parse_user_input(shift_text)
                shifted = WorldTime.parse(start_time).add_duration(shift)
                start_time = shifted.to_string()
            except Exception as exc:
                raise ValueError(f"Invalid time_shift '{shift_text}': {exc}") from exc

        desired_npc_names = [str(x) for x in (npc_names or []) if str(x).strip()]
        if desired_npc_names:
            # Validate NPCs exist and are at the scene location.
            npcs = self.world.get_npcs()
            for npc_name in desired_npc_names:
                if npc_name not in npcs:
                    raise ValueError(f"Unknown NPC: {npc_name}")
                npc = npcs.get(npc_name)
                npc_loc = str(npc.get("location") or "").strip() if isinstance(npc, dict) else ""
                if npc_loc and npc_loc != location:
                    raise ValueError(
                        f"NPC '{npc_name}' has location '{npc_loc}', cannot start scene at '{location}'."
                    )

        scene = {
            "state": "active",
            "location": location,
            "start_time": start_time,
            "scene_description": str(scene_description or ""),
            "player_descriptions": player_descriptions if isinstance(player_descriptions, dict) else {},
            "npcs": desired_npc_names,
            "initiative_order": [str(x) for x in character_names if str(x).strip()],
            "characters": {
                name: {
                    "acted": False,
                    "last_decision": "",
                    "last_thoughts": "",
                    "last_gm_answers": "",
                    "character_input": "",
                }
                for name in character_names
            },
        }
        self.world.set_scene(scene)

        # Auto-maintain location last_active timestamp
        try:
            self.world.add_location_json(
                name=location,
                pointer="/last_active",
                value=json.dumps(start_time, ensure_ascii=False),
            )
        except Exception:
            pass  # Non-critical; don't block scene start

        return scene

    def add_npc_to_scene(self, npc_name: str) -> Dict[str, Any]:
        scene = self.require_active()
        npc_name = str(npc_name or "").strip()
        if not npc_name:
            raise ValueError("npc_name is required")

        npcs = scene.get("npcs")
        if not isinstance(npcs, list):
            npcs = []
        if npc_name not in npcs:
            npcs.append(npc_name)
        scene["npcs"] = npcs
        self.world.set_scene(scene)
        return scene

    def require_active(self) -> Dict[str, Any]:
        scene = self.world.get_scene()
        if not isinstance(scene, dict) or scene.get("state") != "active":
            raise ValueError("No active scene")
        return scene

    def validate_character_in_scene(self, character_name: str) -> Dict[str, Any]:
        scene = self.require_active()
        chars = scene.get("characters") or {}
        if character_name not in chars:
            raise ValueError(f"Character '{character_name}' is not part of the current scene")
        if (chars[character_name] or {}).get("acted") is True:
            raise ValueError(f"Character '{character_name}' already acted this turn")
        return scene

    def mark_character_acted(
        self,
        character_name: str,
        *,
        last_decision: str,
        last_thoughts: str = "",
        last_gm_answers: str = "",
        character_input: str = "",
        output_source: str = "model",
    ) -> Dict[str, Any]:
        scene = self.require_active()
        chars = scene.get("characters") or {}
        if character_name not in chars:
            raise ValueError(f"Character '{character_name}' is not part of the current scene")
        entry = chars[character_name] or {}
        entry["acted"] = True
        entry["last_decision"] = last_decision or ""
        entry["last_thoughts"] = last_thoughts or ""
        entry["last_gm_answers"] = last_gm_answers or ""
        entry["character_input"] = character_input or ""
        entry["output_source"] = output_source or "model"
        chars[character_name] = entry
        scene["characters"] = chars
        self.world.set_scene(scene)
        return scene

    def all_characters_ended(self) -> bool:
        scene = self.require_active()
        chars = scene.get("characters") or {}
        return all((v or {}).get("acted") is True for v in chars.values())

    def update_character_intent(
        self,
        character_name: str,
        *,
        last_decision: str,
        last_thoughts: str = "",
        last_gm_answers: str = "",
        output_source: str = "model",
    ) -> Dict[str, Any]:
        """Replace intent/thoughts for a character who already acted this turn."""
        scene = self.require_active()
        chars = scene.get("characters") or {}
        if character_name not in chars:
            raise ValueError(f"Character '{character_name}' is not part of the current scene")

        entry = chars[character_name] or {}
        if entry.get("acted") is not True:
            raise ValueError(f"Character '{character_name}' has not acted yet")

        entry["last_decision"] = last_decision or ""
        entry["last_thoughts"] = last_thoughts or ""
        entry["last_gm_answers"] = last_gm_answers or ""
        entry["output_source"] = output_source or "model"
        chars[character_name] = entry
        scene["characters"] = chars
        self.world.set_scene(scene)
        return scene

    def end_with_gm_output(self, *, narration: str, location: str, turn_duration: str) -> Dict[str, Any]:
        scene = self.require_active()

        if location != scene.get("location"):
            raise ValueError("Output location must match the current scene location")

        # Validate location exists.
        loc_obj = self.world.get_location(location)

        current_world_time = self.world.get_world_time()
        duration = WorldDuration.parse_user_input(str(turn_duration))
        start = WorldTime.parse(str(scene.get("start_time")))
        new_t = start.add_duration(duration)

        if not self.all_characters_ended():
            raise ValueError("Not all characters ended their turns yet")

        # Snapshot the scene before we mutate/clear it.
        scene_snapshot = copy.deepcopy(scene) if isinstance(scene, dict) else {}

        recap_characters: List[Dict[str, Any]] = []
        try:
            chars = scene_snapshot.get("characters") if isinstance(scene_snapshot, dict) else None
            if isinstance(chars, dict):
                for name, entry in chars.items():
                    e = entry if isinstance(entry, dict) else {}
                    recap_characters.append(
                        {
                            "name": str(name),
                            "acted": bool(e.get("acted") is True),
                            "last_decision": str(e.get("last_decision") or "").strip(),
                            "last_thoughts": str(e.get("last_thoughts") or "").strip(),
                            "last_gm_answers": str(e.get("last_gm_answers") or "").strip(),
                            "character_input": str(e.get("character_input") or "").strip(),
                        }
                    )
        except Exception:
            recap_characters = []

        recap = {
            "location": str(scene_snapshot.get("location") or ""),
            "start_time": str(scene_snapshot.get("start_time") or ""),
            "turn_duration": duration.to_string(),
            "npcs": list(scene_snapshot.get("npcs") or [])
            if isinstance(scene_snapshot.get("npcs"), list)
            else [],
            "characters": recap_characters,
        }

        # Update world info.
        # World time only advances when a character goes beyond the current max.
        info = self.world.get_info()
        info[World.K_LAST_LOCATION] = location
        if new_t.to_seconds() > current_world_time.to_seconds():
            info[World.K_TIME] = new_t.to_string()
        write_json(self.world.paths.info_json, info)

        # Append this turn to the story's ongoing paragraph (10 turns per paragraph).
        char_names = list((scene.get("characters") or {}).keys())
        npc_names = scene.get("npcs") if isinstance(scene.get("npcs"), list) else []
        self.world.append_turn_to_story(
            narration=narration or "",
            start_time=str(scene.get("start_time") or ""),
            end_time=new_t.to_string(),
            location=location,
            characters=[str(x) for x in char_names if str(x).strip()],
            npcs=[str(x) for x in npc_names if str(x).strip()],
        )

        # Update each character in scene.
        for char_name in (scene.get("characters") or {}).keys():
            self.world.add_character_json(
                name=char_name,
                pointer="/location",
                value=json.dumps(location, ensure_ascii=False),
            )
            self.world.add_character_json(
                name=char_name,
                pointer="/last_acted",
                value=json.dumps(new_t.to_string(), ensure_ascii=False),
            )

        # Update each NPC in scene.
        for npc_name in (scene.get("npcs") or []):
            nm = str(npc_name).strip()
            if not nm:
                continue
            try:
                # Keep NPC location consistent with the scene they participated in.
                self.world.update_npc_json(
                    name=nm,
                    pointer="/location",
                    value=json.dumps(location, ensure_ascii=False),
                )
            except Exception:
                pass
            try:
                self.world.set_npc_last_acted(name=nm, last_acted=new_t.to_string())
            except Exception:
                pass

        # Resync info.json characters list.
        self.world._sync_info_characters()

        # Clear scene.
        self.world.clear_scene()

        loc_summary = ""
        try:
            if isinstance(loc_obj, dict):
                loc_summary = str(loc_obj.get("summary") or "").strip()
        except Exception:
            loc_summary = ""

        return {
            "narration": narration or "",
            "location": location,
            "turn_duration": duration.to_string(),
            "new_time": new_t.to_string(),
            "scene_recap": recap,
            "location_summary": loc_summary,
        }
