"""CompositeBackend — auto-routes each entity_id to JsonFileBackend or CharacterFileBackend.

Lookup order:
  1. Is entity_id a key in JsonFileBackend's cache? → use JsonFileBackend
  2. Is entity_id a character folder name? → use CharacterFileBackend
  3. Is there a tag "name:<entity_id>" in JsonFileBackend? → use JsonFileBackend
  4. Default: JsonFileBackend (let it create)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._backend_base import EntityBackend


class CompositeBackend(EntityBackend):
    """Routes each operation to the correct backend based on entity_id.
    
    Writes go to ONE backend. Reads from JsonFileBackend first, falls back
    to CharacterFileBackend.
    """

    def __init__(self, json_backend: EntityBackend, char_backend: EntityBackend):
        self._json = json_backend
        self._char = char_backend

    def _resolve(self, id: str) -> tuple[EntityBackend, str]:
        """Returns (backend, resolved_id). Backend is always JsonFileBackend
        for creation (lets it auto-generate id if needed).
        """
        if not id:
            return (self._json, id)
        id_stripped = id.strip()
        # 1. Check JSON store (by exact key)
        if self._json.get(id_stripped) is not None:
            return (self._json, id_stripped)
        # 2. Check character folders
        if self._char.get(id_stripped) is not None:
            return (self._char, id_stripped)
        # 3. Check name tags in JSON store
        try:
            # query with tag filter — uses JsonFileBackend's own query
            matches = self._json.query(tags=[f"name:{id_stripped}"], include_templates=False, limit=1)
            if matches:
                return (self._json, matches[0]["id"])
        except Exception:
            pass
        # 4. Default: JSON store
        return (self._json, id_stripped)

    def _resolve_read(self, id: str) -> tuple[EntityBackend, str]:
        """For reads, check both backends. JSON first."""
        if not id:
            return (self._json, id)
        id_stripped = id.strip()
        json_entity = self._json.get(id_stripped)
        if json_entity is not None:
            return (self._json, id_stripped)
        char_entity = self._char.get(id_stripped)
        if char_entity is not None:
            return (self._char, id_stripped)
        return (self._json, id_stripped)

    def set(self, id: str = "", **kwargs) -> Dict[str, Any]:
        backend, rid = self._resolve(id or "")
        return backend.set(id=rid, **kwargs)

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        backend, rid = self._resolve_read(id)
        return backend.get(rid)

    def get_facts(self, id: str) -> Optional[str]:
        backend, rid = self._resolve_read(id)
        return backend.get_facts(rid)

    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        backend, rid = self._resolve(id)
        return backend.set_facts(rid, facts)

    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        backend, rid = self._resolve_read(id)
        return backend.get_state(rid)

    def query(self, **kwargs) -> List[Dict[str, Any]]:
        """Query both backends and merge results."""
        results = []
        json_results = self._json.query(**kwargs)
        results.extend(json_results)
        char_results = self._char.query(**kwargs)
        for cr in char_results:
            if not any(r.get("id") == cr.get("id") for r in results):
                results.append(cr)
        return results

    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        backend, rid = self._resolve(id)
        return backend.delete(rid, reason)

    def adjust(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.adjust(rid, **kwargs)

    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.add_tags(rid, tags)

    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.remove_tags(rid, tags)

    def set_effect(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.set_effect(rid, **kwargs)

    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.remove_effect(rid, effect_id)

    def attach_script(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.attach_script(rid, **kwargs)

    def detach_script(self, entity_id: str, script_id: str) -> Dict[str, Any]:
        backend, rid = self._resolve(entity_id)
        return backend.detach_script(rid, script_id)

    def validate_facts(self, facts: str, entity_id: str = "") -> List[str]:
        return self._json.validate_facts(facts, entity_id)
