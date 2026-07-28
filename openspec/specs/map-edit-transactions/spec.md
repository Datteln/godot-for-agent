# map-edit-transactions Specification

## Purpose

Define transaction, revision, write-safety, undo, capture-path, and external-drift guarantees for map editing.

## Requirements

### Requirement: Map transaction boundaries are explicit
Every map mutation MUST belong to either a single-tool transaction or an approved write-group transaction with a stable transaction id.

#### Scenario: Standalone map mutation succeeds
- **WHEN** a mutation has no deferred validation requirement
- **THEN** content, revision, and related index changes commit in one single-tool Undo action

#### Scenario: Planned write group starts
- **WHEN** the first approved batch of a validated plan is applied
- **THEN** the system opens one transaction that remains pending until the group's completion validation

### Requirement: Validation failure rolls back the write group
The system MUST abort all writes in an approved write-group transaction when its required validation fails, is cancelled, or encounters a contract violation.

#### Scenario: Later validation fails
- **WHEN** several map edits succeeded inside a write group and final validation fails
- **THEN** the system restores all affected content, revisions, and indexes to their before-transaction state

### Requirement: Undo and Redo preserve revision consistency
Committed map transactions SHALL restore map content and revision metadata together under Ctrl+Z and Redo.

#### Scenario: User undoes a committed transaction
- **WHEN** the user invokes Ctrl+Z after a committed map write group
- **THEN** all group content and revision metadata return to the pre-group state

#### Scenario: User redoes an undone transaction
- **WHEN** the user invokes Redo
- **THEN** all group content and revision metadata return to the committed state

### Requirement: Incomplete transactions recover after restart
The system SHALL persist a checksummed transaction journal sufficient to identify and roll back an uncommitted write group after editor restart.

#### Scenario: Editor exits with an open write group
- **WHEN** the editor restarts and finds a valid journal for an uncommitted group
- **THEN** it restores the before snapshot before accepting new map writes

#### Scenario: Journal integrity fails
- **WHEN** an incomplete transaction journal has an invalid checksum or missing snapshot
- **THEN** the system blocks automatic map writes and reports recovery instructions instead of guessing state
