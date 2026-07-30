## MODIFIED Requirements

### Requirement: Map workflow state changes only through events
The system MUST route task-epoch initialization and all stage, blocker, checkpoint, batch, validation, evidence, scope, revision, retry, transaction-reference, and no-progress changes through a single Map Workflow reducer. Every state field MUST declare machine-readable lifecycle metadata containing its task, revision, or session scope, reset/default factory, and resume policy, and epoch initialization MUST be derived from that metadata.

#### Scenario: Agent requests a stage transition
- **WHEN** Agent orchestration submits a valid map workflow event
- **THEN** the reducer applies the transition and records the event without direct state-field assignment by the Agent

#### Scenario: QueryEngine requests a state update
- **WHEN** QueryEngine receives a map tool result
- **THEN** it submits an event instead of directly modifying MapTaskState fields or reducer-owned nested containers

#### Scenario: A distinct map task begins
- **WHEN** the runtime creates a new map task rather than resuming the current task lineage
- **THEN** one `task_epoch_started` event atomically resets every task-scoped field, including automatic iterations, blockers, validations, evidence, scopes, revisions, layers, region reads, pending batches, retries, transaction references, and contextual task data

#### Scenario: A workflow field is added
- **WHEN** a new field is introduced without complete lifecycle metadata or its reset/resume behavior differs from that metadata
- **THEN** the exhaustive workflow-state check fails before the field can silently leak across task epochs

#### Scenario: Code bypasses reducer ownership
- **WHEN** a repository check finds a direct write to a reducer-owned scalar or nested container outside the reducer or the exact audited pre-construction hydration boundary
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

### Requirement: Persisted workflow state hydrates through a closed construction boundary
The runtime MUST migrate raw persisted data, validate and normalize the complete value, construct `MapTaskState` once, and publish it as live reducer-owned state. The hydration boundary MUST NOT accept or mutate an already-live `MapTaskState`.

#### Scenario: Persisted state requires schema migration
- **WHEN** an older persisted workflow document is loaded
- **THEN** migration operates on raw data before construction and the complete migrated value passes schema and lifecycle validation before publication

#### Scenario: Hydration completes
- **WHEN** a validated `MapTaskState` has been constructed and published
- **THEN** every later state change, including a migration correction, is represented by a reducer event rather than a hydration allowlist write

#### Scenario: Persisted state round-trips
- **WHEN** a supported workflow state is serialized and hydrated
- **THEN** its task, revision, and session fields preserve the declared resume policies and no field is omitted from classification

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

### Requirement: Dedicated resume authorization is one-shot and request-scoped
The dedicated map-task resume command MUST create one authorization bound to the paused task lineage, and the next user request MUST atomically capture and clear it before fallible request processing. A failure, rejection, or early return MUST NOT authorize a later request.

#### Scenario: The authorized resume request is accepted
- **WHEN** the next user request consumes a dedicated resume authorization for the same resumable task lineage
- **THEN** that request may restore the checkpoint exactly once without widening target, tool, write, or permission scope

#### Scenario: The consuming request exits early
- **WHEN** the request captures the authorization and then fails classification, has no active Frame, raises an exception, or returns early
- **THEN** the authorization remains consumed and the following ordinary message is not classified as a map edit from historical state

#### Scenario: A persisted authorization is loaded
- **WHEN** a Session restarts after the dedicated command but before the next user request
- **THEN** the authorization remains bound to the same task lineage until one request atomically captures it

### Requirement: Completion lifecycle semantics cover every workflow status
Completion-candidate eligibility and an allowed Completion Gate outcome MUST have explicit behavior for every workflow status.

#### Scenario: Running task passes the Gate
- **WHEN** an active `running` task has a current completion candidate and the Gate allows completion
- **THEN** the reducer transitions the task to `completed` exactly once

#### Scenario: Completed outcome is replayed
- **WHEN** the identical committed completion outcome is replayed for a task already in `completed`
- **THEN** the task remains completed without another state transition or duplicated completion effects

#### Scenario: Paused task is evaluated
- **WHEN** a paused task reaches Completion Gate evaluation
- **THEN** the Gate returns a workflow-paused blocker and does not complete the task

#### Scenario: Idle or cancelled task retains a stale candidate
- **WHEN** an `idle` or `cancelled` task still carries a historical completion-candidate marker
- **THEN** the marker is invalidated and the response cannot be reported as successful task completion

#### Scenario: Task lifecycle invalidates an old candidate
- **WHEN** the task is cancelled, replaced, or starts a distinct epoch
- **THEN** the prior lineage's completion-candidate identity is cleared

### Requirement: Capture paths reject malformed Godot scheme spellings
Screenshot and image-review path validation MUST reject malformed lookalikes of the accepted `res://` and `user://` schemes before project-relative path resolution.

#### Scenario: Capture path uses a single-slash pseudo-scheme
- **WHEN** a capture or image-review path begins with `user:/` or `res:/` but not the valid double-slash form
- **THEN** the runtime returns a structured invalid-path result and does not reinterpret it below the project root

#### Scenario: Capture path uses a colon-only pseudo-scheme
- **WHEN** a capture or image-review path begins with `user:` or `res:` without `//`
- **THEN** the runtime rejects it before filesystem access

#### Scenario: A path list contains a malformed scheme
- **WHEN** any element of a list-valued capture path argument is non-string or uses a malformed Godot scheme
- **THEN** the entire path validation fails with a typed invalid-argument result
