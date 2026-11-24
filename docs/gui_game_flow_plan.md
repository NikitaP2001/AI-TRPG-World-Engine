# GUI Game Flow — Design Plan

## Overview

Цель: сделать из текущего GUI (меню + превью сцены) полноценный игровой интерфейс,
где можно нажать Continue и увидеть, как развивается сюжет.

---

## Phase 1 — Core Game Loop (MVP)

### 1. GameOrchestrator singleton

**Сейчас**: `_start_game()` копирует файлы, вызывает `World.ensure_initialized()` и переключает
`state = GameState.PLAYING`. Engine (GM/SM/WM) не инициализирован.

**Нужно**: Создать экземпляр `GameOrchestrator` (`console_app.GameOrchestrator`) после
`_start_game()` и хранить его как глобальный singleton в `main.py`.

```
# main.py
_orchestrator: Optional['GameOrchestrator'] = None

def _init_orchestrator() -> None:
    global _orchestrator
    from console_app import GameOrchestrator
    _orchestrator = GameOrchestrator()
    _orchestrator._load_session_state(show_history_stats=False)
```

Вызов после `_start_game()`:

```python
_game_started = True
state = GameState.PLAYING
_init_orchestrator()
```

### 2. Game View Layout

**Сейчас**: Только `panel_scene_view()` в центре + HUD сверху.

**Новый макет** (ширина ~1600px):

```
┌──────────────────────────────────────────────────────────────┐
│  HUD: Time: Y0001-03-15 12:00    [>> Continue]  [Menu] [Save] │
├──────────┬───────────────────────────────────────┬────────────┤
│          │                                       │            │
│  Story   │  Story Narration (turn cards)         │  Scene     │
│  Arcs    │                                       │  Panel     │
│          │  ┌─ Turn Card ─────────────────────┐  │            │
│  Arc 1   │  │ ⏱ Y0001-03-15 → Y0001-03-16    │  │ Location   │
│  ├─ Para1│  │ 📍 Castle Evermore              │  │ Characters │
│  ├─ Para2│  │                                  │  │  Elena HP  │
│  │       │  │ Scene: The morning sun...        │  │  Marek FP  │
│  Arc 2   │  │ Outcome: Elena confronts...      │  │            │
│  (active)│  │                                  │  │ NPCs       │
│          │  │ Actions:                         │  │  Guard #1  │
│          │  │  Elena: "I demand answers!"      │  │            │
│          │  │  Marek: heals the wounded...     │  │            │
│          │  └──────────────────────────────────┘  │            │
│          │                                       │            │
│          │  ┌─ Turn Card ─────────────────────┐  │            │
│          │  │ ...                             │  │            │
│          │  └──────────────────────────────────┘  │            │
│          │                                       │            │
├──────────┴───────────────────────────────────────┴────────────┤
│  Composer: [Character: (none) ▼]  [■ Stop on scene]  [Continue] │
│           Thoughts: ______________________________              │
│           Intent: _______________________________               │
└──────────────────────────────────────────────────────────────┘
```

**Разделение на 3 колонки**:

| Область | Ширина | Содержание |
|---------|--------|------------|
| Left sidebar | 200px | Story arc tree (арки → параграфы) |
| Center | flex | Story narration (turn cards) + composer внизу |
| Right panel | 240px | Scene info (локация, персонажи, NPC) |

### 3. Story Narration (Turn Cards)

**Формат одного turn card** — похож на старый `turn_card.html`:

```
┌─────────────────────────────────────────────┐
│  ⏱ Y0001-03-15 12:00 → Y0001-03-15 14:30   │
│  📍 Castle Evermore                         │
├─────────────────────────────────────────────┤
│  Scene:                                     │
│  The morning sun casts long shadows across  │
│  the castle courtyard...                     │
│                                             │
│  Outcome:                                   │
│  Elena steps forward, her hand on her       │
│  sword. "I demand answers," she says.       │
│                                             │
│  Actions:                                   │
│  ◆ Elena — "I demand answers!"              │
│    💭 She's ready for a fight               │
│  ◆ Marek — Heals the wounded guardsman      │
│    💭 This situation needs a healer         │
└─────────────────────────────────────────────┘
```

**Источник данных**: `story.json` → arcs → paragraphs → turns.
Каждый turn содержит: `start_time`, `end_time`, `location`, `scene_description`,
`outcome`/`raw_narration`, `characters`, `npcs`, `actions` (intent + thoughts).

**Хранение в памяти**: Кешировать последние N turnов, обновлять после каждого Continue.

### 4. Continue Button + Turn Execution

**Где**: В HUD (рядом с временем) и в Composer (внизу).

**Логика нажатия**:

```python
def _cmd_continue() -> None:
    global _turn_in_progress
    if _turn_in_progress or _orchestrator is None:
        return
    _turn_in_progress = True
    
    # Запуск хода (синхронно, с индикацией)
    try:
        from turn_runner import run_turn
        from langchain_core.messages import HumanMessage
        run_turn(_orchestrator, user_msg=None)
    finally:
        _turn_in_progress = False
        _refresh_story_display()
```

**Индикация прогресса**: Пока `_turn_in_progress == True`:
- Кнопка Continue greyed out, текст "In progress..."
- Анимированный спиннер или "..."
- После завершения: обновить turn cards

Для асинхронности: можно запускать через threading, но проще сначала синхронно
с индикацией (LLM вызовы всё равно блокирующие).

### 5. Story Arc Tree (Left Sidebar)

**Формат**:
- Список арок (из `story.json`)
- Под каждой аркой — параграфы
- Текущий активный параграф подсвечен
- Клик на параграф → загрузить его turnы

Данные: `B.get_story_tree()` уже возвращает эту структуру.

### 6. Composer (нижняя панель)

Как в старом webui:
- **Character override**: выпадающий список персонажей
- **Stop on scene**: чекбокс авто-паузы на смене сцены
- **Continue** кнопка
- Если override активен: показать поля Thoughts + Intent + Ask GM

### 7. Progress/Streaming

Пока ход выполняется:
- Полоска/спиннер "Turn in progress..."
- Stream watchdog читает `logs/stream.txt` и показывает live-текст
- После завершения: обновить turn cards и скрыть спиннер

---

## Phase 2 — Character Override

### Override Flow

1. User выбирает персонажа в Composer
2. Нажимает Continue
3. Engine запускает ход, но когда доходит до этого персонажа — пауза
4. GUI показывает форму: Thoughts + Intent (textarea)
5. User пишет решение персонажа и нажимает Submit
6. Engine продолжает ход с этим решением
7. Результат показывается в turn card

**Данные**: `overrides.arm(character_name)`, `overrides.get_pending_prompt()`,
`cmd_continue_with_decision(character_name, intent, thoughts)`.

### Ask GM

- Кнопка "Ask GM" в форме override
- Вызывает GM с вопросом от персонажа
- Ответ показывается в той же форме

---

## Phase 3 — Continue Until Paragraph

- Кнопка "Continue until paragraph"
- Автоматически вызывает Continue, пока не появится новый параграф
- Максимум 50 итераций (safety limit)
- Показывать прогресс: "Turn 3/50..."

---

## Phase 4 — Storyline Management

- Backup текущей storyline
- Switch между storyline
- Delete storyline
- UI: список лайнов в левой панели или в Settings

---

## Изменения в файлах

### 1. `opengl_app/main.py`

**Новые глобальные переменные**:
```python
_orchestrator: Optional['GameOrchestrator'] = None
_turn_in_progress: bool = False
_turn_cache: list = []  # последние turn-карты
_story_arcs: list = []
_composer_char: str = ""  # выбранный персонаж для override
_composer_thoughts: str = ""
_composer_intent: str = ""
_composer_stop_scene: bool = True
```

**Новые функции**:
- `_init_orchestrator()` — создаёт GameOrchestrator
- `_cmd_continue()` — запускает один ход
- `_cmd_continue_until_paragraph()` — авто-прогон
- `_render_story_view(display_w, display_h)` — центральная панель с turn cards
- `_render_composer(display_w, display_h)` — нижняя панель управления
- `_render_story_sidebar(display_w, display_h)` — левая панель с арками
- `_render_turn_card(turn_data)` — один turn card
- `_refresh_story_display()` — перечитывает story.json

**Изменённые функции**:
- `_start_game()` — добавить вызов `_init_orchestrator()`
- `_render_game_view()` — новый layout с 3 колонками

### 2. `opengl_app/gui_bridge.py`

**Новые методы**:
```python
@staticmethod
def get_turns(arc_index: int = -1, para_name: str = "", limit: int = 20) -> list:
    """Read turns from story.json."""
    
@staticmethod
def continue_turn(user_msg: str = "") -> bool:
    """Run turn_runner.run_turn on the current orchestrator."""
    
@staticmethod
def get_orchestrator():
    """Return the global orchestrator."""
```

### 3. `opengl_app/gui_panels.py`

- `panel_scene_view()` — оставить как debug-панель (правая колонка)
- Добавить: `panel_story_arcs()` — дерево арок (левая колонка)

---

## Примерный внешний вид (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ⏰ Y0001-03-15 12:00              [▶ Continue]  [⏸ Menu]  [💾 Save]   │
├────────────┬────────────────────────────────────────────┬────────────────┤
│ Story      │  Turn Cards                               │ Scene          │
│            │                                            │                │
│ ▶ Arc 1    │  ┌──────────────────────────────────────┐ │ Location:      │
│   Ongoing  │  │ ⏱ Y0001-03-15 → Y0001-03-16         │ │ Castle Evermore│
│            │  │ 📍 Castle Evermore                    │ │                │
│            │  │                                      │ │ Characters:    │
│            │  │ The morning sun casts shadows...      │ │ Elena ████ 12 │
│            │  │                                      │ │ Marek ████ 10 │
│            │  │ Elena: "I demand answers!"            │ │                │
│            │  └──────────────────────────────────────┘ │ NPCs:          │
│            │                                            │ Guard #1       │
│            │  ┌──────────────────────────────────────┐ │ Guard #2       │
│            │  │ ⏱ Y0001-03-16 → Y0001-03-17         │ │                │
│            │  │ 📍 Throne Room                       │ │                │
│            │  │ ...                                  │ │                │
│            │  └──────────────────────────────────────┘ │                │
│            │                                            │                │
├────────────┴────────────────────────────────────────────┴────────────────┤
│  Character: [Elena Brightwood ▼]  ☐ Stop on scene     [▶ Continue]       │
│  Thoughts: ________________________________________________________      │
│  Intent:   ________________________________________________________      │
└──────────────────────────────────────────────────────────────────────────┘
```
