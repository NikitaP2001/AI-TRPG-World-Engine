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


#: At most this many cold-path wake-ups are served in one cycle, so a chain of
#: wake-ups cannot hold the turn loop; what is left waits for the next cycle.
_MAX_COLD_CALLS_PER_CYCLE = 3


def _initialized_marker_path(app: "GameOrchestrator"):
    return app.world.game_root / "world" / "initialized.json"


def world_is_initialized(app: "GameOrchestrator") -> bool:
    """Whether this world has already been through its startup step.

    The fact lives with the world on disk: a restarted process must not build an
    existing world a second time, and the startup agents must never be told to
    initialise a world that is already there.
    """
    try:
        return _initialized_marker_path(app).exists()
    except Exception:
        return False


def _awareness_marker_path(app: "GameOrchestrator"):
    return app.world.game_root / "world" / "awareness.json"


def awareness_was_given(app: "GameOrchestrator") -> bool:
    """Whether this world has already given its characters their opening.

    The fact lives with the world on disk, as the world's initialization does: a
    restarted process is a new process, not a new world. A session of a world that
    has been opened is informed by other channels — what the scene manager states
    in a resolve cycle, question answering, and each character's own scene context.
    """
    try:
        return _awareness_marker_path(app).exists()
    except Exception:
        return False


def mark_awareness_given(app: "GameOrchestrator") -> None:
    """Record that the world's characters have been given their opening, once."""
    from world.io import _write_json

    payload: Dict[str, Any] = {}
    try:
        wt = app.world.get_world_time()
        payload["world_time"] = wt.to_string() if hasattr(wt, "to_string") else str(wt)
    except Exception:
        pass
    try:
        payload["characters"] = [
            str(p.get("name") or "") for p in app.world.get_players() if isinstance(p, dict)
        ]
    except Exception:
        pass
    try:
        _write_json(_awareness_marker_path(app), payload)
    except Exception as e:
        if logs_enabled():
            print(f"[cycle] could not record that awareness was given: {e}")


def mark_world_initialized(app: "GameOrchestrator") -> None:
    """Record that the world has been built, once and for good."""
    from world.io import _write_json

    payload: Dict[str, Any] = {}
    try:
        wt = app.world.get_world_time()
        payload["world_time"] = wt.to_string() if hasattr(wt, "to_string") else str(wt)
    except Exception:
        pass
    try:
        payload["players"] = [
            str(p.get("name") or "") for p in app.world.get_players() if isinstance(p, dict)
        ]
    except Exception:
        pass
    try:
        _write_json(_initialized_marker_path(app), payload)
    except Exception as e:
        if logs_enabled():
            print(f"[cycle] could not record world initialization: {e}")


def _cold_context(app: "GameOrchestrator") -> Dict[str, Any]:
    """What a cold-path invocation is told: the time, and who is where."""
    context: Dict[str, Any] = {}
    try:
        wt = app.world.get_world_time()
        context["world_time"] = wt.to_string() if hasattr(wt, "to_string") else str(wt)
    except Exception:
        pass
    try:
        from scheduler_queue import current_world_tick
        context["current_tick"] = current_world_tick()
    except Exception:
        pass
    try:
        context["players"] = app.world.get_players()
    except Exception:
        pass
    return context


def drain_cold_path(app: "GameOrchestrator") -> int:
    """Call the agents whose wake-up is due; return how many were called.

    The GM and the WM are not in the turn loop. They are called when they asked
    to be — the wake-up queue they write to — or when the world has gone longer
    than the fallback interval without them. Either way what happens here is an
    ordinary invocation, and their own invocation path records that it happened.
    """
    try:
        from engine.agent_schedule import due_invocations
        from scheduler_queue import current_world_tick
    except Exception:
        return 0

    tick = current_world_tick()
    if tick <= 0:
        return 0
    due = due_invocations(tick, limit=_MAX_COLD_CALLS_PER_CYCLE)
    if not due:
        return 0

    called = 0
    for entry in due:
        agent = str(entry.get("agent") or "")
        notice = str(entry.get("notice") or "").strip()
        if logs_enabled():
            print(f"  [cold-path] waking {agent} ({entry.get('source')}): {notice[:80]}")
        try:
            if agent == "gm":
                app._game_master.call_gm(
                    notice=notice,
                    invoked_as="you are woken by the schedule",
                    context=_cold_context(app),
                )
            elif agent == "wm":
                app._wm.call_wm(notice=notice, context=_cold_context(app))
            else:
                continue
            called += 1
        except Exception as e:
            # The invocation did not happen, so what the agent asked for is still
            # owed: it goes back in the queue rather than being dropped.
            if entry.get("source") == "scheduled":
                try:
                    from scheduler_queue import schedule_invocation
                    schedule_invocation(
                        agent=agent,
                        world_time=int(entry.get("scheduled_tick") or 0),
                        comment=notice,
                    )
                except Exception:
                    pass
            if logs_enabled():
                print(f"[cycle] cold-path call for {agent} failed: {e}")
    return called


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
    if cursor <= 0:
        # A cycle that has not reached a resolution point yet leaves the cursor
        # at zero, and a remaining measured against zero is the world's whole
        # count of seconds — read as a span, it says nothing true.
        try:
            from world import WorldTime
            from world.clock import now_string

            cursor = WorldTime.parse(now_string()).to_seconds()
        except Exception:
            cursor = 0

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
            kind = "waiting, with no end set"
        elif tc:
            kind = "waiting on a condition"
        else:
            kind = "running now"

        # Timing info
        t_start = int(entry.get("t_start") or 0)
        timing_parts = []
        if cursor > 0 and t_start > 0:
            elapsed = max(0, cursor - t_start)
            timing_parts.append(f"{elapsed}s behind you")
        if t_end is not None and cursor > 0:
            remaining = max(0, int(t_end) - cursor)
            timing_parts.append(f"{remaining}s left")
        time_str = f", {', '.join(timing_parts)}" if timing_parts else ""

        # Absorption
        absorption = str(entry.get("absorption") or "alert").strip()
        abs_str = f" ({absorption})" if absorption != "alert" else ""

        # Trigger info
        tc_desc = ""
        if isinstance(tc, dict) and tc:
            tc_desc = f" — fires on {tc.get('actor','any')}/{tc.get('action_type','any')}->{tc.get('target_match','any')}"

        lines.append(
            f"- attempt {intent_id}: {kind}{abs_str}{time_str} — "
            f"{str(entry.get('description') or '')[:100]}{tc_desc}"
        )

    return (
        "Your own attempts that still stand "
        "(withdraw one by its id with cancel_subscription):\n"
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
            print(f"  {pname} error: {e}")
            import traceback
            traceback.print_exc()


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
    establish_disposition: bool = True,
) -> None:
    """Run one player agent and mark them acted.

    ``world_time_override`` anchors the player's ``t_start`` offsets to the
    scheduler's current resolution point instead of the scene's fixed start
    time — used when re-invoking on a trigger match mid-cycle (docs/scene_design.md
    §4 step 4: "the actor is re-invoked from this segment's start time forward").

    ``establish_disposition`` is true at a cycle's opening, where the player is
    called to declare an intent and states the psyche he stands on, and false for
    every later call in that cycle — a character brought back to decide again
    continues the cycle he is in, and his opening is not asked for twice.
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

    # What he was told since his last turn is read now, at his turn — a telling
    # waits for the turn it belongs to. His first turn of the session is the one
    # exception: there his memory is what is read, and the record already stands
    # in it, so saying it again would put it in one context twice.
    from player.agent import continues_his_turn

    given = app.take_what_he_was_given(player_name)
    if given and continues_his_turn(player_name):
        gm_notice = "\n".join(text for text in [gm_notice, *given] if text)

    # The notice reaches him once, and it is the parameter's job to state it: it
    # is carried into the turn context with the rest of what he is given, and
    # appending it here as well would put the same words in front of him twice.

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

    # Set SM resolver so observe/recall get answered immediately. Answering is
    # also a phase in which the scene manager may state what a character holds,
    # so what it states is handed over as soon as the answer comes back.
    from player.agent import set_sm_resolver

    def _resolve_and_deliver(*, player_name: str, questions: str) -> str:
        try:
            return app._sm.resolve_player_question(
                player_name=player_name, questions=questions,
            )
        finally:
            app.deliver_awareness()

    try:
        set_sm_resolver(_resolve_and_deliver)
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
        establish_disposition=establish_disposition,
    )

    result = json.loads(result_json) if isinstance(result_json, str) else result_json
    decision = result.get("decision") or {}
    psyche = str(result.get("psyche_core") or decision.get("psyche_core") or "").strip()
    ego = str(result.get("ego_rationalization") or decision.get("ego_rationalization") or "").strip()
    intent = str(result.get("action") or decision.get("intent") or decision.get("action") or "")
    declared = result.get("declared_intents") or []
    cancel_ids = result.get("cancel_subscription_ids") or []

    if declared:
        print(f" ({len(declared)} intents)")
    else:
        print(f" {intent[:80]}")

    # Store declared intents for the segment scheduler (scheduler/timeline.py)
    if declared:
        timeline = app.world.get_timeline()
        existing = timeline.get("intents") or []
        # scene.json also had declared_intents — now they live in timeline.json only
        existing.extend(declared)
        timeline["intents"] = existing
        app.world.set_timeline(timeline)

    if cancel_ids:
        _apply_cancelled_subscriptions(app, player_name, cancel_ids)

    # No acted flag — continuous timeline has no turn boundary (design doc §6)

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
            found = _query(limit=50)
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


def _characters_without_a_place(app: "GameOrchestrator") -> List[str]:
    """Characters who stand nowhere the world holds.

    A place is an entity: a character's place is real when the store holds the
    entity his position names. The empty place reads as a phrase rather than as
    an entity, so it fails the same test as any other name the world does not
    hold — no list of forbidden names is needed, and a world that holds a place
    by that name would be honoured.

    Read from the store on every cycle, so the answer follows the world rather
    than the moment a session first looked.
    """
    from world_manager.entity_core import get_entity

    out: List[str] = []
    try:
        players = app.world.get_players()
    except Exception:
        return out
    for player in players:
        if not isinstance(player, dict):
            continue
        name = str(player.get("name") or "").strip()
        place = str(player.get("location") or "").strip()
        if not name:
            continue
        if place and get_entity(place) is not None:
            continue
        out.append(name)
    return out


_asked_to_place: set = set()


def _ask_gm_to_place_characters(app: "GameOrchestrator", names: List[str]) -> None:
    """Tell the Game Master, once, that characters stand nowhere.

    Where a character begins is a campaign fact, and the Game Master owns those.
    Asked once per set of names: a call that changes nothing would be repeated
    every cycle, and the line printed above already says what still has to happen.
    """
    key = tuple(sorted(names))
    if key in _asked_to_place:
        return
    _asked_to_place.add(key)

    starting = ""
    try:
        from world.io import read_json
        raw = read_json(app.world.game_root / "world_setting.json")
        reference = raw.get("player_reference") if isinstance(raw, dict) else None
        if isinstance(reference, dict):
            starting = str(reference.get("starting_location") or "").strip()
    except Exception:
        pass

    context: Dict[str, Any] = {
        "players": [{"name": name, "location": "?"} for name in names],
    }
    try:
        context["world_time"] = app.world.get_world_time().to_string()
    except Exception:
        pass

    notice = "characters stand nowhere the world holds"
    if starting:
        notice += f"; the world begins at: {starting}"
    try:
        app._game_master.call_gm(
            notice=notice,
            invoked_as="you are invoked to place the characters",
            context=context,
        )
    except Exception as e:
        print(f"  GM could not be asked to place the characters: {e}")


def _run_startup_agents(app: "GameOrchestrator") -> None:
    """First-time startup: invoke WM then GM with full tool access.

    WM creates world structure (regions, factions, templates). GM populates
    entities (NPCs, player state/skills/equipment). Both run ReAct loops
    with their full tool sets until termination.

    This is the world's beginning, not a recurring step: it is gated by the
    initialization record on disk (see world_is_initialized).
    """
    ok = True
    print("\n  WM initializing world structure...")
    try:
        app._wm.call_wm(notice="initialize_world")
    except Exception as e:
        ok = False
        print(f"  WM init error: {e}")

    print("  GM populating entities...")
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
        gm_context: Dict[str, Any] = {"players": player_list}
        if player_list:
            print(f"  GM needs to initialize {len(player_list)} player(s): {', '.join(p['name'] for p in player_list)}")
        # Pass world time on first invocation
        try:
            wt = app.world.get_world_time()
            gm_context["world_time"] = wt.to_string()
        except Exception:
            pass
        # Pass plot on first invocation (prefer original preset over game copy)
        try:
            from world.io import read_json
            workspace_root = app.world.game_root.parent
            for candidate in [
                workspace_root / "setups" / "plot.json",
                app.world.game_root / "setups" / "plot.json",
            ]:
                if candidate.exists():
                    raw = read_json(candidate)
                    if isinstance(raw, dict) and raw.get("init", "").strip():
                        gm_context["plot"] = raw
                        break
        except Exception:
            pass

        # Set inline WM invoker so notify_wm(blocking=True) works synchronously
        from world_manager.tool_registry import set_wm_invoker
        def _wm_inline(message: str) -> str:
            """Invoke WM synchronously with an escalation message."""
            result = app._wm.call_wm(
                notice=f"[escalation] {message[:100]}",
                context={"escalation_message": message},
            )
            thinking = str(result.get("thinking") or result.get("exit_args") or "")
            return thinking[:1000] if thinking else "(WM responded)"
        set_wm_invoker(_wm_inline)

        app._game_master.call_gm(
            notice="",
            invoked_as="you are invoked to initialise the world",
            context=gm_context,
        )

        # Strip description.json to core fields (general/race/personality) now
        # that GM has created entity store entries.
        from world.state import World as _World
        for _cname in app.world.list_character_names():
            _World.strip_character_description_to_core(
                game_root=app.world.game_root, name=_cname,
            )
    except Exception as e:
        ok = False
        print(f"  GM init error: {e}")

    # The world counts as built only if both agents got through; otherwise the
    # next cycle tries again rather than leaving a half-built world behind.
    if ok:
        mark_world_initialized(app)


def run_cycle(app: "GameOrchestrator", user_msg: Optional[HumanMessage]) -> None:
    """Execute one cycle, then let the cold path wake whoever is due."""
    _run_cycle_body(app, user_msg)
    drain_cold_path(app)


def _run_cycle_body(app: "GameOrchestrator", user_msg: Optional[HumanMessage]) -> None:
    """Execute one cycle."""
    app.world.ensure_initialized()
    app._clear_cycle_temp_dir()
    reset_cycle_lock()

    # 1. World setting
    if not app._maybe_create_world_setting():
        if logs_enabled():
            print("[cycle] Waiting for world setting...")
        return

    # 1b. The world is built once, by its startup agents. Whether that has
    #     happened is a fact about the world, recorded on disk with it, so a
    #     restarted process does not build what is already there and no agent is
    #     ever told to initialise an existing world.
    if not getattr(app, '_fully_initialized', False) and not world_is_initialized(app):
        app._fully_initialized = True
        _run_startup_agents(app)

    # 2. A session happens where the characters are. A character's place is an
    #    entity the world holds: while the world is still being built, or when a
    #    character was never put anywhere, there is no scene to stage — and a
    #    session staged in a place that does not exist narrates a room nobody
    #    stands in, which is the scene manager inventing the world it was meant to
    #    read. The session waits, and the Game Master is told once, because where
    #    the characters begin is a campaign fact.
    nowhere = _characters_without_a_place(app)
    if nowhere:
        print(f"  {len(nowhere)} character(s) stand nowhere the world holds: "
              f"{', '.join(nowhere)}")
        _ask_gm_to_place_characters(app, nowhere)
        return

    # 3. Auto-start session if not already active (§1.1 — multi-cycle session)
    if not getattr(app, '_session_active', False):
        app.begin_session()

    try:
        # 4. Bootstrap scene if none active
        app._bootstrap_next_scene()

        # 5. Check if any players exist (no scene.json — players from entity store)
        try:
            all_players = app.world.get_players()
            if not all_players:
                if logs_enabled():
                    print("[cycle] No players found")
                return
        except Exception:
            return

        # 6. Invocation by location (§5.1) — no scene.json, no acted flags.
        #    Players are grouped by location from entity store.
        try:
            all_players = app.world.get_players()
        except Exception:
            all_players = []

        # Group by location. Every player standing here has a place the world
        # holds; every location is called.
        location_groups: Dict[str, List[str]] = {}
        for ch in all_players:
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            loc = str(ch.get("location") or "").strip()
            if name and loc:
                location_groups.setdefault(loc, []).append(name)

        if not location_groups:
            if logs_enabled():
                print("[cycle] No players at any location")
            return

        start_time = app.world.get_world_time().to_string()

        # Step 1: players with no active intent, at every location
        for location, players_at_loc in location_groups.items():
            no_intent = _players_without_active_intent(app, location)
            if no_intent:
                print(f"  Invoking {len(no_intent)} player(s) without active intent at {location}: {', '.join(no_intent)}")
                _invoke_players(
                    app, no_intent,
                    location=location, start_time=start_time,
                    npcs=[], players=players_at_loc,
                    scene_data={},
                )

        # Step 2 removed: alert re-invoke with generic notice was uninformative.
        # SegmentScheduler handles re-invocation via trigger matching (§4).

        # 6. Resolution loop + finalize
        app._finalize_cycle_if_ready()

        # 6b. The world's own processes advance with it: every effect lands what
        #     its period has earned over the time this cycle moved. It runs after
        #     the clock is written, so what lands is the time the world actually
        #     reached, and before the cold path, so an agent woken in this cycle
        #     reads the state as it stands rather than as it was.
        try:
            from world.clock import current_tick as _world_seconds
            from world_manager.entity_core import advance_world_processes

            outcome = advance_world_processes(_world_seconds())
            if logs_enabled() and (outcome.get("effects_applied")
                                   or outcome.get("effects_expired")):
                print(f"  [world] effects: {outcome.get('effects_applied')} landed, "
                      f"{outcome.get('effects_expired')} ended")
        except Exception:
            import traceback
            traceback.print_exc()

        # ── Scheduler tick: evaluate periodic jobs (summaries, etc.) ──
        try:
            app._scheduler.tick(
                cycle_count=0,
                session_count=app._scheduler.session_count,
                paragraph_count=0,
                world=app.world,
            )
        except Exception:
            import traceback
            traceback.print_exc()

    finally:
        # End session only if we auto-started it (backward compat — REPL mode).
        # Multi-cycle sessions manage lifecycle externally via app.begin/end_session.
        if not getattr(app, '_session_active', False):
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
