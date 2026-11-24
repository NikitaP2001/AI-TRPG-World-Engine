"""GuiBridge — thin API layer between Dear ImGui panels and the engine.

All GUI panels call this bridge instead of accessing engine internals directly.
This keeps the GUI decoupled from the engine's internal API.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Engine imports ──
from world_manager.entity_store import (
    set_entity, get_entity, get_entity_facts, get_entity_state,
    query_entities, delete_entity, remove_effect,
)
from world_manager.event_log import query_events, create_event
from scheduler_queue import schedule_invocation, peek_due_invocations


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
    def delete_entity(id: str) -> Dict[str, Any]:
        return delete_entity(id=id, reason="Deleted via GUI")

    @staticmethod
    def remove_effect(entity_id: str, effect_id: str) -> Dict[str, Any]:
        return remove_effect(entity_id=entity_id, effect_id=effect_id)

    # ── Events ──

    @staticmethod
    def get_events(entity_id: str = "", tag: str = "",
                   limit: int = 50) -> List[Dict[str, Any]]:
        return query_events(entity_id=entity_id, tag=tag, limit=limit)

    @staticmethod
    def create_event(description: str, entity_ids: Optional[List[str]] = None,
                     tag: str = "") -> Dict[str, Any]:
        return create_event(description=description, entity_ids=entity_ids or [],
                            tag=tag)

    # ── Agent status ──

    @staticmethod
    def get_scheduler_queue() -> List[Dict[str, Any]]:
        return peek_due_invocations(current_tick=0) or []

    @staticmethod
    def invoke_agent(agent: str, comment: str = "") -> Dict[str, Any]:
        return schedule_invocation(agent=agent, delay_ticks=0, world_time=-1, comment=comment)

    # ── Prompt introspection ──

    @staticmethod
    def get_agent_history(agent: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return raw message history for an agent."""
        from memory_store import load_history
        from pathlib import Path
        game = Path(__file__).resolve().parent.parent / "game"
        path_map = {
            "gm": game / "game_master_messages.json",
            "sm": game / "scene_manager_messages.json",
            "wm": game / "world_master_messages.json",
        }
        path = path_map.get(agent)
        if path and path.exists():
            return load_history(path)[-limit:]
        return []

    # ── The engine itself ──

    @staticmethod
    def orchestrator():
        """The engine's own orchestrator, built once and kept for the panels.

        A panel that asks the engine something asks it of the same object every
        time, so the agents it holds are not rebuilt behind a question's back.
        """
        if not hasattr(GuiBridge, "_orchestrator"):
            from engine.orchestrator import Orchestrator
            GuiBridge._orchestrator = Orchestrator()
        return GuiBridge._orchestrator

    @staticmethod
    def forget_world() -> None:
        """Forget what the panels were reading: the game changed under them.

        A session that was played, or a return to a save point, leaves a world
        and an engine that stand for a state of the game no longer on disk.
        """
        for attr in ("_world_instance", "_orchestrator"):
            if hasattr(GuiBridge, attr):
                delattr(GuiBridge, attr)
