"""Entity Registry — public API for entity CRUD.

Thin re-export from entity_core (CompositeBackend).
All existing imports (from world_manager.entity_store import ...) continue to work.

Storage is routed automatically:
  - NPCs, locations, factions -> game/entity_store/entities.json
  - Player mechanical data -> game/entity_store/entities.json as well; the
    player-specific files (description.json, memory.json, messages.json) stay
    under game/players/{name}/
"""
from __future__ import annotations

from world_manager.entity_core import (
    set_entity, get_entity, get_entity_facts, set_entity_facts,
    get_entity_state, delete_entity, query_entities, query_page,
    add_tags, remove_tags, set_effect, remove_effect,
    adjust_entity,
)

__all__ = [
    "set_entity", "get_entity", "get_entity_facts", "set_entity_facts",
    "get_entity_state", "delete_entity", "query_entities", "query_page",
    "add_tags", "remove_tags", "set_effect", "remove_effect",
    "adjust_entity",
]
