"""GM context injection for the game orchestrator.

Manages GM history bootstrap and scene-pick entity injection.
Extracted from GameOrchestrator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from gm.history_injector import GMHistoryInjector
from world import World


class GMContextManager:
    """Manages GM history: bootstrap anchors, scene-pick entity injections.

    Encapsulates the relationship between GMHistoryInjector and World.
    """

    def __init__(self, gm_injector: GMHistoryInjector, world: World) -> None:
        self._gm_injector = gm_injector
        self._world = world

    def history_contains(self, marker: str) -> bool:
        """Check if a marker exists in GM history."""
        return self._gm_injector.history_contains(marker)

    def ensure_bootstrap(self) -> None:
        """Inject atomic world-state anchor messages into GM history on first run.

        Idempotent: skips if legacy [world_snapshot:world] marker already present.
        """
        try:
            if self.history_contains("[world_snapshot:world]"):
                return

            self._gm_injector.ensure_world_meta(world=self._world)

            for name in self._world.list_character_names():
                if not name:
                    continue
                self._gm_injector.ensure_character_description(world=self._world, name=str(name))

            self._gm_injector.ensure_story_summaries(world=self._world)
        except Exception:
            pass

    def inject_entity_description(self, marker: str, content: str) -> None:
        """Inject a single entity description if absent."""
        if not marker or not content:
            return
        self._gm_injector.inject_if_absent(marker=marker, content=content)

    def inject_scene_pick_context(
        self,
        selected_location: str,
        selected_characters: List[str],
        selected_npcs: List[str],
    ) -> None:
        """Inject location/character/NPC descriptions for a scene pick."""
        if selected_location:
            self._gm_injector.ensure_location_description(world=self._world, location=selected_location)
        for name in selected_characters:
            if not name:
                continue
            self._gm_injector.ensure_character_description(world=self._world, name=str(name))
        for name in selected_npcs:
            if not name:
                continue
            self._gm_injector.ensure_npc_description(world=self._world, name=str(name))

    def inject_scene_description_context(
        self,
        location: str,
        descriptions: Dict[str, str],
        selected_characters: List[str],
        selected_npcs: List[str],
        combined: str = "",
    ) -> None:
        """Inject scene description + entity context for a started scene."""
        if location:
            text_for_context = combined
            if not text_for_context and descriptions:
                text_for_context = "\n\n".join(
                    f"[{name}]\n{text}"
                    for name, text in descriptions.items()
                    if text
                )
            if text_for_context:
                self.inject_entity_description(
                    f"[scene_description:{location}]",
                    f"Location: {location}\n\n{text_for_context}",
                )
        self.inject_scene_pick_context(location, selected_characters, selected_npcs)
