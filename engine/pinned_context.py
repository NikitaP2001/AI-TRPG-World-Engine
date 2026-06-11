"""Centralized pinned context builder for all agents.

Two layers:
  Layer 1 — Persistent pins (PinnedBlockCache, survives trims):
    [world_setting], [arc_summaries], [paragraph_summaries],
    [gm_summaries], [character:{name}]

  Layer 2 — Invocation pins (rebuilt every call, no cache):
    [storage_notice]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage

from world.story import PinnedBlockCache, build_arc_summaries_block, build_paragraph_summaries_block, build_gm_summaries_block
from world.context import is_character_active_in_scene, build_character_info_block


class PinnedContext:
    """Two-layer pinned context builder.

    Usage::

        ctx = PinnedContext(history_path, world)
        persistent = (
            ctx.add_world_setting()
               .add_arc_summaries()
               .add_paragraph_summaries()
               .add_active_characters()
               .build_persistent()
        )
        invocation = ctx.add_storage_notice().build_invocation()
    """

    def __init__(self, history_path: Path, world: Any) -> None:
        self._history_path = history_path
        self._cache = PinnedBlockCache(history_path)
        self._world = world
        self._root = history_path.parent
        self._persistent: List[SystemMessage] = []
        self._invocation: List[SystemMessage] = []

    # ------------------------------------------------------------------
    # Layer 1 — Persistent (cached, only rebuilds on trim)
    # ------------------------------------------------------------------

    def add_world_setting(self) -> "PinnedContext":
        """[world_setting] — never changes."""
        ws_path = self._root / "world_setting.json"
        if ws_path.exists():
            try:
                ws_text = ws_path.read_text(encoding="utf-8").strip()
                if ws_text:
                    self._persistent.append(
                        SystemMessage(content=f"[world_setting]\n{ws_text}")
                    )
            except Exception:
                pass
        return self

    def add_arc_summaries(self) -> "PinnedContext":
        """[arc_summaries] — cached, rebuilds on trim."""
        story_path = self._root / "world" / "story.json"
        block = self._cache.get("arc", story_path, build_arc_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[arc_summaries]\n{block}")
            )
        return self

    def add_paragraph_summaries(self) -> "PinnedContext":
        """[paragraph_summaries] — cached, rebuilds on trim."""
        story_path = self._root / "world" / "story.json"
        block = self._cache.get("paragraph", story_path, build_paragraph_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[paragraph_summaries]\n{block}")
            )
        return self

    def add_gm_summaries(self, history_msgs: Optional[List] = None) -> "PinnedContext":
        """[gm_summaries] — GM-only, cached.

        If *history_msgs* is provided, gm_summary entries are filtered out to
        avoid duplication.
        Returns filtered history_msgs (or original if None).
        """
        block = self._cache.get("gm", self._history_path, build_gm_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[gm_summaries]\n{block}")
            )
            # Filter duplicates from history
            if history_msgs is not None:
                filtered: List = []
                for m in history_msgs:
                    content = str(getattr(m, "content", "") or "")
                    if content.startswith("[gm_summary:"):
                        continue
                    filtered.append(m)
                return filtered
        return history_msgs

    def add_active_characters(self) -> "PinnedContext":
        """[character:{name}] — all scene participants, cached per character."""
        try:
            scene = self._world.get_scene()
            if isinstance(scene, dict) and scene.get("state") == "active":
                chars = scene.get("characters")
                if isinstance(chars, dict):
                    for cname in chars:
                        if not is_character_active_in_scene(self._world, cname):
                            continue
                        char_dir = self._world._character_dir(cname)
                        block = self._cache.get(
                            f"char_{cname}", char_dir, build_character_info_block
                        )
                        if block:
                            self._persistent.append(
                                SystemMessage(content=f"[character:{cname}]\n{block}")
                            )
        except Exception:
            pass
        return self

    # ------------------------------------------------------------------
    # Layer 2 — Invocation-scoped (rebuilt fresh every call)
    # ------------------------------------------------------------------

    def add_storage_notice(self) -> "PinnedContext":
        """[storage_notice] — invocation pin, rebuilt every call.

        Lists characters missing state/skills/equipment.
        Includes fallback text from _stripped.json for missing equipment.
        """
        lines: List[str] = []
        try:
            info = self._world.get_info()
            for ch in (info.get("characters") if isinstance(info, dict) else []):
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                if not name:
                    continue
                missing: List[str] = []
                fallback_parts: List[str] = []
                if not self._world.get_character_state(name):
                    missing.append("state")
                if not self._world.get_character_skills(name):
                    missing.append("skills")
                if not self._world.get_character_equipment(name):
                    missing.append("equipment")
                    # Read fallback from _stripped.json
                    try:
                        stripped_path = self._world._character_dir(name) / "_stripped.json"
                        if stripped_path.exists():
                            stripped_data = json.loads(stripped_path.read_text(encoding="utf-8"))
                            equip = stripped_data.get("equipment")
                            if equip:
                                fallback_parts.append(
                                    "Equipment fallback:\n" + json.dumps(equip, ensure_ascii=False, indent=4)
                                )
                    except Exception:
                        pass
                if not missing:
                    continue
                line = f"Character '{name}' missing: {', '.join(missing)}."
                if fallback_parts:
                    line += "\n" + "\n".join(fallback_parts)
                lines.append(line)
        except Exception:
            pass
        if lines:
            self._invocation.append(
                SystemMessage(
                    content="[storage_notice]\n" + "\n\n".join(lines)
                )
            )
        return self

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def build_persistent(self) -> List[SystemMessage]:
        return list(self._persistent)

    def build_invocation(self) -> List[SystemMessage]:
        return list(self._invocation)

    def build_all(self) -> List[SystemMessage]:
        return self._persistent + self._invocation

    def rebuild_storage_notice(self) -> List[SystemMessage]:
        """Rebuild [storage_notice] from current world state.
        Returns a list with one SystemMessage, or empty list if nothing missing.
        Use as pinned_refresh_fn in react_loop_iteration."""
        fresh = PinnedContext(self._history_path, self._world)
        fresh.add_storage_notice()
        return fresh.build_invocation()
