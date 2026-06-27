"""Detailed parameter documentation for WM tools — served on-demand via read_tool_doc().

This data is NEVER sent to the LLM API as tool schema. It is only read
when the agent explicitly calls read_tool_doc(tool_name, parameters).
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Maps tool_name -> param_name -> detailed description
DOCS: Dict[str, Dict[str, str]] = {
    # ==================================================================
    "set_entity": {
        "_tool": (
            "**Create or update any entity. The universal write operation.** "
            "Works for templates, characters, factions, locations, objects, "
            "effects, laws, alliances — anything.\n\n"
            "Entity Types — the WM defines these strings; the engine has no reserved types. "
            "Conventions (not enforced): 'race_template', 'class_template', 'character', "
            "'npc', 'faction', 'location', 'object', 'effect_source', 'political_entity', 'concept'."
        ),
        "id": (
            "Unique entity identifier. If empty, auto-generated and returned. "
            "On update: provide existing id to modify the entity in place. "
            "Convention: use readable slugs, e.g. 'char_ainz', 'faction_nazarick', "
            "'tpl_undead', 'loc_e_rantel'. Avoid spaces."
        ),
        "type": (
            "Entity type string. Required on create; ignored on update. "
            "The WM defines these strings — the engine has no reserved types."
        ),
        "base_type": (
            "entity_id of a template to inherit from. Optional. "
            "Inheritance: facts, gurps, stats, tags, and scripts from the template "
            "apply unless overridden here. Overrides are per-field. "
            "For multiple inheritance (e.g. race + class), use a chain."
        ),
        "facts": (
            "Prose description for LLM agents. Keep this minimal and functional. "
            "This is NOT the place for lore — it is context the GM and SM need "
            "to play this entity correctly: personality, current situation, "
            "relationships, secrets, behavioral tendencies. "
            "Bad: 'Ainz is the last surviving member of his guild who was transported...' "
            "Good: 'Performs the role of omniscient overlord while privately improvising.' "
            "Max recommended: 200 words."
        ),
        "gurps": (
            "GURPS stat block dict. See §Core Concepts — GURPS Stats Block for all keys. "
            "Primary attributes: ST, DX, IQ, HT (human average: 10). "
            "Secondary: HP, Will, Per, FP, Basic_Speed, Basic_Move. "
            "Combat: DR, Dodge. "
            "Resistances (0.0–1.0): poison_resist, disease_resist, fire_resist, "
            "mental_resist, holy_resist, cold_resist. "
            "Power tier: power_tier (1=civilian, 5=elite, 10=demigod). "
            "Character points: cp_total."
        ),
        "stats": (
            "Free numeric properties beyond GURPS. Any string keys, float values. "
            "Examples: {'level': 100}, {'magic_affinity': 3.0}, {'loyalty': 1.0}, "
            "{'hull_integrity': 1.0}, {'population': 120000}, {'food_reserves': 0.6}. "
            "Scripts and queries can read and modify these."
        ),
        "tags": (
            "Structured labels. Format 'namespace:value'. "
            "Standard namespaces: "
            "'race:undead', 'faction:nazarick', 'class:warrior', "
            "'tier:legendary', 'status:hostile', 'alignment:evil', "
            "'energy:negative', 'setting:new_world', 'created:yggdrasil'. "
            "Tags are the query surface for group membership. Use them."
        ),
        "position": (
            "Where this entity is. Three forms: "
            "{'inside': 'entity_id'} — contained within another entity. "
            "{'near': 'entity_id'} — approximately co-located. "
            "{'coords': [x, y, z]} — absolute world coordinates. "
            "Omit for non-spatial entities (laws, alliances, abstract concepts)."
        ),
    },
    # ==================================================================
    "set_entity_facts": {
        "_tool": (
            "**Update only the prose facts of an entity.** "
            "Does not touch stats, tags, or scripts. "
            "Use for targeted fact updates without risk of overwriting other fields."
        ),
        "id": "Entity identifier. REQUIRED.",
        "facts": "Complete replacement of the facts field. Keep ≤200 words.",
    },
    # ==================================================================
    "get_entity": {
        "_tool": (
            "**Read a single entity in full, including scripts.** "
            "Returns all fields, active effects, movement records, and attached scripts."
        ),
        "id": "Entity identifier. REQUIRED.",
    },
    # ==================================================================
    "get_entity_facts": {
        "_tool": (
            "**Read only the prose facts of an entity.** "
            "Used by SM/GM to load NPC context on demand without receiving full mechanical stats."
        ),
        "id": "Entity identifier. REQUIRED.",
    },
    # ==================================================================
    "add_tags": {
        "_tool": "**Add tags to an entity** without touching other fields.",
        "entity_id": "Entity to modify.",
        "tags": "List of 'namespace:value' strings to add.",
    },
    # ==================================================================
    "remove_tags": {
        "_tool": "**Remove tags from an entity** without touching other fields.",
        "entity_id": "Entity to modify.",
        "tags": "List of 'namespace:value' strings to remove.",
    },
    # ==================================================================
    "query_entities": {
        "_tool": (
            "**Read entities from world state. Supports flexible filtering.** "
            "Use structural queries (tags, type) as primary filters. "
            "facts_search is semantic/fuzzy — use sparingly."
        ),
        "type": "Filter by entity type string. Empty = any type.",
        "base_type": "Filter to instances of a specific template (direct or inherited).",
        "tags": (
            "Filter by tags. All listed tags must be present on matching entities. "
            "Example: ['faction:nazarick', 'tier:legendary'] = legendary Nazarick entities."
        ),
        "position": "Spatial filter center. Same three forms as entity position.",
        "radius": "Spatial radius in world units. -1 = no spatial filter.",
        "facts_search": (
            "Semantic search over entity facts text. Fuzzy match. "
            "Use for natural language queries. Use sparingly."
        ),
        "include_templates": (
            "True = include template entities in results. "
            "False (default) = exclude templates; return only instances."
        ),
        "limit": "Maximum results returned. Increase for broad scans. Default 50.",
    },
    # ==================================================================
    "query_events": {
        "_tool": (
            "**Read from the 4D causal event log.** "
            "Queries can filter on any combination of spatial region, time range, "
            "entity, and tag. The log is append-only, never modified or deleted."
        ),
        "position": "Spatial filter center.",
        "radius": "Spatial radius. -1 = no spatial filter.",
        "z_min": "Lower bound of vertical band. Default covers all depths.",
        "z_max": "Upper bound of vertical band.",
        "t_from": "Tick range start. -1 = unbounded. Example: world_tick-30 = last 30 ticks.",
        "t_to": "Tick range end. -1 = unbounded.",
        "entity_id": "Filter: events where this entity is listed in entity_ids.",
        "tag": "Filter: events with this tag.",
        "narrative_flag_only": "True = return only events flagged for GM/SM attention.",
        "causal_parent": "Return events caused by this event_id (consequence chain).",
        "limit": "Maximum results. Default 100.",
    },
    # ==================================================================
    "record_event": {
        "_tool": (
            "**Write an event to the 4D causal log. Append-only.** "
            "Use for recording occurrences — battles, deaths, diplomacy, discoveries. "
            "For asserting current entity state, use set_entity. "
            "Every call creates an entry indexed by x, y, z (spatial) and t (world tick). "
            "The log is never modified or deleted."
        ),
        "description": (
            "What happened. Written for LLM consumption — clear and specific. "
            "Bad: 'conflict occurred'. "
            "Good: 'Gazef Stronoff's unit engaged undead forces north of E-Rantel. "
            "Undead dispersed without significant losses.'"
        ),
        "entity_ids": (
            "All entities causally involved. Include both actors and patients: "
            "attacker, defender, witness. Used for query filtering."
        ),
        "position": "Where it happened. Used for spatial 4D log indexing.",
        "t_override": (
            "Specific world tick to log this event at. WM-only field. "
            "-1 = current world tick. Use for asserting historical events."
        ),
        "tag": (
            "Single categorical label for query filtering. "
            "Examples: 'combat', 'political', 'discovery', 'death', 'first_contact', "
            "'economic', 'magic', 'wm_correction'."
        ),
        "narrative_flag": (
            "True = this event appears in GM and SM newspaper on next invocation. "
            "Use for events that should proactively reach narrative agents. "
            "False = event is in the log but agents must query to find it."
        ),
        "causal_parent": (
            "event_id of the event that caused this one. "
            "Enables causal chain traversal in query_events. "
            "Leave empty if this event has no traceable cause."
        ),
    },
    # ==================================================================
    "attach_script": {
        "_tool": (
            "**Attach a behavior script to an entity.** "
            "Scripts run automatically when their trigger fires. "
            "Multiple scripts may be attached to one entity."
        ),
        "entity_id": "The entity this script is attached to. REQUIRED.",
        "trigger_type": (
            "What causes this script to run. One of: "
            "'on_tick' — fires every N ticks (params: interval_ticks). "
            "'on_stat_threshold' — fires when a stat crosses a threshold "
            "(params: stat, threshold, direction). "
            "'on_entity_enter' — fires when matching entity enters this entity's location "
            "(params: tag_filter, radius). "
            "'on_event_nearby' — fires when matching event occurs within radius "
            "(params: tag_filter, radius). "
            "'on_world_time' — fires once at a specific tick (params: tick). "
            "'on_tag_added' — fires when a specific tag is added to this entity (params: tag)."
        ),
        "trigger_params": "Parameters for the chosen trigger_type. See trigger_type docs.",
        "script": (
            "Python source. Called with context dict containing entity, trigger_data, world_tick. "
            "Available tools: all WM tools. Available stdlib: math, random, re, datetime. "
            "Scripts should be narrow and deterministic. If judgment is needed, "
            "call invoke_self() with a comment and let the agent decide."
        ),
        "comment": (
            "Why this script exists. Appears in the newspaper when script fires. "
            "Example: 'Fear aura — fires every tick, applies Will check to nearby living'."
        ),
    },
    # ==================================================================
    "detach_script": {
        "_tool": "**Remove a script from an entity.**",
        "entity_id": "Entity the script is attached to.",
        "script_id": "Returned by attach_script.",
    },
    # ==================================================================
    "set_effect": {
        "_tool": (
            "**Apply a persistent stat modifier to an entity.** "
            "Use for simple, arithmetic auto-applying effects: bleeding, regeneration, "
            "poison progression, power drain. "
            "For effects that require world queries or conditional logic, use attach_script."
        ),
        "entity_id": "Target entity. REQUIRED.",
        "effect_id": (
            "Unique id for this effect on this entity. Auto-generated if empty. "
            "Use a readable id if you'll need to remove it later: "
            "'bleeding_wound_1', 'lich_fear_debuff', 'poison_stage_2'."
        ),
        "stat_deltas": (
            "Stats to modify, and by how much each tick (if per_tick=True) "
            "or as a one-shot (if per_tick=False). "
            "Keys must exist in gurps or stats on the entity, or will be created. "
            "Examples: {'HP': -2} — bleeding; {'HP': 1} — regeneration; "
            "{'FP': -1, 'Basic_Move': -1} — exhaustion. "
            "Negative = deplete, positive = restore."
        ),
        "per_tick": (
            "True (default): delta applied every world tick until duration_ticks expires "
            "or remove_effect is called. "
            "False: one-shot delta applied immediately on set_effect call."
        ),
        "duration_ticks": (
            "How many ticks before this effect expires automatically. "
            "-1 = permanent until remove_effect is called. "
            "Example: 10 = lasts 10 world ticks."
        ),
        "description": "Human-readable name. Appears in entity state, newspaper, GM context.",
    },
    # ==================================================================
    "remove_effect": {
        "_tool": "**Remove a named effect from an entity immediately.**",
        "entity_id": "Target entity.",
        "effect_id": "The effect to remove.",
    },
    # ==================================================================
    "set_movement": {
        "_tool": (
            "**Set an entity in motion toward a destination.** "
            "WM-only — positional authority over world-scale entity movement belongs to the WM. "
            "The system updates position automatically over ticks, writes position events "
            "to the 4D log, and fires on_entity_enter/on_arrive triggers on arrival."
        ),
        "entity_id": "Entity to move. REQUIRED.",
        "destination": (
            "Where the entity is heading. Same three forms as position. "
            "If destination is another entity that is itself moving, the system "
            "tracks the target's position at each tick. Use for pursuit/escort."
        ),
        "speed": (
            "World units per tick. Set to 0 for instant teleportation. "
            "Calibrate against your world's coordinate scale. "
            "Example for 1 unit = 1 km, 1 tick = 1 day: "
            "human walking ≈ 0.04, army marching ≈ 0.025, dragon flying ≈ 0.5, "
            "teleportation = 0."
        ),
        "comment": "Why this entity is moving. Written to movement event log.",
    },
    # ==================================================================
    "delete_entity": {
        "_tool": (
            "**Permanently remove an entity. WM-only.** "
            "Does not remove the entity from the event log — past events that reference it "
            "remain. Sets a deleted_at_tick marker on the entity record."
        ),
        "id": "Entity to delete. REQUIRED.",
        "reason": "Why — written to event log.",
    },
    # ==================================================================
    "invoke_self": {
        "_tool": (
            "**Schedule the calling agent's next invocation. Universal scheduling primitive.** "
            "Can be called from tool calls and from inside scripts. "
            "Exactly one of delay_ticks or world_time must be provided."
        ),
        "delay_ticks": (
            "Relative delay: invoke this agent N ticks from now. "
            "-1 = not used (provide world_time instead)."
        ),
        "world_time": (
            "Absolute world tick to invoke at. "
            "-1 = not used (provide delay_ticks instead)."
        ),
        "comment": (
            "Why — appears at the top of the agent's context on invocation. "
            "Be specific: 'Demiurge's scheming tick — review political events' "
            "is better than 'periodic check'."
        ),
    },
    # ==================================================================
    "write_correction": {
        "_tool": (
            "**Send a forward-facing correction to the GM.** "
            "Does not retroactively change anything. Injected into GM's context "
            "on next invocation. Use when WM sees a GM-created entity or event "
            "that contradicts world canon — correct going forward, not backward."
        ),
        "message": (
            "What the WM wants the GM to know and apply going forward. "
            "Be specific: state what was wrong and what should be true instead. "
            "The GM will apply this in narration; no automatic state changes occur."
        ),
        "entity_id": (
            "If the correction involves a specific entity, reference it here. "
            "WM should also call set_entity to update the record alongside this message."
        ),
    },
    # ==================================================================
    "read_tool_doc": {
        "_tool": (
            "**Read detailed parameter documentation for any WM tool.** "
            "Call this when you need to understand specific parameter options, "
            "valid values, ranges, or schema details before making a tool call. "
            "The documentation covers all parameters, their allowed values, "
            "defaults, ranges, and example usage."
        ),
        "tool_name": (
            "Name of the tool to get docs for. One of: "
            "set_entity, set_entity_facts, get_entity, get_entity_facts, "
            "add_tags, remove_tags, query_entities, query_events, "
            "record_event, attach_script, detach_script, "
            "set_effect, remove_effect, set_movement, delete_entity, "
            "invoke_self, write_correction, read_tool_doc."
        ),
        "parameters": (
            "Optional list of specific parameter names to get docs for. "
            "If None or empty, returns docs for ALL parameters of the specified tool. "
            "Example: ['id', 'type'] — only valid parameter names are returned."
        ),
    },
}


def get_tool_doc(tool_name: str, parameters: Optional[List[str]] = None) -> str:
    """Get detailed documentation for a tool and its parameters.

    Args:
        tool_name: Name of the tool to get docs for. Case-sensitive.
        parameters: Optional list of specific parameter names.
            If None or empty, returns docs for ALL parameters.

    Returns:
        Formatted markdown documentation string.
    """
    tool_doc = DOCS.get(tool_name)
    if not tool_doc:
        available = ", ".join(sorted(DOCS.keys()))
        return f"**Unknown tool '{tool_name}'.** Available tools: {available}"

    parts = []

    # Tool-level description
    tool_desc = tool_doc.get("_tool", "")
    if tool_desc:
        parts.append(tool_desc)

    # Filter parameters
    param_keys = [k for k in tool_doc if k != "_tool"]
    if parameters:
        param_keys = [k for k in param_keys if k in parameters]
        missing = [p for p in parameters if p not in tool_doc]
        if missing:
            parts.append(f"\n**Note:** Unknown parameters: {', '.join(missing)}")

    if param_keys:
        parts.append("\n---\n**Parameters:**")
        for key in param_keys:
            desc = tool_doc.get(key, "No documentation available.")
            parts.append(f"\n**{key}:** {desc}")

    return "\n".join(parts)
