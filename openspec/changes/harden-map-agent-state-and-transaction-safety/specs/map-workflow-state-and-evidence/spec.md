## MODIFIED Requirements

### Requirement: Map workflow state changes only through events
The system MUST route task-epoch initialization and all stage, blocker, checkpoint, batch, validation, evidence, scope, revision, retry, transaction-reference, and no-progress changes through a single Map Workflow reducer.

#### Scenario: Agent requests a stage transition
- **WHEN** Agent orchestration submits a valid map workflow event
- **THEN** the reducer applies the transition and records the event without direct state-field assignment by the Agent

#### Scenario: QueryEngine requests a state update
- **WHEN** QueryEngine receives a map tool result
- **THEN** it submits an event instead of directly modifying MapTaskState fields or reducer-owned nested containers

#### Scenario: A distinct map task begins
- **WHEN** the runtime creates a new map task rather than resuming the current task lineage
- **THEN** one `task_epoch_started` event atomically resets every task-scoped field, including automatic iterations, blockers, validations, evidence, scopes, revisions, layers, region reads, pending batches, retries, transaction references, and contextual task data

#### Scenario: Code bypasses reducer ownership
- **WHEN** a repository check finds a direct write to a reducer-owned scalar or nested container outside an audited hydration or migration boundary
- **THEN** the check fails and identifies the bypassing location

### Requirement: Workflow state is scoped by target and revision
The reducer SHALL organize blockers, checkpoints, batches, evidence, validation, and progress under a canonical `(target, revision)` scope, and a gate match SHALL require an exact non-null target and revision.

#### Scenario: A new revision is observed
- **WHEN** a canonical target advances to a new revision
- **THEN** state from the previous revision cannot satisfy gates for the new revision

#### Scenario: Validation omits its revision
- **WHEN** a validation result has a missing or null revision
- **THEN** it cannot satisfy a gate for any concrete target revision and the runtime returns a typed missing-scope result

#### Scenario: Validator failure updates one target
- **WHEN** a validator or reviewer fails for one target and revision
- **THEN** the reducer upserts only the matching scoped blocker and preserves blockers belonging to every other target, revision, and source

## ADDED Requirements

### Requirement: Validation inputs are normalized defensively
The runtime MUST normalize validator and reviewer payloads into typed internal values before they reach workflow state or the Completion Gate.

#### Scenario: Issues collection is null
- **WHEN** a validator or reviewer returns `issues=null` or `structured_issues=null`
- **THEN** the boundary normalizer produces an empty collection and Completion Gate evaluation does not raise an exception

#### Scenario: Issues collection is malformed
- **WHEN** an issues field has a value that violates its collection contract
- **THEN** the runtime records a typed validation-contract blocker and fails closed without replacing unrelated scoped blockers

### Requirement: Automatic completion repair budget is task-local
The automatic completion-repair iteration count MUST belong to one task epoch and MUST NOT leak into a distinct map task.

#### Scenario: A task exhausts its repair budget
- **WHEN** one map task reaches its configured automatic iteration limit
- **THEN** that task pauses or fails with a typed budget outcome without changing the budget available to a later task

#### Scenario: The same task resumes
- **WHEN** a paused task is explicitly resumed from its checkpoint
- **THEN** its existing iteration count is restored rather than reset as a new task
