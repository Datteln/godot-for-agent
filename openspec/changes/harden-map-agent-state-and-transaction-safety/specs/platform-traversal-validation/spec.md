## ADDED Requirements

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
