# Scene Design

*Turn Resolution, Awareness, Intent Scheduling, and Session Lifecycle*

---

## Scope

This document specifies the scene-layer orchestration: how character agents and the
scene manager are invoked, how their declared actions are resolved, what each agent
sees at each invocation, and how sessions begin and end. It governs one location or
a connected cluster of locations, seconds to hours of in-world time.

---

## §1 Shared Timeline

All actors operate on one continuous timeline. There is no per-character time
offset. A declared intent's duration is unrestricted — an intent may last seconds
or hours. Asynchrony is expressed entirely through the scope of declared time
intervals on this shared timeline.

The timeline never rewinds: once a point has been resolved, intents and their
outcomes at that point are fixed. New declarations apply only from the current
resolution point forward.

Resolution proceeds in **cycles**. Within one cycle:

1. All eligible character agents are invoked (see §5).
2. The scene manager is invoked on its resolve cycle — reviews all intents with
   status markers, may declare NPC intents, modify player intents, or proceed
   without changes (see §7).
3. The segment scheduler resolves all currently active intents against each other
   (see §4). SM-authored intents from step 2 are included in the pool.
4. A cycle may trigger re-invocation of specific characters whose subscriptions
   matched or whose intent was interrupted. These re-invocations happen within the
   same cycle.
5. If no intents resolve and no re-invocations fire, the cycle ends silently —
   no LLM call, no narration. The timeline advances to the next segment boundary.

Multiple cycles form a **session** (see §9). A session has a definite beginning
and end, and produces durable records in the agent's persistent message history.

---

## §2 Awareness

Awareness determines what each character agent is shown and which events they may
react to.

- **Base awareness**: derived from location containment plus the connectivity graph.
  A character is aware of events in their own location and, to a degree set by
  connectivity and line-of-sight, in connected locations.
- **Extended awareness**: explicit sensor links (scrying, familiar's senses)
  registered as a standing fact about the entity. Checked the same way as base
  awareness once registered.

The scene manager's view is not filtered by awareness — it has full access to all
active intents and the full resolved history (see §7).

Awareness is the model; what the model produces is **knowledge**. A piece of
knowledge is something that happened, was declared, was asked and answered, or was
perceived, entered once under an id of its own (`knowledge1`, `knowledge2`, …) in
the scene manager's ledger, and held by the characters who know it. Knowledge is
only ever added: a character who was told something is not un-told it. The ledger's
term is the only one used — in the tools, in the line the scene manager reads, and
on the framework's side — so a session manager, a framework author and an agent
all name the same thing the same way.

An attempt is a piece from the moment it stands, not from the moment it resolves:
its id is listed beside the attempt in the resolve phase, and its perceivers are
recorded with it when the attempt resolves. Recorded, not told: what reaches a
character reaches him through the scene manager, which hands a piece on by naming
its id (`add_perception`, `add_knowledge`) — which is why the id has to stand where
the scene manager reads the attempt.

---

## §3 Intent Schema

All declarations — immediate actions, waits, standing reactions, and scene manager
NPC actions — use a single schema with the following fields:

- **actor**: entity identifier of the acting entity.
- **action_type**: one of speech, movement, combat, cosmetic, or an open value.
  Cosmetic may not target an area.
- **target**: character name, "all", "area", or "self".
- **area**: area-of-effect data; only valid when target is "area".
- **interval**: time range with a start and optional end. A null end marks a
  persistent subscription that remains active until explicitly cancelled.
- **visibility**: public or private. Private actions undergo a perception gate
  (see §4 step 3).
- **absorption**: alert or immersed. Determines how available the character is to
  external events while this intent is active (see §3.6).
- **trigger_condition**: the condition, in words, under which the attempt comes to
  pass instead of running now. It is read against what happens; nothing fires it
  by itself, so a standing readiness is judged by whoever reads it.
- **on_trigger**: what happens when the condition is met — either interrupt the
  actor for a new decision, or apply a prepared reaction.
- **prepared_reaction**: a pre-written sub-intent that replaces the current one
  when the condition is met.
- **description**: narrative prose of the intended action.

Not part of a declaration, and no longer carried by the resolver: `is_hostile`,
`detection_difficulty`, `trigger_priority` and `on_timeout` were dropped from both
the character's and the scene manager's `declare_intent`, and from the intent the
scheduler holds. The gate for a private action judges it from the scene, the
several matches of one segment fire in the order the intents stand, and an intent
whose span ends without a match completes as declared.

### §3.1 Status lifecycle

Every intent passes through three statuses visible to the scene manager:

- **new**: first appeared in this cycle. The scene manager sees it for the first
  time.
- **active**: was already present in the previous cycle and remains unresolved.
  The scene manager has already seen it.
- **completed**: was resolved within this cycle. The intent is no longer in the
  active set. The scene manager sees it as completed and will not see it again
  in subsequent cycles.

The scene manager receives a full list of all intents on each cycle, each annotated
with its current status, visibility, and absorption level. This replaces blind
snapshots where the manager could not distinguish new intents from carry-over ones
or know which intents had been resolved between cycles.

### §3.2 Persistent Subscriptions

An intent with no end time marks a persistent subscription — active across
segments and across published blocks until explicitly cancelled. It has no timeout.
Checked at resolution exactly like a bounded intent.

A character agent may cancel any of its own active subscriptions at any invocation.

### §3.3 Visibility and the Perception Gate

Public visibility requires no further check beyond structural matching.
Private visibility triggers a perception gate: the concealed action is put to the
scene manager against the scene, and an observer who passes receives information
about it. Nothing about the concealment carries a number.

### §3.4 Several Matches in One Segment

When several waiting intents match what happens in the same segment, they fire in
the order the intents stand. There is no declared precedence.

### §3.5 Pending Reactions

A wait for a reply and a standby reaction are the same object: a trigger_condition
plus the way it is answered — a prepared reaction, or an interruption for a new
decision. An actor states the condition once; it is read at every subsequent
segment until it matches, is cancelled, or its span ends.

### §3.6 Absorption

Absorption states how available a character is to the world beyond their own
declared trigger_condition, for the duration of this intent. The character sets
this themselves.

- **alert** (default): the character retains background awareness. They remain a
  candidate for interruption based on significant events within their awareness,
  even without a matching trigger_condition.
- **immersed**: the character has committed full attention to this action. They are
  not re-invoked by ordinary events. Interruption only for direct physical threat
  to the character themself.

Absorption applies to the whole declared interval. A character who wants to spend
part of a long action alert and part immersed declares separate sequential intents.

---

## §4 Segment Resolution

The scheduler processes the active set of intents in segments. A segment boundary
is the nearest point where any interval starts, ends, or a trigger_condition
becomes checkable against newly-active intents.

For each segment:

1. Collect all intents active or newly-active in this segment, including persistent
   subscriptions.
2. Structural join: for each intent with a trigger_condition, check whether any
   other active or newly-active intent's actor, action_type, and target match the
   condition.
3. Perception gate: for matches where the matched intent's visibility is private,
   resolve via the scene manager. Matches on public intents pass automatically.
4. For matches that passed step 3, apply them in the order the intents stand:
   prepared reactions replace the intent; interrupt-with-new-decision re-invokes
   the actor from this point forward.
5. For bounded intents whose interval ends this segment without a trigger match:
   the intent completes as declared.
6. Record the segment's resolved state — actor, action, and perceivers — into the
   resolved history and the publication buffer (see §6).

The scene manager is invoked once per cycle, not per segment. Steps 1, 2, 4, 5, 6
are structural; step 3 is the only step that requires the scene manager per-match.
---

## §5 Invocation Eligibility

### Character agents

A character agent is invoked when:

- They have no currently active intent (their previous interval just ended, or they
  are newly present in the scene). They declare their next intent.
- An "alert" character at the same location as another character who just declared
  a new active intent is then invoked to reconsider — they may react or confirm
  their existing intent continues unchanged.
- If no character at a location currently has zero active intents — everyone
  present already has something ongoing declared — no "alert" character at that
  location is invoked.
- "Immersed" characters are never invoked by this condition. They are only
  re-invoked by their own trigger_condition match or by explicit interruption
  (see §7.1).

This condition is evaluated independently per location.

### Scene manager

The scene manager is invoked unconditionally on every cycle, regardless of whether
any character was invoked. Its invocation is not driven by the eligibility
conditions above — it is a structural participant. See §7.

---

## §6 Session Buffer and Session Summary

Resolved segments accumulate in a **session buffer** — a running record of what
happened over all cycles in the current session. This buffer is written to
persistent history only at session end.

### §6.1 What goes into the buffer

Only **resolved segment entries** go into the buffer. Each entry records:

- Which actor performed what action
- What action_type and visibility
- The narrative description of what occurred
- Which observers perceived it

The following do **not** go into the buffer:

- Player observe or recall queries and their answers. These are already part of
  the scene manager's persistent message history and would duplicate information
  the manager already holds (see §1.3 of general requirements — no duplication).
- Tool calls, retries, or intermediate reasoning steps.

### §6.2 Deduplication

If the same resolved segment appears across multiple consecutive cycles (which can
happen when a cycle ends without advancing the timeline), the buffer contains each
segment only once. Duplicate entries are detected by matching actor, action_type,
and description text, and collapsed into a single entry.

### §6.3 Session summary at end

At session end, the scene manager does NOT produce a narrative block. Instead, it
outputs a **constrained per-character session summary** — one structured line per
player character describing their state, discoveries, and disposition at session
close.

The summary follows a fixed schema, not free prose:

```
character: {name}
  disposition: {one sentence — emotional/psychological state at session end}
  knowledge: {one sentence — what this character learned or suspects}
  intent: {one sentence — what this character is positioned to do next}
```

Each field is validated for presence and length before the session record is
written. Unparseable or missing fields cause a retry with rejection, identical to
the resolve cycle validation. The model may not add extra fields or free text.

This constrained format:
- Eliminates the model's ability to write unsolicited narrative (the root cause of
  publish_narrative_block failures).
- Produces machine-parseable records that tools and character agents can consume.
- Keeps the persistent history sparse — no raw narrative text survives to messages.json.

### §6.4 Session safety cap

Independent of the session buffer, the entire session context (including all
tool calls, reasoning, and exchanges) is bounded by a session safety cap of
approximately 15,000–20,000 tokens. If this cap is reached, the manager is forced
to end the session regardless. This is a protective fallback, not part of the
ordinary pacing mechanism.

---

## §7 Scene Manager as Participant

The scene manager is an actor on the same scheduler as character agents, with two
distinctions:

- Its view of intents and resolved history is not filtered by awareness. It sees
  everything.
- Its intents may be authored on behalf of any NPC or world element in the scene,
  not a single character.

The manager is invoked:

| Phase | Count per session | Purpose |
|-------|-------------------|---------|
| Bootstrap | 1 per run | Provide initial knowledge to all characters at the start of the run's first session. Later sessions are informed by other channels (knowledge the scene manager states in a resolve cycle, question answering, each character's own scene context); the earlier per-character conditions — changed place, inactive for long — were dropped deliberately |
| Resolve cycle | N (one per cycle) | Review all intents with status markers, declare NPC intents, modify or correct player intents, then close invocation |
| Player query | M (one per observe/recall) | Answer character perception and memory questions |
| Session summary | 1 | Output constrained per-character session summary (see §6.3) |

On each resolve cycle, the manager receives:

- All active intents from all participants, unfiltered, each annotated with status
  (new, active, or completed), visibility, and absorption level.
- Resolved segments produced since the last cycle (deduplicated).

The manager may, on each resolve cycle:

- **declare_intent** — author new intents on behalf of any NPC or world element.
- **modify_intent** — adjust an existing intent (change target, trigger condition,
  etc.) belonging to any actor in the scene.
- **remove_intent** — cancel an active intent (e.g., because a character was
  interrupted by a significant event).
- **proceed** — call close_invocation without changes when no intervention is
  needed.

The manager may NOT write free text, produce narrative, or call any tool that
writes to persistent history during the resolve cycle. All gameplay edits are
performed through intent tools. The resolve cycle always ends with
close_invocation.

### §7.1 Intent interruption

The manager may interrupt a character's active intent unilaterally:

- Against "alert" characters: for any significant event within their awareness.
- Against "immersed" characters: only for a direct physical threat to that
  character specifically.

The interruption must reference a specific resolved event that justifies it. The
portion of the intent already resolved stands; from the interruption point forward,
the character is re-invoked with the interrupting information.

---

## §8 Scene Manager Resolution Calls

Outside of its cycle role, the manager handles two narrow structural queries:

- **Perception gate**: whether an observer notices a private action in this scene.
- **Schema violation fallback**: when an intent's description implies effects
  outside its action_type's permitted fields.

Both are structured queries against specific fields, not open narrative judgments.

---

## §9 Session Lifecycle

A session is the span between begin_session and end_session. It encompasses one or
more cycles.

### §9.1 begin_session

At session start:

- The hot buffer (in-memory working area for the session) is cleared.
- A structured record is written to the agent's persistent message history
  containing: session number, world time, all players with their positions, all
  NPCs that participated previously, and any intents that survived the previous
  session.
- The persistent message history is reloaded to include this new record.

### §9.2 During session

All of the manager's tool calls, reasoning, player queries, and intermediate cycles
accumulate in the hot buffer. Nothing is written to persistent message history
except entity-changing tool results (which are saved immediately to ensure they
survive a crash). The persistent history is not updated until session end.

### §9.3 end_session

At session end:

- The manager receives a session summary request using the accumulated session
  context, not rebuilt from storage. This preserves intents from re-invoke cycles
  that would otherwise be lost.
- The manager outputs a constrained per-character summary (see §6.3) — one
  structured line per player describing disposition, knowledge gained, and
  positioned intent. This is NOT a narrative block. Validated for schema
  compliance before the session record is written.
- A concluding record is written to persistent message history containing: world
  time, all players with their final positions, all intents that resolved within
  the session, all intents that persist beyond the session, all entities referenced
  during the session, and the per-character summaries.
- The hot buffer is cleared.
- The persistent message history is reloaded to include the new records.

### §9.4 Context layers

The agent's full context is assembled from four layers:

1. Immutable system prompt: built once at agent creation, never modified.
2. Pinned summaries and character descriptions: rebuilt only when the message
   history is trimmed.
3. Persistent message history (messages.json): loaded from disk, grows between
   sessions, never includes raw tool calls or retries.
4. Hot buffer: in-memory, accumulates during the current session, cleared at
   session end. Contains everything the agent generated during the session —
   tool calls, reasoning, retries, raw responses — none of which survives to
   the persistent record.

