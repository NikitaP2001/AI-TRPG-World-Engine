"""World Manager agent — highest-level world agent.

Creates the world setting block at startup via an auto-task.
Prompt is aimed at future world simulation management.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from memory_store import append_message, load_history, limits_from_env
from openrouter_langchain_logging import logs_enabled, _safe_label
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from gm.react_loop import coerce_tool_calls, react_loop_iteration


WORLD_SETTING_FIELDS = [
    "world_essence",
    "gurps_calibration",
    "initial_world_time",
]


# ======================================================================
# Invocation log helper
# ======================================================================


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
        self._last_call_tick: int = 0
        self._last_summary: str = ""

    def create_world_setting(
        self,
        characters: List[Dict[str, Any]],
        plot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Auto-task: generate world setting block based on characters + plot.

        Uses a dedicated forced-tool-call, separate from the prompt.
        Returns the parsed world setting dict, or {} on failure.
        """
        from world_manager.tools import world_setting_result

        llm = build_openrouter_chat_llm(
            temperature=float(self._temperature),
            streaming=True,
            title_suffix="-world-manager-init",
            max_tokens=3000,
            parallel_tool_calls=False,
            timeout=120.0,
        )

        callbacks = openrouter_logging_callbacks(scope="world_manager", label="create_setting")

        # Build rich context with full character cards + plot text
        import json as _json
        char_blocks = []
        for c in characters:
            name = str(c.get("name") or c.get("general", "") or "?").strip()
            block = f"[Character: {name}]\n"
            block += _json.dumps(c, ensure_ascii=False, indent=2)
            char_blocks.append(block)

        plot_text = str(plot.get("init") or plot.get("premise") or plot.get("description", "") or "").strip()

        human_msg = (
            "Generate the initial world setting for this world.\n"
            "---\n"
            "## Characters\n"
            + ("\n\n".join(char_blocks) if char_blocks else "(none)") + "\n\n"
            "## Plot\n"
            f"{plot_text or '(none)'}\n\n"
            "Return a JSON object with these three keys:\n\n"
            "1. world_essence (string):\n"
            "   A dense paragraph (3-5 sentences) describing the world's essential nature.\n"
            "   Cover: what the baseline reality is like, the prevailing tech/magic mix,\n"
            "   the general state of civilisation (fragmented, stable, declining, rising),\n"
            "   and the texture of daily life. Use abstract terms only — no proper nouns.\n\n"
            "2. gurps_calibration (object):\n"
            "   A structured reference block defining world setting.\n"
            "   Where:\n"
            "   - tech_mana_baseline (string): Describe the intersection of technology and\n"
            "     magic. Use relative terms \n"
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
            "       - societal_impact: How the supernatural shapes economics, politics,\n"
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
            "3. initial_world_time (string):\n"
            "   EXACT format Y0000-01-01 00:00 (no prose). Must be meaningful,\n"
            "   not year 0000/0001.\n"
            "Use abstract framing throughout. No proper nouns, no numeric ranges.\n"
        )

        messages = [
            SystemMessage(content=self._prompt_text),
            HumanMessage(content=human_msg),
        ]

        try:
            bound_llm = llm.bind_tools(
                [world_setting_result],
                tool_choice="required",
            ).with_config({"callbacks": callbacks})
        except TypeError:
            bound_llm = llm.bind_tools([world_setting_result]).with_config({"callbacks": callbacks})

        max_retries = 3
        required_top_level = {"world_essence", "gurps_calibration", "initial_world_time"}
        required_gurps_keys = {"tech_mana_baseline", "power_tiers", "supernatural_spectrum"}
        required_tier_keys = {"stat_range", "advantage_budget", "skill_ceiling"}
        required_spectrum_keys = {"frequency", "essence", "ceiling", "societal_impact"}

        for _attempt in range(max_retries):
            try:
                out = bound_llm.invoke(messages)
            except Exception:
                continue

            tool_calls = coerce_tool_calls(getattr(out, "tool_calls", None) or [])
            if not tool_calls:
                messages.append(AIMessage(content="(text output suppressed)"))
                messages.append(HumanMessage(content="ERROR: You MUST call world_setting_result."))
                continue

            tc = tool_calls[0]
            args = tc.get("args") if isinstance(tc, dict) else {}
            if not isinstance(args, dict):
                args = {}

            raw_json = str(args.get("setting_json") or "").strip()
            if not raw_json:
                messages.append(out)
                messages.append(HumanMessage(content="ERROR: setting_json is empty. Provide JSON."))
                continue

            try:
                parsed = json.loads(raw_json)
            except Exception as e:
                messages.append(out)
                messages.append(HumanMessage(content=f"ERROR: Invalid JSON: {e}"))
                continue

            if not isinstance(parsed, dict):
                continue

            # --- Validate top-level keys ---
            missing_top = [f for f in required_top_level if f not in parsed]
            if missing_top:
                messages.append(out)
                messages.append(HumanMessage(
                    content=f"ERROR: Missing top-level keys: {', '.join(missing_top)}"
                ))
                continue

            # world_essence must be non-empty
            essence = str(parsed.get("world_essence") or "").strip()
            if not essence:
                messages.append(out)
                messages.append(HumanMessage(content="ERROR: world_essence is empty."))
                continue

            # gurps_calibration must be a dict with required sub-keys
            gc = parsed.get("gurps_calibration")
            if not isinstance(gc, dict):
                messages.append(out)
                messages.append(HumanMessage(content="ERROR: gurps_calibration must be a JSON object."))
                continue
            missing_gc = [k for k in required_gurps_keys if k not in gc]
            if missing_gc:
                messages.append(out)
                messages.append(HumanMessage(
                    content=f"ERROR: gurps_calibration missing keys: {', '.join(missing_gc)}"
                ))
                continue

            # power_tiers must be an array with at least "average", "exceptional", "unique"
            tiers = gc.get("power_tiers")
            if not isinstance(tiers, list) or len(tiers) < 2:
                messages.append(out)
                messages.append(HumanMessage(
                    content="ERROR: power_tiers must be an array with at least 2 entries."
                ))
                continue
            tier_names = set()
            for i, t in enumerate(tiers):
                if not isinstance(t, dict):
                    messages.append(out)
                    messages.append(HumanMessage(content=f"ERROR: power_tiers[{i}] is not an object."))
                    continue
                name = str(t.get("tier") or "").strip().lower()
                tier_names.add(name)
                missing_tier = [k for k in required_tier_keys if k not in t]
                if missing_tier:
                    messages.append(out)
                    messages.append(HumanMessage(
                        content=f"ERROR: power_tiers[{i}] missing keys: {', '.join(missing_tier)}"
                    ))
                    continue
            if "average" not in tier_names or "unique" not in tier_names:
                messages.append(out)
                messages.append(HumanMessage(
                    content="ERROR: power_tiers must include at least 'average' and 'unique' tiers."
                ))
                continue

            # supernatural_spectrum must have required keys
            ss = gc.get("supernatural_spectrum")
            if not isinstance(ss, dict):
                messages.append(out)
                messages.append(HumanMessage(content="ERROR: supernatural_spectrum must be a JSON object."))
                continue
            missing_ss = [k for k in required_spectrum_keys if k not in ss]
            if missing_ss:
                messages.append(out)
                messages.append(HumanMessage(
                    content=f"ERROR: supernatural_spectrum missing keys: {', '.join(missing_ss)}"
                ))
                continue

            # --- Validate initial_world_time ---
            wt = str(parsed.get("initial_world_time") or "").strip()
            # Auto-prefix "Y" if looks like YYYY-MM-DD
            if wt[:1].isdigit() and len(wt) >= 5 and wt[4:5] == "-":
                wt = f"Y{wt}"
                parsed["initial_world_time"] = wt
            # Fallback: try to extract YYYY from narrative text
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
                    messages.append(HumanMessage(content="ERROR: initial_world_time must be meaningful, not year 0000/0001"))
                    continue
            except Exception:
                messages.append(out)
                messages.append(HumanMessage(
                    content=f"ERROR: initial_world_time format invalid. "
                    f"Must be a calendar date/time like \"Y1479-03-15 18:00\" — "
                    f"do NOT use prose descriptions. Examples: "
                    f"\"Y1479-03-15 18:00\", \"Y1372-01-01 12:00\". "
                    f"Current value: {wt!r}"
                ))
                continue

            # Save to history
            if self._history_path:
                limits = limits_from_env()
                append_message(self._history_path, role="user", content="WORLD_SETTING creation", limits=limits)
                append_message(
                    self._history_path,
                    role="assistant",
                    content=json.dumps(parsed, ensure_ascii=False, indent=2),
                    limits=limits,
                )

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
                parallel_tool_calls=False,
            )

        from world_manager.tools import (
            ready_to_proceed,
            answer_world_question,
            read_tool_doc,
            set_world_orientation,
            set_player_start,
            finalize_world_generation,
            define_world_concept,
            alter_feature,
            define_entity,
            define_faction,
            define_rule,
            declare_world_state,
            subscribe_to_events,
            query_world_state,
            resolve_contradiction,
            post_intent,
            post_entity_directive,
            define_relationship,
        )

        all_tools = tools or [
            ready_to_proceed,
            answer_world_question,
            read_tool_doc,
            set_world_orientation,
            set_player_start,
            finalize_world_generation,
            define_world_concept,
            alter_feature,
            define_entity,
            define_faction,
            define_rule,
            declare_world_state,
            subscribe_to_events,
            query_world_state,
            resolve_contradiction,
            post_intent,
            post_entity_directive,
            define_relationship,
        ]

        tools_by_name = {t.name: t for t in all_tools}
        llm_with_tools = llm.bind_tools(all_tools).with_config({
            "callbacks": openrouter_logging_callbacks(scope="world_manager", label="call_wm"),
        })

        # ── Build intro message ────────────────────────────────────
        intro_parts = [f"[call_wm: {notice}]"]

        # Elapsed time since last call
        elapsed_days = 0
        if context and context.get("world_time"):
            # Orchestrator provides days_since_last_call
            elapsed_days = context.get("days_since_last_call", 0)

        if elapsed_days > 0:
            intro_parts.append(f"World time advanced {elapsed_days} days since last call.")

        # Reason for this call
        if notice == "initialize_world":
            intro_parts.append("Reason: first invocation — world initialization.")
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
            intro_parts.append(f"\n[last_summary]\n{self._last_summary}")

        human_msg = "\n".join(intro_parts)

        # ── Build remaining messages ────────────────────────────────

        # Persistent context
        persistent = []
        if context and context.get("world_registry"):
            persistent.append(SystemMessage(
                content=f"[world_registry]\n{json.dumps(context['world_registry'], indent=2)}"
            ))
        if context and context.get("world_orientation"):
            persistent.append(SystemMessage(
                content=f"[world_orientation]\n{json.dumps(context['world_orientation'], indent=2)}"
            ))

        # History: previous WM write-tool calls (reads are NOT saved)
        history_msgs: List[BaseMessage] = []
        if self._history_path and self._history_path.exists():
            history = load_history(self._history_path)
            for h in history:
                role = (h.get("role") or "").strip().lower()
                content = str(h.get("content") or "")
                if role == "user":
                    history_msgs.append(HumanMessage(content=content))
                elif role == "assistant":
                    history_msgs.append(AIMessage(content=content))

        # Invocation context (fresh each call)
        invocation = []
        if context:
            if context.get("subscription_trigger"):
                invocation.append(SystemMessage(
                    content=f"[subscription_trigger]\n{json.dumps(context['subscription_trigger'], indent=2)}"
                ))
            if context.get("event_summary"):
                invocation.append(SystemMessage(
                    content=f"[event_summary]\n{context['event_summary']}"
                ))
            if context.get("pending_events"):
                invocation.append(SystemMessage(
                    content=f"[pending_events]\n{json.dumps(context['pending_events'], indent=2)}"
                ))
            if context.get("faction_snapshot"):
                invocation.append(SystemMessage(
                    content=f"[faction_snapshot]\n{json.dumps(context['faction_snapshot'], indent=2)}"
                ))
            if context.get("world_debt"):
                invocation.append(SystemMessage(
                    content=f"[world_debt]\n{json.dumps(context['world_debt'], indent=2)}"
                ))
            if context.get("active_intents"):
                invocation.append(SystemMessage(
                    content=f"[active_intents]\n{json.dumps(context['active_intents'], indent=2)}"
                ))

        # ── Assemble and invoke ──────────────────────────────────────

        messages = [
            SystemMessage(content=self._prompt_text),
            *persistent,
            *history_msgs,
            *invocation,
            HumanMessage(content=human_msg),
        ]

        result = react_loop_iteration(
            messages,
            llm_with_tools,
            tools_by_name,
            termination_tools={"ready_to_proceed", "answer_world_question"},
            readonly_tools={
                "query_world_state", "read_tool_doc",
            },
        )

        # ── Extract thinking ─────────────────────────────────────────

        thinking_parts = []
        for m in result["messages"]:
            t = str(getattr(m, "type", "") or "").strip().lower()
            content = str(getattr(m, "content", "") or "").strip()
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

        # ── Persist to history (only write-tools, not reads) ────────
        WRITE_TOOLS = {
            "set_world_orientation", "set_player_start",
            "finalize_world_generation",
            "define_world_concept", "alter_feature",
            "define_entity", "define_faction", "define_rule",
            "declare_world_state", "subscribe_to_events",
            "resolve_contradiction", "post_intent",
            "post_entity_directive", "define_relationship",
        }

        if self._history_path:
            limits = limits_from_env()
            # Save the intro as user message
            append_message(self._history_path, role="user", content=human_msg, limits=limits)

            # Extract exit args from result messages
            exit_tool_name = result.get("exit_tool", "")
            exit_tool_args = result.get("exit_args") or {}

            # Save write-tool calls from the ReAct loop (chronological order)
            for m in result["messages"]:
                t = str(getattr(m, "type", "") or "").strip().lower()
                if t in {"ai", "assistant"}:
                    tcalls = getattr(m, "tool_calls", None) or []
                    for tc in tcalls:
                        name = str(tc.get("name", "") or "")
                        if name in WRITE_TOOLS:
                            args = tc.get("args", {}) or {}
                            args_str = " ".join(f"{k}={v}" for k, v in args.items() if k not in ("world_summary", "fallback_interval_days"))
                            line = f"[{name}] {args_str}"[:500]
                            append_message(
                                self._history_path, role="assistant",
                                content=line, limits=limits,
                            )

            # Save terminal tool result AFTER write-tools (chronological end)
            if exit_tool_name == "ready_to_proceed":
                summary = exit_tool_args.get("world_summary", "") or ""
                if summary:
                    append_message(
                        self._history_path, role="assistant",
                        content=f"[world_summary] {summary}", limits=limits,
                    )
                    self._last_summary = summary
                else:
                    append_message(
                        self._history_path, role="assistant",
                        content="[ready_to_proceed] (no summary)", limits=limits,
                    )
            elif exit_tool_name == "answer_world_question":
                q = exit_tool_args.get("question", "") or ""
                a = exit_tool_args.get("answer", "") or ""
                if q and a:
                    append_message(
                        self._history_path, role="assistant",
                        content=f"[qa] Q: {q}  A: {a}", limits=limits,
                    )

        # ── Update last_call_tick ───────────────────────────────────
        if context and context.get("current_tick"):
            self._last_call_tick = int(context["current_tick"])

        return {
            "exit_tool": result.get("exit_tool"),
            "exit_args": result.get("exit_args"),
            "thinking": thinking,
        }
