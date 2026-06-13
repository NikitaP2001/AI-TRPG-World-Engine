"""World Manager tool definitions — 15 tools + read_tool_doc.

Two termination tools:
  - world_setting_result — for create_world_setting()
  - ready_to_proceed    — for ReAct-based call_wm()

Fifteen world-building tools. Docstrings are minimal — use
read_tool_doc(tool_name, parameters) for detailed parameter docs.

All tools persist to game/wm_state/ sidecar JSON files.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from openrouter_langchain_logging import logs_enabled


# ======================================================================
# Sidecar storage helpers
# ======================================================================

_WM_STORAGE = Path("game") / "wm_state"


def _ensure_storage() -> None:
    _WM_STORAGE.mkdir(parents=True, exist_ok=True)


def _write_json(name: str, data: Any) -> None:
    _ensure_storage()
    (_WM_STORAGE / name).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _read_json(name: str, default: Any = None) -> Any:
    p = _WM_STORAGE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# World generation status helper
# ======================================================================

_WORLD_GEN_STATUS_KEY = "_world_gen_status"


def _get_gen_status() -> str:
    return str(_read_json(_WORLD_GEN_STATUS_KEY, "authoring") or "authoring")


def _set_gen_status(status: str) -> None:
    _write_json(_WORLD_GEN_STATUS_KEY, status)


# ======================================================================
# Termination tools
# ======================================================================


@tool
def world_setting_result(setting_json: str) -> str:
    """Submit the world setting block as a JSON string.

    Termination tool — ends the current invocation.
    Use read_tool_doc('world_setting_result') for details.

    Args:
        setting_json: JSON with world_essence, gurps_calibration, initial_world_time.
    """
    return "ok"


@tool
def ready_to_proceed(
    world_summary: str = "",
    fallback_interval_days: int = 365,
) -> str:
    """Finish this invocation. The world continues existing.

    Triggers for the next call are ALREADY set via subscribe_to_events().
    This tool only:
      - logs what WM did (world_summary → event_summary for next context)
      - sets a fallback — a guarantee WM won't be lost forever

    Args:
        world_summary: What WM did this invocation. Written to event log
            and passed as [last_summary] in the next invocation's intro.
            Be concise but specific: "Registered 12 fauna species, defined
            3 factions, subscribed to research_complete events."
        fallback_interval_days: If NO subscribed event fires within this
            many game days, the orchestrator will call WM with
            notice="fallback_tick". Min 1, max 10000. Default 365.
    """
    return "ok"


@tool
def answer_world_question(
    question: str,
    answer: str,
) -> str:
    """Answer a direct question. Finishes invocation without changing the world.

    Does NOT change triggers set by the last ready_to_proceed.
    Use when GM/SM asks: "how does magic work?" "what are the factions?"

    Args:
        question: The question that was asked.
        answer: Your answer. Written to event log for future context.
    """
    return "ok"


# ======================================================================
# 1. set_world_orientation
# ======================================================================


@tool
def set_world_orientation(
    planet_radius: float,
    reference_meridian: float = 0.0,
    axial_tilt: float = 23.5,
    global_temperature_offset: float = 0.0,
    global_precipitation_modifier: float = 1.0,
    solar_intensity: float = 1.0,
    atmospheric_density: float = 1.0,
    ocean_temperature: float = 15.0,
    tectonic_activity: float = 0.5,
    ambient_rare_materials: Optional[List[Dict[str, Any]]] = None,
    long_cycle_tick_interval: int = 1000,
    climate_drift_rate: float = 0.0,
    direction_names: Optional[Dict[str, str]] = None,
    world_name: str = "",
    tech_level_default: int = 4,
) -> Dict[str, Any]:
    """One-time world init. Enters authoring mode. Call BEFORE any other tool.
    Use read_tool_doc('set_world_orientation') for full param docs.

    Args:
        planet_radius: Radius in any unit. All distances use this.
        axial_tilt: Degrees. 0=no seasons, 23.5=Earth-like.
        global_temperature_offset: Degrees C added to every cell.
        global_precipitation_modifier: Multiplier. >1=wetter.
        solar_intensity: Multiplier. <1=dim, >1=bright.
        atmospheric_density: Temp buffering. Low=wild swings.
        ocean_temperature: Base ocean surface temp in C.
        tectonic_activity: 0-1. Geological complexity.
        ambient_rare_materials: Diffuse ambient materials. Most worlds: [].
        tech_level_default: GURPS TL. TL4=medieval. Default 4.
    """
    data = {
        "planet_radius": planet_radius,
        "reference_meridian": reference_meridian,
        "axial_tilt": axial_tilt,
        "global_temperature_offset": global_temperature_offset,
        "global_precipitation_modifier": global_precipitation_modifier,
        "solar_intensity": solar_intensity,
        "atmospheric_density": atmospheric_density,
        "ocean_temperature": ocean_temperature,
        "tectonic_activity": tectonic_activity,
        "ambient_rare_materials": ambient_rare_materials or [],
        "long_cycle_tick_interval": long_cycle_tick_interval,
        "climate_drift_rate": climate_drift_rate,
        "direction_names": direction_names or {},
        "world_name": world_name,
        "tech_level_default": tech_level_default,
        "set_at": _utc_now(),
    }
    _write_json("world_orientation.json", data)
    _set_gen_status("authoring")

    ambient_ids = [m.get("material_id", "") for m in (ambient_rare_materials or [])]

    if logs_enabled():
        print(f"[trace:WM] set_world_orientation: radius={planet_radius}, TL={tech_level_default}")

    return {
        "status": "ok",
        "cell_side_length": planet_radius / 60.0,
        "top_level_cell_count": 17400,
        "grid_initialized": True,
        "world_generation_status": "authoring",
        "ambient_rare_materials_registered": ambient_ids,
        "warnings": [],
    }


# ======================================================================
# 2. set_player_start
# ======================================================================


@tool
def set_player_start(
    location_absolute: Optional[Dict[str, float]] = None,
    location_region_hint: str = "",
    required_properties: Optional[Dict[str, Any]] = None,
    player_faction_id: str = "",
    cause: str = "",
) -> Dict[str, Any]:
    """Establish the coordinate origin for the campaign start.
    One-time, authoring phase. Use read_tool_doc('set_player_start') for details.

    Args:
        location_absolute: {"lat", "lon", "alt"}.
        location_region_hint: "northern coast" etc.
        required_properties: {"biome_category", "is_coastal"}.
        player_faction_id: Players' home faction.
        cause: Narrative significance.
    """
    data = {
        "location_absolute": location_absolute or {},
        "location_region_hint": location_region_hint,
        "required_properties": required_properties or {},
        "player_faction_id": player_faction_id,
        "cause": cause,
        "set_at": _utc_now(),
    }
    _write_json("player_start.json", data)

    if logs_enabled():
        hint = location_region_hint or str(location_absolute or "?")
        print(f"[trace:WM] set_player_start: {hint}")

    return {
        "origin_established": True,
        "world_generation_status": _get_gen_status(),
        "resolved_location": None,
        "player_faction_id": player_faction_id or None,
        "validation_errors": [],
        "warnings": [],
    }


# ======================================================================
# 3. finalize_world_generation
# ======================================================================


def _compute_h3_ids_for_constraints(params_dict: dict) -> list:
    """Pre-compute H3 cell IDs for constraint resolution (internal helper)."""
    import math, random
    import h3 as _h3
    h3_res = int(params_dict.get("_h3_resolution", 2))
    seed = int(params_dict.get("seed", 42))
    _rng = random.Random(seed)
    all_ids = []
    res0 = list(_h3.get_res0_cells())
    for r0 in res0:
        all_ids.extend(_h3.cell_to_children(r0, h3_res))
    _rng.shuffle(all_ids)
    return all_ids


@tool
def finalize_world_generation(
    world_seed: int = 0,
    constraint_priority: str = "strict",
    review_only: bool = False,
) -> Dict[str, Any]:
    """Trigger world generation from declared constraints.
    One-time, cannot undo. Use read_tool_doc('finalize_world_generation').

    Orchestrates: load WM constraints → resolve spatial → generate_world()
    → save to SQLite → seed fauna → run water balance.

    Args:
        world_seed: Random seed. 0=random (uses current time).
        constraint_priority: "strict"|"best_effort".
        review_only: If True, validate only — no generation.
    """
    status_before = _get_gen_status()
    import random as _random
    seed = world_seed if world_seed != 0 else _random.randint(0, 2**31 - 1)

    # ==================================================================
    # Phase 1: Load and validate constraints
    # ==================================================================
    from simulation.wm_constraint_reader import (
        load_constraints, resolve_all_spatial,
    )
    wmc = load_constraints()

    if review_only:
        # Review: validate constraints without generating
        warnings = []
        unsatisfiable = []
        if not wmc.orientation:
            warnings.append("No set_world_orientation called — using defaults")
        for feat in wmc.features:
            anchor = (feat.get("anchor_strength") or "").strip()
            if anchor == "fixed":
                loc = feat.get("location_absolute") or {}
                outline = feat.get("outline_vertices") or []
                if not loc and not outline:
                    unsatisfiable.append(
                        f"Feature '{feat.get('name', feat.get('feature_id', '?'))}' "
                        f"has anchor_strength=fixed but no coordinates"
                    )
            # Check for river without outline
            if (feat.get("feature_type") or "").strip().lower() in ("river", "water_body"):
                props = feat.get("properties") or {}
                wtype = (props.get("type") or "").strip().lower()
                is_river = (feat.get("feature_type") or "").strip().lower() == "river" or wtype == "river"
                if is_river:
                    outline = feat.get("outline_vertices") or []
                    if len(outline) < 2:
                        unsatisfiable.append(
                            f"River '{feat.get('name', feat.get('feature_id', '?'))}' "
                            f"has <2 outline_vertices — cannot carve valley"
                        )

        # Check for unknown feature types
        if wmc.unknown_feature_types:
            for uft in wmc.unknown_feature_types:
                warnings.append(
                    f"Unknown feature_type '{uft}'. "
                    f"Recognised types: continent, elevation_feature, water_body, river, "
                    f"terrain_cover, climate_zone, settlement, ruin, and others. "
                    f"Use read_tool_doc('alter_feature') for the full list."
                )

        if logs_enabled():
            print(f"[trace:WM] finalize_world_generation REVIEW (seed={seed})")
        return {
            "world_generation_status": status_before,
            "world_seed": seed,
            "constraint_summary": {
                "continent_outlines": len(wmc.continent_constraints),
                "named_terrain_features": len(wmc.features),
                "faction_territories": len(wmc.factions),
                "canon_constraints": len(wmc.canon_constraints),
            },
            "resolved_player_start": None,
            "unsatisfiable_constraints": unsatisfiable,
            "generated_features": 0,
            "generated_factions": 0,
            "validation_errors": [],
            "warnings": warnings + ["Review mode: no generation executed"],
        }

    # ==================================================================
    # Phase 2: Build generation params from orientation
    # ==================================================================
    from simulation.layer0.cell_model import GenerationParams
    o = wmc.orientation
    params = GenerationParams(
        planet_radius=float(o.get("planet_radius", 1.0)),
        axial_tilt=float(o.get("axial_tilt", 23.5)),
        tectonic_activity=float(o.get("tectonic_activity", 0.5)),
        seed=seed,
    )
    params.derive()

    # ==================================================================
    # Phase 3: Resolve spatial constraints to H3 cells
    # ==================================================================
    all_ids = _compute_h3_ids_for_constraints({
        "_h3_resolution": params.h3_resolution,
        "seed": seed,
    })
    resolve_all_spatial(wmc, all_ids)
    wm_c = wmc.to_generator_constraints()

    if logs_enabled():
        n_features = len(wmc.features)
        n_fauna = len(wmc.fauna_species)
        n_flora = len(wmc.flora_pft)
        n_continents = len(wmc.continent_constraints)
        n_rivers = len(wmc.river_constraints)
        print(f"[trace:WM] finalize: {n_features} features, {n_fauna} fauna, "
              f"{n_flora} flora, {n_continents} continents, {n_rivers} rivers")

    # ==================================================================
    # Phase 4: Run Layer 0 generation
    # ==================================================================
    print("[Generator] Starting world generation with WM constraints...")
    from simulation.generator import generate_world
    try:
        cells, feature_store, flow_acc = generate_world(
            params=params,
            wm_constraints=wm_c,
        )
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[Generator] FAILED: {e}")
        print(error_detail)
        _set_gen_status("authoring")
        return {
            "world_generation_status": "authoring",
            "world_seed": seed,
            "constraint_summary": {
                "continent_outlines": len(wmc.continent_constraints),
                "named_terrain_features": len(wmc.features),
                "faction_territories": len(wmc.factions),
                "canon_constraints": len(wmc.canon_constraints),
            },
            "resolved_player_start": None,
            "unsatisfiable_constraints": [],
            "generated_features": 0,
            "generated_factions": 0,
            "validation_errors": [str(e)],
            "warnings": ["Generation failed — see error above"],
        }

    if not cells:
        _set_gen_status("authoring")
        return {
            "world_generation_status": "authoring",
            "world_seed": seed,
            "constraint_summary": {},
            "resolved_player_start": None,
            "unsatisfiable_constraints": [],
            "generated_features": 0,
            "generated_factions": 0,
            "validation_errors": ["Generator returned no cells"],
            "warnings": [],
        }

    print(f"[Generator] {len(cells)} cells, {feature_store.count} features generated")

    # ==================================================================
    # Phase 5: Save to SQLite
    # ==================================================================
    from simulation.world_db import WorldDB
    db_path = "game/simulation/world.sqlite"
    db = WorldDB(db_path)

    # New: save as WorldState (continuous fields + features)
    from simulation.world_state_db import save_generated_world
    init_year = 1000
    save_generated_world(
        db, cells, feature_store, params.__dict__,
        time={"tick": 0, "year": init_year, "day_of_year": 0.0, "hour": 6.0},
    )
    # Legacy: keep cells table for GUI compat
    db.save_cells(cells)
    db.save_features(feature_store)
    db.set_params(**params.__dict__)
    db.init_time(tick=0, year=init_year, day_of_year=0.0, hour=6.0)

    print(f"[DB] Saved to {db_path}")

    # ==================================================================
    # Phase 6: Seed initial fauna
    # ==================================================================
    from simulation.layer1.initial_seeding import seed_initial_fauna
    seeded = seed_initial_fauna(db, cells, wmc.fauna_species)
    if seeded:
        print(f"[Fauna] Seeded {seeded} (species, cell) pairs")
    else:
        print("[Fauna] No fauna seeded (no species registered)")

    # ==================================================================
    # Phase 7: Run water balance (initial L1 equilibrium)
    # ==================================================================
    from simulation.time_engine import TimeEngine
    engine = TimeEngine(db)
    summary = engine.advance(days=500)
    print(f"[TimeEngine] Advanced {summary['tick']} ticks to Y{summary['year']}")

    # ==================================================================
    # Phase 8: Finalize
    # ==================================================================
    _set_gen_status("generated")
    data = {
        "world_seed": seed,
        "constraint_priority": constraint_priority,
        "generated_at": _utc_now(),
        "cell_count": len(cells),
        "features_count": feature_store.count,
    }
    _write_json("generation_complete.json", data)

    if logs_enabled():
        print(f"[trace:WM] finalize_world_generation: seed={seed}, {len(cells)} cells generated")

    # Compute constraint summary
    cont_count = len(wmc.continent_constraints)
    feat_count = len(wmc.features)
    faction_count = len(wmc.factions)
    canon_count = len(wmc.canon_constraints)

    # Build warnings (unknown feature types from constraint reader)
    warnings_list = []
    if wmc.unknown_feature_types:
        for uft in wmc.unknown_feature_types:
            warnings_list.append(
                f"Unknown feature_type '{uft}'. "
                f"Recognised types: continent, elevation_feature, water_body, river, "
                f"terrain_cover, climate_zone, settlement, ruin, and others. "
                f"Use read_tool_doc('alter_feature') for the full list."
            )

    return {
        "world_generation_status": "generated",
        "world_seed": seed,
        "constraint_summary": {
            "continent_outlines": cont_count,
            "named_terrain_features": feat_count,
            "faction_territories": faction_count,
            "canon_constraints": canon_count,
            "myths_seeded": 0,
        },
        "resolved_player_start": wmc.player_start.get("location_absolute") or None,
        "unsatisfiable_constraints": [],
        "generated_features": feature_store.count,
        "generated_factions": faction_count,
        "validation_errors": [],
        "warnings": warnings_list,
    }


# ======================================================================
# 4. define_world_concept
# ======================================================================


@tool
def define_world_concept(
    concept_type: str,
    concept_id: str,
    name: str = "",
    description: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    update_existing: bool = True,
) -> Dict[str, Any]:
    """Register any world-specific type. Single entry point for vocabulary.
    Use read_tool_doc('define_world_concept') for full param schemas by type.

    Args:
        concept_type: existence_type|stratum|norm|mineral|ore_type|flora_pft|
            fauna_species|knowledge_domain|research_project|capability|myth|
            item|canon_constraint|settlement_type|etc.
        concept_id: Unique identifier.
        parameters: Type-specific dict. See tool_docs for schemas.
        update_existing: True=upsert, False=strict create.
    """
    concepts = _read_json("world_concepts.json", [])
    display_name = name or concept_id

    existing_idx = None
    for i, c in enumerate(concepts):
        if c.get("concept_id") == concept_id:
            existing_idx = i
            break

    created = existing_idx is None
    if not created and not update_existing:
        return {
            "concept_id": concept_id,
            "concept_type": concept_type,
            "created": False,
            "validation_errors": [f"Concept '{concept_id}' already exists and update_existing=False"],
            "warnings": [],
        }

    entry = {
        "concept_id": concept_id,
        "concept_type": concept_type,
        "name": display_name,
        "description": description,
        "parameters": parameters or {},
        "created_at": _utc_now(),
    }

    if existing_idx is not None:
        concepts[existing_idx] = entry
    else:
        concepts.append(entry)

    _write_json("world_concepts.json", concepts)

    if logs_enabled():
        print(f"[trace:WM] define_world_concept: [{concept_type}] {concept_id}")

    return {
        "concept_id": concept_id,
        "concept_type": concept_type,
        "created": created,
        "validation_errors": [],
        "warnings": [],
    }


# ======================================================================
# 5. alter_feature
# ======================================================================


@tool
def alter_feature(
    feature_id: str = "",
    name: str = "",
    feature_type: str = "",
    location_absolute: Optional[Dict[str, float]] = None,
    location_relative: Optional[Dict[str, Any]] = None,
    location_inside: str = "",
    location_near: Optional[List[str]] = None,
    location_region_hint: str = "",
    size_preset: str = "",
    size_radius: float = 0.0,
    size_width: float = 0.0,
    size_length: float = 0.0,
    size_height: float = 0.0,
    size_depth: float = 0.0,
    size_shape: str = "",
    outline_vertices: Optional[List[Dict[str, float]]] = None,
    properties: Optional[Dict[str, Any]] = None,
    layer_effects: Optional[Dict[str, Any]] = None,
    contains: Optional[Dict[str, Any]] = None,
    discovery_required: bool = False,
    physics_override: Optional[Dict[str, Any]] = None,
    dissolved: bool = False,
    dissolve_scheduled_tick: int = 0,
    dissolve_gradual_over_ticks: int = 0,
    part_of: str = "",
    connected_to: Optional[List[str]] = None,
    feeds_into: str = "",
    anchor_strength: str = "preferred",
    cause: str = "",
) -> Dict[str, Any]:
    """Create/update/dissolve a geographic feature. Authoring=constraint, Live=real.
    Use read_tool_doc('alter_feature') for full param docs.

    Args:
        feature_id: UUID. Empty=create.
        feature_type: continent|elevation|water_body|river|settlement|ruin|etc.
        location_absolute/relative/inside/near/hint: One location method.
        size_preset: tiny|small|medium|large|massive.
        properties: navigable, passable, elevation_override, etc.
        layer_effects: L0 cell override modifiers.
        contains: Latent items/entities/myths.
        physics_override: Non-standard physics region.
        dissolved: Dissolve this feature.
        cause: "volcanic activity", "magical corruption".
    """
    gen_status = _get_gen_status()
    operation = "constraint_registered" if gen_status == "authoring" else ("dissolved" if dissolved else ("created" if not feature_id else "updated"))

    features = _read_json("features.json", [])
    entry = {
        "feature_id": feature_id or f"feat_{len(features) + 1}",
        "name": name,
        "feature_type": feature_type,
        "location_absolute": location_absolute or {},
        "location_relative": location_relative or {},
        "location_inside": location_inside,
        "location_near": location_near or [],
        "location_region_hint": location_region_hint,
        "size_preset": size_preset,
        "size_radius": size_radius,
        "size_width": size_width,
        "size_length": size_length,
        "size_height": size_height,
        "size_depth": size_depth,
        "size_shape": size_shape,
        "outline_vertices": outline_vertices or [],
        "properties": properties or {},
        "layer_effects": layer_effects or {},
        "contains": contains or {},
        "discovery_required": discovery_required,
        "physics_override": physics_override or {},
        "dissolved": dissolved,
        "dissolve_scheduled_tick": dissolve_scheduled_tick,
        "dissolve_gradual_over_ticks": dissolve_gradual_over_ticks,
        "part_of": part_of,
        "connected_to": connected_to or [],
        "feeds_into": feeds_into,
        "anchor_strength": anchor_strength,
        "cause": cause,
        "generation_status": gen_status,
        "operation": operation,
        "updated_at": _utc_now(),
    }

    updated = False
    for i, f in enumerate(features):
        if f.get("feature_id") == entry["feature_id"] and not dissolved:
            features[i] = entry
            updated = True
            break
    if not updated and not dissolved:
        features.append(entry)
    elif dissolved:
        features = [f for f in features if f.get("feature_id") != entry["feature_id"]]

    _write_json("features.json", features)

    if logs_enabled():
        print(f"[trace:WM] alter_feature: {operation} {feature_type} '{name or feature_id}'")

    return {
        "feature_id": entry["feature_id"],
        "name": name,
        "operation": operation,
        "geometry_type": feature_type,
        "center": {"lat": 0.0, "lon": 0.0},
        "bounding_box": {},
        "cells_affected": 0,
        "layer_effects_applied": {"cells_modified": 0, "fields_written": []},
        "relationships": {
            "contained_by": part_of or None,
            "contains": [],
            "adjacent_to": connected_to or [],
        },
        "event_log_id": None if gen_status == "authoring" else f"evt_{_utc_now()}",
        "warnings": [],
    }


# ======================================================================
# 6. define_entity
# ======================================================================


@tool
def define_entity(
    entity_id: str = "",
    display_name: str = "",
    archetype_id: str = "",
    existence_type: str = "mortal",
    tier: int = 1,
    scale: str = "individual",
    scale_count: float = 1.0,
    faction_id: str = "",
    military_unit_of: str = "",
    narrative_importance: str = "background",
    gurps_sheet: Optional[Dict[str, Any]] = None,
    stocks: Optional[List[Dict[str, Any]]] = None,
    capabilities_unlocked: Optional[List[str]] = None,
    inventory: Optional[List[str]] = None,
    behavior_mode: str = "STATIONARY",
    behavior_parameters: Optional[Dict[str, Any]] = None,
    hfsm_states: Optional[List[Dict[str, Any]]] = None,
    auras: Optional[List[Dict[str, Any]]] = None,
    rules: Optional[List[Dict[str, Any]]] = None,
    immunities: Optional[List[Dict[str, Any]]] = None,
    pre_engagement_effects: Optional[List[Dict[str, Any]]] = None,
    termination_condition: str = "",
    leadership_profile: Optional[Dict[str, Any]] = None,
    authority_overrides: Optional[List[Dict[str, Any]]] = None,
    continuity_depth: str = "none",
    stub_lock_on_conflict: bool = False,
    wm_notify_on_conflict: bool = False,
    query_summary_template: str = "",
    location_absolute: Optional[Dict[str, float]] = None,
    location_relative: Optional[Dict[str, Any]] = None,
    location_inside: str = "",
    location_near: str = "",
    cause: str = "",
) -> Dict[str, Any]:
    """Create/update any named entity (L3). Use read_tool_doc('define_entity').
    GURPS sheet supported — see tool_docs for mapping.

    Args:
        entity_id: UUID. Empty=create.
        existence_type: Registered concept_id. "mortal" built-in.
        tier: 1=need-driven, 2=goal-driven, 3=HFSM.
        scale: individual|party|unit|swarm.
        gurps_sheet: {"st", "dx", "iq", "ht", "advantages", ...}.
        leadership_profile: Objective weights for autonomous faction mgmt.
        authority_overrides: Which factions entity leads.
        behavior_mode: STATIONARY|PATROL|PATH_TO_GOAL|etc.
        immunities: Damage/attrition/need_termination immunities.
        pre_engagement_effects: Effects before L2 combat.
    """
    entities = _read_json("entities.json", [])
    eid = entity_id or f"ent_{len(entities) + 1}_{_utc_now()}"
    operation = "created" if not entity_id else "updated"

    entry = {
        "entity_id": eid,
        "display_name": display_name or eid,
        "archetype_id": archetype_id,
        "existence_type": existence_type,
        "tier": tier,
        "scale": scale,
        "scale_count": scale_count,
        "faction_id": faction_id,
        "military_unit_of": military_unit_of,
        "narrative_importance": narrative_importance,
        "gurps_sheet": gurps_sheet or {},
        "stocks": stocks or [],
        "capabilities_unlocked": capabilities_unlocked or [],
        "inventory": inventory or [],
        "behavior_mode": behavior_mode,
        "behavior_parameters": behavior_parameters or {},
        "hfsm_states": hfsm_states or [],
        "auras": auras or [],
        "rules": rules or [],
        "immunities": immunities or [],
        "pre_engagement_effects": pre_engagement_effects or [],
        "termination_condition": termination_condition,
        "leadership_profile": leadership_profile or {},
        "authority_overrides": authority_overrides or [],
        "continuity_depth": continuity_depth,
        "stub_lock_on_conflict": stub_lock_on_conflict,
        "wm_notify_on_conflict": wm_notify_on_conflict,
        "query_summary_template": query_summary_template,
        "location_absolute": location_absolute or {},
        "location_relative": location_relative or {},
        "location_inside": location_inside,
        "location_near": location_near,
        "cause": cause,
        "operation": operation,
        "updated_at": _utc_now(),
    }

    for i, e in enumerate(entities):
        if e.get("entity_id") == eid:
            entities[i] = entry
            break
    else:
        entities.append(entry)

    _write_json("entities.json", entities)

    if logs_enabled():
        print(f"[trace:WM] define_entity: {operation} '{display_name or eid}' ({existence_type}, T{tier})")

    return {
        "entity_id": eid,
        "display_name": display_name or eid,
        "operation": operation,
        "tier": tier,
        "existence_type": existence_type,
        "stocks_registered": [s.get("stock_id", "") for s in (stocks or [])],
        "capabilities_unlocked": capabilities_unlocked or [],
        "rules_registered": len(rules or []),
        "auras_registered": len(auras or []),
        "immunities_registered": [i.get("immunity_type", "") for i in (immunities or [])],
        "authority_overrides_registered": [a.get("faction_id", "") for a in (authority_overrides or [])],
        "leadership_profile_compiled": bool(leadership_profile),
        "gurps_derivations": {},
        "validation_errors": [],
        "warnings": [],
    }


# ======================================================================
# 7. define_faction
# ======================================================================


@tool
def define_faction(
    faction_id: str = "",
    name: str = "",
    faction_type: str = "",
    template_id: str = "",
    social_complexity: float = 1.0,
    complexity_threshold: float = 0.0,
    settlements: Optional[List[Dict[str, Any]]] = None,
    territory_expansion_rules: Optional[List[Dict[str, Any]]] = None,
    stocks: Optional[List[Dict[str, Any]]] = None,
    flows: Optional[List[Dict[str, Any]]] = None,
    rules: Optional[List[Dict[str, Any]]] = None,
    ideology_vector: Optional[Dict[str, float]] = None,
    social_structure_type: str = "",
    strata: Optional[List[str]] = None,
    relationships: Optional[Dict[str, float]] = None,
    knowledge_stocks: Optional[Dict[str, float]] = None,
    capabilities_unlocked: Optional[List[str]] = None,
    institutions: Optional[List[Dict[str, Any]]] = None,
    active_research: Optional[List[Dict[str, Any]]] = None,
    tech_level: int = -1,
    underground: bool = False,
    council_members: Optional[List[Dict[str, Any]]] = None,
    decision_variance: float = 0.0,
    intent_declarations: Optional[List[Dict[str, Any]]] = None,
    dissolved: bool = False,
    absorb_into: str = "",
    dissolve_scheduled_tick: int = 0,
    founding_tick: int = 0,
    cause: str = "",
) -> Dict[str, Any]:
    """Create/update any faction or proto-faction.
    Use read_tool_doc('define_faction') for full docs on social_complexity,
    council_members, settlements, and leadership.

    Args:
        faction_id: UUID. Upsert.
        social_complexity: 0-1. 0=ecology, 0.3=proto, 0.6=emergent, 1.0=full.
        settlements: List of settlement objects (R18).
        council_members: Leadership council (R11).
        intent_declarations: Active directives.
        knowledge_stocks: Initial knowledge_domain values.
        institutions: Research institutions.
    """
    gen_status = _get_gen_status()
    operation = "constraint_registered" if gen_status == "authoring" else ("dissolved" if dissolved else ("created" if not faction_id else "updated"))

    factions = _read_json("factions.json", [])
    fid = faction_id or f"fac_{len(factions) + 1}"

    entry = {
        "faction_id": fid,
        "name": name or fid,
        "faction_type": faction_type,
        "template_id": template_id,
        "social_complexity": social_complexity,
        "complexity_threshold": complexity_threshold,
        "settlements": settlements or [],
        "territory_expansion_rules": territory_expansion_rules or [],
        "stocks": stocks or [],
        "flows": flows or [],
        "rules": rules or [],
        "ideology_vector": ideology_vector or {},
        "social_structure_type": social_structure_type,
        "strata": strata or [],
        "relationships": relationships or {},
        "knowledge_stocks": knowledge_stocks or {},
        "capabilities_unlocked": capabilities_unlocked or [],
        "institutions": institutions or [],
        "active_research": active_research or [],
        "tech_level": tech_level,
        "underground": underground,
        "council_members": council_members or [],
        "decision_variance": decision_variance,
        "intent_declarations": intent_declarations or [],
        "dissolved": dissolved,
        "absorb_into": absorb_into,
        "dissolve_scheduled_tick": dissolve_scheduled_tick,
        "founding_tick": founding_tick,
        "cause": cause,
        "generation_status": gen_status,
        "operation": operation,
        "updated_at": _utc_now(),
    }

    for i, f in enumerate(factions):
        if f.get("faction_id") == fid and not dissolved:
            factions[i] = entry
            break
    else:
        if not dissolved:
            factions.append(entry)
        elif dissolved:
            factions = [f for f in factions if f.get("faction_id") != fid]

    _write_json("factions.json", factions)

    layers = []
    if social_complexity >= 0.01:
        layers.append("L1")
    if social_complexity >= 0.3:
        layers.append("L2_proto")
    if social_complexity >= 0.6:
        layers.append("L2")
    if social_complexity >= 0.8:
        layers.append("L2.5")

    if logs_enabled():
        print(f"[trace:WM] define_faction: {operation} '{name or fid}' (complexity={social_complexity})")

    return {
        "faction_id": fid,
        "name": name or fid,
        "operation": operation,
        "social_complexity": social_complexity,
        "layers_active": layers,
        "settlements_registered": [s.get("settlement_id", "") for s in (settlements or [])],
        "territory_cells": None if gen_status == "authoring" else [],
        "knowledge_domains_initialized": list((knowledge_stocks or {}).keys()),
        "capabilities_unlocked": capabilities_unlocked or [],
        "institutions_registered": len(institutions or []),
        "stocks_registered": [s.get("stock_id", "") for s in (stocks or [])],
        "flows_registered": len(flows or []),
        "rules_registered": len(rules or []),
        "council_members_registered": len(council_members or []),
        "intent_declarations_active": len(intent_declarations or []),
        "effective_profile_compiled": bool(council_members),
        "validation_errors": [],
        "warnings": [],
    }


# ======================================================================
# 8. define_rule
# ======================================================================


@tool
def define_rule(
    rule_id: str,
    name: str = "",
    description: str = "",
    condition: str = "",
    effects: Optional[List[Dict[str, Any]]] = None,
    scope: str = "global",
    priority: int = 5,
    cooldown_ticks: int = 0,
    enabled: bool = True,
    narrative_flag: bool = False,
    myth_seeds: Optional[List[Dict[str, Any]]] = None,
    firing_limit: int = 0,
    cause: str = "",
) -> Dict[str, Any]:
    """Create/update a global event rule. Use read_tool_doc('define_rule').

    Args:
        rule_id: Unique. Upsert. REQUIRED.
        condition: Boolean expression over world state.
        effects: World-state deltas (modify_field, spawn_entity, etc.).
        scope: global|per_cell|per_entity|per_faction|per_settlement.
        priority: 1-10. Higher = first.
        myth_seeds: Myths planted at firing location.
        firing_limit: 0=unlimited, >0=auto-disable after N.
    """
    rules = _read_json("global_rules.json", [])
    operation = "created"

    entry = {
        "rule_id": rule_id,
        "name": name or rule_id,
        "description": description,
        "condition": condition,
        "effects": effects or [],
        "scope": scope,
        "priority": priority,
        "cooldown_ticks": cooldown_ticks,
        "enabled": enabled,
        "narrative_flag": narrative_flag,
        "myth_seeds": myth_seeds or [],
        "firing_limit": firing_limit,
        "cause": cause,
        "updated_at": _utc_now(),
    }

    for i, r in enumerate(rules):
        if r.get("rule_id") == rule_id:
            rules[i] = entry
            operation = "updated"
            break
    else:
        rules.append(entry)

    _write_json("global_rules.json", rules)

    if logs_enabled():
        print(f"[trace:WM] define_rule: {operation} '{rule_id}' ({scope})")

    return {
        "rule_id": rule_id,
        "operation": operation,
        "scope": scope,
        "enabled": enabled,
        "validation_errors": [] if condition else ["condition is empty"],
    }


# ======================================================================
# 9. declare_world_state
# ======================================================================


@tool
def declare_world_state(
    era_name: str = "",
    era_parameters: Optional[Dict[str, Any]] = None,
    facts: Optional[List[Dict[str, Any]]] = None,
    historical_events: Optional[List[Dict[str, Any]]] = None,
    prophecies: Optional[List[Dict[str, Any]]] = None,
    age_transitions: Optional[List[Dict[str, Any]]] = None,
    deferred_tasks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Declare narrative facts, era, prophecies, history.
    Use read_tool_doc('declare_world_state') for param schemas.

    Args:
        era_name: Current era. Updates active era.
        facts: World fact stubs.
        historical_events: Past events.
        prophecies: Conditions tracked by WM.
        deferred_tasks: Future build-out tasks.
    """
    states = _read_json("world_states.json", [])

    entry = {
        "era_name": era_name,
        "era_parameters": era_parameters or {},
        "facts": facts or [],
        "historical_events": historical_events or [],
        "prophecies": prophecies or [],
        "age_transitions": age_transitions or [],
        "deferred_tasks": deferred_tasks or [],
        "declared_at": _utc_now(),
    }
    states.append(entry)
    _write_json("world_states.json", states)

    if logs_enabled():
        print(f"[trace:WM] declare_world_state: era='{era_name}', facts={len(facts or [])}, events={len(historical_events or [])}")

    return {
        "era_updated": bool(era_name),
        "facts_committed": [f.get("fact_type", "") for f in (facts or [])],
        "historical_events_written": len(historical_events or []),
        "prophecies_registered": [p.get("prophecy_id", "") for p in (prophecies or [])],
        "tasks_scheduled": [t.get("task_type", "") for t in (deferred_tasks or [])],
        "stub_locked_facts": [],
        "warnings": [],
    }


# ======================================================================
# 10. subscribe_to_events
# ======================================================================


@tool
def subscribe_to_events(
    filters: List[Dict[str, Any]],
    replace_existing: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Register WM interest in future events. Sole forward-looking mechanism (R13).
    Use read_tool_doc('subscribe_to_events') for filter schema.

    Args:
        filters: List of filter dicts with event_types, timing (before|after),
            event_payload_filters, field_thresholds, etc.
        replace_existing: filter_ids to remove before adding.
    """
    subs = _read_json("subscriptions.json", [])
    removed = []

    if replace_existing:
        subs = [s for s in subs if s.get("filter_id") not in replace_existing]
        removed = replace_existing

    validation_errors = []
    active_ids = []
    for f in filters:
        fid = f.get("filter_id", f"sub_{len(subs) + 1}")
        if not f.get("event_types") and not f.get("entity_ids") and not f.get("faction_ids") and not f.get("location") and not f.get("field_thresholds") and not f.get("event_payload_filters"):
            validation_errors.append(f"Filter '{fid}' is too broad — must have at least one scoping field")
            continue
        entry = {
            "filter_id": fid,
            **{k: v for k, v in f.items() if k != "filter_id"},
            "subscribed_at": _utc_now(),
        }
        subs.append(entry)
        active_ids.append(fid)

    _write_json("subscriptions.json", subs)

    if logs_enabled():
        print(f"[trace:WM] subscribe_to_events: {len(active_ids)} filters active")

    return {
        "active_filters": active_ids,
        "removed_filters": removed,
        "validation_errors": validation_errors,
        "warnings": [],
    }


# ======================================================================
# 11. query_world_state
# ======================================================================


@tool
def query_world_state(
    query_type: str,
    target_id: str = "",
    region_center: Optional[Dict[str, Any]] = None,
    region_radius: float = 0.0,
    time_range: Optional[Dict[str, int]] = None,
    filters: Optional[Dict[str, Any]] = None,
    fields: Optional[List[str]] = None,
    include_wm_detail: bool = True,
) -> Dict[str, Any]:
    """Read simulation state, registry, rosters. PRIMARY diagnostic tool.
    Use read_tool_doc('query_world_state') for all query_type options and filters.

    Args:
        query_type: registry|variables|world_debt|notifications|region|
            entity|entities|faction|relationships|settlements|events|
            social_context|time|feature.
        target_id: For entity/faction/feature queries.
        filters: Type-specific filters (faction_id, tier, etc.).
        fields: Subset of fields to return.
    """
    result = None
    warnings = []

    if query_type == "registry":
        concepts = _read_json("world_concepts.json", [])
        if filters and filters.get("concept_type"):
            concepts = [c for c in concepts if c.get("concept_type") == filters["concept_type"]]
        result = concepts

    elif query_type == "variables":
        result = {
            "available_layers": ["L0", "L1", "L2", "L2.5", "L3"],
            "note": "Variable registry stubbed — full field paths available after generation",
        }

    elif query_type == "world_debt":
        result = {"task_queue_depth": 0, "overdue_tasks": [], "stub_locked_facts": []}

    elif query_type == "notifications":
        result = _read_json("notifications.json", [])

    elif query_type == "region":
        features = _read_json("features.json", [])
        result = features

    elif query_type == "entity":
        entities = _read_json("entities.json", [])
        for e in entities:
            if e.get("entity_id") == target_id or e.get("display_name") == target_id:
                result = e
                break
        if result is None:
            result = {"error": f"Entity '{target_id}' not found"}

    elif query_type == "entities":
        entities = _read_json("entities.json", [])
        f = filters or {}
        if f.get("faction_id"):
            entities = [e for e in entities if e.get("faction_id") == f["faction_id"]]
        if f.get("tier"):
            entities = [e for e in entities if e.get("tier") == f["tier"]]
        if f.get("behavior_mode"):
            entities = [e for e in entities if e.get("behavior_mode") == f["behavior_mode"]]
        if fields:
            result = [{k: e.get(k) for k in fields if k in e} for e in entities]
        else:
            result = [{"entity_id": e["entity_id"], "display_name": e.get("display_name", ""), "tier": e.get("tier"), "behavior_mode": e.get("behavior_mode")} for e in entities]

    elif query_type == "faction":
        factions = _read_json("factions.json", [])
        for f in factions:
            if f.get("faction_id") == target_id or f.get("name") == target_id:
                result = f
                break
        if result is None:
            result = {"error": f"Faction '{target_id}' not found"}

    elif query_type == "relationships":
        rels = _read_json("relationships.json", [])
        f = filters or {}
        if f.get("relationship_type"):
            rels = [r for r in rels if r.get("relationship_type") == f["relationship_type"]]
        if f.get("faction_id"):
            rels = [r for r in rels if f["faction_id"] in r.get("parties", [])]
        result = rels

    elif query_type == "settlements":
        factions = _read_json("factions.json", [])
        all_settlements = []
        for fac in factions:
            for s in (fac.get("settlements") or []):
                s_copy = dict(s)
                s_copy["faction_id"] = fac.get("faction_id")
                all_settlements.append(s_copy)
        f = filters or {}
        if f.get("faction_id"):
            all_settlements = [s for s in all_settlements if s.get("faction_id") == f["faction_id"]]
        if f.get("settlement_type"):
            all_settlements = [s for s in all_settlements if s.get("settlement_type") == f["settlement_type"]]
        result = all_settlements

    elif query_type == "events":
        result = _read_json("event_log.json", [])

    elif query_type == "social_context":
        result = {"myth_vector": [], "norm_vector": {}, "trust_baselines": {}}

    elif query_type == "time":
        result = {"tick": 0, "year": 0, "day_of_year": 0.0}

    elif query_type == "feature":
        features = _read_json("features.json", [])
        for ft in features:
            if ft.get("feature_id") == target_id or ft.get("name") == target_id:
                result = ft
                break
        if result is None:
            result = {"error": f"Feature '{target_id}' not found"}

    else:
        warnings.append(f"Unknown query_type: {query_type}")
        result = {}

    if logs_enabled():
        print(f"[trace:WM] query_world_state: {query_type} target={target_id or '(none)'}")

    return {
        "query_type": query_type,
        "world_time": {"tick": 0, "year": 0, "day_of_year": 0.0},
        "result": result,
        "warnings": warnings,
    }


# ======================================================================
# 12. resolve_contradiction
# ======================================================================


@tool
def resolve_contradiction(
    contradiction_source: str = "",
    override_type: str = "entity_state",
    target_id: str = "",
    declared_state: Optional[Dict[str, Any]] = None,
    narrative_reason: str = "",
    release_stub_lock: bool = True,
) -> Dict[str, Any]:
    """Declare authoritative state forward. No rewind (R13).
    Use read_tool_doc('resolve_contradiction').

    Args:
        override_type: entity_state|faction_state|pending_event|
            world_field|entity_location|settlement_control.
        target_id: entity_id, faction_id, event_id, etc. REQUIRED.
        declared_state: What is now true. Structure varies by override_type.
        narrative_reason: WHY. REQUIRED. Written to event log.
    """
    contradictions = _read_json("resolved_contradictions.json", [])
    entry = {
        "contradiction_source": contradiction_source,
        "override_type": override_type,
        "target_id": target_id,
        "declared_state": declared_state or {},
        "narrative_reason": narrative_reason,
        "release_stub_lock": release_stub_lock,
        "resolved_at": _utc_now(),
    }
    contradictions.append(entry)
    _write_json("resolved_contradictions.json", contradictions)

    if logs_enabled():
        print(f"[trace:WM] resolve_contradiction: {override_type} '{target_id}' — {narrative_reason[:60]}")

    return {
        "resolution_id": f"res_{_utc_now()}",
        "override_type": override_type,
        "target_id": target_id,
        "fields_overridden": list((declared_state or {}).keys()),
        "stub_lock_released": release_stub_lock,
        "event_log_id": f"evt_{_utc_now()}",
        "warnings": [],
    }


# ======================================================================
# 13. post_intent
# ======================================================================


@tool
def post_intent(
    faction_id: str,
    intent_type: str,
    description: str,
    target: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    issuer_id: str = "",
    strength: float = 0.8,
    domain: str = "all",
    expires_condition: str = "",
    revoke_condition: str = "",
    revoke_on_issuer_death: bool = True,
) -> Dict[str, Any]:
    """Directive to faction's L2 engine. WM says what, sim does how (R11).
    Use read_tool_doc('post_intent') for intent_type list and params.

    Args:
        faction_id: Target faction. REQUIRED.
        intent_type: concentrate_military|defend_location|pursue_faction|
            open_trade|cease_expansion|prioritize_resource|retreat|negotiate.
        description: Narrative order. REQUIRED.
        target: feature_id, faction_id, location.
        strength: 0-1. 1.0=hard constraint. Default 0.8.
        issuer_id: entity_id issuing order. Empty=WM direct.
    """
    intents = _read_json("active_intents.json", [])
    intent_id = f"int_{len(intents) + 1}"

    entry = {
        "intent_id": intent_id,
        "faction_id": faction_id,
        "intent_type": intent_type,
        "description": description,
        "target": target,
        "parameters": parameters or {},
        "issuer_id": issuer_id,
        "strength": strength,
        "domain": domain,
        "expires_condition": expires_condition,
        "revoke_condition": revoke_condition,
        "revoke_on_issuer_death": revoke_on_issuer_death,
        "posted_at": _utc_now(),
    }
    intents.append(entry)
    _write_json("active_intents.json", intents)

    if logs_enabled():
        print(f"[trace:WM] post_intent: [{intent_type}] → {faction_id} ({strength})")

    return {
        "intent_id": intent_id,
        "faction_id": faction_id,
        "issuer_id": issuer_id or None,
        "intent_type": intent_type,
        "strength": strength,
        "expires_condition": expires_condition,
        "l2_objective_delta": {},
        "conflicts_with": [],
        "event_log_id": f"evt_{_utc_now()}",
    }


# ======================================================================
# 14. post_entity_directive
# ======================================================================


@tool
def post_entity_directive(
    entity_id: str,
    directive_type: str,
    target: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    description: str = "",
    duration: str = "until_complete",
    priority_over_own_rules: float = 0.5,
) -> Dict[str, Any]:
    """Scoped directive to one L3 entity (entity-scale post_intent, R14).
    Use read_tool_doc('post_entity_directive').

    Args:
        entity_id: Target entity. REQUIRED.
        directive_type: move_to|patrol|guard|follow|attack|
            use_capability|retreat|wake.
        target: feature_id or entity_id.
        duration: "until_complete"|"permanent"|expression.
        priority_over_own_rules: 0-1. 1.0=override entity's rules.
    """
    directives = _read_json("entity_directives.json", [])
    directives.append({
        "entity_id": entity_id,
        "directive_type": directive_type,
        "target": target,
        "parameters": parameters or {},
        "description": description,
        "duration": duration,
        "priority_over_own_rules": priority_over_own_rules,
        "issued_at": _utc_now(),
    })
    _write_json("entity_directives.json", directives)

    if logs_enabled():
        print(f"[trace:WM] post_entity_directive: {directive_type} → {entity_id}")

    return {
        "entity_id": entity_id,
        "directive_type": directive_type,
        "applied": True,
        "contested_by_rule": None,
        "previous_behavior_mode": "STATIONARY",
        "previous_behavior_parameters": {},
        "new_behavior_mode": None,
        "duration": duration,
        "event_log_id": f"evt_{_utc_now()}",
        "warnings": [],
    }


# ======================================================================
# 15. define_relationship
# ======================================================================


@tool
def define_relationship(
    relationship_id: str = "",
    relationship_type: str = "",
    name: str = "",
    parties: Optional[List[Dict[str, str]]] = None,
    terms: Optional[Dict[str, Any]] = None,
    trust_effect: float = 0.0,
    ongoing_trust_modifier: float = 0.0,
    expires_condition: str = "",
    revoke_condition: str = "",
    dissolved: bool = False,
    cause: str = "",
) -> Dict[str, Any]:
    """Structured agreement between factions (R15).
    Use read_tool_doc('define_relationship') for terms schemas per type.

    Args:
        relationship_type: alliance|vassalage|empire_membership|trade_pact|
            patronage|non_aggression|marriage_pact.
        parties: [{"faction_id", "role": "equal"|"suzerain"|"vassal"|...}].
        terms: Type-specific (mutual_defense, tribute_rate, goods, etc.).
        trust_effect: One-time delta to party pairs. Range -1 to +1.
    """
    rels = _read_json("relationships.json", [])
    rid = relationship_id or f"rel_{len(rels) + 1}"
    operation = "dissolved" if dissolved else ("created" if not relationship_id else "updated")

    entry = {
        "relationship_id": rid,
        "relationship_type": relationship_type,
        "name": name or rid,
        "parties": parties or [],
        "terms": terms or {},
        "trust_effect": trust_effect,
        "ongoing_trust_modifier": ongoing_trust_modifier,
        "expires_condition": expires_condition,
        "revoke_condition": revoke_condition,
        "dissolved": dissolved,
        "cause": cause,
        "updated_at": _utc_now(),
    }

    for i, r in enumerate(rels):
        if r.get("relationship_id") == rid and not dissolved:
            rels[i] = entry
            break
    else:
        if not dissolved:
            rels.append(entry)
        elif dissolved:
            rels = [r for r in rels if r.get("relationship_id") != rid]

    _write_json("relationships.json", rels)

    trust_effects = {}
    if parties:
        for i, p1 in enumerate(parties):
            for p2 in parties[i + 1:]:
                key = f"{p1.get('faction_id')}-{p2.get('faction_id')}"
                trust_effects[key] = trust_effect

    if logs_enabled():
        print(f"[trace:WM] define_relationship: {operation} '{name or rid}' ({relationship_type})")

    return {
        "relationship_id": rid,
        "name": name or rid,
        "operation": operation,
        "relationship_type": relationship_type,
        "parties": parties or [],
        "trust_effects_applied": trust_effects,
        "autonomy_blending_active": any(
            (terms or {}).get("autonomy_level", 1.0) < 1.0
            for _ in (parties or [])
        ) if relationship_type in ("vassalage", "empire_membership", "union_membership") else False,
        "validation_errors": [],
        "warnings": [],
    }


# ======================================================================
# 16. read_tool_doc — detailed parameter docs on demand
# ======================================================================


@tool
def read_tool_doc(
    tool_name: str,
    parameters: Optional[List[str]] = None,
) -> str:
    """Read detailed parameter documentation for any WM tool.

    Call this when you need to understand specific parameter options,
    valid values, ranges, or schema details before making a tool call.
    The documentation covers all parameters, their allowed values,
    defaults, ranges, and example usage.

    Args:
        tool_name: Name of the tool to get docs for. One of:
            set_world_orientation, set_player_start,
            finalize_world_generation, define_world_concept,
            alter_feature, define_entity, define_faction,
            define_rule, declare_world_state, subscribe_to_events,
            query_world_state, resolve_contradiction, post_intent,
            post_entity_directive, define_relationship,
            world_setting_result, ready_to_proceed.

        parameters: Optional list of specific parameter names to get
            docs for. If None or empty, returns docs for ALL parameters
            of the specified tool. Example: ["planet_radius", "axial_tilt"]
            Only valid parameter names for the specified tool are returned.

    Returns:
        Formatted markdown with tool description and parameter docs.
    """
    from world_manager.tool_docs_data import get_tool_doc
    return get_tool_doc(tool_name, parameters)
