# map-edit-transactions Specification

## Purpose

Define transaction, revision, write-safety, undo, capture-path, and external-drift guarantees for map editing.

## Requirements

### Requirement: Map transaction boundaries are explicit
Every map mutation MUST be performed as a worker file CodeAct action associated with a stable `task_execution_id`, task diff evidence, and required write-after validation. It MUST NOT require an Editor Undo transaction, an approved write-group transaction, or a pre-write proposal solely to make an ordinary project-file change.

#### Scenario: Standalone map mutation succeeds
- **WHEN** a map agent applies a project-file edit and its required validation passes
- **THEN** the system records the task diff and successful validation as the mutation evidence without creating an Editor Undo action

#### Scenario: Map transformation uses a temporary script
- **WHEN** a map transformation is performed by a temporary GDScript
- **THEN** it runs only in the task worker, and the system collects diff and validation evidence after the script exits

### Requirement: Validation failure rolls back the write group
The system MUST retain the current project diff when map write-after validation fails, is cancelled, or exhausts its repair budget. It MUST record the typed failure and end the task as `failed_validation` when it cannot continue; it MUST NOT automatically restore map content, revisions, or indexes to a before snapshot.

#### Scenario: Later validation fails
- **WHEN** a map write has changed files and final validation fails after repair budget exhaustion
- **THEN** the system preserves the affected diff for user review and reports `failed_validation` instead of rolling it back

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
The system SHALL persist a versioned, checksummed transaction journal with lifecycle state drawn only from `prepared`, `applying`, `committing`, `committed`, and `rolled_back`, plus before/after evidence sufficient to recover or block an interrupted write group after editor restart. `cleaned` SHALL be a logical outcome represented by the absence of the matching durably terminal journal and SHALL NOT be serialized as a journal state.

#### Scenario: Editor exits with a provably uncommitted write group
- **WHEN** the editor restarts and finds a valid journal in `prepared` or `applying`
- **THEN** it restores the before snapshot and records rollback before accepting new map writes

#### Scenario: Journal records a committed write
- **WHEN** restart finds a valid `committed` journal whose after-state evidence matches
- **THEN** it preserves the committed edit and retries journal cleanup without rolling back content or revision metadata

#### Scenario: Journal records a rolled-back write
- **WHEN** restart finds a valid `rolled_back` journal whose before-state evidence matches
- **THEN** it preserves the restored before-state and retries cleanup without applying the transaction again

#### Scenario: Terminal journal cleanup completes
- **WHEN** a `committed` or `rolled_back` marker is durable and its journal is successfully deleted
- **THEN** the transaction is logically clean and no `cleaned` journal record is written

#### Scenario: Journal is ambiguous
- **WHEN** restart finds a journal in `committing` and durable evidence cannot prove the before or after state
- **THEN** the system blocks automatic map writes and reports the transaction id, affected targets, and recovery instructions instead of guessing rollback or commit

#### Scenario: Journal integrity fails
- **WHEN** an incomplete transaction journal has an invalid checksum, unsupported schema, or missing required snapshot
- **THEN** the system blocks automatic map writes and reports recovery instructions instead of guessing state

#### Scenario: First mutation races deferred startup recovery
- **WHEN** a map mutation is requested before background recovery has completed
- **THEN** the mutation waits for a synchronous recovery barrier and cannot start until recovery is clean or explicitly blocked, then reloads authoritative revision metadata before evaluating mutation preconditions

#### Scenario: Recovery changes or confirms revision metadata
- **WHEN** recovery restores an uncommitted before-state or verifies a committed after-state
- **THEN** the mutation path reads the recovered authoritative revision and compares it with the request and approval expected revisions before creating a new journal, opening an Undo batch, or changing map content

### Requirement: Commit state is durable before journal cleanup
The transaction manager MUST durably record `committed` after the Godot Undo action commits and before attempting to delete the journal, and MUST attempt cleanup only for a durably persisted `committed` or `rolled_back` terminal marker.

#### Scenario: Journal deletion fails after commit
- **WHEN** the map edit and revision metadata commit successfully but journal cleanup fails
- **THEN** the retained journal identifies the transaction as committed and a restart does not roll the edit back

#### Scenario: Commit is replayed
- **WHEN** recovery or a client retry observes the same committed transaction identity
- **THEN** it returns the committed outcome without applying the map mutation or revision increment again

### Requirement: Recovery is bounded, observable, and single-flight
The editor MUST start transaction recovery eagerly, share one recovery operation across all first-write callers, enforce configured journal, snapshot, and operation bounds, and keep mutation blocked until recovery completes cleanly or returns a typed block.

#### Scenario: Multiple writes arrive during startup recovery
- **WHEN** more than one map mutation reaches `ensure_recovered()` while eager recovery is running
- **THEN** every caller awaits the same recovery operation and no duplicate scan, restore, cleanup, or revision reconciliation starts

#### Scenario: Recovery processes the maximum supported fixture
- **WHEN** the configured maximum supported journal and snapshot fixture is recovered
- **THEN** recovery reports progress and satisfies the configured startup/frame-latency policy while no mutation overtakes it

#### Scenario: Recovery input exceeds a configured bound
- **WHEN** a journal, snapshot, or operation exceeds its supported limit
- **THEN** recovery fails closed with a typed diagnostic and does not partially restore, commit, clean, or admit a new mutation

### Requirement: Transaction failure boundaries are deterministically testable
The transaction implementation MUST expose test-only, deterministic failure seams for durable journal/filesystem operations, transaction commit boundaries, cleanup, and process restart while the production composition keeps those seams disabled and unreachable from tool request data.

#### Scenario: A test injects journal deletion failure
- **WHEN** the test adapter activates the named cleanup failpoint after a durable terminal marker
- **THEN** deletion fails deterministically and restart recovery preserves the terminal transaction outcome before retrying cleanup

#### Scenario: A test simulates exit at a durable boundary
- **WHEN** the restart driver terminates a test process at a named journal or commit boundary
- **THEN** the next process observes exactly the persisted fixture state required to verify the corresponding recovery rule

#### Scenario: Production tooling submits a request
- **WHEN** a normal map tool request is handled by the production composition
- **THEN** request fields cannot enable, select, or configure any test failpoint
