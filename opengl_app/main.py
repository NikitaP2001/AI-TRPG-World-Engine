"""LLM World — Desktop GUI.

Game flow: Main Menu → New/Continue → Game View → Pause → Save/Load.
Debug panels are optional, hidden behind Settings.
"""
from __future__ import annotations

import os
import sys
import time
import json
import shutil
from pathlib import Path

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import glfw
import moderngl as mgl
import imgui
from imgui.integrations.glfw import GlfwRenderer
from opengl_app.gui_bridge import GuiBridge as B

# Import panel functions but they're only used in debug mode
from opengl_app.gui_panels import (
    panel_scene_view, panel_entity_browser, panel_entity_editor,
    panel_story_timeline, panel_event_log, panel_agent_dashboard,
    panel_prompt_inspector, panel_effect_monitor,
    editor_open, _init_style,
)

# ── Game state ──

class GameState:
    MENU = 0
    PLAYING = 1
    PAUSED = 2
    READER = 3

state = GameState.MENU
_debug_mode: bool = False
_show_demo: bool = False
_show_new_game_popup: bool = False
_show_settings: bool = False
_show_pause_menu: bool = False
_show_create_preset_popup: bool = False
_init_presets: list = []
_selected_preset: int = -1
_game_started: bool = False
_status_msg: str = ""
_status_timer: float = 0

# ── Create Preset state ──
_preset_name_input: str = ""
_preset_plot_input: str = ""
_preset_characters: list = []  # list of character dicts
_preset_editing_char: int = -1  # -1 = not editing, >=0 = index
_preset_char_form_data: dict = {}  # temp data for character being edited
_preset_create_result: str = ""

# ── Save file helpers ──

_SAVE_DIR = Path("saves")


def _list_saves() -> list[dict]:
    _SAVE_DIR.mkdir(exist_ok=True)
    slots = []
    for p in sorted(_SAVE_DIR.glob("save_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            slots.append({
                "path": str(p),
                "name": data.get("name", p.stem),
                "time": data.get("saved_at", "?"),
                "world_time": data.get("world_time", "?"),
                "turn": data.get("turn", 0),
            })
        except Exception:
            slots.append({"path": str(p), "name": p.stem, "time": "?", "world_time": "?", "turn": 0})
    return slots


def _list_init_presets() -> list[dict]:
    presets = []
    setups_dir = Path("setups")
    if setups_dir.exists():
        for d in sorted(setups_dir.iterdir()):
            if d.is_dir():
                preset_file = d / "preset.json"
                if preset_file.exists():
                    try:
                        raw = preset_file.read_text(encoding="utf-8").strip()
                        plot_text = json.loads(raw) if raw.startswith('"') else raw
                        presets.append({"name": d.name, "title": d.name,
                                        "description": plot_text[:120]})
                    except Exception:
                        presets.append({"name": d.name, "title": d.name, "description": ""})
                else:
                    presets.append({"name": d.name, "title": d.name, "description": ""})
    return presets


def _start_game(preset: str = "") -> None:
    global state, _game_started, _status_msg, _status_timer
    from world import World as _World
    from world.state import _write_json, _read_json
    try:
        workspace = Path(__file__).resolve().parent.parent
        game_root = workspace / "game"
        game_chars = game_root / "players"
        game_world = game_root / "world"

        if preset:
            preset_dir = workspace / "setups" / preset
            if not preset_dir.exists():
                _status_msg = f"Preset '{preset}' not found."
                _status_timer = time.time() + 5.0
                return

            # 1. Copy player files from the preset into game/players/
            preset_chars = preset_dir / "players"
            if preset_chars.exists() and preset_chars.is_dir():
                game_chars.mkdir(parents=True, exist_ok=True)
                for char_dir in sorted(preset_chars.iterdir()):
                    if not char_dir.is_dir():
                        continue
                    dst = game_chars / char_dir.name
                    dst.mkdir(parents=True, exist_ok=True)
                    for json_file in char_dir.glob("*.json"):
                        target = dst / json_file.name
                        if not target.exists():
                            import shutil
                            shutil.copy2(json_file, target)

            # 2. Seed plot into story.json first paragraph
            preset_plot = preset_dir / "preset.json"
            if preset_plot.exists():
                try:
                    raw = preset_plot.read_text(encoding="utf-8").strip()
                    init_text = json.loads(raw) if raw.startswith('"') else raw
                    if init_text:
                        # Ensure story.json exists with default structure
                        game_world.mkdir(parents=True, exist_ok=True)
                        story_path = game_world / "story.json"
                        if not story_path.exists():
                            _write_json(story_path, [
                                {"name": "Ongoing Arc", "paragraphs": [],
                                 "ongoing_paragraph": {"start_time": "", "turns": [],
                                                       "locations": [], "players": [], "npcs": []}}
                            ])
                        arcs = _read_json(story_path)
                        if isinstance(arcs, list) and arcs:
                            oldest = arcs[-1]
                            if isinstance(oldest, dict):
                                paras = oldest.get("paragraphs", [])
                                if not isinstance(paras, list):
                                    paras = []
                                # Check idempotent
                                if not (paras and isinstance(paras[0], dict)
                                        and str(paras[0].get("name") or "").strip() == "Initial Plot"):
                                    paras.insert(0, {
                                        "name": "Initial Plot", "start_time": "", "end_time": "",
                                        "locations": [], "players": [], "npcs": [],
                                        "summary": init_text, "turn_count": 0, "turns": [],
                                    })
                                    oldest["paragraphs"] = paras
                                    arcs[-1] = oldest
                                    _write_json(story_path, arcs)
                except Exception:
                    pass

        # 3. Initialize the world (creates all game/world/*.json files, migrates, etc.)
        w = _World(workspace_root=workspace)
        w.ensure_initialized()

        _game_started = True
        state = GameState.PLAYING
        _status_msg = f"Game started with preset: {preset or 'default'}"
        _status_timer = time.time() + 3.0
    except Exception as e:
        _status_msg = f"Failed to start: {e}"
        _status_timer = time.time() + 5.0


def _save_game(slot_name: str) -> None:
    global _status_msg, _status_timer
    _SAVE_DIR.mkdir(exist_ok=True)
    path = _SAVE_DIR / f"save_{slot_name}.json"
    try:
        data = {
            "name": slot_name,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "world_time": B.get_world_time(),
            "turn": 0,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        _status_msg = f"Saved: {slot_name}"
        _status_timer = time.time() + 3.0
    except Exception as e:
        _status_msg = f"Save failed: {e}"
        _status_timer = time.time() + 5.0


def _quit_to_menu() -> None:
    global state, _game_started
    _game_started = False
    state = GameState.MENU


def _create_preset(preset_name: str, plot_text: str, characters: list) -> str:
    """Create a new world preset with characters and plot.

    Generates preset.json + characters/ under setups/{name}/.
    Returns empty string on success, or error message on failure.
    """
    setups_dir = Path("setups")

    if not preset_name.strip():
        return "Preset name cannot be empty."

    # Sanitize name
    safe = "".join(c for c in preset_name.strip() if c.isalnum() or c in "-_ ")
    safe = safe.strip().replace(" ", "_")
    if not safe:
        return "Invalid preset name. Use letters, numbers, hyphens, underscores."

    dst = setups_dir / safe
    if dst.exists():
        return f"Preset '{safe}' already exists in setups/."

    try:
        setups_dir.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)

        # preset.json — bare JSON string with plot text
        (dst / "preset.json").write_text(
            json.dumps(plot_text.strip(), ensure_ascii=False), encoding="utf-8"
        )

        # characters/
        if characters:
            chars_dir = dst / "players"
            chars_dir.mkdir(parents=True, exist_ok=True)
            for ch in characters:
                name = (ch.get("name") or "").strip()
                if not name:
                    continue
                # Build description.json matching old webui format
                data = {}
                general = (ch.get("general") or "").strip()
                if general:
                    data["general"] = general
                race = (ch.get("race") or "").strip()
                if race:
                    data["race"] = race
                location = (ch.get("location") or "").strip()
                if location:
                    data["location"] = location
                personality = {}
                for pk in ("mbti", "alignment", "traits", "details"):
                    pv = (ch.get("personality_" + pk) or "").strip()
                    if pv:
                        personality[pk] = pv
                if personality:
                    data["personality"] = personality
                appearance = (ch.get("appearance") or "").strip()
                if appearance:
                    data["appearance"] = {"base": appearance}
                status = {}
                health = (ch.get("status_health") or "alive").strip()
                if health:
                    status["health"] = health
                state_str = (ch.get("status_state") or "").strip()
                if state_str:
                    status["state"] = state_str
                if status:
                    data["status"] = status
                skills = (ch.get("skills") or "").strip()
                if skills:
                    data["skills"] = {"base": skills}
                equipment = (ch.get("equipment") or "").strip()
                if equipment:
                    data["equipment"] = {"base": equipment}

                char_dst = chars_dir / name
                char_dst.mkdir(parents=True, exist_ok=True)
                (char_dst / "description.json").write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )

        # Refresh preset list
        global _init_presets
        _init_presets = _list_init_presets()

        return ""  # success
    except Exception as e:
        if dst.exists():
            shutil.rmtree(dst)
        return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════
#  MAIN MENU
# ══════════════════════════════════════════════════════════════════

# ── Character editor helper for Create Preset ──

def _push_char_form_data(index: int) -> None:
    """Push existing character data into the editor form fields."""
    global _preset_char_form_data, _preset_editing_char
    if 0 <= index < len(_preset_characters):
        _preset_char_form_data = dict(_preset_characters[index])
    else:
        _preset_char_form_data = {}
    _preset_editing_char = index


def _render_char_editor(pw: int, line_h: int) -> None:
    """Render the character creation/editing sub-form."""
    global _preset_char_form_data, _preset_editing_char, _preset_characters

    fd = _preset_char_form_data

    def _val(key: str, default: str = "") -> str:
        return str(fd.get(key, default))

    # Basic info
    imgui.text("Character Name:")
    imgui.set_next_item_width(pw * 0.88)
    _changed, v = imgui.input_text("##ch_name", _val("name"), 64, 0)
    if _changed:
        fd["name"] = v

    imgui.text("General Description:")
    imgui.set_next_item_width(pw * 0.88)
    _changed, v = imgui.input_text_multiline("##ch_general", _val("general"), 1024, pw * 0.88, line_h * 3, 0)
    if _changed:
        fd["general"] = v

    # Race + Location on one row
    col_w = int(pw * 0.43)
    imgui.text("Race:")
    imgui.set_next_item_width(col_w)
    _changed, v = imgui.input_text("##ch_race", _val("race"), 64, 0)
    if _changed:
        fd["race"] = v
    imgui.same_line()
    imgui.set_cursor_pos_x(int(pw * 0.06) + col_w + 10)
    imgui.text("Location:")
    imgui.set_next_item_width(col_w)
    _changed, v = imgui.input_text("##ch_location", _val("location"), 64, 0)
    if _changed:
        fd["location"] = v

    # Personality (collapsible)
    _expanded = True
    _expanded, _ = imgui.collapsing_header("Personality", _expanded)
    if _expanded:
        for pk, plabel in [("mbti", "MBTI"), ("alignment", "Alignment"),
                            ("traits", "Traits"), ("details", "Details")]:
            imgui.text(f"{plabel}:")
            key = "personality_" + pk
            ml_h = line_h * (2 if pk in ("traits", "details") else 1)
            imgui.set_next_item_width(pw * 0.88)
            if pk in ("traits", "details"):
                _changed, v = imgui.input_text_multiline(f"##ch_{pk}", _val(key), 512, pw * 0.88, ml_h, 0)
            else:
                _changed, v = imgui.input_text(f"##ch_{pk}", _val(key), 128, 0)
            if _changed:
                fd[key] = v

    # Appearance
    imgui.text("Appearance:")
    imgui.set_next_item_width(pw * 0.88)
    _changed, v = imgui.input_text_multiline("##ch_appearance", _val("appearance"), 512, pw * 0.88, line_h * 2, 0)
    if _changed:
        fd["appearance"] = v

    # Status
    imgui.text("Health:")
    imgui.set_next_item_width(int(pw * 0.30))
    _changed, v = imgui.input_text("##ch_health", _val("status_health", "alive"), 32, 0)
    if _changed:
        fd["status_health"] = v
    imgui.same_line()
    imgui.set_cursor_pos_x(int(pw * 0.06) + int(pw * 0.30) + 10)
    imgui.text("State:")
    imgui.set_next_item_width(int(pw * 0.50))
    _changed, v = imgui.input_text("##ch_state", _val("status_state"), 128, 0)
    if _changed:
        fd["status_state"] = v

    # Skills + Equipment
    imgui.text("Skills:")
    imgui.set_next_item_width(pw * 0.88)
    _changed, v = imgui.input_text_multiline("##ch_skills", _val("skills"), 512, pw * 0.88, line_h * 2, 0)
    if _changed:
        fd["skills"] = v

    imgui.text("Equipment:")
    imgui.set_next_item_width(pw * 0.88)
    _changed, v = imgui.input_text_multiline("##ch_equipment", _val("equipment"), 512, pw * 0.88, line_h * 2, 0)
    if _changed:
        fd["equipment"] = v

    # Buttons
    btn_w2 = int(pw * 0.18)
    btn_h2 = int(line_h * 1.6)
    imgui.set_cursor_pos((int(pw * 0.06), imgui.get_cursor_pos_y() + int(line_h * 0.5)))

    if imgui.button("Save Character", btn_w2 + 20, btn_h2):
        name = (fd.get("name") or "").strip()
        if not name:
            _preset_create_result = "Character name is required."
        else:
            # Build the character entry
            entry = {
                "name": name,
                "general": (fd.get("general") or "").strip(),
                "race": (fd.get("race") or "").strip(),
                "location": (fd.get("location") or "").strip(),
            }
            for pk in ("mbti", "alignment", "traits", "details"):
                v = (fd.get("personality_" + pk) or "").strip()
                if v:
                    entry["personality_" + pk] = v
            appearance = (fd.get("appearance") or "").strip()
            if appearance:
                entry["appearance"] = appearance
            entry["status_health"] = (fd.get("status_health") or "alive").strip()
            entry["status_state"] = (fd.get("status_state") or "").strip()
            skills = (fd.get("skills") or "").strip()
            if skills:
                entry["skills"] = skills
            equipment = (fd.get("equipment") or "").strip()
            if equipment:
                entry["equipment"] = equipment

            if _preset_editing_char >= 0 and _preset_editing_char < len(_preset_characters):
                _preset_characters[_preset_editing_char] = entry
            else:
                _preset_characters.append(entry)

            _preset_editing_char = -1
            _preset_char_form_data = {}
            _preset_create_result = ""

    imgui.same_line()
    if imgui.button("Cancel", btn_w2, btn_h2):
        _preset_editing_char = -1
        _preset_char_form_data = {}
        _preset_create_result = ""


def _render_main_menu(display_w: int, display_h: int):
    global _show_new_game_popup, _show_settings, _selected_preset, _init_presets, _show_create_preset_popup
    global _preset_name_input, _preset_plot_input, _preset_create_result
    global _preset_characters, _preset_editing_char, _preset_char_form_data
    global state

    # Responsive panel — fills most of the window nicely
    pw = int(display_w * 0.55)
    ph = int(display_h * 0.65)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph, imgui.ALWAYS)
    imgui.begin("LLM World", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)

    pw = int(display_w * 0.55)
    ph = int(display_h * 0.65)
    line_h = imgui.get_text_line_height_with_spacing()

    # ── If Create Preset is active, show form instead of menu ──
    if _show_create_preset_popup:
        y = int(ph * 0.04)
        t_w = imgui.calc_text_size("Create Preset").x
        imgui.set_cursor_pos(((pw - t_w) // 2, y))
        imgui.text_colored("Create Preset", 0.23, 0.51, 0.96, 1.0)
        y += int(line_h * 1.4)
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.separator()
        y += int(line_h * 1.0)

        # ── Preset name ──
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.text("Preset name:")
        y += int(line_h * 1.0)
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.set_next_item_width(pw * 0.88)
        changed, _preset_name_input = imgui.input_text("##pname", _preset_name_input, 128, 0)
        y += int(line_h * 1.5)

        # ── Init Plot text ──
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.text("Init Plot:")
        y += int(line_h * 1.0)
        plot_h = int(line_h * 3.0)
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.set_next_item_width(pw * 0.88)
        changed, _preset_plot_input = imgui.input_text_multiline("##pplot", _preset_plot_input, 1024, pw * 0.88, plot_h, 0)
        y += plot_h + int(line_h * 0.8)

        # ── Characters section ──
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.separator()
        y += int(line_h * 0.8)
        imgui.set_cursor_pos((int(pw * 0.06), y))
        imgui.text_colored(f"Characters ({len(_preset_characters)})", 0.58, 0.64, 0.72, 1.0)
        imgui.same_line()
        btn_sm = int(line_h * 1.4)
        if imgui.button("+ Add", btn_sm * 2, btn_sm):
            _push_char_form_data(-1)
        y += int(line_h * 1.2)

        # List existing characters
        remove_idx = -1
        edit_idx = -1
        for ci, ch in enumerate(_preset_characters):
            ch_name = (ch.get("name") or "?").strip()
            imgui.set_cursor_pos((int(pw * 0.08), y))
            imgui.text(f"{ci+1}. {ch_name}")
            imgui.same_line()
            imgui.set_cursor_pos_x(pw * 0.55)
            if imgui.button(f"Edit##{ci}", btn_sm * 1.5, btn_sm):
                edit_idx = ci
            imgui.same_line()
            imgui.set_cursor_pos_x(pw * 0.55 + btn_sm * 1.5 + 6)
            if imgui.button(f"X##{ci}", btn_sm, btn_sm):
                remove_idx = ci
            y += int(line_h * 1.2)

        if edit_idx >= 0:
            _push_char_form_data(edit_idx)
        if remove_idx >= 0:
            _preset_characters.pop(remove_idx)
            if _preset_editing_char >= len(_preset_characters):
                _preset_editing_char = -1

        # ── Character editor sub-form ──
        if _preset_editing_char >= -1:  # -1 = adding new, >=0 = editing existing
                imgui.set_cursor_pos((int(pw * 0.06), y))
                imgui.separator()
                y += int(line_h * 0.6)
                imgui.set_cursor_pos((int(pw * 0.06), y))
                mode = "Edit" if _preset_editing_char >= 0 else "Add"
                imgui.text_colored(f"{mode} Character", 0.23, 0.51, 0.96, 1.0)
                y += int(line_h * 1.0)
                imgui.set_cursor_pos((int(pw * 0.06), y))
                # Use a child region for the character editor
                editor_h = ph - y - int(line_h * 4.0)
                if editor_h > line_h * 10:
                    imgui.begin_child("##chareditor", pw * 0.94, editor_h, False, imgui.WINDOW_ALWAYS_USE_WINDOW_PADDING)
                    _render_char_editor(pw, line_h)
                    imgui.end_child()
                    y = ph - int(line_h * 3.0)  # skip remaining
                else:
                    _render_char_editor(pw, line_h)

        # ── Error / success ──
        if _preset_create_result:
            is_err = not (_preset_create_result.startswith("Saved") or _preset_create_result.startswith("Created"))
            if is_err:
                imgui.set_cursor_pos((int(pw * 0.06), y))
                imgui.text_colored(_preset_create_result, 0.94, 0.27, 0.27, 1.0)
                y += int(line_h * 1.2)

        # ── Bottom buttons ──
        btn_w = int(pw * 0.22)
        btn_h = int(line_h * 1.8)
        y = ph - btn_h - int(line_h * 0.6)
        imgui.set_cursor_pos(((pw - btn_w * 2 - 12) // 2, y))
        if imgui.button("Create", btn_w, btn_h):
            _preset_create_result = _create_preset(_preset_name_input, _preset_plot_input, _preset_characters)
            if not _preset_create_result:
                _show_create_preset_popup = False
                _show_new_game_popup = True
                _init_presets = _list_init_presets()
                _selected_preset = -1
                safe = "".join(c for c in _preset_name_input.strip() if c.isalnum() or c in "-_ ")
                safe = safe.strip().replace(" ", "_")
                for idx, p in enumerate(_init_presets):
                    if p['name'] == safe:
                        _selected_preset = idx
                        break
        imgui.same_line()
        if imgui.button("Back", btn_w, btn_h):
            _show_create_preset_popup = False

        imgui.end()
        return  # skip normal menu rendering

    # ── Normal menu rendering ──
    # ── Title block ──
    y = int(ph * 0.09)
    title = "LLM World"
    title_w = imgui.calc_text_size(title).x
    imgui.set_cursor_pos(((pw - title_w) // 2, y))
    imgui.text_colored(title, 0.23, 0.51, 0.96, 1.0)

    y += int(line_h * 1.5)
    subtitle = "A Narrative World Simulation"
    sub_w = imgui.calc_text_size(subtitle).x
    imgui.set_cursor_pos(((pw - sub_w) // 2, y))
    imgui.text_colored(subtitle, 0.58, 0.64, 0.72, 1.0)

    # ── Buttons ──
    btn_w = int(pw * 0.45)
    btn_h = int(ph * 0.065)
    spacing = int(ph * 0.025)

    # Vertical center for button block
    saves = _list_saves()
    num_btns = 5  # Sessions, New Game, Create Preset, Settings, Exit
    if saves:
        num_btns += 2  # Continue + Load Game
    total_btn_h = btn_h * num_btns + spacing * (num_btns - 1)
    y = int((ph - total_btn_h) * 0.58)

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Sessions", btn_w, btn_h):
        from opengl_app import reader_panels as _reader
        _reader.open_reader()
        state = GameState.READER
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("New Game", btn_w, btn_h):
        _init_presets = _list_init_presets()
        if _init_presets:
            _show_new_game_popup = True
        else:
            _show_create_preset_popup = True
            _preset_name_input = ""
            _preset_plot_input = ""
            _preset_characters = []
            _preset_editing_char = -1
            _preset_char_form_data = {}
            _preset_create_result = ""
    y += btn_h + spacing

    if saves:
        imgui.set_cursor_pos(((pw - btn_w) // 2, y))
        if imgui.button("Continue", btn_w, btn_h):
            _start_game()
        y += btn_h + spacing

        imgui.set_cursor_pos(((pw - btn_w) // 2, y))
        if imgui.button("Load Game", btn_w, btn_h):
            pass
        y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Create Preset", btn_w, btn_h):
        _show_create_preset_popup = True
        _preset_name_input = ""
        _preset_plot_input = ""
        _preset_characters = []
        _preset_editing_char = -1
        _preset_char_form_data = {}
        _preset_create_result = ""
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Settings", btn_w, btn_h):
        _show_settings = not _show_settings
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Exit", btn_w, btn_h):
        glfw.set_window_should_close(glfw.get_current_context(), True)

    # Version
    imgui.set_cursor_pos((pw - 60, ph - 25))
    imgui.text_colored("v0.2", 0.4, 0.4, 0.4, 1.0)

    imgui.end()

    # ── New Game popup ──
    if _show_new_game_popup:
        _render_new_game_popup(display_w, display_h)

    # ── Settings popup ──
    if _show_settings:
        _render_settings_popup(display_w, display_h)


def _render_new_game_popup(display_w: int, display_h: int):
    global _show_new_game_popup, _init_presets, _selected_preset

    pw = int(display_w * 0.50)
    ph = int(display_h * 0.55)
    margin = int(pw * 0.05)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph, imgui.ALWAYS)
    imgui.begin("##new_game", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)

    lh = imgui.get_text_line_height_with_spacing()

    # Title
    y = int(ph * 0.06)
    title_w = imgui.calc_text_size("New Game").x
    imgui.set_cursor_pos(((pw - title_w) // 2, y))
    imgui.text_colored("New Game", 0.23, 0.51, 0.96, 1.0)
    y += int(lh * 1.6)

    imgui.set_cursor_pos((margin, y))
    imgui.text("Select World Preset:")
    y += int(lh * 1.1)
    imgui.set_cursor_pos((margin, y))
    imgui.separator()
    y += int(lh * 0.6)

    if not _init_presets:
        _init_presets = _list_init_presets()
        if not _init_presets:
            imgui.set_cursor_pos(((pw - 160) // 2, y + int(lh * 2)))
            imgui.text_colored("No presets found in setups/", 0.94, 0.27, 0.27, 1.0)

    # Preset list fills remaining space above buttons
    btn_h = int(lh * 1.8)
    btn_w = 140
    available_y = imgui.get_content_region_available().y
    list_h = available_y - btn_h - int(lh * 1.5)

    imgui.set_cursor_pos((margin, y))
    imgui.begin_child("##preset_list", pw - margin * 2, max(list_h, lh * 2), False,
                      imgui.WINDOW_ALWAYS_USE_WINDOW_PADDING)
    for i, preset in enumerate(_init_presets):
        selected = i == _selected_preset
        clicked, _ = imgui.selectable(f"{preset['title']}##preset{i}", selected, 0, 0, 0)
        if clicked:
            _selected_preset = i
        if selected and preset.get("description"):
            desc = preset["description"].strip()
            title = preset.get("title", "").strip()
            # Only show description if it's different from the title
            if desc and desc != title:
                imgui.text_colored(desc[:300], 0.58, 0.64, 0.72, 1.0)
    imgui.end_child()

    # Bottom buttons
    cx = (pw - btn_w * 2 - 12) // 2
    imgui.set_cursor_pos((cx, imgui.get_cursor_pos_y() + int(lh * 0.5)))
    has_selection = _selected_preset >= 0 and _selected_preset < len(_init_presets)
    if has_selection:
        if imgui.button("Start", btn_w, btn_h):
            _start_game(_init_presets[_selected_preset]["name"])
            _show_new_game_popup = False
    else:
        imgui.button("Start", btn_w, btn_h)

    imgui.same_line()
    if imgui.button("Cancel", btn_w, btn_h):
        _show_new_game_popup = False

    imgui.end()


def _render_settings_popup(display_w: int, display_h: int):
    global _show_settings, _debug_mode

    pw = int(display_w * 0.40)
    ph = int(display_h * 0.32)
    margin = int(pw * 0.05)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph, imgui.ALWAYS)
    imgui.begin("##settings", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)

    lh = imgui.get_text_line_height_with_spacing()

    y = int(ph * 0.08)
    t_w = imgui.calc_text_size("Settings").x
    imgui.set_cursor_pos(((pw - t_w) // 2, y))
    imgui.text_colored("Settings", 0.23, 0.51, 0.96, 1.0)
    y += int(lh * 2.0)

    imgui.set_cursor_pos((margin, y))
    imgui.separator()
    y += int(lh * 1.2)

    imgui.set_cursor_pos((margin, y))
    _, _debug_mode = imgui.checkbox("Debug Mode (panels)", _debug_mode)
    y += int(lh * 1.8)

    imgui.set_cursor_pos((margin, y))
    imgui.text_colored("Warning: Debug panels expose internal engine state.", 0.96, 0.62, 0.04, 1.0)
    y += int(lh * 0.9)
    imgui.set_cursor_pos((margin, y))
    imgui.text_colored("Not intended for normal gameplay.", 0.96, 0.62, 0.04, 1.0)

    btn_w = int(pw * 0.25)
    btn_h = int(lh * 1.8)
    imgui.set_cursor_pos(((pw - btn_w) // 2, ph - btn_h - int(lh * 1.0)))
    if imgui.button("Close", btn_w, btn_h):
        _show_settings = False

    imgui.end()


# ══════════════════════════════════════════════════════════════════
#  GAME VIEW
# ══════════════════════════════════════════════════════════════════

def _render_game_view(display_w: int, display_h: int):
    global _show_pause_menu, _status_timer, _status_msg

    # Status message (if any)
    if _status_timer > time.time():
        imgui.set_next_window_position(display_w // 2 - 150, 5)
        imgui.set_next_window_size(300, 30, imgui.ALWAYS)
        imgui.begin("##status", None,
                    imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                    | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)
        tw = imgui.calc_text_size(_status_msg).x
        imgui.set_cursor_pos((150 - tw // 2, 5))
        imgui.text(_status_msg)
        imgui.end()

    # HUD bar at top
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(display_w, 32, imgui.ALWAYS)
    imgui.begin("##hud", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)

    imgui.text(f"Time: {B.get_world_time()}")
    imgui.same_line()
    imgui.set_cursor_pos((display_w - 140, 4))
    if imgui.button("Menu", 60, 22):
        _show_pause_menu = True
    imgui.same_line()
    if imgui.button("Save", 60, 22):
        _save_game(f"quicksave_{int(time.time())}")

    imgui.end()

    # Main game area: scene view centered
    margin = 60
    sw = display_w - margin * 2
    sh = display_h - 60
    imgui.set_next_window_position(margin, 40)
    imgui.set_next_window_size(sw, sh, imgui.ALWAYS)
    panel_scene_view()

    # Pause menu overlay
    if _show_pause_menu:
        _render_pause_menu(display_w, display_h)

    # Debug panels (optional)
    if _debug_mode:
        _render_debug_panels(display_w, display_h)


def _render_pause_menu(display_w: int, display_h: int):
    global _show_pause_menu

    pw = int(display_w * 0.35)
    ph = int(display_h * 0.45)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph, imgui.ALWAYS)
    imgui.begin("Paused", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_SCROLLBAR)

    btn_w = int(pw * 0.55)
    btn_h = int(ph * 0.10)
    spacing = int(ph * 0.035)
    line_h = imgui.get_text_line_height_with_spacing()

    # Title
    y = int(ph * 0.06)
    title_w = imgui.calc_text_size("Paused").x
    imgui.set_cursor_pos(((pw - title_w) // 2, y))
    imgui.text_colored("Paused", 0.94, 0.71, 0.18, 1.0)
    y += int(line_h * 2.0)

    num_btns = 5
    total = btn_h * num_btns + spacing * (num_btns - 1)
    y = int((ph - total) * 0.55)

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Resume", btn_w, btn_h):
        _show_pause_menu = False
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Save Game", btn_w, btn_h):
        _save_game(f"save_{int(time.time())}")
        _show_pause_menu = False
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Load Game", btn_w, btn_h):
        pass  # TODO: load slot browser
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Settings", btn_w, btn_h):
        _show_settings = True
    y += btn_h + spacing

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Quit to Menu", btn_w, btn_h):
        _quit_to_menu()

    imgui.end()


# ══════════════════════════════════════════════════════════════════
#  DEBUG PANELS (optional, toggled from Settings)
# ══════════════════════════════════════════════════════════════════

def _render_debug_panels(display_w: int, display_h: int):
    menu_h = 20
    rw = 240

    # Top menu bar for debug panels
    if imgui.begin_main_menu_bar():
        if imgui.begin_menu("Debug"):
            imgui.menu_item("Scene", None, True)
            imgui.menu_item("Story Timeline", None, True)
            imgui.menu_item("Entity Browser", None, True)
            imgui.menu_item("Entity Editor", None, True)
            imgui.menu_item("Event Log", None, True)
            imgui.menu_item("Agent Dashboard", None, True)
            imgui.menu_item("Prompt Inspector", None, True)
            imgui.menu_item("Automations", None, True)
            imgui.end_menu()
        imgui.end_main_menu_bar()

    # Story left panel
    imgui.set_next_window_position(0, menu_h)
    imgui.set_next_window_size(220, display_h - menu_h)
    panel_story_timeline()

    # Entity Browser right panel
    imgui.set_next_window_position(display_w - rw, menu_h)
    imgui.set_next_window_size(rw, display_h - menu_h)
    panel_entity_browser(on_select=editor_open)

    # Center
    left = 225
    right = rw + 5
    cw = display_w - left - right
    lcol_w = int(cw * 0.55)
    rcol_w = cw - lcol_w - 5

    imgui.set_next_window_position(left, menu_h)
    imgui.set_next_window_size(lcol_w, int(display_h * 0.50))
    panel_scene_view()

    imgui.set_next_window_position(left, menu_h + int(display_h * 0.50) + 5)
    imgui.set_next_window_size(lcol_w, display_h - menu_h - int(display_h * 0.50) - 10)
    panel_entity_editor()

    # Right column stack
    rcol_x = left + lcol_w + 5
    row_y = menu_h

    imgui.set_next_window_position(rcol_x, row_y)
    imgui.set_next_window_size(rcol_w, int(display_h * 0.30))
    panel_agent_dashboard()
    row_y += int(display_h * 0.30) + 3

    imgui.set_next_window_position(rcol_x, row_y)
    imgui.set_next_window_size(rcol_w, int(display_h * 0.20))
    panel_effect_monitor()
    row_y += int(display_h * 0.20) + 3

    imgui.set_next_window_position(rcol_x, row_y)
    imgui.set_next_window_size(rcol_w, display_h - row_y - 5)
    panel_event_log()


# ══════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════

#: The atlas keeps the ranges alive until the texture is built from them, so the
#: object stands here rather than in a local that the loaders could outlive.
_GLYPH_RANGES: list = []


def _glyph_ranges():
    """Everything the game's text is written with, in one atlas.

    Both alphabets the sessions are played in, the punctuation prose arrives
    with (long dashes, ellipses, quotes), the arrows and marks the reader uses
    to point at things, and the Latin-1 range that carries the guillemets.
    """
    if not _GLYPH_RANGES:
        _GLYPH_RANGES.append(imgui.GlyphRanges([
            0x0020, 0x00FF,   # Latin and Latin-1: « » · and the rest
            0x0400, 0x04FF,   # Cyrillic
            0x2000, 0x206F,   # punctuation: — … “ ” • ‰
            0x2190, 0x21FF,   # arrows: ← → ↑ ↓
            0x25A0, 0x25FF,   # marks: ▸ ◆ ●
            0,                # the end of the list
        ]))
    return _GLYPH_RANGES[0]


def main():
    glfw.init()
    glfw.window_hint(glfw.RESIZABLE, glfw.TRUE)
    glfw.window_hint(glfw.SCALE_TO_MONITOR, glfw.TRUE)

    window = glfw.create_window(1600, 900, "LLM World", None, None)
    if not window:
        glfw.terminate()
        sys.exit(1)

    glfw.make_context_current(window)
    glfw.swap_interval(1)

    # The window draws the panels and nothing else behind them: a cleared frame,
    # with no depth buffer and no view to move through.
    ctx = mgl.create_context()
    ctx.clear_color = (0.02, 0.02, 0.06, 1.0)

    # Dear ImGui
    imgui.create_context()
    io = imgui.get_io()

    wx, wy = glfw.get_window_content_scale(window)
    dpi_scale = max(wx, wy, 1.0)

    _init_style(scale=dpi_scale)
    impl = GlfwRenderer(window)

    # Load font AFTER GlfwRenderer — clear default ProggyClean, load
    # Segoe UI (has Cyrillic), then rebuild GPU texture.
    #
    # The atlas holds the letters of both alphabets the game is played in *and*
    # the marks prose is written with — dashes, ellipses, quotes, arrows. A mark
    # that is not in the atlas reads as a question mark in the middle of a
    # sentence, and «Точки…» on a button becomes «Точки?».
    glyph_ranges = _glyph_ranges()
    font_size = 17.0 * dpi_scale
    font_loaded = False
    for fp in ("C:/Windows/Fonts/segoeui.ttf",
               "C:/Windows/Fonts/arial.ttf",
               "C:/Windows/Fonts/seguiemj.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(fp):
            try:
                io.fonts.clear()
                io.fonts.add_font_from_file_ttf(fp, font_size, None, glyph_ranges)
                font_loaded = True
                break
            except Exception:
                continue

    if font_loaded:
        # The sessions are read, so they are read in a size meant for reading:
        # the same face, one step larger, for the reader alone.
        from opengl_app import reader_panels as _reader
        for fp in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"):
            if not os.path.exists(fp):
                continue
            try:
                _reader.set_reader_font(io.fonts.add_font_from_file_ttf(
                    fp, 20.0 * dpi_scale, None, glyph_ranges))
                break
            except Exception:
                continue
        impl.refresh_font_texture()
    else:
        print("[App] Warning: no TTF font found, using default (ASCII only)")

    last_time = time.time()
    frame_count = 0
    fps_timer = 0

    print("[App] LLM World GUI ready")

    while not glfw.window_should_close(window):
        now = time.time()
        dt = now - last_time
        last_time = now

        frame_count += 1
        fps_timer += dt
        if fps_timer >= 1.0:
            glfw.set_window_title(window, f"LLM World — {frame_count}fps")
            frame_count = 0
            fps_timer = 0

        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()

        io = imgui.get_io()
        dw, dh = io.display_size
        if dw < 100:
            dw, dh = 1280, 720

        # Game state machine
        if state == GameState.MENU:
            _render_main_menu(dw, dh)
        elif state == GameState.PLAYING:
            _render_game_view(dw, dh)
        elif state == GameState.PAUSED:
            _render_game_view(dw, dh)
            _render_pause_menu(dw, dh)
        elif state == GameState.READER:
            from opengl_app import reader_panels as _reader
            _reader.render(dw, dh)

        if _show_demo:
            imgui.show_demo_window()

        # Render
        glfw.make_context_current(window)
        ctx.clear()
        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    impl.shutdown()
    glfw.terminate()


if __name__ == "__main__":
    main()
