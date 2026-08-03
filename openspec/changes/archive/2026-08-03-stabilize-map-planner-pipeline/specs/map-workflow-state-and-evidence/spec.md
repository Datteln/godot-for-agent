## ADDED Requirements

### Requirement: Planning snapshots and attempts are reducer-owned evidence
The Map Workflow reducer MUST persist authoritative snapshot identity, target/layer/revision scope, completeness, digest, planner candidate fingerprints, attempt ordinal, structured validation issues, repair artifact references, and publication outcome. These fields MUST have explicit lifecycle metadata and MUST survive conversation compaction, restart, and task resume according to their scope.

#### Scenario: Planner attempt fails before compaction
- **WHEN** deterministic validation records issues and a repair plan
- **THEN** the reducer persists the attempt and the next planner Frame receives those artifacts even if the original messages are compacted

#### Scenario: Same task resumes after restart
- **WHEN** persisted workflow state already contains two attempts for the current snapshot
- **THEN** the resumed task starts from at most the third attempt rather than resetting the planner budget

#### Scenario: A new snapshot replaces stale facts
- **WHEN** authoritative revision or required facts change
- **THEN** the reducer links the new snapshot to the same task lineage, invalidates stale approvals, and retains prior attempts for diagnosis

### Requirement: Planning and execution statuses are stored independently
Workflow state MUST represent planning delivery separately from map execution. Delivering a final candidate MUST NOT imply validation success, a committed map transaction, completion evidence, or revision advancement.

#### Scenario: Third validation attempt fails
- **WHEN** the runtime publishes the last candidate after exhausting its budget
- **THEN** state records `planning_status=delivered`, `execution_status=blocked_by_validation`, zero committed writes for that plan, and the unchanged authoritative revision

#### Scenario: Approved write commits
- **WHEN** an approved plan's write transaction commits and completion evidence passes
- **THEN** execution state advances through the existing reducer events without changing the historical planning publication record

### Requirement: Snapshot-backed progress context is re-derived each turn
For an active map-planning task, the runtime MUST inject a bounded digest containing snapshot identity, revision, attempt count, latest repair summary, planning status, execution status, and artifact references into each relevant agent turn. Large cell, atlas, collision, and repair arrays MUST remain in artifacts rather than being copied into conversation context.

#### Scenario: Planner turn starts after context compaction
- **WHEN** the conversation summary no longer contains the original region and validation tool results
- **THEN** the planner receives current snapshot and repair references re-derived from reducer state

#### Scenario: No active planning task exists
- **WHEN** the session has no active map-planning lineage
- **THEN** no planning snapshot digest is injected
