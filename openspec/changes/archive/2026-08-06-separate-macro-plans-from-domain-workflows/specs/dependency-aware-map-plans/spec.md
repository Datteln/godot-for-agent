## MODIFIED Requirements

### Requirement: Plan dependencies are immutable and executable
The system SHALL preserve stable macro step ids and `depends_on` edges from `create_plan` through domain-owner scheduling. Each executable node SHALL represent a domain-owned outcome; specialist-internal stages and display milestones SHALL NOT become executable PlanGraph nodes.

#### Scenario: Plan is handed to delegate scheduling
- **WHEN** `create_plan` produces macro steps with dependencies
- **THEN** the scheduler consumes the same immutable step definitions without dropping or rewriting dependency edges

#### Scenario: One map objective has internal milestones
- **WHEN** a map macro step displays read, plan, preview, approval, write, and verify milestones
- **THEN** the PlanGraph contains one executable map outcome owned by one map-agent and no sibling node for any milestone

### Requirement: Predecessor results become explicit inputs
The scheduler MUST bind declared predecessor domain-owner publication fields or artifact references into a successor owner's input contract inside the scheduler error boundary. One publication MAY expose multiple independently scoped output artifacts or an immutable execution-batch collection. The scheduler MUST NOT bind a domain's private planning contexts, internal child results, or reducer containers directly to another macro step.

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

## ADDED Requirements

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
