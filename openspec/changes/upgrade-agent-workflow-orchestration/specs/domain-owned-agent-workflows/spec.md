## ADDED Requirements

### Requirement: The coordinator owns macro planning and specialists own domain outcomes
Only the coordinator SHALL create or revise a macro PlanGraph. Each executable PlanGraph step MUST have exactly one registered domain owner Agent. A domain owner MUST receive an owner contract containing its step identity, objective, permitted input artifacts, and accepted publication statuses.

#### Scenario: The coordinator dispatches a scene outcome
- **WHEN** a dependency-ready scene step is scheduled
- **THEN** the service creates one scene-owner execution context with the step's owner contract and does not grant it authority to create sibling graph steps

### Requirement: Owners publish bounded structured outcomes
An owner SHALL publish a structured result containing its plan/step identity, status, human-readable summary, diagnostics, declared output artifacts, and next disposition. The scheduler MUST reject a result whose identity does not match the active owner or whose artifacts exceed configured schema and size constraints.

#### Scenario: A programming owner completes investigation
- **WHEN** the programming owner finishes a read-only investigation step
- **THEN** it publishes only its declared result artifacts and summary for successors rather than its complete message history

#### Scenario: An owner publishes an undeclared artifact
- **WHEN** an owner attempts to publish an artifact not permitted by its step contract
- **THEN** the scheduler records a typed owner-publication failure and does not expose the artifact to successors

### Requirement: Domain-internal workflow remains domain-owned
The generic scheduler SHALL treat an owner step as one macro outcome and MUST NOT turn the owner's tool calls, internal milestones, retry loops, or map-specific phases into sibling PlanGraph steps. A domain owner MUST NOT recursively create unrestricted Agents or alter graph dependencies.

#### Scenario: A map owner performs internal reads and validation
- **WHEN** a map owner needs read, plan, and validation work to finish its macro outcome
- **THEN** the graph continues to represent one map step while the owner controls its internal workflow

### Requirement: Confirmation suspends the same owner and blocks dependents
When an owner reaches an existing mutating-tool confirmation boundary, the system SHALL mark its step `awaiting_confirmation` and retain the same owner continuation identity. Approval MUST resume that step; denial or cancellation MUST publish a typed non-success outcome. A dependent step MUST NOT become runnable while the owner awaits or lacks a successful publication.

#### Scenario: A scene edit awaits user approval
- **WHEN** a scene owner proposes a confirmed mutation
- **THEN** the step is visible as awaiting confirmation and its dependent verification step does not start

#### Scenario: User rejects a pending mutation
- **WHEN** the user rejects the pending scene mutation
- **THEN** the scene owner publishes the rejected or cancelled outcome and the scheduler blocks its dependents without starting another writer

### Requirement: Write-capable owner execution is serialized
The scheduler MUST NOT run more than one graph-managed write-capable owner for the same project at a time. Read-only owner steps may be eligible for future parallel scheduling, but the initial implementation SHALL schedule all graph-managed steps serially.

#### Scenario: Two independent edit steps are ready
- **WHEN** two write-capable steps have no unfinished predecessors
- **THEN** the scheduler starts only one and retains the other as runnable until the active owner reaches a terminal outcome
