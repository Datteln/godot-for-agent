# authoritative-map-planning Specification

## Purpose

Define an immutable authoritative map snapshot that binds planner design data, deterministic validation, and writer execution to a single revision-bound source of truth, and governs the planner validation budget and final plan publication.

## Requirements

### Requirement: Planning uses a revision-bound authoritative snapshot
The system MUST create an immutable authoritative map snapshot before the first planner attempt. The snapshot MUST identify its target, layer, revision, schema, coverage, completeness, digest, canonical cell facts, collision/support facts, traversal profile, entry anchor, reachable frontier, object occupancy freshness, and verified resource bindings.

#### Scenario: Complete snapshot is created
- **WHEN** the reader can collect every execution-critical fact for the requested planning region
- **THEN** it persists a digest-bound snapshot with `execution_eligible=true` and the planner receives its typed projection

#### Scenario: Snapshot coverage is incomplete
- **WHEN** a region read is truncated, an object index is stale, or an explicit movement fact is unavailable
- **THEN** the snapshot records the incomplete field and evidence instead of treating an omitted value or summary count as a complete fact

#### Scenario: Map changes after snapshot creation
- **WHEN** the authoritative map revision differs from the snapshot revision
- **THEN** the runtime invalidates candidates and approvals derived from that snapshot and requires a refreshed snapshot before execution

### Requirement: Planner design data is separated from write-critical atlas data
The planner MUST output route geometry, platform and segment definitions, semantic resource references, reference-cell coordinates, and design rationale. It MUST NOT be the authority for per-operation `source_id`, `atlas_coords`, `alternative_tile`, GridMap item, or orientation values. A deterministic compiler SHALL resolve those values from the full snapshot and verified resource bindings.

#### Scenario: Planner selects a semantic ground resource
- **WHEN** a candidate plan references the verified semantic resource `ground`
- **THEN** the compiler resolves its exact TileSet or GridMap identity without asking the planner to reproduce raw atlas values

#### Scenario: Planner emits a naked atlas operation
- **WHEN** a candidate contains an unbound raw atlas or item operation as its claimed write authority
- **THEN** the plan contract rejects the operation and no approval is created

#### Scenario: Resource binding cannot be verified
- **WHEN** the registry entry and canonical reference cell do not agree
- **THEN** the compiler returns a typed refresh requirement and does not guess an atlas value

### Requirement: Traversal occupancy is explicit and source-backed
The snapshot MUST distinguish observed map and object occupancy from traversal interpretation fields. `movement_model`, `cell_occupancy`, `requires_support`, `support_occupancy`, actor footprint, and movement limits MUST identify authoritative sources and MUST NOT rely on defaults to authorize execution. The planner SHALL consume this traversal profile without overriding it inside an attempt.

#### Scenario: TileMap cell is non-empty
- **WHEN** the reader observes a canonical tile at a coordinate
- **THEN** the snapshot records the observed filled cell independently from whether the actor movement model treats actor cells as empty or filled

#### Scenario: Planner changes occupancy semantics
- **WHEN** a planner candidate attempts to replace the snapshot traversal profile
- **THEN** validation rejects the mismatch and requires a new snapshot if the gameplay interpretation genuinely changed

### Requirement: Deterministic planner validation is limited to three attempts
For one stable task lineage, target, layer, snapshot id, and planning operation, the runtime MUST allow at most three planner candidates to enter deterministic validation. Attempts two and three MUST receive the preceding structured issues and repair plan, and an unchanged candidate MUST NOT silently restart the budget.

#### Scenario: First candidate fails validation
- **WHEN** deterministic validation returns blocking structured issues for attempt one
- **THEN** the runtime persists the candidate, issues, and repair plan and schedules attempt two with those fields bound as explicit inputs

#### Scenario: Repaired second candidate fails
- **WHEN** attempt two addresses the prior repair contract but still fails deterministically
- **THEN** the runtime schedules exactly one final attempt with the remaining structured issues

#### Scenario: Candidate is unchanged
- **WHEN** a later candidate has the same semantic fingerprint or does not consume required repair fields
- **THEN** the runtime records typed `unchanged_plan_attempt` rather than opening an unbounded retry loop

#### Scenario: A fourth attempt is requested
- **WHEN** three candidates for the same snapshot have reached deterministic validation outcomes
- **THEN** the scheduler refuses a fourth attempt and proceeds to final plan publication

### Requirement: Validation exhaustion still publishes a final plan
After the third deterministic validation failure, the system MUST publish the final candidate as a user-visible planning result with `planning_status=delivered` and `execution_status=blocked_by_validation`. The result MUST include the snapshot identity, unresolved issues, validation summaries, and repair artifact references, and MUST NOT contain an approved write batch.

#### Scenario: All three candidates fail
- **WHEN** the third candidate returns one or more blocking validation issues
- **THEN** the latest candidate is delivered as the final plan, its execution is marked blocked, and no writer step becomes runnable

#### Scenario: Candidate passes before exhaustion
- **WHEN** any of the three candidates passes deterministic validation and compilation
- **THEN** the published result has `execution_status=approved` and carries immutable approved batch references

#### Scenario: User receives a blocked planning result
- **WHEN** planning is delivered but execution is blocked
- **THEN** the system does not claim that the map edit completed and reports that the authoritative map revision did not advance through this plan

### Requirement: Reachable frontier is rehydratable and execution-critical
The planner MUST receive entry, boundary, and reachable-frontier facts from the snapshot projection. If those facts are absent, compacted out of message history, incomplete, or stale, the runtime SHALL rehydrate them from the snapshot artifact or deterministically recompute them against the same authoritative revision and traversal profile.

#### Scenario: Conversation compaction removes prior frontier text
- **WHEN** a later planner attempt starts after the earlier tool message is no longer present
- **THEN** the runtime injects the frontier from the snapshot artifact without relying on the conversation summary

#### Scenario: Frontier can be recomputed
- **WHEN** the snapshot contains complete canonical cells and traversal facts but lacks a materialized frontier
- **THEN** the runtime deterministically computes it and derives a new digest-bound snapshot before validation

#### Scenario: Frontier recomputation fails
- **WHEN** a real start, complete coverage, or movement fact is unavailable
- **THEN** the system may deliver a plan with `execution_status=blocked_by_missing_facts` but MUST NOT compile, approve, or write a connectivity-dependent batch
