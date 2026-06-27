# GUI Requirements for LLM World v2

*What the user should see and be able to do — not how it's built.*

---

## §0 Why Not the Old Web GUI

The old webui (`webui_server.py` + `webui/app.py`) was built for the legacy system:
- Read directly from `game/world/*.json` (locations.json, npc.json, story.json)
- No concept of entity_store, composite backend, or agent lifecycle
- Character data from filesystem, NPC/location data from separate JSON files
- No visibility into events, effects, scripts, or agent state

It would need a complete rewrite to work with the new system — so we build fresh.

### 0.1 Why Not Another Web API

The old design assumed a browser-based GUI (FastAPI + Jinja2 + HTMX). This adds:
- HTTP request/response cycle for every action
- WebSocket or SSE for real-time updates
- JSON serialization between Python backend and JS frontend
- A separate frontend codebase (HTML, CSS, JS)

**None of this is needed.** The engine runs in Python. The GUI runs in the same process.
A web API is an unnecessary layer between the user and the engine.

### 0.2 Native Desktop Approach

We build the GUI directly into the existing `opengl_app/main.py` using:
- **GLFW** — window management, input (already set up)
- **ModernGL** — 3D rendering when needed (already set up)
- **Dear ImGui** (`imgui` + `imgui[glfw]` + `imgui[moderngl]`) — immediate mode GUI

Dear ImGui gives us:
- Zero serialization — GUI calls `get_entity()` directly, same process
- No HTTP, no WebSocket, no JSON round-trips
- Full 3D integration — entity editor panel next to 3D world map
- Minimal boilerplate — add a panel in 5 lines of code
- Perfect for tools, debug UIs, and game-like interfaces

The GUI is not a separate application — it's a **compilation of panels** rendered
into the same GLFW window as the 3D viewport.

---

## §1 Core Philosophy

### 1.1 Two Audiences

The GUI serves two distinct users:

| User | Goal | Perspective |
|---|---|---|
| **Player / GM** | Run the game, read narrative, make decisions | Narrative Engine (Level 1) |
| **World Builder / Debugger** | Inspect simulation state, monitor agents, edit entities | World Simulation (Level 2) |

The GUI must support **both** without forcing either into the wrong view.

### 1.2 Three Panels, One Screen

The main screen has three logical zones:

```
┌──────────────────────────────────────────────────────────────┐
│  LEFT SIDEBAR          │  MAIN                          │  RIGHT PANEL      │
│  (narrative tree)      │  (active content)              │  (entity browser) │
│                        │                                │                   │
│  Story arcs            │  Chat / Scene / Entity Detail  │  Entity list      │
│  Paragraphs            │                                │  Event log        │
│  Turns                 │                                │  Script monitor   │
│  Bookmarks             │                                │  Search           │
│                        │                                │                   │
└──────────────────────────────────────────────────────────────┘
```

All three panels are collapsible. On narrow screens, they stack vertically.

---

## §2 Narrative Views (Level 1 — Story Layer)

### 2.1 Story Timeline

A vertical timeline showing the narrative history:

```
Y0001-01-15 ─── Arc: The Defenders of Neverwinter ─── [expand ▾]
│
├─ Paragraph: A Warm Welcome         [3 turns]
│  ├─ Turn 1: Party enters tavern    [GM: "The Siren's Rest..."]
│  ├─ Turn 2: Elena intimidates      [Elena: "I glare at the barkeep"]
│  └─ Turn 3: Marek negotiates       [Marek: "We need information"]
│
├─ Paragraph: The Docks at Night      [2 turns]
│  └─ ...
│
└─ (ongoing) ─── Current Scene ───  [1 turn so far]
   └─ Turn 4: Fight at warehouse    [Tiko: "I sneak around back"]
```

Each item expandable. Clicking a turn shows the full narration in the main panel.

### 2.2 Active Scene View

When a scene is running, the main panel shows:

```
┌─────────────────────────────────────────────────────────┐
│  SCENE: The Siren's Rest                    [Y0001-01-15] │
│  Location: Neverwinter Docks ─── [view in entity panel]   │
│                                                           │
│  ┌─ Characters Present ──────────────────────────────┐   │
│  │  Elena (HP 14/14) ● Marek (HP 12/12)              │   │
│  │  Seraphina (FP 16/16) ● Tiko (HP 10/10)           │   │
│  └───────────────────────────────────────────────────┘   │
│                                                           │
│  ┌─ NPCs Present ───────────────────────────────────┐    │
│  │  Marta Haskel (barkeep)  ●  Captain Vane (patron)│    │
│  │  Father Matthias (priest, having a drink)        │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  ─── narration ───                                        │
│  The common room of the Siren's Rest is warm and         │
│  smoky. Marta polishes a tankard behind the bar...        │
│                                                           │
│  ─── recent events ───                                    │
│  📌 Captain Vane was seen arguing with a hooded figure   │
│  📌 A crate marked "Luskan" was delivered to the cellar  │
│                                                           │
│  [input: character action...]          [Send]             │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- HP/FP bars for each character (color: green > yellow > red)
- NPC list with current_state visible
- Recent events from event_log (narrative_flag=True)
- Character input box sends to SM → character agent loop
- "Advance turn" button for the orchestrator

### 2.3 Character Detail View

Clicking a character in the scene opens a detail overlay:

```
┌─ Elena Brightwood ─────────────────────────────────────┐
│  [Description] Human fighter, chipped longsword...      │
│                                                         │
│  HP  ████████░░░ 14/14  │  FP  ████████░░░ 12/12      │
│  Will ████████░░░ 12    │  ER  ░░░░░░░░░░░░ 0/0       │
│                                                         │
│  Conditions: none                                       │
│  Injuries: none                                         │
│                                                         │
│  [Skills]  Broadsword-14, Shield-13, Crossbow-12 ...   │
│  [Equipment]  Longsword, Leather cuirass, ...          │
│  [Inventory]  Backpack, bedroll, 12 gp, 8 sp ...       │
│                                                         │
│  [Memory] Last 5 decisions + thoughts                   │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Read from entity_store via CharacterFileBackend (character files)
- Read from entity_store via JsonFileBackend (NPC entities)
- HP/FP bars for both

---

## §3 World Views (Level 2 — Simulation Layer)

### 3.1 Entity Browser (Right Panel)

A searchable, filterable list of ALL entities:

```
┌─ Entity Browser ──────────────────────────────────────┐
│  [🔍 search by name, type, tag, facts...]              │
│                                                         │
│  Type: [All ▾]  Tier: [All ▾]  Status: [Active ▾]     │
│                                                         │
│  ── NPCs (24) ──                                        │
│  │  Marta Haskel ........... role:barkeep  [edit]      │
│  │  Captain Vane ........... faction:merchants  [edit] │
│  │  Father Matthias ........ faction:temple  [edit]    │
│  │  Garret the One-Eyed .... role:fence  [edit]        │
│                                                         │
│  ── Characters (4) ──                                    │
│  │  Elena Brightwood ...... fighter  [edit]             │
│  │  Marek Stonehand ....... cleric  [edit]              │
│  │  Seraphina Windmere .... wizard  [edit]              │
│  │  Tiko Swiftfoot ........ rogue  [edit]               │
│                                                         │
│  ── Locations (12) ──                                    │
│  │  Neverwinter ........... city  [view]  [edit]        │
│  │  The Siren's Rest ...... tavern  [view]  [edit]      │
│  │  Neverwinter Docks ..... district  [view]  [edit]    │
│                                                         │
│  ── Factions (3) ──                                      │
│  │  Merchant Guild ......................  [edit]        │
│  │  Temple of Lathander ................  [edit]        │
│  │  Neverwinter Watch .................  [edit]         │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Count badges per type
- Search across facts, tags, and id
- Filter by type, tags, position/radius
- Click to open detail view in main panel
- Each entity type has its own icon

### 3.2 Entity Detail / Editor

Clicking [edit] on any entity opens a full editor:

```
┌─ Edit: Marta Haskel ─── type:npc ─────────────────────┐
│                                                         │
│  ID:       npc_marta                                    │
│  Type:     [npc ▾]                                      │
│  Tags:     [name:Marta Haskel] [role:barkeep] [x]      │
│            [add tag...]                                  │
│                                                         │
│  ┌─ Facts ─────────────────────────────────────────┐   │
│  │ Stout woman in her mid-fifties, grey-streaked    │   │
│  │ brown hair. Runs The Siren's Rest. Knows everyone│   │
│  │ on the Docks.                                    │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ GURPS ─────────────────────────────────────────┐   │
│  │  ST [10]  DX [10]  IQ [11]  HT [10]              │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ State ─────────────────────────────────────────┐   │
│  │  HP [10]/[10]  FP [10]/[10]  ER [0]/[0]          │   │
│  │  Conditions: [+ add]  [frightened] [x]           │   │
│  │  Injuries: [+ add]                               │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ Skills ───────────────────────────────────────┐    │
│  │  cached_skills: [+ add]  [Savoir-Faire-13] [x] │    │
│  │  passive_abilities: [+ add]                    │    │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ Equipment ──────────────────────────────────────┐  │
│  │  Inventory: [+ add]  [Cellar keys] [x]           │   │
│  │  Currency:  gold [5]  silver [12]  copper [30]    │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ Position ──────────────────────────────────────┐   │
│  │  Inside: [loc_sirens_rest ▾]  x: [4.5]  y: [2.0]│   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ Effects / Scripts ────────────────────────────┐    │
│  │  🔄 bleeding  ( -2 HP/tick, 3 ticks left )  [x]│    │
│  │  📜 on_tick: emergency_heal  [view]  [detach]   │    │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  Last updated: Y0001-01-15 14:30                        │
│  [Save]  [Revert]  [Delete]                             │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- All entity blocks editable: facts, gurps, state, skills, equipment, tags, position
- Array fields show add/remove buttons
- Effects show remaining ticks, description, per_tick toggle
- Scripts show trigger type and source code (read-only view, attach/detach)
- Currency shows per-coin-type inputs
- Position has location picker (dropdown of all location entities)
- "Save" calls `set_entity` or `adjust_entity` via the Python backend

### 3.3 Event Log Viewer

```
┌─ Event Log ───────────────────────────────────────────┐
│  [🔍 filter...]  Tag: [All ▾]  Entity: [All ▾]       │
│                                                         │
│  Tick  Time              Event                   Source │
│  ─────────────────────────────────────────────────────  │
│  142   Y0001-01-15 14:30  Party entered tavern    GM   │
│  143   Y0001-01-15 14:35  Elena: "I glare at..."  SM   │
│  143   Y0001-01-15 14:35  Barkeep intimidated     auto │
│  144   Y0001-01-15 14:40  Negotiation resolved     GM   │
│  145   Y0001-01-15 14:45  ── tick ──               sys  │
│  145   Y0001-01-15 14:45  bleeding: -2 HP on Guard auto │
│  146   Y0001-01-15 14:50  Crate delivered to cellar WM  │
│                                                         │
│  [Show: 50 ▾]  [Auto-scroll]                            │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Read from `events.jsonl` via `query_events()`
- Filter by tag, entity_id, time range, narrative_flag
- Tick marks for automatic events (effects, scripts)
- Color-coded by source (GM/SM/WM/auto/system)
- Auto-scroll toggle (default on for new events)

### 3.4 World Map (Future)

Entities with `position.coords` shown on a 2D or 3D map:
- Locations as pins/areas
- NPCs as icons within locations
- Character positions as party markers
- Connections between locations as edges
- Click any pin → open entity detail

The OpenGL app (`opengl_app/`) is the intended host for this.

---

## §4 Agent Monitoring

### 4.1 Agent Status Dashboard

```
┌─ Agents ──────────────────────────────────────────────┐
│                                                         │
│  WM ── idle ── last invocation: 12 ticks ago           │
│    next: tick 160 (18 ticks) ── [invoke now]           │
│    last summary: "Created merchant guild faction..."    │
│                                                         │
│  GM ── active ── in scene: The Siren's Rest            │
│    news queue: 3 items  [show ▾]                       │
│    last summary: "Set negotiation scene..."             │
│                                                         │
│  SM ── running ── turn 4/10                             │
│    phase: character_execution (Tiko)                    │
│    ⏳ waiting for character decision...                 │
│                                                         │
│  Characters:                                            │
│    Elena Brightwood ── idle ── last acted: 2 turns ago  │
│    Marek Stonehand ── idle ── last acted: 2 turns ago   │
│    Seraphina Windmere ── thinking... ── ⏳              │
│    Tiko Swiftfoot ── idle ── last acted: 1 turn ago     │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Agent status: idle, active, error
- Queue lengths (news queue, scheduler queue)
- Force-invoke button for each agent
- Phase/step indicator for SM

### 4.2 Prompt Inspector

For debugging agent behavior. Shows the **entire** message sequence for any
invocation — from initial system prompt through tool calls and responses
to the final `close_invocation`.

**Design principle:** Do NOT filter or pre-digest the prompt into expected
categories. Instead, show **every message** in sequence and **type-label**
the ones we recognize. This catches unexpected or novel content that a
rigid categorizer would miss.

```
┌─ Prompt Inspector ── GM ── invocation #142 ──── 4,200 tok ──┐
│                                                               │
│  Agent: GM  │  Started: Y0001-01-15 14:30  │  Duration: 12s  │
│  [Download raw JSON]  [Save to logs/last_prompts/]            │
│                                                               │
│  ── Full message sequence (27 messages) ────────────────[fit] │
│                                                               │
│  #  Role    Type/Label           Size    Content               │
│  ───────────────────────────────────────────────────────────  │
│   1  system  [prompt.txt]        1,024   "You are the GM..."  │
│   2  system  [world_setting]       412   "World essence: ..." │
│   3  system  [arc_summaries]      850   "Arc 1: ..."          │
│   4  system  [gm_summaries]       320   "3 summaries"         │
│   5  system  [character:Elena]    620   "Description, state..."│
│   6  system  [character:Marek]    580   "Description, state..."│
│   7  system  [UNKNOWN: "store_    124   "Storage notice..."   │
│               age_notice"]                                     │
│   8  user    [reason]              45    "Negotiation scene"   │
│   9  user    [newspaper]          210   "Guard injured, door  │
│                                         opened"               │
│  10  system  [tools_available]    120   "19 tools"            │
│  11  user    [human_input]         56    "Continue the scene" │
│ ───────────────────────────────────────────────────────────── │
│  12  assistant  tool_call:          72   get_entity           │
│                get_entity                   npc_marta         │
│  13  tool       result:            412   "{id: npc_marta...}" │
│  14  assistant  tool_call:          68   query_entities       │
│                query_entities              type=location      │
│  15  tool       result:           1220   "[12 locations]"     │
│  16  assistant  tool_call:          52   adjust_entity        │
│                adjust_entity              npc_guard hp=7      │
│  17  tool       result:            125   "{hp: 14 -> 7}"      │
│  18  assistant  thinking:          340   "The guard is ..."   │
│  19  assistant  tool_call:          48   close_invocation     │
│                close_invocation                                  │
│  20  tool       result:             32   "invocation_closed"  │
│                                                               │
│  ── Per-type summary ──────────────────────────────────────   │
│  System:   10 messages  (3,250 tok)  [show list ▾]           │
│  User:      2 messages  (  266 tok)                           │
│  Assistant: 5 messages  (  580 tok)  [thinking + tool calls]  │
│  Tool:      3 messages  (1,757 tok)                           │
│                                                               │
│  Total tokens: 4,200  │  Tool calls: 3  │  Unique tools: 3   │
└───────────────────────────────────────────────────────────────┘
```

Requirements:
- **Every message** in the sequence shown, numbered, with role, detected
  type/label, token size, and a preview of content.
- **Type/label detection** — scans message content for known block prefixes:
  `[world_setting]`, `[arc_summaries]`, `[gm_summaries]`,
  `[character:{name}]`, `[reason]`, `[newspaper]`, `[tools_available]`,
  `[storage_notice]`, `[world_registry]`, `[subscription_trigger]`,
  `[event_summary]`, `[human_input]`.
- **Unknown blocks** are labeled `[UNKNOWN: first_60_chars...]` — never hidden.
- **Tool calls** shown with tool name, arguments preview, return value size.
- **Tool results** expandable — click to see full JSON.
- **Per-type summary** at bottom: counts per role (system/user/assistant/tool)
  with aggregate token sizes.
- **Collapse/expand** each message row to see full content.
- **Download raw JSON** — full messages array as `messages.json`.
- **Save to logs** — copies to `logs/last_prompts/{agent}_{invocation_id}.json`
  for offline analysis. The system already writes prompt snapshots there
  (`_save_character_prompt_snapshot` etc.) — this panel reads from those files.
- **Capture entire invocation** — not just the initial prompt, but ALL
  messages up to and including `close_invocation`. This shows the full
  tool call chain and agent reasoning.

### 4.3 Script / Effect Monitor

Real-time view of running automations:

```
┌─ Active Automations ──────────────────────────────────┐
│                                                         │
│  Entity           Script/Effect              Next Fire  │
│  ─────────────────────────────────────────────────────  │
│  lich_villain     Aura of Despair (on_tick)  tick 147 │
│  npc_guard_03     bleeding (effect)          2 ticks   │
│  npc_guard_03     on_stat_threshold(hp<3)    watching  │
│  faction_guild    Trade Route Check (on_tick) tick 200 │
│                                                         │
│  [Pause all]  [Resume all]                              │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Read from entity_store `scripts[]` and `effects[]` on all entities
- Show remaining duration for effects
- Show trigger type and interval for scripts
- Ability to detach scripts / remove effects from the list

---

## §5 Human Input

### 5.1 Character Action Input

The main input area during a scene:

```
┌─────────────────────────────────────────────────────────┐
│  [Elena ▾]  [I step forward and...                    ] │
│  [Send]  [or press Ctrl+Enter]                          │
│                                                         │
│  ⚡ Quick actions:                                      │
│  [Attack]  [Talk]  [Move]  [Use item]  [Cast spell]    │
│                                                         │
│  Last 3 inputs:                                         │
│  • Tiko: I sneak around the back                         │
│  • GM: The guard spots you — roll Stealth               │
│  • Marek: I cast Light on the guard's helmet            │
└─────────────────────────────────────────────────────────┘
```

Requirements:
- Character selector (only active scene participants)
- Quick action buttons pre-fill the prompt with structured intent
- Shows last N inputs for context
- Enter sends, Shift+Enter = newline

### 5.2 GM Override Panel

For the GM to directly edit state during play:

```
┌─ GM Tools ────────────────────────────────────────────┐
│  [adjust entity]                                       │
│  Entity: [npc_marta ▾]                                 │
│  HP: [7]  Condition: [+ add]  [stunned] [x]           │
│  [Apply]                                               │
│                                                         │
│  [attach script]                                        │
│  Entity: [npc_marta ▾]  Trigger: [on_tick ▾]           │
│  Interval: [5] ticks  Code: [edit...]                  │
│  [Attach]                                               │
│                                                         │
│  [record event]  desc: [...]  tag: [narrative]         │
│  [Record]                                               │
│                                                         │
│  [create entity]  type: [npc ▾]  id: [...]              │
│  facts: [...]  [Create]                                 │
└─────────────────────────────────────────────────────────┘
```

---

## §6 Technical Requirements

### 6.1 Data Sources

| GUI Component | Backend API | Source |
|---|---|---|
| Story Timeline | `World.get_story()` → parse arcs/paragraphs/turns | `game/world/story.json` |
| Active Scene | `get_scene(location_id)` | entity_store + event_log |
| Entity Browser | `query_entities(type=..., tags=..., facts_search=...)` | entity_store (CompositeBackend) |
| Entity Editor | `get_entity(id)` + `set_entity(id, ...)` / `adjust_entity(...)` | entity_store + character files |
| Event Log | `query_events(tag=..., entity_id=..., limit=...)` | event_log (events.jsonl) |
| Agent Status | scheduler_queue + agent history files | `game/entity_store/scheduler_queue.json` |
| Script/Effect Monitor | `query_entities()` → inspect scripts[]/effects[] | entity_store |

### 6.2 Backend Connector

The GUI needs a bridge module that translates between GUI actions and the Python API:

```python
# gui_bridge.py — thin connector between GUI and the engine

class GuiBridge:
    def get_scene_view(self) -> dict: ...
    def get_story_tree(self) -> list: ...
    def get_entity_list(self, type: str = "", search: str = "") -> list: ...
    def get_entity_detail(self, id: str) -> dict: ...
    def save_entity(self, id: str, data: dict) -> dict: ...
    def send_character_action(self, character: str, action: str) -> str: ...
    def advance_turn(self) -> dict: ...
    def invoke_agent(self, agent: str, notice: str) -> dict: ...
    def get_agent_status(self) -> dict: ...
    def get_event_log(self, **filters) -> list: ...
```

### 6.3 Architecture — Single Process, No Network

```
┌─────────────────────────────────────────────────────────┐
│                    LLM World Process                      │
│                                                           │
│  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │   Engine          │  │   GUI (Dear ImGui panels)    │  │
│  │                   │  │                              │  │
│  │  console_app.py   │  │  SceneView    EntityEditor   │  │
│  │  turn_runner.py   │  │  StoryPanel   EventLogView   │  │
│  │  entity_core.py   │  │  AgentDash    ScriptMonitor  │  │
│  │  tool_registry.py │  │  WorldMap (3D)               │  │
│  │  effect_engine.py │  │                              │  │
│  │  script_engine.py │  │  ┌────────────────────────┐  │  │
│  │  news_queue.py    │  │  │  GuiBridge (thin API) │  │  │
│  └──────────────────┘  │  └────────────────────────┘  │  │
│        │ direct calls  │         │                     │  │
│        └───────────────┴─────────┘                     │  │
│                                                           │
│  Shared state: World(), EntityCore singletons             │
└─────────────────────────────────────────────────────────┘
```

No HTTP. No WebSocket. No JSON serialization between layers.
GUI calls `entity_core.get_entity(id)` directly — same process, same memory.

### 6.4 Transport (Not Applicable)

There is no network transport. The GUI is a native desktop application
rendered in the same process as the engine. Data flows through direct
Python function calls.

- **Push (SSE/WebSocket)**: Event log, agent status, scene updates pushed from engine to GUI
- **Pull (poll)**: Entity list, story tree — polled on open or explicit refresh
- **Manual**: Entity editor saves only on explicit "Save" click

---

## §7 Visual Language

### 7.1 Color Palette (Dark Theme)

```
Background:       #0f172a (slate-900)
Surface:          #1e293b (slate-800)
Surface hover:    #334155 (slate-700)
Border:           #475569 (slate-600)
Text primary:     #f1f5f9 (slate-100)
Text secondary:   #94a3b8 (slate-400)
Accent:           #3b82f6 (blue-500)
Danger:           #ef4444 (red-500)
Success:          #22c55e (green-500)
Warning:          #f59e0b (amber-500)
```

### 7.2 Entity Icons

```
Character:  👤 or silhouette
NPC:        👥 or person outline
Location:   📍 or map pin
Faction:    🏛 or shield
Item:       📦 or box
Template:   📋 or clipboard
```

---

## §8 Priority Matrix

| Feature | Priority | Effort | Value |
|---|---|---|---|
| Scene view (chat + character list + input) | P0 | Medium | High — core gameplay |
| Entity browser (list + filter) | P0 | Medium | High — core usability |
| Entity editor (detail + save) | P0 | Large | High — essential for world building |
| Story timeline | P1 | Medium | Medium — narrative navigation |
| Event log viewer | P1 | Medium | Medium — debugging |
| Agent status dashboard | P1 | Small | Medium — operations |
| Character detail overlay | P1 | Small | Medium — scene context |
| GM override panel | P2 | Medium | Medium — power user feature |
| Script/effect monitor | P2 | Small | Medium — debugging |
| Prompt inspector | P2 | Medium | Low — advanced debugging |
| World map (3D OpenGL) | P3 | Very Large | Low — nice to have |
