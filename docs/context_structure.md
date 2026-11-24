
# Context Architecture for Scene Manager (and similar agents)

## Problem

An LLM agent's context is everything sent to the model on each invocation.
In a multi-turn, multi-session game engine, different parts of this context
have very different lifetimes:

| Part | Lifetime | Must survive |
|------|----------|:---:|
| System prompt | Never changes | ✅ forever |
| World setting, character descriptions | Rarely changes | ✅ across sessions |
| Pinned summaries (arc/paragraph) | Changes only on trim | ✅ across sessions |
| Messages.json history | Grows between sessions | ✅ across sessions |
| Current session's tool calls, retries, raw LLM output | Ephemeral | ❌ discard after session |

The original bug was treating all of these as one flat list (`_session_messages`).
When `end_session()` cleared it, everything was lost — including the system prompt
and messages.json history. The next `begin_session()` had to rebuild everything
from scratch, defeating prefix caching and losing the persistent record.

## Solution: four layers with explicit lifetimes

```
Context sent to LLM = [
    Layer 1a: _immutable_prefix      ← SystemPrompt(prompt.txt)
                                       Built ONCE in __init__.
                                       Never touched again.

    Layer 1b: _pinned_summaries()    ← ## World Setting
                                       ## Campaign Summaries | ## Scene Summaries
                                       | ## World Summaries
                                       ## Character — Elena
                                       Built via PinnedBlockCache: one
                                       process-wide instance, entries keyed
                                       by block name + source path.
                                       Rebuilt only when the source file's
                                       mtime changes (i.e. a summary was
                                       written). A messages.json trim never
                                       touches it.

    ===== messages.json ============================================

    Layer 2: _persistent_history     ← [session_begin] session 1
                                       [entity] adjust_entity ...
                                       [session_end] ...
                                       [session_begin] session 2
                                       ...
                                       Stored on disk in messages.json.
                                       Loaded into memory at __init__
                                       and reloaded at end_session().
                                       Grows slowly — one pair of messages
                                       ([session_begin] + [session_end])
                                       per session, plus occasional
                                       [entity] records.

    ===== session buffer (RAM only) ================================

    Layer 3: _hot_buffer              ← HumanMessage("Session ended...")
                                       AIMessage("Let me check...")
                                       ToolMessage({...})
                                       AIMessage("", tool_calls=[...])
                                       ...
                                       Appended during the current session.
                                       Cleared at session boundary.
                                       NEVER written to disk.
]
```

### Key principle

`messages.json` **is** the agent's message history — the same as in any classical
LLM application. The session buffer is simply "not yet written to messages.json",
temporary RAM. The name "session messages" is just an alias for "everything after
messages.json in the context".

## Lifecycle

### `__init__()` — once, at agent creation

```python
self._immutable_prefix = [SystemMessage(content=prompt_text)]
self._persistent_history = load_history(messages_json_path)
self._hot_buffer = []
self._llm = build_llm(...)  # created once for prefix caching
```

### `begin_session()` — called once per session

```python
self._hot_buffer.clear()
# do NOT touch _immutable_prefix or _persistent_history
```

### During session (repeated LLM invocations)

```python
self._hot_buffer.append(HumanMessage(content=...))
context = self._assemble_context()  # Layers 1a + 1b + 2 + 3
out = llm.invoke(context)
self._hot_buffer.append(AIMessage(content=...))
self._hot_buffer.append(ToolMessage(content=...))

# If the tool changed entity state, immediately save to messages.json
if tool_name in ENTITY_CHANGING_TOOLS:
    append_message(messages_json, role="user", content=entity_record)
```

### `end_session()` — called once after session

```python
# Save useful info to messages.json (on disk)
append_message(messages_json, role="user", content=session_begin_msg)
append_message(messages_json, role="assistant", content=session_end_msg)

# Reload persistent history from disk (picks up new messages)
self._persistent_history = load_history(messages_json)

# Clear session buffer — Layer 3 is ephemeral
self._hot_buffer.clear()
```

## Messages.json content

messages.json is the agent's long-term memory. It stores structured records,
not raw LLM output.

Every entry is a record the framework authored, and every record is written with
the `user` role — including what the agent reasoned (`[reasoning]`) and what it
wrote through tools. The `assistant` role is reserved for turns the model itself
takes in a live conversation, so a stored entry never masquerades as one. Stored
entries are read back as records whatever role an older file used.

### `[session_begin]` — User message, written at `begin_session()`

```
[session_begin]
[Locations and entities near the active players]
inn_sleeping_dragon
  Elena Brightwood [player]
  Marek Stonehand [player]
  Tiko Swiftfoot [player]
  Hargrim Ironboot [character]
tower_district_study
  Seraphina Windmere [player]
```

- States the scene: every place a player stands in, and what stands in each of
  them, each thing named by its id and by what it is in play — one of the players,
  or the world's own word for it.
- This is the only statement of it in the session. No phase restates it: the
  record is in front of the agent at every invocation of the session already.
- Nothing about intents: what is still running is state, stated where it is
  decided on — the review of declared intents at each cycle — and, as what the
  previous session left standing, in that session's own account at its close.
- `session:` is gone: the count of sessions is the framework's bookkeeping and
  lives in its metadata, not in the record. `time:` is written where a time is
  needed — the closing record carries the world time the session ended at.

### `[entity]` — User message, written inline during session

```
[entity] adjust_entity
  entity_id: Elena Brightwood
  current_state: Сидит за столом в углу, доедает ужин
```

Written immediately after each successful entity-changing tool call
(`adjust_entity`, `create_event`, `set_effect`, `remove_effect`,
`declare_intent`, `modify_intent`).

NOT written for read-only tools (`get_entity_facts`, `query_events`, etc.)
or escalation tools (`escalate_to_gm`, `answer_player_question`).

### `[accepted]` and `[exchange]` — User messages

`[accepted]` is entered where it stands: an attempt when the review reads it, and
what it came to when the round reaches its end. The attempt's text stands once —
where it was read, under the id it acts by — and its end names it by that same id
and carries only what the round added: that it reached its end, and who could see
it. Written out again where it resolved, the attempt would read as if it happened
then. Nothing is gathered at the close: a group written there would land after
everything that happened later.
`[exchange]` is entered where the question was asked — the file is still written
once, at the close, but a record left until then would land after everything that
happened later, and the memory would tell its reader something untrue about when
things were said.

```
[accepted] [6ac91d52afc2] Elena Brightwood: movement → хозяин за стойкой
  Елена не спеша поднимается из-за стола и идёт к стойке.
  timing: in progress (+0s elapsed, 60s remaining)

[accepted] [6ac91d52afc2] — resolved
  perceived by: Marek Stonehand, Tiko Swiftfoot

[accepted] [c7f0a4e91b23] Seraphina Windmere: investigation → singing stones
  Серафина раскладывает записи о камнях.
  timing: in progress (+1200s elapsed, 6000s remaining)

[exchange:character → scene_manager]
  question: who is the man with the silver disc
  answer: a pilgrim, by his dress, and not from this town

[exchange:scene_manager → game_master]
  question: does the temple hire outside the walls
  answer: yes, by the week, and it is not spoken of
```

- `[accepted]` — what the session took: an attempt as the review read it, whole
  and under the id it acts by; and, where the round reached its end, that same id
  with those who could see it. An attempt the review cancelled is not in it, and
  the attempt's own words stand once.
- `[exchange]` — the questions and answers between agents, both directions.

### `[session_end]` — User message, written at `end_session()`

```
[session_end]
time: Y1492-03-15 18:45:30

Elena подошла к стойке и спросила о работе за стенами...
```

- Carries the world time the session ended at, and the session's own account of
  what happened in it: what stood unresolved at its start, and what stands now.
- The session's records are entered as the session lives them; the one merge at
  its close is what puts any of them on disk.

## In-session data (Layer 3)

Layer 3 carries every message exchanged during the current session. It has
exactly three forms, and a value produced by the agent is never carried as a
fourth one.

| Form | Role | Shape | Purpose |
|------|------|-------|---------|
| `directive` | user | first line `[kind]`, body is data | framework → agent: what the agent is asked, told, or notified. The two statements made while a cycle runs (`Player intents changed…` and a re-stated advance) are directives too; their first line is the sentence that says what changed, because they are read as text rather than parsed |
| `traffic` | assistant + tool | `AIMessage(content="", tool_calls=[...])` + `ToolMessage` | a call and its result |
| `snapshot` | user | first line `[kind]`, body is data about the agent itself | `[thoughts]`, `[disposition]` |

Three rules follow:

1. **A question is never committed together with its answer.** A request is
   either transient (`context + [HumanMessage(request)]`, never appended) or
   committed as a `directive`; the value it produced is recorded separately.
2. **A value the agent produced is never an assistant turn.** It is either the
   `ToolMessage` of its own call (`traffic`) or a framework-authored record
   (`snapshot`). Assistant content is empty wherever the message carries calls.
3. **Reinvoke appends nothing.** The transcript already holds the directive and
   the record; re-entering a turn adds only the new turn context.

### Direct questions

Some values cannot be delivered by a tool — they are produced by asking the
model a direct question (`tool_choice="none"`) and reading its text. Such a
value is recorded as a single block that states what it is; the question that
produced it is passed to the model transiently and never committed:

```
[disposition]
Your standing psychological baseline:
<the value produced>
```

The baseline is asked at the opening of every cycle in which the character is
called to declare an intent, and what he states is entered in his own memory
where he stated it: his history keeps how he stood at each opening. A character
called back to decide again within the same cycle continues the cycle he is in
and is not asked a second time — the opening stands for the cycle it opened.

The value is never committed as an `AIMessage`, and an instruction that has
already been carried out is never left in the transcript — a model reads an
instruction as pending and starts acting on it again.

### Reasoning

A reasoning-capable backend answers through two channels: the model's own reasoning
and the deliberate answer. The reasoning is a property of the message, not a summary
of it — the backend requires it back on every request that continues that turn, and
rejects the request otherwise. So wherever a message is kept, its reasoning is kept
with it, in the field the provider expects.

| Agent | Reasoning | Where it lives |
|-------|-----------|----------------|
| Character | off — a character acts; adjudicating its own actions is not its part | — |
| Scene Manager | on for its own work, off in the one phase that forces a call | session buffer only |
| Game Master | on | `[reasoning]` records in its memory |
| World Manager | on | `[reasoning]` records in its memory |

Reasoning is not entered as a separate `[thoughts]` message: it already travels inside
the response. Those blocks remain behind `LLM_WORLD_THOUGHTS_BLOCKS=1`, for inspecting
what an agent deliberates about.

## Why this design

1. **Prefix caching.** Layers 1a and 1b never change between invocations.
   The LLM provider caches their tokens. Only Layer 3 (hot buffer, small)
   changes between turns. This makes repeated invocations cheap.

2. **Persistent memory survives.** messages.json grows slowly — one
   `[session_begin]` + `[session_end]` pair per session, plus occasional
   `[entity]` records. No tool call noise, no retries, no raw JSON.

3. **Session buffer is truly ephemeral.** All the garbage (retry loops,
   rejected tool calls, raw entity dumps) stays in RAM and is discarded
   at `end_session()`. It never pollutes messages.json.

4. **Summarization works.** The paragraph summarizer reads ALL of
   messages.json. With structured `[session_begin]` / `[session_end]`
   markers and session numbering, it can find the exact range to summarize
   ("summarize from session 32 to session 42") without overlap.

5. **Entity tracking is complete.** `[entity]` records are written inline
   during the session, so nothing is lost even if the session crashes
   before `end_session()`.

## Invocation flow (per session)

```
1. begin_session()
   → hot_buffer.clear()
   → write [session_begin] to messages.json
   → reload _persistent_history

2. For each LLM call (possibly multiple per turn):
   → self._hot_buffer.append(HumanMessage(...))
   → context = self._assemble_context()
   → llm.invoke(context)
   → self._hot_buffer.append(AIMessage(...))
   → self._hot_buffer.append(ToolMessage(...))
   → if tool is entity-changing: enter [entity] in the session's branch
     (in the order it was lived; the one merge at the close writes the file)

3. end_session()
   → write [session_end] to messages.json
   → reload _persistent_history
   → hot_buffer.clear()
```

## Что изменилось (было → стало)

| Было | Стало | Причина |
|------|-------|---------|
| `_session_messages` — один список для всего | Три отдельных слоя | Разные lifetimes |
| `_get_session_llm()` — новый LLM каждый `begin_session` | `_get_llm()` — один LLM на всё время | Prefix caching |
| `begin_session()` пересоздавала SystemPrompt + pinned | Только чистит `_hot_buffer` | Не сбрасывать кэш |
| `end_session()` убивала `_session_messages = []` | Сохраняет полезное, чистит только `_hot_buffer` | Не терять Layers 1-2 |
| `"Turn \| location: ..."` формат | `[session_begin]` / `[session_end]` | Структурированные данные |
| messages.json не загружался в `begin_session()` | `_persistent_history = load_history()` | Persistent memory |
| Двойной `end_session()` в `run_full_session` | Только один вызов | Дублирование нарратива | 

