# World Master Tool Reference

*Requirements analysis and complete tool definitions for the World Master agent*

---

## Part I — Requirements Analysis

Before defining any tool, every hard problem in the simulation must be stated precisely.
A tool designed without knowing its problem space will be either too narrow (can't handle
unforeseen settings) or too broad (LLM can't use it reliably). The analysis below works
through every category of situation the WM will encounter.

---

### R1 — The Universal Setting Problem

The WM must be able to describe *any* world — fantasy, sci-fi, industrial, primal,
techno-magic, hypothetical — without the tool set hardcoding any setting assumptions.

**Consequence:** No tool may have a parameter whose valid values are drawn from a
closed, world-specific enumeration. Every closed enum that would break for a different
setting must be replaced with an open string plus a semantic registration mechanism.

**Specific traps to avoid:**
- `faction_type` should not enumerate `"kingdom" | "empire" | "tribe"` as hard values.
  Those are examples, not a closed set. A tool that lists them as valid enum values breaks
  for a machine empire, a hive mind, or a dissolved corporate hegemony.
- `resource_id` should not be pre-populated. A world with no iron should not have iron
  in the schema. A world with crystallized void-light should be able to register it.
- `existence_type` should not enumerate `"mortal" | "undead"`. A construct that runs on
  geothermal pressure and a digital mind running on distributed nodes need the same
  registration path.
- `special_resource` semantics should be declared by the WM, not assumed by the engine.

**Solution:** Every domain concept is registered before it is used. The WM defines
the vocabulary for his world at initialization. Tools accept those registered identifiers
as strings validated against the registry, not against a hardcoded enum.

---

### R2 — The High-Authority Individual Problem

A king, faction commander, or arch-wizard is simultaneously:
1. A fully-described narrative entity with personality, relationships, and specific orders
2. An authority that overrides the autonomous simulation logic for his domain

The autonomous simulation (L2 civilization dynamics, Lanchester combat resolution) must
*not* make decisions for this entity. His decisions are inputs to the simulation, not
outputs of it.

**Consequence:** The WM needs a mechanism to register a named entity as the *decision
authority* for specific L2 variables in a civilization, bypassing the autonomous update
for those variables and replacing it with the entity's declared intent.

**This is not a normal entity registration.** A regular L3 entity writes to the world
via action rules. A high-authority entity *overrides the simulation's own autonomous
update loop* for specific variables. The distinction matters for tool design.

**Required capability:** `define_entity` must accept an `authority_overrides` block that
declares which L2/L2.5 variables this entity controls directly. When the override is
active, the simulation reads the entity's declared intent for those variables instead of
running the autonomous update formula.

**The feedback loop:** When simulation dynamics contradict the authority entity's
established intent (e.g. Lanchester says the army collapses, but the king has just
declared an offensive), the contradiction mechanism must flag this to the WM for
resolution — not silently resolve it by ignoring the king.

---

### R3 — The Asymmetric Actor Problem

Some entities fundamentally break the assumptions of aggregate simulation models:

- A powerful wizard in a fantasy battle does not participate in Lanchester attrition.
  He applies a discrete asymmetric effect *before* the Lanchester equations run.
- A dreadnought in WH40K cannot be killed by probability — it dies from a specific
  named event, not from accumulated attrition damage.
- A dragon does not starve — it does not participate in the food-supply flow system.
- A cursed artifact does not obey normal resource consumption rules.

**Consequence:** The entity system needs explicit *immunity declarations* and
*pre-engagement effect injection* mechanisms. These are not edge cases — they are the
core use case for legendary/exceptional entities in any non-mundane setting.

**Required capabilities:**
- `immunity` declarations: this entity's `structural_integrity` stock is immune to
  L2 Lanchester damage aggregation. It can only be reduced by specific named delta types
  (direct combat rule firing, specific event types).
- `pre_engagement_effects`: before any L2 combat resolution that includes this entity's
  location, inject these field modifications to the combatant parameters. The Lanchester
  equations then run on modified inputs.
- `termination_condition`: termination is fired by a named event expression, not by
  need depletion. The entity cannot die from starvation equations regardless of its
  `food` need state.

---

### R4 — The Collective Ambiguity Problem

Goblins are not fauna and not a civilization. They have social structure, but also
ecological dynamics. They may have named leaders who function as narrative entities.
They raid, trade, and fight in ways that matter to the civilization layer.

More generally: many entity types in non-standard settings do not map cleanly onto
"individual entity" or "civilization aggregate." They exist on a spectrum.

**Consequence:** The faction/civilization model needs a `social_complexity` parameter
that determines how much L2.5 behavior the collective exhibits. At low complexity, it
runs as ecology (L1 population counts with emergent raiding rules). At high complexity,
it runs as a faction (L2/L2.5 full dynamics). Named leaders are L3 entities attached
to the collective via `faction_id`.

**Required capability:** `define_faction` must accept a `social_complexity` parameter
and a `proto_faction` flag that sets up the ecological-to-social transition threshold.
Below the threshold: runs as enhanced fauna. Above: activates L2.5 faction dynamics.
The transition is automatic based on population/resource conditions.

---

### R5 — The Advanced Capability Problem

Different settings have different mechanisms for exceptional capability:
- A magic world has wizards who cast spells requiring rare components and years of study.
- A hi-tech world has engineers who build reactors requiring uranium and physics knowledge.
- A fantasy alchemy world has master formulists requiring rare reagents and recipes.
- A psionic world has adepts requiring mental training and perhaps rare crystals.

In every case the structure is the same: **rare physical materials** + **accumulated
knowledge** → **named capability** that specific entities can exercise. There is no
ambient "mana flux" that civilizations passively harvest. A court wizard doesn't harvest
mana — he studied for decades, maintains a supply of components, and expends personal
energy (FP in GURPS terms) when casting.

**The three objects are distinct and must be modeled separately:**

*Rare materials* are physically scarce L0/L1 resources with specific geographic
distribution. They are acquired, transported, stockpiled, and consumed. They are
registered as `ore_type` (mineral deposits), `flora_pft` (harvestable plant yields),
or `fauna_species` (creature drops) concepts with normal L0/L1 mechanics. No special
treatment needed beyond low abundance parameters.

*Knowledge* is intellectual capital that accumulates slowly through research investment.
It lives in L2 as a stock, but unlike food or iron, it only grows through deliberate
institution-driven research flows, not through automatic production. It decays slowly
(when institutions are destroyed or scholars die without successors). It is registered
as a `knowledge_domain` concept.

*Capability* is a named, discrete thing a faction or entity can do, unlocked when both
knowledge and material prerequisites cross thresholds simultaneously. It is binary per
entity or faction (you either have it or you don't). It is registered as a `capability`
concept with explicit prerequisites. Capability use consumes materials (not knowledge).

**Consequence:** The WM needs three additional concept types in `define_world_concept`:
`knowledge_domain`, `capability`, and `research_institution`. The `special_resource_tier`
field on factions is retired — faction magical/technological capability is now expressed
as which knowledge domains have been developed and which capabilities have been unlocked.

**The L0 `special_resource_flux` field** retains a narrow, correct purpose: it
represents the geographic distribution of *rare materials that happen to be ambient or
diffuse* (crystalline ley deposits, ambient radiation, trace void-matter in the
atmosphere). These are input stocks for specific research or production flows, not
"harvested" directly as power. The WM declares what these ambient materials are and
what they are prerequisites for. Most worlds have none.

**Additionally:** The WM needs to be able to declare non-standard physical regimes
that have no simulation model but affect entity rules and GM context. An anti-magic
zone, a high-radiation region, a time-dilation field — these are `alter_feature` with
`physics_override` block. They write named field modifiers to cells and can suppress
specific capabilities or modify need decay rates for entities inside them.

---

### R6 — The GURPS Integration Problem

The WM system prompt includes GURPS. The WM will author individual entities in GURPS
vocabulary. The simulation must parse this into its internal representation deterministically.

**Consequence:** `define_entity` must accept a `gurps_sheet` block. Derived mappings:

- HP → `structural_integrity` stock (max from ST+HT formula)
- FP → `energy_reserve` stock (max from HT); this is the **personal casting resource**
  for any entity that casts spells or uses powers — it depletes on capability use and
  recovers via rest flow. It is an L3 entity stock, not a faction-level aggregate.
- Advantages → Interface and Rule declarations (lookup table, enumerable from GURPS)
- Disadvantages → Need vector additions or flow modifiers
- **Magery N** → entity has `capabilities_unlocked` derived from their registered spell
  list, and a personal `energy_reserve` multiplier. Magery is *not* mapped to a
  faction-level `special_resource_tier` — it is personal capability. A faction full of
  Magery 3 wizards is powerful because it has many such entities, not because the
  faction has a mana harvest rate.
- **Skills** → rule condition modifiers (skill level = success probability modifier)
- TL → L2 tech_tier default for spawned civilizations; also affects Lanchester model
- Racial template name → `existence_type` template lookup

The GURPS sheet is not authoritative over explicit parameters — explicit overrides win.
GURPS provides defaults; explicit parameters override them.

---

### R7 — The Tool Count / Cognitive Load Problem

LLM agents have a context budget. Each tool definition occupies tokens. Each tool the
WM must choose between adds cognitive load. Too many narrow tools → the WM picks wrong
ones. Too few broad tools → parameter lists become unmanageable.

**Target:** 8–12 WM-specific tools. Each tool should be organized around *what is being
defined* (a physical feature, an entity, a faction, a rule, a registry entry) rather
than around *which simulation layer* is being touched. The WM thinks in world-building
terms, not in layer indices.

**Consolidation decisions:**
- `register_resource_type`, `register_existence_type`, `register_stratum`,
  `register_norm_type`, `register_ore_type` all go into a single `define_world_concept`
  tool. Same pattern: name a thing, declare its parameters, it becomes referenceable.
- `create_entity` / `update_entity` → `define_entity` (upsert: creates if absent,
  updates if exists)
- `create_faction` / `update_faction` → `define_faction` (same upsert pattern)
- `create_event_rule` / `update_event_rule` → `define_rule` (same upsert pattern)
- Geography tools (`place_feature`, `update_feature`, `dissolve_feature`) → `alter_feature`
  (unified: presence of `feature_id` means update, absence means create; `dissolved: true`
  means dissolve)
- `set_world_orientation` stays separate — it's a one-time initialization call with a
  fundamentally different signature.

---

### R8 — The Init vs. Live-Update Problem

Every tool must work both at world initialization (before play) and during live play
(while the world is running). The WM building the world from scratch and the WM
responding to a narrative escalation mid-campaign both use the same tools.

**Consequence:** No tool may have state that distinguishes "initialization mode" from
"update mode" in a way that requires the WM to know which mode he's in. The upsert
pattern (define = create-or-update) handles this cleanly for most tools.

**Exception:** `set_world_orientation` is genuinely init-only — it cannot be called
after generation has run. This is acceptable because it's obvious: the WM calls it
once at world setup, never again.

---

### R9 — The Validation and Variable Registry Problem

The WM writing condition expressions and flow rate formulas must reference variables
that actually exist in world state. A flow that references `entity.need[fuel]` when the
entity has no `fuel` need will silently produce zero rather than an error.

**Consequence:** A `query_world_state` tool that returns the current registry of
available variables, stocks, flows, and layer fields is as important as any write tool.
Without it, the WM is writing formulas blind.

The write tools must also validate references against the registry before committing.
A `define_entity` call that references an unregistered `existence_type` should fail
loudly, not register a broken entity.

---

### R10 — The Simulation Contradiction and WM Oversight Problem

As the simulation runs autonomously, it will produce outcomes that contradict narrative
facts. The WM must be notified of contradictions and have tools to resolve them without
manually inspecting every entity and faction.

**Consequence:** `get_world_debt` (already in the existing doc) handles task queue
debt. But the WM also needs:
- A notification queue showing pending contradictions, narrative flags from entity rules,
  and simulation events that crossed narrative-significance thresholds.
- A `resolve_contradiction` tool that lets the WM declare an authoritative resolution,
  which may involve overriding a simulation outcome and rebuilding causal state from
  that point.

---

### R11 — The Leadership Decision Regime Problem

A named faction leader must coexist with the autonomous L2 simulation without either
suppressing it entirely or fighting it turn-by-turn. Three distinct regimes must be
handled without a hard mode switch:

**Regime A — Leaderless faction.** No named ruler registered. L2 runs on a default
profile derived from `social_structure_type`. Decisions are institutional and
conservative — the faction acts like a bureaucracy, not a person.

**Regime B — Named leader exists, not actively directed.** The ruler is registered as
an L3 entity with a `leadership_profile`. L2 still runs autonomously, but its objective
function is biased by the profile's weights. The lich expands his army even without WM
attention. The paranoid king purges advisors when stability drops. This costs zero LLM
tokens — it runs deterministically from the compiled profile.

**Regime C — Active WM/GM direction.** The WM posts an `intent_declaration` — a
time-bounded, domain-scoped directive. L2 must satisfy the declared intent while
continuing to optimize everything else normally. The WM declares *what*; the simulation
executes *how*. No part of L2 is switched off.

**The handoff invariant:** L2 always runs. It is never "off." The WM does not suppress
L2 — he shapes its objective function (via `leadership_profile`) and occasionally adds
constraints to it (via `intent_declarations`). The simulation and the WM are never
adversarial because the WM is giving the simulation a directive, not overriding it.

**Complex leadership structures** (king + lords + parliament) are handled by a
`council_members` list on the faction — each member has an `authority_weight` and a
`leadership_profile`. The effective faction decision profile is their weighted aggregate.
High disagreement between members produces high `decision_variance` — the simulation
occasionally makes surprising choices because the leadership is genuinely incoherent.

**The leaderless default** is derived automatically from `social_structure_type`.
"feudal" → moderate military, high institutional stability, low risk tolerance.
"tribal" → high territory expansion, high risk tolerance, low institutional weight.
When a named leader is later registered, his profile smoothly replaces the default.

**Consequence for toolset:** `define_entity` gains a `leadership_profile` block (what
the entity optimizes for when running a faction autonomously) and a simplified
`authority_overrides` that declares *which faction* this entity leads and at what
authority weight — not which specific L2 variables to override. `define_faction` gains
`council_members` (the leadership structure) and `intent_declarations` (active WM/GM
directives currently in force). A dedicated `post_intent` tool handles the common
live-play action of issuing a directive without redefining the whole faction.

---

### R12 — The Knowledge Visibility Problem

A research tree that is visible to every faction from world initialization is
incorrect. A faction cannot pursue a research project it does not know exists. A
medieval peasant kingdom has no awareness that soul-fusion rituals are a category of
thing that can be researched, regardless of whether the project is registered in the
world.

**Two distinct epistemic levels exist:**

*Structural awareness* — knowing that a phenomenon exists in general terms. "Dragons
exist." "There are ruins in the northern mountains." This is widely held, vague, and
does not by itself enable any action.

*Actionable knowledge* — knowing specifically enough to pursue a research project or
exploit a capability. "Dragon hearts are usable as ritual catalysts." "The ruins
contain pre-Sundering metallurgical texts." This is rare and gated.

**Consequence:** `research_project` concepts must support a `discoverable` flag.
Discoverable projects are invisible to a faction until that faction's accumulated
structural awareness — its **myth** state — crosses a registered conviction threshold
linked to that project. Most mundane projects (`discoverable: false`, the default) are
part of common technical knowledge and require no myth state.

**Myth propagation must be causally grounded, not manually tracked by the WM.** A myth
about dragons should not exist anywhere in the world until a dragon does something —
a raid, a sighting, a kill — that generates a myth seed at that location. The seed
then propagates through L2.5 contact networks (faction trust, stratum contact rates,
trade routes) at a rate determined by the myth's registered propagation parameters.
Factions and strata that have no contact with the source region never receive the myth.

**Consequence:** `define_rule` and entity action rules need a `myth_seeds` field —
when the rule fires, it plants a named myth at the firing location, which then
propagates autonomously through the existing L2.5 social machinery (§7 of the L2/L2.5
architecture). The WM does not manually decide who knows what; the simulation
propagates it from the event that generated it.

**Narrative consequence:** `get_social_context()` must return a `myth_vector` —
the set of myths held by a specific stratum at a specific location, each with a
conviction level. The SM uses this to ground NPC dialogue: a farmer's myth vector
will not contain "dragon hearts are ritual catalysts" unless that specific myth has
propagated to his stratum and region through the simulation's social dynamics. The
SM does not need to filter this manually — the query already returns only what that
population actually knows.

---

### R13 — The Forward-Only World and Re-Engagement Problem

The simulation has no rewind. What has happened has happened — there is no
recomputation of past ticks, no replay, no "undo." `resolve_contradiction` can only
declare what is true *now* and let consequences propagate forward normally from that
point, exactly like any other state write.

**The world advances only because players advance.** World time progresses as a
consequence of player turn progression — it is not something the WM decides to do.
The WM does not "regenerate" anything and does not call an advance-time tool himself.
He exists at the world's current moment.

**The WM does not poll, and is not polled every turn.** A model where the WM is
invoked on every time advance to check whether anything needs his attention does not
scale and does not match how the WM actually works — most of the time nothing
requires him, and the autonomous L2/L2.5 + leadership_profile machinery (R11) is
designed precisely so that nothing needs to.

**Consequence:** the WM's only forward-looking mechanism is a **subscription** —
he declares, in advance, broad and flexible filters describing the categories of
future event he cares about. The simulation evaluates these filters as time advances
(driven by player turns). A subscription with `timing: "before"` pauses the *specific
matching event* before its effects commit, invoking the WM with full context so he can
allow, modify, or redirect it — this is the only point at which the WM can ever
influence an outcome, since nothing can be undone afterward. A subscription with
`timing: "after"` simply queues a notification once the event has already committed,
for narrative awareness.

**No subscription matching a category means the WM is never invoked for it** — the
simulation runs that category unattended, which is the correct default (R11 Regime A/B
already produce plausible autonomous behavior without WM attention).

**"Tick" is not WM-facing vocabulary.** The underlying TimeEngine uses an internal
hourly tick counter for bookkeeping, but WM-facing fields (expirations, schedules,
recomputation anchors) must be expressed as expressions over world-time/event state
(`expires_condition`, `revoke_condition`) or as `event_id` anchors — never as raw
tick numbers.

**Consequence for the toolset:** `advance_simulation` is removed from the WM toolset
entirely — it is an orchestrator-level operation invoked when player turns advance
world time, not something the WM calls. A new `subscribe_to_events` tool is the WM's
sole mechanism for "scheduling his own re-engagement." `resolve_contradiction` drops
all recomputation fields (`recompute_from_tick`, `preserve_events`,
`downstream_events_changed`) and becomes a simple forward-only declaration with a
required `narrative_reason` for audit.

---

### R14 — The Entity Roster and Live Directive Problem

Two related gaps exist for individually-tracked L3 entities and for L2-scale military
allocations within a faction.

**No entity roster.** `query_world_state(query_type="entity")` requires a single
`target_id` — there is no way to enumerate entities matching criteria (all NPCs in a
region, all `tier: 3` legendary entities, everything currently `DORMANT`, all units
belonging to a faction's military). Without this the WM cannot discover `entity_id`s
to act on, nor audit the results of directives he has issued.

**No lightweight live directive for a single entity.** Redirecting a named entity's
behavior — "the dragon flies to Irongate," "this squad now patrols the border" —
currently requires a full `define_entity` upsert, which risks clobbering stocks,
rules, and immunities for what is conceptually a one-line order. The same problem
`post_intent` solves for factions (R11) exists at entity scale.

**Faction-level military intents and individually-tracked units can desynchronize.**
`post_intent` with `intent_type="concentrate_military"` redirects a faction's L2
aggregate military allocation. If the WM has also registered specific units as L3
`scale: "unit"` entities for narrative tracking, nothing currently keeps their
`behavior_mode` consistent with the faction-level decision — the aggregate moves
while the visible unit stays put, or vice versa.

**Consequence:** `query_world_state` gains `query_type="entities"` (plural) — a
filterable roster query. A new `post_entity_directive` tool gives the WM a scoped,
upsert-safe way to redirect a single entity's `behavior_mode`/`behavior_parameters`
without touching its other fields — analogous to `post_intent` at entity scale. A new
`military_unit_of` field on `define_entity` tags a unit entity as representing part of
a faction's military allocation; the L2 update sequence (L2/L2.5 §12) propagates active
military-domain `intent_declarations` to tagged units automatically, so the WM declares
intent once and both levels stay consistent.

---

### R15 — Structured Relationships, Payload-Filtered Triggers, and Canon Constraints

**Inter-faction relationships are not reducible to a trust scalar.** `relationships`
on `define_faction` sets a single trust value with another faction. This cannot
express an alliance's mutual-defense obligation, a vassalage's tribute rate and
autonomy reduction, an empire's suzerain-and-members structure, or a trade pact's
specific goods and tariffs. These are discrete, named, time-bounded agreements with
their own terms and effects — structurally the same kind of object as an
`intent_declaration`, but between parties rather than within a faction's own L2
objective function.

**Consequence:** a new `define_relationship` tool registers structured agreements —
alliances, vassalage, empire/union membership, trade pacts, patronage, non-aggression —
as upsertable objects with `parties`, `relationship_type`-specific `terms`, and trust
effects. An empire is the same primitive as a bilateral alliance with one suzerain and
many member parties; `autonomy_level` in `terms` controls how much a member's effective
decision profile blends with the suzerain's, reusing the `council_members`-style
weighted-aggregate mechanism (L2/L2.5 §6) across faction boundaries rather than within
one faction.

**Subscriptions cannot currently target a specific event by payload.** `event_types`
filters by category (`research_complete`) and `field_thresholds` filters by a field
crossing a value — neither lets the WM say "fire when *any* faction completes
*this specific* research_project," "when *this* capability is unlocked," or "when
*this* feature is discovered." These are payload-identity filters, not category or
threshold filters.

**Consequence:** `subscribe_to_events` filters gain `event_payload_filters` — an open
dict matched against the firing event's payload fields (`project_id`, `capability_id`,
`myth_id`, `feature_id`, or any world-defined event payload key). Combined with
`timing: "before"`, this is how the WM catches "someone is about to complete the
soul-fusion ritual" the moment it happens, anywhere in the world, before its effects
commit.

**A canon-constrained world needs standing enforcement, not one-off declarations.**
`resolve_contradiction` is a single forward declaration; `intent_declarations` shape a
faction's objectives but can be outweighed by other objectives or by `decision_variance`.
Neither mechanism *guarantees* a specific fact remains true indefinitely. For a world
built on established lore (e.g. Forgotten Realms) where certain facts are load-bearing
— a city remains independent, a specific NPC remains alive, a magical phenomenon
continues to function — the WM needs to register that fact once and have it held,
either by automatic correction whenever the simulation would violate it, or by
guaranteed WM notification before any violating change commits.

**Consequence:** a new `canon_constraint` concept type in `define_world_concept`
registers a standing expression checked every update step (L2/L2.5 §12, new step 1.7).
`violation_response: "auto_resolve"` applies a WM-authored correction template
automatically and silently whenever the constraint would be violated — for facts the
WM wants held without per-instance attention. `violation_response: "subscribe_before"`
auto-registers a `"before"`-timing subscription for any change that would violate the
constraint — for facts subtle enough that the WM wants to see the specific situation
each time. `priority: "canon" | "soft_canon" | "flavor"` controls strictness: `canon`
is always enforced; `soft_canon` allows one violation with notification before
re-flagging; `flavor` only ever notifies. This is the mechanism by which most of the
world runs fully autonomous and potentially surprising (R11 Regimes A/B, emergent
outcomes welcome) while a deliberately small set of load-bearing facts are held
regardless of what the simulation would otherwise produce.

---

### R16 — The Persistent Technology Integration Problem

A researched `capability` (R5/§5) needs to change a faction's *baseline behavior*
going forward, not just fire a one-time effect at the moment of unlock.
`unlock_effects`/`on_complete_effects` are one-time `world_deltas` — correct for
"unlocking gunpowder spawns cannon units," but wrong for "iron tools make
agricultural production 30% more efficient, forever, for any faction that has them."
The latter is a standing multiplier on an ongoing L2 coefficient — a flow rate, a
demographic parameter, a combat model selector — that must remain active for as long
as the capability is held, applied uniformly to any faction that holds it on top of
that faction's own registered values.

**Consequence:** the `capability` concept gains `standing_effects` — persistent
coefficient modifiers (`target_type`/`target_id`/`field`/`operation`/`value`) applied
each update step (L2/L2.5 §12, new step 2 sub-step 0) to any faction holding the
capability. This is distinct from and complementary to `unlock_effects`: unlock
fires once at the moment of acquisition; standing effects apply continuously
thereafter. The same mechanism makes §4's `combat_type` (currently a static
registration-time derivation from `tech_level`) dynamic — a faction that researches
`gunpowder_weapons` mid-game transitions from linear to square-law Lanchester via a
`standing_effect` with `target_type: "combat", field: "combat_type", operation: "set"`.

---

## Part II — Tool Definitions

Based on the requirements above, the WM tool set is **13 tools**:

| # | Tool | Purpose |
|---|---|---|
| 1 | `set_world_orientation` | One-time world initialization |
| 2 | `define_world_concept` | Register any named world-specific type: resource, existence_type, stratum, norm, ore, etc. |
| 3 | `alter_feature` | Create, update, or dissolve geographic/physical features |
| 4 | `define_entity` | Create or update any named entity (L3, narrative-persistent, or legendary) |
| 5 | `define_faction` | Create or update any faction, civilization, proto-faction, or institution |
| 6 | `define_rule` | Create or update any event system production rule |
| 7 | `declare_world_state` | Declare narrative facts, era parameters, prophecies, historical events |
| 8 | `subscribe_to_events` | Register forward-looking filters for re-engagement before/after events commit |
| 9 | `query_world_state` | Read current simulation state, registry contents, world debt, notifications, entity and relationship rosters |
| 10 | `resolve_contradiction` | Declare authoritative world state going forward |
| 11 | `post_intent` | Issue a directive to a faction's L2 decision engine |
| 12 | `post_entity_directive` | Issue a scoped behavior directive to a single L3 entity |
| 13 | `define_relationship` | Create or update structured inter-faction agreements: alliances, vassalage, empires, trade pacts |

Query tools for GM/SM are not included here — this document covers WM-only tools.

---

### Tool 1: `set_world_orientation`

One-time initialization. Establishes coordinate reference frame, global physics
parameters, and world-specific resource type registration. Must be called before any
other tool. Cannot be repeated after the generation pipeline runs.

```python
@tool
def set_world_orientation(

    # ── Geometry ─────────────────────────────────────────────────────────────

    planet_radius: float,
    # Radius in any consistent unit. All subsequent distances use this unit.
    # Determines cell size: cell_side_length = planet_radius / 60.
    # ~17,400 top-level H3 cells regardless of value.

    reference_meridian: float = 0.0,
    # Longitude of the prime meridian. Arbitrary — pick a meaningful location.

    axial_tilt: float = 23.5,
    # Planet axial tilt in degrees. 0 = no seasons. 23.5 = Earth-like.
    # Higher = extreme seasonal swings.

    # ── Global climate baseline ───────────────────────────────────────────────

    global_temperature_offset: float = 0.0,
    # Degrees added to every cell's derived temperature.

    global_precipitation_modifier: float = 1.0,
    # Multiplier on derived precipitation. >1 = wetter world.

    solar_intensity: float = 1.0,
    # Multiplier on total solar energy. <1 = dim star, cold world.

    atmospheric_density: float = 1.0,
    # Temperature buffering factor. Low = wild swings. High = greenhouse effect.

    ocean_temperature: float = 15.0,
    # Base ocean surface temperature. Affects maritime climate moderation.

    tectonic_activity: float = 0.5,
    # 0.0–1.0. Geological complexity during generation.
    # Low = stable cratons, few ranges. High = active orogeny, many ranges.

    # ── Ambient rare materials (optional, most worlds have none) ─────────────

    ambient_rare_materials: list[dict] = [],
    # Declares rare materials that are geographically distributed as diffuse
    # ambient deposits across the landscape as a continuous field, rather
    # than as discrete ore veins.
    #
    # Most worlds have none. Use only for materials that are genuinely ambient
    # (e.g. trace void-crystals in ley-lines, atmospheric trace compounds,
    # low-level background radiation). Discrete ore deposits are registered
    # via define_world_concept as ore_type instead.
    #
    # The L0 grid stores a flux value per cell for each declared ambient
    # material, generated via Gray-Scott reaction-diffusion seeded by
    # tectonic activity. These values are input stocks for capability
    # prerequisites and research flows — they are not "harvested" as power.
    #
    # Each entry:
    # {
    #   "material_id":        str,   # identifier; referenced in capability
    #                                # prerequisites and research flows
    #   "name":               str,
    #   "gray_scott_feed":    float, # feed rate [0.01–0.08]
    #   "gray_scott_kill":    float, # kill rate [0.04–0.07]
    #   "diffusion_rate":     float, # spatial spread [0.0–1.0]
    #   "geological_affinity":float, # correlation with tectonic_stress [0.0–1.0]
    #   "description":        str
    # }

    # ── Long-cycle dynamics ───────────────────────────────────────────────────

    long_cycle_tick_interval: int = 1000,
    # Ticks between L0 long-cycle updates (climate drift, geological stress).

    climate_drift_rate: float = 0.0,
    # How much global_temperature_offset shifts per long-cycle tick.
    # 0 = stable climate. Positive = gradual warming. Negative = cooling.

    # ── Narrative frame ───────────────────────────────────────────────────────

    direction_names: dict = {},
    # Optional remapping of cardinal direction labels for narrative output.
    # { "north": "spinward", "south": "rimward", "east": "coreward", ... }
    # Affects natural language output only. All math uses standard lat/lon.

    world_name: str = "",
    # Display name of the world. Used in query outputs and GM context.

    tech_level_default: int = 4,
    # GURPS TL applied to any civilization not given an explicit tech level.
    # TL0=stone age, TL4=medieval, TL8=near-future, TL12=super-science.

) -> dict:
    # Returns:
    # {
    #   "status": "ok",
    #   "cell_side_length": float,
    #   "top_level_cell_count": int,       # always ~17,400
    #   "grid_initialized": bool,
    #   "ambient_rare_materials_registered": list[str],  # material_ids
    #   "warnings": list[str]
    # }
```

**Side effects:** Initializes H3 cell grid. Creates ~17,400 empty cell condition vector
records. Registers all declared special resources in the world registry. No features
created yet.

---

### Tool 2: `define_world_concept`

Registers any named world-specific type into the simulation registry. This tool is
called before referencing a concept type anywhere else — a `define_entity` call that
uses an unregistered `existence_type` will fail validation.

This is the single entry point for extending the world's vocabulary: new entity types,
social strata, cultural norm dimensions, ore formations, faction archetypes, behavior
templates. All settings register their concepts here; the simulation remains generic.

```python
@tool
def define_world_concept(

    concept_type: str,
    # What kind of concept is being registered. Open string — the system
    # recognizes these types and applies appropriate validation:
    #
    # "existence_type"       — entity need vector template (mortal, construct,
    #                          machine, energy_being, swarm, distributed_system, ...)
    # "stratum"              — social class for L2.5 norm queries
    #                          (peasant, merchant, noble, priest, soldier, ...)
    # "norm"                 — a cultural dimension tracked in L2.5
    #                          (violence_tolerance, religious_observance, ...)
    # "mineral"               — a single mineral species (density, hardness,
    #                           category, economic value). Prerequisite for
    #                           ore_type registration.
    # "ore_type"              — formation rule for deposits of a registered
    #                           mineral: where, how, and how often they occur
    #                           during world generation
    # "flora_pft"             — a plant functional type with climate envelope,
    #                           growth parameters, and harvest yields. Populates
    #                           L1 vegetation via the existing suitability model.
    #                           (moonbloom, ironwood, giant_lotus...)
    # "fauna_species"         — an animal population template with habitat
    #                           suitability, demographic rates, and drops.
    #                           Populates L1 current_population[species_id].
    #                           (dire_wolf, ancient_dragon, goblin...)
    # "knowledge_domain"      — a named container for intellectual capital that
    #                           research projects write into on completion.
    #                           (alchemy_advanced, necromantic_theory, nuclear_physics...)
    # "research_project"     — a discrete named research task with explicit cost,
    #                           consumed materials, prerequisite projects, and outputs.
    #                           Forms the research tree. WM defines the full tree.
    #                           (intermediate_alchemy, fission_reactor, void_binding...)
    # "capability"           — a named discrete action a faction or entity can perform,
    #                           unlocked by completing research_projects or declared
    #                           directly at initialization.
    #                           (city_level_ritual, nuclear_reactor, dragonbone_armor...)
    # "research_institution" — an institution type providing research_points per tick
    #                           toward an assigned research_project.
    #                           (wizard_academy, university, monastery, research_lab...)
    # "myth"                  — a named unit of structural awareness that propagates
    #                           through L2.5 social contact networks. Crossing a
    #                           conviction threshold makes linked discoverable
    #                           research_projects visible to a faction or stratum.
    #                           (dragons_exist, soul_fusion_possible, northern_ruins_have_texts...)
    # "item"                  — a named non-actor object: portable artifacts, unique
    #                           materials, cursed equipment. No behavior, no need
    #                           vector, no tick execution. May carry a passive aura
    #                           and/or combat effects active only when carried.
    #                           (philosophers_stone, sundered_crown, ancient_catalyst...)
    # "canon_constraint"      — a standing assertion checked every update step (R15).
    #                           Holds a load-bearing fact regardless of what the
    #                           autonomous simulation would otherwise produce, either
    #                           by automatic correction or by guaranteed WM
    #                           notification before any violating change commits.
    #                           (waterdeep_remains_independent, elminster_survives...)
    # "faction_template"     — default stock/flow/rule set for a faction archetype
    # "behavior_template"    — reusable entity behavior profile
    # "damage_type"          — a named damage category for combat rules
    #                          (piercing, fire, void, divine, psionic, ...)
    # "event_type"           — a named event category for rule conditions and
    #                          causal chain attribution
    # Any other string       — stored in registry as a named tag with parameters.

    concept_id: str,
    # Unique identifier. Used in all tool calls that reference this concept.
    # If a concept with this ID already exists, this call updates it.

    name: str = "",
    # Display name. Defaults to concept_id if omitted.

    description: str = "",
    # WM note. Stored for reference, not used by simulation engine.

    # ── Parameters by concept_type ───────────────────────────────────────────

    parameters: dict = {},
    # Type-specific parameters. Validated against the concept_type schema.
    #
    # ── existence_type parameters ─────────────────────────────
    # {
    #   "needs": [                          # list of needs in this template
    #     {
    #       "need_id":              str,    # unique within this entity
    #       "decay_rate":           float,  # depletion per tick at rest
    #       "depletion_threshold":  float,  # 0.0–1.0; default 0.2
    #       "depletion_consequence": str,   # "seek_resource" | "distress_action"
    #                                       # | "termination" | "cascade:need_id"
    #       "replenishment_source": str,    # "L0" | "L1" | "L2" | "self_carried"
    #                                       # | "raid" | "none"
    #       "decay_modifiers": list[str]    # condition expressions that modify rate
    #     }
    #   ],
    #   "termination_model":  "need_depletion" | "event_driven" | "never",
    #   "termination_event":  str,          # event_type if event_driven
    #   "scale_type":         "individual" | "party" | "unit" | "swarm",
    #   "biological":         bool,         # true = standard biological needs apply
    #   "gurps_racial_template": str        # optional: GURPS racial template name
    #                                       # derives defaults for attributes below
    #   "base_st": int, "base_dx": int, "base_iq": int, "base_ht": int,
    #   # GURPS attributes for entities of this type.
    #   # If gurps_racial_template provided, these override the template values.
    # }
    #
    # ── stratum parameters ────────────────────────────────────
    # {
    #   "default_norms": { "norm_id": float, ... },  # initial norm vector
    #   "default_trust_baseline": float,              # 0.0–1.0 stranger trust
    #   "mobility_to": list[str]                      # strata reachable via mobility
    # }
    #
    # ── norm parameters ───────────────────────────────────────
    # {
    #   "range": [float, float],            # valid value range; default [0.0, 1.0]
    #   "propagation_rate": float,          # SIR model spread rate
    #   "resistance_baseline": float,       # baseline resistance to change
    #   "description_low": str,             # narrative label at low value
    #   "description_high": str             # narrative label at high value
    # }
    #
    # ── mineral parameters ───────────────────────────
    # A single mineral species. Maps to the simulation's MineralDef.
    # {
    #   "formula":          str,    # chemical formula; "" for fantasy minerals
    #   "density_gcm3":     float,  # default 2.5
    #   "hardness_mohs":    float,  # default 5.0
    #   "category":         str,    # "mineral" | "ore_metal" | "ore_gem" |
    #                               # "ore_rare" | "ore_energy" | "fantasy"
    #                               # | (world-defined)
    #   "value":            float,  # economic value index, roughly 1-100;
    #                               # feeds trade_value for L2 trade model (§3)
    #   "description":      str
    # }
    #
    # ── ore_type parameters ───────────────────────
    # An ore formation rule: where, how, and how often deposits of a mineral
    # form during world generation (or when alter_feature modifies geology
    # mid-play, e.g. a new volcanic feature). Maps to the simulation's
    # OreFormation. Deposits are placed as features in the feature store,
    # queryable via query_world_state(query_type="feature").
    # {
    #   "primary_ore":      str,    # mineral concept_id (registered separately
    #                               # via concept_type="mineral")
    #   "secondary_ores":   list[str],  # mineral concept_ids co-occurring
    #   "gangue":           list[str],  # mineral concept_ids of waste rock
    #   "formation_type":   str,    # "sedimentary" | "magmatic" | "hydrothermal"
    #                               # | "metamorphic" | "placer" | (world-defined,
    #                               # e.g. "arcane" for fantasy formations)
    #   "host_rocks":       list[str],  # lithology rock_type values required
    #                               # nearby, e.g. ["granite", "gneiss"]
    #   "depth_range":      [float, float],  # meters
    #   "grade_range":      [float, float],  # ore concentration fraction
    #   "required_geo_types": list[int],  # geological_type codes that qualify
    #                               # (0=oceanic crust, 1=sedimentary basin,
    #                               #  2=continental crust, 3=mountain/orogenic,
    #                               #  4=volcanic, 5=craton, 6=rift; see L0
    #                               #  cell_model for the authoritative list)
    #   "required_age_min": float,  # crustal age in Myr; default 0
    #   "required_age_max": float,  # default 5000
    #   "required_tectonic": str,   # "convergent"|"divergent"|"intraplate"|""
    #   "min_crustal_thickness": float,  # km; default 0
    #   "rarity":           float,  # probability per qualifying cell, 0.0-1.0.
    #                               # Real-world ores: 0.0005-0.02.
    #                               # Mithril-tier fantasy rarity: ~0.0005.
    #   "vein_volume_range": [float, float],  # cubic meters
    #   "vein_shape":       str,    # "vein"|"massive"|"layer"|"pipe"|"lens"|
    #                               # "scattered"|"disseminated"
    #   "ambient_material_affinity": str,  # ambient material_id from
    #                               # ambient_rare_materials: this ore forms
    #                               # more commonly where that flux is high
    #   "capability_use":   str,    # capability_id for which this ore is a
    #                               # material prerequisite (informational)
    #   "description":      str
    # }
    #
    # ── flora_pft parameters ────────────────────────
    # A plant functional type. Maps to the simulation's PlantDef. Registering
    # a flora_pft adds it to the suitability competition the vegetation
    # feature runs each L1 tick — cells where this PFT's climate envelope is
    # the best match develop it, without manual placement.
    # {
    #   "common_name":      str,
    #   "family":           str,    # "conifer"|"deciduous"|"evergreen"|"grass"|
    #                               # "shrub"|"succulent"|"moss"|(world-defined)
    #   "growth_form":      str,    # "tree"|"shrub"|"grass"|"herb"|"moss"|"vine"
    #   # Climate envelope, all normalised 0-1 against world climate range:
    #   "temp_min": float, "temp_opt_min": float,
    #   "temp_opt_max": float, "temp_max": float,
    #   "precip_min": float, "precip_opt_min": float,
    #   "precip_opt_max": float, "precip_max": float,
    #   # Soil preferences:
    #   "ph_min": float, "ph_max": float,        # default 4.0-8.5
    #   "fertility_min":    float,  # minimum soil_fertility to survive
    #   "shade_tolerance":  float,  # 0=pioneer, 1=climax species
    #   # Growth:
    #   "max_height_m":     float,
    #   "max_biomass_kgm2": float,
    #   "max_canopy_density": float,  # 0-1
    #   "growth_rate":      float,  # per-tick fraction of max
    #   "mortality_rate":   float,  # per-tick background mortality
    #   # Phenology:
    #   "leaf_type":        str,    # "evergreen"|"deciduous"|"semi-deciduous"
    #   "dormancy_temp":    float,  # below this, growth stops; -1 = never
    #   "dormancy_precip":  float,  # below this, drought dormancy
    #   # Litter (feeds soil_fertility):
    #   "litterfall_rate":  float,
    #   "litter_cn_ratio":  float,
    #   # Material properties — feed L2 stocks and research materials:
    #   "wood_density_gcm3": float,
    #   "timber_quality":   float,  # 0-1
    #   "fruit_yield_kgm2": float,  # annual edible yield at full biomass
    #   "medicinal":        bool,
    #   "ornamental":       bool,
    #   "harvest_yield": {          # what L2 production flows can extract,
    #     "stock_id": float, ...    # per unit biomass per tick. e.g.
    #                               # {"moonbloom_petals": 0.02} for a rare
    #                               # flower feeding a research_project's
    #                               # consumed_materials
    #   },
    #   # Rooting:
    #   "root_depth_m":     float,
    #   "drought_tolerance": float,
    #   "description":      str
    # }
    #
    # ── fauna_species parameters ───────────────────────
    # An animal population template. Registering a fauna_species populates
    # L1's current_population[species_id] field across cells where habitat
    # suitability is nonzero — no manual placement needed. The same
    # registration mechanism underlies proto-faction populations (R4):
    # a goblin tribe begins as a fauna_species population and transitions
    # to faction dynamics as social_complexity ramps (L2/L2.5 §13).
    # {
    #   "existence_type":   str,    # registered existence_type concept_id;
    #                               # supplies need vector and termination model
    #   "habitat_biomes":   list[str],  # biome classifications this species
    #                               # can inhabit (from L1 biome classification)
    #   "habitat_suitability_modifiers": list[str],  # expressions over L0/L1
    #                               # fields that scale suitability, e.g.
    #                               # "L0.cell[elevation_mean] > 2000 -> suit*0.3"
    #   "base_birth":       float,  # per-tick birth rate at full suitability;
    #                               # feeds demographic model (L2/L2.5 §2)
    #   "base_death":       float,  # per-tick background death rate
    #   "population_density_max": float,  # individuals per cell at suitability=1.0
    #   "diet":             str,    # "herbivore"|"carnivore"|"omnivore"|
    #                               # (world-defined, e.g. "mana_grazer")
    #   "diet_sources":     list[str],  # for herbivore/omnivore: flora_pft
    #                               # concept_ids grazed; for carnivore:
    #                               # fauna_species concept_ids preyed upon
    #   "drops": [                  # what an individual yields when killed
    #     {
    #       "stock_id":     str,    # e.g. "hide", "meat", "dragon_heart"
    #       "quantity":     float,
    #       "probability":  float,  # 0.0-1.0
    #       "condition":    str     # optional expression gating this drop,
    #                               # e.g. "entity.existence_type == 'ancient_dragon'"
    #     }
    #   ],
    #   "description":      str
    # }
    #
    # ── faction_template parameters ───────────────────────────
    # {
    #   "default_stocks": { "stock_id": float, ... },
    #   "default_flows": [ { ... } ],       # see Flow schema in define_faction
    #   "default_rules": [ { ... } ],       # see Rule schema in define_rule
    #   "social_structure": str,            # "feudal" | "flat" | "theocratic"
    #                                       # | "corporate" | "tribal" | (world-defined)
    # }
    #
    # ── damage_type parameters ────────────────────────────────
    # {
    #   "default_resistance": float,        # 0.0–1.0 baseline resistance for all entities
    #   "bypasses_armor": bool,
    #   "bypasses_immunity_of": list[str],  # existence_types whose immunity this pierces
    # }
    #
    # ── event_type parameters ─────────────────────────────────
    # {
    #   "severity_baseline": float,         # default narrative severity 0.0–1.0
    #   "default_narrative_flag": bool,     # auto-flag for GM attention
    #   "causal_children_expected": list[str]  # event_types commonly caused by this
    # }

    # ── knowledge_domain parameters ───────────────────────
    # A knowledge domain is a named container that research projects write
    # into when they complete. It does not accumulate passively.
    # {
    #   "description":          str,
    #   "decay_rate":           float,  # per tick without maintenance
    #                                   # 0.0 = permanent (default, most cases)
    #                                   # >0 = fades without active research
    #   "max_value":            float   # ceiling; default 1.0
    # }
    #
    # ── research_project parameters ───────────────────────
    # A discrete named project that factions or entities are assigned to work
    # on. This is the RimWorld/HoI4 model: the WM defines projects, each with
    # explicit costs and outputs. Nothing accumulates passively. A faction must
    # actively assign research capacity to a project for it to progress.
    # {
    #   "prerequisites": [             # projects that must be completed first
    #     { "project_id": str }        # forms the tech/research tree
    #   ],
    #   "research_cost":         float,# total research_points to complete.
    #                                  # research_points = institution_capacity x ticks.
    #                                  # Example: 500 = one standard academy for ~50 ticks
    #   "consumed_materials": {        # stocks destroyed during research
    #     "stock_id": float, ...       # e.g. {"moonbloom": 5, "silver_dust": 2}
    #   },
    #   "required_materials": {        # must be present but not consumed
    #     "stock_id": float, ...       # e.g. artifact to study, reference sample
    #   },
    #   "required_institution":  str,  # research_institution type_id that must
    #                                  # exist in the faction. Empty = any.
    #   "required_location":     str,  # feature_id that must be controlled
    #   "required_entity":       str,  # entity_id that must be assigned to project
    #                                  # (named master, specific scholar, artifact)
    #   "scope":                 str,  # "faction" = faction assigns institution capacity
    #                                  # "entity" = individual entity researches
    #                                  #   (uses entity.research_capacity stock)
    #   "knowledge_outputs": [         # what completing this project grants
    #     { "domain_id": str, "value_added": float }
    #   ],
    #   "capability_unlocks": list[str], # capability_ids unlocked on completion
    #   "stock_outputs": {             # non-knowledge outputs on completion
    #     "stock_id": float, ...       # e.g. schematic produced as stock item
    #   },
    #   "on_complete_effects": list[dict], # world_deltas fired on completion
    #   "notify_wm_on_complete": bool,
    #   "repeatable":            bool, # false = once done, cannot re-run
    #                                  # true = ongoing research, runs again after cooldown
    #   "discoverable":          bool, # default false. If true, this project is
    #                                  # invisible to a faction until linked myths
    #                                  # (see myth concept) reach conviction threshold
    #                                  # for that faction. Most mundane projects are
    #                                  # false. Anything extraordinary should be true.
    #   "description":           str
    # }
    #
    # ── capability parameters ─────────────────────────────────
    # A named discrete action a faction or entity can perform.
    # Typically unlocked via research_project completion, but can also be
    # declared directly at world generation for pre-existing capabilities.
    # {
    #   "knowledge_prerequisites": [   # must hold to USE this capability
    #     { "domain_id": str, "min_value": float }
    #   ],
    #   "material_prerequisites": [
    #     { "stock_id": str, "min_quantity": float }
    #   ],
    #   "location_prerequisites": list[str],
    #   "entity_prerequisites":   list[str],
    #   "use_cost": {                  # consumed each use
    #     "stock_id": float, ...
    #   },
    #   "use_energy_cost":       float,
    #   "scope":                 str,  # "faction" | "entity" | "both"
    #   "repeatable":            bool,
    #   "cooldown_ticks":        int,
    #   "unlock_effects":        list[dict], # one-time world_deltas fired
    #                                  # the moment this capability is unlocked
    #                                  # (e.g. spawn_entity for new unit types)
    #   "standing_effects": [          # PERSISTENT coefficient modifiers,
    #                                  # applied every update step (L2/L2.5
    #                                  # §12 step 2, sub-step 0) for as long as
    #                                  # ANY faction holding this capability has
    #                                  # it — not a one-time delta like
    #                                  # unlock_effects above.
    #     {
    #       "target_type": str,  # "flow" | "demographic" | "combat" |
    #                            # "trade" | "research"
    #       "target_id":   str,  # flow_id this modifies (matched by id across
    #                            # any faction holding this capability), or
    #                            # "*" for a category-wide effect with no
    #                            # specific target (e.g. all combat engagements
    #                            # this faction is party to)
    #       "field":       str,  # which coefficient on the target:
    #                            # for "flow": "rate"
    #                            # for "demographic": "subsistence_threshold",
    #                            #   "base_birth", "base_death", "safety_factor"
    #                            # for "combat": "combat_type", "lethality",
    #                            #   "rout_threshold"
    #                            # for "trade": "infrastructure_cap",
    #                            #   "trade_value"
    #                            # for "research": "base_capacity"
    #       "operation":   str,  # "multiply" | "add" | "set"
    #       "value":       float | str  # constant or expression
    #     }
    #   ],
    #   "notify_wm_on_unlock":   bool,
    #   "description":           str
    # }
    #
    # ── research_institution parameters ──────────────────────
    # An institution type that contributes research_points per tick toward
    # whichever project the faction has currently assigned it to.
    # {
    #   "eligible_projects": list[str],  # project_ids this institution can work on
    #                                    # empty = can work on any project
    #   "base_capacity":      float,   # research_points per tick at full staffing
    #   "scholar_stratum":    str,     # stratum that staffs this institution
    #   "upkeep_materials": {          # consumed per tick just to stay open
    #     "stock_id": float, ...
    #   },
    #   "ambient_material_req": {      # territory flux threshold to operate
    #     "material_id": float, ...
    #   },
    #   "capacity_modifiers": list[str], # expressions multiplying capacity
    #   "description":        str
    # }
    #
    # ── myth parameters ───────────────────────────────────────
    # A named unit of structural awareness. Held per faction and per stratum,
    # each with an independent conviction value [0.0–1.0]. Propagates through
    # L2.5 social contact networks using the same Deffuant-style mechanism as
    # norm propagation (see L2/L2.5 architecture §7).
    # {
    #   "linked_projects": list[str],  # research_project ids made visible when
    #                                  # conviction crosses conviction_threshold
    #   "conviction_threshold": float, # 0.0–1.0; default 0.6
    #   "propagation_rate":  float,    # conviction spread rate per tick
    #                                  # through contact networks; default 0.01
    #   "decay_rate":        float,    # conviction decay per tick without
    #                                  # reinforcement; default 0.001
    #   "initial_locations": [         # myth seeds present at world initialization
    #     {
    #       "location":     str,       # feature_id or location name
    #       "stratum_id":   str,       # "" = applies to all strata at location
    #       "conviction":   float      # initial conviction value
    #     }
    #   ],
    #   "description":       str       # narrative description, e.g.
    #                                  # "Tales of a great wyrm sleeping beneath
    #                                  #  the Ashfall Peaks"
    # }
    #
    # ── item parameters ───────────────────────────────────────
    # A non-actor object. No behavior_mode, need vector, or tick execution.
    # Referenced by entity inventory[] (define_entity) and by feature
    # contains.items (alter_feature). See L3 specification §12.
    # {
    #   "portable":    bool,  # whether an entity can carry this in inventory
    #   "properties":  dict,  # open key-value; what this item satisfies as a
    #                         # prerequisite. e.g. {"capability_use":
    #                         # "soul_fusion_ritual"} for research_project
    #                         # required_materials / required_entity gates
    #   "aura":        dict,  # same schema as entity auras (define_entity).
    #                         # Active whenever this item exists in the world,
    #                         # regardless of whether it is carried.
    #   "combat_effects": dict, # same schema as entity pre_engagement_effects.
    #                         # Active only when carried by an entity present
    #                         # in an L2 combat engagement.
    #   "destructible": bool, # whether use_cost/consumed_materials consumption
    #                         # can destroy this item
    #   "unique":      bool,  # if true, only one instance may exist at a time.
    #                         # Creating a second while the first exists and is
    #                         # not destroyed is a validation error.
    #   "description": str
    # }
    #
    # ── canon_constraint parameters ───────────────────────────
    # A standing assertion checked every update step (L2/L2.5 §12 step 1.7).
    # {
    #   "constraint_expression": str,  # boolean expression over world state,
    #                                  # using the same grammar as rule
    #                                  # conditions. Constraint is SATISFIED
    #                                  # when this evaluates true.
    #                                  # e.g. "L2.faction[waterdeep].sovereignty == true"
    #                                  #      "entity.exists[elminster] == true"
    #                                  #      "L0.feature[weave_node_3].active == true"
    #
    #   "violation_response": str,     # "auto_resolve" | "subscribe_before"
    #     # "auto_resolve": when a pending change (L2/L2.5 §12 step 6) would
    #     #   cause constraint_expression to become false, automatically apply
    #     #   auto_resolve_template instead of the pending change. No WM
    #     #   invocation. Silent, continuous enforcement.
    #     # "subscribe_before": auto-registers a "before"-timing
    #     #   subscribe_to_events filter matching any change that would
    #     #   violate constraint_expression. The WM is invoked with the
    #     #   pending change every time this would occur, for the life of
    #     #   this constraint.
    #
    #   "auto_resolve_template": dict, # used if violation_response="auto_resolve".
    #                                  # Same shape as resolve_contradiction's
    #                                  # declared_state — the correction applied
    #                                  # in place of the violating change.
    #                                  # e.g. { "override_type": "pending_event",
    #                                  #        "declared_state": {"effects": []} }
    #                                  # (suppress the violating event entirely)
    #
    #   "priority":  str,    # "canon" | "soft_canon" | "flavor"
    #     # "canon":      always enforced per violation_response
    #     # "soft_canon": first violation in a row is allowed through with a
    #     #   WM notification; if constraint_expression is still false on the
    #     #   following update step, violation_response applies as for "canon"
    #     # "flavor":     never blocks or auto-resolves; every violation only
    #     #   queues a notification (equivalent to an "after"-timing subscription
    #     #   on this constraint's violation)
    #
    #   "narrative_reason": str,  # why this fact is held; written to event
    #                             # log whenever violation_response fires
    #   "description": str
    # }
    #
    update_existing: bool = True,
    # If true (default): if concept_id exists, update it.
    # If false: return an error if concept_id already exists (strict creation).

) -> dict:
    # Returns:
    # {
    #   "concept_id": str,
    #   "concept_type": str,
    #   "created": bool,            # true = new, false = updated existing
    #   "validation_errors": list[str],  # empty if clean
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts or updates the concept in the world registry. All subsequent
tool calls that reference `concept_id` will validate against this registration.
For `existence_type`: if entities using this type already exist, a compatibility check
runs — breaking changes require explicit `force_update: true` and trigger re-validation
of all affected entities.

---

### Tool 3: `alter_feature`

Creates, updates, or dissolves any geographic or physical feature. The same tool covers
initialization (building the world) and live play (world evolution). Presence of
`feature_id` determines whether this is a create or update operation. `dissolved: true`
triggers dissolution.

Handles: terrain, water bodies, rivers, climate zones, geological zones, special
resource zones, physics overrides (dead zones, anti-magic fields, radiation belts),
settlements as geographic anchors, ruins, underground structures.

```python
@tool
def alter_feature(

    # ── Identity ──────────────────────────────────────────────────────────────

    feature_id: str = "",
    # UUID or display_name of existing feature to update.
    # If empty: creates a new feature.
    # If provided and feature exists: updates it.
    # If provided and feature does not exist: creates it with this ID.

    name: str = "",
    # Display name. Required for new named features.
    # Empty = unnamed background feature (valid for terrain fills, etc.)

    feature_type: str = "",
    # Open string. The generation pipeline recognizes these built-in types
    # and applies type-default L0 effects:
    #   "continent"             — land/ocean boundary
    #   "elevation_feature"     — mountain range, hills, plateau, valley, ridge
    #   "water_body"            — lake, inland sea, bay, fjord, oasis
    #   "river"                 — flowing water (LineString geometry)
    #   "terrain_cover"         — forest, grassland, desert, tundra, wetland,
    #                             glacier, scrubland, jungle, swamp
    #   "climate_zone"          — forces climate_override on contained cells
    #   "geological_zone"       — modifies geological_type and tectonic_stress
    #   "ambient_material_zone" — overrides ambient_rare_material flux values
    #   "void_zone"             — suppresses generation (deep ocean, impassable)
    #   "mountain_pass"         — traversable gap through elevation feature
    #   "fault_line"            — geological boundary, affects tectonic_stress
    #   "settlement"            — named population center (city, town, village)
    #   "ruin"                  — former settlement or structure
    #   "underground_region"    — cave system, tunnel network, subsurface zone
    #   "physics_override"      — modifies physical laws in region (see below)
    # Unknown types: stored without L0 effects unless layer_effects provided.

    # ── Location (provide exactly one) ───────────────────────────────────────

    location_absolute: dict = {},
    # { "lat": float, "lon": float, "alt": float (optional) }

    location_relative: dict = {},
    # { "from": str,       # feature_id, entity_id, or reserved:
    #                      # "player_start" | "world_center" |
    #                      # "north_pole"   | "south_pole"
    #   "bearing": float,  # degrees clockwise from north
    #   "distance": float, # world units
    #   "alt_offset": float (optional) }

    location_inside: str = "",
    # feature_id or name — places geometrically inside parent feature

    location_near: list[str] = [],
    # feature_id(s) or name(s) — places adjacent to these

    location_region_hint: str = "",
    # Natural language: "northern coast", "center of continent", "deep ocean"
    # Resolved via world orientation reference frame.

    # ── Geometry ─────────────────────────────────────────────────────────────

    size_preset: str = "",
    # "tiny" | "small" | "medium" | "large" | "massive"
    # Interpreted relative to feature type and planet_radius scale.

    size_radius: float = 0.0,
    size_width: float = 0.0,
    size_length: float = 0.0,
    size_height: float = 0.0,   # vertical extent above surroundings
    size_depth: float = 0.0,    # depth below surface (caves, ocean trenches)
    # All in world units.

    size_shape: str = "",
    # Natural language shape description.
    # "elongated north-south", "crescent shaped", "follows the valley floor"
    # "branching delta", "irregular archipelago"

    outline_vertices: list[dict] = [],
    # Explicit polygon for precise placement.
    # [{ "lat": float, "lon": float }, ...]

    # ── Physical properties ───────────────────────────────────────────────────

    properties: dict = {},
    # Open key-value. Recognized keys:
    #   navigable: bool              — supports water or road transit
    #   passable: bool               — traversable on foot/vehicle
    #   elevation_override: float    — forces elevation at feature center
    #   feeds_into: str              — hydrological target (feature_id or "ocean")
    #   resource_richness: str       — "scarce"|"normal"|"rich"|"exceptional"
    #   climate_override: str        — Köppen-Geiger code, forces climate
    #   hazard_modifier: float       — multiplies base hazard_level in cells
    #   depth: float                 — for water bodies and caves
    #   population: int              — for settlements
    #   tech_level: int              — GURPS TL for settlements
    #   underground: bool            — marks subsurface features
    # All other keys stored as metadata without simulation effect.

    # ── Layer 0 physics effects ───────────────────────────────────────────────

    layer_effects: dict = {},
    # Overrides the type-default L0 cell mutations.
    # Omit to use type defaults. Provide to customize or for unknown types.
    # {
    #   "soil_fertility_modifier":          float,  # multiplier
    #   "hazard_modifier":                  float,  # multiplier
    #   "elevation_offset":                 float,  # added to cell elevation mean
    #   "ambient_material_overrides":       dict,   # material_id → absolute value
    #   "ambient_material_modifiers":       dict,   # material_id → multiplier
    #   "water_table_modifier":             float,  # multiplier
    #   "climate_override":                 str,    # Köppen-Geiger class code
    #   "tectonic_stress_modifier":         float   # multiplier
    # }

    # ── Latent contents (R12, L3 spec §14) ────────────────────────────────────

    contains: dict = {},
    # Items, dormant entities, and myth seeds located at this feature.
    # {
    #   "items":      list[item_id],     # registered item concept_ids
    #   "entities":   list[entity_id],   # typically behavior_mode=DORMANT
    #   "myth_seeds": list[dict]         # same schema as define_rule myth_seeds
    # }
    # If discovery_required is true, all of the above are latent: excluded
    # from query_world_state / get_region() / GM and SM queries, and contained
    # entities do not execute tick logic, until a discover_location delta
    # fires for this feature_id (typically from an exploring entity's action
    # rule). On discovery: items become visible, entities activate, myth_seeds
    # are planted at this location and begin propagating from that point —
    # not from world initialization.

    discovery_required: bool = False,
    # If true, the contains block above is latent until discovered.
    # The feature itself (as a geographic feature, e.g. a ruin on the map)
    # may still be visible — only its contents are hidden.

    # ── Physics override (for feature_type == "physics_override") ────────────

    physics_override: dict = {},
    # Declares non-standard physical laws active within this feature region.
    # These do not run simulation models — they are named field modifiers
    # that the GM can query and that entity rules can reference.
    # {
    #   "override_id": str,                # identifier for rule references
    #   "description": str,                # narrative description for GM
    #   "field_modifiers": {               # L0–L3 fields this overrides
    #     "field_path": { "operation": "set|multiply|add", "value": float }
    #   },
    #   "entity_effects": {                # effects on entities inside
    #     "capability_suppression": list[str],      # capability_ids suppressed
    #     "need_decay_modifier": float,             # speeds all need decay
    #     "rule_suppression": list[str]             # rule_ids suppressed
    #   },
    #   "exception_entities": list[str]    # entity_ids immune to this override
    # }

    # ── Dissolution ───────────────────────────────────────────────────────────

    dissolved: bool = False,
    # If true: dissolves this feature. feature_id required.

    dissolve_scheduled_tick: int = 0,
    # If dissolved=true and this > 0: schedule dissolution for this tick.

    dissolve_gradual_over_ticks: int = 0,
    # If > 0: feature shrinks over this many ticks before final dissolution.

    # ── Relationships ──────────────────────────────────────────────────────────

    part_of: str = "",
    # This feature is a sub-feature of (feature_id or name).

    connected_to: list[str] = [],
    # feature_ids with navigable connections (roads, passes, tunnels).

    feeds_into: str = "",
    # Hydrological downstream target. feature_id or "ocean".

    # ── Generation behavior ───────────────────────────────────────────────────

    anchor_strength: str = "preferred",
    # "suggestion" | "preferred" | "fixed"
    # Applies only when called before the generation pipeline runs.
    # suggestion: may shift significantly for coherence
    # preferred:  minor adjustments only
    # fixed:      exact placement guaranteed; seeds generation around it

    # ── Change attribution ────────────────────────────────────────────────────

    cause: str = "",
    # Narrative reason for the change. Written to event log.
    # "centuries of farming", "volcanic activity", "magical corruption",
    # "world initialization"

) -> dict:
    # Returns:
    # {
    #   "feature_id": str,
    #   "name": str,
    #   "operation": "created" | "updated" | "dissolved",
    #   "geometry_type": str,
    #   "center": { "lat": float, "lon": float },
    #   "bounding_box": dict,
    #   "cells_affected": int,
    #   "layer_effects_applied": { "cells_modified": int, "fields_written": list[str] },
    #   "relationships": {
    #     "contained_by": str | null,
    #     "contains": list[str],
    #     "adjacent_to": list[str]
    #   },
    #   "event_log_id": str | null,    # null for pure init calls before generation
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts/updates/dissolves geometry in feature store. Applies
`layer_effects` to all intersecting cell condition vectors. Updates feature spatial
index. If called before generation, registers as anchor constraint. If called during
live play, inserts event record into event log with `cause` attribution.

---

### Tool 4: `define_entity`

Creates or updates any named entity: individual characters (NPCs, legendary entities),
group entities (military units, swarms), and narrative-persistent entities. This is the
primary tool for introducing any entity that the GM may observe, interact with, or query.

The same tool handles a village blacksmith (simple Tier 1 entity), a dragon (legendary
Tier 3 with immunity declarations and asymmetric combat effects), and a dreadnought
(scale unit with event-driven termination). The `existence_type` parameter determines
which template is applied; explicit parameters override template defaults.

```python
@tool
def define_entity(

    # ── Identity ──────────────────────────────────────────────────────────────

    entity_id: str = "",
    # UUID or display_name. If exists: updates it. If absent: creates new.

    display_name: str = "",
    # Name used in event logs, GM queries, and narrative references.

    archetype_id: str = "",
    # Behavior profile identifier. Entities of the same archetype share a
    # rule set and can be bulk-registered. Used for EPL management.

    existence_type: str = "mortal",
    # Registered existence_type concept_id.
    # Determines need vector template and termination model.
    # Must be registered via define_world_concept first.
    # "mortal" is a built-in default (food, water, shelter, safety needs;
    # need-depletion termination). All others are world-defined.

    tier: int = 1,
    # 1 = need-driven (farmer, guard, animal)
    # 2 = goal-driven (adventurer, merchant, spy)
    # 3 = state-machine HFSM (legendary entities, constructs, dormant systems)

    scale: str = "individual",
    # "individual" | "party" | "unit" | "swarm"
    # unit/swarm: scale_count required (number of constituent members).

    scale_count: float = 1.0,
    # For unit and swarm: number of constituent members.
    # Rules that use entity.scale read this value.

    faction_id: str = "",
    # faction_id this entity belongs to. Affects L2.5 norm lookups and
    # trust matrix resolution in condition expressions.

    military_unit_of: str = "",
    # If nonzero: faction_id whose L2 military allocation this entity
    # represents (R14). When the WM posts a military-domain intent_declaration
    # for this faction (post_intent with intent_type concentrate_military,
    # defend_location, retreat, or pursue_faction), the L2 update sequence
    # (L2/L2.5 §12) automatically issues a matching behavior_mode update to
    # all entities tagged with this field — the WM declares intent once and
    # both the faction's aggregate accounting and this entity's visible
    # movement stay consistent. Usually equal to faction_id but may differ
    # for proxy/mercenary forces.

    narrative_importance: str = "background",
    # "background" | "notable" | "named" | "critical"
    # Controls continuity record depth and contradiction-check strictness.
    # Only "named" and "critical" receive full continuity tracking.

    # ── GURPS sheet (optional — derives simulation defaults) ──────────────────

    gurps_sheet: dict = {},
    # If provided, the system derives stocks, need vector adjustments,
    # immunity declarations, and interface rules from the GURPS sheet.
    # Explicit stocks/rules/interfaces below override GURPS-derived values.
    #
    # {
    #   "st": int, "dx": int, "iq": int, "ht": int,    # base attributes
    #   "advantages": list[str],    # GURPS advantage names, e.g. "Unkillable 1",
    #                               # "Doesn't Eat or Drink", "Regeneration (Fast)",
    #                               # "Magery 3", "Doesn't Breathe", etc.
    #   "disadvantages": list[str], # GURPS disadvantage names
    #   "skills": { "skill_name": int, ... },  # skill name → effective level
    #   "power_level": int,         # for supers/divine: Power level (0–5)
    #   "tech_level": int,          # GURPS TL for this entity
    #   "notes": str                # free text carried into continuity snapshot
    # }
    #
    # Derived mappings (applied unless overridden):
    #   HP = (ST + HT) / 2          → structural_integrity stock max
    #   FP = HT                     → energy_reserve stock max
    #   Unkillable 1/2/3            → termination_condition = "event_driven"
    #   Doesn't Eat/Drink           → removes food/water needs
    #   Regeneration (Fast/Slow)    → structural_integrity recovery flow × 10/2
    #   Magery N                    → entity.energy_reserve max multiplied by N+1;
    #                                 entity.capabilities_unlocked populated from
    #                                 spell list if provided; does NOT map to any
    #                                 faction-level stock or tier
    #   Dependency (resource)       → need added with depletion_consequence="termination"
    #   High Pain Threshold         → structural_integrity depletion_threshold 0.05
    #   Combat Reflexes             → priority +2 on all combat action rules
    #   Doesn't Breathe             → removes air need if present
    #   Injury Tolerance (Homogenous/Diffuse/No Brain) → immunity to specific damage_types

    # ── Stocks (override or extend existence_type template) ───────────────────

    stocks: list[dict] = [],
    # Additional stocks not in the existence_type template, or overrides.
    # Each entry:
    # {
    #   "stock_id":             str,    # unique within entity
    #   "initial_value":        float,  # 0.0–1.0 normalized, or absolute float
    #   "max_value":            float,  # ceiling; 1.0 for normalized
    #   "min_value":            float,  # floor; 0.0 for normalized, -inf allowed
    #   "decay_rate":           float,  # depletion per tick at rest
    #   "recovery_rate":        float,  # recovery per tick when replenishment active
    #   "depletion_consequence": str,   # "seek_resource"|"distress"|"termination"|
    #                                   # "cascade:stock_id"|"none"
    #   "replenishment_source": str,    # "L0"|"L1"|"L2"|"self_carried"|"raid"|"none"
    #   "depletion_threshold":  float,  # distress triggers below this value
    #   "decay_modifiers": list[str]    # condition expressions that modify rate
    # }

    capabilities_unlocked: list[str] = [],
    # Named capability concept_ids this entity already possesses at registration.
    # Entities gain capabilities either by explicit declaration here, or
    # automatically when their faction unlocks the capability (if scope="both")
    # and the entity meets any entity-level prerequisites.
    #
    # Entity-scoped capabilities (scope="entity") require personal prerequisites
    # (knowledge, training time, specific stock thresholds) rather than faction
    # prerequisites. A wizard's spells are entity capabilities. A master engineer's
    # designs are entity capabilities.
    #
    # Capability use costs are deducted from entity stocks (energy_reserve for
    # magical/psionic capabilities, specific material stocks for crafting).
    # The entity must have sufficient stock at the time of use or the capability
    # action rule fails its condition check.
    #
    # standing_effects (registered on the capability concept, see
    # define_world_concept) apply at faction scope regardless of which entity
    # holds the capability — an entity-scope capability with standing_effects
    # affects its owning faction's coefficients (via entity.faction_id) for as
    # long as that entity holds it and is part of that faction.

    inventory: list[str] = [],
    # Registered item concept_ids this entity carries. See L3 specification §13.
    # An item's aura (if declared) is active regardless of who carries it; an
    # item's combat_effects (if declared) apply via L2 Phase 1 pre-engagement
    # injection only while carried by an entity present in the engagement.
    # On entity dissolution, inventory items drop to the entity's last location
    # by default (use the dissolve_entity delta's destroy_inventory flag to
    # destroy non-destructible-false items instead).

    # ── Behavior ──────────────────────────────────────────────────────────────

    behavior_mode: str = "STATIONARY",
    # Initial behavior mode.
    # "STATIONARY" | "PATROL" | "PATH_TO_GOAL" | "GOAL_SEEKING" |
    # "FOLLOWING" | "DORMANT"

    behavior_parameters: dict = {},
    # Mode-specific parameters.
    # PATROL: { "waypoints": [...], "loop": bool, "speed_modifier": float }
    # PATH_TO_GOAL: { "goal_location": str, "path_algorithm": str }
    # DORMANT: { "wake_conditions": list[str], "dormancy_cost_modifier": float }
    # GOAL_SEEKING: { "goal_condition": str, "search_radius": float }

    hfsm_states: list[dict] = [],
    # Tier 3 only: Hierarchical FSM state definitions.
    # Each state:
    # {
    #   "state_id":   str,
    #   "behavior_mode": str,
    #   "behavior_parameters": dict,
    #   "entry_condition": str,   # expression: when to enter this state
    #   "exit_condition":  str,   # expression: when to leave this state
    #   "transitions": [          # explicit transitions to other states
    #     { "to_state": str, "condition": str, "priority": int }
    #   ]
    # }

    # ── Auras ─────────────────────────────────────────────────────────────────

    auras: list[dict] = [],
    # Always-on per-tick effects radiating from entity location.
    # Each aura:
    # {
    #   "aura_id":         str,
    #   "radius":          float,      # world units
    #   "falloff":         str,        # "flat"|"linear"|"inverse_square"
    #   "target_layer":    str,        # "L0"|"L1"|"L2"|"L2_5"|"L3_entities"
    #   "target_field":    str,        # dot-notation field path
    #   "effect_type":     str,        # "modify_rate"|"modify_value"|"suppress_field"
    #   "effect_magnitude": float,     # per tick; can be expression string
    #   "condition":       str,        # optional activation condition
    #   "z_range":         [float, float],
    #   "event_log_frequency": str     # "every_tick"|"on_threshold"|"never"
    # }

    # ── Action rules ──────────────────────────────────────────────────────────

    rules: list[dict] = [],
    # IF condition THEN world_state_delta_list.
    # Each rule:
    # {
    #   "rule_id":        str,
    #   "priority":       int,         # 1–10; higher evaluated first
    #   "cooldown_ticks": int,
    #   "condition":      str,         # expression over entity + world state
    #   "effects":        list[dict],  # see Delta Types in entity_definition_schema.md
    #                                  # plus use_capability (fires a registered
    #                                  # capability, deducting use_cost/use_energy_cost;
    #                                  # gate with entity.capability[id] condition) and
    #                                  # discover_location (reveals a feature's
    #                                  # contains block — see L3 spec §14)
    #   "effect_shape":   str,         # "sphere"|"cylinder"|"cone"|"contact"
    #   "multi_fire":     bool,
    #   "event_log_entry": str,        # template string
    #   "narrative_flag": bool,        # or expression string
    #   "myth_seeds":     list[dict],  # same schema as define_rule myth_seeds.
    #                                  # Plants myths at this entity's location
    #                                  # when the rule fires. Used for entities
    #                                  # whose actions are the source of myths —
    #                                  # a dragon raid generates "dragons_exist"
    #                                  # at the raided location, which then
    #                                  # propagates outward through L2.5.
    #   "causal_parent_trigger": str   # event_type to auto-link causal chain
    # }

    # ── Asymmetric combat interfaces (R2, R3) ─────────────────────────────────

    immunities: list[dict] = [],
    # Declares what this entity is immune to.
    # Each immunity:
    # {
    #   "immunity_type":    str,       # "damage_type_id" or one of:
    #                                  # "lanchester_attrition" — immune to L2
    #                                  #   aggregate combat damage
    #                                  # "need_termination" — cannot die from
    #                                  #   need depletion (must use event_driven)
    #                                  # "physics_override:id" — immune to named
    #                                  #   physics_override
    #   "exception_sources": list[str] # entity_ids or event_types that bypass this
    #                                  # immunity (e.g. "slain_by_named_hero")
    # }
    #
    # Note: GURPS Unkillable 1/2/3 automatically populates immunities when
    # gurps_sheet is provided. Explicit immunities extend the GURPS-derived set.

    pre_engagement_effects: list[dict] = [],
    # Effects injected into L2 combat resolution before Lanchester equations
    # run, whenever this entity is present in the engagement zone.
    # This is how a wizard changes a battle — not by participating in attrition,
    # but by modifying combatant parameters before the aggregate equations run.
    # Each effect:
    # {
    #   "effect_id":     str,
    #   "condition":     str,          # when this entity triggers the effect
    #                                  # e.g. "entity.need[energy_reserve] > 0.3"
    #   "target":        str,          # "friendly_forces"|"hostile_forces"|"all"
    #   "field":         str,          # L2 combat field to modify
    #                                  # e.g. "effective_strength", "morale",
    #                                  #      "attrition_rate_modifier"
    #   "operation":     str,          # "multiply"|"add"|"set"
    #   "value":         float,        # or expression string
    #   "energy_cost":   float,        # stock_id depleted when effect fires
    #   "energy_source": str           # stock that pays the energy_cost
    # }

    termination_condition: str = "",
    # For event-driven termination: expression that fires dissolution.
    # Examples:
    #   "event.type == 'slain_by_named_hero'"
    #   "event.type == 'destroyed_by_wm_declaration'"
    #   "entity.hfsm_state == 'DESTROYED' AND entity.scale < 1"
    # If provided, overrides the existence_type termination model.

    # ── Leadership profile (R11) ──────────────────────────────────────────────
    # What this entity optimizes for when running a faction in Regime B
    # (named leader exists, but WM is not actively directing him this session).
    # L2 autonomous simulation runs with these weights shaping its objective
    # function. Zero LLM cost — compiled once, runs deterministically.

    leadership_profile: dict = {},
    # {
    #   "objective_weights": {         # what L2 optimizes for, 0.0–1.0 each
    #     "military_strength":    float,  # 0 = don't care, 1 = top priority
    #     "resource_security":    float,  # food/water/energy stockpile depth
    #     "territory_extent":     float,  # land/resource area controlled
    #     "knowledge_investment":  float,  # priority on research institution funding
    #     "capability_pursuit":    float,  # priority on pursuing capability unlocks
    #     "population_welfare":   float,  # 0 for lich; high for benevolent ruler
    #     "trade_prosperity":     float,  # merchant republic weights this high
    #     "institutional_stability": float,  # internal political cohesion
    #     # any registered norm_id can also be an objective weight
    #   },
    #   "risk_tolerance": float,       # 0.0–1.0. How aggressively L2 acts under
    #                                  # uncertainty. High = accepts high attrition
    #                                  # for gains. Low = waits, builds stockpiles.
    #   "time_horizon_ticks": int,     # How far ahead L2 planning considers.
    #                                  # Human king: ~200. Lich: 10000.
    #   "constraints": [               # Hard constraints L2 will never violate.
    #     {
    #       "constraint_id": str,
    #       "description":   str,      # WM note
    #       "condition":     str,      # expression: constraint is violated if true
    #       "priority":      int       # 1–10; higher = harder constraint
    #     }
    #   ],
    #   "faction_disposition_modifiers": {  # how fast trust decays/grows per faction
    #     "faction_id": float,         # positive = trust grows faster (ally bias)
    #                                  # negative = trust decays faster (enemy bias)
    #   }
    # }

    # ── Authority declaration (R2, R11) ───────────────────────────────────────
    # Declares which faction(s) this entity leads and at what authority weight.
    # This shapes whose leadership_profile is included in the faction's
    # decision council aggregate. Does NOT switch off L2 — see R11.

    authority_overrides: list[dict] = [],
    # Each entry:
    # {
    #   "faction_id":       str,       # faction this entity has authority over
    #   "authority_weight": float,     # 0.0–1.0; share of council vote weight
    #                                  # 1.0 = sole ruler. 0.4 = powerful lord
    #                                  # among peers. 0.1 = minor council member.
    #   "domain":           str,       # "all" | "military" | "economic" |
    #                                  # "diplomatic" | "religious" | (world-defined)
    #                                  # Restricts which objective weights this
    #                                  # entity's profile influences.
    # }

    # ── Narrative persistence ─────────────────────────────────────────────────

    continuity_depth: str = "none",
    # "none" | "shallow" | "deep"
    # shallow: location, behavior_mode, active_goal, companion_ids
    # deep: all shallow + need state, relationships, recent significant events

    stub_lock_on_conflict: bool = False,
    # If true: entity locked when contradiction check fails. Set true for
    # named and critical entities.

    wm_notify_on_conflict: bool = False,
    # If true: contradiction pushed to WM notification queue immediately.

    query_summary_template: str = "",
    # Format of get_entity_state() summary for this entity.
    # Available variables: all entity state fields.

    # ── Initial placement ─────────────────────────────────────────────────────

    location_absolute: dict = {},
    location_relative: dict = {},
    location_inside: str = "",
    location_near: str = "",
    # Same location block semantics as other tools.
    # Omit to register entity without placing it (useful for historical entities
    # or entities not yet introduced to the world).

    # ── Change attribution ────────────────────────────────────────────────────

    cause: str = "",
    # Reason for creation or update. Written to event log for live-play updates.

) -> dict:
    # Returns:
    # {
    #   "entity_id": str,
    #   "display_name": str,
    #   "operation": "created" | "updated",
    #   "tier": int,
    #   "existence_type": str,
    #   "stocks_registered": list[str],
    #   "capabilities_unlocked": list[str],
    #   "rules_registered": int,
    #   "auras_registered": int,
    #   "immunities_registered": list[str],
    #   "authority_overrides_registered": list[str],
    #   "leadership_profile_compiled": bool,
    #   "gurps_derivations": dict,        # what was derived from GURPS sheet
    #   "validation_errors": list[str],   # empty if clean
    #   "warnings": list[str]
    # }
```

**Side effects:** Creates/updates entity record in EPL. Registers all stocks, rules,
auras, and immunity declarations. If `authority_overrides` provided, registers override
handles in the L2/L2.5 variable registry for the specified faction. If `pre_engagement_effects`
provided, registers in the combat resolution injection table. If location provided,
places entity in EPL position index.

---

### Tool 5: `define_faction`

Creates or updates any collective social structure: kingdoms, guilds, religions, tribes,
corporations, proto-factions, hive minds, machine empires. Handles both high-complexity
civilizations (full L2/L2.5 dynamics) and low-complexity collectives (proto-factions
that blur the line between ecology and society).

```python
@tool
def define_faction(

    # ── Identity ──────────────────────────────────────────────────────────────

    faction_id: str = "",
    # UUID or display_name. Upsert: creates if absent, updates if present.

    name: str = "",
    faction_type: str = "",
    # Open string. Examples: "kingdom", "guild", "religion", "tribe",
    # "corporation", "hive_mind", "machine_collective", "proto_faction"
    # Must be consistent with registered faction_templates if any.

    template_id: str = "",
    # Optional: registered faction_template concept_id.
    # Applies template defaults; explicit parameters below override them.

    # ── Social complexity (R4 — collective ambiguity) ─────────────────────────

    social_complexity: float = 1.0,
    # 0.0–1.0. Controls which simulation layers are active for this faction.
    # 0.0: pure ecology — L1 population counts, no L2/L2.5 dynamics.
    #      Entity rules govern behavior. Suitable for wildlife, simple creatures.
    # 0.3: proto-faction — ecological base + emergent raiding/territory rules.
    #      Named leaders activate L2.5 social context. Suitable for goblins,
    #      primitive tribes, feral constructs.
    # 0.6: emergent civilization — partial L2 dynamics (resource stocks, basic
    #      trade), L2.5 faction dynamics. Suitable for barbarian confederacies,
    #      nomadic empires, early-stage organizations.
    # 1.0: full civilization — all L2/L2.5 dynamics active. Suitable for
    #      kingdoms, guilds, corporations, any mature organized entity.

    complexity_threshold: float = 0.0,
    # If > 0: this faction's social_complexity increases automatically when
    # the faction's population exceeds (complexity_threshold × initial_population).
    # Use to model a proto-faction that becomes a civilization as it grows.
    # 0 = complexity is fixed at social_complexity value above.

    # ── Territory ─────────────────────────────────────────────────────────────

    home_region: str = "",
    # feature_id or location name. Primary territory at founding.

    territory_cells: list[str] = [],
    # H3 cell IDs or feature_ids for explicit territory definition.
    # For underground/subterranean factions: use "underground:feature_id" prefix
    # to reference subsurface territory.

    territory_expansion_rules: list[dict] = [],
    # Rules that drive autonomous territory expansion/contraction.
    # Same format as entity action rules; conditions reference faction state.
    # { "rule_id": str, "condition": str, "effects": list[dict], ... }

    # ── Stocks (extend or override template defaults) ─────────────────────────

    stocks: list[dict] = [],
    # Faction-level resource stocks. These are L2 aggregate stocks.
    # {
    #   "stock_id":      str,    # e.g. "food_stores", "iron_reserves",
    #                            #      "ley_crystal_stockpile", "political_legitimacy",
    #                            #      "ale_reserves", "tunnel_extent"
    #   "initial_value": float,
    #   "max_value":     float,  # 0 = unlimited
    #   "min_value":     float   # floor; 0 for most resources
    # }

    # ── Flows (how stocks change) ─────────────────────────────────────────────

    flows: list[dict] = [],
    # Continuous per-tick resource dynamics.
    # {
    #   "flow_id":       str,
    #   "description":   str,          # WM note
    #   "source":        str,          # "L0:field_path" | "L1:field_path" |
    #                                  # "L2:stock_id" | "external"
    #   "sink":          str,          # same format as source, or "void"
    #   "rate":          float,        # units per tick; or expression string
    #   "rate_modifiers": list[str],   # condition expressions that modify rate
    #   "condition":     str           # optional: flow only active when true
    # }
    #
    # Examples:
    # Food production from L1 ecosystem:
    # { "source": "L1:current_population[crops]", "sink": "L2:food_stores",
    #   "rate": "territory_fertility * 0.01" }
    #
    # Ambient material collection into faction stock:
    # { "source": "L0:ambient_material_flux[ley_crystals]",
    #   "sink": "L2:ley_crystal_stockpile",
    #   "rate": "0.001 * territory_ambient_flux_avg",
    #   "condition": "faction.capabilities_unlocked contains 'crystal_harvesting'" }
    #
    # Dwarf ale production:
    # { "source": "L2:grain_stockpile", "sink": "L2:ale_reserves",
    #   "rate": 0.003, "rate_modifiers": ["faction.population > 500 → rate * 1.5"] }

    # ── Faction-level rules ───────────────────────────────────────────────────

    rules: list[dict] = [],
    # IF condition THEN faction_state_delta_list.
    # Conditions reference faction state variables. Effects modify faction stocks,
    # relationships, spawn entities, or trigger events.
    # Same schema as entity action rules.
    #
    # Examples:
    # Dwarf gate closure:
    # condition: "local.entities[faction==hostile, tier<=2] > 3 AND
    #             faction.political_cohesion > 0.5"
    # effects: [{ "delta_type": "modify_field",
    #             "layer": "L2_5", "field": "faction.surface_gate_status",
    #             "operation": "set", "value": "closed" }]
    #
    # Goblin winter raid:
    # condition: "world.season == 'winter' AND faction.food_stores < 0.3 *
    #             faction.population AND faction.raid_cooldown == 0"
    # effects: [{ "delta_type": "spawn_entity", "archetype_id": "raiding_party" }]

    # ── Social / ideological state ─────────────────────────────────────────────

    ideology_vector: dict = {},
    # Initial position in L2.5 Deffuant opinion space.
    # { "norm_id": float (0.0–1.0), ... }
    # Uses registered norm concept_ids.

    social_structure_type: str = "",
    # Open string: "feudal" | "flat" | "theocratic" | "corporate" |
    # "tribal" | "hive" | "council" | (world-defined)
    # Affects L2.5 authority_hierarchy initialization.

    strata: list[str] = [],
    # Registered stratum concept_ids active in this faction.
    # Each stratum gets norm vector initialization from ideology_vector.
    # Omit to use world-default strata.

    # ── Relationships ──────────────────────────────────────────────────────────

    relationships: dict = {},
    # Initial trust scalar values with other factions, for unstructured
    # background relations. { "faction_id": float (-1.0 hostile to +1.0 allied) }
    # For structured agreements — alliances, vassalage, empire/union membership,
    # trade pacts, patronage — use define_relationship instead (R15). A faction
    # can have both: a trust scalar here for general relations, plus formal
    # define_relationship entries for specific binding agreements.

    # ── Special properties ─────────────────────────────────────────────────────

    # ── Knowledge and capabilities ────────────────────────────────────────────

    knowledge_stocks: dict = {},
    # Initial knowledge values for registered knowledge_domain concepts.
    # { "domain_id": float (0.0–1.0) }
    # 0.0 = no knowledge. 1.0 = fully mastered.
    # Most factions start with partial knowledge in some domains.
    # Example:
    # { "basic_metallurgy": 0.8, "alchemy_intermediate": 0.4,
    #   "siege_engineering": 0.3 }

    capabilities_unlocked: list[str] = [],
    # Named capability concept_ids this faction possesses at founding.
    # Capabilities can also be unlocked automatically during simulation
    # when all prerequisites in the capability definition are met.
    # Example: ["basic_fortification", "iron_smelting", "crossbow_production"]

    institutions: list[dict] = [],
    # Research institutions present at founding.
    # Each provides research_points per tick toward the currently-assigned project.
    # {
    #   "institution_id":   str,       # unique within faction
    #   "type_id":          str,       # registered research_institution concept_id
    #   "name":             str,       # display name ("Royal Academy of Magic")
    #   "quality":          float,     # 0.0-1.0 multiplier on base_capacity
    #   "location":         str,       # feature_id or location name
    #   "staffing":         float,     # fraction of scholar_stratum allocated here;
    #                                  # multiplies effective capacity
    #   "active":           bool       # true = contributing capacity this tick
    # }

    active_research: list[dict] = [],
    # Research projects currently assigned to each institution.
    # This is the assignment state: which institution is working on what.
    # If empty at founding, the faction's L2 decision logic will assign
    # projects automatically based on its leadership_profile objective weights
    # (capability_pursuit high → assigns to projects that unlock capabilities;
    #  knowledge_investment high → assigns to foundational knowledge projects).
    # WM can set explicit assignments here or override via post_intent.
    # {
    #   "institution_id":   str,       # institution doing the work
    #   "project_id":       str,       # research_project being worked on
    #   "progress":         float,     # research_points accumulated so far
    #   "assigned_entity":  str        # entity_id if project requires a named entity
    # }

    tech_level: int = -1,
    # GURPS TL. -1 = use world default from set_world_orientation.

    underground: bool = False,
    # True: faction's primary territory is subsurface.
    # Affects L0 resource access (geological stocks vs. surface stocks),
    # L2 trade accessibility, and L2.5 social context queries.

    # ── Leadership structure (R11) ────────────────────────────────────────────

    council_members: list[dict] = [],
    # The faction's decision council. Each member is an L3 entity whose
    # leadership_profile contributes to the faction's effective decision
    # objective. The effective profile is the weighted average of all members'
    # profiles, weighted by authority_weight within the matching domain.
    #
    # Omit for leaderless factions — L2 uses a default profile derived
    # from social_structure_type.
    #
    # Each entry:
    # {
    #   "entity_id":        str,       # registered entity (or stub display_name)
    #   "role":             str,       # "king" | "lord" | "minister" | "general"
    #                                  # | "high_priest" | (world-defined open string)
    #   "authority_weight": float,     # 0.0–1.0; share of council influence
    #   "domain":           str,       # "all" | "military" | "economic" |
    #                                  # "diplomatic" | (world-defined)
    #   "succession_rule":  str        # "eldest_heir" | "council_election" |
    #                                  # "strongest_claimant" | "none" | (world-defined)
    # }
    #
    # Sole ruler:
    #   [{ "entity_id": "king_aldric", "role": "king",
    #      "authority_weight": 1.0, "domain": "all" }]
    # Feudal court:
    #   [{ "entity_id": "king_aldric",    "role": "king",     "authority_weight": 0.6, "domain": "all" },
    #    { "entity_id": "lord_vorn",      "role": "marshal",  "authority_weight": 0.8, "domain": "military" },
    #    { "entity_id": "merchant_guild", "role": "treasurer","authority_weight": 0.7, "domain": "economic" }]
    # Leaderless stub (runs on social_structure_type defaults):
    #   []

    decision_variance: float = 0.0,
    # 0.0–1.0. Noise added to L2 decision outputs.
    # 0.0 = deterministic (single clear ruler, coherent council).
    # High values model civil conflict, contested succession, incoherent councils.
    # Auto-computed from council member profile disagreement if council_members
    # is provided. Set explicitly to override that computation.

    intent_declarations: list[dict] = [],
    # Active WM/GM directives currently in force for this faction.
    # L2 satisfies these constraints while optimizing everything else normally.
    # The WM declares *what*; the simulation executes *how*.
    # L2 is never switched off — intent_declarations add constraints to it,
    # they do not replace it. See R11.
    #
    # Typically posted via post_intent tool during live play.
    # Include here only for initialization with pre-existing directives.
    #
    # {
    #   "intent_id":        str,
    #   "issuer_id":        str,       # entity_id who issued this; "" = WM direct
    #   "domain":           str,       # "military"|"economic"|"diplomatic"|"all"
    #                                  # (world-defined)
    #   "description":      str,       # narrative description of the order
    #   "target":           str,       # feature_id, faction_id, or location ref
    #   "intent_type":      str,       # "concentrate_military" | "open_trade" |
    #                                  # "cease_expansion" | "prioritize_resource" |
    #                                  # "defend_location" | "pursue_faction" |
    #                                  # (world-defined open string)
    #   "parameters":       dict,      # intent_type-specific parameters
    #   "strength":         float,     # 0.0–1.0; how hard L2 pursues this vs.
    #                                  # other objectives. 1.0 = hard constraint.
    #   "expires_condition": str,      # expression; "" = permanent until
    #                                  # explicitly revoked. Time-based expiry
    #                                  # uses world-time fields, e.g.
    #                                  # "world.day_of_year > 120 AND world.year >= 5"
    #   "revoke_condition": str        # expression: auto-revokes when true
    # }

    # ── Dissolution ───────────────────────────────────────────────────────────

    dissolved: bool = False,
    absorb_into: str = "",           # faction_id to receive territory/members
    dissolve_scheduled_tick: int = 0,

    # ── History ───────────────────────────────────────────────────────────────

    founding_tick: int = 0,
    # When this faction was founded. 0 = current tick.
    # Used for pre-existing factions placed with historical context.

    cause: str = "",

) -> dict:
    # Returns:
    # {
    #   "faction_id": str,
    #   "name": str,
    #   "operation": "created" | "updated" | "dissolved",
    #   "social_complexity": float,
    #   "layers_active": list[str],
    #   "knowledge_domains_initialized": list[str],
    #   "capabilities_unlocked": list[str],
    #   "institutions_registered": int,
    #   "stocks_registered": list[str],
    #   "flows_registered": int,
    #   "rules_registered": int,
    #   "council_members_registered": int,
    #   "intent_declarations_active": int,
    #   "effective_profile_compiled": bool,
    #   "validation_errors": list[str],
    #   "warnings": list[str]
    # }
```

---

### Tool 6: `define_rule`

Creates or updates a global event system production rule — a rule that fires on world
state conditions and applies effects, independently of any single entity or faction.
These are the world's autonomous laws of causality.

```python
@tool
def define_rule(

    rule_id: str,
    # Unique identifier. Upsert: creates if absent, updates if present.

    name: str = "",
    description: str = "",
    # WM notes. description stored for reference.

    condition: str,
    # Boolean expression over world state variables.
    # Uses full condition expression grammar from entity_definition_schema.md.
    # Variable namespaces: L0.cell[field], L2.settlement[field],
    # L2_5.faction[field], world.tick, world.time_since[event_type]
    # Examples:
    #   "L2.settlement.population < 0.1 * L2.settlement.founding_population
    #    AND world.time_since[settlement_founded] > 500"
    #   "L0.cell.ambient_material_flux[ley_crystals] > 0.9 AND
    #    world.time_since[last_ley_surge] > 200"
    #   "L2_5.faction.strength < 0.05 AND L2_5.faction.age > 200"
    #   "L2.trade_route.disrupted AND world.tick % 50 == 0"

    effects: list[dict],
    # World-state delta list. Same delta types as entity action rules:
    # modify_field, transfer_resource, spawn_entity, dissolve_entity,
    # modify_entity_state, trigger_event, notify_wm.

    scope: str = "global",
    # "global"      — evaluated once per tick against world state
    # "per_cell"    — evaluated once per cell per tick (use carefully)
    # "per_entity"  — evaluated once per active entity per tick
    # "per_faction" — evaluated once per faction per tick
    # "per_settlement" — evaluated once per active settlement per tick

    priority: int = 5,
    # 1–10. Higher priority rules evaluated and applied first.

    cooldown_ticks: int = 0,
    # Minimum ticks between firings. Prevents event storms.

    enabled: bool = True,
    # Disabled rules are stored but not evaluated.

    narrative_flag: bool = False,
    # If true: all firings added to GM notification queue.

    myth_seeds: list[dict] = [],
    # Myths planted at the firing location when this rule fires.
    # The myth then propagates autonomously through L2.5 contact networks
    # (see R12 and L2/L2.5 architecture §7). The WM does not need to track
    # who learns about this — propagation is causal from this seed.
    # Each entry:
    # {
    #   "myth_id":      str,    # registered myth concept_id
    #   "stratum_id":   str,    # "" = applies to all strata at the firing location
    #   "conviction":   float,  # initial conviction at the seed location
    #   "radius":       float   # 0 = single location only; >0 = also seeds
    #                           # nearby locations at reduced conviction
    # }

    firing_limit: int = 0,
    # 0 = unlimited. >0: rule automatically disables after N firings.
    # Useful for one-time historical events.

    cause: str = "",

) -> dict:
    # Returns:
    # {
    #   "rule_id": str,
    #   "operation": "created" | "updated",
    #   "scope": str,
    #   "enabled": bool,
    #   "validation_errors": list[str]
    # }
```

---

### Tool 7: `declare_world_state`

Declares high-level narrative facts, historical events, era parameters, prophecies,
and world-building stubs that do not yet have full simulation infrastructure. This is
the WM's primary tool for responding to narrative escalations and for initializing
world history.

Combines and replaces `set_world_narrative_state`, `commit_fact`, and `schedule_task`
into a single coherent declaration tool. The WM declares something true; the tool
handles stubbing, locking, and task scheduling automatically.

```python
@tool
def declare_world_state(

    # ── Era / narrative context ───────────────────────────────────────────────

    era_name: str = "",
    # Current era name. If provided, updates the active era.

    era_parameters: dict = {},
    # Domain-defined parameters for this era.
    # Injected into L2.5 norm propagation as baseline modifiers.
    # { "parameter_name": value, ... }
    # Examples:
    # { "magic_availability": 0.8, "social_mobility": 0.3,
    #   "technological_stagnation": True, "divine_presence": 0.6 }

    # ── Facts (world-building stubs) ──────────────────────────────────────────

    facts: list[dict] = [],
    # Declares world facts that may or may not have full simulation
    # infrastructure yet. Each fact is immediately committed as a stub
    # and scheduled for build-out.
    # Each fact:
    # {
    #   "fact_type":    str,       # "faction_exists" | "location_exists" |
    #                              # "entity_exists" | "historical_event" |
    #                              # "relationship_state" | "resource_exists" |
    #                              # "institution_exists" | (world-defined)
    #   "payload":      dict,      # fact-specific content
    #   "stub_lock":    bool,      # true = block queries until built-out
    #   "deadline_tick": int,      # when build-out must complete; 0 = auto
    #   "narrative_context": str   # why this fact was declared
    # }

    # ── Historical events ─────────────────────────────────────────────────────

    historical_events: list[dict] = [],
    # Places events in the past that explain current world state.
    # Written to event log with past tick values.
    # {
    #   "event_type":   str,
    #   "description":  str,
    #   "tick":         int,        # past tick when event occurred
    #   "location":     str,        # feature_id or name
    #   "participants": list[str],  # entity_ids or faction_ids
    #   "effects":      dict        # what changed in world state as a result
    # }

    # ── Prophecies and conditions ─────────────────────────────────────────────

    prophecies: list[dict] = [],
    # Declared prophecy conditions tracked by the WM.
    # {
    #   "prophecy_id":       str,
    #   "description":       str,
    #   "trigger_condition": str,   # event_type or expression
    #   "effects_on_fulfil": list[dict],  # world_deltas when condition met
    #   "narrative_flag":    bool,
    #   "fulfilled":         bool   # true = mark as already fulfilled
    # }

    # ── Age transitions ───────────────────────────────────────────────────────

    age_transitions: list[dict] = [],
    # Scheduled era transitions.
    # {
    #   "from_era":          str,
    #   "to_era":            str,
    #   "trigger_tick":      int,        # scheduled tick; 0 = condition-based
    #   "trigger_condition": str,        # expression; empty = tick-based only
    #   "transition_effects": list[dict] # world_deltas fired at transition
    # }

    # ── Task scheduling ───────────────────────────────────────────────────────

    deferred_tasks: list[dict] = [],
    # World-building tasks to execute at a future tick.
    # {
    #   "task_type":       str,          # "create_faction" | "place_feature" |
    #                                    # "define_entity" | "generate_history" |
    #                                    # "establish_territory" | (world-defined)
    #   "parameters":      dict,         # same schema as the corresponding tool
    #   "deadline_tick":   int,
    #   "dependencies":    list[str],    # task_ids that must complete first
    #   "priority":        str,          # "low"|"normal"|"high"|"immediate"
    #   "fact_id":         str           # links to a stub from facts[] above
    # }

) -> dict:
    # Returns:
    # {
    #   "era_updated": bool,
    #   "facts_committed": list[str],      # fact_ids created
    #   "historical_events_written": int,
    #   "prophecies_registered": list[str],
    #   "tasks_scheduled": list[str],      # task_ids
    #   "stub_locked_facts": list[str],    # facts that are now query-blocking
    #   "warnings": list[str]
    # }
```

---

### Tool 8: `subscribe_to_events`

Registers the WM's interest in categories of future events. This is the WM's sole
forward-looking mechanism (R13) — he does not advance time, poll, or get invoked on
every player turn. World time advances as a consequence of player turn progression,
driven by the orchestrator. Subscriptions determine which of those advancing events,
if any, involve the WM.

```python
@tool
def subscribe_to_events(

    filters: list[dict],
    # Each filter:
    # {
    #   "filter_id":     str,            # unique identifier for this subscription
    #
    #   "event_types":   list[str],      # registered event_type concept_ids;
    #                                    # empty = any type
    #   "entity_ids":    list[str],      # empty = any entity
    #   "faction_ids":   list[str],      # empty = any faction
    #   "location": {                    # empty = anywhere
    #     "feature_id": str              # or omit for "region_hint": str
    #   },
    #
    #   "field_thresholds": [             # fire when a field crosses a value
    #     { "field_path": str,           # e.g. "L2.faction[iron_throne].military_strength"
    #       "operator": str,             # "<" | ">" | "=="
    #       "value": float }
    #   ],
    #
    #   "event_payload_filters": {        # match against the firing event's
    #                                    # payload fields directly — for
    #                                    # targeting a SPECIFIC project,
    #                                    # capability, myth, or feature rather
    #                                    # than a category or threshold.
    #     "project_id":    str,          # for research_complete events:
    #                                    # fires only when THIS project
    #                                    # completes, by any faction
    #     "capability_id": str,          # for capability_unlocked events
    #     "myth_id":       str,          # for myth conviction threshold crossings
    #     "feature_id":    str,          # for discover_location events
    #     # open dict — any key is matched against the event's payload
    #   },
    #
    #   "severity_min":  float,          # 0.0-1.0; only events at/above this
    #                                    # narrative severity match
    #
    #   "timing":        str,            # "before" | "after"
    #     # "before": the matching event's effects are held pending. The WM
    #     #           is invoked with the event, its proposed effects, and
    #     #           surrounding world context BEFORE commit — the only
    #     #           point at which the WM can influence the outcome, since
    #     #           nothing can be undone afterward (R13). The WM responds
    #     #           by allowing it, modifying its effects, issuing a
    #     #           post_intent / post_entity_directive, or calling
    #     #           resolve_contradiction to declare a different outcome —
    #     #           then the event proceeds and the advance continues.
    #     # "after":  the event commits normally. A notification is queued
    #     #           (query_world_state query_type="notifications") for
    #     #           narrative awareness. No blocking.
    #
    #   "expires_condition": str,        # expression; subscription auto-removes
    #                                    # when true. "" = permanent.
    #   "max_firings":   int             # 0 = unlimited; >0 auto-removes after
    #                                    # N matches
    # }
    #
    # A filter with all fields empty except event_types and timing="before"
    # matches every event of that type, anywhere, always — use narrow filters
    # to avoid pausing the world unnecessarily. A filter with no event_types,
    # no entity/faction/location scoping, and no thresholds is rejected as
    # too broad.

    replace_existing: list[str] = [],
    # filter_ids to remove before adding the new filters above. Allows the
    # WM to redefine a subscription rather than accumulate stale ones.

) -> dict:
    # Returns:
    # {
    #   "active_filters": list[str],     # filter_ids now registered
    #   "removed_filters": list[str],
    #   "validation_errors": list[str],  # e.g. filter too broad
    #   "warnings": list[str]
    # }
```

**Side effects:** Registers filters against the event evaluation step that runs as
part of the L2/L2.5 update sequence (L2/L2.5 §12) whenever world time advances.
`"before"`-timing matches pause only the specific matching event, not the whole
advance — other unrelated events in the same advance proceed normally. If no
subscription matches anything during an advance, the WM is not invoked at all and the
advance completes silently.

---

### Tool 9: `query_world_state`

Read-only. Returns current simulation state, registry contents, world debt status,
WM notification queue, and available variable catalogue. The WM's primary diagnostic
tool before writing condition expressions or flow formulas.

```python
@tool
def query_world_state(

    query_type: str,
    # What to return. Required. One of:
    #
    # "registry"       — list all registered concepts (existence_types, resources,
    #                    strata, norms, damage_types, event_types, faction_templates)
    # "variables"      — list all currently available field paths for condition
    #                    expressions and flow formulas, with current values
    # "world_debt"     — task queue depth, overdue tasks, stub-locked facts
    # "notifications"  — pending WM notification queue (contradictions, narrative
    #                    flags, simulation anomalies)
    # "region"         — geographic features, climate, resources, faction control
    #                    (same as GM get_region but with full detail level)
    # "entity"         — entity state (same as GM get_entity_state + WM fields)
    # "entities"       — roster query (R14): list of entity summaries matching
    #                    filters. Use to discover entity_ids before targeting
    #                    them with post_entity_directive, or to audit results
    #                    of intent propagation. Returns summaries, not full
    #                    state — use "entity" with target_id for full detail
    #                    on one result.
    # "faction"        — faction state (same as GM get_faction_state + WM fields).
    #                    For discoverable research_projects: includes a
    #                    visible_projects list (myth conviction has crossed
    #                    threshold) separate from the full registry, which
    #                    only query_type="registry" with include_wm_detail=true
    #                    exposes in full. Also includes active_relationships
    #                    (R15): list of relationship_ids this faction is a
    #                    party to.
    # "relationships"  — roster query (R15): list of relationship summaries
    #                    matching filters (relationship_type, faction_id as
    #                    a party). Use to discover relationship_ids before
    #                    updating with define_relationship, or to audit
    #                    current alliance/vassalage/trade structure.
    # "events"         — event log query (same as GM get_events)
    # "social_context" — L2.5 norm vectors, trust baselines, and myth_vector
    #                    (myths held by the queried stratum/location with
    #                    conviction levels — see R12)
    # "time"           — current world time and tick
    # "feature"        — feature record from feature store

    # ── Target (depends on query_type) ───────────────────────────────────────

    target_id: str = "",
    # For "entity", "faction", "feature": UUID or display_name.

    region_center: dict = {},
    # For "region", "events", "social_context":
    # { "feature_id": str } or { "lat": float, "lon": float } or
    # { "entity_id": str }

    region_radius: float = 0.0,
    time_range: dict = {},
    # For "events": { "t_start": int, "t_end": int }

    # ── Filters ───────────────────────────────────────────────────────────────

    filters: dict = {},
    # Type-specific filters.
    # For "registry": { "concept_type": str }  — filter by type
    # For "variables": { "layer": str }          — show only this layer's fields
    # For "events": { "entity_id": str, "severity_min": float,
    #                 "effect_types": list[str] }
    # For "notifications": { "priority_min": str, "unread_only": bool }
    # For "entities" (R14): {
    #   "faction_id":          str,    # entities belonging to this faction
    #   "military_unit_of":    str,    # units tagged for a faction's military
    #                                  # allocation (see define_entity)
    #   "tier":                int,    # 1 | 2 | 3
    #   "scale":               str,    # "individual"|"party"|"unit"|"swarm"
    #   "existence_type":      str,
    #   "behavior_mode":       str,    # e.g. "DORMANT" for all sleeping entities
    #   "narrative_importance": str,
    #   "has_capability":      str,    # capability_id
    #   "has_immunity":        str,    # immunity_type
    #   "min_scale_count":     float,
    # }
    # region_center + region_radius (above) further restrict "entities" to
    # entities within that radius.
    # For "relationships" (R15): {
    #   "relationship_type":   str,    # "alliance"|"vassalage"|"trade_pact"|
    #                                  # "patronage"|"non_aggression"|
    #                                  # "marriage_pact"|"tributary"|(world-defined)
    #   "faction_id":          str,    # relationships where this faction is a party
    #   "role":                str,    # only relationships where faction_id has
    #                                  # this role ("patron", "vassal", "suzerain", ...)
    # }

    fields: list[str] = [],
    # Subset of fields to return. Empty = all fields.
    # For "entities": projection over summary fields, e.g.
    # ["entity_id", "display_name", "tier", "location", "behavior_mode"].

    include_wm_detail: bool = True,
    # If true: include WM-only fields (authority_override status, stub locks,
    # immunity registrations, pre_engagement_effect table).

) -> dict:
    # Returns vary by query_type.
    # All responses include:
    # {
    #   "query_type": str,
    #   "world_time": { "tick": int, "year": int, "day_of_year": float },
    #   "result": dict | list,    # type-specific result
    #   "warnings": list[str]
    # }
```

---

### Tool 10: `resolve_contradiction`

Declares authoritative world state going forward. Used when the simulation has
produced a result that contradicts established narrative, or when a `"before"`-timing
subscription (Tool 8) has paused an event and the WM wants to declare a different
outcome than the one about to commit.

This is not a general-purpose state editor. It is for contradiction resolution: the
WM declares what is true now, with a required audit reason. There is no
recomputation — the engine has no rewind (R13). Consequences propagate forward
normally from the declared state, exactly as from any other write.

```python
@tool
def resolve_contradiction(

    contradiction_source: str = "",
    # notification_id from the WM notification queue, or the pending event
    # context supplied by a "before"-timing subscription firing. Empty =
    # proactive declaration with no specific trigger.

    # ── What to override ──────────────────────────────────────────────────────

    override_type: str,
    # "entity_state"    — override current state of a specific entity
    # "faction_state"   — override current state of a faction
    # "pending_event"   — replace the effects of an event a "before"
    #                     subscription has paused, before it commits
    # "world_field"     — override a specific L0-L2.5 field value
    # "entity_location" — relocate an entity to resolve a movement contradiction

    target_id: str,
    # entity_id, faction_id, event_id (for pending_event), or feature_id
    # depending on override_type.

    # ── Declared truth ────────────────────────────────────────────────────────

    declared_state: dict = {},
    # For entity_state: { field_path: new_value, ... }
    # For faction_state: { field_path: new_value, ... }
    # For pending_event: { "effects": list[dict] }  — replaces the event's
    #   proposed effects entirely before commit. Empty list = the event is
    #   suppressed (does not fire at all).
    # For world_field: { "field_path": str, "value": float, "region": str }
    # For entity_location: { "lat": float, "lon": float, "alt": float }

    # ── Attribution ───────────────────────────────────────────────────────────

    narrative_reason: str,
    # Why this declaration was made. Required. Written to event log and
    # stored for auditing.
    # "King personally led the northern push — simulation underestimated
    #  command quality modifier"
    # "Dragon survived the siege — Unkillable advantage not correctly applied"

    release_stub_lock: bool = True,
    # If the target was stub-locked, release the lock after resolution.

) -> dict:
    # Returns:
    # {
    #   "resolution_id": str,
    #   "override_type": str,
    #   "target_id": str,
    #   "fields_overridden": list[str],
    #   "stub_lock_released": bool,
    #   "event_log_id": str,
    #   "warnings": list[str]
    # }
```

---

### Tool 11: `post_intent`

Issues a time-bounded directive from a named entity (or the WM directly) to a faction's
L2 decision engine. This is the primary live-play tool for active NPC/WM direction of
faction behavior. L2 is not switched off — it adds a constraint to the existing objective
function and continues to optimize everything else autonomously.

Designed for fast use during play: one call, one directive, the simulation handles
execution. The WM declares intent; L2 handles logistics.

```python
@tool
def post_intent(

    faction_id: str,
    # Which faction receives this directive.

    intent_type: str,
    # What the directive is. Open string validated against registered event_types.
    # Common built-in types:
    #   "concentrate_military"  — move military assets toward a target location
    #   "defend_location"       — prioritize defense of a specific feature
    #   "pursue_faction"        — increase pressure against a target faction
    #   "open_trade"            — establish or expand trade with a target
    #   "cease_expansion"       — halt territorial expansion in a domain/direction
    #   "prioritize_resource"   — concentrate extraction on a specific resource
    #   "purge_internal"        — internal stability action (removes L2.5 dissent)
    #   "retreat"               — withdraw military assets from a region
    #   "negotiate"             — open diplomatic channel with target faction
    # Any world-defined string accepted if registered as an event_type concept.

    description: str,
    # Narrative description of the order as it would be given in-world.
    # "The king commands the northern legions to march on Irongate immediately."
    # "The lich orders all undead forces to converge on the Sunken Citadel."
    # Written to event log as an attributed declaration.

    target: str = "",
    # feature_id, faction_id, entity_id, or location name.
    # The geographic or political target of the directive.

    parameters: dict = {},
    # Intent-type-specific parameters.
    # concentrate_military: { "urgency": float, "force_fraction": float }
    # defend_location: { "minimum_garrison": float }
    # prioritize_resource: { "resource_id": str, "extraction_boost": float }
    # open_trade: { "goods": list[str], "terms": str }
    # (world-defined intent_types define their own parameter schemas)

    issuer_id: str = "",
    # entity_id of the NPC issuing this order.
    # Empty = WM direct declaration (no NPC attribution).
    # The issuer's authority_weight in this faction determines how strongly
    # L2 prioritizes this intent vs. its baseline objectives.

    strength: float = 0.8,
    # 0.0–1.0. How hard L2 pursues this intent vs. other objectives.
    # 1.0 = hard constraint (L2 will not violate this regardless of cost).
    # 0.5 = strong influence (L2 heavily weights this but may deviate if cost is extreme).
    # 0.2 = soft guidance (L2 leans this way but can override for efficiency).

    domain: str = "all",
    # Which part of L2 this intent affects.
    # "all" | "military" | "economic" | "diplomatic" | (world-defined)

    expires_condition: str = "",
    # Expression over world-time fields; intent auto-removes when true.
    # "" = permanent until revoke_condition fires or explicitly revoked.
    # "world.day_of_year > 200" — expires after a specific point in the year
    # "world.year >= 6"         — expires after a number of years have passed

    revoke_condition: str = "",
    # Expression: intent auto-revokes when this evaluates true.
    # "L2.military_concentration[irongate] > 0.7"  — order fulfilled
    # "entity.need[energy_reserve] < 0.1"           — lich runs low on power
    # "L2_5.faction[target].strength < 0.1"         — enemy faction collapses

    revoke_on_issuer_death: bool = True,
    # If true: intent is automatically revoked if the issuer entity is dissolved.
    # Set false for institutional orders that survive the individual who gave them.

) -> dict:
    # Returns:
    # {
    #   "intent_id": str,
    #   "faction_id": str,
    #   "issuer_id": str | null,
    #   "intent_type": str,
    #   "strength": float,
    #   "expires_condition": str,
    #   "l2_objective_delta": dict,   # how this changed the effective objective weights
    #   "conflicts_with": list[str],  # intent_ids this may conflict with (warning only)
    #   "event_log_id": str
    # }
```

**Side effects:** Inserts intent_declaration into the faction's active intent list.
Recomputes the faction's effective L2 objective function for the affected domain.
Writes attributed event log record with `issuer_id` and `description`. If `issuer_id`
provided, validates that the issuer has `authority_overrides` registered for this
faction in the relevant domain.

---

### Tool 12: `post_entity_directive`

Issues a scoped, live-play directive to a single L3 entity, redirecting its
`behavior_mode`/`behavior_parameters` (and for Tier 3, optionally its HFSM state)
without touching its stocks, rules, immunities, inventory, or any other field. This
is the entity-scale analog of `post_intent` (R14) — the WM declares an order; the
entity's own rules and behavior mode mechanics determine how it is carried out.

For autonomous or wild entities (a dragon with its own agenda, a dormant guardian),
this is a *proposed* directive, not an unconditional teleport: an entity's own
`rules[]` may include a high-priority rule that overrides or ignores the directive
under specific conditions (e.g. a dragon guarding its hoard ignores a `move_to`
directive while `entity.need[hoard_guard]` is below threshold). The WM should expect
that issuing a directive to a wild entity is a request the entity's established
character may decline, exactly as `post_intent` does not unconditionally override a
faction's leadership_profile but adds a constraint to it.

```python
@tool
def post_entity_directive(

    entity_id: str,
    # Target entity. Use query_world_state(query_type="entities") to discover
    # entity_ids if unknown.

    directive_type: str,
    # "move_to"        — set behavior_mode=PATH_TO_GOAL toward target
    # "patrol"         — set behavior_mode=PATROL over target (a route or area)
    # "guard"          — set behavior_mode=STATIONARY at target with drift_radius
    # "follow"         — set behavior_mode=FOLLOWING toward target entity_id
    # "attack"         — sets a high-priority temporary rule targeting the
    #                    entity/faction at target for this entity's combat rules
    # "use_capability" — fires a use_capability delta for the named capability
    #                    in parameters, if entity.capability[id] is true
    # "retreat"        — set behavior_mode=PATH_TO_GOAL toward entity's
    #                    registered home/anchor location
    # "wake"           — for DORMANT entities: force wake_conditions check now
    # (world-defined open string for other directive types)

    target: str = "",
    # feature_id, location name, or entity_id, depending on directive_type.
    # For "patrol": a list of waypoint feature_ids/names, comma-separated or
    # provided via parameters.waypoints.

    parameters: dict = {},
    # directive_type-specific parameters.
    # move_to: { "path_algorithm": str, "replan_on_block": bool, "urgency": float }
    # patrol: { "waypoints": list[str], "loop": bool, "speed_modifier": float }
    # guard: { "drift_radius": float }
    # use_capability: { "capability_id": str }

    description: str,
    # Narrative description of the order. Written to event log.
    # "The dragon, provoked, flies toward Irongate."
    # "The squad breaks off pursuit and returns to garrison."

    duration: str = "until_complete",
    # "until_complete" — directive ends when the behavior mode naturally
    #                    terminates (e.g. PATH_TO_GOAL arrives)
    # "permanent"      — becomes the entity's new standing behavior_mode,
    #                    persists until another directive or the entity's
    #                    own rules change it
    # otherwise: an expression (revoke_condition-style) under which the
    #            directive ends and the entity reverts to its prior
    #            behavior_mode/behavior_parameters

    priority_over_own_rules: float = 0.5,
    # 0.0–1.0. How strongly this directive competes with the entity's own
    # behavior-mode-affecting rules. 1.0 = directive wins regardless
    # (use sparingly — for WM-controlled NPCs and tamed/bound creatures).
    # 0.5 = directive applies unless a higher-priority rule on the entity
    # explicitly contests it (the dragon-hoard case above).
    # Low values are appropriate for wild/legendary entities whose
    # established character should be respected.

) -> dict:
    # Returns:
    # {
    #   "entity_id": str,
    #   "directive_type": str,
    #   "applied": bool,            # false if the entity's own rules
    #                               # contested and won (see priority_over_own_rules)
    #   "contested_by_rule": str | null,  # rule_id that overrode this directive,
    #                               # if applied=false
    #   "previous_behavior_mode": str,
    #   "previous_behavior_parameters": dict,
    #   "new_behavior_mode": str | null,
    #   "duration": str,
    #   "event_log_id": str,
    #   "warnings": list[str]
    # }
```

**Side effects:** Evaluates `priority_over_own_rules` against the entity's existing
`rules[]` that would affect `behavior_mode`. If the directive applies, updates
`behavior_mode`/`behavior_parameters` (or fires the HFSM transition / `use_capability`
delta for Tier 3 / `use_capability` directive types) without modifying any other
entity field. Stores `previous_behavior_mode`/`previous_behavior_parameters` so that
`duration`-based reversion can restore them. Writes an attributed event log record.

---

### Tool 13: `define_relationship`

Creates or updates a structured agreement between factions: alliances, vassalage,
empire or union membership, trade pacts, patronage, non-aggression, marriage pacts,
or any world-defined relationship type. The same primitive covers a bilateral alliance
and an empire with one suzerain and many member factions — an empire is simply a
relationship with one `"suzerain"`-role party and several `"member"`-role parties.

This is distinct from the scalar `relationships` trust value on `define_faction`:
trust is a continuous background measure; a `define_relationship` entry is a discrete,
named, binding agreement with its own terms, duration, and effects on L2/L2.5 beyond
what trust alone expresses.

```python
@tool
def define_relationship(

    relationship_id: str = "",
    # UUID or display_name. Upsert: creates if absent, updates if present.

    relationship_type: str,
    # Open string. Examples: "alliance", "vassalage", "empire_membership",
    # "union_membership", "trade_pact", "patronage", "non_aggression",
    # "marriage_pact", "tributary", (world-defined).

    name: str = "",
    # Display name, e.g. "The Silver Concordat", "Empire of the Iron Throne".

    parties: list[dict],
    # { "faction_id": str, "role": str }
    # role: "equal" (bilateral agreements like alliance, non_aggression,
    #        trade_pact between peers)
    #       "patron" | "client" (patronage)
    #       "suzerain" | "vassal" | "member" (vassalage, empire_membership,
    #        union_membership — one suzerain/founder, many vassals/members)
    #       (world-defined roles for other relationship_types)

    # ── Terms ──────────────────────────────────────────────────────────────────

    terms: dict = {},
    # relationship_type-specific. Examples:
    #
    # "alliance":
    # {
    #   "mutual_defense":    bool,   # if a party is attacked, allies'
    #                                # leadership_profile gains a temporary
    #                                # military objective weight increase
    #                                # toward defending them
    #   "shared_intel":      bool,   # parties' myth_vectors propagate to
    #                                # each other at increased propagation_rate
    #   "min_trust_floor":   float   # trust between parties cannot decay
    #                                # below this while the alliance holds
    # }
    #
    # "trade_pact":
    # {
    #   "goods":             list[str],  # mineral/flora_pft/fauna_species
    #                                    # stock_ids covered by this pact
    #   "tariff_modifier":   float,      # multiplies effective trade_value
    #                                    # for these goods between parties
    #   "infrastructure_cap_bonus": float  # added to infrastructure_cap
    #                                    # (L2/L2.5 §3) between parties
    # }
    #
    # "vassalage" / "empire_membership" / "union_membership":
    # {
    #   "tribute_rate":      float,  # fraction of vassal/member stocks
    #                                # flowing to suzerain/founder per step
    #                                # (extends authority hierarchy, L2/L2.5 §10)
    #   "military_obligation": float, # fraction of vassal/member military
    #                                # capacity available to suzerain via
    #                                # post_intent military-domain intents
    #   "autonomy_level":    float   # 0.0-1.0. How much the vassal/member's
    #                                # effective decision profile blends with
    #                                # the suzerain/founder's, reusing the
    #                                # council_members weighted-aggregate
    #                                # mechanism (L2/L2.5 §6) across faction
    #                                # boundaries. 1.0 = fully autonomous
    #                                # (suzerain has no influence on profile,
    #                                # only receives tribute/military_obligation).
    #                                # 0.0 = suzerain's profile fully determines
    #                                # the vassal/member's effective objectives.
    # }
    #
    # "patronage":
    # {
    #   "subsidy_flows": [           # ongoing flows established by this
    #                                # relationship, same schema as define_faction
    #                                # flows but source is the patron's stock
    #     { "flow_id": str, "source": str, "sink": str, "rate": float, ... }
    #   ]
    # }
    #
    # "non_aggression":
    # {
    #   "min_trust_floor":   float   # as in alliance; typically lower
    # }

    # ── Trust effects ─────────────────────────────────────────────────────────

    trust_effect: float = 0.0,
    # One-time trust delta applied to all party pairs on establishment
    # (or on update, if the relationship's terms materially changed).

    ongoing_trust_modifier: float = 0.0,
    # Additive per-update-step trust drift between parties while this
    # relationship is active. Positive = trust grows faster while allied.

    # ── Duration ──────────────────────────────────────────────────────────────

    expires_condition: str = "",
    # Expression over world-time/world-state fields. "" = no automatic expiry.

    revoke_condition: str = "",
    # Expression: relationship auto-dissolves when true. e.g.
    # "L2_5.trust_matrix[party_a][party_b] < 0.1"  — alliance collapses if
    # trust falls too far despite min_trust_floor (e.g. after a betrayal event
    # whose penalty exceeds the floor's protection)

    # ── Dissolution ───────────────────────────────────────────────────────────

    dissolved: bool = False,
    # If true: dissolves this relationship. Effects (subsidy_flows, trust
    # floors, autonomy blending) are removed. Does not retroactively undo
    # trust_effect or accumulated tribute.

    cause: str = "",
    # Narrative reason for creation, update, or dissolution. Written to
    # event log.

) -> dict:
    # Returns:
    # {
    #   "relationship_id": str,
    #   "name": str,
    #   "operation": "created" | "updated" | "dissolved",
    #   "relationship_type": str,
    #   "parties": list[{"faction_id": str, "role": str}],
    #   "trust_effects_applied": dict,    # {"faction_a-faction_b": delta, ...}
    #   "autonomy_blending_active": bool, # true if any party's effective
    #                                     # decision profile now blends with
    #                                     # another's per autonomy_level
    #   "validation_errors": list[str],
    #   "warnings": list[str]
    # }
```

**Side effects:** Inserts/updates/dissolves the relationship record. Applies
`trust_effect` immediately to all party-pair trust values. Registers
`ongoing_trust_modifier`, `min_trust_floor`, `subsidy_flows`, and
`autonomy_level`-based profile blending into the L2/L2.5 update sequence (§12) for
the life of the relationship. For `vassalage`/`empire_membership`/`union_membership`,
extends the suzerain/founder's authority hierarchy (§10) to include the
vassal/member faction as a subordinate node, with `tribute_rate` and
`military_obligation` applied as described.

---

## Part III — Summary Table

| Tool | Auth | Purpose | Simulation Layers Touched |
|---|---|---|---|
| `set_world_orientation` | WM | One-time init: grid, climate, resource types | H3 grid, L0 params, registry |
| `define_world_concept` | WM | Register any world-specific type vocabulary | Registry only |
| `alter_feature` | WM | Create/update/dissolve geographic & physical features | L0 cells, feature store, event log |
| `define_entity` | WM | Create/update any named entity, including legendary | L3 EPL, combat injection table, L2 override handles |
| `define_faction` | WM | Create/update any faction or collective | L2, L2.5, territory, event log |
| `define_rule` | WM | Create/update global event system production rules | Event rule engine |
| `declare_world_state` | WM | Narrative facts, era, prophecies, history, task queue | World debt registry, event log, L2.5 norms |
| `subscribe_to_events` | WM | Register forward-looking filters for re-engagement | Event evaluation step, notification queue |
| `query_world_state` | WM | Read simulation state, registry, notifications, entity/relationship rosters | All layers (read-only) |
| `resolve_contradiction` | WM | Declare authoritative world state going forward | EPL/L2/L2.5 state, event log, stub locks |
| `post_intent` | WM | Issue a directive to a faction's L2 engine | L2 objective function, intent list, event log |
| `post_entity_directive` | WM | Issue a scoped behavior directive to one L3 entity | L3 entity behavior_mode, event log |
| `define_relationship` | WM | Create/update structured inter-faction agreements | L2/L2.5 trust, profile blending, authority hierarchy |

**Total WM tools: 13**

Key design decisions:
- Every create/update pair is unified into a single upsert tool. The WM does not need
  to know or track whether something exists already.
- GURPS vocabulary is a first-class input path in `define_entity`, not a workaround.
- Asymmetric combat actors (wizards, dragons, dreadnoughts) are handled by explicit
  `immunities` and `pre_engagement_effects` blocks, not by special entity types.
- High-authority individuals (kings, commanders) declare `leadership_profile` and
  `authority_overrides` in `define_entity`. L2 is never switched off — it runs with
  the compiled profile shaping its objective function (Regime B) or with an active
  intent constraint added by `post_intent` (Regime C).
- Complex leadership structures (king + lords + parliament, lich + lieutenants) use
  `council_members` in `define_faction`. The effective decision profile is the weighted
  aggregate. High inter-member disagreement produces `decision_variance`.
- Leaderless factions run on a default profile derived from `social_structure_type`.
  Naming a ruler replaces the default smoothly — no hard switch.
- `post_intent` is the live-play workhorse for active NPC direction. One call, zero
  per-tick LLM cost. The simulation handles execution.
- Proto-factions (goblins, primitive tribes) use `social_complexity` to exist on the
  ecology-to-civilization spectrum without forcing a binary choice.
- Physics overrides (anti-magic zones, radiation belts) are `alter_feature` with
  `physics_override` block — a geographic feature with named effects.
- Advanced capabilities (magic, high-tech, alchemy) are modeled as **capability unlocks**
  gated by knowledge stocks + material prerequisites, not as mana harvest flows. The
  `special_resource_tier` field is retired. Knowledge accumulates via research
  institutions; materials are physical stocks; capabilities are binary unlocks.
- GURPS Magery maps to personal `energy_reserve` multiplier and entity-level capability
  unlocks, not to any faction-level harvest rate. A faction's magical capability is
  the aggregate of its named magical entities and its knowledge investments.
- L0 ambient rare materials exist for genuinely diffuse geographic resources only.
  Discrete ore deposits use `ore_type` registration as before.
- Research is a discrete tree of `research_project` concepts with explicit costs,
  prerequisites, and outputs, following the RimWorld/HoI4 pattern. Institutions
  provide capacity that must be assigned to a project; assignment is shaped by
  `leadership_profile` and can be overridden via `post_intent` with
  `intent_type="prioritize_research"`.
- Extraordinary research projects (`discoverable: true`) are invisible to a faction
  until linked `myth` concepts reach conviction threshold for that faction. Myths
  propagate through L2.5 social contact networks from seeds planted by `myth_seeds`
  on rule and entity action firings — the simulation propagates awareness causally
  from what has actually happened, and `get_social_context()` returns a `myth_vector`
  that grounds what a given stratum/location actually knows for narrative use.
- The world has no rewind (R13). World time advances only as a consequence of player
  turn progression, driven by the orchestrator — the WM never advances time himself.
  `subscribe_to_events` is the WM's sole forward-looking mechanism: `"before"`-timing
  filters pause a specific matching event for WM response before it commits (the
  only point at which an outcome can be influenced); `"after"`-timing filters queue
  a notification once committed. No matching subscription means no WM involvement.
  `resolve_contradiction` declares state going forward with a required audit reason
  and performs no recomputation.
- `post_entity_directive` (R14) gives a single L3 entity a scoped behavior order
  without touching its other fields, mirroring `post_intent` at entity scale. Wild or
  legendary entities may contest a directive via their own higher-priority rules,
  controlled by `priority_over_own_rules`. `query_world_state(query_type="entities")`
  provides the entity roster needed to discover targets and audit directive results.
  `military_unit_of` on `define_entity` lets a faction-level military intent
  (`post_intent`) automatically propagate matching `behavior_mode` updates to tagged
  L3 unit entities, keeping aggregate accounting and visible units in sync.
- `define_relationship` (R15) registers structured inter-faction agreements —
  alliances, vassalage, empire/union membership, trade pacts, patronage,
  non-aggression — as a single primitive. An empire is one `"suzerain"`-role party
  plus many `"member"`-role parties; `autonomy_level` extends the `council_members`
  weighted-aggregate decision-profile mechanism (L2/L2.5 §6) across faction boundaries.
  The scalar `relationships` trust dict on `define_faction` remains for unstructured
  background relations; formal agreements use `define_relationship`.
- `event_payload_filters` on `subscribe_to_events` (R15) lets the WM target a specific
  `project_id`, `capability_id`, `myth_id`, or `feature_id` rather than only a broad
  event category or field threshold — "notify me before anyone, anywhere, completes
  this specific research project."
- `canon_constraint` (R15) holds load-bearing world facts against the autonomous
  simulation's natural tendency, either silently (`violation_response="auto_resolve"`,
  applying a pre-authored correction every time) or with guaranteed WM attention
  (`violation_response="subscribe_before"`). `priority` controls strictness from
  always-enforced `"canon"` down to notification-only `"flavor"`. This is how a
  canon-constrained setting (e.g. Forgotten Realms) runs full autonomous simulation
  for most of the world — emergent and potentially surprising — while a deliberately
  small set of load-bearing facts are held regardless of outcome.
- `standing_effects` on a `capability` (R16) are how a researched technology changes
  a faction's baseline coefficients persistently — agricultural production multipliers
  from iron tools, a combat_type transition from gunpowder, a lower
  subsistence_threshold from crop rotation. These are distinct from `unlock_effects`
  (one-time world_deltas fired at the moment of unlock, e.g. spawning new unit types):
  `standing_effects` are re-applied every update step for as long as any faction
  holds the capability, layered on top of that faction's own registered flows,
  demographic rates, and combat parameters.
