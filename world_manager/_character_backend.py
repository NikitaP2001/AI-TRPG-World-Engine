"""CharacterFileBackend — EntityBackend that reads/writes game/characters/{name}/*.json.

Maps entity fields to character file structure:
  Entity field     Character file
  facts            description.json (written as structured JSON)
  state            state.json (hp, fp, conditions, injuries)
  skills           skills.json (ST/DX/IQ/HT, cached_skills, passive_abilities)
  equipment        equipment.json (signature, inventory, currency)
  gurps            skills.json.stats (ST/DX/IQ/HT)
  tags             metadata.json._tags
  position         description.json.location (legacy format)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from world_manager._backend_base import EntityBackend


class CharacterFileBackend(EntityBackend):
    """Reads/writes player character data from game/characters/{name}/*.json files.
    
    Characters are NOT stored in entity_store JSON. They use per-file structure.
    This backend provides the EntityBackend interface so tools work uniformly.
    """

    def __init__(self, game_root: Path = Path("game")):
        self._char_root = (game_root / "characters").resolve()

    def _char_dir(self, name: str) -> Path:
        return (self._char_root / name).resolve()

    def _list_names(self) -> List[str]:
        if not self._char_root.exists():
            return []
        return sorted([p.name for p in self._char_root.iterdir() if p.is_dir()], key=str.lower)

    def _read_json(self, path: Path) -> dict:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _resolve_id(self, id: str) -> Optional[str]:
        """Match entity_id to a character folder name (case-insensitive)."""
        id_lower = id.strip().lower()
        for name in self._list_names():
            if name.lower() == id_lower:
                return name
        return None

    # ── EntityBackend interface ──

    def set(self, id: str = "", type: str = "", base_type: str = "",
            facts: str = "", gurps: Optional[Dict[str, Any]] = None,
            state: Optional[Dict[str, Any]] = None,
            skills: Optional[Dict[str, Any]] = None,
            equipment: Optional[Dict[str, Any]] = None,
            stats: Optional[Dict[str, Any]] = None,
            tags: Optional[List[str]] = None,
            position: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create or update character files. Creates directory if new."""
        resolved = self._resolve_id(id)
        name = resolved or id.strip()
        if not name:
            return {"entity_id": id, "operation": "error", "warnings": ["Character name required"]}

        char_dir = self._char_dir(name)

        if not resolved:
            # New character — create directory and description.json
            char_dir.mkdir(parents=True, exist_ok=True)
            desc = {"general": facts, "race": "", "personality": {}}
            self._write_json(char_dir / "description.json", desc)
            if state is not None:
                self._write_json(char_dir / "state.json", state)
            if skills is not None:
                self._write_json(char_dir / "skills.json", skills)
            if equipment is not None:
                self._write_json(char_dir / "equipment.json", equipment)
            if tags:
                self._write_json(char_dir / "metadata.json", {"_tags": tags})
            operation = "created"
        else:
            # Update existing
            if facts:
                desc = self._read_json(char_dir / "description.json")
                desc["general"] = facts
                self._write_json(char_dir / "description.json", desc)
            if state is not None:
                self._write_json(char_dir / "state.json", state)
            if skills is not None:
                self._write_json(char_dir / "skills.json", skills)
            if equipment is not None:
                self._write_json(char_dir / "equipment.json", equipment)
            if tags is not None:
                meta = self._read_json(char_dir / "metadata.json")
                meta["_tags"] = tags
                self._write_json(char_dir / "metadata.json", meta)
            operation = "updated"

        return {"entity_id": name, "operation": operation, "warnings": []}

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        name = self._resolve_id(id)
        if not name:
            return None
        char_dir = self._char_dir(name)
        desc = self._read_json(char_dir / "description.json")
        state = self._read_json(char_dir / "state.json")
        skills = self._read_json(char_dir / "skills.json")
        equip = self._read_json(char_dir / "equipment.json")
        meta = self._read_json(char_dir / "metadata.json")

        gurps_stats = skills.get("stats", {}) if skills else {}

        return {
            "id": name,
            "type": "character",
            "base_type": "",
            "facts": desc.get("general", ""),
            "gurps": gurps_stats,
            "state": state,
            "skills": skills,
            "equipment": equip,
            "stats": {},
            "tags": meta.get("_tags", []),
            "position": {},
            "scripts": [],
            "effects": [],
            "movement": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        }

    def get_facts(self, id: str) -> Optional[str]:
        entity = self.get(id)
        if entity is None:
            return None
        return entity.get("facts", "")

    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        name = self._resolve_id(id)
        if not name:
            return {"entity_id": id, "operation": "error", "warnings": ["Character not found"]}
        char_dir = self._char_dir(name)
        desc = self._read_json(char_dir / "description.json")
        desc["general"] = facts
        self._write_json(char_dir / "description.json", desc)
        return {"entity_id": name, "operation": "facts_updated"}

    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        entity = self.get(id)
        if entity is None:
            return None
        return {
            "id": entity["id"],
            "gurps": entity.get("gurps", {}),
            "state": entity.get("state", {}),
            "skills": entity.get("skills", {}),
            "equipment": entity.get("equipment", {}),
            "stats": {},
            "active_effects": [],
        }

    def query(self, type_filter: str = "", base_type: str = "",
              tags: Optional[List[str]] = None,
              position: Optional[Dict[str, Any]] = None,
              radius: float = -1.0,
              facts_search: str = "",
              include_templates: bool = False,
              limit: int = 50) -> List[Dict[str, Any]]:
        """List characters, optionally filtered by type='character' or tags."""
        if type_filter and type_filter != "character":
            return []
        results = []
        tag_set = set(tags or [])
        for name in self._list_names():
            entity = self.get(name)
            if entity is None:
                continue
            if tag_set:
                entity_tags = set(entity.get("tags", []))
                if not tag_set.issubset(entity_tags):
                    continue
            if facts_search:
                facts = (entity.get("facts") or "").lower()
                if facts_search.lower() not in facts:
                    continue
            results.append({
                "id": entity["id"],
                "type": "character",
                "base_type": "",
                "facts": entity.get("facts", ""),
                "gurps": entity.get("gurps", {}),
                "state": entity.get("state", {}),
                "skills": entity.get("skills", {}),
                "equipment": entity.get("equipment", {}),
                "stats": {},
                "tags": entity.get("tags", []),
                "position": {},
                "active_effects": [],
                "script_count": 0,
            })
            if len(results) >= limit:
                break
        return results

    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        name = self._resolve_id(id)
        if not name:
            return {"entity_id": id, "status": "not_found"}
        # Character deletion = metadata marker, not file deletion
        char_dir = self._char_dir(name)
        meta = self._read_json(char_dir / "metadata.json")
        meta["deleted_at"] = datetime.now(timezone.utc).isoformat()
        meta["delete_reason"] = reason
        self._write_json(char_dir / "metadata.json", meta)
        return {"entity_id": name, "status": "deleted"}

    def adjust(self, entity_id: str, *,
               hp: Optional[int] = None,
               fp: Optional[int] = None,
               er: Optional[int] = None,
               add_condition: str = "",
               remove_condition: str = "",
               add_injury: str = "",
               remove_injury: str = "",
               add_item: str = "",
               remove_item: str = "",
               currency_delta: Optional[Dict[str, int]] = None,
               add_ability: str = "",
               remove_ability: str = "",
               add_skill: str = "",
               remove_skill: str = "",
               current_state: str = "") -> Dict[str, Any]:
        name = self._resolve_id(entity_id)
        if not name:
            return {"entity_id": entity_id, "error": "Character not found"}
        char_dir = self._char_dir(name)
        mutations: List[str] = []

        # ── State ──
        state = self._read_json(char_dir / "state.json")
        if hp is not None:
            old = state.get("hp")
            state["hp"] = max(0, hp)
            mutations.append(f"hp: {old} -> {hp}")
        if fp is not None:
            old = state.get("fp")
            state["fp"] = max(0, fp)
            mutations.append(f"fp: {old} -> {fp}")
        if er is not None:
            old = state.get("er")
            state["er"] = max(0, er)
            mutations.append(f"er: {old} -> {er}")
        if add_condition:
            conds = state.setdefault("conditions", [])
            if add_condition not in conds:
                conds.append(add_condition)
                mutations.append(f"+condition: {add_condition}")
        if remove_condition:
            conds = state.setdefault("conditions", [])
            if remove_condition in conds:
                conds.remove(remove_condition)
                mutations.append(f"-condition: {remove_condition}")
        if add_injury:
            injs = state.setdefault("injuries", [])
            if add_injury not in injs:
                injs.append(add_injury)
                mutations.append(f"+injury: {add_injury}")
        if remove_injury:
            injs = state.setdefault("injuries", [])
            if remove_injury in injs:
                injs.remove(remove_injury)
                mutations.append(f"-injury: {remove_injury}")
        if current_state:
            state["current_state"] = current_state
            mutations.append(f"current_state: {current_state}")
        self._write_json(char_dir / "state.json", state)

        # ── Equipment ──
        equip = self._read_json(char_dir / "equipment.json")
        if add_item:
            inv = equip.setdefault("inventory", [])
            if add_item not in inv:
                inv.append(add_item)
                mutations.append(f"+item: {add_item}")
        if remove_item:
            inv = equip.setdefault("inventory", [])
            if remove_item in inv:
                inv.remove(remove_item)
                mutations.append(f"-item: {remove_item}")
        if currency_delta:
            currency = equip.setdefault("currency", {})
            for coin, delta in currency_delta.items():
                old_val = currency.get(coin, 0)
                new_val = max(0, old_val + delta)
                currency[coin] = new_val
                mutations.append(f"currency.{coin}: {old_val} -> {new_val}")
        self._write_json(char_dir / "equipment.json", equip)

        # ── Skills ──
        skills = self._read_json(char_dir / "skills.json")
        if add_ability:
            abils = skills.setdefault("passive_abilities", [])
            if add_ability not in abils:
                abils.append(add_ability)
                mutations.append(f"+ability: {add_ability}")
        if remove_ability:
            abils = skills.setdefault("passive_abilities", [])
            if remove_ability in abils:
                abils.remove(remove_ability)
                mutations.append(f"-ability: {remove_ability}")
        if add_skill:
            cached = skills.setdefault("cached_skills", [])
            if add_skill not in cached:
                cached.append(add_skill)
                mutations.append(f"+skill: {add_skill}")
        if remove_skill:
            cached = skills.setdefault("cached_skills", [])
            if remove_skill in cached:
                cached.remove(remove_skill)
                mutations.append(f"-skill: {remove_skill}")
        self._write_json(char_dir / "skills.json", skills)

        return {
            "entity_id": name,
            "mutations_applied": mutations,
            "state_summary": {
                "hp": state.get("hp"),
                "fp": state.get("fp"),
                "conditions": len(state.get("conditions", [])),
                "injuries": len(state.get("injuries", [])),
            },
        }

    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        name = self._resolve_id(entity_id)
        if not name:
            return {"entity_id": entity_id, "tags_added": [], "tags_total": []}
        char_dir = self._char_dir(name)
        meta = self._read_json(char_dir / "metadata.json")
        existing = set(meta.get("_tags", []))
        for t in tags:
            existing.add(t)
        meta["_tags"] = sorted(existing)
        self._write_json(char_dir / "metadata.json", meta)
        return {"entity_id": name, "tags_added": tags, "tags_total": meta["_tags"]}

    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        name = self._resolve_id(entity_id)
        if not name:
            return {"entity_id": entity_id, "tags_removed": [], "tags_total": []}
        char_dir = self._char_dir(name)
        meta = self._read_json(char_dir / "metadata.json")
        existing = set(meta.get("_tags", []))
        removed = []
        for t in tags:
            if t in existing:
                existing.remove(t)
                removed.append(t)
        meta["_tags"] = sorted(existing)
        self._write_json(char_dir / "metadata.json", meta)
        return {"entity_id": name, "tags_removed": removed, "tags_total": meta["_tags"]}

    def set_effect(self, entity_id: str, effect_id: str = "",
                   stat_deltas: Optional[Dict[str, Any]] = None,
                   per_tick: bool = True,
                   duration_ticks: int = -1,
                   description: str = "") -> Dict[str, Any]:
        # Characters don't support effects via filesystem yet — store in state.json conditions
        name = self._resolve_id(entity_id)
        if not name:
            return {"effect_id": "", "entity_id": entity_id, "warnings": ["Character not found"]}
        # For now, append description as a condition
        if description:
            self.adjust(entity_id, add_condition=description)
        return {"effect_id": effect_id, "entity_id": name, "expires_at_tick": None, "warnings": []}

    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        return {"entity_id": entity_id, "effect_id": effect_id, "status": "removed"}

    def attach_script(self, entity_id: str, script_id: str = "",
                      trigger_type: str = "",
                      trigger_params: Optional[Dict[str, Any]] = None,
                      script: str = "", comment: str = "") -> Dict[str, Any]:
        return {"script_id": "", "entity_id": entity_id, "trigger_type": trigger_type,
                "warnings": ["Scripts not supported for character files"]}

    def detach_script(self, entity_id: str, script_id: str) -> Dict[str, Any]:
        return {"entity_id": entity_id, "script_id": script_id, "status": "not_found"}
