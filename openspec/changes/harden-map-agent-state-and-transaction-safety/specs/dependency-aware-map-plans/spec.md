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

### Requirement: Repeated plan creation is bounded
The runtime MUST identify plan attempts by a semantic key containing task, stage, target, revision, operation, and root error and MUST stop unchanged attempts after a configured bound.

#### Scenario: Agent repeats an unchanged failed plan
- **WHEN** repeated `create_plan` calls have the same semantic key and no new input, revision, or successful predecessor
- **THEN** the runtime preserves the existing plan outcomes and returns a typed circuit-breaker result instead of overwriting the plan

#### Scenario: Authoritative input changes
- **WHEN** a new revision, required input, or successful predecessor changes the semantic plan state
- **THEN** the runtime permits a new plan attempt while retaining prior terminal outcomes for diagnosis
