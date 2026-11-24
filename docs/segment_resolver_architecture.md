# Segment Resolver Architecture

*Intent Scheduling, Collision Detection, and Re-invocation*

---

## Scope

This document specifies the segment resolution layer: how declared intents from
multiple agents (player characters and NPCs) are collected, ordered on a shared
timeline, checked for structural matches, and resolved in segments. Agents whose
triggers fire are re-invoked with partial context.

This layer sits between character agent invocation and final narration. It
replaces the current flat "collect intents → SM narrates everything" approach.

---

## §1 Data Model

### 1.1 Structured Intent (matching §3 of scene_design.md)

Every `declare_intent` call produces:

```python
{
    "intent_id": str,             # auto-generated
    "actor": str,                 # character name
    "action_type": str,           # speech | movement | combat | cosmetic
    "target": str,                # entity name | "all" | "area" | "self"
    "description": str,           # narrative content
    "visibility": "public" | "private",
    "area": dict | null,          # extent, only when target is "area"
    "interval": [                 # [t_start, t_end] in world ticks
        int | null,               # t_start — null = "from now"
        int | null                # t_end — null = persistent subscription
    ],
    "trigger_condition": str | {  # the condition the actor waits for: a pattern
        "actor": str,             # for the structural join, or stated in words,
        "action_type": str,       # in which case nothing fires it by itself
        "target_match": str,
    } | null,
    "on_trigger": str,            # "interrupt_with_new_decision" | "apply_prepared_reaction"
    "prepared_reaction": dict | null,  # another intent
    "absorption": "alert" | "immersed",
}
```

### 1.2 Resolved Segment

Output of one segment resolution:

```python
{
    "t_start": int,               # segment start (world tick)
    "t_end": int,                 # segment end (world tick)
    "active_intents": [str],      # intent_ids active during this segment
    "triggered": [str],           # intent_ids that fired triggers
    "narrative": str,             # resolved outcome text
    "awareness": {                # per-character perception
        "Elena": "She sees Tiko approach the counter...",
        "Marek": "He hears Elena's voice from the bar...",
    },
}
```

### 1.3 Resolved History

An ordered list of `ResolvedSegment`, accumulated across turns. Serves as
input context for the next resolution cycle and for character re-invocation.

---

## §2 SM как первичный резолвер

SegmentResolver — **оптимизация**, не замена SM. SM остаётся главным резолвером.
Он просматривает все планы, разрешает их, и перевызывает персонажей если нужно.

### 2.1 Процесс

```
1. Все character agents объявили интенты
2. SM получает полный список интентов
3. SegmentResolver (оптимизация) предлагает:
   - Первую точку коллизии T_next
   - Пары структурных совпадений trigger_condition ↔ intent
4. SM просматривает предложения SegmentResolver:
   - ✅ Согласен → использует как основу
   - ❌ Видит ошибку (диалог адресован не туда, агенты накосячили)
      → сам решает как резолвить
5. SM принимает решение по каждому сегменту:
   - Кто что делает
   - Какие действия пересекаются
   - Как они резолвятся
6. Если есть неучтённые пересечения или нужно переспросить агента:
   → перевызывает character agents с частичным контекстом
7. Только когда SM удовлетворён:
   → финализирует ход (narrative block)
```

### 2.2 Ход не завершается пока SM не закончил

```
❌ НЕПРАВИЛЬНО (как сейчас):
   Characters act → SM narrates → end_turn

✅ ПРАВИЛЬНО:
   Characters act → SM reviews all plans → SM resolves →
   SM re-invokes agents if needed → SM resolves again →
   ... → SM satisfied → SM narrates → end_turn
```

### 2.3 Если агент ошибся — ничего не ломается

Пример:
```
Elena: declare_intent(speech -> "Marek", "Привет, Тихо!")
       ^^^^^  хотела сказать Tiko, но написала Marek

SM видит:
  Elena: speech -> Marek, "Привет, Тихо!"  ← противоречие
SegmentResolver предлагает: Marek отвечает
SM: ❌ Не может быть — текст обращения к Tiko, адресат Marek
SM решает: Elena обратилась к Tiko (по смыслу текста)
         Разрешает диалог между Elena и Tiko
```

SegmentResolver — это подсказка, не истина. SM всегда может перерешить.

### 2.4 SegmentResolver (оптимизация)

Автоматика находит первую коллизию и структурные совпадения,
чтобы SM не делал эту работу руками:

```
Input:  List[Intent] + current tick t_now
Output: {
    "t_next": int,                # первая точка коллизии
    "trigger_matches": [          # структурные совпадения
        {"trigger": intent_id, "matched": intent_id}
    ],
    "suggestion": str             # короткое объяснение для SM
}

1. T_next = find_earliest_collision(intents, t_now)
   ├─ earliest t_end of any bounded intent
   ├─ earliest t_start of any trigger_condition match
   │    (using IntentNormalizer to check matches)
   └─ OR t_now + default_horizon (if no collisions)

2. active = [i for i in intents if i.interval overlaps [t_now, T_next)]

3. triggers = structural_join(active)
   ├─ for each intent with trigger_condition:
   │   check if any OTHER intent in active matches
   │   {actor, action_type, target} == trigger_condition
   └─ returns matched pairs
```

---

## §3 IntentNormalizer

A narrow LLM call (single tool, no ReAct loop). Translates a character's
natural-language `trigger_condition` into structured form at the point the
actor formulates it.

```python
@tool
def normalize_trigger(
    raw_condition: str,
    actor: str,
    all_participants: list[str],
) -> dict:
    """Translate a natural-language trigger condition into structured form.

    Input:  "если кто-то заговорит со мной"
    Output: {"actor": "any", "action_type": "speech", "target_match": "Marek"}

    Input:  "когда эльфийка войдёт"
    Output: {"actor": "Seraphina", "action_type": "movement", "target_match": "any"}
    """
```

Called:
- Once per `declare_intent` that has a non-empty `trigger_condition`
- Result stored alongside the intent for mechanical matching

---

## §4 SM as Scheduler

The Scene Manager plays two roles:

### 4.1 Setting Horizon T

The reached implementation: the horizon of a cycle is **the nearest moment the
scene changes** — the closest declared start or end among the active intents
(`nearest_change` in `scheduler/timeline.py`, whose `.at` is what `next_boundary`
returns), and the structural pause (900s, `Orchestrator.STRUCTURAL_PAUSE_SECONDS`)
when only standing readinesses are left. What ends in that segment is resolved;
what runs longer is carried over with the span it declared, and the cycle that
follows begins where this one ended.

The same reading is what the phase in front of the resolution is given: the
moment the cycle stands at, the advance it will make if the review changes
nothing, and what that advance is estimated by (`.kind` — an attempt's end, its
beginning, one ending where another begins, the pause, or nothing under way at
all). The attempt whose own moment stands there is marked in the list of declared
intents. It is a default and is stated as one: the review itself may take an
attempt, change its span, cut it short or strike it out, and the advance follows
what the pool becomes, not what it was.

A cycle that reaches nowhere — nothing declared, nothing standing — moves the
clock nowhere. The mechanical "no progress" stride of 30s proposed in
`design/03_zero_progress_fallbacks.md` is gone: a stride nothing named would make
the stated default false, and the half of that design which gave the stride meaning
(escalating a stalled cycle to the scene manager) was never built.

The manager shapes that reach through the intents themselves — declaring,
truncating and cancelling them — rather than by naming a look-ahead point:

```
T = the nearest collision, or the structural pause when there is none
```

Not implemented: the manager's own T (`scene_design.md` §5), and passing it to
character agents to scope their `interval.t_start` within `[t_now, T]`. The
`horizon` field in `timeline.json` is left unwritten.

### 4.2 NPC/World Intents (§7)

SM authors intents for NPCs and world elements using the same schema.
These are posted alongside character intents.

Example:
```python
SM_intent = {
    "actor": "innkeeper",
    "action_type": "speech",
    "target": "Elena",
    "description": "Знаю я одну работу... караван в Лусхан ищет охрану",
    "interval": [t_now, t_now + 600],  # 10 minutes
}
```

---

## §5 Character Re-invocation Flow

Re-invocation is driven by **SM decision**, not by SegmentResolver.
SegmentResolver may suggest re-invocations, but SM decides.

### 5.1 When SM re-invokes

SM re-invokes a character when:

- **Trigger matched**: `trigger_condition` fired — character should see the result and react
- **Uncertain intent**: character's intent is ambiguous — SM needs clarification
- **New information**: something happened during resolution that the character couldn't have predicted
- **Missed interaction**: SM sees that character A addressed character B, but B didn't notice

### 5.2 What the character sees

Per `scene_design.md` §9.1:

- Its own active subscriptions (unchanged)
- The resolved segment history (new: what happened since last invocation)
- Horizon T (unchanged until SM resets it)
- Awareness-filtered: only events the character could perceive

### 5.3 Trigger context

The re-invoked agent receives a focused trigger message:

```
[trigger]
  What happened: Elena spoke to you
  Who: Elena Brightwood
  When: just now
  Where: at the bar
```

Not the full scene context — just the trigger event.

### 5.4 Agent decision

The agent decides:
- Continue with original plan → intent carries forward unchanged
- Modify plan → new declare_intent replaces old
- React specifically → declare_intent with response action

### 5.5 Re-invocation loop

```
SM not satisfied → re-invoke agent
  ↓
Agent produces new intents
  ↓
SegmentResolver updates collision proposals
  ↓
SM reviews + resolves
  ↓
SM satisfied? → NO → goto re-invoke
SM satisfied? → YES → produce narrative block → end turn
```

---

## §6 Turn Integration

```
run_turn():
  1. WM world setting (if needed)
  2. SM describe_scene:
     - Sets horizon T
     - Authored NPC intents
     - Per-character scene descriptions
  3. Character agents (each produces intents + decision):
     - Each receives: personalized context, T, resolved_history
     - Each produces: declare_intent (0..N) + commit_decision
  4. SM RESOLUTION LOOP:
     ┌─────────────────────────────────────────┐
     │  a. SM reviews all active intents       │
     │  b. SegmentResolver proposes:           │
     │     - earliest collision T_next         │
     │     - structural trigger matches        │
     │  c. SM accepts/modifies/rejects props   │
     │  d. SM resolves segment [t_now, T_next) │
     │  e. SM decides: re-invoke needed?       │
     │     YES → re-invoke agents with context │
     │           → agents produce new intents  │
     │           → goto a                      │
     │     NO  → continue                      │
     │  f. Accumulate resolved_history         │
     │  g. t_now = T_next                      │
     │  h. If active intents remain → goto a   │
     └─────────────────────────────────────────┘
  5. SM satisfied → produce narrative block
  6. Scene.end_with_gm_output()
```

---

## §7 Module Map

```
engine/
  segment_resolver.py        — SegmentResolver class
  intent_normalizer.py       — LLM-based trigger_condition translator
  resolved_history.py        — ResolvedHistory store (append-only)

character/
  agent.py                   — declare_intent → full schema + IntentNormalizer call

turn_runner.py               — Step 3→4→5 integration
```

---

## §8 Implementation Plan

| Фаза | Что | Статус |
|------|-----|--------|
| **1** | ~IntentNormalizer~ — условие остаётся лингвистическим, SM обрабатывает | Отменено |
| **2** | `declare_intent`: `trigger_condition`, `on_trigger`, `prepared_reaction` | ✅ Реализовано |
| **3** | SegmentResolver: find_earliest_collision (LLM-based) | ✅ SM `resolve_turn` |
| **4** | Character re-invocation: trigger context, partial context | ✅ `_run_single_character` + `gm_notice` |
| **5** | SM as scheduler: T, NPC intents | Частично: NPC-интенты ✅; собственный T не вводится — горизонт цикла это ближайшая граница (§4.1) |
| **6** | ResolvedHistory: аккумуляция между сегментами | ✅ Перенос интентов между сегментами и циклами (`carry_over_intents`, `timeline.json`) |
| **7** | Review объявленных интентов: id, отмена, прерывание | ✅ id при объявлении и в review; `manage_intent` снимает и режет, обрезанный интент зовёт владельца обратно |
| **8** | Интент с нулевой длительностью | ✅ Оседает в том сегменте, где сделан; пустая длительность у актёра = мгновенно, у стойкой готовности = до триггера |
