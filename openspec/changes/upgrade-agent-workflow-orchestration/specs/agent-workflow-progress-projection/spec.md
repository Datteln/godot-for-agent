## ADDED Requirements

### Requirement: Graph lifecycle is visible through the authoritative transcript
The service SHALL project PlanGraph creation, step lifecycle transitions, owner summaries, dependency blocks, and terminal plan outcome through the existing authoritative chat transcript and live event transport. The client MUST render these entries through its existing transcript projection path.

#### Scenario: A graph plan starts
- **WHEN** a validated PlanGraph is persisted
- **THEN** the transcript contains a plan entry with its ordered steps and initial statuses before the first owner starts

#### Scenario: A step becomes blocked
- **WHEN** a predecessor's terminal outcome blocks a dependent step
- **THEN** the transcript contains progress or status information naming the dependent step and its root blocking reason

### Requirement: Owner progress does not create a second chat state system
Graph scheduling and owner publication events MUST NOT directly append unkeyed chat controls or require a separate polling path. HTTP commands remain command transport and existing WebSocket transcript patches remain the live visible transport.

#### Scenario: An owner completes through a graph
- **WHEN** an owner publishes a successful result
- **THEN** the client receives the visible update as a revision-aware transcript patch and does not append an independent legacy message

### Requirement: Plan completion is explicit and truthful
The scheduler SHALL publish one terminal plan outcome. It MUST report `completed` only when every required step has succeeded; otherwise it MUST report a non-success outcome that distinguishes blocked, failed, cancelled, or awaiting-confirmation state.

#### Scenario: One required step remains blocked
- **WHEN** all runnable work is exhausted but a required step is blocked
- **THEN** the final plan entry reports blocked and does not claim the user's overall task completed
