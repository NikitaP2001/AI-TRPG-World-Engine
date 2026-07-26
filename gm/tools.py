from __future__ import annotations
from langchain_core.tools import tool

# Re-export shared infrastructure (used by turn_runner, console_app, etc.)
from engine.tool_base import (
    reset_cycle_lock, is_context_changed, is_cycle_locked, signal_context_changed,
)

# Re-export GM tools from ToolRegistry
from world_manager.tool_registry import (
    set_entity, set_entity_facts, adjust_entity,
    get_entity, get_entity_facts, get_entity_state,
    query_entities, query_events, record_event,
    add_tags, remove_tags,
    set_effect, remove_effect,
    attach_script, detach_script,
    invoke_self, close_invocation,
    notify_wm, escalate_to_sm,
)

__all__ = [
    "reset_cycle_lock", "is_context_changed", "is_cycle_locked", "signal_context_changed",
    "set_entity", "set_entity_facts", "adjust_entity",
    "get_entity", "get_entity_facts", "get_entity_state",
    "add_tags", "remove_tags",
    "query_entities", "query_events", "record_event",
    "attach_script", "detach_script",
    "set_effect", "remove_effect",
    "invoke_self", "close_invocation",
    "notify_wm", "escalate_to_sm",
]
