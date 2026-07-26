"""LocationFileBackend — EntityBackend backed by locations.json.

Stores type="location" entities in game/world/locations.json.
Preserves legacy fields (summary, details, parent_location, etc.)
while adding entity fields (id, type, facts, tags, position, gurps, stats).

Read/write compatible with World.get_location() and World.create_location().
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from world_manager._backend_base import EntityBackend


_LOCATIONS_PATH = Path("game") / "world" / "locations.json"

_LOCATION_WRITE_LOCK = False


def _read_locations() -> Dict[str, Any]:
    """Read locations.json, return {} if missing/corrupt."""
    if not _LOCATIONS_PATH.exists():
        return {}
    try:
        raw = _LOCATIONS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_locations(data: Dict[str, Any]) -> None:
    """Atomically write locations.json."""
    _LOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOCATIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(_LOCATIONS_PATH)


def _generate_id() -> str:
    return f"loc_{uuid.uuid4().hex[:12]}"


class LocationFileBackend(EntityBackend):

    def set(self, id: str = "", type: str = "", base_type: str = "",
            facts: str = "", gurps: Optional[Dict[str, Any]] = None,
            state: Optional[Dict[str, Any]] = None,
            skills: Optional[Dict[str, Any]] = None,
            equipment: Optional[Dict[str, Any]] = None,
            stats: Optional[Dict[str, Any]] = None,
            tags: Optional[List[str]] = None,
            position: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = _read_locations()
        is_new = not id or id not in store

        if not id:
            id = _generate_id()

        if is_new:
            effective_facts = facts or f"A location known as {id}"
            entity: Dict[str, Any] = {
                "id": id,
                "type": type or "location",
                "base_type": base_type or "",
                "facts": effective_facts,
                "gurps": gurps or {},
                "stats": stats or {},
                "tags": tags or [],
                "position": position or {},
                "name": id,
                "summary": effective_facts[:200],
                "details": effective_facts,
                "parent_location": "",
                "sublocations_names": [],
                "npcs_present": [],
                "last_active": "",
                "scripts": [],
                "effects": [],
                "movement": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "deleted_at": None,
            }
            store[id] = entity
            operation = "created"
        else:
            entity = store[id]
            if type:
                entity["type"] = type
            if base_type:
                entity["base_type"] = base_type
            if facts:
                entity["facts"] = facts
                entity["summary"] = facts[:200]
                entity["details"] = facts
            if gurps:
                entity.setdefault("gurps", {}).update(gurps)
            if stats:
                entity.setdefault("stats", {}).update(stats)
            if tags is not None:
                entity["tags"] = tags
            if position:
                entity["position"] = position
            entity["updated_at"] = datetime.now(timezone.utc).isoformat()
            operation = "updated"

        _write_locations(store)

        return {
            "entity_id": id,
            "operation": operation,
            "inherited_from": entity.get("base_type", ""),
            "tags_total": entity.get("tags", []),
            "scripts_inherited": 0,
            "warnings": [],
        }

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        store = _read_locations()
        entity = store.get(id)
        if entity is None:
            return None
        # Ensure entity has all standard fields
        if "id" not in entity:
            entity["id"] = id
        if "type" not in entity:
            entity["type"] = "location"
        # facts fallback: details -> summary -> id
        if "facts" not in entity or not entity["facts"]:
            entity["facts"] = (
                entity.get("details") or
                entity.get("summary") or
                f"A location known as {id}"
            )
        if "tags" not in entity:
            entity["tags"] = []
        if "position" not in entity:
            entity["position"] = {}
        if "gurps" not in entity:
            entity["gurps"] = {}
        if "stats" not in entity:
            entity["stats"] = {}
        return entity

    def get_facts(self, id: str) -> Optional[str]:
        entity = self.get(id)
        if entity is None:
            return None
        return entity.get("facts") or entity.get("details") or entity.get("summary") or ""

    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        store = _read_locations()
        if id not in store:
            return {"error": f"Location '{id}' not found"}
        entity = store[id]
        entity["facts"] = facts
        entity["summary"] = facts[:200]
        entity["details"] = facts
        entity["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_locations(store)
        return {"entity_id": id, "operation": "facts_updated"}

    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        entity = self.get(id)
        if entity is None:
            return None
        return {
            "id": id,
            "gurps": entity.get("gurps", {}),
            "stats": entity.get("stats", {}),
            "active_effects": entity.get("effects", []),
        }

    def query(self, type_filter: str = "", base_type: str = "",
              tags: Optional[List[str]] = None,
              position: Optional[Dict[str, Any]] = None,
              radius: float = -1.0,
              facts_search: str = "",
              include_templates: bool = False,
              limit: int = 50) -> List[Dict[str, Any]]:
        store = _read_locations()
        results = []

        for eid, entity in store.items():
            if not isinstance(entity, dict):
                continue

            # type filter
            if type_filter and entity.get("type") != type_filter:
                continue
            if base_type and entity.get("base_type") != base_type:
                continue
            if not include_templates and entity.get("type", "").endswith("_template"):
                continue

            # tags filter: all listed must be present
            if tags:
                entity_tags = set(entity.get("tags", []))
                if not all(t in entity_tags for t in tags):
                    continue

            # facts_search (simple substring match)
            if facts_search:
                facts_text = (entity.get("facts") or entity.get("details") or entity.get("summary", "")).lower()
                if facts_search.lower() not in facts_text:
                    continue

            # Build result record
            result = {
                "id": eid,
                "type": entity.get("type", "location"),
                "base_type": entity.get("base_type", ""),
                "facts": entity.get("facts") or entity.get("details") or "",
                "gurps": entity.get("gurps", {}),
                "stats": entity.get("stats", {}),
                "tags": entity.get("tags", []),
                "position": entity.get("position", {}),
                "active_effects": entity.get("effects", []),
                "script_count": len(entity.get("scripts", [])),
            }
            results.append(result)

            if len(results) >= limit:
                break

        return results

    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        store = _read_locations()
        if id not in store:
            return {"entity_id": id, "status": "not_found"}
        store[id]["deleted_at"] = datetime.now(timezone.utc).isoformat()
        _write_locations(store)
        return {"entity_id": id, "status": "deleted"}

    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        store = _read_locations()
        if entity_id not in store:
            return {"error": f"Location '{entity_id}' not found"}
        entity = store[entity_id]
        existing = set(entity.get("tags", []))
        for t in tags:
            existing.add(t)
        entity["tags"] = sorted(existing)
        _write_locations(store)
        return {"entity_id": entity_id, "tags_added": tags, "tags_total": entity["tags"]}

    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        store = _read_locations()
        if entity_id not in store:
            return {"error": f"Location '{entity_id}' not found"}
        entity = store[entity_id]
        existing = set(entity.get("tags", []))
        for t in tags:
            existing.discard(t)
        entity["tags"] = sorted(existing)
        _write_locations(store)
        return {"entity_id": entity_id, "tags_removed": tags, "tags_total": entity["tags"]}

    # ── Stubs for unused methods ──

    def adjust(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        return {"error": "adjust not supported for locations"}

    def set_effect(self, entity_id: str, effect_id: str = "",
                   stat_deltas: Optional[Dict[str, Any]] = None,
                   per_tick: bool = True, duration_ticks: int = -1,
                   description: str = "", aura_radius: float = -1.0) -> Dict[str, Any]:
        return {"error": "set_effect not supported for locations"}

    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        return {"error": "remove_effect not supported for locations"}

    def attach_script(self, entity_id: str, script_id: str = "",
                      trigger_type: str = "",
                      trigger_params: Optional[Dict[str, Any]] = None,
                      script: str = "", comment: str = "") -> Dict[str, Any]:
        return {"error": "attach_script not supported for locations"}

    def detach_script(self, entity_id: str, script_id: str) -> Dict[str, Any]:
        return {"error": "detach_script not supported for locations"}
