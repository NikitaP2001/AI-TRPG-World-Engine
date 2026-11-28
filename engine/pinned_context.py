"""Centralized pinned context builder for all agents.

Two layers:
  Layer 1 — Persistent pins (PinnedBlockCache, mtime-based):
    ## World Setting, ## Campaign Summaries, ## Scene Summaries,
    ## World Summaries, ## Character — {name}

  Layer 2 — Invocation pins (rebuilt every call, no cache):
    ## Storage Notice

A block states itself with a heading of words rather than with a bracketed code
name: the words are what the agent reads, they tokenise as words, and a heading
holds attention where `[lower_case_name]` reads as framework noise.

All per-agent summaries are read from {agent}_summaries.json via
build_agent_summaries_block(). NEVER from messages.json — summaries
survive trims independently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage

from world.story import PinnedBlockCache, get_pinned_block_cache
from world.agent_story import build_agent_summaries_block
from world.context import is_character_active_in_scene, build_character_info_block, build_character_info_block_from_entity


class PinnedContext:
    """Two-layer pinned context builder.

    Usage::

        ctx = PinnedContext(history_path, world)
        persistent = (
            ctx.add_world_setting()
               .add_gm_summaries()
               .add_all_player_descriptions()
               .add_active_characters()
               .build_persistent()
        )
        invocation = ctx.add_storage_notice().build_invocation()
    """

    def __init__(
        self,
        history_path: Path,
        world: Any,
        cache: Optional[PinnedBlockCache] = None,
    ) -> None:
        self._history_path = history_path
        # Every builder is created per call, so the cache must be the shared one:
        # an own instance would be cold on every invocation.
        self._cache = cache if cache is not None else get_pinned_block_cache()
        self._world = world
        self._root = history_path.parent
        self._persistent: List[SystemMessage] = []
        self._invocation: List[SystemMessage] = []

    # ------------------------------------------------------------------
    # Layer 1 — Persistent (cached, mtime-based invalidation)
    # ------------------------------------------------------------------

    def add_world_setting(self) -> "PinnedContext":
        """## World Setting — never changes."""
        ws_path = self._root / "world_setting.json"
        if ws_path.exists():
            try:
                ws_text = ws_path.read_text(encoding="utf-8").strip()
                if ws_text:
                    self._persistent.append(
                        SystemMessage(content=f"## World Setting\n{ws_text}")
                    )
            except Exception:
                pass
        return self

    def add_gm_summaries(self) -> "PinnedContext":
        """## Campaign Summaries — from gm_summaries.json, cached by mtime."""
        summaries_path = self._root / "gm_summaries.json"
        block = self._cache.get("gm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"## Campaign Summaries\n{block}")
            )
        return self

    def add_sm_summaries(self) -> "PinnedContext":
        """## Scene Summaries — from sm_summaries.json, cached by mtime."""
        summaries_path = self._root / "sm_summaries.json"
        block = self._cache.get("sm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"## Scene Summaries\n{block}")
            )
        return self

    def add_wm_summaries(self) -> "PinnedContext":
        """## World Summaries — from wm_summaries.json, cached by mtime."""
        summaries_path = self._root / "wm_summaries.json"
        block = self._cache.get("wm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"## World Summaries\n{block}")
            )
        return self

    def add_active_characters(self) -> "PinnedContext":
        """[character:{name}] — all scene participants, cached per character.

        Reads from entity_store (CompositeBackend) first, falls back to
        character filesystem files for backward compatibility.
        """
        try:
            scene = self._world.get_scene()
            if isinstance(scene, dict) and scene.get("state") == "active":
                chars = scene.get("players")
                if isinstance(chars, dict):
                    for cname in chars:
                        if not is_character_active_in_scene(self._world, cname):
                            continue
                        # Try entity_store first
                        block = build_character_info_block_from_entity(cname)
                        if block is None:
                            # Fallback: filesystem
                            char_dir = self._world._character_dir(cname)
                            block = build_character_info_block(char_dir)
                        if block:
                            self._persistent.append(
                                SystemMessage(content=f"## Character — {cname}\n{block}")
                            )
        except Exception:
            pass
        return self

    def add_all_player_descriptions(self) -> "PinnedContext":
        """[character:{name}] — all known players, regardless of scene.

        Used during GM initialization when no scene exists yet.
        Reads from entity_store first, falls back to player files.
        """
        try:
            for name in self._world.list_character_names():
                block = build_character_info_block_from_entity(name)
                if block is None:
                    char_dir = self._world._character_dir(name)
                    block = build_character_info_block(char_dir)
                if block:
                    self._persistent.append(
                        SystemMessage(content=f"## Character — {name}\n{block}")
                    )
        except Exception:
            pass
        return self

    # ------------------------------------------------------------------
    # Layer 2 — Invocation-scoped (rebuilt fresh every call)
    # ------------------------------------------------------------------

    def add_storage_notice(self) -> "PinnedContext":
        """## Storage Notice — invocation pin, rebuilt every call.

        Names the characters whose mechanical blocks the entity store does not
        hold yet. The store is where those blocks live, so a block named here is
        absent — not one this pin failed to reach.
        """
        lines: List[str] = []
        try:
            from world_manager.entity_store import get_entity_state
            for ch in self._world.get_players():
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                if not name:
                    continue
                state = get_entity_state(name)
                if state is None:
                    lines.append(f"Character '{name}' has no entity in storage.")
                    continue
                missing = [block for block in ("gurps", "state", "skills", "equipment")
                           if not state.get(block)]
                if not missing:
                    continue
                lines.append(f"Character '{name}' missing: {', '.join(missing)}.")
        except Exception:
            pass
        if lines:
            self._invocation.append(
                SystemMessage(
                    content="## Storage Notice\n" + "\n".join(lines)
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

    def rebuild_storage_notice(self) -> List[SystemMessage]:
        """Rebuild the storage notice from current world state.
        Returns a list with one SystemMessage, or empty list if nothing missing.
        Use as pinned_refresh_fn in react_loop_iteration."""
        fresh = PinnedContext(self._history_path, self._world)
        fresh.add_storage_notice()
        return fresh.build_invocation()
