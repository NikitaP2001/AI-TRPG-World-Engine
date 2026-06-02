"""Runtime scene package.

This is the engine-side scene lifecycle manager (not the LLM agent in agents/scene_manager/).

Preferred import:
- from scene import Scene
"""

from .runtime import Scene
from .context import (
    build_focused_scene_context,
    collect_story_turns_newest_first,
    estimate_scene_start_time_for_history,
    extract_scene_result_from_narration,
    find_reusable_scene_description,
    story_turn_fingerprint,
)

__all__ = [
    "Scene",
    "build_focused_scene_context",
    "collect_story_turns_newest_first",
    "estimate_scene_start_time_for_history",
    "extract_scene_result_from_narration",
    "find_reusable_scene_description",
    "story_turn_fingerprint",
]
