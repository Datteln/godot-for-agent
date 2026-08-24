## ADDED Requirements

### Requirement: Complex plans are validated durable dependency graphs
The system SHALL persist a PlanGraph before starting a graph-managed complex task. Every step MUST have a stable unique ID, registered owner Agent, non-empty objective, declared dependency IDs, declared input bindings, and a lifecycle status. The system MUST reject a graph with unknown owners, duplicate IDs, missing dependencies, self-dependencies, or dependency cycles without starting an Agent.

#### Scenario: A valid cross-domain plan is accepted
- **WHEN** the coordinator creates a plan whose registered steps have unique IDs and acyclic dependencies
- **THEN** the service persists the PlanGraph and projects the plan before any owner step starts

#### Scenario: A plan contains a cycle
- **WHEN** a plan makes step A depend on step B and step B depend on step A
- **THEN** the service records a typed plan-validation failure and starts no owner step

### Requirement: Scheduler selects only dependency-ready steps
The scheduler SHALL derive runnable steps from durable PlanGraph state rather than a model-selected delegation order. A step MUST become runnable only when every predecessor has published a successful result and every declared input binding resolves to a declared predecessor artifact. The initial implementation MUST start at most one graph-managed step at a time per session.

#### Scenario: Independent precursor steps finish before a dependent writer
- **WHEN** a writer step depends on successful resource and scene investigation steps
- **THEN** the scheduler does not start the writer until both predecessor publications and its declared bindings are available

#### Scenario: A predecessor result is missing its declared artifact
- **WHEN** all predecessor steps are successful but a successor binding references no published artifact
- **THEN** the successor becomes blocked with a typed dependency-binding failure and no owner Frame is created

### Requirement: Terminal precursor failure blocks dependents
The scheduler SHALL not start a step whose predecessor has terminal `failed`, `blocked`, `cancelled`, or rejected-confirmation outcome. It MUST retain the dependent step and publish a typed blocked result that identifies the root predecessor and disposition.

#### Scenario: A read step fails
- **WHEN** a required read owner publishes a terminal failure
- **THEN** each dependent step is marked blocked and no write-capable owner starts

### Requirement: Graph scheduling preserves direct and legacy workflows
The system SHALL continue to support direct coordinator execution and existing single/legacy sequential delegation for requests that do not enter a validated PlanGraph.

#### Scenario: A small direct request is submitted
- **WHEN** a request is handled without creating a graph-managed plan
- **THEN** it follows the existing direct execution path without requiring PlanGraph state
