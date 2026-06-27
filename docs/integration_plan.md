# System Integration Plan — Entity Architecture & Tool Unification

*Complete OOP design, gap analysis, and phased integration roadmap.*

---

## §0 Current State — What Exists vs What's Missing

### 0.1 Three Storage Silos (No Bridge)

```
┌─────────────────────────────────────────────────────────────┐
│ old game/world/*.json    │ entity_store/entities.json       │ game/characters/{name}/*.json │
│                          │                                  │                               │
│ locations.json           │ set_entity(id, type, facts,      │ description.json               │
│ npc.json                 │   gurps, stats, tags, position)  │ state.json                     │
│ info.json                │ get_entity(id)                   │ skills.json                    │
│ story.json               │ query_entities(type=...)         │ equipment.json                 │
│ scene.json               │                                  │ memory.json                    │
│                          │                                  │ metadata.json                  │
│ World.get_npcs() ──▶ here│ World.get_npcs() ──▶ NOT here   │ build_character_info_block ──▶ │
│ World.get_locations() ──▶│ World.get_locations() ──▶ NOT   │ here                           │
│                          │                                  │                               │
│ Legacy, frozen           │ Active development, incomplete   │ Characters only, not entities  │
└──────────────────────────┴──────────────────────────────────┴───────────────────────────────┘
```

### 0.2 Gap Matrix

| Feature | Doc Says | Code Has | Status |
|---|---|---|---|
| Entity CRUD | `set_entity`, `get_entity`, `query_entities` | ✅ Implemented | Done |
| Event log | `record_event`, `query_events` | ✅ Implemented | Done |
| Tags | `add_tags`, `remove_tags` | ✅ Implemented | Done |
| Effects | `set_effect`, `remove_effect` | ✅ Storage only, ⛔ no execution engine | Half-done |
| Scripts | `attach_script`, `detach_script` | ✅ Storage only, ⛔ no execution engine | Half-done |
| Movement | `set_movement` | ✅ Implemented | Done |
| Scheduler | `invoke_self`, `pop_due_invocations` | ✅ Implemented | Done |
| `close_invocation` | Universal termination with summary+news | ⛔ Not implemented | Missing |
| `adjust_entity` | Targeted runtime mutations (HP/items/conditions) | ⛔ Not implemented | Missing |
| News queues | Per-agent accumulated news, consumed on call | ⛔ Not implemented | Missing |
| Agent summaries | Per-agent summary files fed by close_invocation | ⛔ Not implemented | Missing |
| World→entity delegation | `World.get_npcs()` reads from entity_store | ⛔ Still reads old JSON | Missing |
| Characters as entities | Characters registered in entity_store type="character" | ⛔ Characters are filesystem-only | Missing |
| Script execution engine | Runs Python on triggers with tool API injected | ⛔ Not implemented | Missing |
| Effect engine | Applies per_tick deltas, expires on duration | ⛔ Not implemented | Missing |
| Facts validation | Warns if mechanical data in facts | ⛔ Not implemented | Missing |
| `update_entity_stat` bug | Writes to cache, persists only facts | ⛔ Bug exists | Bug |

### 0.3 Tool Duplication

Same tools are re-declared in three files with different wrappers:
- `world_manager/tools.py` — 18 tools for WM
- `gm/tools.py` — 16 tools for GM (re-imports from entity_store directly)
- `scene_manager/tools.py` — 9 tools for SM (re-imports from entity_store directly)
- `gm/game_master.py` — imports from `gm/tools.py` AND declares `invoke_self_gm`, `answer_lore_question_gm` locally
- `world_manager/core.py` — imports from `world_manager/tools.py`

WM gets `set_movement`, `delete_entity`, `write_correction` (GM does not).
GM gets `notify_wm`, `escalate_to_sm` (WM does not).
SM gets `get_scene`, `get_entity_state`, `update_entity_stat`, `apply_effect`, `escalate_to_gm`.

---

## §1 OOP Architecture

### 1.1 Class Hierarchy

```
┌──────────────────────────────────────────────────────────────────┐
│                        ToolRegistry                              │
│  ToolRegistry.for_wm() → List[Tool]                              │
│  ToolRegistry.for_gm() → List[Tool]                              │
│  ToolRegistry.for_sm() → List[Tool]                              │
│  ToolRegistry.for_character() → List[Tool]                       │
│                                                                  │
│  Single source of truth for which tools each agent gets.         │
│  Each tool is a @tool-decorated function, imported once.         │
└──────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
         ┌──────────▼──────┐  ┌────▼───────┐  ┌────▼──────────┐
         │   EntityCore    │  │  EventLog  │  │  Scheduler    │
         │  (write facade) │  │ (exists)   │  │ (exists)      │
         └──────────▲──────┘  └────────────┘  └────▲──────────┘
                    │                               │
         ┌──────────┴──────────┐                   │
         │                     │                   │
  ┌──────▼───────┐    ┌───────▼───────┐    ┌──────▼───────┐
  │EntityBackend │    │ScriptEngine   │    │ NewsQueue    │
  │ (abstract)   │    │ (planned)     │    │ (planned)    │
  └──────▲───────┘    └───────────────┘    └──────────────┘
         │
    ┌────┴────┐
    │         │
┌───▼──┐  ┌───▼──────┐
│JSON  │  │Character │
│Store │  │FileBridge│
│(disk)│  │(planned) │
└──────┘  └──────────┘
```

### 1.2 `EntityBackend` — Abstract Interface

```python
class EntityBackend(ABC):
    """Abstract CRUD interface for entities.
    
    Two implementations:
      - JsonFileBackend: reads/writes game/entity_store/entities.json
      - CharacterFileBackend: reads/writes game/characters/{name}/*.json
    
    The agent never sees this — EntityCore selects the backend.
    """

    @abstractmethod
    def set(self, id: str, type: str, base_type: str,
            facts: str, gurps: dict, state: dict,
            skills: dict, equipment: dict,
            stats: dict, tags: list, position: dict) -> dict:
        """Create or fully overwrite an entity."""
        ...

    @abstractmethod
    def get(self, id: str) -> Optional[dict]:
        """Read entity by id. Returns full record or None."""
        ...

    @abstractmethod
    def get_facts(self, id: str) -> Optional[str]:
        """Read only the facts field."""
        ...

    @abstractmethod
    def set_facts(self, id: str, facts: str) -> dict:
        """Update only the facts field."""
        ...

    @abstractmethod
    def query(self, type_filter: str = "", tags: list = None,
              position: dict = None, radius: float = -1,
              facts_search: str = "", limit: int = 50) -> list:
        """Search entities by type, tags, position, text."""
        ...

    @abstractmethod
    def delete(self, id: str, reason: str = "") -> dict:
        """Soft-delete an entity."""
        ...

    # ── Partial mutations ──

    @abstractmethod
    def adjust(self, id: str, *, hp=None, fp=None, er=None,
               add_condition="", remove_condition="",
               add_injury="", remove_injury="",
               add_item="", remove_item="",
               currency_delta=None,
               add_ability="", remove_ability="",
               add_skill="", remove_skill="",
               current_state="") -> dict:
        """Targeted runtime mutations — only provided fields change."""
        ...

    # ── Array-like mutations (same semantics for filesystem and JSON) ──

    @abstractmethod
    def add_tags(self, id: str, tags: list) -> dict: ...
    @abstractmethod
    def remove_tags(self, id: str, tags: list) -> dict: ...

    @abstractmethod
    def set_effect(self, id: str, effect_id: str,
                   stat_deltas: dict, per_tick: bool,
                   duration_ticks: int, description: str,
                   aura_radius: float = -1) -> dict: ...

    @abstractmethod
    def remove_effect(self, id: str, effect_id: str) -> dict: ...

    @abstractmethod
    def attach_script(self, id: str, trigger_type: str,
                      trigger_params: dict, script: str,
                      comment: str = "") -> dict: ...

    @abstractmethod
    def detach_script(self, id: str, script_id: str) -> dict: ...
```

### 1.3 `JsonFileBackend` — Entity Store Implementation

```python
class JsonFileBackend(EntityBackend):
    """Default backend. Single JSON file at game/entity_store/entities.json.
    
    In-memory cache (_cache dict), atomic save via .tmp rename.
    Same implementation as current world_manager/entity_store.py,
    extended with state/skills/equipment blocks and adjust_entity.
    """
    
    def __init__(self, path: Path = Path("game/entity_store/entities.json")):
        self._path = path
        self._cache: dict = {}
        self._dirty = False
```

### 1.4 `CharacterFileBackend` — Character Filesystem Bridge

```python
class CharacterFileBackend(EntityBackend):
    """Routes entity operations to game/characters/{name}/*.json files.
    
    Maps entity fields to files:
      Entity field     Character file
      facts            description.json (written as {general, race, personality})
      state            state.json  (hp, fp, conditions, injuries)
      skills           skills.json (ST/DX/IQ/HT, cached_skills, passive_abilities)
      equipment        equipment.json (signature, inventory, currency)
      gurps            → split across skills.json (stats) and state.json (hp, fp, will)
      tags             stored in metadata.json as "_tags"
      position         stored in description.json
    """
    
    def __init__(self, game_root: Path = Path("game")):
        self._game_root = game_root
```

### 1.5 `CompositeBackend` — Auto-Select Backend

```python
class CompositeBackend(EntityBackend):
    """Routes each entity_id to the correct backend.
    
    Lookup order:
      1. Is entity_id a key in JsonFileBackend? → use JsonFileBackend
      2. Is entity_id a character folder name? → use CharacterFileBackend
      3. Is there a tag "name:<entity_id>" in JsonFileBackend? → use JsonFileBackend
    
    Writes go to ONE backend. Reads check both (JsonFileBackend first).
    """
    
    def __init__(self, json_backend: JsonFileBackend, 
                 char_backend: CharacterFileBackend):
        self._json = json_backend
        self._char = char_backend
    
    def _resolve(self, id: str) -> tuple[EntityBackend, str]:
        """Returns (backend, resolved_id)."""
        # 1. Check JSON store
        if self._json.get(id) is not None:
            return (self._json, id)
        # 2. Check character folders
        char_dir = self._char._game_root / "characters" / id
        if char_dir.exists():
            return (self._char, id)
        # 3. Check name tags in JSON store
        for eid, entity in self._json._cache.items():
            if f"name:{id}" in entity.get("tags", []):
                return (self._json, eid)
        # Default: JSON store, let it create
        return (self._json, id)
```

### 1.6 `EntityCore` — High-Level Facade

```python
class EntityCore:
    """High-level entity operations. What agents actually call.
    
    Layers on top of EntityBackend:
      - Facts validation (no mechanical data in facts)
      - Field-level warnings
      - Effect expiry tracking (via tick())
      - Script trigger evaluation (via tick())
    """
    
    def __init__(self, backend: CompositeBackend):
        self._backend = backend
    
    def set(self, id, type, ...) -> dict:
        # Validate facts for mechanical patterns
        # Delegate to backend
        # Log to event_log
        pass
    
    def get(self, id) -> Optional[dict]:
        return self._backend.get(id)
    
    def adjust(self, id, **kwargs) -> dict:
        # Validate: hp/fp/er must be positive ints
        # Delegate to backend
        # Return summary of what changed
        pass
    
    def query(self, **kwargs) -> list:
        return self._backend.query(**kwargs)
    
    def tick(self, current_tick: int):
        """Called every world tick.
        
        1. Apply active effects (per_tick deltas)
        2. Expire effects that have reached duration_ticks
        3. Evaluate script triggers
        """
        pass
```

### 1.7 `NewsQueue` — Per-Agent News Accumulation

```python
class NewsQueue:
    """File-backed FIFO queue per agent.
    
    Path: game/entity_store/{agent}_news.jsonl
    
    Lower agents append. Orchestrator consumes when invoking higher agent.
    """
    
    def __init__(self, agent: str):
        self._path = Path(f"game/entity_store/{agent}_news.jsonl")
    
    def append(self, items: list[str], source: str):
        """Append news items from a lower agent."""
        ...
    
    def consume(self) -> list[str]:
        """Read all and clear. Returns items."""
        ...
    
    def peek(self, limit: int = 5) -> list[str]:
        """Read without consuming."""
        ...
```

### 1.8 `ToolRegistry` — Single Source of Truth for Tools

```python
class ToolRegistry:
    """Factory for agent tool sets.
    
    Every tool is defined ONCE here. Each agent gets a subset.
    Tools are only @tool decorated here, not in three separate files.
    """

    # ── Entity tools ──
    @staticmethod
    def _set_entity(...) -> dict: ...
    @staticmethod
    def _adjust_entity(...) -> dict: ...
    @staticmethod
    def _get_entity(...) -> dict: ...
    @staticmethod
    def _get_entity_facts(...) -> str: ...
    @staticmethod
    def _set_entity_facts(...) -> dict: ...
    @staticmethod
    def _query_entities(...) -> list: ...
    @staticmethod
    def _add_tags(...) -> dict: ...
    @staticmethod
    def _remove_tags(...) -> dict: ...
    @staticmethod
    def _delete_entity(...) -> dict: ...

    # ── Effect tools ──
    @staticmethod
    def _set_effect(...) -> dict: ...
    @staticmethod
    def _remove_effect(...) -> dict: ...

    # ── Script tools ──
    @staticmethod
    def _attach_script(...) -> dict: ...
    @staticmethod
    def _detach_script(...) -> dict: ...

    # ── Event tools ──
    @staticmethod
    def _record_event(...) -> dict: ...
    @staticmethod
    def _query_events(...) -> list: ...

    # ── Agent lifecycle tools ──
    @staticmethod
    def _close_invocation(summary, news, invoke_self) -> dict: ...
    @staticmethod
    def _invoke_self(delay_ticks, world_time, comment) -> dict: ...

    # ── Agent-specific tools ──
    @staticmethod
    def _notify_wm(message) -> dict: ...
    @staticmethod
    def _write_correction(message, entity_id) -> dict: ...
    @staticmethod
    def _escalate_to_sm(message) -> dict: ...
    @staticmethod
    def _escalate_to_gm(question, context) -> str: ...
    @staticmethod
    def _get_scene(location_id) -> dict: ...
    @staticmethod
    def _get_entity_state(entity_id) -> dict: ...

    @classmethod
    def for_wm(cls) -> list:
        return [
            cls._set_entity, cls._adjust_entity,
            cls._get_entity, cls._get_entity_facts, cls._set_entity_facts,
            cls._query_entities,
            cls._add_tags, cls._remove_tags, cls._delete_entity,
            cls._set_effect, cls._remove_effect,
            cls._attach_script, cls._detach_script,
            cls._record_event, cls._query_events,
            cls._close_invocation, cls._invoke_self,
            cls._write_correction,
            cls._get_entity_state,
        ]  # 18 tools

    @classmethod
    def for_gm(cls) -> list:
        return [
            cls._set_entity, cls._adjust_entity,
            cls._get_entity, cls._get_entity_facts, cls._set_entity_facts,
            cls._query_entities,
            cls._add_tags, cls._remove_tags,
            cls._set_effect, cls._remove_effect,
            cls._attach_script, cls._detach_script,
            cls._record_event, cls._query_events,
            cls._close_invocation, cls._invoke_self,
            cls._notify_wm, cls._escalate_to_sm,
            cls._get_entity_state,
        ]  # 19 tools

    @classmethod
    def for_sm(cls) -> list:
        return [
            cls._get_entity_facts,
            cls._query_events, cls._record_event,
            cls._close_invocation, cls._invoke_self,
            cls._get_scene, cls._get_entity_state,
            cls._adjust_entity,        # SM: state+inventory only (restricted via docstring)
            cls._set_effect, cls._remove_effect,
            cls._escalate_to_gm,
        ]  # 11 tools
```

---

## §2 Data Model — Entity Schema (Final)

### 2.1 Universal Entity Record

```python
{
  # ── Identity ──
  "id": "elena_brightwood",        # human-readable slug
  "type": "character",             # character | npc | location | faction | item | template
  "base_type": "human_template",   # optional template to inherit from

  # ── Layer 1: Narrative Identity (survives into pinned context) ──
  "facts": "A human fighter with a chipped longsword...",
  # STATIC. Who/what this entity IS. NOT current state, NOT mechanics.
  # This is what every agent sees in their context.

  # ── Layer 2: Mechanical Stats (read by SM for resolution) ──
  "gurps": {"ST": 12, "DX": 12, "IQ": 10, "HT": 12},
  # GURPS core. Static after creation.

  # ── Layer 3: Dynamic State (changes every scene via adjust_entity) ──
  "state": {
    "hp": 14, "max_hp": 14,
    "fp": 12, "max_fp": 12,
    "er": 0, "max_er": 0,
    "will": 12,
    "conditions": [],
    "injuries": [],
    "hunger": 0, "thirst": 0, "fatigue": 0
  },

  # ── Layer 4: Skills (semi-dynamic) ──
  "skills": {
    "ST": 12, "DX": 12, "IQ": 10, "HT": 12,
    "narrative_tier": "exceptional",
    "passive_abilities": ["High Pain Threshold", "Combat Reflexes"],
    "cached_skills": ["Broadsword-14", "Shield-13", ...],
    "lore_interpretation_rule": "Elena is a capable front-line fighter..."
  },

  # ── Layer 5: Equipment (semi-dynamic) ──
  "equipment": {
    "signature": ["Longsword", "Leather cuirass", "Wooden shield"],
    "inventory": ["Backpack", "Bedroll", "Waterskin", ...],
    "currency": {"gold": 12, "silver": 8, "copper": 45}
  },

  # ── Layer 6: Free-Form Extension (belly button, NOT in pinned context) ──
  "stats": {"reputation": 3.0},
  # WM tools and scripts read this. Narrative engine does NOT.

  # ── Layer 7: Query Surface ──
  "tags": ["name:Elena Brightwood", "role:fighter", "faction:neutral"],
  "position": {"inside": "loc_sirens_rest", "relative": [4.0, 2.0]},

  # ── Layer 8: Runtime Automation ──
  "scripts": [],
  "effects": [],

  # ── Layer 9: System ──
  "created_at": "...",
  "updated_at": "...",
  "deleted_at": null
}
```

### 2.2 Backend Mapping

| Entity Field | JsonFileBackend | CharacterFileBackend |
|---|---|---|
| `facts` | `entities.json[id].facts` | `description.json` (with structure) |
| `gurps` | `entities.json[id].gurps` | → `skills.json.stats` (ST/DX/IQ/HT) |
| `state` | `entities.json[id].state` | `state.json` |
| `skills` | `entities.json[id].skills` | `skills.json` (merged with gurps) |
| `equipment` | `entities.json[id].equipment` | `equipment.json` |
| `stats` | `entities.json[id].stats` | Not stored (characters don't use belly button) |
| `tags` | `entities.json[id].tags` | `metadata.json._tags` |
| `position` | `entities.json[id].position` | `description.json.location` (legacy) |
| `scripts` | `entities.json[id].scripts` | Not supported (characters don't get scripts) |
| `effects` | `entities.json[id].effects` | → `state.json.conditions` (simplified) |
| `created_at` | Timestamp | From metadata.json |

---

## §3 Agent Tool Sets — Final

### 3.1 WM Tools (19)

| # | Tool | Category | Notes |
|---|---|---|---|
| 1 | `set_entity` | Entity | Create/full-update any entity type |
| 2 | `set_entity_facts` | Entity | Facts-only update |
| 3 | `get_entity` | Entity | Read any entity in full |
| 4 | `get_entity_facts` | Entity | Read facts only (for context building) |
| 5 | `get_entity_state` | Entity | Read gurps+state+skills+effects (for SM, WM has it too) |
| 6 | `adjust_entity` | Entity | Targeted runtime mutations |
| 7 | `query_entities` | Entity | Search by type, tags, position, text |
| 8 | `add_tags` / `remove_tags` | Entity | Tag management |
| 9 | `delete_entity` | Entity | Soft-delete |
| 10 | `set_effect` / `remove_effect` | Effects | Apply/remove modifiers |
| 11 | `attach_script` / `detach_script` | Scripts | Automated behavior |
| 12 | `set_movement` | Movement | Entity travel |
| 13 | `record_event` / `query_events` | Events | 4D causal log |
| 14 | `write_correction` | Communication | WM→GM correction |
| 15 | `invoke_self` | Lifecycle | Schedule next call |
| 16 | `close_invocation` | Lifecycle | Terminate with summary+news |
| 17 | `read_tool_doc` | Utility | Parameter documentation |

WM-exclusive: `delete_entity`, `set_movement`, `write_correction`

### 3.2 GM Tools (18)

Same as WM minus `delete_entity`, `set_movement`, `write_correction`, plus:
- `notify_wm` — GM→WM notification
- `escalate_to_sm` — GM→SM instruction

GM-exclusive: `notify_wm`, `escalate_to_sm`

### 3.3 SM Tools (11)

| # | Tool | Category | Notes |
|---|---|---|---|
| 1 | `get_entity_facts` | Entity | Read-only facts |
| 2 | `query_events` | Events | Read-only events |
| 3 | `record_event` | Events | Write event |
| 4 | `adjust_entity` | Entity | HP/FP/conditions/items only (NO skills) |
| 5 | `set_effect` / `remove_effect` | Effects | Scene effects |
| 6 | `get_scene` | Scene | Location + participants + events |
| 7 | `get_entity_state` | Entity | gurps+state+effects |
| 8 | `invoke_self` | Lifecycle | Schedule next call |
| 9 | `close_invocation` | Lifecycle | Terminate with summary+news |
| 10 | `escalate_to_gm` | Communication | SM→GM question |
| 11 | `read_tool_doc` | Utility | Documentation |

SM does NOT get: `set_entity`, `set_entity_facts`, `get_entity`, `query_entities`, `add_tags`, `attach_script`, `notify_wm`, `escalate_to_sm`, `write_correction`, `delete_entity`.

### 3.4 Character Agent Tools (2)

| # | Tool | Notes |
|---|---|---|
| 1 | `character_decision` | Commit to an intent (existing) |
| 2 | `ask_scene_manager` | Q&A about scene (existing) |

Character agent tools are unchanged — they operate inside the scene runtime, not on the world model.

---

## §4 Invocation Lifecycle (Final)

### 4.1 Flow

```
1. Orchestrator checks scheduler_queue for due invocations
2. For each due agent:
   a. Build [newspaper] = NewsQueue.consume() + narrative_flag events
   b. Build [reason] = invoke_self.comment | trigger description
   c. Assemble Layer 0-3 (system + pinned + history + invocation)
   d. Call agent → ReAct loop (WM/GM) or forced tool call (SM)
   e. Agent runs tools, eventually calls close_invocation(summary, news, invoke_self?)
   f. Orchestrator:
      - Appends summary to agent's messages.json (feeds PinnedBlockCache)
      - Appends news items to higher agent's NewsQueue
      - If invoke_self provided, calls schedule_invocation()
```

### 4.2 `close_invocation` Implementation

```python
@tool
def close_invocation(
    summary: str,
    news: Optional[list[str]] = None,
    invoke_self: Optional[dict] = None,
) -> dict:
    """End the current invocation with a summary for future context.
    
    Args:
        summary: 2-4 sentences describing what this invocation achieved.
            Saved to messages.json → feeds PinnedBlockCache for future calls.
        news: 0-3 short items for the higher agent's newspaper.
            E.g. ["Player defeated goblin chieftain", "Secret door discovered"].
        invoke_self: Optional scheduling for next call.
            {"delay_ticks": N} or {"world_time": T}.
            Optional "comment" key for the [reason] block.
    
    Returns:
        {"status": "invocation_closed", "summary_length": len(summary)}
    """
    # This is a sentinel tool — the orchestrator detects it was called
    # and uses its arguments to finalize the invocation.
    # The actual implementation is in the orchestrator's loop,
    # not in this function body.
    pass
```

### 4.3 News Flow

```
SM closes → news=["guard injured"] → GM's NewsQueue.append(["guard injured"])
SM closes → news=["door opened"]   → GM's NewsQueue.append(["door opened"])
GM called → newspaper = GM's NewsQueue.consume() → ["guard injured", "door opened"]
GM closes → news=["scene resolved"] → WM's NewsQueue.append(["scene resolved"])
```

News is consumed on read. Items not consumed persist in the queue file.
`record_event(narrative_flag=True)` also adds to the higher agent's queue automatically.

---

## §5 Phase Plan

### Phase 0 — Foundation (Current State + Bugfix) 
**Effort: 1-2 days**

| Step | What | Files |
|---|---|---|
| 0.1 | Fix `update_entity_stat` bug (writes to cache, persists only facts) | `scene_manager/tools.py` |
| 0.2 | Add `state`, `skills`, `equipment` optional fields to `set_entity()` in entity_store | `world_manager/entity_store.py` |
| 0.3 | Add `adjust_entity()` to entity_store backend (shallow merge on state/skills/equipment) | `world_manager/entity_store.py` |
| 0.4 | Wire `adjust_entity` as tool for WM and GM | `world_manager/tools.py`, `gm/tools.py` |
| 0.5 | Wire `adjust_entity` (restricted) for SM | `scene_manager/tools.py` |
| 0.6 | Add facts validation to `set_entity` (warn on mechanical patterns in facts) | `world_manager/entity_store.py` |

### Phase 1 — Backend Unification
**Effort: 2-3 days**

| Step | What | Files |
|---|---|---|
| 1.1 | Extract `EntityBackend` abstract class | `world_manager/backend.py` (new) |
| 1.2 | Rename `entity_store.py` to `JsonFileBackend`, implement `EntityBackend` | `world_manager/json_file_backend.py` |
| 1.3 | Implement `CharacterFileBackend` (reads/writes character files) | `world_manager/character_file_backend.py` (new) |
| 1.4 | Implement `CompositeBackend` (auto-routes entity_id to correct backend) | `world_manager/composite_backend.py` (new) |
| 1.5 | Create `EntityCore` facade (wraps CompositeBackend, adds validation) | `world_manager/entity_core.py` (new) |
| 1.6 | Old `entity_store.py` becomes thin wrapper calling `EntityCore` | `world_manager/entity_store.py` |
| 1.7 | Register characters in entity_store on first access (type="character") | `world_manager/character_file_backend.py` |
| 1.8 | `World.get_npcs()` delegates to entity_store | `world/state.py` |
| 1.9 | `World.get_locations()` delegates to entity_store | `world/state.py` |

### Phase 2 — Agent Lifecycle (close_invocation + News)
**Effort: 2-3 days**

| Step | What | Files |
|---|---|---|
| 2.1 | Implement `NewsQueue` class | `world_manager/news_queue.py` (new) |
| 2.2 | Implement `close_invocation` sentinel tool | `world_manager/lifecycle_tools.py` (new) |
| 2.3 | Implement orchestrator detection of `close_invocation` in ReAct loop | `gm/react_loop.py`, `world_manager/core.py` |
| 2.4 | Orchestrator saves summary to messages.json on close_invocation | `turn_runner.py` or `console_app.py` |
| 2.5 | Orchestrator appends news to higher agent's NewsQueue | `turn_runner.py` or `console_app.py` |
| 2.6 | Orchestrator builds [newspaper] from NewsQueue.consume() | `turn_runner.py` or `console_app.py` |
| 2.7 | Remove old termination tools (`invoke_self_gm`, `answer_lore_question_gm`) | `gm/game_master.py` |
| 2.8 | Migration table for GM: `invoke_self_gm` → `close_invocation` | docs |

### Phase 3 — ToolRegistry (Eliminate Duplication)
**Effort: 1 day**

| Step | What | Files |
|---|---|---|
| 3.1 | Implement `ToolRegistry` class — tools defined once | `world_manager/tool_registry.py` (new) |
| 3.2 | GM imports tools from `ToolRegistry.for_gm()` | `gm/game_master.py` |
| 3.3 | WM imports tools from `ToolRegistry.for_wm()` | `world_manager/core.py` |
| 3.4 | SM imports tools from `ToolRegistry.for_sm()` | `scene_manager/core.py` |
| 3.5 | Remove `world_manager/tools.py` (all tools moved to registry) | Delete |
| 3.6 | Remove `gm/tools.py` (tools defined in registry) | Delete |
| 3.7 | Remove `scene_manager/tools.py` (tools defined in registry) | Delete |

### Phase 4 — Script Engine
**Effort: 3-5 days**

| Step | What | Files |
|---|---|---|
| 4.1 | Implement `ScriptEngine` class: trigger evaluation, execution sandbox | `world_manager/script_engine.py` (new) |
| 4.2 | Inject tool API into script namespace (get_entity, query_entities, set_effect, etc.) | `world_manager/script_engine.py` |
| 4.3 | Implement `on_tick`, `on_stat_threshold`, `on_entity_enter`, `on_event_nearby`, `on_world_time`, `on_tag_added` triggers | `world_manager/script_engine.py` |
| 4.4 | Wire `ScriptEngine.tick()` into `EntityCore.tick()` | `world_manager/entity_core.py` |
| 4.5 | Wire `EntityCore.tick()` into orchestrator's tick loop | `turn_runner.py` |
| 4.6 | Execution timeout (100ms), no imports, no disk/network | `world_manager/script_engine.py` |
| 4.7 | Add `aura_radius` to `set_effect` (engine auto-generates script) | `world_manager/entity_store.py` |

### Phase 5 — Effect Engine
**Effort: 1-2 days**

| Step | What | Files |
|---|---|---|
| 5.1 | Implement `EffectEngine`: apply per_tick deltas, expire on duration | `world_manager/effect_engine.py` (new) |
| 5.2 | Wire `EffectEngine.tick()` into `EntityCore.tick()` | `world_manager/entity_core.py` |
| 5.3 | Effects with `aura_radius > 0` apply to all entities in radius, not to self | `world_manager/effect_engine.py` |
| 5.4 | Effect expiry writes to event_log with tag="effect_expired" | `world_manager/effect_engine.py` |

### Phase 6 — Pinned Context (Character Data from Entity Store)
**Effort: 1 day**

| Step | What | Files |
|---|---|---|
| 6.1 | `build_character_info_block` reads from entity_store via CompositeBackend | `world/context.py` |
| 6.2 | Add `sm_summaries` block to PinnedContext | `engine/pinned_context.py` |
| 6.3 | Add `wm_summaries` block to PinnedContext | `engine/pinned_context.py` |
| 6.4 | WM gets pinned context (currently has none) | `world_manager/core.py` |

### Phase 7 — Cleanup & Retirement
**Effort: 1 day**

| Step | What | Files |
|---|---|---|
| 7.1 | Remove old `update_entity_stat` (replaced by `adjust_entity`) | `scene_manager/tools.py` (if still exists) |
| 7.2 | Remove old `apply_effect` (replaced by `set_effect`) | `scene_manager/tools.py` (if still exists) |
| 7.3 | Archive old `game/world/npc.json` (read-only legacy) | — |
| 7.4 | Archive old `game/world/locations.json` (read-only legacy) | — |
| 7.5 | Remove `world/state.py` `create_npc`, `create_location`, `update_npc_json`, `update_location_json` (no callers) | `world/state.py` |

---

## §6 Dependency Graph

```
Phase 0 ──────► Phase 1 ──────► Phase 2 ──────► Phase 3
  │                │                │
  │                ▼                ▼
  └──────► Phase 4 ──────► Phase 5
              │
              ▼
         Phase 6 ──────► Phase 7
```

- Phase 0: No dependencies (bugfix + quick additions)
- Phase 1: Needs Phase 0 (entity_store extended)
- Phase 2: Needs Phase 1 (backends exist for close_invocation to target)
- Phase 3: Needs Phase 2 (agents migrated to close_invocation)
- Phase 4: Needs Phase 1 (backends exist), can run parallel to Phase 2-3
- Phase 5: Needs Phase 4 (effect engine uses script engine for aura_radius)
- Phase 6: Needs Phase 1 (backends exist)
- Phase 7: Needs Phase 1-6 (everything stable before cleanup)

---

## §7 Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Character files get out of sync with entity_store | Medium | High | CompositeBackend writes to BOTH on first migration, then character files become read-only cache; pinned context reads from entity_store |
| Script engine security (arbitrary Python in attach_script) | Low | Critical | No imports beyond stdlib, 100ms timeout, no disk/network, no set_entity/delete_entity in script namespace |
| LLM puts mechanical data in facts despite validation | High | Low | Warning on write, fact validation is heuristic; agent learns over time |
| ToolRegistry breaks existing agent calls | High | Medium | Phase 3 is last; Phase 0-2 keep existing imports. ToolRegistry is a refactor, not a redesign |
| Migration from old JSON files loses data | Low | High | Old files are archived, not deleted. Bridge delegates to old files if entity not found in entity_store |
| Character agent doesn't see entity_store updates | Medium | Medium | Character agent reads via World methods, which delegate to entity_store after Phase 1.8 |
