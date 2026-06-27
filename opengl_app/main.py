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
from typing import Optional

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import glfw
import moderngl as mgl
import imgui
from imgui.integrations.glfw import GlfwRenderer
from opengl_app.camera import Camera
from opengl_app.gui_bridge import GuiBridge as B

# Import panel functions but they're only used in debug mode
from opengl_app.gui_panels import (
    panel_scene_view, panel_entity_browser, panel_entity_editor,
    panel_story_timeline, panel_event_log, panel_agent_dashboard,
    panel_prompt_inspector, panel_script_monitor,
    editor_open, _init_style,
)

# ── Game state ──

class GameState:
    MENU = 0
    PLAYING = 1
    PAUSED = 2

state = GameState.MENU
_save_list: list = []
_save_slots: list = []
_selected_slot: int = -1
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
_preset_desc_input: str = ""
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
                desc = d / "description.json"
                if desc.exists():
                    try:
                        data = json.loads(desc.read_text(encoding="utf-8"))
                        presets.append({"name": d.name, "title": data.get("title", d.name),
                                        "description": data.get("description", "")})
                    except Exception:
                        presets.append({"name": d.name, "title": d.name, "description": ""})
                else:
                    presets.append({"name": d.name, "title": d.name, "description": ""})
    return presets


def _start_game(preset: str = "") -> None:
    global state, _game_started, _status_msg, _status_timer
    try:
        # TODO: wire to console_app initialization flow
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


def _create_preset(preset_name: str, description: str) -> str:
    """Export current init/ (characters + plot) into setups/{preset_name}/.

    Returns empty string on success, or error message on failure.
    """
    init_dir = Path("init")
    setups_dir = Path("setups")

    if not preset_name.strip():
        return "Preset name cannot be empty."

    # Sanitize name: only alphanumeric, hyphens, underscores
    safe = "".join(c for c in preset_name.strip() if c.isalnum() or c in "-_ ")
    safe = safe.strip().replace(" ", "_")
    if not safe:
        return "Invalid preset name. Use letters, numbers, hyphens, underscores."

    dst = setups_dir / safe
    if dst.exists():
        return f"Preset '{safe}' already exists in setups/."

    try:
        setups_dir.mkdir(parents=True, exist_ok=True)
        chars_src = init_dir / "characters"
        plot_src = init_dir / "plot.json"

        # description.json
        desc = {"title": preset_name.strip(), "description": description.strip()}
        (dst / "description.json").write_text(
            json.dumps(desc, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # plot.json
        if plot_src.exists():
            shutil.copy2(plot_src, dst / "plot.json")

        # characters/
        if chars_src.exists() and chars_src.is_dir():
            for char_dir in sorted(chars_src.iterdir()):
                if not char_dir.is_dir():
                    continue
                char_dst = dst / "characters" / char_dir.name
                char_dst.mkdir(parents=True, exist_ok=True)
                for json_file in char_dir.glob("*.json"):
                    shutil.copy2(json_file, char_dst / json_file.name)

        # Refresh preset list
        global _init_presets
        _init_presets = _list_init_presets()

        return ""  # success
    except Exception as e:
        # Clean up on failure
        if dst.exists():
            shutil.rmtree(dst)
        return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════
#  MAIN MENU
# ══════════════════════════════════════════════════════════════════

def _render_main_menu(display_w: int, display_h: int):
    global _show_new_game_popup, _show_settings, _selected_preset, _init_presets, _show_create_preset_popup

    # Responsive panel — fills most of the window nicely
    pw = int(display_w * 0.55)
    ph = int(display_h * 0.65)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph)
    imgui.begin("LLM World", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)

    line_h = imgui.get_text_line_height_with_spacing()

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
    num_btns = 4  # New Game, Create Preset, Settings, Exit
    if saves:
        num_btns += 2  # Continue + Load Game
    total_btn_h = btn_h * num_btns + spacing * (num_btns - 1)
    y = int((ph - total_btn_h) * 0.58)

    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("New Game", btn_w, btn_h):
        _init_presets = _list_init_presets()
        if _init_presets:
            _show_new_game_popup = True
        else:
            _show_create_preset_popup = True
            _preset_name_input = ""
            _preset_desc_input = ""
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
        _preset_desc_input = ""
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

    # ── Create Preset popup ──
    if _show_create_preset_popup:
        _render_create_preset_popup(display_w, display_h)


def _render_new_game_popup(display_w: int, display_h: int):
    global _show_new_game_popup, _init_presets, _selected_preset

    pw = int(display_w * 0.50)
    ph = int(display_h * 0.55)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph)
    imgui.begin("New Game", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_SCROLLBAR)

    # Title
    y = 15
    title_w = imgui.calc_text_size("New Game").x
    imgui.set_cursor_pos(((pw - title_w) // 2, y))
    imgui.text_colored("New Game", 0.23, 0.51, 0.96, 1.0)
    y += 28

    imgui.set_cursor_pos((15, y))
    imgui.text("Select World Preset:")
    y += 22
    imgui.set_cursor_pos((15, y))
    imgui.separator()
    y += 8

    if not _init_presets:
        _init_presets = _list_init_presets()
        if not _init_presets:
            imgui.set_cursor_pos(((pw - 220) // 2, y + 20))
            imgui.text_colored("No presets found in setups/", 0.94, 0.27, 0.27, 1.0)

    for i, preset in enumerate(_init_presets):
        imgui.set_cursor_pos((25, y))
        selected = i == _selected_preset
        clicked, _ = imgui.selectable(f"{preset['title']}##preset{i}", selected, 0, (pw - 50, 0))
        if clicked:
            _selected_preset = i
        y += imgui.get_text_line_height_with_spacing()
        if selected and preset.get("description"):
            imgui.set_cursor_pos((35, y))
            imgui.text_colored(preset["description"][:300], 0.58, 0.64, 0.72, 1.0)
            y += imgui.get_text_line_height_with_spacing()

    # Bottom buttons — centered
    btn_w = 170
    btn_h = 32
    y = ph - btn_h - 15
    imgui.set_cursor_pos(((pw - btn_w * 2 - 12) // 2, y))
    if _selected_preset >= 0 and _selected_preset < len(_init_presets):
        if imgui.button("Start", btn_w, btn_h):
            _start_game(_init_presets[_selected_preset]["name"])
            _show_new_game_popup = False

    imgui.same_line()
    if imgui.button("Cancel", btn_w, btn_h):
        _show_new_game_popup = False

    imgui.end()


def _render_settings_popup(display_w: int, display_h: int):
    global _show_settings, _debug_mode

    pw = int(display_w * 0.40)
    ph = int(display_h * 0.40)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph)
    imgui.begin("Settings", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_SCROLLBAR)

    lh = imgui.get_text_line_height_with_spacing()
    margin = int(pw * 0.05)

    y = int(ph * 0.08)
    t_w = imgui.calc_text_size("Settings").x
    imgui.set_cursor_pos(((pw - t_w) // 2, y))
    imgui.text_colored("Settings", 0.23, 0.51, 0.96, 1.0)
    y += int(lh * 2.0)

    imgui.set_cursor_pos((margin, y))
    imgui.separator()
    y += int(lh * 1.2)

    imgui.set_cursor_pos((margin, y))
    _, _debug_mode = imgui.checkbox("Debug Mode (панели)", _debug_mode)
    y += int(lh * 1.8)

    imgui.set_cursor_pos((margin, y))
    imgui.text_colored("⚠ Debug panels показывают внутреннее состояние движка.", 0.96, 0.62, 0.04, 1.0)
    y += int(lh * 1.0)
    imgui.set_cursor_pos((margin, y))
    imgui.text_colored("Не предназначены для нормальной игры.", 0.96, 0.62, 0.04, 1.0)

    btn_w = int(pw * 0.25)
    btn_h = int(lh * 1.8)
    y = ph - btn_h - int(lh * 1.2)
    imgui.set_cursor_pos(((pw - btn_w) // 2, y))
    if imgui.button("Закрыть", btn_w, btn_h):
        _show_settings = False

    imgui.end()


def _render_create_preset_popup(display_w: int, display_h: int):
    global _show_create_preset_popup, _preset_name_input, _preset_desc_input, _preset_create_result

    pw = int(display_w * 0.50)
    ph = int(display_h * 0.55)
    imgui.set_next_window_position((display_w - pw) // 2, (display_h - ph) // 2)
    imgui.set_next_window_size(pw, ph)
    imgui.begin("Create Preset", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_SCROLLBAR)

    lh = imgui.get_text_line_height_with_spacing()
    margin = int(pw * 0.04)
    field_w = pw - margin * 2

    # ── Title ──
    y = int(ph * 0.06)
    t_w = imgui.calc_text_size("Create Preset").x
    imgui.set_cursor_pos(((pw - t_w) // 2, y))
    imgui.text_colored("Create Preset", 0.23, 0.51, 0.96, 1.0)
    y += int(lh * 1.8)

    # ── Subtitle ──
    imgui.set_cursor_pos((margin, y))
    imgui.text("Сохранить текущих персонажей и сюжет как готовый пресет")
    y += int(lh * 1.6)
    imgui.set_cursor_pos((margin, y))
    imgui.separator()
    y += int(lh * 0.8)

    # ── Preset name ──
    imgui.set_cursor_pos((margin, y))
    imgui.text("Название пресета:")
    y += int(lh * 1.0)
    imgui.set_cursor_pos((margin, y))
    input_h = int(lh * 1.6)
    changed, _preset_name_input = imgui.input_text("##preset_name",
                                                     _preset_name_input, 128,
                                                     imgui.INPUT_TEXT_CHARS_NO_BLANK
                                                     if hasattr(imgui, 'INPUT_TEXT_CHARS_NO_BLANK') else 0)
    y += input_h + int(lh * 0.6)

    # ── Description ──
    imgui.set_cursor_pos((margin, y))
    imgui.text("Описание (необязательно):")
    y += int(lh * 1.0)
    ml_h = int(lh * 3.5)
    imgui.set_cursor_pos((margin, y))
    _, _preset_desc_input = imgui.input_text_multiline("##preset_desc",
                                                         _preset_desc_input, 512, ml_h, 0)
    y += ml_h + int(lh * 0.8)

    # ── Preview block ──
    imgui.set_cursor_pos((margin, y))
    imgui.separator()
    y += int(lh * 0.6)
    imgui.set_cursor_pos((margin, y))
    imgui.text_colored("В пресет войдут:", 0.58, 0.64, 0.72, 1.0)
    y += int(lh * 0.2)

    init_dir = Path("init")
    plot_src = init_dir / "plot.json"
    chars_src = init_dir / "characters"

    # Plot line
    y += int(lh * 0.6)
    imgui.set_cursor_pos((margin + int(lh * 0.5), y))
    if plot_src.exists():
        imgui.text("- Сюжет: " + _preview_text(plot_src))
    else:
        imgui.text_colored("! Сюжет (plot.json) не найден", 0.94, 0.62, 0.04, 1.0)
    y += int(lh * 1.0)

    # Character lines
    if chars_src.exists() and chars_src.is_dir():
        names = sorted(d.name for d in chars_src.iterdir() if d.is_dir())
        if names:
            imgui.set_cursor_pos((margin + int(lh * 0.5), y))
            imgui.text("- Персонажи: " + ", ".join(names))
            y += int(lh * 1.0)
        else:
            imgui.set_cursor_pos((margin + int(lh * 0.5), y))
            imgui.text_colored("! Нет персонажей в init/characters/", 0.94, 0.62, 0.04, 1.0)
            y += int(lh * 1.0)

    # Destination hint
    imgui.set_cursor_pos((margin, y))
    safe_name = _preset_name_input.strip().replace(" ", "_") or "..."
    imgui.text_colored(f"> сохранится в setups/{safe_name}/",
                       0.4, 0.4, 0.4, 1.0)
    y += int(lh * 0.5)

    # ── Error message ──
    if _preset_create_result:
        y += int(lh * 0.3)
        imgui.set_cursor_pos((margin, y))
        is_err = _preset_create_result.startswith("Error") or "cannot" in _preset_create_result.lower() or "already" in _preset_create_result.lower() or "Invalid" in _preset_create_result
        if is_err:
            imgui.text_colored(_preset_create_result, 0.94, 0.27, 0.27, 1.0)
        else:
            imgui.text_colored(_preset_create_result, 0.27, 0.94, 0.27, 1.0)
        y += int(lh * 1.2)

    # ── Bottom buttons ──
    btn_w = int(pw * 0.25)
    btn_h = int(lh * 1.8)
    y = ph - btn_h - int(lh * 0.8)
    imgui.set_cursor_pos(((pw - btn_w * 2 - int(lh * 0.5)) // 2, y))
    if imgui.button("Создать пресет", btn_w, btn_h):
        _preset_create_result = _create_preset(_preset_name_input, _preset_desc_input)
        if not _preset_create_result:
            _show_create_preset_popup = False

    imgui.same_line()
    if imgui.button("Отмена", btn_w, btn_h):
        _show_create_preset_popup = False

    imgui.end()


def _preview_text(path: Path, max_len: int = 80) -> str:
    """Return first max_len chars of a JSON file's 'init' or first string value."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = data.get("init") or next((v for v in data.values() if isinstance(v, str)), "")
            if isinstance(val, str):
                return val[:max_len] + (".." if len(val) > max_len else "")
        return "(file " + path.name + ")"
    except Exception:
        return "(file " + path.name + ")"


# ══════════════════════════════════════════════════════════════════
#  GAME VIEW
# ══════════════════════════════════════════════════════════════════

def _render_game_view(display_w: int, display_h: int):
    global _show_pause_menu, _status_timer, _status_msg

    # Status message (if any)
    if _status_timer > time.time():
        imgui.set_next_window_position(display_w // 2 - 150, 5)
        imgui.set_next_window_size(300, 30)
        imgui.begin("##status", None,
                    imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                    | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR)
        tw = imgui.calc_text_size(_status_msg).x
        imgui.set_cursor_pos((150 - tw // 2, 5))
        imgui.text(_status_msg)
        imgui.end()

    # HUD bar at top
    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(display_w, 32)
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
    imgui.set_next_window_size(sw, sh)
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
    imgui.set_next_window_size(pw, ph)
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
        if imgui.begin_menu("Tools"):
            if imgui.menu_item("Tick (effects+scripts)")[0]:
                B.tick(current_tick=int(time.time()))
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
    panel_script_monitor()
    row_y += int(display_h * 0.20) + 3

    imgui.set_next_window_position(rcol_x, row_y)
    imgui.set_next_window_size(rcol_w, display_h - row_y - 5)
    panel_event_log()


# ══════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════

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

    ctx = mgl.create_context()
    ctx.enable(mgl.DEPTH_TEST)
    ctx.clear_color = (0.02, 0.02, 0.06, 1.0)

    camera = Camera(distance=3.5)
    glfw.set_scroll_callback(window, lambda w, xo, yo: camera.zoom(-yo * 50))

    # Dear ImGui
    imgui.create_context()
    io = imgui.get_io()

    wx, wy = glfw.get_window_content_scale(window)
    dpi_scale = max(wx, wy, 1.0)

    # Load font BEFORE GlfwRenderer — so the default ProggyClean is replaced
    # by a proper TTF with Cyrillic at the correct size.
    font_size = 16.0 * dpi_scale
    font_loaded = False
    for fp in ("C:/Windows/Fonts/segoeui.ttf",
               "C:/Windows/Fonts/arial.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(fp):
            try:
                # Remove default ProggyClean, add our TTF as the sole font
                io.fonts.clear()
                io.fonts.add_font_from_file_ttf(fp, font_size)
                font_loaded = True
                break
            except Exception:
                continue

    if not font_loaded:
        print("[App] Warning: no TTF font found, using default (small)")

    _init_style(scale=dpi_scale)
    impl = GlfwRenderer(window)

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
