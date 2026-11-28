"""World runtime package.

Canonical home for world state persistence, time utilities, and JSON pointer helpers.

Public imports (preferred):
- from world import World, WorldTime, WorldDuration

Internal/advanced:
- _json_error_snippet (used for human-friendly JSON decode diagnostics)
"""

from .json_pointer import set_at_pointer
from .context import build_game_master_context_block
from .io import _json_error_snippet
from .state import World
from .time import WorldDuration, WorldTime

__all__ = [
    "World",
    "WorldTime",
    "WorldDuration",
    "set_at_pointer",
    "build_game_master_context_block",
    "_json_error_snippet",
]
