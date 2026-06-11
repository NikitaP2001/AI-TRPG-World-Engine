from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from character.agent import run_character_agent
from world import WorldDuration
from openrouter_langchain_logging import logs_enabled

# Shared tool infrastructure — singletons, guards, state
from engine.tool_base import (
    _WORLD,
    _SCENE,
    _TURN_LOCKED,
    _CONTEXT_CHANGED,
    reset_turn_lock,
    is_context_changed,
    is_turn_locked,
    signal_context_changed,
    _signal_context_changed,
    _require_turn_unlocked,
    _json,
    _tool_error,
    _guard_tool,
    _override_state_path,
    _override_load,
    _override_save,
    _override_disarm,
    _active_scene_dict,
)


__all__ = [
    "reset_turn_lock",
    "is_context_changed",
    "is_turn_locked",
    "signal_context_changed",
    # Tool callables
    "create_location",
    "get_location",
    "update_location",
    "delete_location_path",
    "delete_location",
    "create_npc",
    "get_npc",
    "update_npc",
    "delete_npc_path",
    "delete_npc",
    "get_character_detail",
    "update_character_state",
    "update_character_skills",
    "update_character_equipment",
    "read_character_diary",
]


# Tracks which characters were already read via get_character_detail in the current invocation.
_read_characters: set = set()


def reset_read_tracker() -> None:
    global _read_characters
    _read_characters = set()





def _auto_character_input(scene: Dict[str, Any], character_name: str) -> str:
    """Build character_input from scene description and prior actions."""

    parts: List[str] = []

    scene_desc = str(scene.get("scene_description") or "").strip()
    if scene_desc:
        parts.append(scene_desc)

    chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}

    order = scene.get("initiative_order") if isinstance(scene.get("initiative_order"), list) else []
    if not order:
        order = list(chars.keys()) if isinstance(chars, dict) else []

    # Show actions of characters who already acted (earlier in initiative order).
    prior_lines: List[str] = []
    for name in order:
        if str(name) == str(character_name):
            break
        entry = chars.get(name) if isinstance(chars.get(name), dict) else {}
        if not entry.get("acted"):
            continue
        action = str(entry.get("last_decision") or "").strip()
        if action:
            prior_lines.append(f"{name}: {action}")

    if prior_lines:
        parts.append("Other announced actions this turn:\n" + "\n".join(["- " + x for x in prior_lines]))

    return "\n\n".join([p for p in parts if p.strip()]).strip()


def _guard_tool(
    tool_name: str,
    *,
    require_unlocked: bool = False,
    extra_hint: str = "",
) -> Optional[str]:
    """GM-specific guard — checks turn lock only (tools are bound by the caller)."""
    from engine.tool_base import _guard_tool as _base_guard
    return _base_guard(
        tool_name,
        allowed_tools_fn=None,
        require_unlocked=require_unlocked,
        extra_hint=extra_hint,
    )


@tool
def create_location(name: str, summary: str, details: str, parent_location: str = "") -> str:
    """Create a new location by name with summary + details.

    Args:
        name: Location name.
        summary: Short location summary.
        details: Longer location details.
        parent_location: Optional parent location name (empty by default).
    """

    err = _guard_tool("create_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    loc = _WORLD.create_location(
        name=name,
        summary=summary,
        details=details,
        parent_location=parent_location,
    )
    return _json(loc)


@tool
def get_location(name: str) -> str:
    """Fetch a location object by name."""

    err = _guard_tool("get_location")
    if err:
        return err
    _WORLD.ensure_initialized()
    return _json(_WORLD.get_location(name))


@tool
def update_location(name: str, json_pointer: str, value_json: str) -> str:
    """Upsert a JSON field in a location record using JSON Pointer (creates missing containers)."""

    err = _guard_tool("update_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if ptr in {"/name", ""}:
        return _tool_error("/name cannot be modified via update_location")
    if ptr == "/last_active":
        return _tool_error("/last_active is runtime-managed; it cannot be set via GM tools")
    if ptr in {"/parent_location", "/sublocations_names"}:
        return _tool_error(f"{ptr} is auto-synced from location hierarchy; update parent_location on the child location instead")

    data = _WORLD.add_location_json(name=name, pointer=json_pointer, value=value_json)
    return _json(data)


@tool
def delete_location_path(name: str, json_pointer: str) -> str:
    """Delete single field from a location record using JSON Pointer."""

    err = _guard_tool("delete_location_path", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if ptr == "/":
        return _tool_error("Cannot delete document root")
    if ptr == "/name":
        return _tool_error("/name cannot be deleted")
    if ptr == "/last_active":
        return _tool_error("/last_active is runtime-managed; it cannot be deleted via GM tools")
    if ptr in {"/parent_location", "/sublocations_names"}:
        return _tool_error(f"{ptr} is auto-synced from location hierarchy; delete parent_location on the child location instead")

    if logs_enabled():
        print(f"[trace] delete_location_path: {name} {ptr}")

    data = _WORLD.delete_location_json(name=name, pointer=ptr)
    return _json(data)


@tool
def delete_location(name: str) -> str:
    """Delete a location that no longer exists or is no longer relevant."""

    err = _guard_tool("delete_location", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    nm = str(name or "").strip()
    if not nm:
        return _tool_error("name is required")

    if logs_enabled():
        print(f"[trace] delete_location: {nm}")

    _WORLD.delete_location(nm)
    return f"Location '{nm}' deleted."


@tool
def create_npc(name: str, location: str, current_state: str, description: str) -> str:
    """Create an NPC and store it in npc.json (GM-controlled).
    NPC — any being capable of at least simple movement and decision making.
    Merge identical NPCs into a single entry with an amount field.
    Errors if the NPC already exists; use `update_npc` to modify an existing NPC.
    """

    err = _guard_tool("create_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    npc = _WORLD.create_npc(name=name, location=location, current_state=current_state, description=description)

    # If a scene is active, this NPC participates in it.
    try:
        scene = _WORLD.get_scene()
        if isinstance(scene, dict) and scene.get("state") == "active":
            _SCENE.add_npc_to_scene(name)
    except Exception:
        pass

    return _json(npc)


@tool
def get_npc(name: str) -> str:
    """Fetch an NPC object by name."""

    err = _guard_tool("get_npc")
    if err:
        return err
    _WORLD.ensure_initialized()
    return _json(_WORLD.get_npc(name))


@tool
def update_npc(name: str, json_pointer: str, value_json: str) -> str:
    """Upsert a JSON field in an NPC record using JSON Pointer (creates missing containers)."""

    err = _guard_tool("update_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    val_raw = str(value_json or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if not val_raw:
        return _tool_error("value_json is required")

    if ptr in {"/name", ""}:
        return _tool_error("/name cannot be modified via update_npc")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it is synced from the scene when a turn ends")
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it is set automatically when a turn ends")

    if logs_enabled():
        print(f"[trace] update_npc field: {name} {ptr} = {val_raw}")

    data = _WORLD.add_npc_json(name=name, pointer=json_pointer, value=value_json)
    return _json(data)


@tool
def delete_npc_path(name: str, json_pointer: str) -> str:
    """Delete a field from an NPC record using JSON Pointer."""

    err = _guard_tool("delete_npc_path", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("json_pointer is required")
    if ptr == "/":
        return _tool_error("Cannot delete document root")
    if ptr == "/name":
        return _tool_error("/name cannot be deleted")
    if ptr == "/location":
        return _tool_error("/location is runtime-managed; it cannot be deleted via GM tools")
    if ptr == "/last_acted":
        return _tool_error("/last_acted is runtime-managed; it cannot be deleted via GM tools")

    if logs_enabled():
        print(f"[trace] delete_npc_path: {name} {ptr}")

    data = _WORLD.delete_npc_json(name=name, pointer=ptr)
    return _json(data)


@tool
def delete_npc(name: str) -> str:
    """Delete an NPC that is no longer relevant and most probably won't appear anymore."""

    err = _guard_tool("delete_npc", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()

    nm = str(name or "").strip()
    if not nm:
        return _tool_error("name is required")

    if logs_enabled():
        print(f"[trace] delete_npc: {nm}")

    _WORLD.delete_npc(nm)
    return f"NPC '{nm}' deleted."


@tool
def get_character_detail(name: str) -> str:
    """Fetch ALL character data: description + state + skills + equipment."""

    err = _guard_tool("get_character_detail")
    if err:
        return err
    _WORLD.ensure_initialized()
    nm = str(name or "").strip()
    if not nm:
        return _tool_error("name is required")
    if nm in _read_characters:
        return f"Already read — character '{nm}' details were already fetched this call."
    _read_characters.add(nm)
    merged = {}
    try:
        desc = _WORLD.get_character_description(name)
        if desc:
            merged["description"] = desc
    except Exception:
        pass
    try:
        state = _WORLD.get_character_state(name)
        if state:
            merged["state"] = state
    except Exception:
        pass
    try:
        skills = _WORLD.get_character_skills(name)
        if skills:
            merged["skills"] = skills
    except Exception:
        pass
    try:
        equip = _WORLD.get_character_equipment(name)
        if equip:
            merged["equipment"] = equip
    except Exception:
        pass
    return _json(merged)





@tool
def update_character_state(name: str, json_pointer: str, value_json: str) -> str:
    """Update character state.json.

    Two modes — choose one:

    A) Full replace — provide the COMPLETE state dict in one call.
       Set json_pointer="/", value_json='{"hp":12,"max_hp":20,"fp":10,
       "max_fp":14,"er":5,"max_er":10,"will":10,"sanity":10,
       "max_sanity":10,"hunger":0,"thirst":0,"fatigue":0,
       "appearance":"...","conditions":[],"injuries":[]}'

    B) Single field — set json_pointer to a path like "/hp",
       value_json="12". Other fields are preserved.

    If state.json does not exist yet, use mode A (full replace) to create it.
    """

    err = _guard_tool("update_character_state", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    state = _WORLD.get_character_state(name)
    ptr = str(json_pointer or "").strip()
    if not ptr:
        return _tool_error("Invalid json_pointer")
    try:
        parsed = json.loads(value_json) if isinstance(value_json, str) else value_json
    except Exception as e:
        return _tool_error(f"Invalid value_json: {e}")
    if not state and ptr != "/":
        return _tool_error(
            f"Character '{name}' has no state.json yet. "
            "Use mode A (json_pointer=\"/\") with a complete state dict to create it."
        )
    if ptr == "/":
        if isinstance(parsed, dict):
            state = parsed
        else:
            return _tool_error("Full replace mode expects a JSON object (dict)")
    else:
        from world.json_pointer import set_at_pointer
        set_at_pointer(state, ptr, parsed, create_missing=False)
    _WORLD.set_character_state(name, state)
    # Don't strip description fields again — already done on first creation
    p = _WORLD._character_state_path(name)
    from world.io import _write_json
    _write_json(p, state)
    return _json(state)


@tool
def update_character_skills(name: str, skills_json: str) -> str:
    """Update character skills.json. Structure:
    {"stats": {...}, "narrative_tier": "...", "origin": "...",
     "passive_abilities": [...], "lore_interpretation_rule": "...",
     "cached_skills": [...]}
    Send only the fields you want to change; omitted fields are preserved.
    """

    err = _guard_tool("update_character_skills", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    try:
        data = json.loads(skills_json)
    except Exception as e:
        return _tool_error(f"Invalid JSON: {e}")
    if not isinstance(data, dict):
        return _tool_error("skills_json must be a JSON object")

    existing = _WORLD.get_character_skills(name)
    if not existing:
        # No existing file — model must provide all fields at once
        required = {"stats", "narrative_tier", "origin"}
        missing = required - set(data.keys())
        if missing:
            return _tool_error(
                f"Character '{name}' has no skills.json yet. "
                "Send ALL fields in one call: stats, narrative_tier, origin, "
                "passive_abilities, lore_interpretation_rule, cached_skills."
            )
        skills = {
            "stats": data.get("stats", {}),
            "narrative_tier": str(data.get("narrative_tier", "mortal") or "mortal").strip(),
            "origin": str(data.get("origin", "") or "").strip(),
            "passive_abilities": data.get("passive_abilities", []) if isinstance(data.get("passive_abilities"), list) else [],
            "lore_interpretation_rule": str(data.get("lore_interpretation_rule", "") or "").strip(),
            "cached_skills": data.get("cached_skills", []) if isinstance(data.get("cached_skills"), list) else [],
        }
    else:
        # Merge: replace scalar fields, merge passives, append cached
        skills = dict(existing)
        if "stats" in data:
            skills["stats"] = data["stats"]
        if "narrative_tier" in data:
            skills["narrative_tier"] = str(data["narrative_tier"]).strip()
        if "origin" in data:
            skills["origin"] = str(data["origin"]).strip()
        if "lore_interpretation_rule" in data:
            skills["lore_interpretation_rule"] = str(data["lore_interpretation_rule"]).strip()
        # Merge passives: replace same-name, append new
        new_passives = data.get("passive_abilities", [])
        if isinstance(new_passives, list):
            existing_passives = list(skills.get("passive_abilities", []))
            for np in new_passives:
                if not isinstance(np, dict):
                    continue
                nname = np.get("name", "")
                found = False
                for ep in existing_passives:
                    if isinstance(ep, dict) and ep.get("name") == nname:
                        ep.update(np)
                        found = True
                        break
                if not found:
                    existing_passives.append(np)
            skills["passive_abilities"] = existing_passives
        # Append to cached_skills
        new_cached = data.get("cached_skills", [])
        if isinstance(new_cached, list):
            existing_cached = list(skills.get("cached_skills", []))
            for nc in new_cached:
                if not isinstance(nc, dict):
                    continue
                nname = nc.get("name", "")
                found = False
                for ec in existing_cached:
                    if isinstance(ec, dict) and ec.get("name") == nname:
                        ec.update(nc)
                        found = True
                        break
                if not found:
                    existing_cached.append(nc)
            skills["cached_skills"] = existing_cached

    _WORLD.set_character_skills(name, skills)
    return _json(skills)


@tool
def update_character_equipment(name: str, equipment_json: str) -> str:
    """Update character equipment.json. Structure:
    {"signature": [...], "inventory": [...], "currency": {...},
     "lore_interpretation_rule": "..."}
    Send only the fields you want to change; omitted fields are preserved.
    """

    err = _guard_tool("update_character_equipment", require_unlocked=True)
    if err:
        return err
    _WORLD.ensure_initialized()
    try:
        equip = json.loads(equipment_json)
    except Exception as e:
        return _tool_error(f"Invalid JSON: {e}")
    if not isinstance(equip, dict):
        return _tool_error("equipment_json must be a JSON object")
    # Merge with existing (partial update support)
    existing = _WORLD.get_character_equipment(name)
    if existing:
        sig = equip.get("signature", []) if isinstance(equip.get("signature"), list) else existing.get("signature", [])
        inv = equip.get("inventory", []) if isinstance(equip.get("inventory"), list) else existing.get("inventory", [])
        currency = equip.get("currency", {}) if isinstance(equip.get("currency"), dict) else existing.get("currency", {})
        lore = str(equip.get("lore_interpretation_rule", "") or existing.get("lore_interpretation_rule", "") or "").strip()
    else:
        # No existing file — model must provide all fields at once
        has_all = {"signature", "inventory", "currency"}.issubset(equip.keys())
        if not has_all:
            return _tool_error(
                f"Character '{name}' has no equipment.json yet. "
                "Send ALL fields in one call: signature, inventory, currency."
            )
        sig = equip.get("signature", []) if isinstance(equip.get("signature"), list) else []
        inv = equip.get("inventory", []) if isinstance(equip.get("inventory"), list) else []
        currency = equip.get("currency", {}) if isinstance(equip.get("currency"), dict) else {}
        lore = str(equip.get("lore_interpretation_rule", "") or "").strip()
    normalized = {"signature": sig, "inventory": inv, "currency": currency}
    if lore:
        normalized["lore_interpretation_rule"] = lore
    _WORLD.set_character_equipment(name, normalized)
    return _json(normalized)


@tool
def read_character_diary(name: str) -> str:
    """Read a character's private diary — their personal summary of past experiences.

    Returns the diary content including arc summaries and recent paragraphs.
    """
    err = _guard_tool("read_character_diary")
    if err:
        return err

    try:
        from character.reflection import load_diary
        diary = load_diary(name)
    except Exception as exc:
        return _tool_error(f"Failed to load diary for '{name}': {exc}")

    arc_summaries = diary.get("arc_summaries") or []
    paragraphs = diary.get("paragraphs") or []

    if not arc_summaries and not paragraphs:
        return f"Character '{name}' has no diary entries yet."

    return _json({"arc_summaries": arc_summaries, "paragraphs": paragraphs})



