# dependency-aware-map-plans Specification

## Purpose

Define executable dependency-aware map plans, typed step outcomes, explicit predecessor inputs, and approval-bound writer execution.

## Requirements

### Requirement: Plan dependencies are immutable and executable
The system SHALL preserve stable step ids and `depends_on` edges from plan creation through delegate scheduling.

#### Scenario: Plan is handed to delegate scheduling
- **WHEN** `create_plan` produces steps with dependencies
- **THEN** the scheduler consumes the same immutable step definitions without dropping or rewriting dependency edges

### Requirement: Success unlocks dependent steps
The scheduler MUST start a step only after every declared predecessor has completed successfully.

#### Scenario: All predecessors succeed
- **WHEN** all dependencies of a pending step have typed status `succeeded`
- **THEN** the scheduler makes that step runnable

#### Scenario: A predecessor fails
- **WHEN** any dependency fails, is cancelled, or is blocked
- **THEN** the scheduler does not start the dependent step and records a typed blocked result that identifies the predecessor

### Requirement: Predecessor results become explicit inputs
The scheduler MUST bind predecessor typed results or artifact references into the successor input contract.

#### Scenario: Planner output feeds writer
- **WHEN** a planner step succeeds with approved batch artifacts
- **THEN** the writer receives those artifacts as named contract inputs rather than reconstructing a plan from write operations

### Requirement: Writers execute only approved batches
The service layer MUST reject map writes that are not bound to a planner and validator approval contract.

#### Scenario: Unapproved edit batch is requested
- **WHEN** a writer receives an edit batch without a valid approval contract for the same target and revision
- **THEN** the system routes the work back to planning and does not synthesize platform parameters
