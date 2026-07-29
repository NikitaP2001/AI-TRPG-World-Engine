"""Centralized pinned context builder for all agents.

Two layers:
  Layer 1 — Persistent pins (PinnedBlockCache, mtime-based):
    [world_setting], [gm_summaries], [sm_summaries], [wm_summaries],
    [character:{name}]

  Layer 2 — Invocation pins (rebuilt every call, no cache):
    [storage_notice]

All per-agent summaries are read from {agent}_summaries.json via
build_agent_summaries_block(). NEVER from messages.json — summaries
survive trims independently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage

from world.story import PinnedBlockCache
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

    def __init__(self, history_path: Path, world: Any) -> None:
        self._history_path = history_path
        self._cache = PinnedBlockCache()
        self._world = world
        self._root = history_path.parent
        self._persistent: List[SystemMessage] = []
        self._invocation: List[SystemMessage] = []
        self.last_digest_event_id: str = ""

    # ------------------------------------------------------------------
    # Layer 1 — Persistent (cached, mtime-based invalidation)
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

    def add_gm_summaries(self) -> "PinnedContext":
        """[gm_summaries] — from gm_summaries.json, cached by mtime."""
        summaries_path = self._root / "gm_summaries.json"
        block = self._cache.get("gm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[gm_summaries]\n{block}")
            )
        return self

    def add_sm_summaries(self) -> "PinnedContext":
        """[sm_summaries] — from sm_summaries.json, cached by mtime."""
        summaries_path = self._root / "sm_summaries.json"
        block = self._cache.get("sm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[sm_summaries]\n{block}")
            )
        return self

    def add_wm_summaries(self) -> "PinnedContext":
        """[wm_summaries] — from wm_summaries.json, cached by mtime."""
        summaries_path = self._root / "wm_summaries.json"
        block = self._cache.get("wm", summaries_path, build_agent_summaries_block)
        if block:
            self._persistent.append(
                SystemMessage(content=f"[wm_summaries]\n{block}")
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
                                SystemMessage(content=f"[character:{cname}]\n{block}")
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
                        SystemMessage(content=f"[character:{name}]\n{block}")
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
            for ch in self._world.get_players():
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

    def add_news_digest(self, last_seen_event_id: str = "") -> "PinnedContext":
        """[news_digest] — invocation pin, rebuilt every call.

        Queries the 4D event log for narrative_flag events since the
        given event_id and formats them as a digest block.
        """
        try:
            from world_manager.event_log import query_events
            from world.time import WorldTime

            events = query_events(
                narrative_flag_only=True,
                since_event_id=last_seen_event_id,
                limit=20,
            )
        except Exception:
            events = []

        if not events:
            return self

        try:
            wt = self._world.get_world_time()
            now_str = wt.to_string() if hasattr(wt, "to_string") else str(wt)
        except Exception:
            now_str = "?"

        lines = [f"[news_digest] Since your last invocation — current time ({now_str})"]
        for ev in events:
            ev_tick = ev.get("tick", 0)
            desc = str(ev.get("description", "")).strip()
            tag = str(ev.get("tag", "")).strip()
            if tag:
                lines.append(f"  [{tag}] {desc[:200]}")
            else:
                lines.append(f"  - {desc[:200]}")
            # Capture the last event_id for marker update
            self.last_digest_event_id = ev.get("event_id", "")

        self._invocation.append(
            SystemMessage(content="\n".join(lines))
        )
        return self
