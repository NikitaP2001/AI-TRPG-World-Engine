# Scene Design

*Turn Resolution, Awareness, and Intent Scheduling*

---

## Scope

This document specifies the scene/turn orchestration layer: how character agents
(player characters and NPCs) and the scene manager are invoked, how their declared
actions are resolved against each other, and what each agent sees at each
invocation. This sits above the L0-L3/L2.5 world simulation
(`wm_tools.md`, `l2_l25_architecture.md`, `l3_specification.md`,
`l1_completion_design.md`) and governs scene-scale interaction — one location or a
connected cluster of locations, seconds to hours of in-world time.

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
across narrative blocks until explicitly cancelled. Has no `on_timeout`. Checked at
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
   as declared.
6. Record the segment's resolved state — actor, action, and what each aware
   character perceived (§2) — into the resolved history.

Natural-language `trigger_condition`s are translated to the structured form once, at
the point the actor formulates them. Steps 1, 2, 4, 5, 6 are structural; step 3 is
the only step that currently requires the scene manager.

## §5 Planning Horizon

Before each resolution cycle, the scene manager sets a horizon `T` on the shared
timeline. Character agents declare intents with `interval.t_start` within
`[t_now, T]`; `interval.t_end` is unrestricted — an action may extend arbitrarily far
past `T`.

`T` is the point up to which the manager expects the current set of intents and
subscriptions to resolve without requiring a wider view. It is set from: any
world/NPC intent the manager intends to introduce this cycle (§7); the nearest point
at which an existing persistent subscription is likely to match given the current
trajectory; or a default cap if neither applies.

An intent with `interval.t_end > T` is not re-elaborated when `T` is reached — it
remains an active record on the scheduler and carries into the next cycle's
resolution unchanged, unless a trigger match (§4 step 4) interrupts it. Re-invocation
of an agent happens only on a trigger match, never solely because `T` was reached.

After resolution reaches `T`, the manager re-evaluates the resolved history and sets
the next `T`.

## §6 Resolved History and Narrative Blocks

Resolved segments (§4 step 6) accumulate as the resolved history. Publishing a
narrative block — assembling a portion of the resolved history into text for
players — is independent of the scheduler: the scheduler continues across block
boundaries with no reset of intervals, subscriptions, or awareness state.

Older portions of the resolved history may be condensed into summaries for context
management; the manager's view (§7) always includes at least the unsummarized
portion since the last `T`.

## §7 Scene Manager as Participant

The scene manager is an actor on the same scheduler, using the same §3 schema, with
two distinctions from character agents:

- Its view of the resolved history and active intents/subscriptions is not filtered
  by awareness.
- Its intents may be authored on behalf of any NPC or world element in the scene, not
  a single character.

The manager's world/NPC intents are posted as part of setting `T` (§5) — based on the
resolved history available at that point, not on character intents for the cycle
about to be planned (those do not exist yet when `T` is set). A manager intent
becomes part of the context character agents receive when declaring intents for
`[t_now, T]`.

The manager may also hold persistent subscriptions (§3.2) on behalf of NPCs or world
state — e.g. a standing condition on a class of event ("any combat action",
"any mention of a specific name") with `actor: "manager"`. When such a subscription
matches during §4 step 4, its resolution (a new manager-authored intent, or a
prepared reaction) becomes part of the resolved history feeding the next `T`.

## §8 Scene Manager Resolution Calls

Outside of setting `T` and authoring its own intents, the scene manager is invoked
for two narrow, structural queries:

- **Perception gate** (§3.3, §4 step 3): observer's relevant skill vs. the matched
  intent's `detection_difficulty`. Only for `"private"`-visibility matches.
- **Schema-violation fallback** (§3.1): an intent's `description` implies effects
  outside its `action_type`'s declared fields.

Both are narrow, structured queries against specific fields — not open narrative
judgments.

## §9 Per-Invocation Input

### §9.1 Character Agent

- Its own active subscriptions and pending intents (bounded and persistent),
  including those carried from prior cycles, each with an identifier usable in
  `cancel_subscription_ids`. Capped in count.
- The current cycle's horizon `T`.
- Resolved history since its last invocation, filtered by its awareness (§2). On its
  first invocation in a scene, this includes an awareness snapshot of current
  location/participants rather than only a delta.
- Any manager-authored intent affecting `[t_now, T]` that falls within its awareness.

### §9.2 Scene Manager

- All participants' active subscriptions and pending intents, unfiltered, with no
  count cap.
- The resolved history since the last `T` (unsummarized), plus access to summarized
  earlier history.
- The previous cycle's `T` and its outcome — whether an expected trigger matched, or
  the cycle ran to the default cap with no match — as input to setting the next `T`.

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
