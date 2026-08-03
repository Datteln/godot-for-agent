# platform-traversal-validation Specification

## Purpose

Define deterministic platform traversal validation using map geometry, physics parameters, coverage, and structured issue reporting.
## Requirements
### Requirement: Collision facts are authoritative
Platform validation MUST read collision facts from the canonical scene target or an immutable, digest-bound authoritative map snapshot for the same target, layer, and revision used by the planner. Public planner and validation requests MUST NOT substitute caller-supplied raw collision cells, incomplete region summaries, or stale snapshot data for those facts.

#### Scenario: Caller supplies raw collision cells
- **WHEN** a public validation request attempts to provide unverified collision cells
- **THEN** the validator ignores or rejects them and obtains authoritative facts

#### Scenario: Reader artifact revision is stale
- **WHEN** collision facts reference a different target revision
- **THEN** platform validation stops and requests a fresh read

#### Scenario: Snapshot coverage is incomplete
- **WHEN** the planner snapshot omits actor, trajectory, landing, headroom, or support cells required by validation
- **THEN** validation returns a typed incomplete-coverage issue and creates no approved batch

### Requirement: Leap validation samples the complete actor trajectory
The validator SHALL sample the leap trajectory with the configured actor footprint and check intermediate collision, headroom, landing width, and landing clearance.

#### Scenario: Arc intersects an overhead tile
- **WHEN** start and landing cells are valid but the sampled actor footprint intersects overhead collision
- **THEN** the leap is not executable and the result identifies the obstructed samples

#### Scenario: Landing has insufficient clearance
- **WHEN** the trajectory reaches a platform whose landing footprint or headroom is insufficient
- **THEN** the leap is not executable

### Requirement: Segments match referenced platform geometry
Each route segment MUST reference existing endpoint ids and its from/to coordinates and direction MUST agree with the referenced platform geometry.

#### Scenario: Segment references an unknown endpoint
- **WHEN** a segment's from or to id does not exist in the platform set
- **THEN** validation fails with a structured endpoint issue

#### Scenario: Segment coordinates disagree with ids
- **WHEN** segment coordinates do not lie on the referenced endpoint platforms
- **THEN** validation fails even if the textual route order is otherwise valid

### Requirement: Default movement abilities cannot authorize execution
The system MUST require explicit actor movement and clearance facts before returning an executable platform plan.

#### Scenario: Ability values are defaults
- **WHEN** platform validation uses default ability or actor-size values
- **THEN** the plan remains non-executable and reports the missing explicit inputs

### Requirement: Approved batches are consumed only by committed writes
The system MUST generate a platform-validation approval only after deterministic validation and compilation resolve every semantic resource reference to exact write operations from the same authoritative snapshot. It MUST retain that approval until the matching map write is durably committed, MUST compare its expected revision and snapshot digest with authoritative Godot state after recovery and immediately before mutation, and MUST advance workflow revision state only from a matching committed result.

#### Scenario: Platform batch passes validation
- **WHEN** a candidate plan passes platform validation and deterministic compilation for a canonical target, layer, revision, and snapshot digest
- **THEN** the system records an immutable approval id, snapshot id, batch fingerprint, and compiled batch without removing the batch or speculatively advancing revision

#### Scenario: Approved write commits
- **WHEN** the matching write transaction durably commits and returns its approval id, batch fingerprint, target, and committed revision
- **THEN** the reducer consumes that approval exactly once and advances workflow state to the observed committed revision

#### Scenario: Approved write is rejected or fails
- **WHEN** authorization, execution, validation, persistence, cancellation, or rollback prevents the approved write from committing
- **THEN** the approval and batch remain available while their expected target revision and snapshot digest are still current and no speculative revision is recorded

#### Scenario: Committed result is replayed
- **WHEN** the same committed transaction and approval result is submitted again
- **THEN** the system returns the prior outcome without consuming another batch or advancing revision again

#### Scenario: Target revision changes before commit
- **WHEN** authoritative observation shows that the target no longer has the approval's expected revision
- **THEN** the runtime returns a typed conflict containing the authoritative revision before creating a transaction journal, opening an Undo batch, mutating map content, or consuming the approval, and requires fresh platform facts and validation

#### Scenario: Revision conflict reconciles service state
- **WHEN** Godot rejects a write because its authoritative revision differs from the service's persisted `latest_revisions`
- **THEN** the service reducer records the trusted actual revision, invalidates approvals for the stale revision, and does not retry a mutation until the affected target has been reread and revalidated

#### Scenario: Service exits after Godot commit
- **WHEN** Godot durably commits revision `N+1` but the service exits before reducing the committed result and later retries from persisted revision `N`
- **THEN** the post-recovery authoritative revision check rejects the stale retry before mutation and the service reconciles to `N+1` without applying or consuming a second batch

#### Scenario: Snapshot digest changes without an accepted revision match
- **WHEN** the compiled approval references a snapshot digest that does not match the authoritative pre-write evidence
- **THEN** the writer rejects the batch before mutation and requires snapshot refresh and recompilation

### Requirement: Subjective design-quality checks are advisory
The validator SHALL distinguish objective traversal correctness from subjective design quality. Subjective design-quality conditions — a non-rest platform exceeding the configured maximum width, a challenge role repeated beyond the configured limit, and a route with fewer than the minimum segment count — SHALL be reported in `issues` and `repair_plan` but MUST NOT set a blocking failure, MUST NOT empty `edit_map_batches`, and MUST NOT prevent execution of an otherwise objectively valid plan.

#### Scenario: Over-wide non-rest platform
- **WHEN** a non-rest platform exceeds the configured max width and every objective traversal check passes
- **THEN** the plan executes and the over-width is reported as an advisory issue, not a blocking failure

#### Scenario: Repeated challenge roles
- **WHEN** the same challenge role repeats beyond the configured limit and every objective traversal check passes
- **THEN** the plan executes and the repetition is reported as advisory only

#### Scenario: Objective reachability still blocks
- **WHEN** a planned platform transition exceeds movement ability or a segment endpoint disagrees with referenced platform geometry
- **THEN** validation blocks execution with a typed reachability issue regardless of any subjective design-quality findings

#### Scenario: Advisory and blocking score issues coexist
- **WHEN** one or more advisory score issues precede a blocking score issue in `issue_details`
- **THEN** validation blocks execution and the top-level `error_code` identifies the first non-advisory issue rather than an advisory entry

### Requirement: Entry anchor accepts a flat coordinate dictionary
The validator SHALL accept an `entry_anchor` supplied as a flat coordinate dictionary (`x`, `y`, optional `role`) without a nested `cell` key and SHALL treat it as a valid anchor. Anchor parsing MUST NOT discard a flat coordinate dictionary as empty.

#### Scenario: Flat entry anchor is provided
- **WHEN** a plan provides `entry_anchor` as a flat `{x, y, role}` dictionary with no nested `cell` key
- **THEN** the validator consumes it as the entry anchor and does not return `entry_anchor_not_found`

#### Scenario: Wrapped entry anchor is provided
- **WHEN** a plan provides `entry_anchor` as a wrapper containing a nested `cell` coordinate dictionary
- **THEN** the validator unwraps `cell` and consumes the inner coordinates

### Requirement: Absent structured fields are rejected, not silently defaulted
The validator SHALL reject a plan with a typed missing-field issue when a required structured field is absent, and MUST NOT silently treat an absent field as a present empty collection. A presence guard that supplies an empty dictionary or array as the default value MUST distinguish "key absent" from "key present and empty".

#### Scenario: Required coordinate field is absent
- **WHEN** an entry omits a required coordinate field and the validator guards presence with a default collection
- **THEN** the guard reports the field as missing instead of treating the default empty collection as a present value

#### Scenario: Required field is present and empty
- **WHEN** a required structured field is present as an explicitly empty collection
- **THEN** the guard treats it as present-and-empty and reports the emptiness as a typed issue

### Requirement: Connectivity repair plans do not fabricate unreachable paths
A connectivity repair plan SHALL only suggest paths that have been validated as reachable under the configured movement model. The validator MUST NOT synthesize a repair path from an unvalidated geometric heuristic such as a raw manhattan trace.

#### Scenario: Repair hint is requested
- **WHEN** a connectivity failure produces a repair plan
- **THEN** the suggested path is either validated as reachable or omitted, never a fabricated manhattan trace

### Requirement: Platform compilation resolves semantic plans deterministically
The platform validator/compiler MUST accept route geometry and semantic resource references and SHALL generate exact write batches solely from verified snapshot resource bindings or canonical reference-cell rules. Compilation failure MUST be reported separately from route validation failure.

#### Scenario: Route validates and resources resolve
- **WHEN** every objective traversal check passes and every semantic resource has one verified binding
- **THEN** the compiler emits exact operations and a stable fingerprint for approval

#### Scenario: Route validates but resource resolution fails
- **WHEN** traversal is valid but a semantic resource is missing, ambiguous, or stale
- **THEN** the result reports a typed compilation issue, produces no approval, and preserves the valid route candidate for a refresh attempt

