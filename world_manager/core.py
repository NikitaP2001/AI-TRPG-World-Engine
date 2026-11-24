"""World Manager agent — highest-level world agent.

Creates the world setting block at startup via an auto-task.
Prompt is aimed at future world simulation management.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from history_pipeline import memory_at
from memory_store import load_history
from record_voice import spoken
from openrouter_langchain_logging import logs_enabled, _safe_label
from openrouter_llm import bind_agent_tools, build_openrouter_chat_llm, extract_reasoning, openrouter_logging_callbacks, read_prompt_text
from gm.react_loop import coerce_tool_calls, react_loop_iteration, sanitize_messages
from engine.history_meta import HistoryMeta as WMHistoryMeta
from engine.invocation import opening


WORLD_SETTING_FIELDS = [
    "world_essence",
    "gurps_calibration",
    "initial_world_time",
]

#: A notice that names only the mode of the wake. It says nothing a later reader
#: can use, so it is not a record: the invocation itself is kept in the metadata.
_BARE_NOTICES = {"initialize_world"}


# ======================================================================
# Invocation log helper
# ======================================================================


def _game_root() -> Path:
    """Where the world lives, anchored where the code lives.

    Anchored rather than relative to the working directory: the World Manager
    reads the preset's characters from here, and a start from another directory
    would show it a world with nothing in it.
    """
    return Path(__file__).resolve().parent.parent / "game"


def _invocation_log_dir() -> Path:
    """Directory for full prompt+output logs per WM invocation."""
    base = Path("logs") / "wm_invocations"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _dump_invocation(
    tag: str,
    prompt_text: str,
    all_messages: List[BaseMessage],
    raw_result: dict,
    thinking: str,
) -> None:
    """Dump full WM invocation (prompt + messages + result + thinking) to a log file."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    safe_tag = _safe_label(tag)[:40]
    log_path = _invocation_log_dir() / f"wm_{ts}_{safe_tag}.json"

    msgs_serializable = []
    for m in all_messages:
        entry = {"role": type(m).__name__, "content": str(m.content)}
        if hasattr(m, "tool_calls") and m.tool_calls:
            entry["tool_calls"] = [
                {"name": tc.get("name", ""), "args": tc.get("args", {})}
                for tc in m.tool_calls
            ]
        msgs_serializable.append(entry)

    log = {
        "tag": tag,
        "timestamp": ts,
        "prompt_text": prompt_text,
        "messages": msgs_serializable,
        "result": {
            "exit_tool": raw_result.get("exit_tool"),
            "exit_args": raw_result.get("exit_args"),
        },
        "thinking": thinking,
    }

    try:
        log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ======================================================================


class WorldManager:
    """World Manager agent — defines and maintains the core world setting.

    Two invocation modes:
      create_world_setting()  — startup: generates world essence/GURPS/time
      call_wm()               — subscription-triggered: responds to world events
    """

    def __init__(
        self,
        *,
        prompt_path: str = "agents/world_manager/prompt.txt",
        history_path: Optional[Path] = None,
        temperature: float = 0.7,
    ) -> None:
        self._prompt_text = read_prompt_text(prompt_path)
        self._temperature = temperature
        self._history_path = history_path
        self._peer_history_path: Optional[Path] = None
        self._last_summary: str = ""
        self._meta = WMHistoryMeta(
            (self._history_path.parent / "wm_history_meta.json")
            if self._history_path else Path("wm_history_meta.json"),
        )

    def create_world_setting(
        self,
        characters: List[Dict[str, Any]],
        plot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate world seed: essence, GURPS calibration, initial time, optional player reference.

        One forced LLM call (not ReAct loop). Returns parsed dict or {} on failure.
        """
        from world_manager.tools import world_setting_result

        llm = build_openrouter_chat_llm(
            temperature=float(self._temperature),
            streaming=True,
            title_suffix="-world-manager-init",
            max_tokens=3000,
            parallel_tool_calls=True,
            timeout=120.0,
            thinking=True,  # world building is deliberation
            effort="low",
        )

        callbacks = openrouter_logging_callbacks(scope="world_manager", label="create_setting")

        # Build character blocks
        import json as _json
        char_blocks = []
        for c in characters:
            name = str(c.get("name") or c.get("general", "") or "?").strip()
            block = f"[Character: {name}]\n"
            block += _json.dumps(c, ensure_ascii=False, indent=2)
            char_blocks.append(block)

        plot_text = str(plot.get("init") or plot.get("premise") or plot.get("description", "") or "").strip()

        human_msg = (
            "Generate the world seed for this world.\n"
            "---\n"
            "## Characters\n"
            + ("\n\n".join(char_blocks) if char_blocks else "(none)") + "\n\n"
            "## Plot\n"
            f"{plot_text or '(none)'}\n\n"
            "Return a JSON object with these keys:\n\n"
            "1. world_essence (string): REQUIRED.\n"
            "   A dense paragraph (3-5 sentences) describing the world's essential nature.\n"
            "   Cover: what the baseline reality is like, the prevailing tech/magic mix,\n"
            "   the general state of civilisation (fragmented, stable, declining, rising),\n"
            "   and the texture of daily life. Use abstract terms only — no proper nouns.\n\n"
            "2. gurps_calibration (object): REQUIRED.\n"
            "   A structured reference block defining world setting.\n"
            "   Where:\n"
            "   - tech_mana_baseline (string): Describe the intersection of technology\n"
            "     and magic. Use relative terms.\n"
            "   - power_tiers (array of exactly 3 objects): tiers named \"average\",\n"
            "     \"exceptional\", \"unique\". Each has:\n"
            "       - tier: \"average\" / \"exceptional\" / \"unique\"\n"
            "       - stat_range: Describe from frail to robust / typical to extraordinary.\n"
            "       - advantage_budget: Describe how many defining traits or notable flaws\n"
            "         are typical — from minimal to defining.\n"
            "       - skill_ceiling: Describe the breadth and depth of competence — from\n"
            "         trade-specialist to near-superhuman breadth.\n"
            "     Use relative descriptors throughout — no numeric ranges.\n\n"
            "   - supernatural_spectrum (object):\n"
            "       - frequency: rare / uncommon / common / ubiquitous\n"
            "       - essence: What supernatural forces are — a natural resource? divine\n"
            "         gift? technology? fundamental law?\n"
            "       - ceiling: Abstract bound of power\n"
            "       - societal_impact: How the supernatural shapes economics, politics.\n"
            "   - social_fabric (object, optional):\n"
            "       - government_types: list of abstract descriptors\n"
            "       - law_level: description of how order is maintained\n"
            "       - social_mobility: how easy it is to rise in station\n"
            "       - wealth_disparity: distribution of resources\n"
            "   - cosmic_context (object, optional):\n"
            "       - planar_structure: single / layered / infinite\n"
            "       - deities: active / distant / absent / unknown\n"
            "       - afterlife: known / speculated / unknown / none\n"
            "       - notes: anything else about the cosmos\n"
            "3. initial_world_time (string): REQUIRED.\n"
            "   EXACT format Y0000-01-01 00:00 (no prose). Must be meaningful,\n"
            "   not year 0000/0001.\n"
            "Use abstract framing throughout. No proper nouns, no numeric ranges.\n"
        )

        messages = [
            SystemMessage(content=self._prompt_text),
            HumanMessage(content=human_msg),
        ]

        bound_llm = bind_agent_tools(llm, [world_setting_result], callbacks=callbacks)

        max_retries = 3
        required_top_level = {"world_essence", "gurps_calibration", "initial_world_time"}
        required_gurps_keys = {"tech_mana_baseline", "power_tiers", "supernatural_spectrum"}
        required_tier_keys = {"stat_range", "advantage_budget", "skill_ceiling"}
        required_spectrum_keys = {"frequency", "essence", "ceiling", "societal_impact"}

        def _reject(note: str, call_id: str = "") -> None:
            """Answer the call that failed the way a tool result answers a call.

            A call answered by a user message is not a conversation this backend
            accepts, so the retry would be refused for a second reason while the
            first was still unfixed — and the world would never get built.
            """
            # A world that is refused three times and says nothing about why is
            # a wall with no door: whoever reads the console has to guess.
            if logs_enabled():
                print(f"[trace] world setting not accepted: {note[:300]}")
            if call_id:
                messages.append(ToolMessage(content=note, tool_call_id=call_id,
                                            name="world_setting_result"))
            else:
                messages.append(HumanMessage(content=note))

        for _attempt in range(max_retries):
            try:
                out = bound_llm.invoke(sanitize_messages(messages))
            except Exception as exc:
                from engine.incidents import record_incident

                record_incident("wm.create_world_setting", exc)
                # The rejection reports itself; the attempt needs no second line.
                _reject(f"[error] the request failed: {type(exc).__name__}: {exc}")
                continue

            tool_calls = coerce_tool_calls(getattr(out, "tool_calls", None) or [])
            if not tool_calls:
                _reject("[error] No world_setting_result call was made.")
                continue

            tc = tool_calls[0]
            call_id = str(tc.get("id") or "")
            args = tc.get("args") if isinstance(tc, dict) else {}
            if not isinstance(args, dict):
                args = {}

            raw_json = str(args.get("setting_json") or "").strip()
            if not raw_json:
                messages.append(out)
                _reject("ERROR: setting_json is empty. Provide JSON.", call_id)
                continue

            try:
                parsed = json.loads(raw_json)
            except Exception as e:
                messages.append(out)
                _reject(f"ERROR: Invalid JSON: {e}. The argument carries one JSON "
                        f"object and nothing after it.", call_id)
                continue

            if not isinstance(parsed, dict):
                messages.append(out)
                _reject("ERROR: setting_json must be one JSON object.", call_id)
                continue

            # --- Validate top-level keys ---
            missing_top = [f for f in required_top_level if f not in parsed]
            if missing_top:
                messages.append(out)
                _reject(f"ERROR: Missing top-level keys: {', '.join(missing_top)}", call_id)
                continue

            # world_essence must be non-empty
            essence = str(parsed.get("world_essence") or "").strip()
            if not essence:
                messages.append(out)
                _reject("ERROR: world_essence is empty.", call_id)
                continue

            # gurps_calibration must be a dict with required sub-keys
            gc = parsed.get("gurps_calibration")
            if not isinstance(gc, dict):
                messages.append(out)
                _reject("ERROR: gurps_calibration must be a JSON object.", call_id)
                continue
            missing_gc = [k for k in required_gurps_keys if k not in gc]
            if missing_gc:
                messages.append(out)
                _reject(f"ERROR: gurps_calibration missing keys: {', '.join(missing_gc)}", call_id)
                continue

            # power_tiers must have at least "average" and "unique"
            problem = ""
            tiers = gc.get("power_tiers")
            if not isinstance(tiers, list) or len(tiers) < 2:
                problem = "power_tiers must be an array with at least 2 entries."
            else:
                tier_names = set()
                for i, t in enumerate(tiers):
                    if not isinstance(t, dict):
                        problem = f"power_tiers[{i}] must be a JSON object."
                        break
                    tier_names.add(str(t.get("tier") or "").strip().lower())
                    missing_tier = [k for k in required_tier_keys if k not in t]
                    if missing_tier:
                        problem = f"power_tiers[{i}] missing keys: {', '.join(missing_tier)}"
                        break
                if not problem and ("average" not in tier_names or "unique" not in tier_names):
                    problem = "power_tiers must include at least 'average' and 'unique' tiers."
            if problem:
                messages.append(out)
                _reject(f"ERROR: {problem}", call_id)
                continue

            # supernatural_spectrum must have required keys
            ss = gc.get("supernatural_spectrum")
            if not isinstance(ss, dict):
                messages.append(out)
                _reject("ERROR: supernatural_spectrum must be a JSON object.", call_id)
                continue
            missing_ss = [k for k in required_spectrum_keys if k not in ss]
            if missing_ss:
                messages.append(out)
                _reject(f"ERROR: supernatural_spectrum missing keys: {', '.join(missing_ss)}", call_id)
                continue

            # --- Validate initial_world_time ---
            wt = str(parsed.get("initial_world_time") or "").strip()
            if wt[:1].isdigit() and len(wt) >= 5 and wt[4:5] == "-":
                wt = f"Y{wt}"
                parsed["initial_world_time"] = wt
            if not wt.startswith("Y") or "-" not in wt:
                import re as _re
                _year_m = _re.search(r"(?:^|\D)(\d{4})\s*(?:DR)?(?:\D|$)", wt)
                if _year_m:
                    _y = int(_year_m.group(1))
                    if 100 < _y < 10000:
                        wt = f"Y{_y}-01-01 12:00"
                        parsed["initial_world_time"] = wt
            try:
                from world import WorldTime
                parsed_wt = WorldTime.parse(wt)
                if parsed_wt.year <= 1:
                    messages.append(out)
                    _reject("ERROR: initial_world_time must be meaningful, not year 0000/0001", call_id)
                    continue
            except Exception:
                messages.append(out)
                _reject(
                    f"ERROR: initial_world_time format invalid. "
                    f"Must be a calendar date/time like \"Y1479-03-15 18:00\". "
                    f"Current value: {wt!r}",
                    call_id,
                )
                continue

            # --- Save to history ---
            if self._history_path:
                own_memory = memory_at(self._history_path)
                own_memory.record("WORLD_SETTING creation")
                own_memory.record("[world_setting]\n" + json.dumps(parsed, ensure_ascii=False, indent=2))

            if logs_enabled():
                print(f"[trace] World setting created: {essence[:80]}")

            return parsed

        return {}

    def load_world_setting(self) -> Optional[Dict[str, Any]]:
        """Load the persisted world setting from disk."""
        if not self._history_path:
            return None
        try:
            setting_path = self._history_path.parent / "world_setting.json"
            raw = setting_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def save_world_setting(self, setting: Dict[str, Any]) -> None:
        """Persist the world setting to disk."""
        if not self._history_path:
            return
        try:
            setting_path = self._history_path.parent / "world_setting.json"
            setting_path.parent.mkdir(parents=True, exist_ok=True)
            setting_path.write_text(
                json.dumps(setting, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── ReAct-based invocation (world events / subscription triggers) ──

    def initialize_world(
        self,
        *,
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """First-time WM invocation to define concepts, factions, rules, entities.

        Args:
            context: Optional dict with characters, plot info.
            tools: Override tool list (defaults to all WM tools).

        Returns:
            Dict with "exit_tool", "exit_args", "thinking".
        """
        return self.call_wm(
            notice="initialize_world",
            context=context,
            tools=tools,
        )

    def set_peer_history(self, path: Optional[Path]) -> None:
        """Name the memory this agent reports its changes into (the GM's)."""
        self._peer_history_path = path

    def call_wm(
        self,
        *,
        notice: str,
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        llm: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Invoke WM with a notice via ReAct loop. WM uses tools until termination.

        Args:
            notice: Why WM was called.
            context: Optional dict with subscription_trigger, event_summary,
                     pending_events, faction_snapshot, world_debt, etc.
            tools: Override tool list (defaults to all WM tools).
            llm: Optional LLM instance.

        Returns:
            Dict with: "exit_tool" (str|None), "exit_args" (dict|None), "thinking" (str).
        """
        if llm is None:
            llm = build_openrouter_chat_llm(
                temperature=float(self._temperature),
                streaming=True,
                title_suffix="-world-manager",
                max_tokens=4000,
                parallel_tool_calls=True,
                thinking=True,  # world building is deliberation
                effort="low",
            )

        from world_manager.tool_registry import ToolRegistry

        all_tools = tools or ToolRegistry.for_wm()

        tools_by_name = {t.name: t for t in all_tools}
        llm_with_tools = bind_agent_tools(
            llm, all_tools,
            callbacks=openrouter_logging_callbacks(scope="world_manager", label="call_wm"),
        )

        # ── Build intro message ────────────────────────────────────
        intro_parts = [f"[call_wm: {notice}]"]

        # Elapsed time since this agent was last called, from its own record.
        # The interval fallback is measured from the same record, so a call the
        # clock made and a call a trigger made read the same way here.
        elapsed_days = 0
        world_time_str = str((context or {}).get("world_time") or "").strip()
        if world_time_str:
            from engine.agent_schedule import last_call_tick
            since = last_call_tick("wm")
            if since > 0:
                try:
                    from world.time import WorldTime
                    gap = WorldTime.parse(world_time_str).to_seconds() - since
                    elapsed_days = max(0, gap // (24 * 60 * 60))
                except Exception:
                    elapsed_days = 0

        if elapsed_days > 0:
            intro_parts.append(f"World time advanced {elapsed_days} days since last call.")

        # Reason for this call
        if notice == "initialize_world":
            # Inject character descriptions from preset files + plot
            from world.io import _read_json as _read_json_file
            game_root = _game_root()
            char_dir = (game_root / "players").resolve()
            char_blocks = []
            plot_text = ""
            if char_dir.exists():
                for pdir in sorted(char_dir.iterdir()):
                    if not pdir.is_dir():
                        continue
                    desc_file = pdir / "description.json"
                    if desc_file.exists():
                        desc = _read_json_file(desc_file)
                        if isinstance(desc, dict):
                            name = desc.get("name") or pdir.name
                            block = f"[Character: {name}]\n{json.dumps(desc, ensure_ascii=False, indent=2)}"
                            char_blocks.append(block)
            # Read plot from setups if available
            try:
                plot_path = self._history_path.parent / "setups" / "plot.json" if self._history_path else game_root / "setups" / "plot.json"
                if plot_path and plot_path.exists():
                    from world.io import read_json as _read_json_plot
                    plot_data = _read_json_plot(str(plot_path)) or {}
                    plot_text = str(plot_data.get("init") or plot_data.get("premise") or plot_data.get("description", "") or "")
            except Exception:
                pass
            intro_parts.append("## Characters")
            if char_blocks:
                intro_parts.append("\n\n".join(char_blocks))
            else:
                intro_parts.append("(none)")
            if plot_text:
                intro_parts.append(f"\n## Plot\n{plot_text}")
            intro_parts.append("\nCreate location entities for each unique character starting location. Do NOT create player character entities — they are handled separately.")
        elif notice == "fallback_tick":
            intro_parts.append("Reason: fallback interval expired — no subscribed events fired.")
        elif context and context.get("subscription_trigger"):
            st = context["subscription_trigger"]
            intro_parts.append("Reason: subscription match.")
            intro_parts.append(
                "[trigger]\n"
                f"  filter_id: {st.get('filter_id', '?')}\n"
                f"  event_type: {st.get('event_type', '?')}\n"
                f"  timing: {st.get('timing', 'after')}\n"
                f"  payload: {json.dumps(st.get('payload', {}), ensure_ascii=False)}"
            )
        else:
            intro_parts.append(f"Reason: {notice}.")

        # Last summary from previous invocation
        if self._last_summary:
            intro_parts.append(f"\n## Last Summary\n{self._last_summary}")

        human_msg = opening(intro_parts, moment=world_time_str)

        # ── Build remaining messages ────────────────────────────────

        # Persistent context
        persistent = []
        # Add per-agent summaries from PinnedContext
        try:
            from engine.pinned_context import PinnedContext
            _ctx = PinnedContext(self._history_path, self._world)
            persistent.extend(
                _ctx.add_wm_summaries()
                    .build_persistent()
            )
        except Exception:
            pass
        if context and context.get("world_registry"):
            persistent.append(SystemMessage(
                content=f"## World Registry\n{json.dumps(context['world_registry'], indent=2)}"
            ))
        if context and context.get("world_orientation"):
            persistent.append(SystemMessage(
                content=f"## World Orientation\n{json.dumps(context['world_orientation'], indent=2)}"
            ))

        # History: previous WM write-tool calls (reads are NOT saved)
        history_msgs: List[BaseMessage] = []
        if self._history_path and self._history_path.exists():
            for h in load_history(self._history_path):
                role = (h.get("role") or "").strip().lower()
                # Stored entries are records the framework authored, so they are
                # read back as records whatever role they were written with, and
                # in the world's voice: the markers are the code's.
                said = spoken(str(h.get("content") or ""))
                if role in ("user", "assistant") and said.strip():
                    history_msgs.append(HumanMessage(content=said))

        # Invocation context (fresh each call)
        invocation = []
        if context:
            if context.get("subscription_trigger"):
                invocation.append(SystemMessage(
                    content=f"## Subscription Trigger\n{json.dumps(context['subscription_trigger'], indent=2)}"
                ))
            if context.get("event_summary"):
                invocation.append(SystemMessage(
                    content=f"## Event Summary\n{context['event_summary']}"
                ))
            if context.get("pending_events"):
                invocation.append(SystemMessage(
                    content=f"## Pending Events\n{json.dumps(context['pending_events'], indent=2)}"
                ))
            if context.get("faction_snapshot"):
                invocation.append(SystemMessage(
                    content=f"## Faction Snapshot\n{json.dumps(context['faction_snapshot'], indent=2)}"
                ))
            if context.get("world_debt"):
                invocation.append(SystemMessage(
                    content=f"## World Debt\n{json.dumps(context['world_debt'], indent=2)}"
                ))
            if context.get("active_intents"):
                invocation.append(SystemMessage(
                    content=f"## Active Intents\n{json.dumps(context['active_intents'], indent=2)}"
                ))

        # ── Assemble and invoke ──────────────────────────────────────

        # Set up close-invocation validation for initialize_world:
        # check that location entities exist for each character's starting location.
        from world_manager.tool_registry import set_close_validation_hook
        _starting_locations: set = set()
        if notice == "initialize_world":
            try:
                from world.io import _read_json as _read_json_file
                char_dir = (_game_root() / "players").resolve()
                if char_dir.exists():
                    for pdir in sorted(char_dir.iterdir()):
                        if not pdir.is_dir():
                            continue
                        desc_file = pdir / "description.json"
                        if desc_file.exists():
                            desc = _read_json_file(desc_file)
                            if isinstance(desc, dict):
                                loc = str(desc.get("location", "")).strip()
                                if loc:
                                    _starting_locations.add(loc)
            except Exception:
                pass

        def _validate_wm_close() -> Optional[str]:
            if not _starting_locations:
                return None
            try:
                from world_manager.entity_core import query_entities as _qe
                existing = {e.get("id", "") for e in _qe(include_templates=False, limit=200)}
                missing = [loc for loc in sorted(_starting_locations) if loc not in existing]
                if missing:
                    return (f"WM initialization incomplete — missing location entities:\n"
                            + "\n".join(f"  - {loc}" for loc in missing)
                            + "\n\nCreate each missing location with set_entity(type=\"location\", ...).")
            except Exception:
                pass
            return None

        set_close_validation_hook(_validate_wm_close)

        messages = [
            SystemMessage(content=self._prompt_text),
            *persistent,
            *history_msgs,
            *invocation,
            HumanMessage(content=human_msg),
        ]

        from engine.change_summary import ChangeCollector, append_change_record
        changes = ChangeCollector()

        # The invocation is a branch of the WM's memory, opened when it wakes and
        # closed by the termination tool that ends its work.
        own_memory = memory_at(self._history_path) if self._history_path else None
        if own_memory is not None:
            own_memory.open_branch()

        result = react_loop_iteration(
            messages,
            llm_with_tools,
            tools_by_name,
            termination_tools={"invoke_self", "close_invocation"},
            readonly_tools={
                "get_entity", "get_entity_facts", "get_entity_state",
                "query_entities", "query_events",
            },
            changes=changes,
        )

        # ── Extract thinking ─────────────────────────────────────────

        thinking_parts = []
        for m in result["messages"]:
            t = str(getattr(m, "type", "") or "").strip().lower()
            content = str(getattr(m, "content", "") or "").strip() or extract_reasoning(m)
            if t in {"ai", "assistant"} and content:
                thinking_parts.append(content)

        thinking = "\n\n".join(thinking_parts)

        # ── Log full invocation ──────────────────────────────────────

        _dump_invocation(
            tag=notice,
            prompt_text=self._prompt_text,
            all_messages=messages,
            raw_result=result,
            thinking=thinking,
        )

        # ── Persist to history ──────────────────────────────────────
        # In the order the invocation lived: the notice it was woken with, its
        # deliberation, and what it wrote through its tools. The intro is per-call
        # context — characters, plot, trigger — and is rebuilt for every call, so
        # persisting it would duplicate facts that the next call states again; a
        # notice naming only the mode is the same kind of non-news.

        if self._history_path:
            if str(notice or "").strip().lower() not in _BARE_NOTICES:
                own_memory.record(f"[call_wm: {notice}]")

            # What the manager reasoned is kept as a record of its own deliberation.
            if thinking:
                own_memory.record(f"[reasoning]\n{thinking}")

            # What it wrote through its tools, in the order it wrote it. Without it
            # the memory keeps the deliberation and loses the deed, and the next
            # wake reads its own past as thinking that led nowhere.
            for m in result["messages"]:
                body = str(getattr(m, "content", "") or "").strip()
                if body.startswith("[written]"):
                    own_memory.record(body)

            # What this invocation changed, in outline: one aggregated record
            # instead of the calls that caused it, sent to the Game Master — the
            # authority below this one — so what the world gained reaches the
            # campaign's memory. Not kept here: the World Manager made these
            # changes itself in this invocation, so a copy of them in its own
            # memory tells it what it already knows, and pushes out what it was
            # told by others.
            changes_block = changes.build("wm")
            append_change_record(self._peer_history_path, changes_block)

            # Extract exit args from result messages
            exit_tool_name = result.get("exit_tool", "")
            exit_tool_args = result.get("exit_args") or {}

            # Save terminal tool result (chronological end)
            if exit_tool_name == "invoke_self":
                comment = exit_tool_args.get("comment", "") or ""
                if comment:
                    own_memory.record(f"[invoke_self] {comment}")
            elif exit_tool_name == "write_correction":
                msg = exit_tool_args.get("message", "") or ""
                if msg:
                    own_memory.record(f"[correction] {msg[:200]}")
            # The termination tool ended the wake: the branch closes with it, and
            # what it kept is merged into the thread.
            own_memory.close_branch()

        from world_manager.tool_registry import set_close_validation_hook as _clear_hook
        _clear_hook(None)

        # ── Record this invocation ──────────────────────────────────
        # In world time and in ticks, persisted: what the cycle's wake-up drain
        # and the elapsed-time line above are both measured from.
        from engine.agent_schedule import record_call
        record_call("wm")

        # What the trims just dropped is still in this invocation's own context,
        # so the paragraph about it is written from here, before the next call
        # opens on a history that starts mid-story.
        self._cover_retired_history(history_msgs=history_msgs, pinned=persistent)

        # ── Summarization triggers (every 10 invocations, every 10 paragraphs) ──
        inv_count = self._meta.increment_invocation()
        if inv_count > 0 and inv_count % 10 == 0:
            self._run_paragraph_summary()

        para_count = self._meta.paragraph_count
        if para_count > 0 and para_count % 10 == 0 and inv_count % 10 != 0:
            self._run_arc_summary()

        return {
            "exit_tool": result.get("exit_tool"),
            "exit_args": result.get("exit_args"),
            "thinking": thinking,
        }

    def _cover_retired_history(self, *, history_msgs, pinned) -> None:
        """Turn what a trim dropped into a paragraph, while it is still in context.

        ``history_msgs`` is the history as this invocation saw it, ``pinned`` the
        blocks above it. The request reuses both, so its prefix is the one this
        invocation already paid for and only the task at its end is new.
        """
        if not self._history_path:
            return
        try:
            from engine.history_retirement import dropped_from, pending_retirement, retire
            from world.agent_story import append_paragraph

            fell_out = dropped_from(self._history_path, history_msgs)
            if not fell_out and not pending_retirement(self._history_path):
                return

            root = self._history_path.parent

            def _write(name, summary):
                append_paragraph(
                    summaries_path=root / "wm_summaries.json",
                    story_path=root / "wm_story.json",
                    name=name or "World Summary",
                    summary=summary,
                )

            if retire(
                self._history_path,
                dropped=fell_out,
                prefix=list(pinned or []),
                history_messages=history_msgs,
                prompt_text=self._prompt_text,
                temperature=self._temperature,
                scope="world_manager",
                write=_write,
                kind="paragraph",
                label="wm_history_retirement",
            ):
                self._meta.append_entry("auto_injection", "[wm_history_retirement]")
                if logs_enabled():
                    print("[trace] WM covered retired history")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] WM history retirement failed: {e}")

    def _run_paragraph_summary(self) -> None:
        """Run WM paragraph summary (every 10 invocations).

        Writes to wm_story.json + wm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_paragraph
            from engine.summarizer import SummaryRunner

            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="world_manager",
            )
            last_ref = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[wm_paragraph_summary_task]\n"
                "Summarize the world state since \"{last_ref}\".\n"
                "Below are the events since then (auto-injected context excluded):\n\n"
                "{refs}\n\n"
                "Write a paragraph with a short title (3-10 words) and 5-10 sentence summary.\n"
                "Respond with JSON: {\"name\": \"short title\", \"summary\": \"your summary\"}.",
                last_ref=last_ref,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-world-manager-paragraph",
                label="wm_paragraph_summary",
            )
            if result:
                pname, summary = result
                pname = pname or "World Summary"
                root = self._history_path.parent
                append_paragraph(
                    summaries_path=root / "wm_summaries.json",
                    story_path=root / "wm_story.json",
                    name=pname,
                    summary=summary,
                )
                self._meta.append_entry("auto_injection", f"[wm_paragraph:{pname}]")
                self._meta.mark_summarized(pname)
                self._meta.increment_paragraph()
                if logs_enabled():
                    print(f"[trace] WM paragraph summary: '{pname}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] WM _run_paragraph_summary error: {e}")

    def _run_arc_summary(self) -> None:
        """Run WM arc summary (every 10 paragraphs).

        Writes to wm_story.json + wm_summaries.json (NOT messages.json).
        """
        if not self._history_path:
            return
        try:
            from world.agent_story import append_arc
            from engine.summarizer import SummaryRunner

            runner = SummaryRunner(
                self._meta, self._history_path, self._prompt_text,
                temperature=self._temperature, scope="world_manager",
            )
            last_ref = self._meta.last_summarized_paragraph or "(start)"
            task_prompt = runner.build_task_prompt(
                "[wm_arc_summary_task]\n"
                "Since arc summary after paragraph \"{last_ref}\", "
                "10 new paragraphs have been summarized.\n"
                "Write an arc-level summary covering all these paragraphs.\n"
                "Respond with JSON: {\"name\": \"arc title\", \"summary\": \"your summary\"}.",
                last_ref=last_ref,
            )
            result = runner.run_summary(
                task_prompt=task_prompt,
                title_suffix="-world-manager-arc",
                label="wm_arc_summary",
            )
            if result:
                arc_name, summary = result
                arc_name = arc_name or "Arc Summary"
                root = self._history_path.parent
                append_arc(
                    summaries_path=root / "wm_summaries.json",
                    story_path=root / "wm_story.json",
                    name=arc_name,
                    summary=summary,
                )
                self._meta.append_entry("auto_injection", f"[wm_arc:{arc_name}]")
                if logs_enabled():
                    print(f"[trace] WM arc summary: '{arc_name}' ({len(summary)} chars)")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] WM _run_arc_summary error: {e}")
