"""Detailed parameter documentation for WM tools — served on-demand via read_tool_doc().

This data is NEVER sent to the LLM API as tool schema. It is only read
when the agent explicitly calls read_tool_doc(tool_name, parameters).
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Maps tool_name -> param_name -> detailed description
DOCS: Dict[str, Dict[str, str]] = {
    # ==================================================================
    "set_world_orientation": {
        "_tool": (
            "**One-time world initialization.** Establishes coordinate reference frame, "
            "global physics parameters, and ambient rare material types. "
            "Must be called BEFORE any other tool. Cannot be repeated after finalize_world_generation.\n\n"
            "Side effects:\n"
            "- Initialises H3 cell grid topology (~17 400 empty cells)\n"
            "- Registers ambient_rare_materials in world registry\n"
            "- Sets world_generation_status = 'authoring'\n"
            "- No procedural generation runs yet — that happens at finalize_world_generation"
        ),
        "planet_radius": (
            "Radius in ANY consistent unit. All subsequent distances use this unit. "
            "Determines cell size: cell_side_length = radius / 60. "
            "~17 400 top-level H3 cells regardless of value. REQUIRED."
        ),
        "reference_meridian": (
            "Longitude of the prime meridian. Arbitrary — pick a meaningful location. Default 0.0."
        ),
        "axial_tilt": (
            "Planet axial tilt in degrees. 0 = no seasons. 23.5 = Earth-like. "
            "Higher = extreme seasonal swings. Default 23.5. Rough range 0–90."
        ),
        "global_temperature_offset": (
            "Degrees Celsius added to every cell's derived temperature. "
            "Default 0.0. Rough range -50 to +50."
        ),
        "global_precipitation_modifier": (
            "Multiplier on derived precipitation. >1 = wetter world. <1 = drier world. "
            "Default 1.0. Range 0.0–10.0."
        ),
        "solar_intensity": (
            "Multiplier on total solar energy. <1 = dim star, cold world. "
            ">1 = bright star, hot world. Default 1.0. Range 0.0–5.0."
        ),
        "atmospheric_density": (
            "Temperature buffering factor. Low = wild temperature swings. "
            "High = greenhouse effect. Default 1.0. Range 0.0–5.0."
        ),
        "ocean_temperature": (
            "Base ocean surface temperature in Celsius. Affects maritime climate moderation. "
            "Default 15.0. Rough range -2 to 40."
        ),
        "tectonic_activity": (
            "Geological complexity during generation. 0.0–1.0. "
            "Low = stable cratons, few mountain ranges. "
            "High = active orogeny, many ranges. Default 0.5."
        ),
        "ambient_rare_materials": (
            "List of diffuse ambient material registrations. MOST WORLDS HAVE NONE (empty list).\n"
            "Use only for genuinely ambient materials (ley-line crystals, atmospheric compounds, "
            "background radiation). Discrete ore deposits use define_world_concept as ore_type.\n"
            "Each entry dict:\n"
            '  {"material_id": str,      # identifier for capability prerequisites\n'
            '   "name": str,\n'
            '   "gray_scott_feed": float, # feed rate [0.01–0.08]\n'
            '   "gray_scott_kill": float, # kill rate [0.04–0.07]\n'
            '   "diffusion_rate": float,  # spatial spread [0.0–1.0]\n'
            '   "geological_affinity": float, # correlation with tectonic_stress [0.0–1.0]\n'
            '   "description": str}\n'
            "Default []."
        ),
        "long_cycle_tick_interval": (
            "Ticks between L0 long-cycle updates (climate drift, geological stress). Default 1000."
        ),
        "climate_drift_rate": (
            "How much global_temperature_offset shifts per long-cycle tick. "
            "0 = stable climate. Positive = gradual warming. Negative = cooling. Default 0.0."
        ),
        "direction_names": (
            "Optional remapping of cardinal direction labels for narrative output.\n"
            '{"north": "spinward", "south": "rimward", "east": "coreward", ...}\n'
            "Affects natural language output only. All math uses standard lat/lon. Default {}."
        ),
        "world_name": "Display name of the world. Used in query outputs and GM context.",
        "tech_level_default": (
            "GURPS TL applied to any civilisation not given an explicit tech level. "
            "TL0=stone age, TL4=medieval, TL8=near-future, TL12=super-science. Default 4."
        ),
    },
    # ==================================================================
    "set_player_start": {
        "_tool": (
            "**Establish the coordinate origin** for the campaign starting position.\n\n"
            "Called early in authoring phase (after set_world_orientation, before "
            "any location_relative constraints) — all location_relative constraints "
            "are measured from this origin.\n\n"
            "One-time call — cannot be called again after finalize_world_generation.\n\n"
            "Provide EXACTLY ONE of location_absolute OR location_region_hint."
        ),
        "location_absolute": (
            'Precise coordinates: {"lat": float, "lon": float, "alt": float (optional)}.\n'
            "Use when you have a specific real-world-analog location in mind "
            "(e.g. Waterdeep's approximate lat/lon on a continent outline)."
        ),
        "location_region_hint": (
            "Natural language region description:\n"
            "'northern coast', 'temperate river valley', 'mountain pass between two ranges'.\n"
            "Generator resolves to a concrete point satisfying any already-declared constraints."
        ),
        "required_properties": (
            "Properties the generated terrain must satisfy.\n"
            '{"biome_category": "temperate", "is_coastal": true,\n'
            ' "elevation_range": [0.1, 0.4], "near_navigable_water": true}\n'
            "Used whether location is absolute or region_hint. Default {}."
        ),
        "player_faction_id": (
            "Optional faction_id (to be defined via define_faction) designated as "
            "the players'/party's home faction. Default ''."
        ),
        "cause": "Narrative description of the starting position's significance. Default ''.",
    },
    # ==================================================================
    "finalize_world_generation": {
        "_tool": (
            "**Trigger procedural world generation** from declared constraints.\n\n"
            "One-time, explicit call. Transitions world_generation_status from "
            "'authoring' to 'generated'. After this, all tools operate on real generated "
            "content; authoring-mode constraint semantics no longer apply.\n\n"
            "THE WM MUST REVIEW everything declared during authoring BEFORE calling.\n\n"
            "Cannot be undone (no rewind). If unsatisfactory, continue live-play authoring "
            "on the generated world rather than re-generating."
        ),
        "world_seed": (
            "Random seed for aspects not fully determined by constraints "
            "(coastline detail, ore deposit placement, etc.). "
            "0 = random seed (recorded in return for reproducibility). Default 0."
        ),
        "constraint_priority": (
            "'strict' (default): generation must satisfy EVERY declared constraint "
            "exactly as specified. Use for canon-constrained worlds (Forgotten Realms).\n"
            "'best_effort': satisfy constraints where geologically plausible, "
            "report unsatisfiable in warnings. Use for original worlds."
        ),
        "review_only": (
            "If true: validates all declared constraints for consistency WITHOUT "
            "running generation or changing status. Returns the same validation_errors "
            "as a real run. world_generation_status remains 'authoring'. Default False."
        ),
    },
    # ==================================================================
    "define_world_concept": {
        "_tool": (
            "**Register any named world-specific type** into the simulation registry.\n\n"
            "This is the SINGLE entry point for extending the world's vocabulary. "
            "Must be called BEFORE concept_id is referenced anywhere else.\n\n"
            "See wm_tools.md Part II Tool 4 for the FULL parameter schema per concept_type."
        ),
        "concept_type": (
            "What kind of concept. OPEN STRING. Recognised types:\n"
            "- existence_type: entity need vector (mortal, construct, machine, ...)\n"
            "- stratum: social class (peasant, merchant, noble, ...)\n"
            "- norm: cultural dimension (violence_tolerance, ...)\n"
            "- mineral: single mineral species, prerequisite for ore_type\n"
            "- ore_type: formation rule for mineral deposits\n"
            "- flora_pft: plant functional type (climate envelope, growth, harvest)\n"
            "- fauna_species: animal population template (habitat, demographics, drops)\n"
            "- knowledge_domain: intellectual capital container\n"
            "- research_project: discrete research task (RimWorld/HoI4 model)\n"
            "- capability: named action unlocked by research\n"
            "- research_institution: provides research_points/tick\n"
            "- myth: structural awareness unit, propagates via L2.5 networks\n"
            "- item: non-actor object (artifact, cursed equipment)\n"
            "- canon_constraint: standing assertion checked every tick (R15)\n"
            "- faction_template: default stocks/flows/rules for faction archetype\n"
            "- settlement_type: footprint coefficients for settlement category (R19)\n"
            "- behavior_template: reusable entity behaviour profile\n"
            "- damage_type: combat damage category (piercing, fire, void, ...)\n"
            "- event_type: event category for rule conditions\n"
            "Any other string stored as named tag with parameters."
        ),
        "concept_id": (
            "Unique identifier. Used in ALL tool calls that reference this concept. "
            "If exists and update_existing=True, updates it."
        ),
        "name": "Display name. Defaults to concept_id if omitted.",
        "description": "WM narrative note. Stored for reference, NOT used by simulation engine.",
        "parameters": (
            "Type-specific parameters dict. Validated against concept_type.\n\n"
            "=== existence_type ===\n"
            '{"needs": [{"need_id": str, "decay_rate": float, "depletion_threshold": float,\n'
            '  "depletion_consequence": "seek_resource"|"distress_action"|"termination"|"cascade:need_id",\n'
            '  "replenishment_source": "L0"|"L1"|"L2"|"self_carried"|"raid"|"none",\n'
            '  "decay_modifiers": list[str]}],\n'
            ' "termination_model": "need_depletion"|"event_driven"|"never",\n'
            ' "termination_event": str, "scale_type": "individual"|"party"|"unit"|"swarm",\n'
            ' "biological": bool, "gurps_racial_template": str,\n'
            ' "base_st": int, "base_dx": int, "base_iq": int, "base_ht": int}\n\n'
            "=== mineral ===\n"
            '{"formula": str, "density_gcm3": float, "hardness_mohs": float,\n'
            ' "category": "mineral"|"ore_metal"|"ore_gem"|"ore_rare"|"ore_energy"|"fantasy",\n'
            ' "value": float (1-100 economic value index), "description": str}\n\n'
            "=== ore_type ===\n"
            '{"primary_ore": str, "secondary_ores": list[str],\n'
            ' "formation_type": "sedimentary"|"magmatic"|"hydrothermal"|"metamorphic"|"placer"|"arcane",\n'
            ' "host_rocks": list[str], "depth_range": [float, float],\n'
            ' "grade_range": [float, float], "rarity": float (0.0-1.0),\n'
            ' "vein_volume_range": [float, float], "description": str}\n\n'
            "=== research_project ===\n"
            '{"prerequisites": [{"project_id": str}], "research_cost": float,\n'
            ' "consumed_materials": {"stock_id": float, ...},\n'
            ' "required_institution": str, "scope": "faction"|"entity",\n'
            ' "knowledge_outputs": [{"domain_id": str, "value_added": float}],\n'
            ' "capability_unlocks": list[str], "discoverable": bool,\n'
            ' "notify_wm_on_complete": bool, "description": str}\n\n'
            "=== capability ===\n"
            '{"knowledge_prerequisites": [{"domain_id": str, "min_value": float}],\n'
            ' "material_prerequisites": [{"stock_id": str, "min_quantity": float}],\n'
            ' "use_cost": {"stock_id": float, ...}, "use_energy_cost": float,\n'
            ' "scope": "faction"|"entity"|"both", "repeatable": bool,\n'
            ' "cooldown_ticks": int,\n'
            ' "unlock_effects": list[dict],  # one-time on unlock\n'
            ' "standing_effects": [{"target_type": str, "target_id": str,\n'
            '   "field": str, "operation": "multiply"|"add"|"set", "value": float}],\n'
            ' "description": str}\n\n'
            "=== myth ===\n"
            '{"linked_projects": list[str], "conviction_threshold": float (0.0-1.0),\n'
            ' "propagation_rate": float, "decay_rate": float,\n'
            ' "initial_locations": [{"location": str, "conviction": float}],\n'
            ' "description": str}\n\n'
            "=== canon_constraint ===\n"
            '{"constraint_expression": str (boolean over world state),\n'
            ' "violation_response": "auto_resolve"|"subscribe_before",\n'
            ' "auto_resolve_template": dict,\n'
            ' "priority": "canon"|"soft_canon"|"flavor",\n'
            ' "narrative_reason": str, "description": str}\n\n'
            "=== settlement_type (R19) ===\n"
            '{"deforestation_factor": float, "hunting_factor": float,\n'
            ' "soil_modification_factor": float, "water_table_factor": float,\n'
            ' "hazard_modifier": float, "population_suppression_factor": float,\n'
            ' "ambient_material_extraction": list[str],\n'
            ' "recovery_rate_modifier": float, "description": str}'
        ),
        "update_existing": (
            "If True (default): updates concept if concept_id already exists. "
            "If False: returns validation error if concept_id already exists (strict creation)."
        ),
    },
    # ==================================================================
    "alter_feature": {
        "_tool": (
            "**Create, update, or dissolve a geographic/physical feature.**\n\n"
            "AUTHORING MODE (status='authoring'): registers a GENERATION CONSTRAINT. "
            "feature_id becomes constraint identifier. Operation = 'constraint_registered'.\n\n"
            "LIVE MODE (status='generated'): creates/updates/dissolves real content. "
            "Operation = 'created'/'updated'/'dissolved'. event_log_id is produced.\n\n"
            "feature_id empty = create new. Provided+exists = update. Provided+not exist = create with ID."
        ),
        "feature_id": "UUID or display_name of existing feature. Empty = create new. Upsert semantics.",
        "name": "Display name. Required for named features. Empty = unnamed background feature.",
        "feature_type": (
            "Open string. Recognised types:\n"
            "continent, elevation_feature, water_body, river, terrain_cover,\n"
            "climate_zone, geological_zone, ambient_material_zone, void_zone,\n"
            "mountain_pass, fault_line, settlement, ruin, underground_region,\n"
            "physics_override. Unknown types stored without L0 effects."
        ),
        "location_absolute": '{"lat": float, "lon": float, "alt": float (optional)}.',
        "location_relative": (
            '{"from": str (feature_id|entity_id|"player_start"|"world_center"|"north_pole"|"south_pole"),\n'
            ' "bearing": float (degrees clockwise from north),\n'
            ' "distance": float (world units),\n'
            ' "alt_offset": float (optional)}'
        ),
        "location_inside": "feature_id or name — places geometrically INSIDE a parent feature.",
        "location_near": "list of feature_ids or names — places ADJACENT to these features.",
        "location_region_hint": 'Natural language: "northern coast", "center of continent", "deep ocean".',
        "size_preset": '"tiny"|"small"|"medium"|"large"|"massive". Relative to feature_type and planet_radius.',
        "size_radius": "Explicit radius in world units.",
        "size_width": "Explicit width in world units.",
        "size_length": "Explicit length in world units.",
        "size_height": "Vertical extent above surroundings.",
        "size_depth": "Depth below surface (caves, ocean trenches).",
        "size_shape": 'Natural language: "elongated north-south", "crescent shaped", "branching delta".',
        "outline_vertices": 'Explicit polygon: [{"lat": float, "lon": float}, ...].',
        "properties": (
            "Open key-value. Recognised:\n"
            "navigable: bool, passable: bool, elevation_override: float,\n"
            'feeds_into: str (feature_id or "ocean"),\n'
            'resource_richness: "scarce"|"normal"|"rich"|"exceptional",\n'
            "climate_override: str (Köppen-Geiger code), hazard_modifier: float,\n"
            "depth: float, underground: bool. All other keys stored as metadata."
        ),
        "layer_effects": (
            "Overrides type-default L0 cell mutations:\n"
            '{"soil_fertility_modifier": float, "hazard_modifier": float,\n'
            ' "elevation_offset": float, "water_table_modifier": float,\n'
            ' "climate_override": str, "tectonic_stress_modifier": float}'
        ),
        "contains": (
            '{"items": list[item_id], "entities": list[entity_id],\n'
            ' "myth_seeds": list[dict]}. '
            "If discovery_required=True, all are latent until discovered."
        ),
        "discovery_required": (
            "If True, contains block is LATENT until a discover_location delta "
            "fires for this feature_id. Feature itself may still be visible."
        ),
        "physics_override": (
            'Non-standard physics in region:\n'
            '{"override_id": str, "description": str,\n'
            ' "field_modifiers": {"field_path": {"operation": "set"|"multiply"|"add", "value": float}},\n'
            ' "entity_effects": {"capability_suppression": list[str],\n'
            '   "need_decay_modifier": float, "rule_suppression": list[str]},\n'
            ' "exception_entities": list[str]}'
        ),
        "dissolved": "If True, dissolves this feature. feature_id required.",
        "dissolve_scheduled_tick": "If >0, schedule dissolution for this tick.",
        "dissolve_gradual_over_ticks": "If >0, feature shrinks over this many ticks before final dissolution.",
        "part_of": "This feature is a sub-feature of (feature_id or name).",
        "connected_to": "list of feature_ids with navigable connections (roads, passes, tunnels).",
        "feeds_into": 'Hydrological downstream target. feature_id or "ocean".',
        "anchor_strength": (
            "Authoring mode only:\n"
            '"suggestion" — may shift significantly for coherence\n'
            '"preferred" (default) — minor adjustments only\n'
            '"fixed" — exact placement guaranteed; seeds generation around it'
        ),
        "cause": 'Narrative reason. "centuries of farming", "volcanic activity", "magical corruption".',
    },
    # ==================================================================
    "define_entity": {
        "_tool": (
            "**Create or update any named entity** (L3, narrative, legendary).\n\n"
            "The primary tool for introducing ANY entity the GM may observe. "
            "Same tool handles village blacksmith (Tier 1), dragon (Tier 3 with immunities), "
            "military unit (scale='unit').\n\n"
            "GURPS → simulation mappings (applied unless overridden):\n"
            "  HP=(ST+HT)/2 → structural_integrity max\n"
            "  FP=HT → energy_reserve max\n"
            "  Unkillable 1/2/3 → termination_condition='event_driven'\n"
            "  Doesn't Eat/Drink → removes food/water needs\n"
            "  Regeneration (Fast/Slow) → structural_integrity recovery ×10/2\n"
            "  Magery N → energy_reserve max × (N+1)\n"
            "  Dependency (resource) → added need with depletion=termination\n"
            "  High Pain Threshold → structural_integrity threshold 0.05\n"
            "  Combat Reflexes → priority +2 on combat action rules\n"
            "  Doesn't Breathe → removes air need if present\n"
            "  Injury Tolerance (Homogenous/...) → immunity to specific damage_types"
        ),
        "entity_id": "UUID or display_name. If exists: updates. If empty: creates new.",
        "display_name": "Name in event logs, GM queries, and narrative references.",
        "archetype_id": "Behaviour profile identifier for bulk registration.",
        "existence_type": (
            "Registered concept_id (via define_world_concept). Determines need vector and termination. "
            "'mortal' is built-in (food/water/shelter/safety needs). All others world-defined."
        ),
        "tier": "1=need-driven (farmer, guard). 2=goal-driven (adventurer, spy). 3=HFSM (legendary).",
        "scale": '"individual"|"party"|"unit"|"swarm". unit/swarm: scale_count required.',
        "scale_count": "Number of constituent members for unit/swarm.",
        "faction_id": "Faction this entity belongs to. Affects L2.5 norm lookups.",
        "military_unit_of": "faction_id OR settlement_id whose military allocation this represents (R14).",
        "narrative_importance": '"background"|"notable"|"named"|"critical". Only named/critical get full continuity.',
        "gurps_sheet": (
            '{"st": int, "dx": int, "iq": int, "ht": int,\n'
            ' "advantages": list[str], "disadvantages": list[str],\n'
            ' "skills": {"name": int, ...}, "power_level": int (0-5),\n'
            ' "tech_level": int, "notes": str}\n'
            "Derives defaults per GURPS mapping. Explicit params override."
        ),
        "stocks": (
            'Each: {"stock_id": str, "initial_value": float, "max_value": float,\n'
            ' "decay_rate": float, "recovery_rate": float,\n'
            ' "depletion_consequence": str, "replenishment_source": str}'
        ),
        "capabilities_unlocked": "Capability concept_ids entity possesses. scope='entity' requires personal prereqs.",
        "inventory": "Registered item concept_ids carried. Items drop to last location on dissolution.",
        "behavior_mode": '"STATIONARY"|"PATROL"|"PATH_TO_GOAL"|"GOAL_SEEKING"|"FOLLOWING"|"DORMANT".',
        "behavior_parameters": (
            'PATROL: {"waypoints": [...], "loop": bool}\n'
            'PATH_TO_GOAL: {"goal_location": str}\n'
            'DORMANT: {"wake_conditions": list[str]}'
        ),
        "hfsm_states": (
            "Tier 3 only. Each:\n"
            '{"state_id", "behavior_mode", "entry_condition", "exit_condition",\n'
            ' "transitions": [{"to_state": str, "condition": str, "priority": int}]}'
        ),
        "auras": (
            'Each: {"aura_id", "radius": float, "falloff": "flat"|"linear",\n'
            ' "target_layer": str, "target_field": str,\n'
            ' "effect_type": str, "effect_magnitude": float}'
        ),
        "rules": (
            'Each: {"rule_id": str, "priority": int (1-10),\n'
            ' "cooldown_ticks": int, "condition": str,\n'
            ' "effects": list[dict], "narrative_flag": bool,\n'
            ' "myth_seeds": list[dict]}'
        ),
        "immunities": (
            'Each: {"immunity_type": str\n'
            '  (damage_type_id|"lanchester_attrition"|"need_termination"|"physics_override:id"),\n'
            ' "exception_sources": list[str]}'
        ),
        "pre_engagement_effects": (
            'Injected BEFORE L2 Lanchester combat. Each:\n'
            '{"effect_id", "condition", "target": str,\n'
            ' "field": str, "operation": "multiply"|"add"|"set",\n'
            ' "value": float, "energy_cost": float, "energy_source": str}'
        ),
        "termination_condition": (
            "Expression for event-driven termination. Overrides existence_type.\n"
            '"event.type == \'slain_by_named_hero\'"'
        ),
        "leadership_profile": (
            "What entity optimises when running a faction autonomously (Regime B, R11). "
            "Zero LLM cost — compiled once, runs deterministically.\n"
            '{"objective_weights": {"military_strength": float (0-1),\n'
            '  "resource_security": float, "territory_extent": float,\n'
            '  "knowledge_investment": float, "capability_pursuit": float,\n'
            '  "population_welfare": float, "trade_prosperity": float,\n'
            '  "institutional_stability": float, ...any norm_id...},\n'
            ' "risk_tolerance": float (0-1),\n'
            ' "time_horizon_ticks": int,\n'
            ' "constraints": [{"constraint_id": str, "condition": str}],\n'
            ' "faction_disposition_modifiers": {"faction_id": float}}'
        ),
        "authority_overrides": (
            "Which factions entity leads. Does NOT switch off L2 (R11).\n"
            'Each: {"faction_id": str, "authority_weight": float (0-1),\n'
            ' "domain": "all"|"military"|"economic"|"diplomatic"}'
        ),
        "continuity_depth": '"none" (default) | "shallow" | "deep".',
        "stub_lock_on_conflict": "If True, entity locks on contradiction check failure.",
        "wm_notify_on_conflict": "If True, contradiction pushed to WM notification queue immediately.",
        "query_summary_template": "Template for get_entity_state() summary.",
        "location_absolute": 'Same as alter_feature: {"lat": float, "lon": float, "alt": float}.',
        "location_relative": 'Same as alter_feature: {"from", "bearing", "distance", "alt_offset"}.',
        "location_inside": "Place inside parent feature by feature_id.",
        "location_near": "Place near feature by feature_id.",
        "cause": "Narrative reason for creation/update. Written to event log.",
    },
    # ==================================================================
    "define_faction": {
        "_tool": (
            "**Create or update any faction, civilisation, or proto-faction.**\n\n"
            "SOCIAL COMPLEXITY SCALE (R4):\n"
            "  0.0 = pure ecology — L1 population counts, no L2/L2.5\n"
            "  0.3 = proto-faction — ecological + emergent raiding/territory\n"
            "  0.6 = emergent civilisation — partial L2 dynamics, basic trade\n"
            "  1.0 = full civilisation — all L2/L2.5 dynamics active (default)\n\n"
            "LEADERSHIP (R11):\n"
            "  No council_members → leaderless; uses default from social_structure_type\n"
            "  council_members provided → effective profile = weighted aggregate\n"
            "  High disagreement → high decision_variance\n\n"
            "Authoring mode: registers settlement location constraints.\n"
            "Live mode: creates/updates/dissolves real factions."
        ),
        "faction_id": "UUID or display_name. Upsert.",
        "name": "Display name.",
        "faction_type": '"kingdom"|"guild"|"religion"|"tribe"|"corporation"|"hive_mind"|"proto_faction" or world-defined.',
        "template_id": "Registered faction_template concept_id. Applies defaults; explicit params override.",
        "social_complexity": "0.0–1.0. Controls active simulation layers. Default 1.0 (full civ).",
        "complexity_threshold": "If >0, auto-increases complexity when population exceeds threshold×initial. 0=fixed.",
        "settlements": (
            "LIST of settlement objects (R18). territory_cells DERIVED from these — never declared as input.\n"
            'Each: {"settlement_id": str, "settlement_type": str,\n'
            '  "location": str (feature_id), "population_share": float,\n'
            '  "settlement_tier": str, "control_radius": float,\n'
            '  "garrison": {"military_supply_share": float, "unit_entity_ids": list[str]},\n'
            '  "institutions": list[str], "resources_present": list[str],\n'
            '  "contains": dict, "garrison_collapse_threshold": float}\n'
            "Every faction has at least one settlement at ANY social_complexity."
        ),
        "territory_expansion_rules": "Rules for autonomous settlement founding. Same format as entity action rules.",
        "stocks": 'Each: {"stock_id": str, "initial_value": float, "max_value": float, "min_value": float}.',
        "flows": (
            'Each: {"flow_id": str, "description": str,\n'
            ' "source": str (L0:field|L1:field|L2:stock|external),\n'
            ' "sink": str, "rate": float, "rate_modifiers": list[str],\n'
            ' "condition": str}'
        ),
        "rules": "Faction-level IF condition THEN delta rules. Same schema as entity action rules.",
        "ideology_vector": 'L2.5 Deffuant opinion space: {"norm_id": float (0.0–1.0)}.',
        "social_structure_type": '"feudal"|"flat"|"theocratic"|"corporate"|"tribal"|"hive"|"council"|world-defined.',
        "strata": "Registered stratum concept_ids active in this faction. Omit for world-default strata.",
        "relationships": (
            'Trust scalars: {"faction_id": float (-1.0 hostile to +1.0 allied)}.\n'
            "For STRUCTURED agreements (alliances, vassalage) use define_relationship instead (R15)."
        ),
        "knowledge_stocks": 'Initial knowledge_domain values: {"domain_id": float (0.0–1.0)}.',
        "capabilities_unlocked": "Capability concept_ids this faction possesses at founding.",
        "institutions": (
            'Research institutions. Each:\n'
            '{"institution_id": str, "type_id": str, "name": str,\n'
            ' "quality": float (0-1), "location": str,\n'
            ' "staffing": float (0-1), "active": bool}'
        ),
        "active_research": (
            'Currently assigned projects. Each:\n'
            '{"institution_id": str, "project_id": str,\n'
            ' "progress": float, "assigned_entity": str}'
        ),
        "tech_level": "GURPS TL. -1 = use world default from set_world_orientation.",
        "underground": "True = subsurface territory. Affects L0 resource access and L2 trade.",
        "council_members": (
            "Leadership council (R11). Effective profile = weighted average of members.\n"
            'Each: {"entity_id": str, "role": str,\n'
            ' "authority_weight": float (0-1), "domain": str,\n'
            ' "succession_rule": str}\n'
            "Example — sole ruler:\n"
            '  [{"entity_id": "king_aldric", "role": "king",\n'
            '    "authority_weight": 1.0, "domain": "all"}]\n'
            "Example — feudal court:\n"
            '  [{"entity_id": "king_aldric", "role": "king",\n'
            '    "authority_weight": 0.6, "domain": "all"},\n'
            '   {"entity_id": "lord_vorn", "role": "marshal",\n'
            '    "authority_weight": 0.8, "domain": "military"}]'
        ),
        "decision_variance": "0.0–1.0. Noise in L2 decisions. Auto-computed from council disagreement if provided.",
        "intent_declarations": (
            'Active WM/GM directives. Each:\n'
            '{"intent_id": str, "issuer_id": str, "domain": str,\n'
            ' "description": str, "target": str, "intent_type": str,\n'
            ' "parameters": dict, "strength": float (0-1),\n'
            ' "expires_condition": str, "revoke_condition": str}'
        ),
        "dissolved": "Dissolve this faction.",
        "absorb_into": "faction_id to receive territory/members on dissolution.",
        "dissolve_scheduled_tick": "Scheduled dissolution tick.",
        "founding_tick": "Historical founding tick. 0 = current tick.",
        "cause": "Narrative reason. Default ''.",
    },
    # ==================================================================
    "define_rule": {
        "_tool": (
            "**Create or update a global event system production rule.**\n\n"
            "Fires on world state conditions independently of any entity or faction. "
            "The world's autonomous laws of causality.\n\n"
            "Condition examples:\n"
            '  "L2.settlement.population < 0.1 * founding_population AND world.time_since[founded] > 500"\n'
            '  "L0.cell.ambient_material_flux[ley_crystals] > 0.9 AND world.time_since[last_ley_surge] > 200"\n'
            '  "L2_5.faction.strength < 0.05 AND L2_5.faction.age > 200"'
        ),
        "rule_id": "Unique identifier. Upsert. REQUIRED.",
        "name": "Display name. Defaults to rule_id if omitted.",
        "description": "WM notes. Stored for reference.",
        "condition": (
            "Boolean expression. Variable namespaces:\n"
            "L0.cell[field], L2.settlement[field], L2_5.faction[field],\n"
            "world.tick, world.time_since[event_type]."
        ),
        "effects": "World-state deltas: modify_field, transfer_resource, spawn_entity, dissolve_entity, trigger_event, notify_wm.",
        "scope": '"global" (default) | "per_cell" | "per_entity" | "per_faction" | "per_settlement".',
        "priority": "1–10. Higher priority rules evaluated and applied FIRST. Default 5.",
        "cooldown_ticks": "Minimum ticks between firings. Prevents event storms. Default 0.",
        "enabled": "If False, stored but NOT evaluated. Default True.",
        "narrative_flag": "If True, all firings added to GM notification queue. Default False.",
        "myth_seeds": (
            'Myths planted at firing location. Propagate via L2.5 contact networks.\n'
            'Each: {"myth_id": str, "stratum_id": str,\n'
            ' "conviction": float, "radius": float}'
        ),
        "firing_limit": "0 = unlimited. >0: auto-disables after N firings. For one-time events.",
        "cause": "Narrative reason.",
    },
    # ==================================================================
    "declare_world_state": {
        "_tool": (
            "**Declare high-level narrative facts**, historical events, era parameters, "
            "prophecies, and world-building stubs.\n\n"
            "Primary tool for responding to narrative escalations and initialising world history. "
            "Combines narrative state, facts, prophecy tracking, and task scheduling."
        ),
        "era_name": "Current era name. If provided, updates the active era.",
        "era_parameters": 'Era baseline modifiers: {"magic_availability": 0.8, "social_mobility": 0.3}.',
        "facts": (
            'World facts committed as stubs. Each:\n'
            '{"fact_type": str (faction_exists|location_exists|entity_exists|\n'
            '  historical_event|relationship_state|resource_exists|institution_exists|world-defined),\n'
            ' "payload": dict, "stub_lock": bool,\n'
            ' "deadline_tick": int, "narrative_context": str}'
        ),
        "historical_events": (
            'Past events explaining current state. Written to event log with past tick values.\n'
            'Each: {"event_type": str, "description": str, "tick": int,\n'
            ' "location": str, "participants": list[str], "effects": dict}'
        ),
        "prophecies": (
            'Each: {"prophecy_id": str, "description": str,\n'
            ' "trigger_condition": str, "effects_on_fulfil": list[dict],\n'
            ' "narrative_flag": bool, "fulfilled": bool}'
        ),
        "age_transitions": (
            'Each: {"from_era": str, "to_era": str, "trigger_tick": int,\n'
            ' "trigger_condition": str, "transition_effects": list[dict]}'
        ),
        "deferred_tasks": (
            'Future tasks: {"task_type": str, "parameters": dict,\n'
            ' "deadline_tick": int, "dependencies": list[str],\n'
            ' "priority": "low"|"normal"|"high"|"immediate"}'
        ),
    },
    # ==================================================================
    "subscribe_to_events": {
        "_tool": (
            "**Register the WM's interest in categories of future events.**\n\n"
            "This is the WM's SOLE forward-looking mechanism (R13). WM does NOT "
            "advance time, poll, or get invoked every turn. World time advances "
            "as a consequence of player turn progression.\n\n"
            "'before'-timing: PAUSES the matching event's effects before commit. "
            "WM can allow, modify, or redirect — the ONLY point where outcome "
            "can be influenced (no rewind).\n"
            "'after'-timing: queues notification after commit, for awareness.\n\n"
            "No matching subscription = simulation runs that category unattended "
            "(R11 Regimes A/B already produce plausible behaviour)."
        ),
        "filters": (
            "LIST of filter dicts. A filter with NO scoping fields is REJECTED.\n"
            'Each: {"filter_id": str,\n'
            '  "event_types": list[str] (empty=any),\n'
            '  "entity_ids": list[str] (empty=any),\n'
            '  "faction_ids": list[str] (empty=any),\n'
            '  "location": {"feature_id": str} or {"region_hint": str},\n'
            '  "field_thresholds": [{"field_path": str, "operator": "<"|">"|"==",\n'
            '                        "value": float}],\n'
            '  "event_payload_filters": {"project_id": str, "capability_id": str,\n'
            '                            "myth_id": str, "feature_id": str, ...},\n'
            '    # Payload-identity: target a SPECIFIC project, capability, etc.\n'
            '  "severity_min": float (0.0-1.0),\n'
            '  "timing": "before"|"after",\n'
            '  "expires_condition": str (""=permanent),\n'
            '  "max_firings": int (0=unlimited)}'
        ),
        "replace_existing": "list of filter_ids to REMOVE before adding new filters.",
    },
    # ==================================================================
    "query_world_state": {
        "_tool": (
            "**Read current simulation state**, registry contents, world debt, "
            "notification queue, and entity/relationship/settlement rosters.\n\n"
            "WM's PRIMARY diagnostic tool before writing condition expressions or flow formulas."
        ),
        "query_type": (
            "REQUIRED. One of:\n"
            '"registry" — list ALL registered concepts. filters: {"concept_type": str}\n'
            '"variables" — available field paths for condition expressions\n'
            '"world_debt" — task queue depth, overdue tasks, stub locks\n'
            '"notifications" — pending WM notification queue\n'
            '"region" — geographic features, climate, resources\n'
            '"entity" — single entity state (use target_id)\n'
            '"entities" — ROSTER query (R14): entity summaries matching filters.\n'
            '  filters: {"faction_id", "tier", "behavior_mode", "existence_type",\n'
            '    "narrative_importance", "military_unit_of", "has_capability", "min_scale_count"}\n'
            '  fields: subset per entity. Use to DISCOVER entity_ids.\n'
            '"faction" — faction state (use target_id). Includes visible_projects.\n'
            '"relationships" — ROSTER (R15). filters: {"relationship_type", "faction_id", "role"}\n'
            '"settlements" — ROSTER (R18). filters: {"faction_id", "settlement_type",\n'
            '  "settlement_tier", "min_population_share"}\n'
            '"events" — event log (use time_range)\n'
            '"social_context" — L2.5 norm vectors, trust baselines, myth_vector\n'
            '"time" — current world time and tick\n'
            '"feature" — feature record (use target_id)'
        ),
        "target_id": 'For "entity", "faction", "feature": UUID or display_name.',
        "region_center": 'For region/events/social_context: {"feature_id"} or {"lat","lon"} or {"entity_id"}.',
        "region_radius": "Search radius in world units.",
        "time_range": 'For events: {"t_start": int, "t_end": int}.',
        "filters": "Type-specific filter dict. See query_type docs for per-type filter keys.",
        "fields": 'Subset of fields to return. For entities: ["entity_id", "display_name", "tier"].',
        "include_wm_detail": "If True, include WM-only fields. Default True.",
    },
    # ==================================================================
    "resolve_contradiction": {
        "_tool": (
            "**Declare authoritative world state going forward.**\n\n"
            "Used when simulation contradicts narrative, or a 'before'-timing "
            "subscription paused an event. NOT a general-purpose state editor.\n\n"
            "NO RECOMPUTATION (R13: no rewind). Consequences propagate forward "
            "from the declared state. narrative_reason is REQUIRED for audit."
        ),
        "contradiction_source": "notification_id from queue, or 'before'-timing event context. Empty = proactive.",
        "override_type": (
            "What to override:\n"
            '"entity_state" — override entity fields\n'
            '"faction_state" — override faction fields\n'
            '"pending_event" — REPLACE effects of paused event before commit\n'
            '"world_field" — override specific L0-L2.5 field\n'
            '"entity_location" — relocate entity\n'
            '"settlement_control" — WM-declared settlement ownership change (R18)'
        ),
        "target_id": "entity_id, faction_id, event_id, feature_id, or settlement_id. REQUIRED.",
        "declared_state": (
            "What is now true. Structure per override_type:\n"
            "entity_state: {'field_path': new_value, ...}\n"
            "faction_state: {'field_path': new_value, ...}\n"
            "pending_event: {'effects': list[dict]} (empty list = suppress event)\n"
            "world_field: {'field_path': str, 'value': float, 'region': str}\n"
            'entity_location: {"lat": float, "lon": float, "alt": float}\n'
            'settlement_control: {"new_faction_id": str,\n'
            '  "garrison_disposition": "reassign"|"release"}'
        ),
        "narrative_reason": "WHY. REQUIRED. Written to event log for auditing.",
        "release_stub_lock": "Release stub-lock on target after resolution. Default True.",
    },
    # ==================================================================
    "post_intent": {
        "_tool": (
            "**Issue a time-bounded directive to a faction's L2 decision engine.**\n\n"
            "WM declares *what*; simulation executes *how*. L2 is NOT switched off — "
            "adds a constraint to the existing objective function (R11 Regime C).\n\n"
            "Designed for fast use during play: one call, one directive, "
            "the simulation handles execution."
        ),
        "faction_id": "Which faction receives this directive. REQUIRED.",
        "intent_type": (
            "What the directive is. Open string. Common built-in:\n"
            '"concentrate_military" — move military toward target\n'
            '"defend_location" — defend feature/settlement. settlement_id scopes to garrison\n'
            '"pursue_faction" — pressure target faction\n'
            '"open_trade" — establish/expand trade\n'
            '"cease_expansion" — halt territorial expansion\n'
            '"prioritize_resource" — concentrate extraction on resource\n'
            '"purge_internal" — internal stability action\n'
            '"retreat" — withdraw from region\n'
            '"negotiate" — open diplomatic channel\n'
            "Any world-defined string accepted. REQUIRED."
        ),
        "description": "Narrative order as given in-world. Written to event log. REQUIRED.",
        "target": "feature_id, faction_id, entity_id, or location name.",
        "parameters": (
            "Intent-type-specific:\n"
            'concentrate_military: {"urgency": float, "force_fraction": float}\n'
            'defend_location: {"minimum_garrison": float}\n'
            'prioritize_resource: {"resource_id": str, "extraction_boost": float}\n'
            'open_trade: {"goods": list[str], "terms": str}'
        ),
        "issuer_id": "entity_id of NPC issuing order. Empty = WM direct. Authority_weight affects priority.",
        "strength": (
            "0.0–1.0. 1.0 = hard constraint (L2 will NOT violate). "
            "0.5 = strong influence. 0.2 = soft guidance. Default 0.8."
        ),
        "domain": '"all" (default) | "military" | "economic" | "diplomatic" | world-defined.',
        "expires_condition": 'Expression; auto-removes when true. "" = permanent. "world.day_of_year > 200".',
        "revoke_condition": 'Expression; auto-revokes when true. "L2.military_concentration[irongate] > 0.7".',
        "revoke_on_issuer_death": "If True, intent revoked if issuer entity dissolved. Default True.",
    },
    # ==================================================================
    "post_entity_directive": {
        "_tool": (
            "**Issue a scoped, live-play directive to a single L3 entity.**\n\n"
            "Redirects behavior_mode/behavior_parameters without touching "
            "stocks, rules, immunities, or inventory. Entity-scale analog of post_intent (R14).\n\n"
            "For wild/legendary entities: this is a PROPOSED directive — entity's own "
            "rules may override if priority_over_own_rules is low.\n\n"
            "Use query_world_state(query_type='entities') to discover entity_ids before targeting."
        ),
        "entity_id": "Target entity. REQUIRED.",
        "directive_type": (
            '"move_to" — PATH_TO_GOAL toward target\n'
            '"patrol" — PATROL over target\n'
            '"guard" — STATIONARY at target\n'
            '"follow" — FOLLOWING target entity\n'
            '"attack" — set high-priority combat rule targeting entity/faction\n'
            '"use_capability" — fire capability (params: {"capability_id": str})\n'
            '"retreat" — PATH_TO_GOAL toward home/anchor\n'
            '"wake" — for DORMANT: force wake_conditions check\n'
            "World-defined open string accepted. REQUIRED."
        ),
        "target": "feature_id, location name, or entity_id. For patrol: waypoints via parameters.",
        "parameters": (
            'move_to: {"path_algorithm": str, "urgency": float}\n'
            'patrol: {"waypoints": list[str], "loop": bool}\n'
            'guard: {"drift_radius": float}\n'
            'use_capability: {"capability_id": str}'
        ),
        "description": "Narrative description. Written to event log.",
        "duration": (
            '"until_complete" (default) — ends when behaviour naturally terminates\n'
            '"permanent" — becomes standing behaviour mode\n'
            "Otherwise: an expression (revoke_condition-style)."
        ),
        "priority_over_own_rules": (
            "0.0–1.0. How strongly directive competes with entity's own rules.\n"
            "1.0 = directive wins regardless (use sparingly — WM-controlled NPCs).\n"
            "0.5 (default) = applies unless higher-priority rule contests.\n"
            "Low values for wild/legendary entities whose character should be respected."
        ),
    },
    # ==================================================================
    "define_relationship": {
        "_tool": (
            "**Create or update a structured agreement between factions.**\n\n"
            "Covers alliances, vassalage, empire/union membership, trade pacts, "
            "patronage, non-aggression, marriage pacts, and world-defined types.\n\n"
            "DISTINCT from define_faction.relationships scalar trust value: this is "
            "a discrete, named BINDING agreement with terms, duration, and L2/L2.5 effects.\n\n"
            "An EMPIRE is a relationship with one 'suzerain'-role party + multiple "
            "'member'-role parties. autonomy_level controls profile blending across "
            "faction boundaries (reuses council_members weighted-aggregate)."
        ),
        "relationship_id": "UUID or display_name. Upsert.",
        "relationship_type": (
            '"alliance" — mutual defense, shared intel, min_trust_floor\n'
            '"vassalage" — suzerain+vassal: tribute, military obligation\n'
            '"empire_membership" — one suzerain, many members\n'
            '"union_membership" — founder + members\n'
            '"trade_pact" — goods, tariffs, infrastructure\n'
            '"patronage" — patron+client: subsidy_flows\n'
            '"non_aggression" — peace commitment\n'
            '"marriage_pact" — dynastic alliance\n'
            "World-defined open string accepted. REQUIRED."
        ),
        "name": 'Display name, e.g. "The Silver Concordat", "Empire of the Iron Throne".',
        "parties": (
            '[{"faction_id": str, "role": str}, ...].\n'
            'role: "equal" (bilateral) | "patron"|"client" (patronage) |\n'
            '      "suzerain"|"vassal"|"member" (vassalage/empire/union) |\n'
            "      world-defined for other relationship_types."
        ),
        "terms": (
            "Relationship_type-specific terms.\n\n"
            "=== alliance ===\n"
            '{"mutual_defense": bool, "shared_intel": bool,\n'
            ' "min_trust_floor": float}\n\n'
            "=== trade_pact ===\n"
            '{"goods": list[str], "tariff_modifier": float,\n'
            ' "infrastructure_cap_bonus": float}\n\n'
            "=== vassalage / empire_membership / union_membership ===\n"
            '{"tribute_rate": float,\n'
            ' "military_obligation": float,\n'
            ' "autonomy_level": float (0.0–1.0; 1.0=fully autonomous)}\n\n'
            "=== patronage ===\n"
            '{"subsidy_flows": [{"flow_id": str, "source": str,\n'
            '   "sink": str, "rate": float, ...}]}\n\n'
            "=== non_aggression ===\n"
            '{"min_trust_floor": float}'
        ),
        "trust_effect": "One-time trust delta to all party pairs on establishment. Range -1.0 to +1.0. Default 0.0.",
        "ongoing_trust_modifier": "Per-update-step trust drift while active. Positive = faster growth. Default 0.0.",
        "expires_condition": 'Expression; auto-dissolves when true. "" = no automatic expiry.',
        "revoke_condition": 'Expression; auto-dissolves. "L2_5.trust_matrix[a][b] < 0.1".',
        "dissolved": (
            "If True, dissolves this relationship. Effects removed. "
            "Does NOT retroactively undo trust_effect or accumulated tribute."
        ),
        "cause": "Narrative reason for creation, update, or dissolution. Written to event log.",
    },

    # ==================================================================
    "ready_to_proceed": {
        "_tool": (
            "**Finish this invocation.** The world continues existing.\n\n"
            "Call this when done. Triggers for the next call are ALREADY "
            "set via subscribe_to_events(). This tool only sets the "
            "fallback — a safety net.\n\n"
            "You MUST provide world_summary — what you did this invocation."
        ),
        "world_summary": (
            "REQUIRED. What you accomplished. Concise but specific.\n"
            'Example: "Registered 12 fauna species, defined 3 factions '
            '(Iron Throne, Free Kingdoms, Necromancers), subscribed to '
            'research_complete events with 365d fallback."\n'
            "Shown as [last_summary] in your next invocation."
        ),
        "fallback_interval_days": (
            "Safety net — if NO subscribed event fires within this many "
            "game days, orchestrator calls you with notice='fallback_tick'.\n"
            "Default 365. Min 1, max 10000."
        ),
    },

    # ==================================================================
    "answer_world_question": {
        "_tool": (
            "**Answer a direct question.** Finishes invocation without "
            "changing the world.\n\n"
            "Does NOT change triggers from the last ready_to_proceed. "
            "Use when GM/SM asks about lore, factions, magic, etc."
        ),
        "question": "REQUIRED. The question that was asked.",
        "answer": "REQUIRED. Your answer. Written to event log.",
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
