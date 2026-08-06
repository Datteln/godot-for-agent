## ADDED Requirements

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
