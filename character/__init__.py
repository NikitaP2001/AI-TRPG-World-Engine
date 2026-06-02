"""Character runtime package.

Contains the character-agent invocation and per-character memory helpers.
"""

from .agent import run_character_agent
from .memory import ensure_memory_file, update_turn_memory

__all__ = [
    "ensure_memory_file",
    "run_character_agent",
    "update_turn_memory",
]
