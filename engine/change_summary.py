"""Aggregate of what one invocation changed, for the other agent to read.

An agent's permanent memory holds what happened, not how it was called. The
other agent needs to know, in outline, everything that was added or that
changed state — with a handle it can follow up on, and without the calls that
did it: one line says that a place moved, not that adjust_entity was called
with these arguments.

An outline names what changed and who it changed for, and nothing of what it
became: the area of an entity — `current_state`, `position`, `tags` — is named
once, with the entities it changed for; an event is named by the id it was
recorded under; a creation is grouped by the kind of thing that appeared. A
reader who wants the value goes and reads it, which is the point of a handle.

What the block therefore says about the session is where to look, not what is
there — and it is bounded by its own structure rather than by cutting the tail:
counts are exact, the kinds of creation name at most twelve and fold the rest
into one entry, and the members of a group are listed up to a fixed number with
the remainder counted.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Tuple

# How much of the outline is spelled out. Beyond these, only counts grow.
_MAX_LISTED_MEMBERS = 20
_MAX_LISTED_TYPES = 12

# Blocks an entity can carry, in the order a reader would look for them. The
# identity (`name`) is the entity's own line and is not repeated as an area.
_ENTITY_FIELDS = ("type", "base_type", "facts", "gurps", "stats", "tags",
                  "position", "state", "skills", "equipment")

# Tool -> the field(s) of an entity its success changes, when the args do not
# already say. Reads, queries and termination tools are absent on purpose.
_FIELD_TOOLS = {
    "set_entity_facts": ("facts",),
    "add_tags": ("tags",),
    "remove_tags": ("tags",),
    "set_effect": ("effects",),
    "remove_effect": ("effects",),
}


def tool_failed(result: Any) -> bool:
    """Whether a tool reported failure, in which case nothing changed.

    Shared with the ReAct loop so that a record of a write and a summary of
    changes agree on what actually happened.
    """
    if isinstance(result, dict):
        if str(result.get("status") or "") == "error":
            return True
        text = str(result.get("error") or "")
        return bool(text) and not result.get("entity_id")
    text = str(result or "").strip()
    return text.startswith("Error:") or text.startswith("ERROR:")


def _named(text: Any, limit: int = 60) -> str:
    value = str(text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _listed(members: List[str]) -> str:
    """The members of one group, up to the bound; beyond it, counted."""
    shown = members[:_MAX_LISTED_MEMBERS]
    text = ", ".join(shown)
    if len(members) > len(shown):
        text += f", +{len(members) - len(shown)} more"
    return text


def _touched_fields(args: Dict[str, Any]) -> List[str]:
    """Which blocks a call carried, in the order an entity carries them."""
    out = []
    for field in _ENTITY_FIELDS:
        value = args.get(field)
        if value not in (None, "", {}, []):
            out.append(field)
    return out


def _adjust_fields(args: Dict[str, Any]) -> List[str]:
    """Which runtime mutations an adjust_entity call carried."""
    out = []
    for key, value in args.items():
        if key == "entity_id":
            continue
        if value in (None, "", {}, []):
            continue
        out.append(key)
    return out


class ChangeCollector:
    """Collects what the tools of one invocation actually changed."""

    def __init__(self) -> None:
        self._order: List[str] = []
        self._entities: Dict[str, Dict[str, Any]] = {}
        self._created_kinds: Dict[str, List[str]] = {}
        self._areas: Dict[str, List[str]] = {}
        self._removed: List[str] = []
        self._event_ids: List[str] = []
        self._created = 0
        self._changed = 0
        self._removed_count = 0
        self._intents = 0
        self._corrections = 0
        self._notes = 0

    # ── Collection ──────────────────────────────────────────────────

    def record(self, tool_name: str, args: Dict[str, Any], result: Any) -> None:
        """Note what one executed tool call changed, if it changed anything."""
        name = str(tool_name or "").strip()
        if not isinstance(args, dict):
            args = {}
        if tool_failed(result):
            return

        if name == "set_entity":
            entity_id = str((result or {}).get("entity_id") or args.get("name") or "").strip()
            if not entity_id:
                return
            operation = str((result or {}).get("operation") or "")
            fields = _touched_fields(args)
            if operation == "created":
                self._created += 1
                kind = _named(args.get("type") or "?")
                self._note(entity_id, "created", kind, ())
            else:
                self._changed += 1
                self._note(entity_id, "changed", "", fields)
            return

        if name == "adjust_entity":
            entity_id = str(args.get("entity_id") or "").strip()
            if entity_id:
                self._changed += 1
                self._note(entity_id, "changed", "", _adjust_fields(args))
            return

        if name == "delete_entity":
            # The tool names its argument `name`; `id` is read as well, so a call
            # that passed either form is a removal the outline reports.
            entity_id = str(args.get("name") or args.get("id") or "").strip()
            if entity_id:
                self._removed_count += 1
                self._note(entity_id, "removed", "", ())
            return

        if name in _FIELD_TOOLS:
            entity_id = str(args.get("entity_id") or args.get("name") or "").strip()
            if entity_id:
                self._changed += 1
                self._note(entity_id, "changed", "", _FIELD_TOOLS[name])
            return

        if name == "create_event":
            # An event is named by the id it was recorded under: the outline has
            # no area of an entity to hand a reader for it.
            event_id = str((result or {}).get("event_id") or "").strip()
            if event_id:
                self._event_ids.append(event_id)
            return
        if name in ("declare_intent", "modify_intent", "manage_intent"):
            self._intents += 1
            return
        if name == "write_correction":
            self._corrections += 1
            return
        if name == "notify_wm":
            self._notes += 1
            return

    def _note(self, entity_id: str, verb: str, kind: str,
              fields: Tuple[str, ...]) -> None:
        """Enter one entity under the areas of it this call touched.

        The entity's line is not its own: it stands among the entities an area
        changed for, so an area is named once however many entities it moved.
        """
        entry = self._entities.get(entity_id)
        if entry is None:
            entry = {"verb": verb, "kind": kind, "fields": OrderedDict()}
            self._entities[entity_id] = entry
            self._order.append(entity_id)
        for field in fields:
            entry["fields"].setdefault(field, True)
            if entity_id not in self._areas.setdefault(field, []):
                self._areas[field].append(entity_id)
        if verb == "created":
            members = self._created_kinds.setdefault(kind or "?", [])
            if entity_id not in members:
                members.append(entity_id)
        elif verb == "removed":
            if entity_id not in self._removed:
                self._removed.append(entity_id)

    # ── Reporting ───────────────────────────────────────────────────

    def empty(self) -> bool:
        return not (self._order or self._event_ids or self._intents
                    or self._corrections or self._notes)

    def build(self, actor: str) -> str:
        """The one record the other agent reads, or '' when nothing changed."""
        if self.empty():
            return ""
        head = []
        if self._created or self._changed or self._removed_count:
            parts = []
            if self._created:
                parts.append(f"{self._created} created")
            if self._changed:
                parts.append(f"{self._changed} changed")
            if self._removed_count:
                parts.append(f"{self._removed_count} removed")
            head.append("entities: " + ", ".join(parts))
        if self._event_ids:
            head.append(f"events: {len(self._event_ids)} recorded")
        if self._intents:
            head.append(f"intents: {self._intents}")
        if self._corrections:
            head.append(f"corrections: {self._corrections}")
        if self._notes:
            head.append(f"notes: {self._notes}")

        lines = [f"[{actor}_changes]", "; ".join(head)]
        lines.extend(self._group_lines())
        return "\n".join(lines)

    def _group_lines(self) -> List[str]:
        """One line per kind that appeared, and per area that changed.

        The kind, or the area, is named once and the entities it stands for are
        listed beside it: the same fact is said once however many entities it
        moved. What an area became is not said — a reader who wants the value
        goes and reads the entity, and a block carrying every value would be a
        second copy of the world.
        """
        out: List[str] = []
        kinds = list(self._created_kinds.items())
        for kind, members in kinds[:_MAX_LISTED_TYPES]:
            out.append("  created " + kind + ": " + _listed(members))
        rest = kinds[_MAX_LISTED_TYPES:]
        if rest:
            out.append(f"  (+{len(rest)} kind(s) of creation, "
                       f"{sum(len(m) for _, m in rest)} entities, not named)")
        for area, members in self._areas.items():
            out.append("  " + area + ": " + _listed(members))
        if self._removed:
            out.append("  removed: " + _listed(self._removed))
        if self._event_ids:
            out.append("  events: " + _listed(self._event_ids))
        return out


def append_change_record(history_path: Any, block: str) -> None:
    """Enter the outline into another agent's memory, as a record.

    A change outline has exactly one reader: the authority it is reported to. If
    there is no such memory, the outline is not written anywhere, and that loss
    is said out loud — the authority silently never learning what changed is the
    kind of failure nothing else in the system would show.
    """
    if not block:
        return
    if history_path is None:
        print("  change outline not delivered: it has no memory to report into")
        return
    from history_pipeline import memory_at

    memory_at(history_path).record(block)
