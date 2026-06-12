_Supplement - Layer 3 Event System_

**Spatial-Temporal Event Log and Layer Propagation**

_4D R\*-tree indexing + weighted spatial join for upward effect propagation_

June 8, 2026

# **1\. Problem Statement**

Every entity action rule and passive aura writes effects to the world. These effects need to do two things simultaneously:

- Propagate upward to the aggregate simulation layers (L0-L2.5) so that higher-level dynamics reflect what entities are actually doing.
- Be indexed for later querying, so the GM can ask what affected a given zone during a given time window and receive a complete, causally attributed answer.

Two structural problems make this non-trivial:

- Effect footprints do not align with simulation layer cells. An entity acting in a valley affects parts of several Layer 1 cells in unequal proportions. A shallow-water creature's territory overlaps with a deep-water cell boundary. The correct fraction of each effect must reach each cell.
- The world is genuinely three-dimensional. A deep underground entity has a fundamentally different zone of influence than a surface entity at the same (x, y) coordinate. Collapsing to 2D loses this distinction entirely, and while it may seem like an edge case now, it becomes load-bearing the moment underground civilizations, subsurface aquifers, or stratified aerial zones enter the simulation.

_Both problems are solved by the same architecture: a 4D R\*-tree as the event log, and a weighted spatial join as the upward propagation mechanism. The R\*-tree is the source of truth. The layer mutations are derived projections of it._

# **2\. The 4D R\*-Tree Event Log**

## **2.1 Why R\*-Tree**

The R\*-tree is the standard spatial index for multi-dimensional range queries with mixed point and interval data. The R\* variant (Beckmann et al. 1990) outperforms the original R-tree on query performance for high-overlap datasets, which is exactly what a simulation event log produces - many overlapping effect regions across time.

The four dimensions are:

- x - horizontal west-east coordinate
- y - horizontal north-south coordinate
- z - vertical coordinate (altitude / depth). Surface = 0, positive = above ground, negative = below. Granularity set by world definition - could be meters, abstract levels, or geological strata.
- t - simulation tick. Integer. Every event has a tick range \[t_start, t_end\].

Every entry in the tree is a bounding box in all four dimensions, not a point. This is essential: an aura that is active for 50 ticks has t_start = 200, t_end = 250. A query covering any part of that range returns the aura. A one-shot action rule has t_start = t_end = tick_of_fire.

## **2.2 Event Record Schema**

Each record inserted into the R\*-tree has the following fields:

| **Field**       | **Type**                     | **Description**                                                                                                                                                                                           |
| --------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| event_id        | UUID                         | Unique identifier. Stable across queries. Used for deduplication and causal chain tracing.                                                                                                                |
| entity_id       | UUID                         | The entity whose rule or aura produced this event. Nullable for event system rules not tied to a specific entity.                                                                                         |
| source_id       | string                       | rule_id or aura_id that fired. Combined with entity_id gives full causal attribution: 'army_raid_food_stores on entity ent_003'.                                                                          |
| effect_type     | enum                         | Categorical tag: resource_extraction, population_change, infrastructure_damage, resource_transfer, state_change, spawn, dissolve, special_resource_flux_change, social_norm_shift. Filterable in queries. |
| bbox_x          | \[x_min, x_max\]             | Spatial extent in x. For a point effect: x_min = x_max. For a radius effect: x_min = center_x - radius, x_max = center_x + radius.                                                                        |
| bbox_y          | \[y_min, y_max\]             | Spatial extent in y.                                                                                                                                                                                      |
| bbox_z          | \[z_min, z_max\]             | Spatial extent in z. For a surface-only effect: z_min = z_max = 0. For a volumetric effect (explosion, underground aura): z_min and z_max span the affected depth band.                                   |
| bbox_t          | \[t_start, t_end\]           | Temporal extent. One-shot events: t_start = t_end. Sustained auras: t_start = tick aura became active, t_end = tick aura deactivated or entity dissolved.                                                 |
| delta_field     | string (layer.field_path)    | The specific world-state field this event modifies. E.g. L1.cell\[x,y,z\].population\[deer\], L2.settlement\[id\].food_stores, L2_5.region\[id\].norm_vectors\[violence_tolerance\].                      |
| delta_magnitude | float                        | Total magnitude of effect over the full bbox. Used for proportional distribution during upward propagation. Negative = depletion/damage. Positive = growth/addition.                                      |
| delta_type      | enum: add \| multiply \| set | How delta_magnitude is applied to the target field.                                                                                                                                                       |
| narrative_flag  | bool                         | Whether this event is flagged for GM attention on next query. Set by the action rule definition.                                                                                                          |
| causal_parent   | UUID \| null                 | event_id of the event that caused this one, if any. Enables causal chain traversal: why did this happen? Because that happened.                                                                           |

_The causal_parent field is what allows the GM to answer 'why did the population in zone X decline over the last 30 ticks' with a traceable chain rather than a correlation. Query returns events affecting zone X in that time. Each event has a source_id (which rule) and entity_id (which entity). Each event may have a causal_parent pointing to the trigger. The chain is fully traversable._

# **3\. Upward Propagation to Aggregate Layers**

## **3.1 The Mismatch Problem**

Entity effects have arbitrary spatial footprints. Simulation layers operate on a fixed cell grid. A forester working in a single forest patch that spans parts of three Layer 1 cells must distribute its effect correctly across those three cells - not apply the full effect to each, and not apply it only to the cell containing the entity's current position.

The correct mechanism is a weighted spatial join:

| **Step**                | **Operation**                                                                     | **Detail**                                                                                                                                                                                                                    |
| ----------------------- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1\. Compute footprint   | Determine the effect's 3D spatial volume                                          | For a radius effect: sphere or cylinder centered on entity position. For a directed effect: cone or rectangle. For a contact effect: single cell. Shape is defined in the action rule or aura definition.                     |
| 2\. Intersect with grid | Find all Layer cells whose bounding boxes overlap the footprint                   | Cells in L0, L1, L2 are regular grid cells with known bounding boxes. Intersection test is fast: axis-aligned bounding box overlap check in 3D.                                                                               |
| 3\. Compute weights     | For each intersecting cell: weight = intersection_volume / total_footprint_volume | Weights sum to 1.0 across all intersecting cells. A footprint that falls entirely within one cell gives that cell weight 1.0. A footprint spanning three cells might give weights 0.6, 0.3, 0.1 depending on overlap volumes. |
| 4\. Apply deltas        | For each cell: apply delta_magnitude \* weight to the target field                | The total effect is conserved - no duplication, no loss. The forester's timber harvest is distributed across the three forest cells in proportion to how much of its working area falls in each.                              |
| 5\. Write event record  | Insert one R\*-tree record covering the full footprint bbox                       | The event record stores the total magnitude. The per-cell application is implementation detail. Queries against the R\*-tree return the event; layer state reflects the distributed application.                              |

## **3.2 The Z-Dimension in Practice**

The z-axis separates effects that would otherwise incorrectly bleed across vertical boundaries. Concrete cases:

| **Entity**                | **Effect**                           | **Without Z**                                                                                                            | **With Z**                                                                                                                                                           |
| ------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deep underground predator | Hunts prey in subterranean ecosystem | Effect bleeds upward into surface fauna population - surface deer population decreases for no surface-observable reason. | Effect confined to z_min..z_max band matching the underground zone. Surface Layer 1 cells unaffected unless their z range intersects.                                |
| Surface fire event        | Burns flora in surface zone          | Fire damage applies to underground root systems at same x,y - incorrect for most world definitions.                      | Fire effect has z_min = z_max = 0 (surface only). Underground cells at same x,y untouched.                                                                           |
| Aerial entity with aura   | Emits effect from high altitude      | Effect reaches ground-level cells at full strength regardless of altitude - incorrect for attenuating effects.           | Effect footprint has z_min = flight_altitude - radius, z_max = flight_altitude + radius. Ground cells only receive effect if they fall within the z range.           |
| Aquifer depletion         | Entity extracts groundwater at depth | Water table depletion appears as surface resource drain.                                                                 | Depletion event confined to aquifer z band. Surface water availability only affected if a separate propagation rule connects aquifer level to surface soil moisture. |

_Z-granularity is a world definition parameter set at initialization. A coarse world (surface / shallow underground / deep underground - three levels) is sufficient for most settings. A fine-grained world (meter-level altitude) is rarely necessary and significantly increases intersection computation cost. Choose the minimum granularity that distinguishes the vertical separations that matter for your specific world._

# **4\. Auras vs. Action Rules: Different Insertion Patterns**

The two entity effect mechanisms insert into the R\*-tree differently. This distinction matters for query correctness.

| **Effect Type**          | **Insertion Pattern**                                                                                                                                                                  | **t Range**                                                                                      | **Delta Representation**                                                                                                                           |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Passive aura (sustained) | Single record inserted when aura activates. Record updated (t_end extended) each tick the aura remains active. Record closed (t_end set) when entity moves, deactivates, or dissolves. | t_start = tick of activation. t_end = tick of deactivation. Spans many ticks as a single record. | delta_magnitude = magnitude_per_tick. Queries must account for duration when computing total effect: total = delta_magnitude \* (t_end - t_start). |
| Action rule (one-shot)   | New record inserted each time the rule fires.                                                                                                                                          | t_start = t_end = tick of firing.                                                                | delta_magnitude = total effect of one firing. Multiple firings produce multiple records with the same source_id - queryable as a series.           |
| Action rule (duration)   | For rules with explicit duration (e.g. a siege that lasts N ticks): single record with full time extent.                                                                               | t_start = tick siege begins. t_end = tick siege ends.                                            | delta_magnitude = total effect over full duration. Per-tick application is implementation detail below the event log.                              |

_The key invariant: a range query on \[x0..x1, y0..y1, z0..z1, t0..t1\] returns every event record whose bounding box overlaps the query box in all four dimensions simultaneously. An aura that was active the entire query window will appear once. An action rule that fired 12 times in the window will appear 12 times. The GM's get_events() tool performs exactly this query and returns the result set with causal attribution intact._

# **5\. Query Interface**

The R\*-tree is the backend for get_events(). The tool accepts a spatial-temporal query box and optional filters, and returns the matching event records.

## **5.1 get_events() Parameters**

| **Parameter**        | **Type**                            | **Description**                                                                                                                                                                         |
| -------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| center               | vec3 (x, y, z)                      | Center of the query region.                                                                                                                                                             |
| radius               | float                               | Spatial radius around center. Constructs x, y, z ranges as \[center - radius, center + radius\] in each spatial dimension. For asymmetric queries, explicit bbox can be passed instead. |
| bbox                 | optional explicit 4D box            | Override for non-spherical queries. Specify \[x_min, x_max, y_min, y_max, z_min, z_max, t_start, t_end\] directly.                                                                      |
| t_start, t_end       | int (tick)                          | Temporal range. Required. The simulation does not support open-ended time queries - always specify a range.                                                                             |
| filter_entity        | UUID \| null                        | Return only events from this entity. Null = all entities.                                                                                                                               |
| filter_effect_type   | list\[enum\] \| null                | Return only events of these effect types. Null = all types.                                                                                                                             |
| filter_severity      | float \| null                       | Return only events where abs(delta_magnitude) >= threshold. Useful for filtering out low-magnitude aura noise.                                                                          |
| filter_layer         | list\[enum: L0,L1,L2,L2_5\] \| null | Return only events targeting fields in these layers.                                                                                                                                    |
| include_causal_chain | bool, default false                 | If true, for each returned event also return its causal_parent chain up to root. Expensive for large result sets - use only when investigating causation.                               |

## **5.2 Query Examples**

**What affected the northern forest zone in the last 100 ticks?**

get_events(

center = (forest_center_x, forest_center_y, 0),

radius = forest_radius,

t_start = current_tick - 100,

t_end = current_tick

)

// Returns: all entity auras, action rule firings, and event system

// events whose 4D bbox overlaps this query. Includes underground

// events only if their z range reaches surface (z=0).

**Why did the population in zone X decline - full causal chain?**

get_events(

center = zone_X_center,

radius = zone_X_radius,

t_start = decline_start_tick,

t_end = current_tick,

filter_effect_type = \[population_change\],

filter_severity = 0.05,

include_causal_chain = true

)

// Returns: significant population change events with causal parents.

// e.g. event A (population_change) caused_by event B (resource_extraction)

// caused_by rule army_raid_food_stores on entity ent_army_003.

**What was entity X doing between ticks 200 and 300?**

get_events(

bbox = \[-INF, +INF, -INF, +INF, -INF, +INF, 200, 300\],

filter_entity = entity_X_id

)

// No spatial filter - returns all events from this entity in the

// time window regardless of location. Full movement footprint.

# **6\. Full Layer Write Pipeline**

When an entity action rule or aura fires, the following sequence executes atomically before advancing to the next entity:

| **Step**                      | **Operation**                                                                                                                        | **Output**                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| 1\. Evaluate effect footprint | Compute 3D spatial volume from effect type, entity position, and radius or shape parameter.                                          | Footprint bounding box \[x_min, x_max, y_min, y_max, z_min, z_max\] |
| 2\. Spatial join              | Find all Layer 0/1/2/2.5 cells overlapping the footprint. Compute intersection weights.                                              | Cell list with weights summing to 1.0                               |
| 3\. Apply cell mutations      | For each cell: target_field += delta_magnitude \* weight (or multiply or set). Propagate to dependent fields if any.                 | Updated Layer 0-2.5 state for affected cells                        |
| 4\. Insert R\*-tree record    | Construct event record with full 4D bbox \[footprint_bbox + t_start, t_end\]. Insert into R\*-tree.                                  | Indexed event record, queryable immediately                         |
| 5\. Set narrative flag        | If rule/aura has narrative_flag=true: append event_id to the GM notification queue for next get_events() call.                       | GM notification queue entry                                         |
| 6\. Set causal parent         | If this event was triggered by another event (via event system cascade or entity reaction): set causal_parent = triggering event_id. | Causal chain link in R\*-tree record                                |

_Steps 1-4 are a single atomic operation from the simulation's perspective. Either all of them complete or none of them do. This prevents partial writes where the layer state is updated but no event record exists, or vice versa. The R\*-tree and the layer state are always consistent with each other._

# **7\. Implementation Notes**

## **7.1 Library Choice**

The R\*-tree is a well-studied data structure with mature implementations in most languages. Key requirements for this use case:

- Must support arbitrary-dimension bounding boxes, or at minimum configurable dimensionality. 4D is non-standard; many spatial libraries only support 2D or 3D.
- Must support bulk-loading for world initialization (inserting thousands of historical events at startup).
- Must support range queries returning all overlapping records, not just nearest-neighbor.
- Must support record deletion or time-based expiry for log pruning (old events beyond the retention horizon can be archived or summarized).

Viable options by language:

| **Language** | **Library**                                       | **Notes**                                                                                                                                                                                   |
| ------------ | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python       | rtree (libspatialindex wrapper)                   | Supports N-dimensional indexes. Well-maintained. Standard choice for Python spatial work. Serialize to disk for persistence.                                                                |
| Python       | scipy.spatial.cKDTree                             | KD-tree not R\*-tree - faster for point queries, slower for range queries on interval data. Less appropriate for temporal extent.                                                           |
| C++ / Rust   | Boost.Geometry R-tree (C++) or rstar crate (Rust) | Native performance. Boost supports configurable dimensions. rstar crate supports generic dimensions cleanly.                                                                                |
| JavaScript   | rbush (2D only) or custom port                    | rbush is excellent for 2D but does not support 4D natively. Would require extension or replacement.                                                                                         |
| Any          | PostgreSQL + PostGIS with temporal extension      | If world state is already in a relational DB, PostGIS handles 3D spatial queries and a tick column handles temporal range. Slower than in-memory R\*-tree but requires no separate library. |

## **7.2 Record Volume and Pruning**

The R\*-tree will accumulate records at a rate proportional to entity count \* average rules per entity \* tick frequency. For a world with 10,000 active entities each firing an average of 2 rules per tick, that is 20,000 records per tick. At 1,000 ticks per session, that is 20 million records. This is within the range of in-memory R\*-tree performance for range queries, but requires attention to memory footprint.

Pruning strategy:

- Define a retention horizon: the maximum tick age of events kept in hot R\*-tree memory. Events older than the horizon are serialized to a cold archive (flat file or database) and removed from the live tree.
- Summary records: when pruning a time window, optionally generate aggregate summary records that capture the net effect of all events in that window per cell per effect type. These summary records are smaller than the raw event set but preserve approximate queryability for old history.
- Narrative-flagged events are never pruned from hot storage regardless of age. They are the events the GM cares about and must remain queryable with full causal chains.

## **7.3 Aura Record Management**

Sustained aura records have an open t_end while the aura is active. This creates a bookkeeping requirement: the tick engine must maintain a reference to each active aura's current R\*-tree record so it can extend t_end each tick without creating a new record per tick.

Implementation: maintain an in-memory map of entity_id + aura_id -> current_record_id. Each tick: if aura still active, update record's t_end in-place. If aura deactivated: finalize t_end and remove from active map. This produces one record per aura activation period, not one record per tick - a significant reduction in tree size for long-running auras.

# **8\. Integration with Entity Definition Schema**

The entity definition schema (action rules and aura definitions) requires two additions to support this system:

| **Addition**       | **Location in Schema**            | **Purpose**                                                                                                                                                                                                                                               |
| ------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| effect_shape       | Action rule and aura definition   | Defines the 3D spatial footprint shape: sphere(radius), cylinder(radius, z_min_offset, z_max_offset), cone(direction, angle, length), contact(z_offset=0). Default: sphere. The shape determines how the footprint bbox is computed from entity position. |
| z_range            | Action rule and aura definition   | Explicit z band for effects that should not radiate vertically beyond a defined stratum. E.g. an underground entity's aura: z_range = \[entity.z - 2, entity.z + 1\]. Overrides the shape's natural vertical extent.                                      |
| effect_type tag    | Action rule and aura definition   | Categorical tag used for R\*-tree record and query filtering. Must be one of the defined effect_type enum values.                                                                                                                                         |
| narrative_flag     | Already present in action rule    | Already defined. Confirmed: this field maps directly to the R\*-tree record's narrative_flag and the GM notification queue.                                                                                                                               |
| causal_parent_rule | Action rule definition (optional) | If this rule fires in response to another event, specify the event_type it responds to. The tick engine uses this to look up the triggering event_id and set causal_parent automatically.                                                                 |

_This document specifies the event log and propagation system as a supplement to the full architecture. The 4D R\*-tree is the single source of truth for what happened, where, when, and why. The aggregate layer state is a derived projection of it. These two representations must always be consistent - the atomic write pipeline in Section 6 is the mechanism that guarantees this._