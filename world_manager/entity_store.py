"""Entity Registry — public API for entity CRUD.

Thin re-export from entity_core (CompositeBackend).
All existing imports (from world_manager.entity_store import ...) continue to work.

Storage is routed automatically:
  - NPCs, locations, factions -> game/entity_store/entities.json
  - Player characters -> game/characters/{name}/*.json
"""
from __future__ import annotations

from world_manager.entity_core import (
    set_entity, get_entity, get_entity_facts, set_entity_facts,
    get_entity_state, delete_entity, query_entities,
    add_tags, remove_tags, set_effect, remove_effect,
    attach_script, detach_script, adjust_entity,
    reset_creation_counter,
)

__all__ = [
    "set_entity", "get_entity", "get_entity_facts", "set_entity_facts",
    "get_entity_state", "delete_entity", "query_entities",
    "add_tags", "remove_tags", "set_effect", "remove_effect",
    "attach_script", "detach_script", "adjust_entity",
]
