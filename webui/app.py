from __future__ import annotations

import asyncio
import os
import json
import shutil
import signal
import threading
import html
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from console_app import ConsoleApp
from backup_storylines import (
    create_story_line,
    delete_story_line,
    list_story_lines,
    switch_story_line,
    checkout_turn_by_key,
)

from .override_state import OverrideStore
from .state import ParagraphRef, WorldStateReader


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

if not os.getenv("LLM_WORLD_LOGS_DIR"):
    os.environ["LLM_WORLD_LOGS_DIR"] = os.path.join(REPO_ROOT, "logs")
if not os.getenv("LLM_WORLD_STREAM_PATH"):
    os.environ["LLM_WORLD_STREAM_PATH"] = os.path.join(REPO_ROOT, "logs", "stream.txt")

# Track active SSE connections for clean shutdown
_active_sse_tasks: set[asyncio.Task] = set()
_shutdown_event = asyncio.Event()
_TURN_COMMAND_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for clean startup/shutdown."""
    # Startup
    _shutdown_event.clear()
    yield
    # Shutdown - signal all SSE streams to stop
    _shutdown_event.set()
    # Give SSE tasks a moment to clean up
    if _active_sse_tasks:
        await asyncio.sleep(0.1)
        for task in _active_sse_tasks:
            if not task.done():
                task.cancel()


app = FastAPI(title="LLM World Web UI", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# ── Planet viewer textures ──
_TEXTURES_DIR = os.path.join(REPO_ROOT, "data", "planet_test", "textures")
if os.path.isdir(_TEXTURES_DIR):
    app.mount(
        "/static/textures",
        StaticFiles(directory=_TEXTURES_DIR),
        name="planet_textures",
    )


@app.get("/planet")
def planet_viewer():
    return FileResponse(os.path.join(REPO_ROOT, "webui", "static", "webgl", "index.html"))


def _highlight_quotes_html(text: object) -> str:
    s = str(text or "")
    if not s:
        return ""

    # Highlight direct speech wrapped in regular or typographic quotes.
    # Keep all non-highlighted content HTML-escaped.
    pattern = re.compile(r'"[^"\n]*"|“[^”\n]*”')
    out: list[str] = []
    last = 0
    for m in pattern.finditer(s):
        a, b = m.span()
        if a > last:
            out.append(html.escape(s[last:a]))
        out.append(f'<span class="speech-quote">{html.escape(m.group(0))}</span>')
        last = b
    if last < len(s):
        out.append(html.escape(s[last:]))
    return "".join(out)


templates.env.filters["highlight_quotes"] = _highlight_quotes_html

state = WorldStateReader(REPO_ROOT)
overrides = OverrideStore(REPO_ROOT)


def _reload_world_state_reader() -> None:
    global state
    state = WorldStateReader(REPO_ROOT)


def _stream_state_path() -> str:
    return os.path.join(REPO_ROOT, "game", "user_inputs", "webui_stream_state.json")


def _stream_log_path() -> str:
    raw = (os.getenv("LLM_WORLD_STREAM_PATH") or "").strip()
    if raw:
        return raw
    return os.path.join(REPO_ROOT, "logs", "stream.txt")


def _stream_state_load() -> dict:
    try:
        with open(_stream_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _stream_state_save(data: dict) -> None:
    path = _stream_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _stream_state_begin() -> None:
    stream_path = _stream_log_path()
    try:
        os.makedirs(os.path.dirname(stream_path), exist_ok=True)
        if not os.path.exists(stream_path):
            with open(stream_path, "w", encoding="utf-8") as f:
                f.write("")
        start = int(os.path.getsize(stream_path))
    except Exception:
        start = 0
    _stream_state_save(
        {
            "active": True,
            "start_offset": start,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _stream_state_clear() -> None:
    _stream_state_save({"active": False, "start_offset": 0, "started_at": ""})


def _did_story_progress(
    before_snapshot: tuple[int, bool, int, int, int],
    after_snapshot: tuple[int, bool, int, int, int],
) -> bool:
    try:
        return (int(after_snapshot[2]) > int(before_snapshot[2])) or (
            int(after_snapshot[3]) > int(before_snapshot[3])
        )
    except Exception:
        return False


def _known_characters() -> list[str]:
    # Simple directory-based listing; does not require loading full story.
    root = os.path.join(REPO_ROOT, "game", "characters")
    try:
        names = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
        return sorted([n for n in names if n and not n.startswith("__")])
    except Exception:
        return []


def _render_composer(
    *,
    request: Request,
    selected_character: str = "",
    message: str = "",
    gm_question: str = "",
    gm_answer: str = "",
) -> HTMLResponse:
    pending = overrides.get_pending_prompt()
    if not (selected_character or "").strip():
        try:
            selected_character = overrides.armed_character()
        except Exception:
            selected_character = ""
    try:
        auto_pause = ConsoleApp._auto_scene_pause
    except Exception:
        auto_pause = True
    return templates.TemplateResponse(
        request,
        "partials/composer.html",
        {
            "pending_prompt": pending,
            "selected_character": selected_character,
            "character_names": _known_characters(),
            "message": message,
            "gm_question": gm_question,
            "gm_answer": gm_answer,
            "auto_scene_pause": auto_pause,
        },
    )


def _ref_from_query(arc: int, para: str) -> ParagraphRef:
    if para == "ongoing":
        return ParagraphRef(arc_index=arc, kind="ongoing", paragraph_index=None)
    return ParagraphRef(arc_index=arc, kind="paragraph", paragraph_index=int(para))


@app.get("/", response_class=HTMLResponse)
def index(request: Request, arc: int = 0, para: str = "ongoing"):
    arc_tree = state.get_arc_tree()
    ref = _ref_from_query(arc, para)

    details_index = state.build_turn_details_index()
    turns = [state.enrich_turn(t, details_index) for t in state.get_last_turns(ref, limit=10)]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "arc_tree": arc_tree,
            "current_arc": arc,
            "current_para": para,
            "turns": turns,
        },
    )


@app.get("/partials/composer", response_class=HTMLResponse)
def partial_composer(request: Request, selected: str = ""):
    return _render_composer(request=request, selected_character=(selected or "").strip())


@app.get("/partials/chat", response_class=HTMLResponse)
def partial_chat(request: Request, arc: int = 0, para: str = "ongoing", limit: int = 10):
    ref = _ref_from_query(arc, para)
    details_index = state.build_turn_details_index()
    turns = [state.enrich_turn(t, details_index) for t in state.get_last_turns(ref, limit=limit)]

    return templates.TemplateResponse(
        request,
        "partials/chat.html",
        {
            "turns": turns,
        },
    )


@app.get("/api/arcs", response_class=JSONResponse)
def api_arcs():
    return JSONResponse(state.get_arc_tree())


def _read_stream_txt(*, mode: str, lines: int) -> tuple[str, str]:
    """Return (text, note). Note may be empty.

    mode:
      - tail: last N lines of the whole file
      - turn: only content since the current turn started (offset-based)
      - full: full file (capped)
    """

    path = _stream_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            return ("", "")
    except Exception:
        return ("", "logs/stream.txt not found")

    try:
        # Guard: don't try to load arbitrarily large files into memory.
        size = os.path.getsize(path)
    except Exception:
        size = 0

    try:
        mode = (mode or "tail").strip().lower()

        if mode == "turn":
            st = _stream_state_load()
            if not bool(st.get("active")):
                return ("", "")
            start_offset = int(st.get("start_offset") or 0)
            if start_offset < 0:
                start_offset = 0
            if int(size) < start_offset:
                start_offset = 0

            with open(path, "rb") as f:
                f.seek(start_offset)
                raw = f.read()
            data = raw.decode("utf-8", errors="replace")

            all_lines = data.splitlines()
            tail_n = max(50, min(int(lines or 0), 5000))
            return ("\n".join(all_lines[-tail_n:]), "")

        if mode == "full" and size <= 1024 * 1024:
            return (open(path, "r", encoding="utf-8", errors="replace").read(), "")

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        all_lines = data.splitlines()
        tail_n = max(50, min(int(lines or 0), 5000))
        tail_text = "\n".join(all_lines[-tail_n:])
        note = ""
        if mode == "full" and size > 1024 * 1024:
            note = "File is large; showing tail instead of full."
        return (tail_text, note)
    except Exception as e:
        return ("", f"Failed to read stream.txt: {e}")


@app.get("/partials/stream_log", response_class=HTMLResponse)
def partial_stream_log(request: Request, mode: str = "tail", lines: int = 600):
    text, note = _read_stream_txt(mode=mode, lines=lines)
    return templates.TemplateResponse(
        request,
        "partials/stream_log.html",
        {
            "text": text,
            "note": note,
            "mode": (mode or "tail").strip().lower(),
        },
    )


def _prompt_path_for(*, agent: str, name: str = "") -> str:
    agent = (agent or "").strip().lower()
    if agent == "gm":
        return os.path.join(REPO_ROOT, "logs", "last_prompts", "gm__gm.txt")
    if agent == "character":
        safe = []
        for ch in (name or "").strip():
            if ch.isalnum() or ch in {"-", "_"}:
                safe.append(ch)
            elif ch.isspace():
                safe.append("_")
        slug = "".join(safe).strip("_") or "unknown"
        return os.path.join(REPO_ROOT, "logs", "last_prompts", f"character__{slug}.txt")
    return ""


def _read_text_file(path: str, *, max_chars: int = 200_000) -> str:
    if not path:
        return ""
    try:
        if not os.path.exists(path):
            return ""
        data = open(path, "r", encoding="utf-8", errors="replace").read()
        data = data or ""
        return data
    except Exception:
        return ""


@app.get("/partials/debug_clear", response_class=HTMLResponse)
def partial_debug_clear(request: Request):
    return templates.TemplateResponse(request, "partials/debug_empty.html", {})


@app.get("/partials/debug_shell", response_class=HTMLResponse)
def partial_debug_shell(request: Request, agent: str, name: str = ""):
    agent = (agent or "").strip().lower()
    title = "GM" if agent == "gm" else (name.strip() or "Character")
    return templates.TemplateResponse(
        request,
        "partials/debug_shell.html",
        {
            "agent": agent,
            "name": name,
            "title": title,
        },
    )


@app.get("/partials/debug_prompt", response_class=HTMLResponse)
def partial_debug_prompt(request: Request, agent: str, name: str = ""):
    p = _prompt_path_for(agent=agent, name=name)
    text = _read_text_file(p)
    if not text.strip():
        text = "(No prompt captured yet. Trigger a turn / agent call, then try again.)"
    return templates.TemplateResponse(
        request,
        "partials/debug_prompt.html",
        {
            "text": text,
        },
    )


@app.get("/stream")
async def stream(arc: int = 0, para: str = "ongoing"):
    """SSE stream: emits an event whenever story.json mtime changes."""

    async def gen():
        story_path = os.path.join(REPO_ROOT, "game", "world", "story.json")
        last_mtime: Optional[float] = None

        yield "event: ready\ndata: ok\n\n"

        try:
            while not _shutdown_event.is_set():
                try:
                    mtime = os.path.getmtime(story_path)
                except FileNotFoundError:
                    mtime = None

                if mtime is not None and mtime != last_mtime:
                    last_mtime = mtime
                    yield "event: story_changed\ndata: 1\n\n"

                # Use wait_for with timeout instead of plain sleep for faster shutdown response
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=0.5)
                    break  # Shutdown signaled
                except asyncio.TimeoutError:
                    pass  # Normal case - continue loop
        except asyncio.CancelledError:
            # Graceful shutdown on Ctrl+C
            pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/cmd/continue", response_class=HTMLResponse)
def cmd_continue(request: Request, override_character: str = Form("")):
    """Run one /continue tick.

    If override_character is set, arm a one-turn override.
    The engine will pause (without advancing the story) once that character is prompted.
    """

    if not _TURN_COMMAND_LOCK.acquire(blocking=False):
        return _render_composer(
            request=request,
            selected_character=(override_character or "").strip(),
            message="Another turn is already running. Please wait until it finishes.",
        )

    try:
        override_character = (override_character or "").strip()
        if override_character:
            overrides.arm(override_character)
        elif overrides.armed_character():
            # User switched back to "(none)" — disarm any pending override.
            overrides.disarm()

        # Start a new per-turn stream window.
        _stream_state_begin()

        console = ConsoleApp()
        # IMPORTANT: load persisted GM conversation history before running /continue.
        # The console runner does this in ConsoleApp.run(), but the web UI calls the
        # command dispatcher directly.
        console._load_session_state(show_history_stats=False)
        before_snapshot = console._progress_snapshot()
        try:
            console._dispatch_user_input(user_text="/continue 1", debug_trace=False)
        except Exception as e:  # noqa: BLE001
            return _render_composer(
                request=request,
                selected_character=override_character,
                message=f"Error: {e}",
            )

        after_snap = console._progress_snapshot()
        turn_finalized = _did_story_progress(before_snapshot, after_snap)

        pending = overrides.get_pending_prompt()
        msg = ""
        if pending is not None:
            msg = "Waiting for your character decision."
        elif turn_finalized:
            msg = "Turn complete."
        else:
            msg = "Turn did not finalize yet. Check stream log for blocking retries or try Continue again."
        return _render_composer(request=request, selected_character=override_character, message=msg)
    finally:
        _TURN_COMMAND_LOCK.release()


@app.post("/cmd/continue_with_decision", response_class=HTMLResponse)
def cmd_continue_with_decision(
    request: Request,
    character_name: str = Form(...),
    intent: str = Form(""),
    thoughts: str = Form(""),
):
    """Submit a one-off character decision and resume /continue.

    If both intent and thoughts are empty, treat it as 'skip override' and
    let the normal LLM agent handle the character.
    """

    if not _TURN_COMMAND_LOCK.acquire(blocking=False):
        return _render_composer(
            request=request,
            selected_character=(character_name or "").strip(),
            message="Another turn is already running. Please wait until it finishes.",
        )

    try:
        character_name = (character_name or "").strip()
        intent = (intent or "").strip()
        thoughts = (thoughts or "").strip()

        if not intent and not thoughts:
            # User submitted empty form — skip override, let the AI decide.
            overrides.disarm()
        else:
            overrides.set_pending_decision(
                character_name=character_name,
                intent=intent,
                thoughts=thoughts,
            )

        console = ConsoleApp()
        # IMPORTANT: load persisted GM conversation history before running /continue.
        console._load_session_state(show_history_stats=False)
        before_snapshot = console._progress_snapshot()
        try:
            console._dispatch_user_input(user_text="/continue 1", debug_trace=False)
        except Exception as e:  # noqa: BLE001
            return _render_composer(
                request=request,
                selected_character="",
                message=f"Error: {e}",
            )

        after_snap = console._progress_snapshot()
        turn_finalized = _did_story_progress(before_snapshot, after_snap)

        # If we consumed properly, the pending prompt should be gone.
        pending = overrides.get_pending_prompt()
        msg = ""
        if pending is not None:
            msg = "Still waiting for your character decision (engine re-prompted)."
        elif turn_finalized:
            msg = "Turn complete."
        else:
            msg = "Turn did not finalize yet. Check stream log for blocking retries or try Continue again."
        return _render_composer(request=request, selected_character="", message=msg)
    finally:
        _TURN_COMMAND_LOCK.release()


@app.post("/cmd/toggle_auto_scene_pause", response_class=HTMLResponse)
def cmd_toggle_auto_scene_pause(request: Request):
    """Toggle the auto-scene-pause flag."""
    try:
        ConsoleApp._auto_scene_pause = not ConsoleApp._auto_scene_pause
        state = "on" if ConsoleApp._auto_scene_pause else "off"
    except Exception:
        state = "?"
    return _render_composer(
        request=request,
        message=f"Stop on next scene: {state}",
    )


@app.post("/cmd/override_ask_gm", response_class=HTMLResponse)
def cmd_override_ask_gm(
    request: Request,
    character_name: str = Form(""),
    gm_question: str = Form(""),
):
    """Ask GM a question during an active character-override pause.

    This mirrors the character-agent `ask_game_master` flow by using
    GameMaster ANSWER_QUESTION task with current narrative context.
    """

    pending = overrides.get_pending_prompt()
    q = (gm_question or "").strip()

    if pending is None:
        return _render_composer(
            request=request,
            selected_character=(character_name or "").strip(),
            message="No active override prompt to ask from.",
            gm_question=q,
            gm_answer="",
        )

    if not q:
        return _render_composer(
            request=request,
            selected_character=pending.character_name,
            message="Question cannot be empty.",
            gm_question="",
            gm_answer="",
        )

    try:
        console = ConsoleApp()
        console._load_session_state(show_history_stats=False)

        from gm.game_master import build_game_master_qa_context

        payload = {
            "character_name": pending.character_name,
            "questions": q,
        }
        ctx = build_game_master_qa_context(console.world)
        answer = console._game_master.run_task(
            task="ANSWER_QUESTION",
            payload=payload,
            context_text=ctx,
        ).strip()
        if not answer:
            answer = "The GM did not provide an answer."

        return _render_composer(
            request=request,
            selected_character=pending.character_name,
            message="GM answered.",
            gm_question=q,
            gm_answer=answer,
        )
    except Exception as e:  # noqa: BLE001
        return _render_composer(
            request=request,
            selected_character=pending.character_name,
            message=f"Error: {e}",
            gm_question=q,
            gm_answer="",
        )


@app.post("/cmd/continue_until_paragraph", response_class=HTMLResponse)
def cmd_continue_until_paragraph(request: Request, override_character: str = Form("")):
    """Auto-continue until a new paragraph is produced.

    Keeps calling /continue turn-by-turn until paragraph count increases
    (normally every 10 finalized turns in the current arc).
    """

    if not _TURN_COMMAND_LOCK.acquire(blocking=False):
        return _render_composer(
            request=request,
            selected_character=(override_character or "").strip(),
            message="Another turn is already running. Please wait until it finishes.",
        )

    try:
        override_character = (override_character or "").strip()
        if override_character:
            overrides.arm(override_character)
        elif overrides.armed_character():
            overrides.disarm()

        # Use one ConsoleApp instance for the whole operation to avoid per-iteration
        # reinitialization side effects.
        console = ConsoleApp()
        console._load_session_state(show_history_stats=False)

        # Baseline progress for this operation.
        try:
            _baseline_turns, baseline_paragraphs, _baseline_last = console.story_progress_and_last_text()
        except Exception:
            baseline_paragraphs = 0

        max_iterations = 50  # Safety limit
        iterations = 0
        no_progress_streak = 0
        max_no_progress = 3

        while iterations < max_iterations:
            iterations += 1

            # Start a new per-turn stream window
            _stream_state_begin()

            before_snapshot = console._progress_snapshot()

            try:
                console._dispatch_user_input(user_text="/continue 1", debug_trace=False)
            except Exception as e:  # noqa: BLE001
                msg = f"Error: {e}"
                return _render_composer(request=request, selected_character=override_character, message=msg)

            # Check if override paused us
            pending = overrides.get_pending_prompt()
            if pending is not None:
                msg = f"Paused after {iterations} step(s) - waiting for your character decision."
                return _render_composer(request=request, selected_character=override_character, message=msg)

            # Check if turn finalized (reuse same logic as cmd_continue)
            after_snap = console._progress_snapshot()
            turn_finalized = _did_story_progress(before_snapshot, after_snap)

            # Stop only when a NEW paragraph is produced.
            try:
                _turns_now, paragraphs_now, _last_now = console.story_progress_and_last_text()
            except Exception:
                paragraphs_now = baseline_paragraphs

            if int(paragraphs_now) > int(baseline_paragraphs):
                msg = f"Paragraph complete after {iterations} turn(s)."
                return _render_composer(request=request, selected_character=override_character, message=msg)

            # If no story progress happened, allow a few retries before stopping.
            if not turn_finalized:
                no_progress_streak += 1
                if no_progress_streak >= max_no_progress:
                    msg = (
                        "No turn was finalized after multiple retries. "
                        "Check stream log for blockers and continue manually if needed."
                    )
                    return _render_composer(request=request, selected_character=override_character, message=msg)
            else:
                no_progress_streak = 0

        # Safety limit reached
        msg = f"Stopped after {iterations} turn(s) (safety limit) before paragraph completion."
        return _render_composer(request=request, selected_character=override_character, message=msg)
    finally:
        _TURN_COMMAND_LOCK.release()


# -----------------------------------------------------------------------------
# Config API: Game (restart/reset)
# -----------------------------------------------------------------------------

def _game_path() -> Path:
    return Path(REPO_ROOT) / "game"


def _get_game_status() -> dict:
    """Get current game status info."""
    game_path = _game_path()
    game_exists = game_path.exists()
    
    character_count = 0
    story_exists = False
    
    if game_exists:
        chars_path = game_path / "characters"
        if chars_path.exists():
            character_count = sum(1 for d in chars_path.iterdir() if d.is_dir() and not d.name.startswith("__"))
        
        story_path = game_path / "world" / "story.json"
        story_exists = story_path.exists()
    
    return {
        "game_exists": game_exists,
        "character_count": character_count,
        "story_exists": story_exists,
    }


@app.get("/partials/config_game", response_class=HTMLResponse)
def partial_config_game(request: Request):
    status = _get_game_status()
    return templates.TemplateResponse(
        request,
        "partials/config_game.html",
        {**status, "message": "", "success": True},
    )


@app.post("/api/config/game/restart", response_class=HTMLResponse)
def api_restart_game(request: Request):
    """Reset the game: delete game/ folder and re-initialize from init/."""
    from bootstrap import initialize_game_dir
    
    game_path = _game_path()
    
    # Try to delete the game directory
    try:
        if game_path.exists():
            shutil.rmtree(game_path)
    except Exception:
        # If deletion fails (Windows file locks), try renaming
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = Path(REPO_ROOT) / f"game_old_{ts}"
            if game_path.exists() and not backup.exists():
                game_path.rename(backup)
        except Exception as e:
            status = _get_game_status()
            return templates.TemplateResponse(
                request,
                "partials/config_game.html",
                {**status, "message": f"Failed to remove game folder: {e}", "success": False},
            )
    
    # Re-initialize from init/
    try:
        initialize_game_dir(init_root="init", game_root="game")
    except Exception as e:
        status = _get_game_status()
        return templates.TemplateResponse(
            request,
            "partials/config_game.html",
            {**status, "message": f"Failed to initialize: {e}", "success": False},
        )
    
    status = _get_game_status()
    return templates.TemplateResponse(
        request,
        "partials/config_game.html",
        {**status, "message": "Game restarted successfully! Reload the page to see the fresh state.", "success": True},
    )


# -----------------------------------------------------------------------------
# Config API: Characters (init/)
# -----------------------------------------------------------------------------

def _init_characters_path() -> Path:
    return Path(REPO_ROOT) / "init" / "characters"


def _parse_character_json(data: dict) -> dict:
    """Parse character JSON into a flat structure for the template."""
    result = {
        "name": data.get("name", ""),
        "general": data.get("general", ""),
        "race": data.get("race", ""),
        "location": data.get("location", ""),
        "personality": data.get("personality") or {},
        "appearance": data.get("appearance") or {},
        "status": data.get("status") or {},
        "skills_text": "",
        "equipment_text": "",
    }
    
    # Handle skills - can be dict with 'base' or other keys
    skills = data.get("skills")
    if isinstance(skills, dict):
        if "base" in skills:
            result["skills_text"] = skills["base"]
        else:
            # Join all skill entries
            parts = [f"{k}: {v}" for k, v in skills.items() if v]
            result["skills_text"] = "\n".join(parts)
    elif isinstance(skills, str):
        result["skills_text"] = skills
    
    # Handle equipment - can be dict, list, or string
    equipment = data.get("equipment")
    if isinstance(equipment, dict):
        if "base" in equipment:
            result["equipment_text"] = equipment["base"]
        else:
            parts = [f"{k}: {v}" for k, v in equipment.items() if v]
            result["equipment_text"] = "\n".join(parts)
    elif isinstance(equipment, list):
        result["equipment_text"] = "\n".join(str(e) for e in equipment)
    elif isinstance(equipment, str):
        result["equipment_text"] = equipment
    
    return result


def _list_init_characters() -> list[dict]:
    """List all characters in init/characters/ with their parsed data."""
    chars_path = _init_characters_path()
    if not chars_path.exists():
        return []

    characters = []
    for d in sorted(chars_path.iterdir()):
        if not d.is_dir() or d.name.startswith("__"):
            continue
        desc_path = d / "description.json"
        char_data = {"name": d.name}
        if desc_path.exists():
            try:
                raw = desc_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                char_data = _parse_character_json(data)
                char_data["name"] = d.name  # Use folder name as canonical
            except Exception:
                pass
        characters.append(char_data)
    return characters


def _build_character_json(
    general: str,
    race: str,
    location: str,
    personality_mbti: str,
    personality_alignment: str,
    personality_traits: str,
    personality_details: str,
    appearance: str,
    status_health: str,
    status_state: str,
    skills: str,
    equipment: str,
) -> dict:
    """Build a character JSON dict from form fields."""
    data = {}
    
    if general.strip():
        data["general"] = general.strip()
    if race.strip():
        data["race"] = race.strip()
    if location.strip():
        data["location"] = location.strip()
    
    # Personality
    personality = {}
    if personality_mbti.strip():
        personality["mbti"] = personality_mbti.strip()
    if personality_alignment.strip():
        personality["alignment"] = personality_alignment.strip()
    if personality_traits.strip():
        personality["traits"] = personality_traits.strip()
    if personality_details.strip():
        personality["details"] = personality_details.strip()
    if personality:
        data["personality"] = personality
    
    # Appearance
    if appearance.strip():
        data["appearance"] = {"base": appearance.strip()}
    
    # Status
    status = {}
    if status_health.strip():
        status["health"] = status_health.strip()
    if status_state.strip():
        status["state"] = status_state.strip()
    if status:
        data["status"] = status
    
    # Skills
    if skills.strip():
        data["skills"] = {"base": skills.strip()}
    
    # Equipment
    if equipment.strip():
        data["equipment"] = {"base": equipment.strip()}
    
    return data


@app.get("/partials/config_characters", response_class=HTMLResponse)
def partial_config_characters(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/config_characters.html",
        {"characters": _list_init_characters(), "message": "", "success": True},
    )


@app.post("/api/config/character/{name}", response_class=HTMLResponse)
def api_save_character(
    request: Request,
    name: str,
    general: str = Form(""),
    race: str = Form(""),
    location: str = Form(""),
    personality_mbti: str = Form(""),
    personality_alignment: str = Form(""),
    personality_traits: str = Form(""),
    personality_details: str = Form(""),
    appearance: str = Form(""),
    status_health: str = Form("healthy"),
    status_state: str = Form(""),
    skills: str = Form(""),
    equipment: str = Form(""),
):
    """Update an existing character's description.json."""
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "Character name required", "success": False},
        )

    char_path = _init_characters_path() / name
    if not char_path.exists():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Character '{name}' not found", "success": False},
        )

    if not general.strip():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "General description is required", "success": False},
        )

    if not race.strip():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "Race is required", "success": False},
        )

    data = _build_character_json(
        general=general,
        race=race,
        location=location,
        personality_mbti=personality_mbti,
        personality_alignment=personality_alignment,
        personality_traits=personality_traits,
        personality_details=personality_details,
        appearance=appearance,
        status_health=status_health,
        status_state=status_state,
        skills=skills,
        equipment=equipment,
    )

    desc_path = char_path / "description.json"
    try:
        content = json.dumps(data, indent=2, ensure_ascii=False)
        desc_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Failed to save: {e}", "success": False},
        )

    return templates.TemplateResponse(
        request,
        "partials/config_characters.html",
        {"characters": _list_init_characters(), "message": f"Saved '{name}'", "success": True},
    )


@app.post("/api/config/character", response_class=HTMLResponse)
def api_create_character(
    request: Request,
    name: str = Form(""),
    general: str = Form(""),
    race: str = Form(""),
    location: str = Form(""),
    personality_mbti: str = Form(""),
    personality_alignment: str = Form(""),
    personality_traits: str = Form(""),
    personality_details: str = Form(""),
):
    """Create a new character with basic info."""
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "Character name is required", "success": False},
        )

    if not general.strip():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "General description is required", "success": False},
        )

    if not race.strip():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": "Race is required", "success": False},
        )

    char_path = _init_characters_path() / name
    if char_path.exists():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Character '{name}' already exists", "success": False},
        )

    data = _build_character_json(
        general=general,
        race=race,
        location=location,
        personality_mbti=personality_mbti,
        personality_alignment=personality_alignment,
        personality_traits=personality_traits,
        personality_details=personality_details,
        appearance="",
        status_health="healthy",
        status_state="",
        skills="",
        equipment="",
    )

    try:
        char_path.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, ensure_ascii=False)
        (char_path / "description.json").write_text(content, encoding="utf-8")
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Failed to create: {e}", "success": False},
        )

    # If an active game exists, add the character there too and notify both agents.
    active_game_message = ""
    if (_game_path() / "characters").exists():
        try:
            console = ConsoleApp()
            console.add_character_to_active_game(name, data)
            active_game_message = " Also added to active game."
        except Exception as e:
            active_game_message = f" (Active game update failed: {e})"

    return templates.TemplateResponse(
        request,
        "partials/config_characters.html",
        {"characters": _list_init_characters(), "message": f"Created '{name}'.{active_game_message}", "success": True},
    )


@app.delete("/api/config/character/{name}", response_class=HTMLResponse)
def api_delete_character(request: Request, name: str):
    """Delete a character folder."""
    name = name.strip()
    char_path = _init_characters_path() / name
    if not char_path.exists():
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Character '{name}' not found", "success": False},
        )

    try:
        shutil.rmtree(char_path)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_characters.html",
            {"characters": _list_init_characters(), "message": f"Failed to delete: {e}", "success": False},
        )

    return templates.TemplateResponse(
        request,
        "partials/config_characters.html",
        {"characters": _list_init_characters(), "message": f"Deleted '{name}'", "success": True},
    )


# -----------------------------------------------------------------------------
# Config API: Plot (init/plot.json)
# -----------------------------------------------------------------------------

def _plot_path() -> Path:
    return Path(REPO_ROOT) / "init" / "plot.json"


@app.get("/partials/config_plot", response_class=HTMLResponse)
def partial_config_plot(request: Request):
    plot_path = _plot_path()
    init_text = ""
    exists = plot_path.exists()
    if exists:
        try:
            raw = plot_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            init_text = data.get("init", "") if isinstance(data, dict) else ""
        except Exception:
            init_text = ""

    return templates.TemplateResponse(
        request,
        "partials/config_plot.html",
        {"init": init_text, "exists": exists, "message": "", "success": True},
    )


@app.post("/api/config/plot", response_class=HTMLResponse)
def api_save_plot(request: Request, init: str = Form("")):
    """Save plot.json from form field."""
    init_text = init.strip()
    if not init_text:
        return templates.TemplateResponse(
            request,
            "partials/config_plot.html",
            {"init": "", "exists": _plot_path().exists(), "message": "Plot setup is required", "success": False},
        )

    data = {"init": init_text}
    content = json.dumps(data, indent=2, ensure_ascii=False)

    try:
        _plot_path().parent.mkdir(parents=True, exist_ok=True)
        _plot_path().write_text(content, encoding="utf-8")
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_plot.html",
            {"init": init_text, "exists": True, "message": f"Failed to save: {e}", "success": False},
        )

    return templates.TemplateResponse(
        request,
        "partials/config_plot.html",
        {"init": init_text, "exists": True, "message": "Plot saved", "success": True},
    )


@app.delete("/api/config/plot", response_class=HTMLResponse)
def api_delete_plot(request: Request):
    """Delete plot.json."""
    plot_path = _plot_path()
    if not plot_path.exists():
        return templates.TemplateResponse(
            request,
            "partials/config_plot.html",
            {"init": "", "exists": False, "message": "Plot not found", "success": False},
        )

    try:
        plot_path.unlink()
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_plot.html",
            {"init": "", "exists": False, "message": f"Failed to delete: {e}", "success": False},
        )

    return templates.TemplateResponse(
        request,
        "partials/config_plot.html",
        {"init": "", "exists": False, "message": "Plot deleted", "success": True},
    )


# -----------------------------------------------------------------------------
# Config API: Setups (init snapshots)
# -----------------------------------------------------------------------------

def _setups_path() -> Path:
    return Path(REPO_ROOT) / "setups"


def _init_path() -> Path:
    return Path(REPO_ROOT) / "init"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += int(p.stat().st_size)
            except Exception:
                continue
    except Exception:
        return 0
    return max(0, int(total))


def _format_size(size_bytes: int) -> str:
    size = float(max(0, int(size_bytes)))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def _list_setups() -> list[dict]:
    setups_path = _setups_path()
    if not setups_path.exists():
        return []

    out: list[dict] = []
    for d in sorted(setups_path.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue

        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime)
            date_str = mtime.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = "Unknown"

        chars_count = 0
        chars_path = d / "characters"
        if chars_path.exists():
            try:
                chars_count = sum(1 for x in chars_path.iterdir() if x.is_dir() and not x.name.startswith("__"))
            except Exception:
                chars_count = 0

        plot_exists = (d / "plot.json").exists()

        out.append(
            {
                "name": d.name,
                "date": date_str,
                "characters": chars_count,
                "plot_exists": plot_exists,
                "size": _format_size(_dir_size(d)),
            }
        )
    return out


def _save_current_init_as_setup(setup_name: str) -> tuple[bool, str]:
    init_root = _init_path()
    chars_src = init_root / "characters"
    plot_src = init_root / "plot.json"

    if not chars_src.exists():
        return False, "init/characters not found"

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(setup_name or "").strip())
    if not safe_name:
        return False, "Setup name is required"

    setup_dir = _setups_path() / safe_name
    if setup_dir.exists():
        return False, f"Setup '{safe_name}' already exists"

    try:
        setup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(chars_src, setup_dir / "characters")
        if plot_src.exists():
            shutil.copy2(plot_src, setup_dir / "plot.json")
    except Exception as e:
        try:
            if setup_dir.exists():
                shutil.rmtree(setup_dir)
        except Exception:
            pass
        return False, f"Failed to save setup: {e}"

    return True, f"Saved setup '{safe_name}'"


def _restore_setup_to_init(setup_name: str) -> tuple[bool, str]:
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(setup_name or "").strip())
    if not safe_name:
        return False, "Setup name is required"

    setup_dir = _setups_path() / safe_name
    chars_src = setup_dir / "characters"
    plot_src = setup_dir / "plot.json"

    if not setup_dir.exists() or not chars_src.exists():
        return False, f"Setup '{safe_name}' not found or incomplete"

    init_root = _init_path()
    chars_dst = init_root / "characters"
    plot_dst = init_root / "plot.json"

    try:
        init_root.mkdir(parents=True, exist_ok=True)

        if chars_dst.exists():
            shutil.rmtree(chars_dst)
        shutil.copytree(chars_src, chars_dst)

        if plot_src.exists():
            shutil.copy2(plot_src, plot_dst)
        else:
            if plot_dst.exists():
                plot_dst.unlink()
    except Exception as e:
        return False, f"Failed to load setup: {e}"

    return True, (
        f"Loaded setup '{safe_name}' into init/. "
        "Use Restart Game to apply it to the active game state."
    )


@app.get("/partials/config_setups", response_class=HTMLResponse)
def partial_config_setups(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/config_setups.html",
        {"setups": _list_setups(), "message": "", "success": True},
    )


@app.post("/api/config/setup", response_class=HTMLResponse)
def api_create_setup(request: Request, name: str = Form("")):
    ok, msg = _save_current_init_as_setup(name)
    return templates.TemplateResponse(
        request,
        "partials/config_setups.html",
        {"setups": _list_setups(), "message": msg, "success": bool(ok)},
    )


@app.post("/api/config/setup/{name}/load", response_class=HTMLResponse)
def api_load_setup(request: Request, name: str):
    ok, msg = _restore_setup_to_init(name)
    return templates.TemplateResponse(
        request,
        "partials/config_setups.html",
        {"setups": _list_setups(), "message": msg, "success": bool(ok)},
    )


@app.delete("/api/config/setup/{name}", response_class=HTMLResponse)
def api_delete_setup(request: Request, name: str):
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name or "").strip())
    setup_dir = _setups_path() / safe_name
    if not setup_dir.exists():
        return templates.TemplateResponse(
            request,
            "partials/config_setups.html",
            {"setups": _list_setups(), "message": f"Setup '{safe_name}' not found", "success": False},
        )
    try:
        shutil.rmtree(setup_dir)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_setups.html",
            {"setups": _list_setups(), "message": f"Failed to delete setup: {e}", "success": False},
        )

    return templates.TemplateResponse(
        request,
        "partials/config_setups.html",
        {"setups": _list_setups(), "message": f"Deleted setup '{safe_name}'", "success": True},
    )


# -----------------------------------------------------------------------------
# Config API: Backups
# -----------------------------------------------------------------------------

def _storyline_panel_context(*, message: str = "", success: bool = True) -> dict:
    data = list_story_lines(REPO_ROOT)
    return {
        "lines": data.get("lines") or [],
        "current_line_id": str(data.get("current_line_id") or ""),
        "message": message,
        "success": bool(success),
    }


@app.get("/partials/config_backups", response_class=HTMLResponse)
def partial_config_backups(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/config_backups.html",
        _storyline_panel_context(),
    )


@app.post("/api/config/storyline", response_class=HTMLResponse)
def api_create_storyline(request: Request, name: str = Form("")):
    try:
        line = create_story_line(REPO_ROOT, name=(name or "").strip())
        msg = f"Created story line '{line.get('name')}'"
        return templates.TemplateResponse(
            request,
            "partials/config_backups.html",
            _storyline_panel_context(message=msg, success=True),
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "partials/config_backups.html",
            _storyline_panel_context(message=f"Failed to create story line: {e}", success=False),
        )


@app.post("/api/config/storyline/{line_id}/switch", response_class=HTMLResponse)
def api_switch_storyline(request: Request, line_id: str):
    ok, msg = switch_story_line(REPO_ROOT, line_id=(line_id or "").strip())
    return templates.TemplateResponse(
        request,
        "partials/config_backups.html",
        _storyline_panel_context(message=msg, success=ok),
    )


@app.delete("/api/config/storyline/{line_id}", response_class=HTMLResponse)
def api_delete_storyline(request: Request, line_id: str):
    ok, msg = delete_story_line(REPO_ROOT, line_id=(line_id or "").strip())
    return templates.TemplateResponse(
        request,
        "partials/config_backups.html",
        _storyline_panel_context(message=msg, success=ok),
    )


@app.post("/cmd/checkin_turn", response_class=HTMLResponse)
def cmd_checkin_turn(
    request: Request,
    start_time: str = Form(""),
    end_time: str = Form(""),
    location: str = Form(""),
):
    ok, msg = checkout_turn_by_key(
        REPO_ROOT,
        start_time=(start_time or "").strip(),
        end_time=(end_time or "").strip(),
        location=(location or "").strip(),
        drop_after=True,
    )
    if ok:
        # After restoring a snapshot, reset all character "acted" flags so
        # the system waits for fresh player intents instead of immediately
        # finalizing the turn with the old last_decision.
        try:
            import json as _json
            scene_path = Path(REPO_ROOT) / "game" / "scene.json"
            if scene_path.exists():
                scene_data = _json.loads(scene_path.read_text(encoding="utf-8"))
                chars = scene_data.get("characters") or {}
                changed = False
                for name, entry in chars.items():
                    if isinstance(entry, dict) and entry.get("acted") is True:
                        entry["acted"] = False
                        entry["last_decision"] = ""
                        entry["last_thoughts"] = ""
                        entry["output_source"] = ""
                        changed = True
                if changed:
                    scene_data["characters"] = chars
                    scene_path.write_text(_json.dumps(scene_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        _reload_world_state_reader()
    return _render_composer(
        request=request,
        selected_character=(overrides.armed_character() or ""),
        message=msg,
        gm_question="",
        gm_answer="",
    )
