# L2 / L2.5 Architecture

*Civilization Metabolism and Social Dynamics*

---

## Design Principles

**Setting agnosticism.** No hardcoded resource names, strata, norms, or capability
types. Every domain concept is registered by the WM. The same machinery runs a medieval
kingdom, a dwarf hold, an undead empire, and a machine collective with different
parameters but identical update logic.

**Computational honesty.** L2 and L2.5 are aggregate layers. They model populations,
resource flows, and social dynamics at civilization scale — not individual decisions.
Individual decisions belong to L3. The boundary is strict: L2 computes *what is
available and what the aggregate tendency is*; L3 entities and WM intent_declarations
determine *what specific choices are made*.

**Capability is earned, not harvested.** Advanced capability — magic, high technology,
alchemy, psionics — results from knowledge investment combined with material acquisition.
It is not a resource flow. A court wizard does not harvest mana; he spent decades
building knowledge, maintains a supply of rare components, and expends personal energy
when casting. A faction's magical capability is expressed by what capabilities it has
unlocked, not by a harvest rate.

**Leadership continuity.** The simulation produces plausible civilizational behavior
without WM attention, and accepts directed overrides without becoming incoherent.
The three-regime model (leaderless / profiled-leader / active-intent) operates without
hard mode switches. See §6.

**Forward-only, player-driven time.** The simulation has no rewind — what has happened
has happened, and `resolve_contradiction` declares state going forward rather than
recomputing the past. World time advances only as a consequence of player turn
progression; the WM does not advance time himself and is not invoked on every advance.
`subscribe_to_events` is his sole forward-looking mechanism, evaluated in §12 step 6.

---

## Layer Map

```
L0 ──► L1 ──► L2 ◄──► L2.5
              │           │
         resource      social
         metabolism    dynamics
              │           │
              └─────┬─────┘
                    ▼
              Query API
```

**L2 (Civilization Metabolism):** physical resource economics — what a civilization
produces, consumes, researches, trades, and fights over. Primarily quantitative.

**L2.5 (Social and Institutional Dynamics):** the social fabric — how populations
organize, what norms govern behavior, who holds authority, how trust and ideology
evolve. Primarily relational and structural.

The two sublayers are tightly coupled: L2 resource stress drives L2.5 social
instability; L2.5 institutional health determines L2 production efficiency and military
effectiveness. They are architecturally separated because their update models differ
and their data structures don't overlap.

---

## Part I — L2: Civilization Metabolism

### §1 Resource Stocks and Flows

Every faction has a **stock bundle** — named float values representing accumulated
resources. Stocks are world-defined (registered via `define_world_concept`). The engine
has no built-in resource names.

**Stock schema:**
```
stock_id:         string (world-defined)
current_value:    float
max_value:        float  (0 = unbounded)
min_value:        float  (default 0.0; negative allowed for deficit stocks)
updated_at_tick:  int
```

Stocks are updated each tick by **flows** — continuous per-tick transfer rates between
a source and a sink. Sources and sinks can be:

- An L0 cell field: `L0:soil_fertility`, `L0:ambient_material_flux[ley_crystals]`
- An L1 ecological field: `L1:current_population[cattle]`
- Another L2 stock on this faction: `L2:grain_stockpile`
- An L2 stock on another faction: `L2:faction[trade_partner].iron_reserves`
- `void` — production from nothing, or consumption to nothing

**Flow schema:**
```
flow_id:          string
source:           string  (field path)
sink:             string  (field path)
rate:             float | expression
rate_modifiers:   list[expression]
condition:        expression | null
```

Rate expressions and conditions use the standard condition expression grammar shared
across all simulation layers.

**Origin of L0/L1 stocks referenced in flows.** Stock and flow definitions frequently
reference `L0:` and `L1:` field paths — `L0:ambient_material_flux[ley_crystals]`,
`L1:current_population[dire_wolf]`. These fields are populated by the WM's
`define_world_concept` registrations of `mineral`/`ore_type` (geological deposits),
`flora_pft` (vegetation populations and their `harvest_yield`), and `fauna_species`
(animal populations and their `drops`). Registration alone is sufficient — the
generation pipeline (for world-init registrations) or the next L1 tick (for live-play
registrations) places these across the grid wherever environmental suitability is
nonzero, without manual cell-by-cell placement. `harvest_yield` and `drops` fields on
these concepts are what connect L1 populations to L2 stocks and to
`research_project.consumed_materials` / `required_materials`.

---

### §2 Population Dynamics

Population is a stock with a nonlinear update model rather than a simple flow.

**Model: Demographic Transition**

```
Δpop = pop × (birth_rate − death_rate)

birth_rate = base_birth × food_adequacy × safety_factor × shelter_factor
death_rate  = base_death × (1 + disease_pressure) × (1 + conflict_stress)

food_adequacy   = min(1.0, food_stores / (pop × subsistence_threshold))
safety_factor   = 1.0 − max(0, hazard_level − defense_rating) × 0.5
conflict_stress = recent_combat_losses / max(1, pop)
disease_pressure = f(density, water_table_depth, sanitation_level)
```

`base_birth` and `base_death` are registered per `existence_type` for L3 entities and
factions, and per `fauna_species` for L1 populations (`fauna_species` may reference
an `existence_type` for need vector/termination semantics while supplying its own
demographic rates). Dwarves breed slowly and live long; goblins breed fast and die
young; undead have `base_birth = 0.0`.

**Migration pressure** is computed separately:

```
migration_out = pop × max(0, resource_deficit − migration_threshold) × 0.01
```

`migration_out` does not directly reduce the population stock. It fires a `migration`
event that spawns a `refugee_group` L3 entity near the faction boundary. That entity
carries the migrant population and seeks a destination via `GOAL_SEEKING` behavior.
This preserves causality: migration leaves a footprint in the event log and can be
intercepted, redirected, or absorbed by adjacent factions.

---

### §3 Trade Model

**Model: Gravity with Infrastructure Cap**

```
raw_flow(i,j) = prosperity_i × prosperity_j / distance(i,j)²
actual_flow   = min(raw_flow, infrastructure_cap(i,j)) × relationship_modifier(i,j)

infrastructure_cap = min(route_quality, border_crossing_capacity)
                    + Σ(active trade_pacts between i,j): terms.infrastructure_cap_bonus
relationship_modifier = f(L2_5.trust_matrix[i][j], diplomatic_status[i][j])
```

`prosperity` is a derived aggregate of a faction's key stocks weighted by registered
`trade_value` parameters, with `trade_value` for goods listed in an active
`trade_pact`'s `terms.goods` multiplied by that pact's `terms.tariff_modifier` for
flows between its parties. Stocks with `trade_value = 0` (political_legitimacy,
social_cohesion, knowledge stocks) never participate in trade automatically.

Trade flows are directional. Each faction exports surplus and imports deficit goods.
The simulation finds bilateral flows that maximize aggregate surplus reduction across
all pairs subject to infrastructure constraints, solved as an LP relaxation per tick.

`route_quality` between factions is computed from the feature store: the simulation
finds the best path between faction centroids through navigable features and sums
quality scores. Roads, rivers, mountain passes, and sea routes all contribute.
Feature degradation from geological events or conflict degrades routes automatically.

**Trade disruption** fires when `actual_flow` drops below 50% of previous tick's
value. This event propagates to L2.5 (relationship pressure) and can trigger entity
spawning (merchants diverting, scouts investigating).

---

### §4 Large-Scale Combat

**Model: Generalized Lanchester with Pre-Engagement Injection**

Two phases run in sequence:

**Phase 1 — Pre-engagement modification.** Before any Lanchester calculation, all
registered `pre_engagement_effects` from L3 entities present in the engagement zone
are applied. A wizard modifies `effective_strength`. A dragon modifies `morale` on
both sides. A dreadnought modifies `attrition_rate_modifier`. These are typed field
mutations on combatant parameter vectors — the Lanchester equations then run on
modified inputs.

**Phase 2 — Lanchester resolution.**

Ranged/modern combat (square law):
```
dA/dt = −k_B × B
dB/dt = −k_A × A
where k_x = lethality_x × morale_x × tech_modifier_x
```

Melee/ancient combat (linear law):
```
dA/dt = −k_B × A × B / max(A,B)
```

`combat_type` per faction defaults from `tech_level` at registration (TL ≤ 4: linear,
TL 5+: square), but is re-evaluated each update step as a coefficient subject to
`standing_effects` (R16, §12 step 2 sub-step 0) — a faction that researches a
capability with `target_type: "combat", field: "combat_type", operation: "set"` (e.g.
`gunpowder_weapons`) transitions from linear to square-law mid-game without the WM
editing the faction directly. Mixed engagements between factions with different
`combat_type` use a blended coefficient.

Integration runs for `engagement_duration_ticks` with adaptive Euler step. Outcome:
`A_final`, `B_final`, attrition events for each side.

**Morale collapse** fires when either side's `morale` stock drops below `rout_threshold`
(registered per `existence_type`; undead have `rout_threshold = 0.0`). A routed force
halves effective strength immediately and enters RETREAT behavior on all L3 units.

**Supply attrition.** Engaged forces draw from their faction's `military_supply` stock
each engagement tick. Supply below `supply_critical_threshold` accelerates morale decay
and reduces lethality. This connects combat to economics without explicit logistics
entities for every army.

**Asymmetric entity immunity.** Entities with `"lanchester_attrition"` in their
`immunities` list are not reduced by Lanchester. They must be eliminated through
specific L3 interaction rules. Non-immune force elements are still affected normally.

**Alliance mutual defense (R15).** If a faction with active engagements is party to
a `define_relationship` of type `"alliance"` with `terms.mutual_defense = true`, each
allied party's effective decision profile (§6) receives a temporary increase to its
military objective weight for the duration of the engagement, biasing their L2
auto-reassignment and `concentrate_military` defaults toward reinforcing the attacked
party. This is a profile shift, not an automatic troop transfer — an ally with other
pressing constraints (a contested `intent_declaration`, low `risk_tolerance`) may
still not send forces, exactly as any other objective-weight effect can be outweighed.

---

### §5 Knowledge, Research, and Capability Unlock

**The three objects:**

*Rare materials* are physically scarce stocks acquired through normal means (mining,
hunting, trade, theft). Geography from L0. No special treatment beyond low abundance.

*Knowledge* is intellectual capital stored in named `knowledge_domain` stocks. It does
NOT accumulate passively. It increases only when a research_project that produces
that domain is completed. It decays very slowly when no maintenance research is active.

*Capability* is a named discrete action a faction or entity can perform, typically
unlocked by completing one or more research_projects.

**The research_project model (RimWorld/HoI4 pattern):**

The WM defines a research tree by registering `research_project` concepts. Each
project has:
- Prerequisite projects that must be completed first (forms the tree)
- A `research_cost` in research_points (the "how long it takes")
- `consumed_materials` (stocks destroyed during the research process)
- `required_materials` (stocks that must be present but are not consumed)
- `required_institution` type, `required_location`, `required_entity` (optional gates)
- `knowledge_outputs` (what domain values increase on completion)
- `capability_unlocks` (what capabilities become available on completion)
- `discoverable` (whether this project is visible only after a linked myth crosses
  conviction threshold for the faction — see §15)

Unlocked capabilities may declare `unlock_effects` (one-time `world_deltas` fired at
the moment of unlock — spawning new unit types, registering a new flow) and
`standing_effects` (persistent coefficient modifiers applied every update step for as
long as any faction holds the capability — see R16 and §12 step 2 sub-step 0). A
single capability commonly has both: unlocking `iron_tooling` might one-time-spawn
an `ironworks` institution (`unlock_effects`) while also permanently multiplying
`mining_extraction` rate by 1.5 for any faction that has it (`standing_effects`).

Research_points accumulate toward a project only when a research_institution is
explicitly **assigned** to it. Assignment is the active decision.

**Research_points and capacity:**

Each `research_institution` provides `base_capacity × quality × staffing_factor`
research_points per tick. The faction's total capacity is the sum across all active
institutions eligible for the assigned project.

```
effective_capacity(institution, project) =
  base_capacity
  × quality_modifier
  × staffing_factor               (fraction of scholar_stratum assigned)
  × PRODUCT of capacity_modifiers (condition expressions)
  × 0.0 if upkeep_materials not met
  × 0.0 if ambient_material_req not met in territory

progress_per_tick = SUM over assigned institutions: effective_capacity
```

When `progress >= research_cost`, the project completes.

**Research assignment (L2 decision logic):**

Each faction with institutions has a research assignment state: which institution is
working on which project. This assignment is an L2 decision variable shaped by the
faction's leadership profile:

- `capability_pursuit` weight high → assigns to projects on the critical path to
  desired capabilities
- `knowledge_investment` weight high → assigns to foundational domain projects
- `resource_security` weight high → assigns to projects that improve production flows

The WM can override this via `post_intent` with `intent_type = "prioritize_research"`:
```
post_intent(
    faction_id = "iron_throne",
    intent_type = "prioritize_research",
    target = "necromantic_ritual_advanced",   # project_id
    description = "The lich commands all academies to focus on the soul-binding ritual",
    strength = 1.0
)
```

This overrides the autonomous assignment for the duration of the intent, without
disabling any other L2 logic.

**Research tree example — a magic academy setting:**

```
[basic_herbalism]
    └─► [intermediate_alchemy]     cost: 200pts, consumes: {rare_herbs: 10}
            └─► [advanced_alchemy] cost: 800pts, consumes: {dragon_bile: 3, moonbloom: 20}
                    └─► [elixir_of_immortality]  cost: 2000pts,
                                                  consumes: {philosopher_stone: 1},
                                                  required_entity: "master_alchemist",
                                                  capability_unlocks: ["brew_immortality_elixir"]

[runic_theory]
    └─► [runic_binding]            cost: 300pts, requires_institution: "mage_circle"
            └─► [city_ward]        cost: 1500pts, required_location: "city_center_altar",
                                    capability_unlocks: ["city_level_ward"]
```

The WM defines this entire tree once at world initialization. The faction's institutions
then work through it based on L2 assignment logic or WM directives.

**Entity-scope research:**

Some projects have `scope = "entity"`. These are researched by a specific individual
rather than by a faction institution. The entity must have a `research_capacity` stock
and be assigned to the project:

```
# A wizard personally researching a new spell
define_entity(
    entity_id = "archmage_volren",
    stocks = [{ "stock_id": "research_capacity", "initial_value": 0.8 }],
    ...
)
```

The entity's `research_capacity` stock depletes as they work (representing time and
mental energy). It recovers during rest (a recovery flow). Entity-scope research is
appropriate for unique spells, personal techniques, and knowledge that cannot be
institutionalized.

**The lich's soul-fusion ritual — complete example:**

```
# 1. WM registers the research tree
define_world_concept("research_project", "soul_theory_foundations",
    parameters={ "research_cost": 500, "knowledge_outputs": [{"domain_id": "necromantic_theory", "value_added": 0.3}] })

define_world_concept("research_project", "soul_binding_intermediate", 
    parameters={ "prerequisites": [{"project_id": "soul_theory_foundations"}],
                 "research_cost": 1200,
                 "knowledge_outputs": [{"domain_id": "necromantic_theory", "value_added": 0.4},
                                       {"domain_id": "soul_theory", "value_added": 0.5}] })

define_world_concept("research_project", "soul_fusion_ritual",
    parameters={ "prerequisites": [{"project_id": "soul_binding_intermediate"}],
                 "research_cost": 5000,
                 "consumed_materials": {"soul_gems": 10000},
                 "required_entity": "ancient_catalyst",        # named artifact entity
                 "scope": "entity",                            # lich does this personally
                 "capability_unlocks": ["soul_fusion_mega_ritual"],
                 "notify_wm_on_complete": True })

# 2. The lich accumulates soul_gems via battlefield rules over centuries
# 3. L2 assigns research capacity toward the tree (or WM posts intent)
# 4. When soul_fusion_ritual completes, capability fires automatically
```
---

## Part II — L2.5: Social and Institutional Dynamics

### §6 Leadership Architecture and the Three Regimes

Every faction has an **effective decision profile** at all times that shapes what L2's
autonomous optimization prioritizes. The three regimes differ only in *where the profile
comes from*.

**Regime A — Leaderless.** No `council_members` registered. Profile derived from
`social_structure_type` defaults:

```
"feudal"     → { military: 0.5, resource_security: 0.6, territory: 0.4,
                  knowledge_investment: 0.2, institutional_stability: 0.7,
                  risk_tolerance: 0.3, time_horizon: 300 }
"tribal"     → { military: 0.6, territory: 0.7, resource_security: 0.4,
                  knowledge_investment: 0.05, institutional_stability: 0.2,
                  risk_tolerance: 0.8, time_horizon: 50 }
"theocratic" → { capability_pursuit: 0.7, knowledge_investment: 0.6,
                  institutional_stability: 0.9, population_welfare: 0.5,
                  risk_tolerance: 0.2, time_horizon: 500 }
"corporate"  → { trade_prosperity: 0.9, resource_security: 0.8,
                  knowledge_investment: 0.5, institutional_stability: 0.7,
                  risk_tolerance: 0.5, time_horizon: 150 }
"hive"       → { military: 0.7, capability_pursuit: 0.7,
                  population_welfare: 0.0, risk_tolerance: 0.9,
                  time_horizon: 2000 }
```

**Regime B — Named leader, not actively directed.** `council_members` registered.
Effective profile is the weighted average of all members' `leadership_profile`
objective_weights, partitioned by domain:

```
effective_weight[objective] =
  Σ(m where m.domain covers objective): weight[m] × profile[m][objective]
  / Σ(same m): weight[m]
```

This runs at zero LLM cost — compiled once at entity registration, evaluated
deterministically each tick. The lich with `time_horizon: 10000` and
`population_welfare: 0.0` will autonomously expand his army and accumulate rare
materials without WM attention. The paranoid king with high `institutional_stability`
weight will purge advisors when stability drops. These behaviors emerge from the
registered profile without narration.

`decision_variance` adds Gaussian noise to the effective profile output per tick.
This models genuinely incoherent leadership (contested succession, council deadlock).
Auto-computed from council member profile divergence if `council_members` is provided.

**Regime C — Active WM/GM direction.** One or more `intent_declarations` in force.
L2 objective function becomes:

```
l2_objective = profile_objectives + intent_constraints

For each active intent:
  if intent.strength == 1.0: add as hard constraint
  else: add as boosted soft objective weighted by (intent.strength × issuer.authority_weight)
```

L2 is never switched off. `post_intent` adds constraints; it does not replace the
system. An intent to "concentrate military at Irongate" does not disable trade,
research, or internal politics — L2 satisfies the constraint while running everything
else.

**Transition between regimes** requires no explicit WM action:
- A → B: `define_entity` + `define_faction council_members`. Profile takes effect next tick.
- B → C: `post_intent`. Constraint added immediately.
- C → B: Intent expires or `revoke_condition` fires. Profile reverts automatically.

**Cross-faction profile blending (R15).** `council_members` aggregates leadership
profiles *within* a faction. `define_relationship` entries of type `"vassalage"`,
`"empire_membership"`, or `"union_membership"` extend the same weighted-aggregate
mechanism *across* faction boundaries via `terms.autonomy_level`:

```
effective_weight[objective] (vassal faction) =
  (1 - autonomy_level) × suzerain_effective_profile[objective]
  + autonomy_level × vassal_own_effective_profile[objective]
```

`autonomy_level = 1.0` means the relationship contributes nothing to the vassal's
profile (it only carries `tribute_rate`/`military_obligation` per §10). `autonomy_level
= 0.0` means the suzerain's effective profile fully determines the vassal's objectives
— the vassal has no independent will. Intermediate values model real autonomy: a
tributary kingdom that mostly governs itself but leans toward its overlord's
priorities under pressure. This computation happens after each faction's own
`council_members` aggregation (§6 above) and before the final effective profile used
in the update sequence (§12 step 5).

---

### §7 Opinion Dynamics and Norm Propagation

**Model: Deffuant Bounded-Confidence at stratum level**

Each stratum in each region has a `norm_vector` — `{norm_id: float}`. Each tick,
strata interact with neighbors (adjacent strata in same region, same stratum in
adjacent regions):

```
For each pair (stratum_a, stratum_b) with contact probability p(a,b):
  For each norm dimension n:
    diff = |norm_a[n] − norm_b[n]|
    if diff < ε[n]:                      # ε registered per norm concept
      shift = μ[n] × (norm_b[n] − norm_a[n])
      norm_a[n] += shift
      norm_b[n] −= shift
```

`ε` (confidence bound) and `μ` (convergence rate) are registered per norm dimension.
High-ε norms spread easily (fashion, surface attitudes). Low-ε norms barely spread
(deep religious convictions, ethnic identity, institutional loyalty).

**Faction ideology as attractor.** A faction's `ideology_vector` applies a gentle
pull on strata within its territory each tick:

```
norm_a[n] += attractor_strength × institutional_health × (ideology[n] − norm_a[n])
```

`attractor_strength` is a small constant (~0.001). A stable, well-governed faction
shapes its population's norms more effectively than a chaotic one.

**Norm → L2 feedback.** Norm values modify L2 parameters. The mapping is
world-defined per norm concept:

```
violence_tolerance high   → rout_threshold lower (populations accept more casualties)
trade_openness high       → infrastructure_cap multiplier for trade flows
authority_deference high  → decision_variance lower (population follows orders)
research_culture high     → research_institution capacity_modifier for all
                             institutions staffed from this stratum
```

**Myth propagation.** Myth conviction values propagate through the same stratum
contact network using an identical Deffuant-style update, with parameters registered
per `myth` concept (`propagation_rate`, `decay_rate`) rather than per norm. See §15.

---

### §8 Trust Networks and Diplomatic State

**Trust is a directed weighted graph.** Each faction pair has two independent trust
values: `trust[A→B]` and `trust[B→A]`.

**Three update mechanisms per tick:**

*Decay:* All trust decays toward neutral (0.5) at a configured rate. Trust without
interaction trends to indifference. Rate is modulated by `faction_disposition_modifiers`
in the leader's `leadership_profile`.

*Event-driven:* Specific events modify trust directly with registered delta values:
trade completed, military incursion, alliance agreement, betrayal, gift, insult.
Betrayal events apply large negative deltas that decay slowly — they are hard to
recover from by design.

*Ideological proximity:* Factions with similar `ideology_vector` values experience
slow upward trust drift. Ideological distance applies constant downward pressure.

*Relationship-derived (R15):* `define_relationship` entries contribute two additional
effects. `ongoing_trust_modifier` is an additive per-step drift applied to all party
pairs while the relationship is active — on top of decay, event-driven, and
ideological mechanisms above. `min_trust_floor` (declared in `terms` for
`relationship_type`s such as `"alliance"` and `"non_aggression"`) clamps the
post-update trust value for party pairs: if the other three mechanisms would push
trust below the floor, it is held at the floor instead. A relationship's
`revoke_condition` can still reference the pre-clamp trajectory (e.g.
`"L2_5.trust_matrix[party_a][party_b] < 0.1"` checks the value the floor is
protecting against, allowing a severe betrayal to dissolve an alliance despite its
own floor).

**Diplomatic status** is derived from current trust value with hysteresis:

```
trust < 0.15  → WAR
0.15–0.30     → HOSTILE
0.30–0.45     → TENSE
0.45–0.55     → NEUTRAL
0.55–0.70     → CORDIAL
0.70–0.85     → FRIENDLY
0.85–1.0      → ALLIED
```

Hysteresis band of ±0.05 prevents oscillation at boundaries.

**Named entity influence.** Diplomat entities can shift trust via action rules:
```
condition: "entity.location.type == settlement AND local.L2_5[faction] == target"
effect: modify_field L2_5.trust_matrix[my_faction][target] += 0.005
```
This is the mechanism for player-driven and NPC-driven diplomacy to affect aggregate
relations without the WM manually editing trust values.

---

### §9 Institutional Stability

**Model: Stability Approximation from Coalition Theory**

Each institution has a `stability_score`:

```
stability = institutional_health × (1 − internal_tension)

institutional_health = resource_adequacy × authority_legitimacy × member_satisfaction
internal_tension     = f(ideology_divergence_among_members,
                         resource_competition_among_members,
                         succession_clarity)
```

`ideology_divergence` is the variance of member entities' `ideology_vector` values.
High divergence among council members produces high tension. `succession_clarity` is
low when no clear succession rule is registered or multiple claimants exist.

**Research institution stability** has an additional factor:

```
research_institutional_health = base_health
                               × scholar_retention_rate
                               × material_supply_adequacy
```

`scholar_retention_rate` drops when the scholar stratum shrinks (war casualties,
emigration, plague). `material_supply_adequacy` drops when materials required for
research are below threshold. Both cascade into slower knowledge accumulation.

**Collapse trigger.** If `stability_score` < `collapse_threshold` for more than
`collapse_patience_ticks`, the institution fires a dissolution event. Effects are
declared in the institution's dissolution rules. A collapsed academy may lose
accumulated knowledge (partial decay of associated knowledge stocks). A collapsed
court may spawn claimant entities and split territory.

**Stability → L2 production.** `institutional_health` directly multiplies L2
production efficiency for all flows associated with that institution. A faction in
civil war (stability 0.1) produces at 10% capacity. This is the economic cost of
instability without separate modeling.

**Research institutions specifically:** `research_institutional_health` multiplies the
knowledge research rate. A destroyed or collapsing academy produces no knowledge flow,
causing knowledge to stagnate and eventually decay in domains with nonzero decay rates.

---

### §10 Authority Hierarchy

**Model: Directed tree with tribute flows**

Power flows from a root node (sovereign authority) downward through layers of
subordinates. Tribute flows upward. The tree is derived from `council_members` and
`social_structure_type` *within* a faction, and extended *across* factions by
`define_relationship` entries of type `"vassalage"`, `"empire_membership"`, or
`"union_membership"` (R15) — a vassal/member faction's own internal authority tree
becomes a subordinate subtree attached at the suzerain/founder faction's root, exactly
as a conquered faction's tree does under the *Conquest* event below. The difference is
provenance: conquest produces this structure as a consequence of Lanchester outcomes;
`define_relationship` produces it as a direct WM declaration (e.g. establishing an
empire at world initialization, or a player-driven vassalage negotiated in play).

**Authority effects on intent propagation.** When `post_intent` is called with an
entity as issuer, the intent propagates downward through the hierarchy in the issuer's
domain. A king's military order is adopted by all vassal lords automatically unless
a vassal's `leadership_profile.constraints` declare a defiance condition. For
military-domain intents, propagation also reaches individually-tracked L3 units via
`military_unit_of` tagging (§12 step 1.5) — the king's order moves both the vassal's
aggregate allocation and any visible unit entities representing it. Across the
cross-faction extension above, an empire's founder issuing a military-domain intent
propagates to member factions' allocations and units up to each member's
`terms.military_obligation` fraction.

**Tribute flows.** Each tier passes `tribute_rate × stock_value` upward per step.
The root node accumulates tribute. Subordinates retain the remainder. Within a single
faction, `tribute_rate` is registered in the `faction_template` for the relevant
`social_structure_type`. Across factions, `tribute_rate` is declared explicitly in
the governing `define_relationship`'s `terms` (§ R15) and flows from the
vassal/member faction's stocks to the suzerain/founder faction's stocks rather than
between stocks of a single faction.

**Modification events.** Four events alter the authority hierarchy:

*Succession:* a council_member entity is dissolved. Its registered `succession_rule`
fires. A new entity (named or stub) is inserted at the same node. The new entity's
`leadership_profile` replaces the dissolved entity's contribution to the effective
decision profile.

*Conquest:* a faction's territory is absorbed. The conquered faction's authority
hierarchy becomes a subordinate subtree of the conqueror's. Their council_members
become vassals with reduced `authority_weight`.

*Coup:* an entity with sufficient military/institutional support displaces the root
node. Requires `stability_score` below `coup_threshold`. The new root's profile
reshapes the effective decision profile immediately. Fires `succession_crisis` event
which may trigger loyalty checks across all vassal entities.

*Relationship change (R15):* a `define_relationship` of type `"vassalage"`,
`"empire_membership"`, or `"union_membership"` is created or dissolved. On creation,
the named member faction's authority tree is grafted as a subordinate subtree at the
suzerain/founder's root, tribute and military_obligation flows begin, and
`autonomy_level` blending (§6) activates. On dissolution, the subtree detaches, the
member faction's effective profile reverts to its own `council_members` aggregation
with no blending, and tribute/military_obligation flows stop. Unlike *Conquest*,
which is a Lanchester-driven consequence, this event is a direct result of the WM
calling `define_relationship`.

---

### §11 Stratum Dynamics and Social Mobility

Strata are population fractions that move between each other based on economic and
social conditions.

```
mobility_flow(a → b) per tick =
  fraction_eligible × mobility_rate[a→b] × opportunity_factor × social_mobility_rate

opportunity_factor = f(economic_gap[a,b], institutional_permeability, prosperity_delta)
```

`mobility_rate[a→b]` is registered in the stratum concept definition. `social_mobility_rate`
is the faction-level parameter shaped by norms (`authority_deference` high → lower
mobility) and institutions (universities increase upward mobility for the stratum they
serve).

**Scholar stratum mechanics.** The scholar stratum deserves explicit attention because
it feeds research institutions. Scholar population grows via:
- Upward mobility from lower strata when `research_culture` norm is high
- Deliberate investment events (WM or entity rules founding schools)

Scholar population shrinks via:
- War casualties (scholars are low-priority military targets but are present)
- Downward mobility when `research_culture` norm falls
- Emigration when faction institutional stability collapses

When scholar_population_fraction falls below `minimum_research_threshold`, all research
institutions enter `reduced_operation` state with halved knowledge production rates.

**Stratum population → L2 production.** Production efficiency for each flow is weighted
by the population fraction in the producing stratum. If the merchant stratum shrinks,
trade production efficiency drops. This connects social dynamics to economic output
without per-individual simulation.

---

### §12 Update Sequence

This sequence runs each time world time advances — driven by player turn progression
via the orchestrator, never by the WM directly (R13). The world has no rewind; all
steps below produce forward-only state changes.

```
1. INTENT CONSTRAINTS COMPUTED
   - Collect active intent_declarations for all factions
   - Evaluate expires_condition and revoke_condition for each; remove expired/revoked
   - Build constrained objective function for each faction

1.5. MILITARY INTENT PROPAGATION (R14)
   - For each active military-domain intent_declaration (concentrate_military,
     defend_location, retreat, pursue_faction):
     - Find all L3 entities with military_unit_of == faction_id
     - Issue a matching behavior_mode/behavior_parameters update to each,
       subject to the same priority_over_own_rules consideration as
       post_entity_directive (an entity's own higher-priority rules may contest)
   - This keeps the faction's L2 aggregate accounting and its visible L3 units
     consistent without a separate WM call per unit

1.6. RELATIONSHIP EFFECTS COMPUTED (R15)
   - For each active define_relationship:
     - Evaluate expires_condition and revoke_condition; dissolve if either is true
       (§10 Relationship change event)
     - Apply ongoing_trust_modifier to all party-pair trust values (§8)
     - Recompute cross-faction profile blending from autonomy_level for
       vassalage/empire_membership/union_membership parties (§6)
     - Apply tribute_rate and subsidy_flows (patronage) as additional flows
       in step 2 below

1.7. CANON CONSTRAINT CHECK (R15)
   - For each registered canon_constraint with priority "canon" or "soft_canon":
     - Evaluate constraint_expression against current state
     - If satisfied: no action
     - If violated (or would be violated by a pending change once steps 2-5
       run): apply violation_response
       * "auto_resolve": apply auto_resolve_template in place of the
         violating change, silently
       * "subscribe_before": ensure a "before"-timing subscription matching
         the violating change is active for this update
     - "soft_canon" allows the first violation through with a notification;
       if still violated after this update, treated as "canon" on the next
   - "flavor"-priority constraints are evaluated in step 6 as notification-only

2. L2 RESOURCE UPDATE
   0. STANDING TECHNOLOGY EFFECTS (R16)
      For each faction, for each capability in capabilities_unlocked with
      registered standing_effects: apply the modifier to the matching
      target_id's coefficient (flow rate, demographic parameter, combat
      parameter, trade parameter, or research capacity) for this step's
      computation only. These do not alter the faction's stored registration
      values — they are applied fresh each step, so dissolving/suppressing a
      capability (e.g. via a physics_override's capability_suppression)
      removes the effect on the very next step with no separate cleanup.
   a. Apply all flows (production, consumption, trade) using current stocks,
      including tribute_rate and subsidy_flows from active relationships (step 1.6)
   b. Run demographic transition (population change)
   c. Propagate migration_out events → spawn refugee_group L3 entities
   d. Run trade gravity model for all faction pairs
   e. Check capability prerequisites for all factions
      - For any capability where ALL prerequisites now met: unlock it
      - Notify WM if notify_wm_on_unlock = true
   f. Run Lanchester for active engagements:
      i.  Collect pre_engagement_effects from L3 entities in zone
      ii. Apply phase-1 modifications to combatant parameters
      iii. Run Lanchester equations for the engagement's configured duration
      iv. Apply attrition to military stocks
   g. Apply L2 production modifier from previous step's L2.5 institutional_health

3. L2.5 SOCIAL UPDATE
   a. Run Deffuant opinion dynamics for all stratum pairs
   b. Apply faction ideology attractor pull on own strata
   c. Recompute norm → L2 modifier mappings
   d. Update trust_matrix (decay + event-driven)
   e. Recompute diplomatic_status for changed pairs
   f. Recompute institutional_stability scores
   g. Recompute research_institutional_health for all research institutions
   h. Fire collapse events for institutions below collapse_threshold
   i. Update authority_hierarchy for succession/coup events
   j. Run stratum mobility flows

4. RESEARCH AND KNOWLEDGE UPDATE
   a. For each active research assignment:
      - Compute effective_capacity for each assigned institution
      - Add capacity to project progress
      - Check if progress >= research_cost:
        * Apply knowledge_outputs to faction/entity knowledge stocks
        * Unlock declared capabilities
        * Apply on_complete_effects
        * Notify WM if notify_wm_on_complete
        * Clear assignment (project complete)
   b. Apply knowledge decay for domains with decay_rate > 0
      where no recent project contributed to that domain
   c. L2 auto-reassignment: for institutions with no current assignment,
      assign next project based on effective decision profile priorities
   d. Log significant knowledge threshold crossings (narrative flags)

5. EFFECTIVE PROFILE RECOMPUTATION
   - Recompute effective decision profile from council_members + intent_declarations
   - Recompute decision_variance from member disagreement

6. SUBSCRIPTION EVALUATION (R13, R15)
   - For each pending state change produced by steps 2-5, check active
     subscribe_to_events filters (WM tool 8), including event_payload_filters
     matching against the change's payload fields (project_id, capability_id,
     myth_id, feature_id, or other world-defined keys)
   - "before"-timing matches: hold this specific change's effects pending;
     invoke the WM with the proposed change and surrounding context. The WM
     responds via resolve_contradiction (override_type="pending_event"),
     post_intent, post_entity_directive, or allows it unmodified. Only then
     does this change commit. Other unrelated changes in this update proceed
     normally — only the matching change is held.
   - "after"-timing matches: change commits normally; queue a notification
   - "flavor"-priority canon_constraints (step 1.7) are evaluated here as
     "after"-timing matches: violation queues a notification, never blocks
   - No matching subscription: change commits normally, WM is not invoked

7. EVENT LOG FLUSH
   - Write all committed L2/L2.5 state changes to the event log with
     world-time attribution
   - Fire narrative flags for changes above significance threshold
```

Steps 2, 3, and 4 run sequentially with a one-step lag between them: L2 runs first on
the previous step's L2.5 state; L2.5 runs on updated L2 state; knowledge runs on
updated L2.5 state. This avoids circular dependencies at negligible cost given the
granularity of a single update pass.

---

### §13 Complexity Scaling

`social_complexity` on `define_faction` determines active subsystems:

```
0.0       Pure L1 ecology. Population counts only. No L2/L2.5 computation.

0.1–0.2   Emergent raiding. L1 population + faction identity + one stock
          (food_pressure). One rule: raid when food_pressure > threshold.
          No knowledge, no research, no L2.5.

0.3–0.4   Proto-faction. Basic stocks and flows. Simple raiding/territory rules.
          L2.5 initialized with one stratum. Named leaders activate profile.
          Suitable for goblin tribes, feral constructs, primitive clans.

0.5–0.6   Emergent civilization. Full L2 stocks, flows, and trade.
          L2.5 with faction identity, norms, simple trust.
          Knowledge system active but no research institutions yet.
          Suitable for barbarian confederacies, nomadic empires.

0.7–0.9   Mature civilization. Full L2 including research tree and capability unlocks.
          Full L2.5 with institutions and authority hierarchy.
          Research institutions operate; L2 assigns projects autonomously.
          WM can define a full research tree for this faction.

1.0       Full complexity. All subsystems active. Complex succession, trade
          networks, full knowledge economy, capability unlock system.
```

`complexity_threshold` enables automatic progression: when population exceeds the
threshold multiple of founding population, `social_complexity` steps up. A goblin tribe
that overruns a region and grows can become a warlord state without WM intervention.

WM should register most background factions at 0.3–0.5 unless narrative-significant.
A world with 5 fully-complex and 25 proto-factions is dramatically cheaper to simulate
than one with 30 fully-complex factions.

---

### §14 Integration Points

**← From L0:**
- `soil_fertility`, `precipitation` → agricultural production flows
- `geological_type`, ore deposit locations → mining flows and material availability
- `ambient_material_flux[material_id]` → ambient material collection flows for
  factions with the relevant capability unlocked
- `hazard_level` → safety_factor in demographic model
- Climate/season → seasonal rate modifiers on all production flows

**← From L1:**
- `current_population[species]` → livestock/crop stocks for agricultural factions
- `encounter_probability` → hazard modifier for frontier territories
- `regeneration_rate` → sustainable yield cap on extraction

**→ To L3:**
- `migration_out` events → spawn `refugee_group` entities
- `military_supply` stocks → supply inputs for military unit entities
- `trust_matrix` → available to entity condition expressions as `local.L2_5[...]`
- `norm_vectors` → shape entity action rule outcomes via condition modifiers
- `institution_collapse` events → spawn claimant/successor entities
- `capability_unlocked` events → enable capability-dependent entity action rules
- `research_complete` events → knowledge stocks updated, capabilities enabled,
  WM notified if flagged; triggers L2 reassignment to next project
- active military-domain `intent_declarations` → behavior_mode updates for L3
  entities tagged `military_unit_of` this faction (§12 step 1.5)

**→ To Query API:**
- `get_region()` → `faction_control`, `settlements`, `social_context`
- `get_faction_state()` → all L2/L2.5 state including knowledge stocks, capabilities,
  and visible_projects (research_projects whose discoverability conditions are met)
- `get_social_context()` → norm_vector, trust_baseline, and myth_vector for
  stratum/location
- `get_events()` → L2/L2.5 events filtered by type, faction, severity
- `query_world_state(query_type="entities")` → filterable roster of L3 entities,
  including by `military_unit_of` and `faction_id`

**← WM intent_declarations (via post_intent):**
- Modify L2 objective function without disabling it
- Evaluated and pruned (expires_condition/revoke_condition) in step 1 of the
  update sequence; propagated to tagged units in step 1.5
- All modifications logged with intent attribution

**← WM subscriptions (via subscribe_to_events):**
- Evaluated in step 6 of the update sequence against all pending state changes
  from steps 2-5, including event_payload_filters matching against payload fields
- "before"-timing matches hold a specific pending change for WM response
  (resolve_contradiction / post_intent / post_entity_directive) before it commits
- "after"-timing matches queue a notification once committed
- No matching subscription → no WM involvement for that change

**← WM relationships (via define_relationship, R15):**
- Evaluated in step 1.6 of the update sequence
- `ongoing_trust_modifier` and `min_trust_floor` modify §8 trust update
- `autonomy_level` for vassalage/empire_membership/union_membership extends §6
  cross-faction profile blending and §10 authority hierarchy
- `tribute_rate` and `subsidy_flows` (patronage) become additional flows in step 2
- `mutual_defense` (alliance) shifts allied factions' effective profiles during
  active engagements (§4)
- `shared_intel` (alliance) increases myth propagation contact probability
  between allied factions' strata (§15)

**← WM canon constraints (via define_world_concept "canon_constraint", R15):**
- Evaluated in step 1.7 of the update sequence, before steps 2-5 produce changes
  that might violate them
- "canon"/"soft_canon": violating changes are corrected (`auto_resolve`) or
  forced through a "before" subscription (`subscribe_before`)
- "flavor": violations are notification-only, evaluated in step 6

---

### §15 Myth Propagation and Knowledge Visibility

**Two epistemic levels.** Every `research_project` concept has a `discoverable` flag.
Non-discoverable projects (the default) represent common technical knowledge and are
visible to all factions regardless of myth state. Discoverable projects represent
extraordinary phenomena — they become visible to a faction only when that faction's
accumulated myth conviction for a linked `myth` concept crosses the registered
`conviction_threshold`.

**Myth state.** A `myth` concept is tracked as a conviction value in [0.0, 1.0],
held independently per (faction, stratum, location) combination. Conviction values
are not global — a myth can be widely believed in one region and entirely unknown
in another.

**Myth seeding.** Myths enter the world only through `myth_seeds` declared on
`define_rule` or on entity action rules. When such a rule fires, it plants a myth at
the firing location:

```
seed_conviction[myth_id][location][stratum_id] = seed.conviction

if seed.radius > 0:
  for each location within radius:
    seed_conviction[myth_id][location][stratum_id] =
      seed.conviction × falloff(distance, seed.radius)
```

No myth exists in the world before some event generates its seed. A `dragon_sighting`
myth does not exist until a dragon entity's action rule fires with a `myth_seeds`
entry — there is no ambient "world lore" that exists independent of simulated events,
except for `initial_locations` the WM explicitly declares at world initialization
(representing pre-existing legends, religious traditions, or historical knowledge).

**Propagation.** Each tick, myth conviction propagates through the same stratum
contact network used for norm propagation (§7), using the myth's registered
`propagation_rate` and `decay_rate` in place of a norm's `ε`/`μ`:

```
For each pair (stratum_a, stratum_b) with contact probability p(a,b):
  conviction_a[myth_id] += propagation_rate × p(a,b) × conviction_b[myth_id]
  conviction_a[myth_id] -= decay_rate × conviction_a[myth_id]
```

Conviction values are clamped to [0.0, 1.0]. Strata with no contact path to the seed
location never receive nonzero conviction.

**Alliance shared intelligence (R15).** If two factions are party to a
`define_relationship` of type `"alliance"` with `terms.shared_intel = true`, the
contact probability `p(a,b)` between their respective strata is increased for myth
propagation specifically (not for norm propagation in §7) — allied factions become
aware of discoverable phenomena affecting each other faster than unallied factions
at equivalent geographic distance.

**Faction-level conviction.** A faction's conviction for a myth is the maximum
conviction across its constituent strata in any controlled location. When this value
crosses a linked myth's `conviction_threshold`, the linked `research_project`(s)
become visible to that faction — they appear in `visible_projects` for
`get_faction_state()` and become eligible for assignment in the research update step
(§12 step 4).

**Effect on capability acquisition.** A discoverable capability cannot be unlocked by
a faction that has not crossed conviction threshold for the myths linked to its
prerequisite research_projects, even if that faction otherwise satisfies the material
and institutional prerequisites. The capability simply does not appear as an option.

**Narrative query.** `get_social_context()` returns `myth_vector` — the set of
`{myth_id: conviction}` pairs for the queried stratum at the queried location. This
is the basis for grounding NPC dialogue: an NPC's awareness of any extraordinary
phenomenon is bounded by the myth_vector of their stratum and location, which in turn
is bounded by what has actually propagated through the simulation from real events.
