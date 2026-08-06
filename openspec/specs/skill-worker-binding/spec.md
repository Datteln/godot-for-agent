# skill-worker-binding Specification

## Purpose

Define explicit Skill resolution, context-scoped worker tools, fail-closed capability checks, and bounded worker result artifacts.

## Requirements

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

#### Scenario: Dynamic reader is created by a narrow map orchestrator
- **WHEN** the parent map orchestrator intentionally lacks context-read tools and creates a `read_only` dynamic Worker
- **THEN** the Worker Agent Interface is derived from its own mode Capability Contract and registered tools before Skill, stage, and permission filtering, rather than intersecting with the parent's tool list

#### Scenario: Dynamic Worker mode excludes writes
- **WHEN** a non-writing Worker derives its interface from registered tools
- **THEN** only tools declared for that Worker mode are retained and `delegate`, `delegate_many`, and `create_plan` remain unavailable

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

#### Scenario: Artifact reader receives an image reference
- **WHEN** `read_map_artifact` or `read_delegate_artifact` receives a `res://` or `user://` screenshot/image reference
- **THEN** it returns structured `incompatible_artifact_kind` with `actual_kind=image`, the expected artifact kind, and `recommended_tool=read_image_metadata` without attempting filesystem path concatenation

### Requirement: Visual review questions use the image-understanding side channel
`read_image_metadata` SHALL accept an optional bounded `question` and pass it separately from the media type to the configured asset-understanding model.

#### Scenario: Reader asks a visual confirmation question
- **WHEN** a compatible reader calls `read_image_metadata` with a screenshot and a question about appearance, visibility, composition, or obstruction
- **THEN** the runtime invokes image understanding with media type `image`, uses the question as the visual prompt, and returns the question with its semantic answer

#### Scenario: Reader asks for exact tile facts from a screenshot
- **WHEN** a question asks for an exact cell coordinate, column number, source id, atlas coordinate, or revision
- **THEN** tool guidance states that the visual answer is non-authoritative and directs exact-fact collection to `describe_map_context` or `describe_map_region`

### Requirement: Map planning skills consume an authoritative snapshot contract
A map planning skill that designs routes against existing map content MUST declare and receive a compatible authoritative snapshot input. Its instructions MUST distinguish snapshot-provided facts, planner-owned design fields, validator/compiler-owned write fields, and writer-owned transaction effects.

#### Scenario: Map-area expansion skill is bound
- **WHEN** a planner loads the map-area expansion skill for an existing map
- **THEN** the binding supplies a snapshot projection containing target, layer, revision, coverage, traversal profile, entry, boundary, frontier, and semantic resource references

#### Scenario: Required snapshot is absent
- **WHEN** a planning worker is created without a compatible snapshot artifact
- **THEN** worker construction or scheduling returns a typed missing-input outcome instead of asking the planner to infer exact facts from summaries

### Requirement: Planner scope cannot become atlas write authority
The effective planner contract MUST NOT authorize planner output as the source of naked per-operation atlas or GridMap item values. Exact resource facts SHALL remain available to the reader and validator/compiler scopes, and writer authority SHALL remain limited to approved compiled artifacts.

#### Scenario: Planner has context-read capability
- **WHEN** a planner can inspect a semantic resource or reference-cell fact for design purposes
- **THEN** that read does not authorize planner-authored raw atlas operations for writing

#### Scenario: Tool search discovers edit or resource-write tools
- **WHEN** the planning worker searches for globally registered write tools
- **THEN** binding reports them unavailable in planner scope and does not expand runtime authority

### Requirement: Planning skills request typed fact refreshes
When snapshot facts are incomplete or stale, the planning skill MUST return a typed refresh or recompute request that identifies the missing field and required coverage. It MUST NOT establish an untracked second fact baseline through ad hoc reads.

#### Scenario: Reachable frontier is missing
- **WHEN** the planner projection lacks a complete frontier required for connecting the route
- **THEN** the worker requests deterministic frontier recomputation for the snapshot target and revision

#### Scenario: Exact resource evidence is stale
- **WHEN** a semantic resource reference cannot be compiled against the snapshot
- **THEN** the worker requests reader/resource refresh and preserves the route candidate instead of guessing atlas coordinates

### Requirement: Map planning authority is bound to owner lineage
The runtime MUST resolve map planning capability only for a planner Frame whose parent is the persisted current map owner and whose role, child-local binding stage, task, workflow lineage, worker identity, Skill, and required planning-context bundle satisfy the frozen contract. Required context entries MUST be current for their own scopes but MUST NOT be required to share one target, layer, or revision.

#### Scenario: Current owner creates a compatible planner
- **WHEN** the current map owner creates a `propose_only` worker with the required planner Skill and planning-context bundle
- **THEN** binding resolves the planner capability for that child and only for its frozen scope

#### Scenario: Generic scheduler assigns planning to map-agent
- **WHEN** a generic plan step asks `map-agent` with orchestrator stage to plan route geometry
- **THEN** binding is `incompatible`, no planner capability is granted, and no LLM provider call begins

#### Scenario: Planner belongs to another owner
- **WHEN** an otherwise compatible planner Frame references a different parent owner or durable map task
- **THEN** binding is `incompatible` with a structured lineage mismatch

### Requirement: Map worker inputs are stage scoped
The system SHALL derive each map worker's authoritative inputs from its stage contract. Planner binding MUST include declared route-design facts such as exact cell and occupancy data through independently identified planning contexts, while compiler, writer, and reviewer bindings MUST receive only the immutable candidates, operations, batches, execution scopes, facts, and artifacts required by their stages. Planning contexts SHALL NOT grant mutation authority.

#### Scenario: Planner binding is compressed for a model request
- **WHEN** conversation history or snapshot presentation is summarized
- **THEN** required authoritative planner fields remain runtime-bound and their authority does not depend on the summary text

#### Scenario: Map owner lacks direct exact-fact tools
- **WHEN** the map owner needs missing route-design facts
- **THEN** it creates a compatible reader child and binds the resulting or refreshed context entries to the planner rather than reading or inventing the facts itself

#### Scenario: Planner uses several context roles
- **WHEN** route design needs gameplay occupancy, multiple backgrounds, and a regional frontier
- **THEN** planner binding resolves all required context roles independently and does not collapse them into one synthetic target scope

#### Scenario: Writer receives planner references only
- **WHEN** a writer request contains planning contexts but lacks deterministically compiled operations and approved execution scopes
- **THEN** binding fails closed and grants no map mutation capability

### Requirement: Worker creation is domain-owner controlled
The generic macro scheduler MUST dispatch a domain owner but MUST NOT supply `worker_spec` or directly create a specialist-internal dynamic worker. Dynamic map workers SHALL be created only through the current map owner's allowed stage transitions.

#### Scenario: Macro step becomes runnable
- **WHEN** a map-domain macro step is ready
- **THEN** the scheduler starts or resumes the map owner without selecting its reader, planner, writer, or reviewer worker specification

### Requirement: Worker result authority requires a closed stage contract
The runtime MUST assign `map_worker_result_v1`, a worker instance identity, specialized result constraints, and allowed worker transitions only when the Frame carries a valid worker-stage contract whose stage is in the closed specialist set and whose role is compatible with that stage. Domain-owner metadata or a non-empty generic map contract MUST NOT grant worker result authority.

#### Scenario: Frame factory receives an owner contract
- **WHEN** the contract identifies a map owner and durable domain task but no specialist worker stage
- **THEN** the Frame receives no worker result schema, worker instance identity, or worker next-stage contract

#### Scenario: Frame factory receives a planner contract
- **WHEN** the contract identifies planner stage, current owner lineage, worker identity, Skill, and required planning-context bundle
- **THEN** the Frame receives the specialized planner result contract for that frozen workflow and context binding

#### Scenario: Role and contract stage disagree
- **WHEN** an orchestrator role receives a planner-stage contract or a specialist role receives an owner contract
- **THEN** binding fails closed with a typed role/contract mismatch before provider invocation

### Requirement: Skill binding stage is derived from the requested child contract
For a specialist map child, the runtime MUST derive `worker_binding_stage` from the closed worker-stage contract and use it for Skill compatibility and effective-tool resolution. It MUST NOT use the owner's previous persisted `task_stage` as the child's Skill stage. The runtime SHALL preflight any task-stage transition without mutation and SHALL commit it with child lineage only after child construction succeeds.

#### Scenario: Planner is requested while task stage is read
- **WHEN** a valid planner contract is requested from a workflow checkpoint whose persisted task stage is `read`
- **THEN** planner Skills are evaluated with `worker_binding_stage=plan` and the legal task-stage transition is committed only with successful child start

#### Scenario: Planner Skill is widened to read
- **WHEN** a planner-only Skill would bind solely because its compatible stages were broadened to include `read`
- **THEN** contract tests reject the configuration as weakening the closed planner capability boundary

#### Scenario: Prompt construction fails after binding preflight
- **WHEN** the child Skill is compatible but prompt or Frame construction fails
- **THEN** the persisted task stage and child lineage remain unchanged and no provider call begins

### Requirement: Skill advertisement matches effective binding
The system prompt SHALL advertise a Skill as directly loadable only when the same binding resolver used by `load_skill` resolves it for the current Frame's role, binding stage, worker mode, effective tools, and permissions. Incompatible Skills MAY be represented as typed delegation hints but MUST NOT be listed as currently loadable.

#### Scenario: Coordinator sees planner Skills
- **WHEN** the coordinator prompt is assembled
- **THEN** map planner-only Skills are absent from its directly loadable Skill list

#### Scenario: Map owner sees planner Skills
- **WHEN** a map orchestrator prompt is assembled
- **THEN** planner-only Skills are not advertised as directly loadable and may only appear as instructions to create a compatible planner child

#### Scenario: Skill has no effective tools
- **WHEN** a Skill passes role or stage checks but its allowed or required tools have no intersection with the Frame's effective permitted tools
- **THEN** it is omitted from the directly loadable catalog and a direct `load_skill` call remains fail closed with `no_effective_tools`
