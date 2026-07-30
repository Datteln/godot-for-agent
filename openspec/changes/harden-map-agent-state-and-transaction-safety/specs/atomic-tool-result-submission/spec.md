## MODIFIED Requirements

### Requirement: Staged map artifacts follow the Session transaction
The system SHALL aggregate map results for the active submission as one staged turn block, allow the active transaction to read that block, and coordinate publication of the artifact block and Session locator through a recoverable, idempotent commit.

#### Scenario: Agent reads an artifact during the same submission
- **WHEN** a server-side map artifact reader requests an entry produced by front-tool results in the active uncommitted submission
- **THEN** the reader resolves the entry from the transaction-local staged turn block without requiring the persistent file to contain it

#### Scenario: Session commit succeeds
- **WHEN** the Session working copy and complete staged artifact turn block can both be persisted
- **THEN** the runtime makes the artifact entry and its Session locator jointly visible under the same turn identity and canonical fingerprint

#### Scenario: Artifact publication fails
- **WHEN** artifact preparation or publication fails before the coordinated commit completes
- **THEN** the active Session remains equal to its pre-request state, exposes no locator for the artifact, and keeps the submission safely retryable

#### Scenario: Submission is interrupted or rolled back
- **WHEN** the request is interrupted, cancelled, rejected, or fails reducer, artifact, or Session persistence
- **THEN** no committed locator or readable residual turn block remains, and recovery can discard or reconcile prepared data using the commit record

#### Scenario: Process stops between resource publications
- **WHEN** a process exit occurs after one coordinated resource is written but before the complete commit is marked durable
- **THEN** startup recovery uses recorded identities and digests to finish or roll back the known state without exposing a dangling locator or guessing content

## ADDED Requirements

### Requirement: Artifact publication never leaves a dangling locator
Every committed map-artifact locator MUST resolve to the exact committed artifact entry identified by Session, turn, entry, and canonical content fingerprint.

#### Scenario: A committed locator is read
- **WHEN** an Agent resolves a map-artifact locator from committed Session state
- **THEN** the artifact document contains the matching entry and fingerprint or the system reports a typed integrity failure and blocks dependent mutation

#### Scenario: An unreferenced prepared artifact exists
- **WHEN** recovery finds an artifact turn block that is not referenced by a matching committed Session turn
- **THEN** normal readers cannot observe it and recovery either reuses it for the identical retry or removes it after reconciliation

### Requirement: Coordinated publication is idempotent
The coordinated Session/artifact commit MUST preserve the existing completed-turn identity and canonical submission-fingerprint semantics, extending that path to prepared coordinated commits rather than replacing its identity algorithm.

#### Scenario: Client retries an interrupted identical submission
- **WHEN** a retry has the same turn identity and canonical fingerprint as a prepared coordinated commit
- **THEN** the runtime resumes or returns that commit without duplicating artifact entries, messages, grants, or workflow mutations

#### Scenario: Retry conflicts with prepared content
- **WHEN** a retry reuses a known turn identity with a different canonical fingerprint
- **THEN** the runtime rejects the conflict and preserves the original prepared or committed data for recovery

#### Scenario: Retry matches an existing completed turn
- **WHEN** a retry has the same turn identity and canonical fingerprint as a result already held by the existing completed-turn cache
- **THEN** the runtime returns that cached result through the existing identity path without starting a new coordinated commit

### Requirement: Coordinated commit boundaries are deterministically testable
The coordinated Session/artifact implementation MUST expose test-only named failpoints at each durable preparation, resource-publication, commit-marker, and cleanup boundary, and the production composition MUST keep them disabled and unreachable from submission payloads.

#### Scenario: Process exit is injected between publications
- **WHEN** a test process exits at the named boundary after artifact publication and before Session publication
- **THEN** restart reconciliation deterministically proves that no committed Session exposes a dangling locator

#### Scenario: Production submission is processed
- **WHEN** an ordinary client submits tool results
- **THEN** no request field can activate or select a coordinated-commit failpoint

### Requirement: Session turn identity is monotonic and never reused
The session turn counter SHALL be monotonic and non-decreasing across request failures, in-memory snapshot rollbacks, and process restarts. A failed request that rolls back to its pre-request snapshot SHALL NOT lower the persisted turn counter: the next successful session save SHALL persist `max(persisted_counter, in_memory_counter)`, the same monotonic rule already applied to the history event counter. Once a turn id has been committed in the coordinated commit record or `map_artifacts.json`, it SHALL never be reallocated, so a later submission always receives a turn id strictly greater than every previously committed turn id.

#### Scenario: Request fails after allocating a turn id
- **WHEN** a request allocates one or more turn ids and then fails, rolling the in-memory session back to its pre-request snapshot
- **THEN** the persisted turn counter is not lowered, and the next successful save persists `max(persisted, in-memory)` so a later submission cannot receive a turn id already committed

#### Scenario: Session is restored after a restart
- **WHEN** the session is loaded from disk after a process restart
- **THEN** the restored turn counter exceeds every turn id committed in `map_artifacts.json`, and the next allocated turn id is strictly greater than all of them

### Requirement: A turn-identity conflict is a typed, recoverable failure
When a staged submission reuses a turn id whose committed fingerprint differs (a state that can only arise from prior counter divergence or data corruption), the runtime SHALL reject the conflicting submission with a typed integrity failure instead of raising an uncaught exception or wedging the session. The rejection SHALL preserve the original committed data and SHALL allow the submission to be retried under a fresh, strictly-greater turn id without requiring manual deletion of the committed turn. The identity and canonical-fingerprint algorithm is unchanged; only the failure's observability and recovery path change.

#### Scenario: Staged turn conflicts with a committed turn
- **WHEN** a staged submission carries a turn id already committed with a different canonical fingerprint
- **THEN** the runtime returns a typed integrity failure, leaves the committed turn intact, and makes the session retryable by allocating a new turn id for the resubmission rather than wedging on the conflicting id

#### Scenario: Conflict does not bypass the idempotency algorithm
- **WHEN** the conflict is reported
- **THEN** the existing completed-turn identity and canonical-fingerprint semantics remain the source of truth, and no second identity algorithm is introduced
