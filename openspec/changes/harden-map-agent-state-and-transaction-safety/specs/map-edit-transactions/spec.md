## MODIFIED Requirements

### Requirement: Undo and Redo preserve revision consistency
Committed map transactions SHALL restore map content, revision metadata, and the revision tracker's authoritative fingerprint together under Ctrl+Z, programmatic Undo, and Redo.

#### Scenario: User undoes a committed transaction
- **WHEN** the user invokes Ctrl+Z after a committed map write group
- **THEN** all group content and revision metadata return to the pre-group state and the tracker recaptures that state's fingerprint without incrementing its revision

#### Scenario: User redoes an undone transaction
- **WHEN** the user invokes Redo
- **THEN** all group content and revision metadata return to the committed state and the tracker recaptures that state's fingerprint without incrementing its revision

#### Scenario: Runtime invokes history programmatically
- **WHEN** map tooling invokes Undo or Redo without a keyboard action
- **THEN** it uses the same authoritative revision reload and fingerprint synchronization path as interactive history

#### Scenario: Content changes after history synchronization
- **WHEN** a later map content change does not originate from the tracked transaction or history restoration
- **THEN** the external-change scanner recognizes the fingerprint drift and advances the revision exactly once

### Requirement: Incomplete transactions recover after restart
The system SHALL persist a versioned, checksummed transaction journal with explicit lifecycle state and before/after evidence sufficient to recover or block an interrupted write group after editor restart.

#### Scenario: Editor exits with a provably uncommitted write group
- **WHEN** the editor restarts and finds a valid journal in `prepared` or `applying`
- **THEN** it restores the before snapshot and records rollback before accepting new map writes

#### Scenario: Journal records a committed write
- **WHEN** restart finds a valid `committed` journal whose after-state evidence matches
- **THEN** it preserves the committed edit and retries journal cleanup without rolling back content or revision metadata

#### Scenario: Journal is ambiguous
- **WHEN** restart finds a journal in `committing` and durable evidence cannot prove the before or after state
- **THEN** the system blocks automatic map writes and reports the transaction id, affected targets, and recovery instructions instead of guessing rollback or commit

#### Scenario: Journal integrity fails
- **WHEN** an incomplete transaction journal has an invalid checksum, unsupported schema, or missing required snapshot
- **THEN** the system blocks automatic map writes and reports recovery instructions instead of guessing state

#### Scenario: First mutation races deferred startup recovery
- **WHEN** a map mutation is requested before background recovery has completed
- **THEN** the mutation waits for a synchronous recovery barrier and cannot start until recovery is clean or explicitly blocked

## ADDED Requirements

### Requirement: Commit state is durable before journal cleanup
The transaction manager MUST durably record `committed` after the Godot Undo action commits and before attempting to delete the journal.

#### Scenario: Journal deletion fails after commit
- **WHEN** the map edit and revision metadata commit successfully but journal cleanup fails
- **THEN** the retained journal identifies the transaction as committed and a restart does not roll the edit back

#### Scenario: Commit is replayed
- **WHEN** recovery or a client retry observes the same committed transaction identity
- **THEN** it returns the committed outcome without applying the map mutation or revision increment again
