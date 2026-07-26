# Scene Design

*Turn Resolution, Awareness, and Intent Scheduling*

---

## Scope

This document specifies the scene/turn orchestration layer: how character agents
(player characters and NPCs) and the scene manager are invoked, how their declared
actions are resolved against each other, and what each agent sees at each
invocation. It sits above the shared entity store and event log described in
[`architecture.md`](architecture.md) and governs scene-scale interaction — one
location or a connected cluster of locations, seconds to hours of in-world time.

---

## §1 Shared Timeline

All characters and the scene manager operate on one continuous timeline. There is no
per-character time offset. A declared action's duration (`interval.t_end`) is
unrestricted — a character may declare an action lasting seconds or hours.
Asynchrony is expressed entirely through the *scope* of declared intervals on this
shared timeline.

The timeline never rewinds: once a point on the timeline has been resolved, intents
and their outcomes at that point are fixed. New declarations from any actor apply
only from the current resolution point forward.

**There is no turn boundary and no fixed order of declaration.** Nothing in this
design assigns characters an initiative order, a speaking order, or any sequence in
which they must declare intents relative to one another. Resolution outcome is
determined entirely by declared `interval` values, `trigger_condition` matches, and
`trigger_priority` — never by which character's tool call happened to return first.
Where invocation order does matter (§5), it is driven by a structural condition on
who currently has an active intent, not by a fixed sequence assigned to characters.

**There is no planning horizon set in advance.** Resolution proceeds continuously,
segment by segment (§4), without a manager-declared point up to which the scheduler
commits to resolving before reconsidering. What was previously handled by such a
horizon — carrying forward intents that outlast the current attention, deciding when
to reconsider the overall picture — is handled instead by continuous segment
resolution (§4) plus the publication mechanism (§6), which are driven by accumulated
content, not by a pre-declared span of world-time.

## §2 Awareness

Awareness determines what each character agent is shown and which events they may
react to.

- **Base awareness**: derived from `location_inside` plus the location/feature
  connectivity graph (`connected_to`, and — where a GM has populated interior
  geometry for the current location — `connections[].blocks_line_of_sight`). A
  character is aware of events in their own location and, to a degree set by
  connectivity and line-of-sight, in connected locations.
- **Extended awareness**: explicit sensor links (scrying, a security device, a
  familiar's senses) registered as a standing fact about the character/entity.
  Checked the same way as base awareness once registered; not recomputed per
  segment.

The scene manager's view is not filtered by awareness — it has access to the full
resolved history and the full set of active intents/subscriptions for all
participants (§7).

## §3 Intent and Trigger Schema

All declarations — immediate actions, waits, standing reactions, and the scene
manager's own world/NPC actions — use one schema:

```
{
  actor: str,
  action_type: "speech" | "movement" | "combat" | "cosmetic" | (open),
  target: str,                 # character name | "all" | "area" | "self"
  area: { ... } | null,        # only valid if target == "area"
  interval: [t_start, t_end | null],  # null t_end = persistent subscription
  is_hostile: bool,            # default false; only valid for action_type == "combat"
  description: str,

  absorption: "immersed" | "alert",  # default "alert" — see §3.6

  visibility: "public" | "private",  # default "public"
  detection_difficulty: int | null,  # required if visibility == "private" and
                                      # this intent has a mechanical effect

  trigger_condition: {
    actor: str | "any",
    action_type: str | "any",
    target_match: str
  } | null,

  trigger_priority: int,        # default 0; lower resolves first on multi-match

  on_trigger: "interrupt_with_new_decision" | "apply_prepared_reaction",
  prepared_reaction: { ... } | null,

  on_timeout: { ... } | null    # not applicable to persistent subscriptions
}
```

### §3.1 `action_type` Field Constraints

- `cosmetic`: may not set `is_hostile: true`, `target: "area"`, `area`, or any
  combat-effect fields, regardless of `visibility`.
- `combat`: may set `is_hostile`, `area`, damage-related fields. If
  `visibility == "private"`, `detection_difficulty` is required.
- `speech`: `target` is the addressee (`"all"` for public statements); `area` not
  applicable.
- `movement`: `target` may be a location/feature reference; `area` not applicable.

An intent whose `description` implies effects outside its `action_type`'s permitted
fields is a schema violation, flagged at resolution.

### §3.2 Persistent Subscriptions

`interval: [t, null]` marks a persistent subscription — active across segments and
across published blocks until explicitly cancelled. Has no `on_timeout`. Checked at
§4 step 2 exactly like a bounded-interval intent.

A character agent may cancel any of its own active subscriptions at any invocation by
returning the subscription's id in `cancel_subscription_ids`.

### §3.3 Visibility and the Perception Gate

- `"public"` (default): structural match (§4 step 2) is sufficient for any observer
  with awareness to perceive the action. No further check.
- `"private"`: a deliberate concealment attempt. Step 3 (perception gate) is invoked
  only for observers whose own triggers structurally matched this intent, comparing
  the observer's relevant skill against `detection_difficulty`. Currently resolved
  by the scene manager (§8); a mechanical replacement is future work (§10, §11).

### §3.4 Trigger Priority

When multiple intents' `trigger_condition`s match the same event in the same
segment, §4 step 4 applies them in ascending `trigger_priority` order.

### §3.5 Pending Reactions

A wait for a reply and a standby reaction to an observed event are the same object:
`trigger_condition` + `on_timeout` (bounded) or a persistent subscription (§3.2,
unbounded). An actor states the condition once; it is checked mechanically at every
subsequent segment until it matches, is cancelled, or (for bounded intents) times
out.

### §3.6 Absorption

`absorption` states how available a character is to the world beyond their own
declared `trigger_condition`, for the duration of this intent. The character sets
this themselves — it is not inferred by the scene manager.

- **`"alert"`** (default): the character is doing this, but retains background
  awareness of their surroundings. They remain a candidate for `interrupt_intent`
  (§7.1) on the basis of significant events within their awareness, even without a
  matching `trigger_condition`. Subject to the invocation condition in §5.

- **`"immersed"`**: the character has deliberately committed their full attention to
  this action. They are not re-invoked by ordinary events in their awareness, and are
  not a candidate for `interrupt_intent` except where the interrupting cause is a
  direct physical threat to the character themself — not merely something happening
  nearby to someone else. A character who declares `absorption: "immersed"` while
  reading for eight hours will not notice a distant commotion; they will notice their
  own door being broken down. This is a deliberate in-fiction choice with a real
  narrative cost, not a system default to route around.

`absorption` applies to the whole declared interval. A character who wants to spend
part of a long action alert and part of it immersed declares this as separate
sequential intents rather than a single interval with a changing value.

## §4 Segment Resolution

The scheduler processes the timeline in segments. A segment boundary is the nearest
point where any interval starts, ends, or a `trigger_condition` becomes checkable
against newly-active intents.

For each segment:

1. Collect all intents active or newly-active in this segment, including persistent
   subscriptions.
2. **Structural join**: for each intent with a `trigger_condition`, check whether any
   other active/newly-active intent's `{actor, action_type, target}` matches the
   condition.
3. **Perception gate**: for matches where the matched intent's `visibility ==
   "private"`, resolve via §8. Matches on `"public"` intents pass automatically.
4. For matches that passed step 3, sort by `trigger_priority` (ascending) and apply:
   - `prepared_reaction` present → apply mechanically, posting its `interval` as a
     new scheduler entry starting at this segment's start.
   - `on_trigger == "interrupt_with_new_decision"` and no prepared reaction → the
     actor is re-invoked from this segment's start time forward, given only the
     information that triggered the match.
   - Persistent subscriptions remain active after firing.
5. For bounded-interval intents whose `interval` ends this segment without a
   matched trigger → apply `on_timeout` if present; otherwise the intent completes
   as declared. If the actor has nothing further declared, they become eligible for
   re-invocation under §5.
6. Record the segment's resolved state — actor, action, and what each aware
   character perceived (§2) — into the resolved history, and add its content to the
   publication buffer (§6).

Natural-language `trigger_condition`s are translated to the structured form once, at
the point the actor formulates them. Steps 1, 2, 4, 5, 6 are structural; step 3 is
the only step that currently requires the scene manager.

An intent with `interval.t_end` far beyond the current point of resolution is not
re-elaborated as resolution passes it — it remains an active record on the scheduler,
carried forward unchanged, until its own `interval` ends, a trigger matches it (step
4), or it is interrupted (§7.1).

## §5 Invocation Eligibility

Re-invoking every `"alert"` character every time any nearby intent resolves produces
unmotivated invocations: a character in a quiet location has no reason to be asked
anything just because an unrelated part of the scene happens to be resolving. The
following condition governs re-invocation, evaluated continuously per location as
intents end or new characters become present — not on a fixed cycle:

1. A character with **no currently active intent** (their previous interval just
   ended per §4 step 5, or they are newly present in the scene) is invoked as soon
   as this becomes true, independent of order relative to other characters at the
   same location (§1). They declare their next intent.
2. If, as a result of step 1, a character at a given location declared a new active
   intent, `"alert"` characters at the same location whose own interval is still
   ongoing are then invoked to reconsider — they may react, or confirm their
   existing intent continues unchanged.
3. If no character at a location currently has zero active intents — everyone
   present already has something ongoing declared — no `"alert"` character at that
   location is invoked. Nothing structurally new has occurred there.
4. `"immersed"` characters are never invoked by this condition. They are only
   re-invoked by their own `trigger_condition` match (§4) or by `interrupt_intent`
   (§7.1).

This condition is evaluated independently per location — activity elsewhere in the
world does not cause re-invocation of characters whose awareness does not reach it.

## §6 Resolved History and Publication

Resolved segments (§4 step 6) accumulate as the resolved history — a continuously
growing record, never cleared, never reset. Nothing in this design clears the scene,
resets declared intents, or wipes accumulated state at any point. An implementation
that clears scene state after producing narration is not implementing this section —
it has reintroduced a turn boundary the continuous timeline was built to eliminate.

### §6.1 Publication Buffer

Each segment's resolved content (§4 step 6) — the substance of what happened, not
tool-call or reasoning overhead — accumulates in a **publication buffer** from the
point of the last published block. The buffer's size is measured in the token length
of this resolved content: primarily the `description` text of `record_event` entries
generated since the last publication.

### §6.2 Publication Threshold

When the publication buffer's accumulated content reaches the **publication
threshold**, the scene manager assembles it into a narrative block and publishes it.
This is the sole ordinary trigger for publication — there is no separate time-based
or cycle-based schedule.

Starting value: **~600–900 tokens of accumulated resolved-content**, calibrated
against a target published block length of roughly 200–400 words, assuming a
resolved-content-to-narration compression ratio of roughly 1.5–2x. This value is a
starting assumption, expected to be tuned from observed play rather than derived
formally. Nothing in this design fixes it permanently.

This threshold is deliberately content-driven, not time-driven: a densely-eventful
hour of combat and a quiet two-hour journey are expected to reach the threshold at
very different points on the world timeline, and should — a dense scene publishes
frequently in short world-time spans; a quiet scene publishes rarely, covering long
world-time spans in a single block.

### §6.3 Session Safety Cap

Independent of the publication buffer, the scene manager's full invocation session
(including QA exchanges, tool calls, and reasoning — not just resolved content) is
bounded by a **session safety cap**, starting value **~15,000–20,000 tokens**. If
this cap is reached before the publication threshold (§6.2) is reached through
ordinary content accumulation, the manager is forced to publish whatever is in the
buffer regardless of size, to prevent unbounded session growth. This is a protective
fallback, not part of the ordinary pacing mechanism, and should fire rarely if the
publication threshold is well-calibrated.

### §6.4 `publish_narrative_block`

```
publish_narrative_block(
    since_event_id: str,   # resolved-history event id to start assembling from
    narration: str,        # the assembled narrative text delivered to players
    covers_t_range: [t_from, t_to],  # the timeline range this block narrates
)
```

This is the only tool that produces player-facing narrative text. It is distinct
from `record_event` (architecture.md), which writes structured facts to the entity
store and is not narrative prose intended for players. Calling this tool clears the
publication buffer (§6.1) and resets it to accumulate from this point forward. It
does not reset or clear the scheduler, active intents, or awareness state — resolved
history continues to accumulate underneath, and the scheduler proceeds unaffected by
publication.

Older, already-published portions of the resolved history may be condensed into
summaries for context management; the manager's working context always includes at
least the unpublished portion accumulated since the last `publish_narrative_block`
call.

## §7 Scene Manager as Participant

The scene manager is an actor on the same scheduler, using the same §3 schema, with
two distinctions from character agents:

- Its view of the resolved history and active intents/subscriptions is not filtered
  by awareness.
- Its intents may be authored on behalf of any NPC or world element in the scene, not
  a single character.

The manager may post world/NPC intents at any point during resolution, exactly as
any other actor on the scheduler — there is no separate phase in which manager
intents must be declared in advance of character intents.

The manager may also hold persistent subscriptions (§3.2) on behalf of NPCs or world
state — e.g. a standing condition on a class of event ("any combat action",
"any mention of a specific name") with `actor: "manager"`. When such a subscription
matches during §4 step 4, its resolution (a new manager-authored intent, or a
prepared reaction) becomes part of the resolved history and the publication buffer.

### §7.1 `interrupt_intent`: One-Sided Interruption

Structural trigger matching (§4) only fires on conditions a character explicitly
declared. This is not sufficient on its own: the world contains events a character
could not have been expected to anticipate — a companion is unexpectedly attacked
elsewhere in their awareness, a location floods, an alarm they had no reason to
predict sounds. An `"alert"` character (§3.6) should be interruptible by such events
even without having declared a matching `trigger_condition` for them. This is a
distinct mechanism from trigger matching, and it belongs to the scene manager alone:

```
interrupt_intent(
    target_actor: str,
    reason: str,        # must reference a specific resolved event
                         # justifying the interruption
    interrupt_at: t,     # point on the timeline the interruption takes effect,
                          # normally t_now — not the start of the interval
)
```

The portion of the character's interval already resolved before `interrupt_at`
stands unchanged (§1 — the timeline does not rewind). From `interrupt_at` forward,
the character is re-invoked with the interrupting information, exactly as if a
trigger had matched.

`interrupt_intent` is constrained by `absorption` (§3.6): freely available against
`"alert"` characters for any event within their awareness that the manager judges
significant; available against `"immersed"` characters only where the cause is a
direct physical threat to that character specifically, not an event concerning
someone or something else nearby. `reason` must cite the resolved event that
justifies the interruption — this is not a narrative-judgment call made from
nothing, it is a structural response to something that has already happened and
been recorded.

## §8 Scene Manager Resolution Calls

Outside of authoring its own intents, deciding publication (§6), and issuing
interruptions (§7.1), the scene manager is invoked for two narrow, structural
queries:

- **Perception gate** (§3.3, §4 step 3): observer's relevant skill vs. the matched
  intent's `detection_difficulty`. Only for `"private"`-visibility matches.
- **Schema-violation fallback** (§3.1): an intent's `description` implies effects
  outside its `action_type`'s declared fields.

Both are narrow, structured queries against specific fields — not open narrative
judgments.

## §9 Per-Invocation Input

### §9.1 Character Agent

- Its own active subscriptions and pending intents (bounded and persistent),
  including those carried from prior invocations, each with an identifier usable in
  `cancel_subscription_ids`. Capped in count.
- Resolved history since its last invocation, filtered by its awareness (§2). On its
  first invocation in a scene, this includes an awareness snapshot of current
  location/participants rather than only a delta.
- Any manager-authored intent affecting the current point of resolution that falls
  within its awareness.
- If this invocation is the result of `interrupt_intent` (§7.1): the specific
  interrupting information only — not a full scene re-summary.

A character agent is never given information about what order other characters were
invoked in, and never given another character's declared intent for the current
resolution point before that intent has been resolved (§4) and become part of the
resolved history within this character's awareness.

### §9.2 Scene Manager

- All participants' active subscriptions and pending intents, unfiltered, with no
  count cap, including each character's current `absorption` state.
- The full accumulated content of the current publication buffer (§6.1) — every
  question asked and answered, every resolved segment, every intermediate
  reasoning step since the last published block. Not only the most recent resolved
  segments — the complete unpublished record. This is what the manager assembles
  into the next `publish_narrative_block` call, and it must not be lost or
  implicitly relied upon from raw session history; it is provided as an explicit,
  structured accumulation for this purpose.
- The current publication buffer size against the publication threshold (§6.2), and
  the current session length against the session safety cap (§6.3).
- The event id of the most recent `publish_narrative_block` call, to determine what
  portion of the resolved history is unpublished.

---

## §10 Future Work

The perception gate (§3.3, §8) is currently a manager call. A future mechanical
replacement would compile registered passive effects (auras, resistances, senses)
into structural checks applied during §4 step 3 without a manager call — extending
the `standing_effects` mechanism (R16) to intent-scale `target_type`s
(`intent_validation`, `damage_payload`, `state_modifier`).

A further extension would add a hidden `true_intent` structure alongside the
declared intent for `"private"` actions with mechanical effects, with
`detection_difficulty` evaluated per-observer against `true_intent` rather than the
declared intent — allowing a concealed action's facade and its actual effect to
diverge, with only observers who pass the perception check receiving information
derived from `true_intent`.

Both depend on a reliable compilation step from rule references to structural
records, and on confirming the `target_type` extension does not require unbounded
growth of the enum.

A further extension to §5 would generalize the location-based invocation condition
to account for observable adjacent locations (an alert character on a balcony
overlooking a courtyard should be invocation-eligible on courtyard activity even
though they are not co-located with it) rather than exact location match only. Not
implemented at this stage.

The publication threshold (§6.2) and session safety cap (§6.3) are starting
assumptions, expected to be recalibrated from observed play — including possibly
weighting buffer content by number of distinct entities involved, not only by raw
token length, since a scene with many simultaneous actors may carry more narrative
weight per token than a single character's uneventful travel. Not implemented at
this stage.
