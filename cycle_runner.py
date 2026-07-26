"""Cycle runner: orchestrates one game cycle.

Flow:
  1. Ensure world setting exists
  2. Bootstrap scene if none active
  3. Run unacted characters (each calls observe/recall + character_decision)
  4. Resolution loop: SM reviews intents, may re-invoke characters
  5. SM narrates, scene closes
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from gm.tools import reset_cycle_lock
from openrouter_langchain_logging import logs_enabled
from player.agent import run_player_agent

if TYPE_CHECKING:
    from engine.orchestrator import GameOrchestrator as ConsoleApp


def _build_public_intents_notice(
    character_name: str,
    all_intents: List[Dict[str, Any]],
) -> str:
    """Build auto-injection notice: public intents this character would perceive.

    This is a same-round, pre-scheduler courtesy notice only — it lets a
    character react to what a peer declared earlier in the SAME initiative
    pass, before the segment scheduler (scheduler/timeline.py) has run at
    all. The scheduler is the authoritative mechanism for perception gating
    (private visibility) and trigger-based re-invocation (docs/scene_design.md
    §2-§4); this heuristic intentionally stays simple and public-only.

    Characters in the same scene automatically notice:
    - Public speech (target=all, area, or this character)
    - Public movement (visible in the room)
    - Any public action targeting them directly

    Args:
        character_name: The perceiving character.
        all_intents: All declared intents so far.

    Returns:
        Formatted notice string, or empty string if nothing relevant.
    """
    name_lower = character_name.strip().lower()
    relevant: List[str] = []

    for intent in all_intents:
        actor = str(intent.get("actor") or "").strip()
        if not actor or actor.strip().lower() == name_lower:
            continue  # Skip own intents
        visibility = str(intent.get("visibility") or "").strip().lower()
        if visibility != "public":
            continue
        target = str(intent.get("target") or "").strip().lower()
        action_type = str(intent.get("action_type") or "").strip()
        description = str(intent.get("description") or "").strip()

        perceives = False
        if target in ("all", "area", name_lower):
            perceives = True
        elif action_type == "speech":
            perceives = True  # Speech is audible in same room
        elif action_type == "movement":
            perceives = True  # Movement is visible in same room
        elif action_type in ("cosmetic", "combat"):
            perceives = True  # Notable actions are perceivable

        if perceives:
            line = f"{actor} {action_type} → {target}"
            if description:
                line += f": {description}"
            relevant.append(line)

    if not relevant:
        return ""

    return (
        "You notice what others in the scene are doing:\n"
        + "\n".join(f"- {r}" for r in relevant)
    )


def _format_active_subscriptions(app: "GameOrchestrator", character_name: str) -> str:
    """§9.1 — list a character's own active subscriptions/pending intents with ids.

    Shows both persistent subscriptions and bounded intents that are still
    active (not yet completed). Includes timing info (elapsed, remaining)
    when available.

    Reads the persisted timeline store (independent of scene lifecycle — see
    world/state.py get_timeline/set_timeline), capped in count per docs/scene_design.md §9.1.
    """
    try:
        timeline = app.world.get_timeline()
    except Exception:
        return ""
    intents = timeline.get("intents") if isinstance(timeline, dict) else []
    if not isinstance(intents, list):
        return ""

    cursor = int(timeline.get("cursor") or 0)

    mine = [
        i for i in intents
        if isinstance(i, dict) and str(i.get("actor") or "").strip() == character_name
        and str(i.get("status") or "active") == "active"
    ]
    if not mine:
        return ""

    MAX_SHOWN = 8
    lines = []
    for entry in mine[:MAX_SHOWN]:
        intent_id = str(entry.get("id", "?"))

        # Determine kind
        t_end = entry.get("t_end")
        tc = entry.get("trigger_condition") or {}
        is_subscription = t_end is None

        if is_subscription:
            kind = "persistent subscription"
        elif tc:
            kind = "pending wait"
        else:
            kind = "active intent"

        # Timing info
        t_start = int(entry.get("t_start") or 0)
        timing_parts = []
        if cursor > 0 and t_start > 0:
            elapsed = max(0, cursor - t_start)
            timing_parts.append(f"elapsed={elapsed}s")
        if t_end is not None:
            remaining = max(0, int(t_end) - cursor)
            timing_parts.append(f"remaining={remaining}s")
        time_str = f" [{', '.join(timing_parts)}]" if timing_parts else ""

        # Absorption
        absorption = str(entry.get("absorption") or "alert").strip()
        abs_str = f" ({absorption})" if absorption != "alert" else ""

        # Trigger info
        tc_desc = ""
        if isinstance(tc, dict) and tc:
            tc_desc = f" — fires on {tc.get('actor','any')}/{tc.get('action_type','any')}->{tc.get('target_match','any')}"

        lines.append(f"- [{intent_id}] {kind}{abs_str}{time_str}: {str(entry.get('description') or '')[:100]}{tc_desc}")

    return (
        "Your active intents/subscriptions (cancel via cancel_subscription(subscription_ids=[...])):\n"
        + "\n".join(lines)
    )


def _apply_cancelled_subscriptions(app: "GameOrchestrator", character_name: str, ids: List[str]) -> None:
    """Mark the given subscription ids (owned by ``character_name``) as cancelled."""
    ids_clean = {str(x).strip() for x in (ids or []) if str(x).strip()}
    if not ids_clean:
        return
    try:
        timeline = app.world.get_timeline()
    except Exception:
        return
    intents = timeline.get("intents") if isinstance(timeline, dict) else []
    if not isinstance(intents, list):
        return
    changed = False
    for entry in intents:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "") in ids_clean and str(entry.get("actor") or "") == character_name:
            entry["status"] = "cancelled"
            changed = True
    if changed:
        app.world.set_timeline(timeline)


def _players_without_active_intent(app: "GameOrchestrator", location: str) -> List[str]:
    """§5.1 step 1 — find players at this location with no active intent.

    Checks the timeline store for any active intent owned by a player at this
    location. A player without any active intent needs to declare one.
    """
    try:
        chars = app.world.get_players()
        timeline = app.world.get_timeline()
        intents = timeline.get("intents") if isinstance(timeline, dict) else []
    except Exception:
        return []

    # Get all players at this location
    players_here = []
    for ch in chars:
        if isinstance(ch, dict) and ch.get("location") == location:
            players_here.append(str(ch.get("name") or "").strip())

    # Find who has active intents
    active_actors = set()
    for entry in intents:
        if isinstance(entry, dict) and str(entry.get("status") or "active") == "active":
            active_actors.add(str(entry.get("actor") or "").strip())

    # Return those without active intents
    return [p for p in players_here if p and p not in active_actors]


def _alert_players_at_location(app: "GameOrchestrator", location: str) -> List[str]:
    """§5.1 step 2 — find alert players at this location who may need to react."""
    try:
        chars = app.world.get_players()
        timeline = app.world.get_timeline()
        intents = timeline.get("intents") if isinstance(timeline, dict) else []
    except Exception:
        return []

    players_here = set()
    for ch in chars:
        if isinstance(ch, dict) and ch.get("location") == location:
            players_here.add(str(ch.get("name") or "").strip())

    # Only alert players with ongoing active intents
    alert_with_intent = set()
    for entry in intents:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "active") != "active":
            continue
        actor = str(entry.get("actor") or "").strip()
        if actor not in players_here:
            continue
        if str(entry.get("absorption") or "alert").strip() == "immersed":
            continue
        alert_with_intent.add(actor)

    return sorted(alert_with_intent)


def _invoke_players(
    app: "GameOrchestrator",
    player_names: List[str],
    *,
    location: str,
    start_time: str,
    npcs: List[str],
    players: List[str],
    gm_notice: str = "",
    scene_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Invoke a list of players — each runs observe/recall → declare_intent."""
    if scene_data is None:
        scene_data = app.world.get_scene()
    for pname in player_names:
        entry = (scene_data.get("players") or {}).get(pname) or {}
        if entry.get("acted") is True:
            continue
        try:
            _run_single_player(
                app=app, player_name=pname, scene_data=scene_data,
                location=location, start_time=start_time,
                npcs=npcs, players=players,
                gm_notice=gm_notice,
            )
        except Exception as e:
            print(f"  ✗ {pname} error: {e}")
            import traceback
            traceback.print_exc()
            try:
                from scene.runtime import Scene
                app._scene.mark_player_acted(
                    player_name=pname,
                    last_decision=f"(error: {e})",
                    last_thoughts="",
                    output_source="error",
                )
            except Exception:
                pass


def _run_single_player(
    *,
    app: "GameOrchestrator",
    player_name: str,
    scene_data: Dict[str, Any],
    location: str,
    start_time: str,
    npcs: List[str],
    players: List[str],
    gm_notice: str = "",
    world_time_override: str = "",
) -> None:
    """Run one player agent and mark them acted.

    ``world_time_override`` anchors the player's ``t_start`` offsets to the
    scheduler's current resolution point instead of the scene's fixed start
    time — used when re-invoking on a trigger match mid-cycle (docs/scene_design.md
    §4 step 4: "the actor is re-invoked from this segment's start time forward").
    """
    scene_desc = str(scene_data.get("scene_description") or "")
    player_descriptions = scene_data.get("player_descriptions") or {}
    effective_time = str(world_time_override or start_time)

    # Build personalized context
    personal_desc = player_descriptions.get(player_name, "")
    if personal_desc:
        char_context = f"Location: {location}\nTime: {effective_time}\n\n{personal_desc}"
    else:
        ctx_parts = [f"Location: {location}", f"Time: {effective_time}"]
        if scene_desc:
            ctx_parts.append(f"\nScene:\n{scene_desc}")
        if npcs:
            ctx_parts.append(f"\nNPCs present: {', '.join(str(n) for n in npcs)}")
        char_context = "\n".join(ctx_parts)

    # Add re-invoke context from SM
    if gm_notice:
        char_context += f"\n\n[Notice]\n{gm_notice}"

    # §9.1 — surface this player's own active subscriptions/pending intents
    subs_notice = _format_active_subscriptions(app, player_name)
    if subs_notice:
        char_context += f"\n\n{subs_notice}"

    # Load character description (fictional persona data)
    char_desc = {}
    try:
        char_desc = app.world.get_character_description(player_name)
    except Exception:
        pass

    # Set SM resolver so observe/recall get answered immediately
    from player.agent import set_sm_resolver
    try:
        sm_resolver = app._sm.resolve_player_question
        set_sm_resolver(sm_resolver)
    except Exception:
        set_sm_resolver(None)

    # Run player agent
    result_json = run_player_agent(
        character_name=player_name,
        character_description=char_desc,
        current_scene_context=char_context,
        scene_location=location,
        world_time=effective_time,
        scene_npcs=list(npcs) if npcs else None,
        scene_players=players,
        require_decision=False,
        persist_history=True,
        gm_reality_notice=gm_notice,
    )

    result = json.loads(result_json) if isinstance(result_json, str) else result_json
    decision = result.get("decision") or {}
    psyche = str(result.get("psyche_core") or decision.get("psyche_core") or "").strip()
    ego = str(result.get("ego_rationalization") or decision.get("ego_rationalization") or "").strip()
    intent = str(result.get("action") or decision.get("intent") or decision.get("action") or "")
    declared = result.get("declared_intents") or []
    cancel_ids = result.get("cancel_subscription_ids") or []
    # Include pre-tool reasoning (Phase 5 — character thoughts output)
    thoughts_text = str(result.get("thoughts_text") or "").strip()

    if declared:
        print(f" ({len(declared)} intents)")
    else:
        print(f" {intent[:80]}")

    # Store declared intents for the segment scheduler (scheduler/timeline.py)
    if declared:
        s = app.world.get_scene()
        s.setdefault("declared_intents", []).extend(declared)
        app.world.set_scene(s)

    if cancel_ids:
        _apply_cancelled_subscriptions(app, player_name, cancel_ids)

    # Mark player as acted
    thoughts_block = f"psyche: {psyche}\nego: {ego}"
    if thoughts_text:
        thoughts_block = f"[thoughts]\n{thoughts_text}\n\n{thoughts_block}"
    app._scene.mark_player_acted(
        player_name=player_name,
        last_decision=intent,
        last_thoughts=thoughts_block,
        output_source="model",
    )


def _resolve_player_locations(player_list: list, app: "GameOrchestrator") -> list:
    """Resolve each player's location string to a real entity_id.

    Queries the entity store after WM has created the world. If a location
    name doesn't match any existing entity, prefixes with '?' so GM knows
    the location could not be verified.
    """
    from world_manager.entity_store import query_entities as _query
    resolved = []
    for p in player_list:
        loc = (p.get("location") or "").strip()
        if loc and not loc.startswith("?"):
            found = _query(type_filter="location", limit=50)
            match = None
            loc_lower = loc.lower()
            for entity in found:
                eid = (entity.get("id") or entity.get("name") or "").lower()
                if eid == loc_lower:
                    match = entity
                    break
                if loc_lower in eid or eid in loc_lower:
                    match = entity
            if match:
                p = dict(p)
                p["location"] = match.get("id") or match.get("name") or loc
            else:
                p = dict(p)
                p["location"] = "?" + loc
        resolved.append(p)
    return resolved


def _run_startup_agents(app: "GameOrchestrator") -> None:
    """First-time startup: invoke WM then GM with full tool access.

    WM creates world structure (regions, factions, templates). GM populates
    entities (NPCs, player state/skills/equipment). Both run ReAct loops
    with their full tool sets until termination.
    """
    print("\n  🌍 WM initializing world structure...")
    try:
        app._wm.call_wm(notice="initialize_world")
    except Exception as e:
        print(f"  ⚠ WM init error: {e}")

    print("  📜 GM populating entities...")
    try:
        # Build player context so GM knows who needs state files
        # Read players from entity store (WM has already created entities)
        player_list = []
        try:
            player_list = app.world.get_players()
        except Exception:
            pass
        # Resolve location strings to real entity_ids (or mark as unresolved)
        player_list = _resolve_player_locations(player_list, app)
        gm_context = {"players": player_list}
        if player_list:
            print(f"  📋 GM needs to initialize {len(player_list)} player(s): {', '.join(p['name'] for p in player_list)}")
        app._game_master.call_gm(notice="initialize", context=gm_context)
    except Exception as e:
        print(f"  ⚠ GM init error: {e}")


def run_cycle(app: "GameOrchestrator", user_msg: Optional[HumanMessage]) -> None:
    """Execute one cycle."""
    app.world.ensure_initialized()
    app._clear_cycle_temp_dir()
    reset_cycle_lock()

    # 1. World setting
    if not app._maybe_create_world_setting():
        if logs_enabled():
            print("[cycle] Waiting for world setting...")
        return

    # 1b. First-time startup: WM and GM full invocations with all tools.
    #     Only runs once, right after world setting is created.
    if not getattr(app, '_fully_initialized', False):
        app._fully_initialized = True
        _run_startup_agents(app)

    # 2. Begin SM session + character session contexts (§1.1)
    try:
        app._sm.begin_session()
    except Exception:
        pass
    try:
        from player.agent import begin_session
        begin_session()
    except Exception:
        pass

    try:
        # 3. Bootstrap scene if none active
        app._bootstrap_next_scene()

        # 4. Check if scene is active
        scene = app.world.get_scene()
        if not isinstance(scene, dict) or scene.get("state") != "active":
            if logs_enabled():
                print("[cycle] No active scene")
            return

        # 5. T-cycle invocation (§5.1) — per location:
        #    Step 1: players WITHOUT active intents declare first.
        #    Step 2: if any declared new intent, alert players re-evaluate.
        #    Step 3: no one without intent → all alert players skip.
        #    Step 4: immersed players never invoked by this condition.
        chars = scene.get("players") or {}
        location = str(scene.get("location") or "")
        start_time = str(scene.get("start_time") or "")
        npcs = scene.get("npcs") or []
        players = list(chars.keys())

        scene_data = scene  # capture initial scene state

        # Step 1: players with no active intent
        no_intent = _players_without_active_intent(app, location)
        if no_intent:
            print(f"  🎯 Invoking {len(no_intent)} player(s) without active intent: {', '.join(no_intent)}")
            _invoke_players(
                app, no_intent,
                location=location, start_time=start_time,
                npcs=npcs, players=players,
                scene_data=scene_data,
            )

        # Step 2: check if any new intents appeared, then alert players
        # Refresh from scene to get declared_intents
        scene_after = app.world.get_scene()
        declared_after = scene_after.get("declared_intents") or []
        had_new_intents = bool(declared_after)

        if had_new_intents:
            alert_players = _alert_players_at_location(app, location)
            if alert_players:
                print(f"  👀 Re-evaluating {len(alert_players)} alert player(s): {', '.join(alert_players)}")
                _invoke_players(
                    app, alert_players,
                    location=location, start_time=start_time,
                    npcs=npcs, players=players,
                    scene_data=scene_after,
                    gm_notice="You notice new activity at this location.",
                )

        # Step 3 & 4: handled implicitly — if no one without intent, or everyone
        # is immersed, nothing happens. The scheduler handles carry-over.

        # 6. Resolution loop + finalize
        app._finalize_cycle_if_ready()

    finally:
        # Always end SM session (cleanup)
        try:
            app._sm.end_session()
        except Exception:
            pass
        try:
            from player.agent import end_session
            end_session()
        except Exception:
            pass
        try:
            from player.agent import set_sm_resolver
            set_sm_resolver(None)
        except Exception:
            pass
