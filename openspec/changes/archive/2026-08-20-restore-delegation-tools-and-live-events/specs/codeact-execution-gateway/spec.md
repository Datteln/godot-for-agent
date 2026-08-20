## ADDED Requirements

### Requirement: Delegation orchestration tools remain registered
The service MUST always register the delegation orchestration tools `create_plan`, `delegate`, and `delegate_many` on the server side, independent of the front-facing tool enable switch. The agent role tool-set resolution (effective tools) SHALL include them for roles that declare them, so a role prompt that mandates `create_plan` before delegation is satisfiable with the declared tools.

#### Scenario: Front tools disabled, orchestration tools available
- **WHEN** the service starts with front-facing tool registration disabled (CodeAct mode)
- **THEN** `create_plan`, `delegate`, and `delegate_many` remain in the tool registry and coordinator effective tools include all three

#### Scenario: Coordinator follows the mandated planning workflow
- **WHEN** a coordinator turn calls `create_plan` for a complex map task without first searching for it
- **THEN** the tool resolves from the registry and the frame-level planning route executes it, returning the plan tasks for `delegate_many`