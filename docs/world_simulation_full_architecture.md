_Full Architecture_

**AI TRPG World Simulation**

_Causal World Simulation + Epistemic Narrative Hierarchy_

June 8, 2026

# **Core Principles**

Three principles govern the entire system:

- Causal integrity. Every world fact has a causal origin. Entities affect the world as they move through it by writing structured events to world state. No fact is invented at query time if it could have been computed at simulation time.
- Epistemic discipline. Narrative agents narrate only what their information access justifies. Invention is permitted only under explicit escalation, and every invented fact is immediately committed to world state.
- Computational honesty. Expensive machinery runs only where necessary. Entity behavior is defined as authored rule sets, not trained policies. Rules are cheap, deterministic, inspectable, and debuggable.

# **System Map**

The system has two halves separated by a read-only query interface. The World Simulation produces causal state. The Narrative Layer consumes it.

| **Subsystem**    | **Components**                                             | **What It Produces**                                                                                                         |
| ---------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| World Simulation | Layers 0 - 3                                               | Causal world state: terrain, ecosystems, civilizations, social structures, entity positions, behavior footprints, event logs |
| Narrative Layer  | Character Agents, Scene Manager, Game Master, World Master | Story: scene narration, character perspective, GM coherence, WM world authority and async world-building                     |
| Query Interface  | Tool API (read-only except WM write tools)                 | get_region(), get_events(), get_entity_state(), get_faction_state() - filtered, scaled                                       |

_The narrative layer cannot invent world facts. It narrates only what the query interface returns, or what has been escalated to and committed by the World Master. This is not a guideline - it is the architectural constraint that makes world consistency possible._

# **Part I - World Simulation**

**LAYER 0 Substrate Physics**

Autonomous. Runs once at world generation, then on long-cycle ticks for climate drift and domain-defined special resource flux shifts. No entity interaction.

| **Component**         | **Model**                                                         | **Output**                                                                      |
| --------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Terrain               | Diamond-square fractal + tectonic uplift                          | Height map, slope, geological type per cell                                     |
| Climate               | Simplified Koppen-Geiger (latitude + altitude + prevailing winds) | Temperature and precipitation bands per cell                                    |
| Resource distribution | Gray-Scott anisotropic reaction-diffusion                         | Resource deposits, biomass density, water table, special_resource_flux per cell |
| River systems         | D8 flow-accumulation on height map                                | River network, drainage basins, flood plains                                    |

Input: _world seed, map dimensions, tectonic activity level, special resource density parameter._

Output: _per-cell condition vector - elevation, slope, temperature, precipitation, soil_fertility, resource_pools\[\], hazard_level, special_resource_flux\[\]._

**LAYER 1 Ecosystem Budget**

Autonomous. Medium-cycle ticks. Computes carrying capacity and encounter rates from Layer 0 conditions. Populations only - no individual organisms.

| **Component**         | **Model**                                                               | **Output**                                      |
| --------------------- | ----------------------------------------------------------------------- | ----------------------------------------------- |
| Flora capacity        | Logistic growth: K = f(soil_fertility, precipitation)                   | Max sustainable flora biomass per cell per tick |
| Fauna dynamics        | Lotka-Volterra multi-species ODE, capped at 5 active species per cell   | Population counts by species; boom-bust cycles  |
| Encounter probability | Poisson process: rate = population_density \* detection_radius \* speed | P(encounter_type) per cell per tick             |
| Extraction cap        | Sustainable yield = r\*K/4 per cell (logistic max-yield formula)        | Hard ceiling on extraction before depletion     |

Input: _Layer 0 condition vectors._

Output: _per-cell - carrying_capacity\[\], encounter_probability\[\], regeneration_rate\[\], current_population_by_species\[\]._

**LAYER 2 Civilization Metabolism**

Aggregate simulation of civilizations as resource-processing systems. Population-level flows only - no individual entity decisions at this layer.

| **Component**       | **Model**                                                                                                      | **Output**                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Resource accounting | Forrester stock-and-flow system dynamics                                                                       | Resource stocks, production and consumption rates, surpluses and deficits per settlement |
| Population dynamics | Demographic transition model: birth/death = f(food, health, conflict stress)                                   | Population per settlement, growth pressure, migration pressure                           |
| Trade               | Gravity model: flow = GDP_i \* GDP_j / distance^2, capped by infrastructure                                    | Trade flow volumes, route health, settlement prosperity                                  |
| Technology          | Directed tech tree: nodes unlock at resource-threshold conditions                                              | Tech tier, available unit types, production multipliers                                  |
| Large-scale combat  | Lanchester equations: ranged = square law, melee = linear law                                                  | Army attrition rates, battle outcomes, territory changes                                 |
| Magic economy       | special_resource_flux (L0) \* civ_special_resource_tier = production_rate; consumed by registered institutions | Special resource stocks per settlement, institution production capacity                  |

Input: _Layer 1 resource budgets, Layer 2.5 faction and institutional state, civilization definition parameters._

Output: _per-civilization - population, resource_stocks\[\], trade_flows\[\], tech_tier, special_resource_tier, military_strength, demographic_pressure._

**LAYER 2.5 Social and Institutional Dynamics**

Governs how populations self-organize into factions, norms, and institutions. Produces the social context that makes individual entity behavior location- and culture-specific. Updated by Layer 2 stress signals and entity event reports from Layer 3.

| **Component**           | **Model**                                                                                         | **Output**                                                                |
| ----------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Faction formation       | Deffuant bounded-confidence opinion dynamics on population graph                                  | Faction membership maps, ideological distance between factions            |
| Norm propagation        | SIR epidemic model on social contact graph: norms spread as contagions, resistance = entrenchment | Norm vectors per social stratum per region                                |
| Trust network           | Weighted directed graph updated by interaction outcomes, time-decayed                             | Pairwise and group trust scores - used as action rule condition variables |
| Institutional stability | Cooperative game theory coalition formation: stable iff core is non-empty                         | Active institutions, membership, resources, dissolution risk              |
| Authority hierarchy     | Directed tree: authority flows down, tribute flows up                                             | Power structure per region - feudal, theocratic, or flat                  |

Input: _Layer 2 population and resource stress, conflict history, civilization social structure type._

Output: _per-region - faction_map, norm_vectors\[\], trust_matrix, institutions\[\], authority_hierarchy, social_mobility_rate._

_Layer 2.5 outputs are the social context that entity action rules read via condition variables. The same entity archetype (blacksmith, guard, merchant) produces different behavior in different regions automatically - not because the archetype changes, but because local.L2_5 fields resolve differently. No manual per-region entity configuration needed._

**LAYER 3 Entity Persistence Layer (EPL)**

The core of the simulation. Manages every named entity instance. Tracks position, executes behavior ticks that write causal footprints to world state, and manages the fidelity boundary between abstract simulation and full-detail narrative zones.

## **3A - Entity Definition and Behavior Engine**

Each entity is defined by a schema authored by the GM or WM (see Entity Definition Schema document). The schema compiles to a rule set that the behavior tick engine executes. There is no training, no policy network, and no inference at runtime - only compiled rule evaluation against world state variables.

| **Schema Section**    | **Content**                                                                          | **Used By**                                                                 |
| --------------------- | ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| Identity              | Archetype, tier, existence type, scale, faction, narrative importance                | EPL registration, query API, continuity records                             |
| Need vector           | Per-need decay rates, depletion consequences, replenishment sources                  | Behavior tick engine: need update each tick; termination check              |
| Behavior mode         | Movement pattern: STATIONARY, PATROL, PATH_TO_GOAL, GOAL_SEEKING, FOLLOWING, DORMANT | Pathfinding and position update each tick                                   |
| Passive aura effects  | Always-on per-tick world-state mutations radiating from entity location              | Behavior tick engine: aura application each tick regardless of action rules |
| Action rules          | IF condition_expression THEN world_state_delta_list                                  | Behavior tick engine: rule evaluation each tick; event log writes           |
| Narrative persistence | Continuity depth, observation triggers, contradiction check fields                   | EPL continuity records; query API response shaping                          |

## **3B - Behavior Tick Execution**

Each simulation tick, for every active entity, the behavior tick engine runs the following sequence:

- Update position along behavior mode path. Replan if blocked.
- Apply need vector decays. Apply any decay modifiers triggered by current location or state.
- Apply passive aura effects at current location and radius. Write aura mutations to world state.
- Evaluate action rules in priority order. For each rule whose condition is true and cooldown has elapsed: apply world_state_deltas, write event log entry, set narrative_flag if configured.
- Check termination conditions. If any critical need at depletion consequence = termination: dissolve entity, write termination event, release resource claims to parent aggregate pool.

_This sequence is the causal footprint mechanism. Every entity writes real world-state mutations as it moves - not narrative descriptions, not probabilistic summaries, but typed field mutations on Layer 0-2.5 state. When the GM later queries get_events(path_Y, time_range), the results are already there because the tick engine wrote them at simulation time._

## **3C - Entity Tiers**

Tiers determine how behavior mode transitions work. They have no effect on the rule execution mechanism - all tiers use the same tick engine.

| **Tier**          | **Examples**                                                                                           | **Behavior Mode Transitions**                                                                | **Need Vector Complexity**                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 - Need-driven   | Farmer, peasant, village animal, town guard                                                            | Manual or event-triggered. Rarely change mode.                                               | Simple: food, water, shelter, safety. Dense decay. Most rules are need-satisfaction seeking.                                                 |
| 2 - Goal-driven   | Adventurer, merchant, spy, army commander                                                              | Goal completion triggers mode change. Active replanning on obstacle.                         | Standard needs plus goal_progress, resource_target. Rules include goal-pursuit and risk-tolerance.                                           |
| 3 - State-machine | Dormant defense system, long-lived apex predator, autonomous construct, distributed swarm intelligence | HFSM: explicit state transitions guarded by conditions. DORMANT state has minimal tick cost. | Custom need vectors defined per existence type. Standard biological needs may be absent. Termination by event condition, not need depletion. |

## **3D - Multi-Scale Group Entities**

| **Group Type**          | **Architecture**                                                                                                                                                           | **Rationale**                                                                                                                                                                |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Party (2-8 individuals) | Each member is a full entity with own need vector and rules. Shared behavior mode: FOLLOWING leader or shared PATH_TO_GOAL. Party-level rules fire on the group as a unit. | Individual agency matters at party scale. Members can defect, get separated, have conflicting needs. Group rules handle coordination without merging identities.             |
| Military unit           | Commander entity at full detail. Rank-and-file as a single aggregate entity with scale parameter and army-specific need vector (food_stockpile, morale, cohesion).         | Soldiers in formation are not individual decision-makers. Commander runs full rule set. Unit is a single behavior tick entity whose scale determines action rule magnitudes. |
| Army (campaign scale)   | Layer 2 Lanchester aggregate. Commander as narrative-persistent Tier 2 entity. Lanchester outcomes feed back to Layer 2 population and resource stocks.                    | Army-scale behavior is genuinely aggregate. Only the commander needs entity-level treatment.                                                                                 |
| Faction                 | Layer 2.5 only. No entity layer representation. Behavior emerges from institutional stability model and member entity actions.                                             | Factions have no body, no location, no perception. They are aggregate social phenomena.                                                                                      |
| Swarm or horde          | Single entity with scale parameter. Rules execute once per cluster with magnitude proportional to scale.                                                                   | A rat swarm or orc horde is one behavior tick entity. One rule set, one position, one resource calculation.                                                                  |

## **3E - Fidelity Management**

Entities near operator-active narrative regions run at full tick resolution. Entities far from active regions run at reduced tick frequency. Fidelity level does not change what rules execute - only how often.

| **Component**       | **Mechanism**                                                                                                                                                                                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Fidelity zones      | Voronoi partition around operator-active regions with hysteresis band. Inner radius: full-frequency ticks. Outer radius: reduced-frequency ticks. Hysteresis prevents oscillation at boundary.                                                         |
| Transition handoff  | On zone entry: entity state snapshot passed to full-frequency execution. On zone exit: state snapshot archived, reduced-frequency execution resumes from current state.                                                                                |
| Provenance tracking | Append-only event log per entity: spawn cause, parent aggregate pool, all resource deductions. Every entity instance has a traceable origin in the aggregate layers. Resource deductions always propagate to parent Layer 2 pool - no double counting. |

## **3F - Narrative-Persistent Entity Tier**

The moment an entity is named or directly observed in narrative, it is promoted to narrative-persistent status. This is an identity continuity guarantee independent of fidelity level.

| **Component**       | **Purpose**                                                          | **Mechanism**                                                                                                                                                 |
| ------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Continuity snapshot | Records everything narratively established at last observation.      | Structured record: stated intentions, emotional register, companions, active goals, known relationships. Created and updated on each narrative observation.   |
| Continuity delta    | Tracks simulation events since last observation.                     | Running list of behavior tick events affecting this entity since snapshot. Checked for contradiction before next query.                                       |
| Contradiction check | Ensures simulation history is consistent with established narrative. | Runs automatically when entity is queried after simulation advance. Flags conflicts to WM task queue for resolution before GM receives the answer.            |
| Stub lock           | Prevents contradicted answers from reaching the narrative layer.     | Entity locked during WM resolution. Queries return 'information unavailable' rather than a contradicted answer. Resolution completes before next observation. |

# **Part II - Event System**

Cross-cutting reactive system embedded in the simulation loop. Not a layer - a production-rule engine that fires on world state conditions at any layer boundary.

| **Property**    | **Specification**                                                                                                                                                                                                                                                                         |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Trigger format  | IF condition_expression THEN effect_list. Conditions reference any world state variable from Layers 0-3.                                                                                                                                                                                  |
| Effect types    | Parameter adjustment, entity spawn or dissolution, Layer 2 stock modification, WM notification, narrative flag for GM attention.                                                                                                                                                          |
| Execution model | Priority queue. Conflicting events resolved by priority value. Cooldown periods prevent event storms.                                                                                                                                                                                     |
| Editability     | Dynamically addable and removable by WM without simulation restart. Primary mechanism for WM to inject world rules without touching entity definitions.                                                                                                                                   |
| Domain examples | IF settlement.population &lt; 10% of founding_population AND settlement.age &gt; 100_ticks THEN spawn(remnant_population_archetype, count=f(original_population)). IF special_resource_flux > threshold AND civ.special_resource_tier >= 3 THEN spawn(resource_anomaly_event), notify_wm. |

# **Part III - Narrative Layer**

The existing player-facing system. Interfaces with the world simulation exclusively through the query API. Three agents operate under a strict epistemic hierarchy.

## **Epistemic Hierarchy**

| **Agent**        | **Information Access**                                                                                                       | **Invention Authority**                                                                                                                      | **Escalation Path**                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Character Agents | Own memory only: personal observations, conversations, explicitly learned facts. Fully sealed.                               | None. Uses ask_manager tool. Cannot assert world facts.                                                                                      | To Scene Manager only.                                                        |
| Scene Manager    | Character perspective filter over GM-provided scene context. Returns only what character's knowledge and perception justify. | None. Cannot assert facts not grounded in GM context or tool results.                                                                        | To GM when world-lore question cannot be answered from character perspective. |
| Game Master      | Full query access to simulation via tool API. Maintains GM-scope narrative memory and event log summaries.                   | Limited inference from tool results. Cannot assert new world facts. Escalates unresolvable queries.                                          | To World Master when a required fact does not exist in simulation state.      |
| World Master     | Full read and write access to world simulation. Maintains World Narrative State vector: ages, prophecies, era parameters.    | Full. Only agent permitted to invent new world facts. All inventions immediately committed to world state and scheduled for async build-out. | None. Terminal authority.                                                     |

## **World Master: Lazy Initialization and World Debt**

When the WM invents a fact to resolve an escalation, it operates in two phases:

- Immediate: declare the fact, commit it as a stub to world state with a reservation lock, return the answer up the escalation chain.
- Async: schedule a task queue of world-building operations with a world-time deadline. Tasks execute as simulation time advances: create_faction(), establish_territory(), populate_members(), assign_relationships(), generate_history_events().

The task queue depth is a measure of world debt - declared facts whose simulation infrastructure is not yet built. World debt is safe as long as each stub has a completion task with a deadline before the next likely query touching that fact.

_World debt grows fastest during early play when the world is sparse. Prioritize completing high-traffic stubs - major cities, named factions, key NPCs - before advancing world time significantly. A debt monitoring view visible to the operator is strongly recommended._

## **Query API**

All narrative agents access world state through a typed query API. Filters are mandatory - raw layer dumps are never returned.

| **Tool**             | **Key Parameters**                                                                                 | **Returns**                                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| get_region()         | location, radius, filters: terrain, resources, settlements, faction_control, hazard_level          | Filtered cell properties and aggregate statistics. No entity data unless entity_filter specified.                                                 |
| get_events()         | location, radius, time_range, filters: entity_id, event_type, faction, severity_min                | Event log entries matching filters. Primary tool for causal footprint queries: what happened along this path, what did this entity do last month. |
| get_entity_state()   | entity_id, fields: location, needs, behavior_mode, active_goal, relationships, continuity_snapshot | Current simulation state for a named entity. Includes continuity snapshot. Blocked if entity stub lock is active.                                 |
| get_faction_state()  | faction_id, fields: territory, strength, norms, relationships, institutional_health                | Layer 2.5 aggregate state for a faction or institution.                                                                                           |
| get_social_context() | location, stratum: peasant, merchant, noble, clergy, military                                      | Norm vectors and trust baselines for a social stratum in a region. Tells GM what behavioral context to expect from locals.                        |
| wm_commit_fact()     | WM only - fact_type, payload, stub_lock, task_queue\[\]                                            | Commits declared fact to world state, places reservation lock, schedules build-out tasks.                                                         |
| wm_schedule_task()   | WM only - task_type, world_time_deadline, dependencies\[\], parameters                             | Adds a world-building task to the async queue.                                                                                                    |

# **Part IV - Simulation Completeness Check**

| **Requirement**                                                    | **Covered By**                                                                                                            | **Status**                                                                                                                                   |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Terrain, climate, and resources feel geographically coherent       | Layer 0: fractal terrain + KG climate + Gray-Scott diffusion                                                              | Covered                                                                                                                                      |
| Ecosystems respond to entity pressure and recover over time        | Layer 1: Lotka-Volterra + logistic growth. Entity extraction deducts from L1 pools via behavior ticks.                    | Covered                                                                                                                                      |
| Civilizations grow, trade, conflict, and collapse                  | Layer 2: stock-and-flow + gravity trade + Lanchester combat + demographic model                                           | Covered                                                                                                                                      |
| Social structures, factions, and norms emerge and evolve           | Layer 2.5: Deffuant opinion dynamics + SIR norm propagation + coalition game theory                                       | Covered                                                                                                                                      |
| Entity behavior is consistent with archetype and local culture     | Entity action rules reading Layer 2.5 norm and trust variables as condition inputs                                        | Covered - same archetype, different behavior by region automatically                                                                         |
| Entities affect the world as they move through it                  | Layer 3B behavior ticks: rule evaluation writes typed world-state mutations along entity paths                            | Covered - the core mechanism; no fact is invented at query time                                                                              |
| Named entities remain consistent between player observations       | Layer 3F narrative-persistent tier: continuity records, contradiction check, stub locks                                   | Covered                                                                                                                                      |
| Magic is a real resource with geographic and economic implications | Layer 0 special_resource_flux + Layer 2 special resource economy + entity special resource as need-vector component       | Covered                                                                                                                                      |
| Non-standard existence types work correctly                        | Tier 3 with custom need vectors. Termination by event condition, not need depletion.                                      | Covered - Tier 3 entities define custom need vectors and event-based termination conditions; no biological assumptions imposed by the engine |
| Groups and armies operate at appropriate scales                    | Party: individual entities with shared mode. Unit: commander + aggregate entity. Army: L2 Lanchester. Faction: L2.5 only. | Covered                                                                                                                                      |
| World facts are causally grounded, not invented at query time      | Epistemic hierarchy: SM/GM narrate only tool-confirmed facts. WM commits before inventing.                                | Covered - architectural constraint, not best-effort policy                                                                                   |
| World can be extended during live play without restart             | WM lazy initialization + async task queue + event system hot-reload                                                       | Covered                                                                                                                                      |
| Divine and supernatural actors beyond resource constraints         | WM sub-agents with direct Layer 0-2 write access via event system. Not entity-layer entities.                             | Covered                                                                                                                                      |
| Historical weight, ages, prophecy                                  | WM World Narrative State vector injecting era parameters into L2.5 norms                                                  | Covered - requires explicit WM persona design per campaign                                                                                   |

_No critical gaps remain. The removal of CMDP and RL training simplifies the architecture without losing any of the behavioral richness those approaches were meant to provide. Behavioral specificity comes from authored rule sets, which are more expressive, more debuggable, and more directly controllable than trained policies._

# **Part V - Implementation Sequence**

Ordered by dependency and risk. Each stage is independently testable and produces observable output before the next begins.

| **Stage**                        | **Deliverable**                                                                                                                                                           | **What It Proves**                                                                                                                            | **Inputs**                                             |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 1 - Behavior tick engine         | Single Tier 1 entity (farmer). Hardcoded flat environment. Need vector, behavior mode, 5 action rules. Behavior tick executes. Events written to log.                     | Tick engine works. Rules evaluate correctly. World-state mutations are typed and logged. Causal footprint is readable.                        | All environment state hardcoded. No simulation layers. |
| 2 - Causal footprint query       | Entity travels path A to B with 3 action rules that fire en-route. get_events(path_A_B) returns the footprint.                                                            | The GM can query what happened along a path and receive real simulation-time events rather than invented narration.                           | Same hardcoded environment.                            |
| 3 - Narrative-persistent entity  | Entity becomes narrative-persistent. Simulate N ticks. Run contradiction check. Query returns continuity-consistent state.                                                | Continuity mechanism works. Named entity remains consistent across a simulation gap.                                                          | Same hardcoded environment.                            |
| 4 - Tier 3 state-machine entity  | One Tier 3 entity definition: custom need vector, DORMANT behavior mode, wake conditions, aura effects, event-triggered termination rule.                                 | Tier 3 entities compile and execute correctly. Dormancy costs minimal ticks. Auras write continuous mutations. Wake on event fires correctly. | Hardcoded environment. No prior stages required.       |
| 5 - Layer 0-1 (world generation) | Terrain, climate, resources, ecosystems run autonomously from seed. Stable across 1000 ticks.                                                                             | World foundation is stable and diverse. Real inputs available to replace hardcoded environment.                                               | None - standalone.                                     |
| 6 - Layer 2 (civilization)       | One civilization metabolizing Layer 1 resources. Population, trade, resource stocks, Lanchester combat.                                                                   | Aggregate dynamics stable. Real resource stocks available for entity consumption deduction.                                                   | Layers 0-1.                                            |
| 7 - Layer 2.5 (social)           | Faction formation and norm vectors for one region. Entity action rule conditions read local.L2_5 fields.                                                                  | Social context modifies entity behavior measurably. Same archetype produces different behavior in different social environments.              | Layers 0-2.                                            |
| 8 - Full EPL (multi-entity)      | Multiple archetypes running simultaneously. Fidelity zone management. Resource deductions propagating to Layer 2.                                                         | EPL manages fidelity transitions correctly. Resource accounting has no double-counting. Inter-entity events (combat, trade) write correctly.  | Layers 0-2.5.                                          |
| 9 - Narrative integration        | Existing player-adventure layer connected via query API. GM uses get_events() and get_entity_state() instead of inventing. SM uses get_social_context() for scene flavor. | Full stack integration. World facts ground narrative. Causal footprints surface in play. Epistemic hierarchy enforced.                        | Full simulation stack.                                 |

_Stage 4 (Tier 3 state-machine entity) is deliberately early and independent of the world generation stages. It is the most structurally distinct entity type and the most common source of behavioral edge cases - dormancy, event-triggered termination, custom need vectors, and aura effects all need to work correctly. Getting this right against a hardcoded environment is cheaper than discovering problems after the full layer stack exists._

_This document supersedes all previous architecture versions. CMDP and RL training have been removed entirely. Entity behavior is defined by the GM-authored Entity Definition Schema, compiled to rule sets, and executed by the Layer 3 behavior tick engine. The behavioral richness previously attributed to trained policies is achieved through authored action rules reading live world-state variables - which is more expressive, fully deterministic, and directly debuggable._