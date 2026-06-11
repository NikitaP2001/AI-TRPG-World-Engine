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
from openrouter_langchain_logging import logs_enabled
from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text
from gm.react_loop import coerce_tool_calls


WORLD_SETTING_FIELDS = [
    "world_essence",
    "gurps_calibration",
    "initial_world_time",
]


class WorldManager:
    """World Manager agent — defines and maintains the core world setting."""

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
