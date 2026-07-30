# platform-traversal-validation Specification

## Purpose

Define deterministic platform traversal validation using map geometry, physics parameters, coverage, and structured issue reporting.

## Requirements

### Requirement: Collision facts are authoritative
Platform validation MUST read collision facts from the canonical scene target or a digest-bound reader artifact for the same target and revision.

#### Scenario: Caller supplies raw collision cells
- **WHEN** a public validation request attempts to provide unverified collision cells
- **THEN** the validator ignores or rejects them and obtains authoritative facts

#### Scenario: Reader artifact revision is stale
- **WHEN** collision facts reference a different target revision
- **THEN** platform validation stops and requests a fresh read

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
The system MUST retain a platform-validation approval until the matching map write is durably committed, MUST compare its expected revision with the authoritative Godot revision after recovery and immediately before mutation, and MUST advance workflow revision state only from a matching committed result.

#### Scenario: Platform batch passes validation
- **WHEN** a candidate batch passes platform validation for a canonical target and revision
- **THEN** the system records an immutable approval id and batch fingerprint without removing the batch or speculatively advancing revision

#### Scenario: Approved write commits
- **WHEN** the matching write transaction durably commits and returns its approval id, batch fingerprint, target, and committed revision
- **THEN** the reducer consumes that approval exactly once and advances workflow state to the observed committed revision

#### Scenario: Approved write is rejected or fails
- **WHEN** authorization, execution, validation, persistence, cancellation, or rollback prevents the approved write from committing
- **THEN** the approval and batch remain available while their expected target revision is still current and no speculative revision is recorded

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
