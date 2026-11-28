"""Abstract base class for entity backends.

Defines the contract that every storage backend must implement.
Two implementations: JsonFileBackend (game/entity_store/entities.json) and
CompositeBackend (a thin pass-through to it).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class EntityBackend(ABC):
    """Abstract CRUD interface for entities.
    
    Two implementations:
      - JsonFileBackend: reads/writes game/entity_store/entities.json
      - CompositeBackend: thin pass-through to JsonFileBackend
    """

    @abstractmethod
    def set(self, id: str = "", type: str = "", **record: Any) -> Dict[str, Any]:
        """Create or fully overwrite an entity.

        The `record` dict contains type-specific fields pre-built by
        entity_schema.build_record(). The backend stores these fields
        directly, adding only storage-level fields (created_at, etc.).

        There is NO generic/default entity type — every record must match
        a concrete schema (location, npc, faction, etc.).

        Args:
            id: Entity identifier.
            type: Concrete entity type (location, npc, faction, ...).
            **record: Type-specific record fields.
        """
        ...

    @abstractmethod
    def get(self, id: str) -> Optional[Dict[str, Any]]:
        """Read entity by id. Returns full record or None."""
        ...

    @abstractmethod
    def get_facts(self, id: str) -> Optional[str]:
        """Read only the facts field."""
        ...

    @abstractmethod
    def set_facts(self, id: str, facts: str) -> Dict[str, Any]:
        """Update only the facts field."""
        ...

    @abstractmethod
    def get_state(self, id: str) -> Optional[Dict[str, Any]]:
        """Read mechanical state: gurps, state, skills, equipment, stats, effects."""
        ...

    @abstractmethod
    def query(self, type_filter: str = "", base_type: str = "",
              tags: Optional[List[str]] = None,
              position: Optional[Dict[str, Any]] = None,
              radius: float = -1.0,
              facts_search: str = "",
              include_templates: bool = False,
              limit: int = 50) -> List[Dict[str, Any]]:
        """Search entities by type, tags, position, text."""
        ...

    @abstractmethod
    def query_page(self, type_filter: str = "", base_type: str = "",
                   tags: Optional[List[str]] = None,
                   position: Optional[Dict[str, Any]] = None,
                   radius: float = -1.0,
                   facts_search: str = "",
                   include_templates: bool = False,
                   limit: int = 10,
                   offset: int = 0) -> Dict[str, Any]:
        """Search entities and report the store, the match, and one slice of it."""
        ...

    @abstractmethod
    def delete(self, id: str, reason: str = "") -> Dict[str, Any]:
        """Soft-delete an entity."""
        ...

    @abstractmethod
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
               current_state: str = "",
               gurps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Targeted runtime mutations — only provided fields change."""
        ...

    @abstractmethod
    def add_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def remove_tags(self, entity_id: str, tags: List[str]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def set_effect(self, entity_id: str, effect_id: str = "",
                   stat_deltas: Optional[Dict[str, Any]] = None,
                   period: int = 0,
                   duration: int = 0,
                   description: str = "") -> Dict[str, Any]:
        ...

    @abstractmethod
    def remove_effect(self, entity_id: str, effect_id: str) -> Dict[str, Any]:
        ...
