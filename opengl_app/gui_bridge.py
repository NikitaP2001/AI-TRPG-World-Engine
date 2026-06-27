"""GuiBridge — thin API layer between Dear ImGui panels and the engine.

All GUI panels call this bridge instead of accessing engine internals directly.
This keeps the GUI decoupled from the engine's internal API.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Engine imports ──
from world_manager.entity_core import _get_backend, tick as engine_tick
from world_manager.entity_store import (
    set_entity, get_entity, get_entity_facts, get_entity_state,
    query_entities, adjust_entity, delete_entity,
    add_tags, remove_tags, set_effect, remove_effect,
    attach_script, detach_script,
)
from world_manager.event_log import query_events, record_event
from news_queue import consume as consume_news, peek as peek_news, append as append_news
from scheduler_queue import pop_due_invocations, schedule_invocation, peek_due_invocations


class GuiBridge:
    """Stateless bridge — each method reads fresh state from the engine."""

    # ── World ──

    @staticmethod
    def _world():
        """Lazy-init World singleton."""
        from world import World as _World
        if not hasattr(GuiBridge, "_world_instance"):
            try:
                GuiBridge._world_instance = _World()
                GuiBridge._world_instance.ensure_initialized()
            except Exception:
                GuiBridge._world_instance = None
        return GuiBridge._world_instance

    @staticmethod
    def get_world_time() -> str:
        w = GuiBridge._world()
        if w is None:
            return ""
        try:
            return str(w.get_world_time())
        except Exception:
            return ""

    @staticmethod
    def get_story_tree() -> List[Dict[str, Any]]:
        """Return story arcs with paragraphs and turn counts."""
        w = GuiBridge._world()
        if w is None:
            return []
        try:
            arcs = w.get_story()
            if not isinstance(arcs, list):
                return []
            tree = []
            for i, arc in enumerate(arcs):
                if not isinstance(arc, dict):
                    continue
                paras = arc.get("paragraphs", []) or []
                ongoing = arc.get("ongoing_paragraph", {}) or {}
                entry = {
                    "name": arc.get("name", f"Arc {i}"),
                    "index": i,
                    "paragraphs": [],
                    "has_ongoing": bool(ongoing.get("turns")),
                    "ongoing_turns": len(ongoing.get("turns", [])),
                }
                for pi, p in enumerate(paras):
                    if isinstance(p, dict):
                        entry["paragraphs"].append({
                            "name": p.get("name", f"Paragraph {pi}"),
                            "turn_count": p.get("turn_count", 0),
                            "summary": (p.get("summary", "") or "")[:200],
                        })
                tree.append(entry)
            return tree
        except Exception:
            return []

    # ── Scene ──

    @staticmethod
    def get_scene(location_id: str) -> Dict[str, Any]:
        """Get current scene: location, participants, recent events."""
        from scene_manager.tools import get_scene as _get_scene
        return _get_scene(location_id)

    @staticmethod
    def get_active_scene() -> Optional[Dict[str, Any]]:
        """Get the currently active scene from World."""
        w = GuiBridge._world()
        if w is None:
            return None
        try:
            scene = w.get_scene()
            if isinstance(scene, dict) and scene.get("state") == "active":
                return scene
        except Exception:
            pass
        return None

    # ── Entity ──

    @staticmethod
    def get_entity(id: str) -> Optional[Dict[str, Any]]:
        return get_entity(id)

    @staticmethod
    def get_entity_facts(id: str) -> str:
        result = get_entity_facts(id)
        return result if result else ""

    @staticmethod
    def get_entity_state(id: str) -> Dict[str, Any]:
        result = get_entity_state(id)
        return result if result else {}

    @staticmethod
    def query_entities(type: str = "", tags: Optional[List[str]] = None,
                       search: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        return query_entities(type_filter=type, tags=tags or [],
                              facts_search=search, include_templates=False, limit=limit)

    @staticmethod
    def save_entity(id: str, type: str, facts: str = "",
                    gurps: Optional[Dict] = None, state: Optional[Dict] = None,
                    skills: Optional[Dict] = None, equipment: Optional[Dict] = None,
                    stats: Optional[Dict] = None, tags: Optional[List[str]] = None,
                    position: Optional[Dict] = None) -> Dict[str, Any]:
        return set_entity(id=id, type=type, facts=facts, gurps=gurps,
                          state=state, skills=skills, equipment=equipment,
                          stats=stats, tags=tags, position=position)

    @staticmethod
    def adjust(entity_id: str, **kwargs) -> Dict[str, Any]:
        return adjust_entity(entity_id=entity_id, **kwargs)

    @staticmethod
    def delete_entity(id: str) -> Dict[str, Any]:
        return delete_entity(id=id, reason="Deleted via GUI")

    @staticmethod
    def remove_effect(entity_id: str, effect_id: str) -> Dict[str, Any]:
        return remove_effect(entity_id=entity_id, effect_id=effect_id)

    # ── Entity sets ──

    @staticmethod
    def get_entity_sets() -> Dict[str, List[Dict[str, Any]]]:
        """Return entities grouped by type."""
        result = {}
        for etype in ("npc", "character", "location", "faction", "item"):
            entities = query_entities(type_filter=etype, include_templates=False, limit=200)
            if entities:
                result[etype] = entities
        return result

    # ── Events ──

    @staticmethod
    def get_events(entity_id: str = "", tag: str = "",
                   limit: int = 50) -> List[Dict[str, Any]]:
        return query_events(entity_id=entity_id, tag=tag, limit=limit)

    @staticmethod
    def record_event(description: str, entity_ids: Optional[List[str]] = None,
                     tag: str = "", narrative_flag: bool = False) -> Dict[str, Any]:
        return record_event(description=description, entity_ids=entity_ids or [],
                            tag=tag, narrative_flag=narrative_flag)

    # ── Agent status ──

    @staticmethod
    def get_scheduler_queue() -> List[Dict[str, Any]]:
        return peek_due_invocations(current_tick=0) or []

    @staticmethod
    def get_news_queue(agent: str) -> List[str]:
        return peek_news(agent, limit=20)

    @staticmethod
    def invoke_agent(agent: str, comment: str = "") -> Dict[str, Any]:
        return schedule_invocation(agent=agent, delay_ticks=0, world_time=-1, comment=comment)

    # ── Engine tick ──

    @staticmethod
    def tick(current_tick: int) -> Dict[str, Any]:
        return engine_tick(current_tick)

    # ── Prompt introspection ──

    @staticmethod
    def get_agent_history(agent: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return raw message history for an agent."""
        from memory_store import load_history
        from pathlib import Path
        game = Path("game")
        path_map = {
            "gm": game / "gm_history" / "messages.json",
            "sm": game / "sm_history" / "messages.json",
            "wm": game / "wm_history" / "_wm_history.json",
        }
        path = path_map.get(agent)
        if path and path.exists():
            return load_history(path)[-limit:]
        return []
