from __future__ import annotations

from world_manager.tool_registry import (
    get_entity, get_entity_facts, get_entity_state, get_location_info,
    query_entities, adjust_entity,
    create_event, query_events,
    close_invocation,
    set_effect, remove_effect,
    escalate_to_gm,
)

# SM-specific tools (implemented in sm_tools.py)
from scene_manager.sm_tools import (
    manage_intent, add_perception,
    declare_intent, modify_intent,
    answer_player_question, write_session_narrative,
)

__all__ = [
    # From world_manager.tool_registry
    "get_entity", "get_entity_facts", "get_entity_state", "query_entities",
    "query_events", "create_event",
    "close_invocation",
    "get_location_info",
    "adjust_entity",
    "set_effect", "remove_effect",
    "escalate_to_gm",
    # From scene_manager.sm_tools
    "manage_intent", "add_perception",
    "write_session_narrative",
]
