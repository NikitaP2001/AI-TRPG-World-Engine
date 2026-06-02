from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from memory_store import load_history, trim_history
from world.delta import build_gm_world_meta_content


class HistoryInjector:
    """Single authority for conditional world-state delta injection.

    Presence checks are based on the current trimmed active history window.
    This allows automatic re-injection recovery when older injected entries
    were trimmed out by token budget limits.
    """

    def __init__(
        self,
        *,
        history_loader: Callable[[], List[Any]],
        delta_injector: Callable[[str], None],
    ) -> None:
        self._history_loader = history_loader
        self._delta_injector = delta_injector

    def _load_active_history(self) -> List[Any]:
        try:
            rows = self._history_loader()
            if isinstance(rows, list):
                return rows
            return []
        except Exception:
            return []

    @staticmethod
    def _row_content(row: Any) -> str:
        if row is None:
            return ""
        if isinstance(row, dict):
            return str(row.get("content") or "")
        try:
            return str(getattr(row, "content", "") or "")
        except Exception:
            return str(row)

    def history_contains(self, marker: str) -> bool:
        needle = str(marker or "").strip()
        if not needle:
            return False
        for row in self._load_active_history():
            if needle in self._row_content(row):
                return True
        return False

    def inject_if_absent(self, *, marker: str, content: str) -> bool:
        mk = str(marker or "").strip()
        body = str(content or "").strip()
        if not mk or not body:
            return False
        if self.history_contains(mk):
            return False
        try:
            self._delta_injector(f"{mk}\n{body}")
            return True
        except Exception:
            return False

    def ensure_world_meta(self, *, world: Any) -> None:
        marker = "[world_snapshot:world_meta]"
        if self.history_contains(marker):
            return
        meta = build_gm_world_meta_content(world)
        self.inject_if_absent(marker=marker, content=meta)

    def ensure_character_description(self, *, world: Any, name: str) -> None:
        nm = str(name or "").strip()
        if not nm:
            return
        marker = f"[player_description:{nm}]"
        if self.history_contains(marker):
            return
        try:
            desc = world.get_character_description(nm)
            if isinstance(desc, dict):
                desc = {k: v for k, v in desc.items() if str(k) != "last_acted"}
            self.inject_if_absent(marker=marker, content=json.dumps(desc, ensure_ascii=False, indent=2))
        except Exception:
            return

    def ensure_location_description(self, *, world: Any, location: str) -> None:
        loc = str(location or "").strip()
        if not loc:
            return
        marker = f"[location_description:{loc}]"
        if self.history_contains(marker):
            return
        try:
            data = world.get_location(loc)
            self.inject_if_absent(marker=marker, content=json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            return

    def ensure_npc_description(self, *, world: Any, name: str) -> None:
        nm = str(name or "").strip()
        if not nm:
            return
        marker = f"[npc_description:{nm}]"
        if self.history_contains(marker):
            return
        try:
            npc = world.get_npc(nm)
            self.inject_if_absent(marker=marker, content=json.dumps(npc, ensure_ascii=False, indent=2))
        except Exception:
            return

    def ensure_story_summaries(self, *, world: Any) -> None:
        """Recover paragraph/arc summary deltas if they were trimmed out of GM history."""
        try:
            arcs = world.get_story()
        except Exception:
            arcs = []
        if not isinstance(arcs, list) or not arcs:
            return

        # Inject from oldest to newest for readability.
        for arc in reversed(arcs):
            if not isinstance(arc, dict):
                continue

            paragraphs = arc.get("paragraphs") if isinstance(arc.get("paragraphs"), list) else []
            for para in paragraphs:
                if not isinstance(para, dict):
                    continue
                name = str(para.get("name") or "").strip()
                summary = str(para.get("summary") or "").strip()
                if not name or not summary or name == "Summary":
                    continue

                locations = para.get("locations") if isinstance(para.get("locations"), list) else []
                characters = para.get("characters") if isinstance(para.get("characters"), list) else []
                npcs = para.get("npcs") if isinstance(para.get("npcs"), list) else []

                loc_str = ", ".join(str(x) for x in locations) if locations else "unknown"
                char_str = ", ".join(str(x) for x in characters) if characters else "unknown"
                npc_str = ", ".join(str(x) for x in npcs) if npcs else "none"

                parts = [
                    f'Story paragraph completed: "{name}"',
                    f"Locations: {loc_str}",
                    f"Players: {char_str}",
                ]
                if npc_str != "none":
                    parts.append(f"NPCs: {npc_str}")
                parts.append("")
                parts.append(summary)

                self.inject_if_absent(
                    marker=f"[paragraph_summary:{name}]",
                    content="\n".join(parts),
                )

            arc_name = str(arc.get("name") or "").strip()
            arc_summary = str(arc.get("summary") or "").strip()
            if not arc_name or not arc_summary:
                continue

            arc_locs = arc.get("locations") if isinstance(arc.get("locations"), list) else []
            arc_chars = arc.get("characters") if isinstance(arc.get("characters"), list) else []
            arc_npcs = arc.get("npcs") if isinstance(arc.get("npcs"), list) else []

            parts = [
                f'Arc finalized: "{arc_name}"',
            ]
            if arc_locs:
                parts.append(f"Arc locations: {', '.join(str(x) for x in arc_locs)}")
            if arc_chars:
                parts.append(f"Arc players: {', '.join(str(x) for x in arc_chars)}")
            if arc_npcs:
                parts.append(f"Arc NPCs: {', '.join(str(x) for x in arc_npcs)}")
            parts.append("")
            parts.append(arc_summary)

            self.inject_if_absent(
                marker=f"[arc_finalized:{arc_name}]",
                content="\n".join(parts),
            )

    def inject_paragraph_summary(
        self,
        *,
        name: str,
        summary: str,
        locations: List[str],
        characters: List[str],
        npcs: List[str],
    ) -> None:
        nm = str(name or "").strip()
        sm = str(summary or "").strip()
        if not nm or not sm or nm == "Summary":
            return

        loc_str = ", ".join(str(x) for x in (locations or []) if str(x).strip()) or "unknown"
        char_str = ", ".join(str(x) for x in (characters or []) if str(x).strip()) or "unknown"
        npc_str = ", ".join(str(x) for x in (npcs or []) if str(x).strip()) or "none"

        parts = [
            f'Story paragraph completed: "{nm}"',
            f"Locations: {loc_str}",
            f"Players: {char_str}",
        ]
        if npc_str != "none":
            parts.append(f"NPCs: {npc_str}")
        parts.append("")
        parts.append(sm)

        self.inject_if_absent(
            marker=f"[paragraph_summary:{nm}]",
            content="\n".join(parts),
        )

    def inject_arc_summary(
        self,
        *,
        arc_name: str,
        arc_summary: str,
        paragraph_names: List[str],
        locations: List[str],
        characters: List[str],
        npcs: List[str],
    ) -> None:
        an = str(arc_name or "").strip()
        sm = str(arc_summary or "").strip()
        if not an or not sm:
            return

        parts: List[str] = [f'Arc finalized: "{an}"']
        names = [str(x).strip() for x in (paragraph_names or []) if str(x).strip()]
        if names:
            parts.append(f"Paragraphs: {', '.join(repr(x) for x in names)}")

        locs = [str(x).strip() for x in (locations or []) if str(x).strip()]
        chars = [str(x).strip() for x in (characters or []) if str(x).strip()]
        npc_list = [str(x).strip() for x in (npcs or []) if str(x).strip()]
        if locs:
            parts.append(f"Arc locations: {', '.join(locs)}")
        if chars:
            parts.append(f"Arc players: {', '.join(chars)}")
        if npc_list:
            parts.append(f"Arc NPCs: {', '.join(npc_list)}")

        parts.append("")
        parts.append(sm)

        self.inject_if_absent(
            marker=f"[arc_finalized:{an}]",
            content="\n".join(parts),
        )


class GMHistoryInjector(HistoryInjector):
    """GM-specific wrapper around the generic HistoryInjector."""

    def __init__(self, *, game_master: Any, gm_history_path: Path, history_limits: Any) -> None:
        def _loader() -> List[Dict[str, Any]]:
            rows = load_history(gm_history_path)
            return trim_history(rows, limits=history_limits)

        def _inject(content: str) -> None:
            game_master.inject_delta(content)

        super().__init__(history_loader=_loader, delta_injector=_inject)
