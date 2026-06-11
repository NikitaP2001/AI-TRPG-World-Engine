"""Main entry point — AI World Engine GUI.

Launches the desktop application with an OpenGL viewport.
Accepts a path to generated cell data (Parquet file).

Usage:
    python -m opengl_app.main [path/to/cells.parquet]

If no path is given, looks for game/simulation/cells.parquet.
If not found there, auto-runs the generator.
"""

from __future__ import annotations

import os
import sys
import time
import subprocess
from pathlib import Path

# Add project root
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import glfw
import moderngl as mgl
import numpy as np

from opengl_app.camera import Camera
from opengl_app.planet_scene import PlanetScene


def _ensure_data(parquet_path: str) -> str:
    """Ensure cell data exists at the given path. Auto-generate if missing."""
    if os.path.isfile(parquet_path):
        return parquet_path

    # Try auto-generation
    output_dir = os.path.dirname(parquet_path)
    print(f"[App] Cell data not found: {parquet_path}")
    print(f"[App] Auto-generating to {output_dir}...")

    result = subprocess.run(
        [sys.executable, "-m", "simulation.layer0.run", "--output-dir", output_dir],
        cwd=_project_root,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[Error] Generator failed:\n{result.stderr}")
        sys.exit(1)
    print(result.stdout)

    if os.path.isfile(parquet_path):
        print(f"[App] Generation complete: {parquet_path}")
        return parquet_path

    print(f"[Error] Generation produced no output at {parquet_path}")
    sys.exit(1)


def main():
    # Resolve data path
    if len(sys.argv) > 1:
        parquet_path = sys.argv[1]
    else:
        parquet_path = os.path.join(_project_root, "game", "simulation", "cells.parquet")

    parquet_path = _ensure_data(parquet_path)

    # ── Window setup ──
    glfw.init()
    glfw.window_hint(glfw.RESIZABLE, glfw.TRUE)
    glfw.window_hint(glfw.SAMPLES, 4)  # MSAA

    window = glfw.create_window(1280, 720, "World Engine — Planet Viewer", None, None)
    if not window:
        glfw.terminate()
        sys.exit(1)

    glfw.make_context_current(window)
    glfw.swap_interval(1)  # VSync

    # ── ModernGL context ──
    ctx = mgl.create_context()
    ctx.enable(mgl.DEPTH_TEST)
    ctx.multisample = True
    ctx.clear_color = (0.02, 0.02, 0.06, 1.0)

    # ── Camera ──
    camera = Camera(distance=3.5)
    cam_dragging = False
    cam_last_x = 0
    cam_last_y = 0
    cam_press_x = 0
    cam_press_y = 0
    click_threshold = 5  # pixels — movement below this = click, above = drag
    scrollbar_drag = False

    # ── Scene ──
    scene = PlanetScene(parquet_path)
    scene.setup(ctx)

    # ── Scroll callback (set once) ──
    # ── Scroll callback (set once) — always zoom ──
    glfw.set_scroll_callback(window, lambda w, xo, yo: camera.zoom(-yo * 50))

    # ── Key callback — view mode switching ──
    def _key_cb(w, key, scancode, action, mods):
        if action == glfw.PRESS:
            if key == glfw.KEY_1:
                scene.set_view_mode(0)
            elif key == glfw.KEY_2:
                scene.set_view_mode(1)
            elif key == glfw.KEY_3:
                scene.set_view_mode(2)
            elif key == glfw.KEY_4:
                scene.set_view_mode(3)
            elif key == glfw.KEY_5:
                scene.set_view_mode(4)
            elif key == glfw.KEY_TAB:
                scene.cycle_view_mode()
    glfw.set_key_callback(window, _key_cb)

    # ── Main loop ──
    last_time = time.time()
    frame_count = 0
    fps_timer = 0

    print("[App] Running. Drag to orbit, scroll to zoom. Keys 1-5: view modes, Tab: cycle.")

    while not glfw.window_should_close(window):
        now = time.time()
        dt = now - last_time
        last_time = now

        # FPS counter
        frame_count += 1
        fps_timer += dt
        if fps_timer >= 1.0:
            title = f"World Engine — {scene.name()} — {frame_count}fps"
            glfw.set_window_title(window, title)
            frame_count = 0
            fps_timer = 0

        # ── Handle input ──
        glfw.poll_events()

        # Camera orbit + click detection + scrollbar drag
        if glfw.get_mouse_button(window, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS:
            x, y = glfw.get_cursor_pos(window)
            w_win, h_win = glfw.get_window_size(window)
            if not cam_dragging:
                # Check if click is on scrollbar
                panel_right = w_win // 3
                if scene.selected_cell_data and panel_right - 15 < x < panel_right:
                    scrollbar_drag = True
                    scene.start_scrollbar_drag(y, h_win)
                cam_dragging = True
                cam_last_x = x
                cam_last_y = y
                cam_press_x = x
                cam_press_y = y
            else:
                if scrollbar_drag:
                    scene.drag_scrollbar(y, h_win)
                else:
                    dx = x - cam_last_x
                    dy = y - cam_last_y
                    camera.orbit(dx, dy)
                cam_last_x = x
                cam_last_y = y
        else:
            if cam_dragging:
                if not scrollbar_drag:
                    # Check if it was a click (not a drag)
                    dx = cam_last_x - cam_press_x
                    dy = cam_last_y - cam_press_y
                    if abs(dx) < click_threshold and abs(dy) < click_threshold:
                        w, h = glfw.get_framebuffer_size(window)
                        scene.on_click(cam_press_x, cam_press_y, w, h, camera, ctx)
                scene.end_scrollbar_drag()
                scrollbar_drag = False
            cam_dragging = False

        # Resize
        w, h = glfw.get_framebuffer_size(window)
        ctx.viewport = (0, 0, w, h)
        camera.set_aspect(w / max(h, 1))

        # ── Render ──
        ctx.clear()
        scene.update(dt)
        camera.update(dt)
        scene.render(ctx, camera)

        glfw.swap_buffers(window)

    # ── Cleanup ──
    scene.cleanup()
    glfw.terminate()


if __name__ == "__main__":
    main()
