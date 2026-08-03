## MODIFIED Requirements

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
The scheduler MUST bind predecessor typed results or artifact references into the successor input contract inside the scheduler error boundary. The planner SHALL receive a typed snapshot projection, the validator/compiler SHALL receive the semantic plan plus full snapshot reference, and the writer SHALL receive only compiled approved batch artifacts.

#### Scenario: Planner output feeds validator and compiler
- **WHEN** a planner step succeeds with a semantic route candidate
- **THEN** the validator/compiler receives that candidate and the exact authoritative snapshot reference as named contract inputs

#### Scenario: Planner output feeds writer
- **WHEN** validation and compilation succeed with approved batch artifacts
- **THEN** the writer receives those artifacts as named contract inputs rather than reconstructing a plan from write operations

#### Scenario: A binding path is invalid
- **WHEN** the successor contract refers to a path that is absent from the predecessor's typed result
- **THEN** the scheduler returns a stable typed `dependency_binding_failed` outcome instead of propagating an exception or HTTP 500

### Requirement: Writers execute only approved batches
The service layer MUST reject map writes that are not bound to a deterministic validator/compiler approval for the same target, layer, revision, snapshot digest, and batch fingerprint. Planner-produced naked atlas operations MUST NOT be treated as an approval contract.

#### Scenario: Unapproved edit batch is requested
- **WHEN** a writer receives an edit batch without a valid approval contract for the same target and revision
- **THEN** the system routes the work back to planning or refresh and does not synthesize platform parameters

#### Scenario: Planner supplies raw atlas operations
- **WHEN** a planner result contains exact atlas operations but no matching compiler approval
- **THEN** the writer rejects them without starting a transaction or mutating the map

## ADDED Requirements

### Requirement: Planning publication is independent from writer execution
The dependency graph MUST represent user-visible plan publication as a separate step from map writing. Publication SHALL accept either an approved plan or a validation-exhausted final candidate and SHALL accurately expose its execution status.

#### Scenario: Approved plan is published
- **WHEN** validation and compilation succeed
- **THEN** publication reports `planning_status=delivered` and `execution_status=approved` while the writer follows its approval dependency

#### Scenario: Exhausted plan is published
- **WHEN** all three planner candidates fail deterministic validation
- **THEN** publication reports `planning_status=delivered` and `execution_status=blocked_by_validation` while no writer dependency is satisfied
