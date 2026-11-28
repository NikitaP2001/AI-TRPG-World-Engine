"""Orchestrator — game engine orchestration layer.

Owns the resolution cycle: bootstrap scene → run characters → finalize.
Orchestrator is the single entry point for "advance the game one step."
It does NOT contain UI, REPL, or GUI code.

All agent instances (SM, GM, WM, Scene) are created here and exposed
for UI layers to inspect.

Layer 1 in the architecture — above agents, below entry points.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage

from world import World, WorldTime
from world.time import WorldTime as _WorldTime
from scene_manager import SceneManager
from world_manager import WorldManager
from gm.game_master import GameMaster
from gm.tools import reset_cycle_lock
from engine.incidents import format_incident_report, record_incident

from scheduler import TickScheduler
from scheduler.jobs import register_all_jobs
from memory_store import limits_from_env
from openrouter_langchain_logging import logs_enabled
from bootstrap import initialize_game_dir
from gm.full_history import gm_max_cycles_from_env
from cycle_runner import run_cycle


class WorldSettingUnavailable(RuntimeError):
    """The world setting could not be created, so no world can be opened."""


class Orchestrator:
    """Game engine orchestrator — owns all agent instances and the cycle logic.

    Entry points (console_app, opengl_app) create one Orchestrator and call
    ``run_cycle()`` to advance the game by one step.
    """

    _startup_scene_cleanup_done: bool = False

    def __init__(self) -> None:
        self.world = World()
        self.world.ensure_initialized()
        self._scene_bootstrapped = False
        if not Orchestrator._startup_scene_cleanup_done:
            Orchestrator._startup_scene_cleanup_done = True

        self.limits = limits_from_env()
        self.max_cycles = gm_max_cycles_from_env()
        self._prev_intent_keys: set = set()

        # ── GM ──
        self.gm_master_history_path = (self.world.game_root / "game_master_messages.json").resolve()
        self._game_master = GameMaster(history_path=self.gm_master_history_path)

        # ── SM ──
        self._sm = SceneManager(
            history_path=(self.world.game_root / "scene_manager_messages.json").resolve(),
        )

        # ── WM ──
        self.wm_messages_path = (self.world.game_root / "world_master_messages.json").resolve()
        self._wm = WorldManager(
            history_path=self.wm_messages_path,
        )
        # Each of the two writes an outline of what it changed into the other's
        # memory: the Game Master learns what the world gained, the World Manager
        # learns what the campaign changed.
        self._game_master.set_peer_history(self.wm_messages_path)
        self._wm.set_peer_history(self.gm_master_history_path)
        # The scene manager invokes the Game Master on its own; those invocations
        # report into the World Manager's memory like any other. What the scene
        # manager itself changes in a session is read by the Game Master.
        self._sm.set_gm_peer_history(self.wm_messages_path)
        self._sm.set_peer_history(self.gm_master_history_path)
        # The authority is reachable from the scene manager in every phase of a
        # session, not only while it answers a character's question: wherever the
        # scene manager escalates, the call goes through this one invoker.
        from world_manager.tool_registry import set_gm_invoker
        set_gm_invoker(self._sm.ask_the_gm)
        self._world_setting_ready = bool(self._wm.load_world_setting())
        # Whether the world has been built is recorded with the world itself, so
        # a restarted process resumes it instead of building it again.
        try:
            from cycle_runner import world_is_initialized
            self._fully_initialized = world_is_initialized(self)
        except Exception:
            self._fully_initialized = False
        # The cold path measures "how long since the agent was called" from a
        # record; an agent called before that record existed gets one here.
        try:
            from engine.agent_schedule import ensure_call_records
            ensure_call_records()
        except Exception:
            pass

        # ── Scheduler ──
        self._scheduler = TickScheduler(
            state_path=self.world.game_root / "world" / "scheduler_state.json",
        )
        self._register_scheduled_jobs()

        # ── Stream echo ──
        raw_stream = os.getenv("LLM_WORLD_STREAM_ECHO")
        if raw_stream is None or not raw_stream.strip():
            self.stream_echo = True
        else:
            self.stream_echo = raw_stream.strip().lower() in {"1", "true", "yes", "y", "on"}
        self._apply_stream_env()

        # ── Session management ──
        self._session_active = False

    # ── Properties for backward compat ──

    @property
    def _rag_search(self):
        return None

    # ── Stream ──

    def _apply_stream_env(self) -> None:
        os.environ["LLM_WORLD_STREAM_ECHO"] = "1" if self.stream_echo else "0"

    # ── World setting ──

    def ensure_world_setting(self) -> bool:
        """Create the world setting block on-demand.
        Returns True if setting is ready.
        """
        if self._world_setting_ready:
            return True
        try:
            characters = []
            try:
                for name in self.world.list_character_names():
                    desc = self.world.get_character_description(name)
                    if isinstance(desc, dict):
                        characters.append(desc)
            except Exception:
                pass
            plot_data = {}
            try:
                from world.io import read_json
                # Prefer the original preset plot over the game copy
                workspace_root = self.world.game_root.parent
                for candidate in [
                    workspace_root / "setups" / "plot.json",
                    self.world.game_root / "setups" / "plot.json",
                ]:
                    if candidate.exists():
                        raw = read_json(candidate)
                        if isinstance(raw, dict) and raw.get("init", "").strip():
                            plot_data = raw
                            break
            except Exception:
                pass
            ws = self._wm.create_world_setting(characters=characters, plot=plot_data)
            if not ws:
                # Without a setting there is no essence, no calibration and no
                # starting time, so every fact written afterwards would be stamped
                # year zero. Refuse to open a world rather than open a broken one.
                record_incident(
                    "world_setting_missing",
                    RuntimeError("create_world_setting returned no setting"),
                )
                raise WorldSettingUnavailable(
                    "The World Manager produced no valid world setting in 3 attempts, so "
                    "the world would have no essence, no GURPS calibration and no starting "
                    "time. Nothing has been written — re-run to try again."
                )
            initial_time = str(ws.get("initial_world_time") or "").strip()
            if not initial_time:
                record_incident(
                    "world_setting_no_time",
                    RuntimeError("initial_world_time is missing"),
                )
                raise WorldSettingUnavailable(
                    "The world setting carries no initial_world_time, so every timestamp "
                    "in the world would be year zero. Nothing has been written — re-run "
                    "to try again."
                )
            self._wm.save_world_setting(ws)
            self.world.set_world_time(initial_time)
            self._world_setting_ready = True
            if logs_enabled():
                print("[trace] World setting created")
            return True
        except WorldSettingUnavailable:
            raise
        except Exception as e:
            if logs_enabled():
                print(f"[trace] World setting creation failed: {e}")
            return False

    # ── Preset loading ──

    def load_preset(self, name: str) -> None:
        """Load a preset (characters, locations, plot) from setups/{name}/ or setups/{name}.json."""
        from world.io import read_json, write_json
        import shutil
        game_root = self.world.game_root
        workspace_root = game_root.parent
        preset_dir = workspace_root / "setups"
        src_dir = preset_dir / name

        if src_dir.is_dir():
            # Load structured format: character subdirectories with description.json
            chars_src = src_dir / "characters"
            if chars_src.is_dir():
                for char_dir in sorted(chars_src.iterdir()):
                    if not char_dir.is_dir():
                        continue
                    char_name = char_dir.name
                    dst_dir = game_root / "players" / char_name
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for json_file in char_dir.glob("*.json"):
                        shutil.copy2(json_file, dst_dir / json_file.name)
                    desc_path = dst_dir / "description.json"
                    if desc_path.exists():
                        desc = read_json(desc_path)
                        if isinstance(desc, dict):
                            desc["name"] = char_name
                            write_json(desc_path, desc)

            # Also check for legacy flat JSON files
            for f in ["characters.json", "locations.json", "plot.json"]:
                src = src_dir / f
                if src.exists():
                    data = read_json(src)
                    if f == "plot.json":
                        write_json(game_root / "setups" / f, data)

            # Copy description.json if present (shared character descriptions)
            desc_src = src_dir / "description.json"
            if desc_src.exists():
                shutil.copy2(desc_src, game_root / "setups" / "description.json")

        else:
            preset_file = preset_dir / f"{name}.json"
            if preset_file.exists():
                data = read_json(preset_file)
                chars = data.get("players", [])
                for ch_data in chars:
                    char_name = str(ch_data.get("name") or "").strip()
                    if char_name:
                        ch_data["name"] = char_name
                        dst_dir = game_root / "players" / char_name
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        (dst_dir / "description.json").write_text(
                            json.dumps(ch_data, ensure_ascii=False, indent=2))
                locs = data.get("locations", [])
                for loc_data in locs:
                    self.world.add_location(loc_data)

        # Ensure character files are properly indexed
        try:
            from world.io import ensure_character_description_fields
            ensure_character_description_fields(game_root=game_root)
        except Exception:
            pass

        # Sync info.json so get_info() reflects loaded characters immediately
        try:
            self.world._sync_info_characters()
        except Exception:
            pass

        print(f"  Story seeded")
        info = read_json(self.world.paths.info_json)
        chars_info = info.get("players") if isinstance(info, dict) else []
        loaded_chars = [c for c in chars_info if isinstance(c, dict) and c.get("name")]
        print(f"  {len(loaded_chars)} characters loaded")
        try:
            locs = self.world.get_locations()
            print(f"  {len(locs)} location(s) created")
        except Exception:
            pass
        print(f"Preset '{name}' loaded. Use 'continue' to start.")

    # ── Scene bootstrap ──

    def bootstrap_scene(self) -> bool:
        """Give every character the opening scene, through the scene manager.

        Runs once for the world, not once for the process: what tells a restarted
        process that the opening is behind it is recorded with the world, as the
        world's initialization is. Resolves the places the characters stand in and
        holds the phase to them: the close is refused while a character in the
        scene knows nothing of it, so the scope is checked rather than announced.
        The scene manager enters what each of them perceives and hands it to him.
        What the framework then does is deliver what was entered
        (deliver_awareness): the delivery is the framework's, never a second
        telling by the agent.
        """
        from cycle_runner import awareness_was_given, mark_awareness_given

        if self._scene_bootstrapped or awareness_was_given(self):
            return False

        groups = self._sm.resolve_active_groups()
        if not groups:
            print("  No active groups to bootstrap")
            return False

        # A place is an entity. The gate that opens a session has already
        # established that every character stands somewhere the world holds, so a
        # group whose place is not an entity is an inconsistency: it is left out
        # of what this invocation is for rather than staged.
        from world_manager.entity_core import get_entity as _ge
        groups = [g for g in groups if g.get("location") and _ge(g["location"]) is not None]
        if not groups:
            print("  No character stands anywhere the world holds")
            return False

        # Build pending list for validation
        pending_chars = []
        for g in groups:
            for cname in g.get("character_names", []):
                pending_chars.append((cname, g["location"]))

        print(f"  SM bootstrap: {len(pending_chars)} character(s) need awareness "
              f"across {len(groups)} location(s)")

        # Set close validation hook: every pending character must hold something
        # of this scene. Read from the ledger — the state the tools write — rather
        # than from the prose the scene manager happens to have written.
        from world_manager.tool_registry import set_close_validation_hook
        from scene_manager.awareness import ledger as _ledger

        def _validate_bootstrap_close():
            """Every character in the scene knows something of it."""
            if not pending_chars:
                return None
            book = _ledger()
            missing = [name for name, _ in pending_chars if not book.knows(name)]
            if missing:
                return (
                    "Bootstrap incomplete — these characters know nothing of the "
                    "scene they are standing in:\n"
                    + "\n".join(f"  - {name}" for name in missing)
                )
            return None

        set_close_validation_hook(_validate_bootstrap_close)

        try:
            # Run SM bootstrap
            self._sm.run_bootstrap()
        finally:
            set_close_validation_hook(None)

        self.deliver_awareness()

        mark_awareness_given(self)
        self._scene_bootstrapped = True
        return True

    def _enter_attempts(self, intents) -> Dict[str, str]:
        """Enter the attempts standing in the session, and say which piece each is.

        An attempt is evidence the session holds from the moment it stands, so it
        is entered here and reads under an id of its own from then on: the id the
        scene manager is shown beside the attempt, the id its resolution is
        marked under, and the id its perceivers are given it by.

        The words are the identity of the piece — an attempt already entered is
        the piece it was entered as, not a second one — so an attempt that stands
        across several cycles keeps one id for its whole life.
        """
        from scene_manager.awareness import attempt_piece, ledger as _ledger

        book = _ledger()
        out: Dict[str, str] = {}
        for d in intents or []:
            if not isinstance(d, dict):
                continue
            if str(d.get("status") or "active").strip() != "active":
                continue
            ident = str(d.get("id") or "").strip()
            text = attempt_piece(d.get("actor"), d.get("action_type"),
                                 d.get("target"), d.get("description"))
            if not ident or not text:
                continue
            out[ident] = book.id_of(text) or book.register(kind="action", content=text)
        return out

    def _enter_resolved_actions(
        self, entries, knowledge_by_intent: Optional[Dict[str, str]] = None,
    ) -> List[Any]:
        """Enter what the cycle resolved, so that each act stands as one piece.

        The attempt is already a piece — it was entered when it stood — and this
        is where its resolution is entered on that same piece: nothing is entered
        twice for one act, and the id the scene manager read beside the attempt is
        the id the act stands under afterwards.

        Who was there to notice it is a property of the resolution and is recorded
        with it; who is *told* of it is not decided here. The one hand that puts
        knowledge into a character's memory is the scene manager's own — what it
        gives by naming a piece — and an act that reached a character by itself
        would put another actor's declaration into his memory with nobody having
        decided that he noticed anything.

        Returns the pieces, as ``(id, text)``, in the order they resolved.
        """
        from scene_manager.awareness import attempt_piece, ledger as _ledger

        book = _ledger()
        known = dict(knowledge_by_intent or {})
        entered: List[Any] = []
        for entry in entries or []:
            if str(getattr(entry, "kind", "action") or "action") != "action":
                continue
            text = attempt_piece(
                getattr(entry, "actor", ""), getattr(entry, "action_type", "?"),
                getattr(entry, "target", "?"), getattr(entry, "description", ""),
            )
            if not text:
                continue
            intent_id = str(getattr(entry, "intent_id", "") or "").strip()
            record_id = known.get(intent_id) or book.id_of(text)
            if not record_id:
                record_id = book.register(kind="action", content=text)
            entered.append((record_id, text))
        return entered

    def _given_still_unread(self) -> Dict[str, List[str]]:
        """What each character has been told and has not read yet.

        A telling is not an interruption. What a character is given stands here
        until his next turn comes, and his turn is what reads it: the record is in
        his memory, and what waits is the turn he will have. Breaking into the
        cycle he is in is a separate hand — an attempt cut short — and it is not
        this one.
        """
        given = getattr(self, "_given", None)
        if given is None:
            given = self._given = {}
        return given

    def take_what_he_was_given(self, character_name: str) -> List[str]:
        """What this character was given since his last turn; his turn reads it."""
        return self._given_still_unread().pop(str(character_name or "").strip(), [])

    def deliver_awareness(self) -> int:
        """Hand every character the knowledge the session gave him.

        The ledger states who knows what; this is the act of giving it. Nothing is
        rewritten on the way — what the session entered is the text the character
        receives — and it lands in his own memory through the same pipeline every
        other record uses. It is delivered while the session runs, into the branch
        the character is already reading, so he has it in the session it belongs
        to and not a session later.

        Every delivery is also remembered for the turn it belongs to
        (_given_still_unread): what he was told is read when his next turn comes,
        and not in the middle of the turn he is in.
        """
        from history_pipeline import memory_at
        from memory_store import limits_from_env
        from scene_manager.awareness import ledger as _ledger

        book = _ledger()
        limits = limits_from_env()
        delivered = 0
        for player in self.world.get_players():
            name = str((player or {}).get("name") or "").strip()
            if not name:
                continue
            pending = book.pending(name)
            if not pending:
                continue
            pfile = self.world.game_root / "players" / name / "messages.json"
            memory = memory_at(pfile, limits=limits)
            for _rid, content in pending:
                # Marked: what stands in his memory as something he was told is
                # readable as such, and not as a record he wrote himself.
                memory.record(f"[account] {content}", kind="account")
                self._given_still_unread().setdefault(name, []).append(str(content))
            book.mark_delivered(name, [rid for rid, _ in pending])
            delivered += len(pending)
        if delivered:
            print(f"  Knowledge delivered: {delivered} piece(s)")
        return delivered

    # ── Cycle finalization ──

    def finalize_cycle(self, end_session: bool = False) -> bool:
        """Run the full resolution cycle. end_session=True publishes narration."""
        return self._do_finalize_cycle(end_session=end_session)

    #: How far a cycle reaches when nothing in the scene is going to change: a
    #: standing readiness decides nothing by itself, so time is allowed to move
    #: this far in the hope that something happens. It is the only span in the
    #: cycle that no intent named.
    STRUCTURAL_PAUSE_SECONDS = 900

    def _cycle_reach(self, intents: List[Any], cursor: int) -> int:
        """Where this pass of the cycle ends: the nearest moment the scene changes.

        The scene changes where an attempt begins or ends, so a pass reaches no
        further than the next such moment. An attempt that runs longer than the
        others is left running rather than closed with them, and the cycle that
        follows begins where this one ended; a scene that will not change at all
        gets the structural pause, and a scene holding nothing at all reaches
        nowhere, which is the cursor itself.
        """
        from scheduler.timeline import nearest_change

        change = nearest_change(
            intents, cursor, cursor + self.STRUCTURAL_PAUSE_SECONDS,
        )
        return change.at if change.at is not None else cursor

    def _advance_estimate(self, intents: List[Any], now_sec: int) -> Dict[str, Any]:
        """What the cycle does to the clock if the review leaves the pool alone.

        Read from the pool in front of the review, with the attempt standing at
        that moment named: the review is what decides the advance, so what it is
        offered is what its own pool amounts to.
        """
        from scheduler.timeline import nearest_change

        change = nearest_change(
            intents, now_sec, now_sec + self.STRUCTURAL_PAUSE_SECONDS,
        )
        return {
            "seconds": (change.at - now_sec) if change.at is not None else 0,
            "kind": change.kind,
            "by": list(change.by),
        }

    def _do_finalize_cycle(self, end_session: bool = False) -> bool:
        try:
            chars_data = self.world.get_players()
            if not chars_data:
                return False
        except Exception:
            return False

        location = str(getattr(self.world.get_world_time(), 'to_string', lambda: '')()) if hasattr(self, 'world') else ""
        start_time = self.world.get_world_time().to_string()

        def _char_plans_from(players_list):
            out = []
            for ch in players_list:
                if not isinstance(ch, dict):
                    continue
                out.append({
                    "name": str(ch.get("name") or ""),
                    "intent": "(active)",
                    "thoughts": "",
                })
            return out

        char_plans = _char_plans_from(chars_data)
        timeline_store = self.world.get_timeline()
        all_intents = timeline_store.get("intents") or []

        # Every attempt standing in the session is entered as the session's
        # knowledge before the review sees the pool: the id stands beside the
        # attempt in the phase message, and the same id is what its perceivers
        # are given it by when it resolves. Entered here rather than at the
        # resolution because an attempt is evidence from the moment it stands —
        # the scene manager has to be able to tell a character about it, and to
        # read back who has been told.
        knowledge_by_intent = self._enter_attempts(all_intents)

        # ── Compute intent status markers ──
        current_intent_keys: set = set()
        for d in all_intents:
            actor = str(d.get("actor", ""))
            atype = str(d.get("action_type", ""))
            desc = str(d.get("description", ""))[:80]
            current_intent_keys.add((actor, atype, desc))

        intent_statuses: Dict[str, str] = {}
        for d in all_intents:
            actor = str(d.get("actor", ""))
            atype = str(d.get("action_type", ""))
            desc = str(d.get("description", ""))[:80]
            k = (actor, atype, desc)
            if k not in self._prev_intent_keys:
                intent_statuses[k] = "new"
            else:
                intent_statuses[k] = "active"

        payload = {
            "character_plans": char_plans,
            "location": location,
            "cycle_start_time": start_time,
            "npcs": [],
            "declared_intents": all_intents,
            "intent_statuses": intent_statuses,
            "knowledge_ids": knowledge_by_intent,
        }
        context_text = f"Location: {location}\nTime: {start_time}\nCharacters: {[p['name'] for p in char_plans]}"

        from scheduler.timeline import (
            Intent, SegmentScheduler, format_resolved_history_for_sm,
            parse_duration_seconds,
        )
        from world.time import WorldTime as _WT

        def _dict_to_intent(d, *, anchor_sec=0):
            """One stored declaration as an attempt. Relative spans are placed on
            the anchor the declaration belongs to."""
            if not isinstance(d, dict) or not str(d.get("actor") or "").strip():
                return None
            if "_t_start_raw" in d or "_duration_raw" in d:
                d = dict(d)
                t_start = anchor_sec + parse_duration_seconds(d.pop("_t_start_raw", "0s"), default=0)
                duration_raw = d.pop("_duration_raw", "")
                t_end = None if not duration_raw else t_start + max(0, parse_duration_seconds(duration_raw, default=0))
                d["t_start"] = t_start
                d["t_end"] = t_end
            return Intent.from_dict(d)

        try:
            start_sec = _WT.parse(start_time).to_seconds()
        except Exception:
            start_sec = self.world.get_world_time().to_seconds()

        payload["cycle_start_seconds"] = start_sec

        # player_names is needed for SM resolve cycle (below)
        player_names = [str(c.get("name", "")) for c in chars_data if isinstance(c, dict)]
        payload["player_names"] = player_names

        # What this cycle will do to the clock, as the pool stands now. The
        # review is what decides it — it can take an attempt, change its span, cut
        # it or strike it out — so what is stated here is what the pool in front
        # of the agent already amounts to, not a promise about what it will
        # become: this is the advance, if the review leaves it alone.
        pool_now = [i for i in (_dict_to_intent(d, anchor_sec=start_sec) for d in all_intents)
                    if i is not None]
        payload["time_advance"] = self._advance_estimate(pool_now, start_sec)

        # ── The pool the review decides about, built before it speaks ──
        # What was carried over from the previous cycle and what was declared in
        # this one are the same list — take it once, or every intent resolves twice
        # under two different ids. It is built before the review rather than after
        # it because a decision taken in the middle of the review lands on this
        # pool: an attempt ended there, and what its owner decides in its place,
        # both stand in front of the review when it goes on to decide.
        carried_dicts = timeline_store.get("intents") or []
        intents_pool = []
        for d in carried_dicts:
            i = _dict_to_intent(d, anchor_sec=start_sec)
            if i is not None and i.status == "active":
                intents_pool.append(i)
        # The review's own hand on what runs: a change to an intent, a cut, or a
        # cancellation. Whatever it names lands on the intent it names before a
        # single second of it is resolved — this is the moment a declared attempt
        # is taken, changed or struck out.
        interrupted: Dict[str, List[str]] = {}

        def _review(patch: Dict[str, Any]) -> bool:
            """Land one review patch; False when it names no declared intent."""
            named = str(patch.get("id") or "")
            target = next((i for i in intents_pool if i.id == named), None)
            if target is None:
                return False
            mode = str(patch.get("_review") or "")
            if mode == "remove":
                target.status = "cancelled"
                return True
            if mode == "interrupt":
                offset = str(patch.get("_cut_after_raw") or "").strip()
                cut = start_sec + (parse_duration_seconds(offset, default=0) if offset else 0)
                if cut <= target.t_start:
                    # Cut before anything of it began: nothing of it happens.
                    target.status = "cancelled"
                else:
                    target.t_end = cut
                interrupted.setdefault(target.actor, []).append(str(patch.get("reason") or "").strip())
                return True
            fields = {k: v for k, v in patch.items() if not k.startswith("_") and k != "id"}
            duration_raw = str(patch.get("_duration_raw") or "").strip()
            if duration_raw:
                # A new span for the attempt, measured from where it began.
                fields["t_end"] = target.t_start + parse_duration_seconds(duration_raw, default=0)
            for k, v in fields.items():
                setattr(target, k, v)
            return True

        def _drain_review() -> None:
            """Land what the review decided on the pool it decided about.

            A review ends with its own decisions: a change to an attempt, a cut, a
            cancellation, or an intent authored for an NPC or the world. They are
            taken from the session's hand and put on the pool here.
            """
            from scene_manager.sm_tools import get_sm_intents

            for d in get_sm_intents():
                if isinstance(d, dict) and d.get("_patched"):
                    if not _review(d):
                        record_incident(
                            "intent_review:unknown_intent",
                            ValueError(
                                f"no declared intent carries the id {d.get('id')!r}"
                            ),
                        )
                    continue
                i = _dict_to_intent(d, anchor_sec=start_sec)
                if i is not None:
                    intents_pool.append(i)

        def _manage_and_call_back(iid: str, mode: str, cut_after: str, reason: str) -> str:
            """Take an attempt out of the review's hand, and let its owner decide again.

            The review does not wait for a later invocation to learn what a
            character does in place of the attempt it managed: he is called back,
            with the reason in front of him, from the moment the action leaves him
            standing at — the beginning of the cycle when the attempt is dropped
            before it happened, the point it was cut at otherwise. What he now
            attempts stands in the pool the review is looking at, under the id the
            review can go on to change or take. This is the answer the call itself
            gets.
            """
            target = next((i for i in intents_pool if i.id == iid), None)
            if target is None:
                return (f"ERROR: no attempt in front of this review carries the id {iid}.")
            actor = str(target.actor or "").strip()
            if mode == "remove":
                # Dropped before anything of it befell: the world stands where the
                # cycle opened, so that is the moment he decides again from.
                target.status = "cancelled"
                moment = start_sec
                happened = "was dropped"
                notice = f"What you understand now: {reason}"
            else:
                moment = start_sec + (parse_duration_seconds(cut_after, default=0)
                                      if cut_after else 0)
                if moment <= target.t_start:
                    # Cut before anything of it began: nothing of it happens, and
                    # the attempt leaves him standing where it found him.
                    target.status = "cancelled"
                    ended = "before any of it happened"
                    moment = target.t_start
                else:
                    target.t_end = moment
                    ended = f"after {cut_after}" if cut_after else "at once"
                happened = f"was cut short {ended}"
                notice = f"Your attempt was cut short: {reason}"
            if actor not in player_names:
                return (f"Attempt {iid} {happened}. No character is called back for it.")
            at = _WT.from_seconds(moment).to_string()
            standing_before = {str(d.get("id") or "")
                               for d in (self.world.get_timeline().get("intents") or [])
                               if isinstance(d, dict)}
            try:
                from cycle_runner import _run_single_player
                _run_single_player(
                    app=self, player_name=actor, scene_data={},
                    location=location, start_time=start_time,
                    npcs=[], players=player_names,
                    gm_notice=notice,
                    world_time_override=at,
                    establish_disposition=False,
                )
            except Exception as e:
                print(f"  Cut-back error for {actor}: {e}")
                record_incident("phase_failed:cut_callback", e, character=actor)
                return (f"Attempt {iid} {happened}. {actor} could not be reached to "
                        f"decide again: {e}")
            declared = [d for d in (self.world.get_timeline().get("intents") or [])
                        if isinstance(d, dict)
                        and str(d.get("id") or "") not in standing_before]
            if not declared:
                return (f"Attempt {iid} {happened}, and {actor} was called back at {at}. "
                        f"He stands where he stood: he attempts nothing new, and the "
                        f"pool is as it was but for that.")
            declared_intents: List[Dict[str, Any]] = []
            for d in declared:
                i = _dict_to_intent(d, anchor_sec=moment)
                if i is not None:
                    intents_pool.append(i)
                    declared_intents.append(i.to_dict())
            if not declared_intents:
                return (f"Attempt {iid} {happened}, and {actor} was called back at {at}. "
                        f"Nothing he declared stands in the pool.")
            # The attempt is entered where the pool takes it: the review read the
            # pool before it existed, and its resolution has to stand in a memory
            # that was told of it.
            self._sm.note_attempts_read(declared_intents, now_seconds=moment)
            knowledge_by_intent.update(self._enter_attempts(declared_intents))
            # What he decided is said the way the review reads its pool: his
            # attempts alone, each under the id it is acted by, how far it has
            # got, and the id it is known as knowledge by. The moment and the
            # advance stand under them, as they do at the review's opening.
            advance = self._advance_estimate(intents_pool, moment)
            lines = [f"Attempt {iid} {happened}. {actor} was called back and decided again."]
            lines.extend(self._sm.intents_changed_lines(
                actor, declared_intents, knowledge_by_intent,
                now_seconds=moment, marked={str(x) for x in (advance.get("by") or [])},
            ))
            lines.append(f"Current moment of time: {at}")
            # Where time would stop if nothing else changed moved with the pool.
            lines.extend(self._sm.state_advance(advance))
            return "\n".join(lines)

        # ── SM resolve cycle: review intents, declare NPC intents, modify/correct ──
        # Called before SegmentScheduler so SM-authored intents are included in resolution.
        print(f"  SM resolve cycle: {len(player_names)} player(s), {len(payload.get('declared_intents',[]))} intent(s)")
        from scene_manager.sm_tools import set_callback_resolver
        set_callback_resolver(_manage_and_call_back)
        try:
            self._sm.run_resolve_cycle(payload=payload)
        except Exception as e:
            print(f"  SM resolve cycle error: {e}")
        finally:
            # The hand is only held while a review is in front of it: elsewhere
            # managing an attempt is a request the phase's close puts on the pool.
            set_callback_resolver(None)

        # What the cycle decided the characters hold is handed over now, not at
        # the session's close: a character who was there when it happened reads
        # about it in the session it happened in. It is a telling and not an
        # interruption — he reads it at his next turn, and the cycle runs on.
        self.deliver_awareness()

        now_sec = max(start_sec, self.world.get_world_time().to_seconds())
        MAX_RESOLUTION_ROUNDS = 10
        all_actors = list(player_names)

        def _awareness_fn(observer, subject):
            return True

        _drain_review()
        # The review has had its hand on the pool — an attempt taken out, one cut
        # short, one authored — and what the cycle will do to the clock if nothing
        # else changes moves with it. Stated again only when it moved.
        self._sm.note_time_advance(self._advance_estimate(intents_pool, start_sec))

        seen_action_ids = set()
        cycle_resolved_history = []
        npc_trigger_notes = []
        round_num = 0
        char_reinvokes = {}

        while round_num < MAX_RESOLUTION_ROUNDS:
            round_num += 1
            horizon = self._cycle_reach(intents_pool, now_sec)
            sched = SegmentScheduler(
                intents_pool, now=now_sec, horizon=horizon,
                perception_gate=self._sm.perception_check,
                awareness_fn=_awareness_fn, all_actors=all_actors,
                seen_action_ids=seen_action_ids,
            )
            result = sched.run()
            cycle_resolved_history.extend(result.resolved_history)
            # What the round brought to pass stands before the scene manager now,
            # where it happened — not gathered at the session's close.
            self._sm.note_resolved(result.resolved_history)
            seen_action_ids.update(e.intent_id for e in result.resolved_history if e.kind == "action" and e.intent_id)
            for w in result.warnings:
                pass
            intents_pool = result.carry_over_intents
            now_sec = result.next_cursor
            char_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a in player_names}
            npc_reinvokes = {a: notes for a, notes in result.reinvoke.items() if a not in player_names}
            for a, notes in npc_reinvokes.items():
                npc_trigger_notes.extend(f"{a}: {n}" for n in notes)
            if round_num == 1:
                # Whoever the review cut short is called back the way a trigger
                # calls a character back: the reason first, then his own decision.
                for actor, notes in interrupted.items():
                    said = "; ".join(n for n in notes if n)
                    if not said:
                        continue
                    if actor in player_names:
                        char_reinvokes.setdefault(actor, []).append(
                            f"Your attempt was cut short: {said}"
                        )
                    else:
                        npc_trigger_notes.append(f"{actor}: cut short — {said}")
            if not char_reinvokes:
                break
            reinvoke_time_str = _WT.from_seconds(now_sec).to_string()

            def _standing_ids() -> set:
                """What the timeline holds, by id, as it stands right now."""
                try:
                    store = self.world.get_timeline()
                except Exception:
                    return set()
                return {str(d.get("id") or "")
                        for d in (store.get("intents") or []) if isinstance(d, dict)}

            def _declared_since(standing: set) -> List[Dict[str, Any]]:
                """What stands in the timeline now that did not stand before."""
                try:
                    store = self.world.get_timeline()
                except Exception:
                    return []
                return [d for d in (store.get("intents") or [])
                        if isinstance(d, dict)
                        and str(d.get("id") or "") not in standing]

            for cname, notes in char_reinvokes.items():
                combined_ctx = "\n".join(f"- {n}" for n in notes) or "New information available."
                standing_before = _standing_ids()
                try:
                    from cycle_runner import _run_single_player
                    _run_single_player(
                        app=self, player_name=cname, scene_data={},
                        location=location, start_time=start_time,
                        npcs=[], players=player_names,
                        gm_notice=combined_ctx,
                        world_time_override=reinvoke_time_str,
                        establish_disposition=False,
                    )
                except Exception as e:
                    print(f"  Re-invoke error for {cname}: {e}")
                    record_incident("phase_failed:reinvoke", e, character=cname)
                declared = _declared_since(standing_before)
                if not declared:
                    # A character called back to decide again may decide to stand
                    # where he stood. Nothing changed, so nothing is said of it.
                    continue
                for d in declared:
                    i = _dict_to_intent(d, anchor_sec=now_sec)
                    if i is not None:
                        intents_pool.append(i)
                # The attempt is entered where the pool takes it, because the
                # review read the pool before it existed: without this its
                # resolution would stand in the memory of an attempt nobody was
                # ever told of.
                self._sm.note_attempts_read(declared, now_seconds=now_sec)
                # What he decided stands in the pool from here — and it stands
                # there for this agent too, under the ids it will act by: the one
                # the attempt is known as knowledge by, and the one it carries in
                # a review. Said for him, and only for what changed.
                knowledge_by_intent.update(self._enter_attempts(declared))
                self._sm.note_intents_changed(
                    cname, declared, knowledge_by_intent, now_seconds=now_sec,
                )
                # And what the cycle will do to the clock if nothing else changes
                # moves with the pool: stated again, if it moved.
                self._sm.note_time_advance(self._advance_estimate(intents_pool, now_sec))
            timeline_store = self.world.get_timeline()

        if round_num >= MAX_RESOLUTION_ROUNDS:
            raise RuntimeError(
                f"SegmentScheduler: exhausted {MAX_RESOLUTION_ROUNDS} rounds at {location}"
            )

        # What this cycle resolved is entered as the pieces it resolved, under the
        # ids the scene manager read beside the attempts: one act, one piece, one
        # id, from the moment it stood to the moment it is told of. Who was there
        # to notice stands with the resolution; who is told is the scene manager's
        # own act, and this hands on whatever it gave.
        self._enter_resolved_actions(cycle_resolved_history, knowledge_by_intent)
        self.deliver_awareness()

        consecutive_zero = int(timeline_store.get("consecutive_zero", 0))
        if now_sec <= start_sec and not cycle_resolved_history:
            # A cycle that resolved nothing and moved nothing: counted, and left
            # at that. Time is where the resolution reached, and a resolution that
            # reached nowhere leaves the clock standing — a stride of its own would
            # be time moving over a scene that named no moment of it, and the
            # scene manager is told the advance before it decides, so what it is
            # told has to be what the cycle does.
            consecutive_zero += 1
        else:
            consecutive_zero = 0

        # The world clock is where the resolution reached. Time that moved in the
        # timeline has moved in the world, so the next cycle, the next session and
        # every record written between them read the same now — a session that is
        # still open has no business holding the clock back until it publishes.
        if now_sec > start_sec:
            self.world.set_world_time(_WT.from_seconds(now_sec).to_string())

        # Update statuses from resolved history (distinguish kind)
        for entry in cycle_resolved_history:
            actor = str(getattr(entry, "actor", ""))
            atype = str(getattr(entry, "action_type", ""))
            desc = str(getattr(entry, "description", ""))[:80]
            k = (actor, atype, desc)
            kind = str(getattr(entry, "kind", "action"))
            if kind == "prepared_reaction":
                intent_statuses[k] = "triggered"
            else:
                intent_statuses[k] = "completed"

        # Mark cancelled intents
        for d in all_intents:
            if str(d.get("status") or "").strip() == "cancelled":
                k = (str(d.get("actor","")), str(d.get("action_type","")), str(d.get("description",""))[:80])
                intent_statuses[k] = "cancelled"

        self._prev_intent_keys = current_intent_keys

        if cycle_resolved_history:
            self._sm.add_resolved_segments_to_buffer(cycle_resolved_history)
        buffer_ready = self._sm.is_session_buffer_ready() or self._sm.is_session_buffer_force()

        if not cycle_resolved_history and not char_reinvokes and not npc_trigger_notes:
            try:
                self.world.set_timeline({
                    "intents": [i.to_dict() for i in intents_pool],
                    "resolved_history": timeline_store.get("resolved_history", []),
                    "consecutive_zero": consecutive_zero,
                    "cursor": now_sec,
                })
            except Exception:
                pass
            return True

        if not buffer_ready and not end_session:
            try:
                self.world.set_timeline({
                    "intents": [i.to_dict() for i in intents_pool],
                    "resolved_history": (timeline_store.get("resolved_history", []) +
                                         [e.to_dict() for e in cycle_resolved_history])[-500:],
                    "consecutive_zero": consecutive_zero,
                    "cursor": now_sec,
                })
            except Exception:
                pass
            return True

        duration_sec = now_sec - start_sec
        cycle_end_time_str = _WT.from_seconds(now_sec).to_string()

        try:
            prior_history = timeline_store.get("resolved_history") or []
            merged_history = (prior_history + [e.to_dict() for e in cycle_resolved_history])[-500:]
            self.world.set_timeline({
                "intents": [i.to_dict() for i in intents_pool],
                "resolved_history": merged_history,
                "consecutive_zero": consecutive_zero,
                "cursor": now_sec,
            })
        except Exception:
            pass

        char_plans = _char_plans_from(chars_data)
        payload["character_plans"] = char_plans
        carry_over = timeline_store.get("intents") or []
        active_carry = [i for i in carry_over if isinstance(i, dict) and str(i.get("status") or "active") == "active"]
        payload["declared_intents"] = active_carry
        timeline_text = format_resolved_history_for_sm(cycle_resolved_history, start_sec)
        if npc_trigger_notes:
            timeline_text += "\n\nNPC/world triggers:\n" + "\n".join(f"  - {n}" for n in npc_trigger_notes)
        payload["timeline_text"] = timeline_text
        payload["computed_duration"] = f"{duration_sec}s"
        payload["cycle_end_time"] = cycle_end_time_str

        return True

    def run_full_session(self) -> None:
        """Run cycles until the session's buffer is full enough, then close it.

        Each cycle says where the session stands: which cycle it was, and how much
        of the session's buffer is filled — that number is what decides when the
        session closes, so a reader of the run can see how near that is.
        """
        for cycle in range(1, 51):
            run_cycle(self, None)
            held = self._sm.session_buffer_chars()
            limit = self._sm.session_buffer_threshold()
            print(f"  cycle {cycle} — session buffer {held}/{limit}")
            if self._sm.is_session_buffer_ready() or self._sm.is_session_buffer_force():
                break
        # end_session is called by the caller (_run_session.py) —
        # do NOT call it here to avoid double narration.

    def begin_session(self) -> None:
        """Start a new session: SM + character contexts (§1.1)."""
        # Nothing is left unread into a new session: what he was told is where his
        # memory put it, and his first turn of the session reads his memory.
        self._given_still_unread().clear()
        try:
            self._sm.begin_session()
        except Exception as e:
            record_incident("sm.begin_session", e)
        try:
            from player.agent import begin_session
            begin_session()
        except Exception as e:
            record_incident("player.begin_session", e)
        self._session_active = True

    def end_session(self) -> None:
        """End session: per-character summary, save to disk, clear memory."""
        # Use accumulated session context — do NOT rebuild from storage.
        try:
            wt = self.world.get_world_time()
            time_str = wt.to_string() if hasattr(wt, 'to_string') else str(wt)
            char_names = []
            try:
                for ch in self.world.get_players():
                    if isinstance(ch, dict):
                        n = str(ch.get("name") or "").strip()
                        if n:
                            char_names.append(n)
            except Exception:
                pass
            payload = {
                "character_plans": [],
                "location": time_str,
                "cycle_start_time": time_str,
                "declared_intents": [],
                "player_names": char_names,
            }
            context_text = f"World time: {time_str}"
            result = self._sm.run_session_summary(payload=payload, context_text=context_text)
            narrative = str(result.get("narrative") or "")
            if narrative:
                print(f"  Session narrative: {len(narrative)} characters")
            else:
                print("  Session narrative: nothing recorded")
        except Exception as e:
            print(f"  Session summary error: {e}")
        # From here the session is closed one memory at a time: the scene
        # manager's, then every character's. Each merge may be followed by a
        # summary call covering what the merge dropped, so each one says so.
        print("  Closing the memories...")
        try:
            self._sm.end_session()
        except Exception as e:
            print(f"  end_session error: {e}")
            record_incident("sm.end_session", e)
        # Whatever the session stated a character holds is handed over before his
        # own memory closes: a record that was decided in this session belongs to
        # it, and the session is not over until it has reached him.
        self.deliver_awareness()
        try:
            from player.agent import end_session
            end_session()
        except Exception as e:
            record_incident("player.end_session", e)
        try:
            from player.agent import set_sm_resolver
            set_sm_resolver(None)
        except Exception as e:
            record_incident("player.set_sm_resolver", e)
        self._session_active = False

        print("  Session closed — no session work remains.")
        report = format_incident_report()
        if report:
            print("\n" + report)

    # ── Job registration ──

    def _register_scheduled_jobs(self) -> None:
        register_all_jobs(self._scheduler, self)

    # ── Scheduled job handlers (called by tick scheduler) ──

    def _run_character_reflection(self, *, character_name: str) -> None:
        """Run reflection for a character (scheduled job)."""
        try:
            from player.reflection import run_reflection
            run_reflection(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] reflection error for {character_name}: {e}")
            record_incident("job_failed:reflection", e, character=character_name)

    def _run_character_diary(self, *, character_name: str) -> None:
        """Run diary update for a character (scheduled job)."""
        try:
            from player.reflection import run_diary
            run_diary(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] diary error for {character_name}: {e}")
            record_incident("job_failed:diary", e, character=character_name)

    def _run_character_review(self, *, character_name: str) -> None:
        """Run relationship review for a character (scheduled job)."""
        try:
            from player.relationship_review import run_relationship_review
            run_relationship_review(character_name=character_name)
        except Exception as e:
            if logs_enabled():
                print(f"[scheduler] review error for {character_name}: {e}")
            record_incident("job_failed:review", e, character=character_name)

    # ── Clear cycle temp dir ──

    def _clear_cycle_temp_dir(self) -> None:
        tmp = self.world.game_root / "tmp_cycle"
        if tmp.exists():
            import shutil
            shutil.rmtree(str(tmp))
        tmp.mkdir(parents=True, exist_ok=True)


# Backward-compatible alias
GameOrchestrator = Orchestrator

# Backward-compatible method aliases
Orchestrator._cmd_load_preset = Orchestrator.load_preset
Orchestrator._maybe_create_world_setting = Orchestrator.ensure_world_setting
