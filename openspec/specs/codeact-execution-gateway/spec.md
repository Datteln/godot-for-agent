# codeact-execution-gateway Specification

## Purpose

Define the unified CodeAct tool execution gateway and its Editor routing and result-continuation responsibilities.

## Requirements

### Requirement: Gateway dispatches the unified CodeAct tool protocol
The backend SHALL dispatch `project.read`, `project.search`, `project.edit`, `shell.run`, `godot.headless`, `git.status`, `git.diff`, `skill.load`, `tool.search`, and allowlisted `godot.editor.*` calls through one Execution Gateway. It MUST validate task identity, role visibility, parameters, path policy, timeout, approval policy, and cancellation before dispatching and MUST return a typed result to the originating agent loop.

#### Scenario: Agent invokes an allowed project tool
- **WHEN** a programming agent invokes `project.search` in its allowed project scope
- **THEN** the Gateway executes the request through the approved implementation and returns a structured result to that same task execution

#### Scenario: Agent invokes an unavailable tool
- **WHEN** a role invokes a tool not in its effective tool set
- **THEN** the Gateway returns a typed authorization result and performs no side effect

### Requirement: Gateway owns Editor routing and result continuation
The Gateway MUST route Editor requests directly to the matching registered EditorPlugin and automatically continue the originating agent with the structured result or artifact reference. The frontend MUST NOT execute tools or submit a separate chat request containing tool results.

#### Scenario: Editor returns a screenshot artifact
- **WHEN** an allowed capture request succeeds
- **THEN** the Gateway records the artifact reference and delivers it to the original agent turn without frontend result forwarding

### Requirement: Delegation orchestration tools remain registered
The service MUST always register the delegation orchestration tools `create_plan`, `delegate`, and `delegate_many` on the server side, independent of the front-facing tool enable switch. The agent role tool-set resolution (effective tools) SHALL include them for roles that declare them, so a role prompt that mandates `create_plan` before delegation is satisfiable with the declared tools.

#### Scenario: Front tools disabled, orchestration tools available
- **WHEN** the service starts with front-facing tool registration disabled (CodeAct mode)
- **THEN** `create_plan`, `delegate`, and `delegate_many` remain in the tool registry and coordinator effective tools include all three

#### Scenario: Coordinator follows the mandated planning workflow
- **WHEN** a coordinator turn calls `create_plan` for a complex map task without first searching for it
- **THEN** the tool resolves from the registry and the frame-level planning route executes it, returning the plan tasks for `delegate_many`