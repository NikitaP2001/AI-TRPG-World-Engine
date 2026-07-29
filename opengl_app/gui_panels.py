"""GUI Panels — Dear ImGui panels for LLM World.

Each panel is a function that renders one UI section.
Panels are assembled in opengl_app/main.py.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import imgui

from opengl_app.gui_bridge import GuiBridge as B


# ── Style ──

_COL_BG = (0.06, 0.09, 0.16, 1.0)       # slate-900
_COL_SURFACE = (0.12, 0.16, 0.23, 1.0)  # slate-800
_COL_SURFACE2 = (0.20, 0.24, 0.33, 1.0) # slate-700
_COL_BORDER = (0.28, 0.33, 0.37, 1.0)   # slate-600
_COL_TEXT = (0.95, 0.97, 1.0, 1.0)      # slate-100
_COL_TEXT2 = (0.58, 0.64, 0.72, 1.0)    # slate-400
_COL_ACCENT = (0.23, 0.51, 0.96, 1.0)   # blue-500
_COL_DANGER = (0.94, 0.27, 0.27, 1.0)   # red-500
_COL_SUCCESS = (0.13, 0.77, 0.37, 1.0)  # green-500
_COL_WARN = (0.96, 0.62, 0.04, 1.0)     # amber-500
_COL_HP = (0.13, 0.77, 0.37, 1.0)       # green
_COL_HP_LOW = (0.94, 0.27, 0.27, 1.0)   # red
_COL_FP = (0.23, 0.51, 0.96, 1.0)       # blue


def _health_color(ratio: float):
    if ratio > 0.5:
        return _COL_HP
    elif ratio > 0.25:
        return _COL_WARN
    return _COL_HP_LOW


def _init_style(scale: float = 1.0):
    """Apply dark theme style, optionally scaled for HiDPI displays.

    Args:
        scale: DPI scaling factor (1.0 = normal, 2.0 = 4K/Retina).
            Obtained from glfw.get_window_content_scale().
    """
    style = imgui.get_style()
    style.colors[imgui.COLOR_WINDOW_BACKGROUND] = _COL_SURFACE
    style.colors[imgui.COLOR_TITLE_BACKGROUND] = _COL_BG
    style.colors[imgui.COLOR_TITLE_BACKGROUND_ACTIVE] = _COL_BG
    style.colors[imgui.COLOR_FRAME_BACKGROUND] = _COL_SURFACE2
    style.colors[imgui.COLOR_TEXT] = _COL_TEXT
    style.colors[imgui.COLOR_BORDER] = _COL_BORDER
    style.colors[imgui.COLOR_BUTTON] = _COL_SURFACE2
    style.colors[imgui.COLOR_HEADER] = _COL_SURFACE2
    # Style dimensions � scale for HiDPI
    style.window_rounding = 4.0 * scale
    style.frame_rounding = 3.0 * scale
    style.child_rounding = 2.0 * scale
    style.popup_rounding = 2.0 * scale
    style.grab_rounding = 2.0 * scale
    style.scrollbar_rounding = 9.0 * scale
    style.scrollbar_size = 14.0 * scale
    style.grab_min_size = 10.0 * scale
    style.indent_spacing = 21.0 * scale
    style.columns_min_spacing = 6.0 * scale
    style.frame_border_size = 1.0 * scale
    style.window_border_size = 1.0 * scale
    style.child_border_size = 1.0 * scale
    style.popup_border_size = 1.0 * scale
    style.mouse_cursor_scale = max(1.0, scale)


# ── Helpers ──

def _icon(entity: dict) -> str:
    """Determine display icon from data shape, not type string.

    Rules (first match wins):
      - position.coords exists → world map (#)
      - position.inside exists → inside something (+)
      - gurps exists (non-empty) → creature (@)
      - stats.members exists → faction (&)
      - type ends with _template → template (?)
      - default → bullet (.)
    """
    pos = entity.get("position") or {}
    if pos.get("coords"):
        return "#"
    if pos.get("inside") or pos.get("near"):
        return "+"
    if entity.get("gurps"):
        return "@"
    stats = entity.get("stats") or {}
    if stats.get("members"):
        return "&"
    etype = str(entity.get("type", "") or "")
    if etype.endswith("_template"):
        return "?"
    return "."


def _hp_bar(label: str, current: float, maximum: float, width: float = 80):
    """Draw a compact HP/FP bar."""
    if maximum <= 0:
        return
    ratio = max(0, min(1, current / maximum))
    color = _health_color(ratio)
    draw_list = imgui.get_window_draw_list()
    cursor = imgui.get_cursor_screen_pos()
    height = 14.0
    # Background
    draw_list.add_rect_filled(cursor.x, cursor.y, cursor.x + width, cursor.y + height,
                              imgui.get_color_u32_rgba(0.1, 0.1, 0.1, 0.5))
    # Fill
    draw_list.add_rect_filled(cursor.x, cursor.y, cursor.x + width * ratio, cursor.y + height,
                              imgui.get_color_u32_rgba(*color))
    # Text
    text = f"{label} {int(current)}/{int(maximum)}"
    text_w = imgui.calc_text_size(text).x
    text_x = cursor.x + (width - text_w) * 0.5
    imgui.set_cursor_screen_pos((text_x, cursor.y))
    imgui.text_colored(text, *_COL_TEXT)
    imgui.set_cursor_screen_pos((cursor.x, cursor.y + height + 2))


# ══════════════════════════════════════════════════════════════════
# PANELS
# ══════════════════════════════════════════════════════════════════


# ── Scene View ──

_scene_expanded: Dict[str, bool] = {}
_scene_input: str = ""
_scene_char_inputs: Dict[str, str] = {}


def panel_scene_view():
    """Active scene view: chat, HP bars, NPCs, character input."""
    imgui.begin("Scene", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    scene = B.get_active_scene()
    if not scene:
        imgui.text_colored("No active scene", *_COL_TEXT2)
        imgui.end()
        return

    wt = B.get_world_time()
    loc_name = scene.get("location", "?")
    imgui.text(f"Location: {loc_name}  |  Time: {wt}")
    imgui.separator()

    # ── Characters present ──
    chars = scene.get("players", {})
    if isinstance(chars, dict):
        imgui.text("Characters:")
        imgui.indent(10)
        for cname in chars:
            state_data = B.get_entity_state(cname)
            st = state_data.get("state", {}) if state_data else {}
            hp = st.get("hp", 0)
            mhp = st.get("max_hp", hp)
            fp = st.get("fp", 0)
            mfp = st.get("max_fp", fp)
            imgui.text(f"{cname}")
            imgui.same_line()
            _hp_bar("HP", hp, mhp, 80)
            imgui.same_line()
            _hp_bar("FP", fp, mfp, 80)
            conds = st.get("conditions", [])
            if conds:
                imgui.same_line()
                imgui.text_colored(f"[{', '.join(conds[:2])}]", *_COL_WARN)
        imgui.unindent(10)

    imgui.separator()

    # ── NPCs present ──
    npc_names = scene.get("npcs", [])
    if npc_names:
        imgui.text("NPCs:")
        imgui.indent(10)
        for nname in npc_names[:10]:
            facts = B.get_entity_facts(nname) if isinstance(nname, str) else ""
            summary = facts[:80] if facts else str(nname)
            imgui.text(f"{summary}")
        imgui.unindent(10)

    imgui.separator()

    # ── Character input ──
    if isinstance(chars, dict) and chars:
        char_names = list(chars.keys())
        current_char = _scene_char_inputs.get("_selected", char_names[0])
        if imgui.begin_combo("Character", current_char):
            for cn in char_names:
                if imgui.selectable(cn, cn == current_char)[0]:
                    _scene_char_inputs["_selected"] = cn
            imgui.end_combo()

        input_key = f"_input_{current_char}"
        if input_key not in _scene_char_inputs:
            _scene_char_inputs[input_key] = ""

        changed, val = imgui.input_text_multiline(
            "##action", _scene_char_inputs.get(input_key, ""), 1024,
            -1, 60, imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
        if changed:
            _scene_char_inputs[input_key] = val

        if imgui.button("Send (Ctrl+Enter)", 120, 28):
            text = _scene_char_inputs.get(input_key, "").strip()
            if text:
                # Record as user input event
                B.record_event(
                    description=f"[player] {current_char}: {text}",
                    entity_ids=[current_char],
                    tag="player_input")
                _scene_char_inputs[input_key] = ""

    imgui.end()


# ── Entity Browser ──

_entity_filter: str = ""
_entity_type_filter: str = "all"


def panel_entity_browser(on_select):
    """Right panel: searchable, filterable entity list."""
    global _entity_filter, _entity_type_filter
    imgui.begin("Entities", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    # Search
    changed, _entity_filter = imgui.input_text("Search", _entity_filter, 128)
    search = _entity_filter.strip()

    # Type filter
    # Build dynamic type list from actual entities in store (no hardcoded types)
    try:
        all_entities = B.query_entities(limit=200)
        types_set = sorted(set(e.get("type", "?") for e in all_entities if e.get("type")))
        types = ["all"] + types_set
    except Exception:
        types = ["all"]
    if imgui.begin_combo("Type", _entity_type_filter):
        for t in types:
            if imgui.selectable(t, t == _entity_type_filter)[0]:
                _entity_type_filter = t
        imgui.end_combo()

    imgui.separator()

    # Get entities
    type_filter = "" if _entity_type_filter == "all" else _entity_type_filter
    entities = B.query_entities(type=type_filter, search=search)

    if not entities:
        imgui.text_colored("No entities found", *_COL_TEXT2)
    else:
        imgui.text(f"{len(entities)} entities")
        imgui.separator()

        for ent in entities:
            eid = ent.get("id", "?")
            etype = ent.get("type", "?")
            tags = ent.get("tags", [])
            facts = ent.get("facts", "") or ""
            label = f"{_icon(ent)} {eid}"
            # Show first tag as subtitle
            name_tag = next((t[5:] for t in tags if t.startswith("name:")), "")
            if name_tag:
                label = f"{_icon(ent)} {name_tag}"

            if imgui.selectable(label, False)[0]:
                on_select(eid)

            # Show brief facts on hover
            if imgui.is_item_hovered():
                imgui.begin_tooltip()
                imgui.text(f"ID: {eid}")
                imgui.text(f"Type: {etype}")
                if facts:
                    imgui.text(facts[:200])
                imgui.end_tooltip()

    imgui.end()


# ── Entity Editor ──

_editor_data: Dict[str, Any] = {}
_editor_id: str = ""
_editor_dirty: bool = False
_editor_tab: str = "facts"


def panel_entity_editor():
    """Full entity detail/editor view."""
    global _editor_data, _editor_id, _editor_dirty, _editor_tab

    imgui.begin("Entity Editor", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    if not _editor_id:
        imgui.text_colored("Select an entity from the browser", *_COL_TEXT2)
        imgui.end()
        return

    if not _editor_data:
        _editor_data = B.get_entity(_editor_id) or {}

    data = _editor_data
    if not data:
        imgui.text_colored("Entity not found", *_COL_DANGER)
        imgui.end()
        return

    # Header
    imgui.text(f"Edit: {_editor_id}")
    imgui.same_line()
    imgui.text_colored(f"  type: {data.get('type', '?')}", *_COL_TEXT2)
    imgui.separator()

    # Tabs
    tabs = ["facts", "gurps/state", "skills", "equipment", "tags/position", "effects/scripts"]
    for tab in tabs:
        if _editor_tab == tab:
            imgui.text_colored(f"> {tab}", *_COL_ACCENT)
        else:
            if imgui.small_button(tab):
                _editor_tab = tab
        imgui.same_line()
    imgui.new_line()
    imgui.separator()

    if _editor_tab == "facts":
        # Facts editor
        facts = data.get("facts", "")
        changed, new_facts = imgui.input_text_multiline(
            "Facts", str(facts), 4096, -1, 120)
        if changed:
            data["facts"] = new_facts
            _editor_dirty = True

    elif _editor_tab == "gurps/state":
        gurps = data.get("gurps", {}) or {}
        state = data.get("state", {}) or {}
        # GURPS core
        imgui.text("GURPS")
        for k in ("ST", "DX", "IQ", "HT"):
            val = int(gurps.get(k, 10))
            changed, new_val = imgui.input_int(k, val)
            if changed:
                gurps[k] = new_val
                _editor_dirty = True
            imgui.same_line()
        imgui.new_line()

        imgui.separator()
        imgui.text("State")
        for k in ("hp", "max_hp", "fp", "max_fp", "er", "will"):
            val = int(state.get(k, 0))
            changed, new_val = imgui.input_int(k, val)
            if changed:
                state[k] = new_val
                _editor_dirty = True
            imgui.same_line()
        imgui.new_line()

        # Conditions
        imgui.separator()
        conds = state.get("conditions", [])
        imgui.text(f"Conditions ({len(conds)}):")
        for i, c in enumerate(conds[:5]):
            if imgui.small_button(f"x##cond{i}"):
                state["conditions"] = [x for j, x in enumerate(conds) if j != i]
                _editor_dirty = True
            imgui.same_line()
            imgui.text(c)
        imgui.new_line()

    elif _editor_tab == "skills":
        skills = data.get("skills", {}) or {}
        imgui.text("Skills")
        cached = skills.get("cached_skills", [])
        for i, sk in enumerate(cached[:10]):
            if imgui.small_button(f"x##sk{i}"):
                skills["cached_skills"] = [x for j, x in enumerate(cached) if j != i]
                _editor_dirty = True
            imgui.same_line()
            imgui.text(sk)
        imgui.new_line()

        imgui.separator()
        imgui.text("Passive abilities")
        abilities = skills.get("passive_abilities", [])
        for i, ab in enumerate(abilities[:5]):
            if imgui.small_button(f"x##ab{i}"):
                skills["passive_abilities"] = [x for j, x in enumerate(abilities) if j != i]
                _editor_dirty = True
            imgui.same_line()
            imgui.text(ab)

    elif _editor_tab == "equipment":
        equip = data.get("equipment", {}) or {}
        inv = equip.get("inventory", [])
        imgui.text(f"Inventory ({len(inv)} items)")
        for i, item in enumerate(inv[:10]):
            if imgui.small_button(f"x##inv{i}"):
                equip["inventory"] = [x for j, x in enumerate(inv) if j != i]
                _editor_dirty = True
            imgui.same_line()
            imgui.text(str(item))

        imgui.separator()
        currency = equip.get("currency", {}) or {}
        imgui.text("Currency")
        for ctype in ("gold", "silver", "copper"):
            val = int(currency.get(ctype, 0))
            changed, new_val = imgui.input_int(ctype, val)
            if changed:
                currency[ctype] = new_val
                _editor_dirty = True
            imgui.same_line()

    elif _editor_tab == "tags/position":
        tags = data.get("tags", [])
        imgui.text("Tags")
        for i, t in enumerate(tags):
            if imgui.small_button(f"x##tag{i}"):
                data["tags"] = [x for j, x in enumerate(tags) if j != i]
                _editor_dirty = True
            imgui.same_line()
            imgui.text(t)

        imgui.separator()
        pos = data.get("position", {}) or {}
        imgui.text("Position")
        inside = str(pos.get("inside", ""))
        changed, new_inside = imgui.input_text("Inside", inside, 128)
        if changed:
            pos["inside"] = new_inside
            _editor_dirty = True

    elif _editor_tab == "effects/scripts":
        effects = data.get("effects", [])
        imgui.text(f"Effects ({len(effects)})")
        for ef in effects[:5]:
            desc = ef.get("description", "?")
            delta = ef.get("stat_deltas", {})
            dur = ef.get("duration_ticks", -1)
            imgui.bullet_text(f"{desc}  {delta}  {'permanent' if dur < 0 else f'{dur} ticks'}")
            eid = ef.get("effect_id", "")
            if imgui.small_button(f"Remove##{eid}"):
                B.remove_effect(data.get("id", ""), eid)
                _editor_data = B.get_entity(_editor_id) or {}

        imgui.separator()
        scripts = data.get("scripts", [])
        imgui.text(f"Scripts ({len(scripts)})")
        for sc in scripts[:3]:
            imgui.bullet_text(f"{sc.get('trigger_type', '?')}: {sc.get('comment', '')[:60]}")

    imgui.separator()

    # Save/Delete buttons
    if _editor_dirty:
        if imgui.button("Save", 100, 28):
            result = B.save_entity(
                id=_editor_id,
                type=data.get("type", ""),
                facts=data.get("facts", ""),
                gurps=data.get("gurps"),
                state=data.get("state"),
                skills=data.get("skills"),
                equipment=data.get("equipment"),
                tags=data.get("tags"),
                position=data.get("position"),
            )
            _editor_dirty = False
            if result.get("warnings"):
                imgui.text_colored("; ".join(result["warnings"]), *_COL_WARN)
            else:
                imgui.text_colored("Saved", *_COL_SUCCESS)

    imgui.same_line()
    if imgui.button("Revert", 100, 28):
        _editor_data = B.get_entity(_editor_id) or {}
        _editor_dirty = False

    imgui.same_line()
    if imgui.button("Delete", 100, 28):
        B.delete_entity(_editor_id)
        _editor_id = ""
        _editor_data = {}
        _editor_dirty = False

    imgui.end()


def editor_open(entity_id: str):
    """Open the entity editor for a given entity ID."""
    global _editor_id, _editor_data, _editor_dirty
    _editor_id = entity_id
    _editor_data = B.get_entity(entity_id) or {}
    _editor_dirty = False


# ── Story Timeline ──

_story_expanded: Dict[str, bool] = {}


def panel_story_timeline():
    """Left sidebar: story arc/paragraph tree."""
    imgui.begin("Story", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    arcs = B.get_story_tree()
    if not arcs:
        imgui.text_colored("No story data", *_COL_TEXT2)
        imgui.end()
        return

    for arc in arcs:
        aname = arc.get("name", "?")
        expanded = _story_expanded.get(f"arc_{arc['index']}", False)
        if imgui.tree_node(f"{aname}##arc{arc['index']}"):
            _story_expanded[f"arc_{arc['index']}"] = True
            for p in arc.get("paragraphs", []):
                pname = p.get("name", "?")
                tc = p.get("turn_count", 0)
                imgui.bullet_text(f"{pname} ({tc} turns)")
                if imgui.is_item_hovered():
                    summary = p.get("summary", "")
                    if summary:
                        imgui.begin_tooltip()
                        imgui.text(summary[:300])
                        imgui.end_tooltip()

            if arc.get("has_ongoing"):
                imgui.text_colored(f"* Ongoing ({arc.get('ongoing_turns', 0)} turns)", *_COL_ACCENT)

            imgui.tree_pop()
        else:
            _story_expanded[f"arc_{arc['index']}"] = False

    imgui.end()


# ── Event Log ──

_event_filter: str = ""
_event_show_auto: bool = True


def panel_event_log():
    global _event_filter, _event_show_auto
    imgui.begin("Event Log", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    changed, _event_filter = imgui.input_text("Filter", _event_filter, 128)
    imgui.same_line()
    _, _event_show_auto = imgui.checkbox("Show auto", _event_show_auto)

    events = B.get_events(limit=100)
    for ev in events[:50]:
        desc = ev.get("description", "")
        tag = ev.get("tag", "")
        tick = ev.get("tick", 0)

        if _event_filter.strip() and _event_filter.lower() not in desc.lower():
            continue
        if not _event_show_auto and tag in ("system", "auto"):
            continue

        color = _COL_TEXT
        if tag == "narrative":
            color = _COL_ACCENT
        elif tag == "script":
            color = _COL_WARN
        elif tag == "system":
            color = _COL_TEXT2

        imgui.text_colored(f"[{tick}]", *_COL_TEXT2)
        imgui.same_line()
        imgui.text_colored(desc[:120], *color)

    imgui.end()


# ── Agent Dashboard ──

_agent_status_data: Dict[str, Any] = {}


def panel_agent_dashboard():
    imgui.begin("Agents", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    for agent in ("wm", "gm", "sm"):
        imgui.text_colored(f"[{agent.upper()}]", *_COL_ACCENT)

        # News queue
        news = B.get_news_queue(agent)
        if news:
            imgui.text_colored(f"  news: {len(news)} items", *_COL_WARN)
        else:
            imgui.text("  news: empty")

        if imgui.small_button(f"Invoke##{agent}"):
            B.invoke_agent(agent, comment="GUI trigger")

        imgui.separator()

    # Scheduler queue
    sched = B.get_scheduler_queue()
    if sched:
        imgui.text_colored(f"Scheduled: {len(sched)} items", *_COL_ACCENT)
        for s in sched[:5]:
            a = s.get("agent", "?")
            t = s.get("scheduled_tick", 0)
            c = s.get("comment", "")
            imgui.text(f"  {a} @ tick {t}: {c[:40]}")

    imgui.end()


# ── Prompt Inspector ──

_prompt_agent: str = "gm"
_prompt_messages: List[Dict[str, Any]] = []
_prompt_expanded: Dict[int, bool] = {}


def panel_prompt_inspector():
    global _prompt_messages, _prompt_agent, _prompt_expanded

    imgui.begin("Prompt Inspector", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    # Agent selector
    agents = ["gm", "sm", "wm"]
    if imgui.begin_combo("Agent", _prompt_agent):
        for a in agents:
            if imgui.selectable(a, a == _prompt_agent)[0]:
                _prompt_agent = a
                _prompt_messages = []
                _prompt_expanded.clear()
        imgui.end_combo()

    imgui.same_line()
    if imgui.button("Load History", 120, 24):
        _prompt_messages = B.get_agent_history(_prompt_agent)

    imgui.separator()

    if not _prompt_messages:
        imgui.text_colored("Load agent history to inspect", *_COL_TEXT2)
        imgui.end()
        return

    imgui.text(f"{len(_prompt_messages)} messages")
    imgui.separator()

    # Counts per type
    role_counts: Dict[str, int] = {}
    role_tokens: Dict[str, int] = {}
    for msg in _prompt_messages:
        role = msg.get("role", "?")
        role_counts[role] = role_counts.get(role, 0) + 1
        content = str(msg.get("content", "") or "")
        role_tokens[role] = role_tokens.get(role, 0) + len(content) // 4

    summary_parts = [f"{r}: {role_counts[r]} msgs ({role_tokens[r]} tok)" for r in role_counts]
    imgui.text(" | ".join(summary_parts))
    imgui.separator()

    # Message list
    for i, msg in enumerate(_prompt_messages):
        role = msg.get("role", "?")
        content = str(msg.get("content", "") or "")
        tool_calls = msg.get("tool_calls") or msg.get("tool_calls") or []

        # Detect type/label
        label = role
        if role == "system":
            # Detect known block prefixes
            for prefix in ["[world_setting]", "[arc_summaries]", "[gm_summaries]",
                          "[character:", "[reason]", "[newspaper]", "[tools_available]",
                          "[storage_notice]", "[world_registry]", "[subscription_trigger]",
                          "[event_summary]", "[human_input]"]:
                if prefix in content[:100]:
                    label = prefix.rstrip(":]").lstrip("[")
                    break
            else:
                label = f"UNKNOWN: {content[:60]}..."
        elif role == "assistant" and tool_calls:
            tc_names = [tc.get("name", "?") for tc in tool_calls[:3]]
            label = f"tool_call: {', '.join(tc_names)}"
        elif role == "tool":
            label = f"result: {len(content)}B"
        elif role == "user":
            label = "human_input"

        # Collapsible row
        header = f"#{i+1} {role:10s} {label[:50]:50s} {len(content):5d}B"
        expanded = _prompt_expanded.get(i, False)

        if imgui.tree_node(f"{header}##msg{i}"):
            _prompt_expanded[i] = True
            imgui.text_wrapped(content[:2000])
            # Show tool calls if any
            if tool_calls:
                imgui.separator()
                for tc in tool_calls[:5]:
                    tc_name = tc.get("name", "?")
                    tc_args = tc.get("args", {})
                    imgui.text_colored(f"  -> {tc_name}({json.dumps(tc_args)[:200]})", *_COL_ACCENT)
            imgui.tree_pop()
        else:
            _prompt_expanded[i] = False

        imgui.separator()

    imgui.end()


# ── Script/Effect Monitor ──

def panel_script_monitor():
    imgui.begin("Automations", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    entities = B.query_entities(limit=100)
    active_scripts = 0
    active_effects = 0

    for ent in entities:
        eid = ent.get("id", "?")
        effects = ent.get("active_effects", []) or ent.get("effects", [])
        scripts = ent.get("scripts", [])
        if effects:
            for ef in effects[:3]:
                desc = ef.get("description", "?")
                dur = ef.get("duration_ticks", -1)
                imgui.bullet_text(f"{eid}: {desc} ({'perm' if dur < 0 else f'{dur} ticks'})")
                active_effects += 1
        if scripts:
            for sc in scripts[:2]:
                tt = sc.get("trigger_type", "?")
                cmt = sc.get("comment", "") or sc.get("trigger_type", "")
                imgui.text_colored(f"  {eid}: [{tt}] {cmt[:60]}", *_COL_WARN)
                active_scripts += 1

    if active_scripts == 0 and active_effects == 0:
        imgui.text_colored("No active automations", *_COL_TEXT2)

    imgui.end()
