from __future__ import annotations
from langchain_core.tools import tool
from world_manager.tool_registry import (
    set_entity, set_entity_facts, adjust_entity,
    get_entity, get_entity_facts, get_entity_state,
    query_entities,
    add_tags, remove_tags,
    set_effect, remove_effect,
    attach_script, detach_script,
    record_event, query_events,
    invoke_self, close_invocation,
    delete_entity, set_movement, write_correction,
    read_tool_doc,
)

@tool
def world_setting_result(setting_json: str) -> str:
    """Submit the world setting. Called exactly once at startup.

    Args:
        setting_json: JSON string with world_essence, gurps_calibration,
            initial_world_time, and optional player_reference.
    """
    return "ok"

