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
The coordinated Session/artifact commit MUST use turn identity and canonical submission fingerprint to make recovery and retry idempotent.

#### Scenario: Client retries an interrupted identical submission
- **WHEN** a retry has the same turn identity and canonical fingerprint as a prepared coordinated commit
- **THEN** the runtime resumes or returns that commit without duplicating artifact entries, messages, grants, or workflow mutations

#### Scenario: Retry conflicts with prepared content
- **WHEN** a retry reuses a known turn identity with a different canonical fingerprint
- **THEN** the runtime rejects the conflict and preserves the original prepared or committed data for recovery
