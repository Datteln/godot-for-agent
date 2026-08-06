# dependency-aware-map-plans Specification

## Purpose

Define executable dependency-aware map plans, typed step outcomes, explicit predecessor inputs, and approval-bound writer execution.

## Requirements

### Requirement: Plan dependencies are immutable and executable
The system SHALL preserve stable macro step ids and `depends_on` edges from `create_plan` through domain-owner scheduling. Each executable node SHALL represent a domain-owned outcome; specialist-internal stages and display milestones SHALL NOT become executable PlanGraph nodes.

#### Scenario: Plan is handed to delegate scheduling
- **WHEN** `create_plan` produces macro steps with dependencies
- **THEN** the scheduler consumes the same immutable step definitions without dropping or rewriting dependency edges

#### Scenario: One map objective has internal milestones
- **WHEN** a map macro step displays read, plan, preview, approval, write, and verify milestones
- **THEN** the PlanGraph contains one executable map outcome owned by one map-agent and no sibling node for any milestone

### Requirement: Success unlocks dependent steps
The scheduler MUST start an execution step only after every declared predecessor has completed successfully and every declared predecessor-result binding has been resolved into the successor contract. A plan-publication step MAY depend on an exhausted deterministic validation outcome, but that outcome MUST NOT satisfy or unlock a writer dependency.

#### Scenario: All predecessors succeed
- **WHEN** all dependencies of a pending execution step have typed status `succeeded` and their declared bindings resolve
- **THEN** the scheduler makes that step runnable

#### Scenario: A predecessor reaches terminal failure
- **WHEN** any dependency reaches an exhausted or proven permanent `failed`, explicit `cancelled`, or terminal `blocked` state
- **THEN** the scheduler does not start dependent execution steps and records a typed blocked result that identifies the predecessor

#### Scenario: A predecessor attempt is recoverable
- **WHEN** a dependency attempt requests reader recovery, a new attempt, authoritative refresh, or replan
- **THEN** the scheduler preserves the pending dependent step and does not propagate terminal blocked state while recovery remains available

#### Scenario: A predecessor result cannot be bound
- **WHEN** dependencies succeeded but a required result path or artifact reference is absent or malformed
- **THEN** the scheduler records a typed binding failure with the step, dependency, and path and creates no child Frame

#### Scenario: Planner validation budget is exhausted
- **WHEN** the third candidate fails deterministic validation
- **THEN** the scheduler runs final plan publication with the exhausted result, keeps the writer blocked, and creates no write Frame

### Requirement: Predecessor results become explicit inputs
The scheduler MUST bind declared predecessor domain-owner publication fields or artifact references into a successor owner's input contract inside the scheduler error boundary. One publication MAY expose multiple independently scoped output artifacts or an immutable execution-batch collection. The scheduler MUST NOT bind a domain's private planning contexts, internal child results, or reducer containers directly to another macro step.

#### Scenario: Planner output feeds validator and compiler
- **WHEN** a planner step succeeds with a semantic route candidate
- **THEN** the validator/compiler receives that candidate and the exact authoritative snapshot reference as named contract inputs

#### Scenario: Planner output feeds writer
- **WHEN** validation and compilation succeed with approved batch artifacts
- **THEN** the writer receives those artifacts as named contract inputs rather than reconstructing a plan from write operations

#### Scenario: Code output feeds a map outcome
- **WHEN** a code-domain owner completes with a declared scene or script artifact required by a map outcome
- **THEN** the scheduler binds that owner publication into the map owner's macro input contract

#### Scenario: Planner output feeds writer inside map workflow
- **WHEN** a planner child succeeds with candidate artifacts
- **THEN** the map owner binds those artifacts to its internal validator and writer workflow without exposing planner and writer as macro PlanGraph steps

#### Scenario: Map owner publishes multiple execution scopes
- **WHEN** one completed map outcome contains gameplay, background, and object-placement batches with different targets or layers
- **THEN** the owner publication exposes declared immutable output references that successors can bind without requiring one shared map target

#### Scenario: Successor requests a private planning context
- **WHEN** a macro predecessor binding addresses a planner context entry or internal child payload not declared by the owner publication
- **THEN** the scheduler returns `dependency_binding_failed` and does not expose the private workflow state

#### Scenario: A binding path is invalid
- **WHEN** the successor contract refers to a path absent from the predecessor owner publication
- **THEN** the scheduler returns a stable typed `dependency_binding_failed` outcome instead of propagating an exception or HTTP 500

### Requirement: Writers execute only approved batches
The service layer MUST reject map writes that are not bound to a deterministic validator/compiler approval for the same target, layer, revision, snapshot digest, and batch fingerprint. Planner-produced naked atlas operations MUST NOT be treated as an approval contract.

#### Scenario: Unapproved edit batch is requested
- **WHEN** a writer receives an edit batch without a valid approval contract for the same target and revision
- **THEN** the system routes the work back to planning or refresh and does not synthesize platform parameters

#### Scenario: Planner supplies raw atlas operations
- **WHEN** a planner result contains exact atlas operations but no matching compiler approval
- **THEN** the writer rejects them without starting a transaction or mutating the map

### Requirement: Worker stage transitions fail as typed plan outcomes
Worker contract construction and workflow stage transitions MUST execute inside a boundary that converts invalid transitions and payload construction failures into typed plan outcomes.

#### Scenario: Worker cannot enter write stage
- **WHEN** orchestration requests a transition to `write` that is invalid from the current workflow state
- **THEN** the step becomes `blocked` or `replan_required` with a stable error code and no unhandled server exception

#### Scenario: Task payload construction fails
- **WHEN** a runnable step cannot produce a payload conforming to its worker contract
- **THEN** the scheduler records the typed failure without creating a worker Frame or partially advancing workflow state

### Requirement: Plan attempts and terminal step outcomes are distinct
The scheduler MUST record each fallible execution as an attempt owned by a durable plan step. A failed attempt SHALL NOT make the step terminal while its typed recovery disposition permits reader recovery, retry, authoritative refresh, replacement, or replan. Only an exhausted or proven permanent failure may set terminal `failed` and propagate dependency blocking.

#### Scenario: Reader recovery can satisfy missing inputs
- **WHEN** a step attempt fails with typed missing inputs within its reader-recovery budget
- **THEN** the step remains non-terminal, a reader attempt is scheduled, and dependent steps remain pending

#### Scenario: Authoritative facts invalidate the current plan
- **WHEN** an attempt returns a revision or fact conflict with disposition `refresh_and_replan`
- **THEN** the scheduler records the attempt outcome, refreshes the authoritative input, and creates a new plan attempt without converting the durable task to terminal failure

#### Scenario: Recovery is exhausted
- **WHEN** all permitted recovery dispositions for a step reach their configured bounds
- **THEN** the scheduler records one terminal failed or paused outcome with the first root cause and only then propagates dependency blocking

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

### Requirement: Planning publication is independent from writer execution
The dependency graph MUST represent user-visible plan publication as a separate step from map writing. Publication SHALL accept either an approved plan or a validation-exhausted final candidate and SHALL accurately expose its execution status.

#### Scenario: Approved plan is published
- **WHEN** validation and compilation succeed
- **THEN** publication reports `planning_status=delivered` and `execution_status=approved` while the writer follows its approval dependency

#### Scenario: Exhausted plan is published
- **WHEN** all three planner candidates fail deterministic validation
- **THEN** publication reports `planning_status=delivered` and `execution_status=blocked_by_validation` while no writer dependency is satisfied

### Requirement: A map task has one open executable owner step
For one durable map task id, the macro scheduler MUST NOT run multiple sibling executable steps owned by separate `map-agent` Frames. Additional map progress phases SHALL be represented by the domain workflow or display milestones.

#### Scenario: Coordinator submits sibling map phases
- **WHEN** a macro plan attempts to schedule separate map-agent siblings for reading, planning, previewing, writing, or verifying one map task
- **THEN** plan validation rejects it with a typed ownership violation and requests regeneration as one domain-owned outcome

### Requirement: Domain publications unlock macro successors
A dependent macro step SHALL become runnable only when predecessor owner publications have statuses accepted by its input contract and all declared bindings resolve.

#### Scenario: Map owner is awaiting approval
- **WHEN** a successor requires the completed map artifact but its predecessor owner has published only `awaiting_confirmation`
- **THEN** the successor remains pending

#### Scenario: Owner completes with required artifact
- **WHEN** the predecessor publishes `completed` and the declared artifact binding resolves
- **THEN** the scheduler makes the successor runnable according to its immutable dependencies
