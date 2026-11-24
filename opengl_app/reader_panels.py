"""The reader — what happened, session by session, and the hand on the game.

The screen is built to be read: the sessions down the left, the session itself
across the middle as prose and nothing else, and the working parts — what the
session was made of, what a running session is doing, the points the campaign
passed through, one character's own hand — in panels that stand out of the way
until they are asked for.

Nothing here writes the game's own records: playing a session is the game's own
command run as a child process, and the panels that touch the world do it
through the reader's data layer, which writes exactly what the engine writes.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import imgui

from opengl_app import session_reader as R
from opengl_app.session_runner import SessionRun

# ── what the reader is looking at ──
_sessions: List[Dict[str, Any]] = []
_chapters: List[Dict[str, Any]] = []
_selected: int = -1            # the number of the session on screen
_filter: str = ""
_status: str = ""
_status_until: float = 0.0
_show_list: bool = True
_show_details: bool = True
_show_run: bool = True
_show_player: bool = False
_show_points: bool = False
_reader_font = None

_run: Optional[SessionRun] = None
_ask: Optional["_Worker"] = None
_answer: str = ""

#: What a session was made of, by its number — read once, kept until the game is
#: read again. It is the reader's most expensive answer and the least changing.
_details: Dict[int, Dict[str, Any]] = {}

# ── one character's own turn, filled in by hand ──
_player: str = ""
_question: str = ""
_intent_type: str = "speech"
_intent_target: str = ""
_intent_text: str = ""
_intent_start: str = "0s"
_intent_span: str = "60s"
_intent_visibility: str = "public"
_intent_absorption: str = "alert"
_intent_trigger: str = ""

_TYPE_NAMES = {"speech": "речь", "movement": "движение",
               "combat": "бой", "cosmetic": "жест"}
_VIS_NAMES = {"public": "открыто", "private": "скрыто"}
_ABS_NAMES = {"alert": "начеку", "immersed": "погружён"}


def set_reader_font(font) -> None:
    """The font the sessions are read in, loaded by the application."""
    global _reader_font
    _reader_font = font


def open_reader() -> None:
    """Come into the reader: read the game as it stands now."""
    refresh(keep=False)


def _say(text: str, seconds: float = 6.0) -> None:
    global _status, _status_until
    _status = text
    _status_until = time.time() + seconds


# ── reading the game ─────────────────────────────────────────────────────

def refresh(keep: bool = True) -> None:
    """Read the sessions and the chapters again."""
    global _sessions, _chapters, _selected
    _sessions = R.sessions()
    _chapters = R.chapters()
    _details.clear()
    if not keep or _selected < 0:
        _selected = _sessions[-1]["number"] if _sessions else -1
    if _selected not in [s["number"] for s in _sessions]:
        _selected = _sessions[-1]["number"] if _sessions else -1
    from opengl_app.gui_bridge import GuiBridge

    GuiBridge.forget_world()


def _details_of(number: int) -> Dict[str, Any]:
    """What one session was made of, read once and kept until the game is re-read.

    Reading it is reading the scene manager's whole memory and the event log —
    tens of megabytes in a world that has lived — and doing that while the window
    draws is what made the reader hang: every frame paid for the same answer.
    A session that has closed does not change, so the answer stands until a
    session is played or the game is re-read.
    """
    if number not in _details:
        _details[number] = R.session_details(number, _sessions)
    return _details[number]


def _session(number: int) -> Optional[Dict[str, Any]]:
    for item in _sessions:
        if item["number"] == number:
            return item
    return None


# ── playing a session ────────────────────────────────────────────────────

def start_run(argv: List[str], title: str) -> None:
    """Play the game's next session — or go back to a point — as a child process."""
    global _run
    if _run is not None and _run.running:
        _say("Сессия уже идёт.")
        return
    _run = SessionRun(argv, title=title)
    _run.start()
    _say(title + " — пошло.")


def play_next() -> None:
    start_run([str(R.WORKSPACE / "run_session.py"), "--next"], "Следующая сессия")


def take_point() -> None:
    start_run([str(R.WORKSPACE / "run_session.py"), "--take"], "Точка")


def go_back_to(number: int) -> None:
    start_run([str(R.WORKSPACE / "run_session.py"), "--revert", str(number)],
              f"Возврат к точке {number}")


def _poll_run() -> None:
    """Take what the running session has said, and read the game when it ends."""
    if _run is None:
        return
    was_running = _run.running
    _run.poll()
    if was_running and not _run.running:
        refresh(keep=False)
        if _run.state == "done":
            _say("Сессия закрыта — читаю заново.")
        elif _run.reverted_to:
            _say(f"Сессия остановлена, игра возвращена к точке {_run.reverted_to}.")
        else:
            _say("Сессия остановлена, смотри подробности.")


# ── one call in the background, so the window keeps drawing ──────────────

class _Worker:
    """One thing asked of the engine, done off the drawing thread."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        import threading

        self._done = False
        self.result: Any = None
        self.error: str = ""
        self.started = time.time()

        def _work() -> None:
            try:
                self.result = fn()
            except Exception as error:  # noqa: BLE001 — shown, not swallowed
                self.error = f"{type(error).__name__}: {error}"
            finally:
                self._done = True

        self._thread = threading.Thread(target=_work, daemon=True)
        self._thread.start()

    @property
    def done(self) -> bool:
        return self._done

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


def ask_the_sm(who: str, question: str) -> None:
    """Ask the scene manager, as this character would ask it."""
    global _ask, _answer
    if _run is not None and _run.running:
        _say("Сессия идёт: спрашивать можно между сессиями.")
        return
    _answer = ""
    _ask = _Worker(lambda: _ask_scene_manager(who, question))


def _ask_scene_manager(player_name: str, question: str) -> str:
    """Ask, and keep the question and its answer in the scene manager's memory.

    The question is put the way a turn puts it — addressed, so one answer comes
    back for it and not for another — and the scene manager answers with its own
    long memory in front of it and nothing of a session, which is what a question
    between sessions is. The record the exchange leaves is the one the scene
    manager's own answering writes, entered through the same interface and merged
    into the file on the same terms; what the answer states about what the
    character holds is handed to him where the game hands it.
    """
    import json

    from history_pipeline import memory_at

    from opengl_app.gui_bridge import GuiBridge

    app = GuiBridge.orchestrator()
    history = memory_at(R.GAME / "scene_manager_messages.json")
    history.open_branch()
    try:
        raw = str(app._sm.resolve_player_question(
            player_name=player_name, questions=f"[observe:1] {question}") or "")
        app.deliver_awareness()
    finally:
        history.close_branch()
    try:
        answers = json.loads(raw)
    except Exception:
        answers = {"1": raw}
    lines = [str(v).strip() for v in answers.values() if str(v).strip()]
    return "\n\n".join(lines) if lines else "Мастер сцены не ответил."


# ── where the columns stand ──────────────────────────────────────────────

#: How much of the window width each side column takes. They start at a share
#: and then follow whatever the reader drags them to.
_list_frac: float = 0.20
_side_frac: float = 0.31
#: The width of the window being laid out, and of the columns as drawn, in pixels.
_work_w: float = 1.0
_list_w: float = 0.0
_side_w: float = 0.0
#: What is left for reading, which the other two give way to.
_MIN_READING: float = 240.0
#: The size of the window each column was last fitted to.
_fitted: Dict[str, Tuple[float, float]] = {}
#: How narrow and how wide each column may be dragged.
_bounds: Dict[str, Tuple[float, float]] = {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value)) if high > low else low


def _top() -> float:
    """Where the screens begin: under the menu bar, at whatever height it is."""
    bar = float(imgui.get_frame_height())
    try:
        bar = max(bar, float(imgui.get_main_viewport().work_pos[1]))
    except Exception:
        pass
    return bar


def _fit(name: str, x: float, y: float, w: float, h: float) -> int:
    """Put a column where the window says, and let it be resized by hand.

    A column may be dragged, but only within the limits the layout gives it: the
    bounds are handed to the window itself, so it cannot be pulled over its
    neighbour or past the edge of the screen. What it is dragged to is kept — the
    columns after it are placed where it now ends — and the size is set again
    only when the window itself changed size, so a resize does not leave a gap.

    Returns the flags the window needs on top of its own.
    """
    key = (round(w, 1), round(h, 1))
    first = _fitted.get(name) != key
    _fitted[name] = key
    imgui.set_next_window_position(x, y)
    imgui.set_next_window_size(w, h, imgui.ALWAYS if first else imgui.ONCE)
    limits = _bounds.get(name)
    if limits is None:
        return imgui.WINDOW_NO_RESIZE
    low, high = limits
    # Only the width is dragged: a column's height belongs to the layout.
    imgui.set_next_window_size_constraints((low, h), (max(low, high), h))
    return 0


def _keep_inside(name: str) -> None:
    """Hold a column to the size it is allowed to have."""
    limits = _bounds.get(name)
    if limits is None:
        return
    low, high = limits
    width, height = imgui.get_window_size()
    held = _clamp(width, low, max(low, high))
    if abs(held - width) > 0.5:
        imgui.set_window_size(held, height)


def _footer_height() -> float:
    """The room a row of buttons at the bottom of a panel needs."""
    style = imgui.get_style()
    return imgui.get_frame_height() + style.item_spacing[1] * 2.0


# ── the screen ───────────────────────────────────────────────────────────

def render(display_w: int, display_h: int) -> None:
    """The reader: the session list, the session, and the panels asked for."""
    global _list_frac, _side_frac, _work_w
    _poll_run()
    _menu_bar(display_w)

    gap = imgui.get_style().item_spacing[0]
    top = _top()
    total_h = max(240.0, float(display_h) - top)
    total_w = max(240.0, float(display_w))
    _work_w = total_w

    sides = (1 if _show_list else 0) + (1 if (_show_details or _show_run) else 0)
    list_w = _clamp(_list_frac * total_w, 220.0, total_w * 0.42) if _show_list else 0.0
    side_w = _clamp(_side_frac * total_w, 280.0, total_w * 0.52) \
        if (_show_details or _show_run) else 0.0

    # The reading column is the reason the screen exists, so the two side columns
    # give way to it when the window is too narrow to hold all three at the size
    # they ask for. What they give up stays given up as a share, so widening the
    # window widens them again in the same proportion.
    budget = max(120.0, total_w - _MIN_READING - gap * sides)
    wanted = list_w + side_w
    if wanted > budget and wanted > 0.0:
        share = budget / wanted
        list_w *= share
        side_w *= share

    x = 0.0
    asked_list = list_w
    # What the two side columns may be dragged to: neither can be pulled over the
    # reading column, and neither can be pulled onto its neighbour.
    _bounds["sessions"] = (180.0, max(180.0, total_w - _MIN_READING - side_w - gap * 2.0))
    side_limit = (240.0, max(240.0, total_w - _MIN_READING - list_w - gap * 2.0))
    _bounds["run"] = side_limit
    _bounds["details"] = side_limit
    if _show_list:
        _window_list(x, top, list_w, total_h)
        if _list_w and total_w and abs(_list_w - asked_list) > 2.0:
            # It is not the width it was given: the reader dragged it.
            _list_frac = _clamp(_list_w / total_w, 0.10, 0.42)
        # The column's real width, which is whatever the reader left it at.
        x += (_list_w or list_w) + gap
    centre_w = max(_MIN_READING, total_w - x - (side_w + gap if side_w else 0.0))
    _window_session(x, top, centre_w, total_h)

    if side_w:
        side_x = total_w - side_w
        if _show_run and _show_details:
            run_h = _clamp(total_h * 0.46, 140.0, max(140.0, total_h - 160.0))
            _window_run(side_x, top, side_w, run_h)
            _window_details(side_x, top + run_h + gap, side_w, total_h - run_h - gap)
        elif _show_run:
            _window_run(side_x, top, side_w, total_h)
        else:
            _window_details(side_x, top, side_w, total_h)
        if _side_w and total_w and abs(_side_w - side_w) > 2.0:
            _side_frac = _clamp(_side_w / total_w, 0.15, 0.52)

    if _show_points:
        _window_points(display_w, display_h, top, total_h, total_w)
    if _show_player:
        _window_player(display_w, display_h, top, total_h, total_w)
    if _status and time.time() < _status_until:
        _window_status(display_w, top)


def _menu_bar(display_w: int) -> None:
    global _show_points, _show_list, _show_details, _show_run, _show_player
    if not imgui.begin_main_menu_bar():
        return
    if imgui.begin_menu("Сессия"):
        if imgui.menu_item("Следующая сессия")[0]:
            play_next()
        if _run is not None and _run.state == "running":
            if imgui.menu_item("Остановить и вернуть игру")[0]:
                _run.stop()
        if imgui.menu_item("Точки сохранения")[0]:
            _show_points = True
        imgui.separator()
        if imgui.menu_item("Прочитать заново")[0]:
            refresh(keep=True)
        if imgui.menu_item("Главное меню")[0]:
            _leave_to_menu()
        imgui.end_menu()
    if imgui.begin_menu("Вид"):
        _, _show_list = imgui.menu_item("Список сессий", None, _show_list)
        _, _show_details = imgui.menu_item("Что было в сессии", None, _show_details)
        _, _show_run = imgui.menu_item("Ход сессии", None, _show_run)
        _, _show_player = imgui.menu_item("Играть за персонажа", None, _show_player)
        imgui.end_menu()
    imgui.end_main_menu_bar()


def _leave_to_menu() -> None:
    import opengl_app.main as app

    app.state = app.GameState.MENU


def _window_status(display_w: int, top: float) -> None:
    padding = imgui.get_style().window_padding[0]
    width = min(float(display_w) - 40.0,
                imgui.calc_text_size(_status).x + padding * 2.0 + 12.0)
    imgui.set_next_window_position((float(display_w) - width) / 2.0, top + 6.0)
    imgui.set_next_window_size(width, 0, imgui.ALWAYS)
    imgui.begin("##reader_status", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE
                | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_SCROLLBAR
                | imgui.WINDOW_NO_NAV | imgui.WINDOW_NO_FOCUS_ON_APPEARING)
    imgui.text_colored(_status, 0.86, 0.75, 0.45, 1.0)
    imgui.end()


# ── the list of sessions ─────────────────────────────────────────────────

def _window_list(x: float, y: float, w: float, h: float) -> None:
    global _list_w
    flags = _fit("sessions", x, y, w, h)
    imgui.begin("Сессии", None, imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | flags)
    _keep_inside("sessions")
    _list_w = imgui.get_window_size()[0]

    # The field says what it looks for, and says it inside itself: an empty box
    # above a list is a question, not a tool.
    imgui.set_next_item_width(imgui.get_content_region_available_width())
    changed, value = imgui.input_text_with_hint("##find", "поиск по сессиям", _filter, 128)
    if changed:
        globals()["_filter"] = value

    # The list takes the room that is left; the way to the next session stands
    # under it, across the column, and never below the window's edge.
    imgui.begin_child("##sessions", 0, -_footer_height(), False)
    if not _sessions:
        imgui.text_colored("Здесь ещё нет закрытых сессий.", 0.6, 0.6, 0.6, 1.0)
    by_number = {s["number"]: s for s in _sessions}
    shown = 0
    for number in _numbers_newest_first():
        if not _matches(by_number[number]):
            continue
        shown += 1
        _session_row(by_number[number])
    if not shown and _sessions:
        imgui.text_colored("Ничего не найдено.", 0.6, 0.6, 0.6, 1.0)
    imgui.end_child()

    # The one thing this column is for, given the weight of the one thing it is
    # for: the whole width, in the colour the interface uses for "this".
    imgui.push_style_color(imgui.COLOR_BUTTON, 0.16, 0.38, 0.78, 1.0)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.22, 0.47, 0.92, 1.0)
    imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, 0.30, 0.57, 1.00, 1.0)
    if imgui.button("Следующая сессия", imgui.get_content_region_available_width()):
        play_next()
    imgui.pop_style_color(3)
    imgui.end()


def _numbers_newest_first() -> List[int]:
    """The sessions as a reader flips through them: the last one on top."""
    return [s["number"] for s in reversed(_sessions)]


def _matches(item: Dict[str, Any]) -> bool:
    needle = _filter.strip().lower()
    if not needle:
        return True
    return needle in item["text"].lower() or needle in item["world_time"].lower()


def _clock_of(world_time: str) -> str:
    """The hour a session closed at — what a list of sessions is scanned for."""
    parts = str(world_time or "").split()
    return parts[-1][:5] if len(parts) > 1 else ""


def _session_row(item: Dict[str, Any]) -> None:
    selected = item["number"] == _selected
    # The hour stands beside the number: a second line for it is a line spent.
    label = f"Сессия {item['number']}    {_clock_of(item['world_time'])}##s{item['number']}"
    if imgui.selectable(label, selected, imgui.SELECTABLE_SPAN_ALL_COLUMNS)[0]:
        globals()["_selected"] = item["number"]
    opening = item["opening"]
    if opening:
        imgui.push_style_color(imgui.COLOR_TEXT, 0.58, 0.6, 0.64, 1.0)
        imgui.text_wrapped(_cut(opening, 110))
        imgui.pop_style_color()
    for chapter in _chapters:
        if item["number"] == chapter["sessions"][0]:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.55, 0.45, 0.3, 1.0)
            imgui.text_wrapped(("▸ " if chapter["kind"] == "paragraph" else "◆ ")
                               + chapter["name"])
            imgui.pop_style_color()


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ── the session itself ───────────────────────────────────────────────────

def _window_session(x: float, y: float, w: float, h: float) -> None:
    flags = _fit("session", x, y, w, h)
    item = _session(_selected) if _selected >= 0 else None
    # The moment the session closed at belongs to the session's own name: a line
    # of its own for it would be a line of the prose lost.
    title = f"Сессия {item['number']} · {item['world_time']}" if item else "Чтение"
    imgui.begin(title + "##reader", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | flags)
    if item is None:
        imgui.text_colored("Выбери сессию слева.", 0.6, 0.6, 0.6, 1.0)
        imgui.end()
        return

    arrow = imgui.get_frame_height()
    if imgui.button("<", arrow, arrow):
        _step(-1)
    imgui.same_line()
    if imgui.button(">", arrow, arrow):
        _step(1)
    imgui.same_line()
    if imgui.button("Что было в сессии"):
        globals()["_show_details"] = not _show_details
    imgui.separator()

    # The prose takes whatever the window has left, and rolls on its own.
    imgui.push_text_wrap_pos(0.0)
    imgui.begin_child("##prose", 0, 0, False)
    if _reader_font is not None:
        imgui.push_font(_reader_font)
    for para in item["paragraphs"]:
        imgui.text_wrapped(para)
        imgui.spacing()
        imgui.spacing()
    if _reader_font is not None:
        imgui.pop_font()
    imgui.end_child()
    imgui.pop_text_wrap_pos()
    imgui.end()


def _step(direction: int) -> None:
    numbers = [s["number"] for s in _sessions]
    if _selected not in numbers:
        return
    index = numbers.index(_selected) + direction
    if 0 <= index < len(numbers):
        globals()["_selected"] = numbers[index]


# ── what one session was made of ─────────────────────────────────────────

def _window_details(x: float, y: float, w: float, h: float) -> None:
    global _side_w
    flags = _fit("details", x, y, w, h)
    imgui.begin("Что было в сессии", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | flags)
    _keep_inside("details")
    _side_w = imgui.get_window_size()[0]
    if _selected < 0:
        imgui.text_colored("Нечего показывать.", 0.6, 0.6, 0.6, 1.0)
        imgui.end()
        return
    details = _details_of(_selected)
    imgui.begin_child("##details", 0, 0, False)
    imgui.text_colored("Заявки, которые взяли", 0.75, 0.68, 0.5, 1.0)
    if not details["accepted"]:
        imgui.text_colored("ничего не разбирали", 0.55, 0.55, 0.55, 1.0)
    for deed in details["accepted"]:
        imgui.text_wrapped(deed["act"] or "—")
        if deed["text"]:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.72, 0.73, 0.76, 1.0)
            imgui.text_wrapped("   " + _cut(deed["text"], 400))
            imgui.pop_style_color()
        if deed["seen_by"]:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.5, 0.55, 0.6, 1.0)
            imgui.text_wrapped("   видели: " + ", ".join(deed["seen_by"]))
            imgui.pop_style_color()
        imgui.spacing()
    imgui.separator()
    imgui.text_colored("Что произошло", 0.75, 0.68, 0.5, 1.0)
    if not details["events"]:
        imgui.text_colored("ничего не записано", 0.55, 0.55, 0.55, 1.0)
    for event in details["events"]:
        imgui.push_style_color(imgui.COLOR_TEXT, 0.5, 0.55, 0.6, 1.0)
        imgui.text(event["moment"])
        imgui.pop_style_color()
        imgui.text_wrapped(_cut(event["description"], 600))
        if event["entities"]:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.5, 0.55, 0.6, 1.0)
            imgui.text_wrapped("   " + ", ".join(event["entities"]))
            imgui.pop_style_color()
        imgui.spacing()
    imgui.end_child()
    imgui.end()


# ── what a running session is doing ──────────────────────────────────────

def _window_run(x: float, y: float, w: float, h: float) -> None:
    global _side_w
    flags = _fit("run", x, y, w, h)
    imgui.begin("Ход сессии", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | flags)
    _keep_inside("run")
    _side_w = imgui.get_window_size()[0]
    run = _run
    if run is None:
        imgui.text_colored("Сессия не идёт. Запусти её из меню «Сессия».",
                           0.6, 0.6, 0.6, 1.0)
        imgui.separator()
        imgui.text_colored("Мир на " + (R.world_time() or "?"), 0.55, 0.58, 0.62, 1.0)
        imgui.text_colored(f"Сессий прожито: {R.session_count()}", 0.55, 0.58, 0.62, 1.0)
        imgui.end()
        return

    imgui.text_colored(run.title, 0.86, 0.75, 0.45, 1.0)
    imgui.same_line()
    imgui.text_colored(f"идёт {_clock(run.elapsed)}", 0.6, 0.62, 0.66, 1.0)
    if run.state == "running" and imgui.button("Остановить и вернуть игру"):
        # A session stopped halfway is not a state to stand on: stopping is the
        # child's end and the point's return, in that order.
        run.stop()
    elif run.state == "stopping":
        imgui.text_colored("Останавливаю…", 0.86, 0.75, 0.45, 1.0)
    imgui.separator()
    imgui.text_wrapped("Фаза: " + run.phase)
    if run.who:
        imgui.text_wrapped("Сейчас работает: " + run.who)
    if run.cycle:
        held, limit = run.buffer
        imgui.text(f"Цикл {run.cycle}")
        imgui.same_line()
        imgui.push_style_color(imgui.COLOR_TEXT, 0.6, 0.62, 0.66, 1.0)
        imgui.text(f"буфер сессии {held} из {limit}")
        imgui.pop_style_color()
        imgui.progress_bar(run.buffer_share(), (0.0, 12.0), "")
    imgui.separator()
    if run.acted:
        imgui.text_colored("Кто как отработал", 0.75, 0.68, 0.5, 1.0)
        for name, state in run.acted.items():
            imgui.text_wrapped(f"{name} — {state}")
        imgui.separator()
    if run.tools:
        imgui.text_colored("Вызванные инструменты", 0.75, 0.68, 0.5, 1.0)
        imgui.push_style_color(imgui.COLOR_TEXT, 0.68, 0.7, 0.74, 1.0)
        for call in run.tools[-14:]:
            imgui.text_wrapped("· " + call)
        imgui.pop_style_color()
        imgui.separator()
    imgui.text_colored("Что говорит сессия", 0.75, 0.68, 0.5, 1.0)
    # What the session said at length is a door under the log, so the log takes
    # the room that is left above it rather than being cut off by it.
    reserve = _footer_height() if run.other else 0.0
    imgui.begin_child("##runlog", 0, -reserve, False)
    for phrase in reversed(run.log[-60:]):
        imgui.text_wrapped(phrase)
    if run.trouble:
        imgui.separator()
        imgui.push_style_color(imgui.COLOR_TEXT, 0.85, 0.45, 0.4, 1.0)
        for line in run.trouble[-20:]:
            imgui.text_wrapped(line)
        imgui.pop_style_color()
    imgui.end_child()
    if run.other:
        if imgui.collapsing_header("Что сессия сказала так, как сказала")[0]:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.55, 0.57, 0.6, 1.0)
            for line in reversed(run.other[-40:]):
                imgui.text_wrapped(line)
            imgui.pop_style_color()
    imgui.end()


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


# ── the points the campaign passed through ───────────────────────────────

def _window_points(display_w: int, display_h: int, top: float, total_h: float,
                   total_w: float) -> None:
    w = _clamp(total_w * 0.55, 400.0, 780.0)
    h = max(240.0, total_h - 60.0)
    _fit("points", (total_w - w) / 2.0, top + 24.0, w, h)
    imgui.begin("Точки", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)
    imgui.text_colored("Состояние игры, к которому можно вернуться. Возврат "
                       "сохраняет то, что стоит на диске сейчас.", 0.6, 0.62, 0.66, 1.0)
    imgui.separator()
    imgui.begin_child("##points", 0, -_footer_height(), False)
    here = R.line_at()
    for point in reversed(R.points()):
        note = point["label"]
        if here is not None and point["point"] == here:
            note = "линия кончается здесь" + (" — " + note if note else "")
        imgui.text(f"Точка {point['point']}   {point['world_time']}"
                   + (f"   сессий: {point['session_count']}" if point["session_count"] else ""))
        if note:
            imgui.push_style_color(imgui.COLOR_TEXT, 0.6, 0.62, 0.66, 1.0)
            imgui.text_wrapped(note)
            imgui.pop_style_color()
        if imgui.button(f"Вернуться##p{point['point']}"):
            go_back_to(point["point"])
        imgui.spacing()
    imgui.end_child()
    if imgui.button("Закрыть"):
        globals()["_show_points"] = False
    imgui.same_line()
    if imgui.button("Запомнить точку"):
        take_point()
        globals()["_show_points"] = False
    imgui.end()


# ── one character's own hand ─────────────────────────────────────────────

def _label_room(label: str) -> float:
    """How much of a line a field's label takes, in pixels."""
    style = imgui.get_style()
    return imgui.calc_text_size(label).x + style.item_inner_spacing[0]


def _field(label: str, value: str, length: int) -> str:
    """One text field, as wide as the room its label leaves for it."""
    room = imgui.get_content_region_available_width()
    imgui.set_next_item_width(max(90.0, room - _label_room(label)))
    changed, text = imgui.input_text(label, value, length)
    return text if changed else value


def _combo(label: str, options: Dict[str, str], key: str) -> str:
    """One chooser, read in the reader's words and sized like a field."""
    room = imgui.get_content_region_available_width()
    imgui.set_next_item_width(max(90.0, room - _label_room(label)))
    chosen = key
    if imgui.begin_combo(label, options.get(key, key)):
        for option_key, option_label in options.items():
            if imgui.selectable(option_label, option_key == key)[0]:
                chosen = option_key
        imgui.end_combo()
    return chosen


def _window_player(display_w: int, display_h: int, top: float, total_h: float,
                   total_w: float) -> None:
    global _ask, _answer
    w = _clamp(total_w * 0.42, 380.0, 720.0)
    h = max(260.0, total_h - 24.0)
    _fit("player", float(display_w) - w - 12.0, top + 6.0, w, h)
    imgui.begin("Играть за персонажа", None,
                imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_RESIZE)

    names = R.characters()
    if not names:
        imgui.text_colored("В этом мире нет персонажей.", 0.6, 0.6, 0.6, 1.0)
        imgui.end()
        return
    globals().setdefault("_player", names[0])
    current = _player if _player in names else names[0]
    room = imgui.get_content_region_available_width()
    imgui.set_next_item_width(max(120.0, room - _label_room("Персонаж")))
    if imgui.begin_combo("Персонаж", current):
        for name in names:
            if imgui.selectable(name, name == current)[0]:
                globals()["_player"] = name
        imgui.end_combo()
    who = _player if _player in names else names[0]

    situation = R.player_situation(who)
    imgui.push_style_color(imgui.COLOR_TEXT, 0.6, 0.62, 0.66, 1.0)
    imgui.text_wrapped(f"{situation['place'] or '—'} · {situation['world_time']}"
                       + (f" · {situation['current_state']}" if situation["current_state"] else ""))
    imgui.pop_style_color()

    # The whole panel rolls as one: a form that only half fits on the screen is
    # worse than one the reader scrolls through.
    imgui.begin_child("##player", 0, 0, False)

    imgui.text_colored("Спросить у мастера сцены", 0.75, 0.68, 0.5, 1.0)
    globals()["_question"] = _field("##question", _question, 400)
    if imgui.button("Спросить"):
        if _question.strip():
            ask_the_sm(who, _question.strip())
        else:
            _say("Сначала напиши вопрос.")
    if _ask is not None:
        imgui.same_line()
        if _ask.done:
            if _ask.error:
                imgui.text_wrapped("Мастер не ответил: " + _ask.error)
            else:
                globals()["_answer"] = str(_ask.result or "")
                _ask = None
        else:
            imgui.text_colored(f"мастер думает… {_clock(_ask.elapsed)}",
                               0.7, 0.65, 0.5, 1.0)
    if _answer:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped(_answer)
        imgui.pop_text_wrap_pos()
    imgui.separator()

    imgui.text_colored("Объявить намерение", 0.75, 0.68, 0.5, 1.0)
    globals()["_intent_type"] = _combo("Вид действия", _TYPE_NAMES, _intent_type)
    globals()["_intent_target"] = _field("На что направлено", _intent_target, 200)

    imgui.set_next_item_width(imgui.get_content_region_available_width())
    changed, value = imgui.input_text_multiline(
        "Что делает и говорит##intent", _intent_text, 4096, 0,
        imgui.get_text_line_height() * 5 + 10, 0)
    if changed:
        globals()["_intent_text"] = value

    globals()["_intent_start"] = _field("Когда начинается", _intent_start, 32)
    globals()["_intent_span"] = _field("Сколько длится", _intent_span, 32)
    globals()["_intent_visibility"] = _combo("Видно ли", _VIS_NAMES, _intent_visibility)
    globals()["_intent_absorption"] = _combo("Внимание", _ABS_NAMES, _intent_absorption)
    globals()["_intent_trigger"] = _field("Чего ждёт (готовность)", _intent_trigger, 400)

    if imgui.button("Объявить"):
        _declare(who)
    imgui.same_line()
    if imgui.button("Очистить"):
        globals()["_intent_text"] = ""

    standing = R.player_intents(who)
    imgui.separator()
    imgui.text_colored("Стоящие намерения", 0.75, 0.68, 0.5, 1.0)
    if not standing:
        imgui.text_colored("ничего не стоит", 0.55, 0.55, 0.55, 1.0)
    for intent in standing:
        imgui.text_wrapped(f"{_TYPE_NAMES.get(intent['action_type'], intent['action_type'])}"
                           f" → {intent['target'] or '—'}")
        imgui.push_style_color(imgui.COLOR_TEXT, 0.68, 0.7, 0.74, 1.0)
        imgui.text_wrapped(_cut(intent["description"], 300))
        imgui.pop_style_color()
        if imgui.button(f"Снять##w{intent['id']}"):
            if R.withdraw_intent(intent["id"]):
                _say("Намерение снято.")
        imgui.spacing()

    imgui.separator()
    imgui.text_colored("Что он помнит и знает", 0.75, 0.68, 0.5, 1.0)
    for record in reversed(R.player_memory(who)):
        imgui.push_style_color(imgui.COLOR_TEXT, 0.7, 0.72, 0.76, 1.0)
        imgui.text_wrapped(_cut(record["text"], 700))
        imgui.pop_style_color()
        imgui.spacing()
    imgui.end_child()
    imgui.end()


def _declare(who: str) -> None:
    if not _intent_text.strip():
        _say("Намерению нужно описание: что персонаж делает и говорит.")
        return
    written = R.declare_for(
        who, action_type=_intent_type, target=_intent_target,
        description=_intent_text, t_start=_intent_start, duration=_intent_span,
        visibility=_intent_visibility, absorption=_intent_absorption,
        trigger_condition=_intent_trigger,
    )
    globals()["_intent_text"] = ""
    _say(f"{who}: намерение принято ({written['id']}).")
