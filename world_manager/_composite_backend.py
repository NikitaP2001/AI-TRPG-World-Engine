"""CompositeBackend — thin pass-through to JsonFileBackend.

Previously routed between JsonFileBackend and CharacterFileBackend.
CharacterFileBackend has been removed — all entities (including player
mechanical data) now go through the single JsonFileBackend path.

Player-specific files (description.json, metadata.json, memory.json,
messages.json) remain on disk under game/players/{name}/ — the entity
store only contains mechanical data (gurps, stats, state, skills, etc.).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from world_manager._backend_base import EntityBackend


class CompositeBackend(EntityBackend):
    """Thin pass-through to the single JsonFileBackend."""

    def __init__(self, json_backend: EntityBackend):
        self._json = json_backend

    def set(self, id: str = "", **kwargs) -> Dict[str, Any]:
        return self._json.set(id=id, **kwargs)

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        return self._json.get(id)

    def get_facts(self, id: str) -> Optional[str]:
        return self._json.get_facts(id)

    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        return self._json.set_facts(id, facts)

    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        return self._json.get_state(id)

    def query(self, **kwargs) -> List[Dict[str, Any]]:
        return self._json.query(**kwargs)

    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        return self._json.delete(id, reason)

    def adjust(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        return self._json.adjust(entity_id, **kwargs)

    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        return self._json.add_tags(entity_id, tags)

    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        return self._json.remove_tags(entity_id, tags)

    def set_effect(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        return self._json.set_effect(entity_id, **kwargs)

    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        return self._json.remove_effect(entity_id, effect_id)

    def attach_script(self, entity_id: str, **kwargs) -> Dict[str, Any]:
        return self._json.attach_script(entity_id, **kwargs)

    def detach_script(self, entity_id: str, script_id: str) -> Dict[str, Any]:
        return self._json.detach_script(entity_id, script_id)

    def validate_facts(self, facts: str, entity_id: str = "") -> List[str]:
        from world_manager.entity_validation import validate_facts as _vfacts
        return _vfacts(facts, entity_id)
