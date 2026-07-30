## MODIFIED Requirements

### Requirement: Success unlocks dependent steps
The scheduler MUST start a step only after every declared predecessor has completed successfully and every declared predecessor-result binding has been resolved into the successor contract.

#### Scenario: All predecessors succeed
- **WHEN** all dependencies of a pending step have typed status `succeeded` and their declared bindings resolve
- **THEN** the scheduler makes that step runnable

#### Scenario: A predecessor fails
- **WHEN** any dependency fails, is cancelled, or is blocked
- **THEN** the scheduler does not start the dependent step and records a typed blocked result that identifies the predecessor

#### Scenario: A predecessor result cannot be bound
- **WHEN** dependencies succeeded but a required result path or artifact reference is absent or malformed
- **THEN** the scheduler records a typed binding failure with the step, dependency, and path and creates no child Frame

### Requirement: Predecessor results become explicit inputs
The scheduler MUST bind predecessor typed results or artifact references into the successor input contract inside the scheduler error boundary.

#### Scenario: Planner output feeds writer
- **WHEN** a planner step succeeds with approved batch artifacts
- **THEN** the writer receives those artifacts as named contract inputs rather than reconstructing a plan from write operations

#### Scenario: A binding path is invalid
- **WHEN** the successor contract refers to a path that is absent from the predecessor's typed result
- **THEN** the scheduler returns a stable typed `dependency_binding_failed` outcome instead of propagating an exception or HTTP 500

## ADDED Requirements

### Requirement: Worker stage transitions fail as typed plan outcomes
Worker contract construction and workflow stage transitions MUST execute inside a boundary that converts invalid transitions and payload construction failures into typed plan outcomes.

#### Scenario: Worker cannot enter write stage
- **WHEN** orchestration requests a transition to `write` that is invalid from the current workflow state
- **THEN** the step becomes `blocked` or `replan_required` with a stable error code and no unhandled server exception

#### Scenario: Task payload construction fails
- **WHEN** a runnable step cannot produce a payload conforming to its worker contract
- **THEN** the scheduler records the typed failure without creating a worker Frame or partially advancing workflow state

### Requirement: Delegate groups have one authoritative pending-step source
The scheduler graph MUST be the sole source of pending delegate-group work. The runtime MUST NOT maintain or execute a parallel legacy `remaining` task queue.

#### Scenario: A delegate group starts
- **WHEN** orchestration creates a dependency-aware delegate group
- **THEN** the first and every subsequent runnable child are selected from scheduler graph state without storing a second pending-task list

#### Scenario: A child completes
- **WHEN** a delegate child reaches a terminal result
- **THEN** the next child is derived only from dependency status and runnable steps in the updated scheduler graph

#### Scenario: A legacy persisted group is loaded
- **WHEN** compatibility loading encounters a delegate group containing the old `remaining` field
- **THEN** it migrates the group to one scheduler graph or returns a typed blocked outcome before execution and never runs both representations

### Requirement: Repeated plan creation is bounded
The runtime MUST identify plan attempts by a semantic key containing task, stage, target, revision, operation, and root error and MUST stop unchanged attempts after a configured bound.

#### Scenario: Agent repeats an unchanged failed plan
- **WHEN** repeated `create_plan` calls have the same semantic key and no new input, revision, or successful predecessor
- **THEN** the runtime preserves the existing plan outcomes and returns a typed circuit-breaker result instead of overwriting the plan

#### Scenario: Authoritative input changes
- **WHEN** a new revision, required input, or successful predecessor changes the semantic plan state
- **THEN** the runtime permits a new exact plan attempt while retaining prior terminal outcomes for diagnosis and without implicitly resetting task-level convergence accounting

### Requirement: Plan convergence is bounded across revisions
The runtime MUST maintain a reducer-owned convergence count scoped to the stable task lineage, target, operation, and root-error family, independent of the exact attempt revision, and MUST stop plan cycles that repeatedly fail to reach an explicit convergence checkpoint or terminal outcome.

#### Scenario: Partial success advances revision without convergence
- **WHEN** repeated plan cycles each produce a successful predecessor or map write and advance revision from `N` to `N+1` and onward, but the same task lineage returns to `create_plan` without reaching an explicit convergence checkpoint or terminal outcome
- **THEN** each cycle increments the same task-level convergence count and the runtime returns a typed circuit-breaker result at the configured bound

#### Scenario: Multi-revision work makes authoritative task progress
- **WHEN** a plan cycle advances revision and satisfies a declared convergence checkpoint or reaches the task's terminal outcome
- **THEN** the runtime records that progress and does not classify the productive cycle as cross-revision thrash

#### Scenario: The same task resumes
- **WHEN** a paused or restarted task resumes the same stable lineage
- **THEN** its convergence count and prior exact attempt outcomes are restored rather than reset by the current revision

#### Scenario: A distinct task epoch begins
- **WHEN** the runtime starts a distinct task lineage through `task_epoch_started`
- **THEN** the new task receives a fresh convergence budget without removing the prior task's diagnostic history
