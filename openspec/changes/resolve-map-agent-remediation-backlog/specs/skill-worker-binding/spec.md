## ADDED Requirements

### Requirement: Skill binding has explicit resolution states
The system SHALL resolve each requested Skill as exactly one of `resolved`, `missing`, or `incompatible`.

#### Scenario: Skill is available and compatible
- **WHEN** a Skill exists, is enabled, and its required capabilities are available to the current Agent, stage, and worker mode
- **THEN** the binding result is `resolved` and includes the effective capability and tool set

#### Scenario: Skill is absent or disabled
- **WHEN** a requested Skill cannot be found or is disabled
- **THEN** the binding result is `missing` and worker creation is rejected

#### Scenario: Skill conflicts with worker context
- **WHEN** a Skill exists but its role, stage, mode, or required capabilities do not match the worker context
- **THEN** the binding result is `incompatible` with structured reasons and worker creation is rejected

### Requirement: Effective tools are context-scoped
The system MUST derive effective tools from the intersection of Agent Interface, stage/mode Capability Contract, permissions, and resolved Skill capabilities.

#### Scenario: Global registry contains extra tools
- **WHEN** a Skill is loaded in a worker whose stage cannot use some globally registered tools
- **THEN** those tools are excluded from the binding and from the worker's callable tool set

### Requirement: Callers cannot supply a duplicate tool whitelist
Dynamic worker requests MUST NOT accept an `allowed_tools` field as an authority for tool reachability.

#### Scenario: Legacy request includes allowed_tools
- **WHEN** a dynamic worker request includes `allowed_tools`
- **THEN** the system rejects or ignores the field according to migration mode and derives tools from the binding contract

### Requirement: Skill loading reports current binding
`load_skill` SHALL return the binding resolved for the current Agent, stage, and mode rather than a global registry projection.

#### Scenario: Skill is loaded by a restricted worker
- **WHEN** a restricted worker calls `load_skill`
- **THEN** the response lists only tools and capabilities effective in that worker context

### Requirement: Tool guidance matches artifact kind and effective scope
The runtime MUST generate artifact-reading guidance from the artifact kind and the current Agent's effective tool set and MUST NOT direct an Agent to call a scope-excluded tool.

#### Scenario: Map artifact is returned to map orchestrator
- **WHEN** a map-tool result includes a Session artifact locator
- **THEN** its guidance identifies the compatible map artifact reader and does not instruct the Agent to use unavailable `read_file`

#### Scenario: Delegate artifact is returned
- **WHEN** a child Frame produces a delegate-schema artifact
- **THEN** the guidance identifies the delegate artifact reader and does not present that reader as compatible with raw map-tool entries

### Requirement: Tool search cannot expand runtime authority
`search_tools` MUST NOT activate a tool excluded by the current Agent Interface, stage or mode Capability Contract, or permission scope.

#### Scenario: Restricted Agent searches for a globally registered tool
- **WHEN** the registry contains `read_file` or `describe_map_context` but the current Agent contract excludes it
- **THEN** search reports `unavailable_in_agent_scope` and leaves the callable tool set unchanged

### Requirement: Map context reads route to a compatible reader
The scheduler SHALL route requests for complete map context, scene-tree facts, or exact map facts to an Agent or worker whose resolved binding includes the required context-read capability.

#### Scenario: Map orchestrator needs scene-tree facts
- **WHEN** the map orchestrator lacks `read_scene_tree` or `describe_map_context`
- **THEN** it delegates the focused read to a compatible reader instead of repeatedly searching for or directly invoking excluded tools

### Requirement: Artifact readers enforce their schemas
The map artifact reader MUST resolve Session `map_artifacts.json` entries by turn and tool-use id, while the delegate artifact reader MUST continue to accept only delegate-schema artifacts.

#### Scenario: Delegate reader receives a raw map artifact locator
- **WHEN** `read_delegate_artifact` is called with a locator for a raw map-tool entry
- **THEN** it returns a structured incompatible-artifact-kind error without attempting to treat the map document as a delegate artifact
