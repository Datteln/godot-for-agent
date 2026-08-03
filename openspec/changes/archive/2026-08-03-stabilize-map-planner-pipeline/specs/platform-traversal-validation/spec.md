## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: Platform compilation resolves semantic plans deterministically
The platform validator/compiler MUST accept route geometry and semantic resource references and SHALL generate exact write batches solely from verified snapshot resource bindings or canonical reference-cell rules. Compilation failure MUST be reported separately from route validation failure.

#### Scenario: Route validates and resources resolve
- **WHEN** every objective traversal check passes and every semantic resource has one verified binding
- **THEN** the compiler emits exact operations and a stable fingerprint for approval

#### Scenario: Route validates but resource resolution fails
- **WHEN** traversal is valid but a semantic resource is missing, ambiguous, or stale
- **THEN** the result reports a typed compilation issue, produces no approval, and preserves the valid route candidate for a refresh attempt
