# map-domain-orchestration Specification

## Purpose

Define the single-owner map workflow, multi-context typed planner-child routing, deterministic per-operation execution scopes, approval continuation, and route-contract rejection.

## Requirements

### Requirement: One map agent owns a map task lineage
The system MUST assign one durable `map-agent` owner to a map task lineage. All reader, planner, validator, publication, approval, writer, reviewer, retry, and recovery activity for that task MUST be created or resumed under that owner.

#### Scenario: A new map macro outcome starts
- **WHEN** the macro scheduler dispatches a new map-domain outcome
- **THEN** it creates one map owner and records the owner Frame id with the durable map task id

#### Scenario: The same task resumes
- **WHEN** the task resumes after approval, timeout, reconnect, or process recovery
- **THEN** the runtime selects the recorded owner and restores its internal workflow instead of creating another map-agent sibling

### Requirement: Route design belongs only to a planner child
Only a child Frame of the current map owner with role `map_planner`, `map_stage=planner`, a frozen matching task/workflow/worker contract, and a current planning-context bundle SHALL perform route design or produce a candidate route. Planning authority MUST NOT depend on every context entry having the same target, layer, or revision.

#### Scenario: Map owner reaches planning
- **WHEN** the map owner determines that route design is the next internal stage
- **THEN** it creates or resumes a typed `map-planner-agent` child and supplies the runtime-bound planning-context bundle

#### Scenario: Route design uses gameplay and background contexts
- **WHEN** the route depends on a Mid gameplay layer, multiple Background layers, and a reference region
- **THEN** one planner child receives independently identified context entries for all required roles without a cross-entry target, layer, or revision equality check

#### Scenario: Map orchestrator attempts route design
- **WHEN** a Frame with `map_stage=orchestrator` invokes the planning operation or is scheduled with a route-design contract
- **THEN** the runtime returns `map_route_contract_violation` before any LLM provider call and does not accept prose as a candidate route

#### Scenario: Legitimate map owner starts orchestration
- **WHEN** the current map owner has an owner contract and no specialist worker result schema or route-design contract
- **THEN** the route guard permits its orchestration turn so it can create or resume the next typed child

#### Scenario: Unrelated planner attempts the task
- **WHEN** a planner Frame is not a child of the recorded owner or its task, workflow lineage, worker instance, or required context binding differs
- **THEN** the runtime rejects the operation without advancing map workflow state

### Requirement: Planning facts are runtime bound and stage scoped
The runtime MUST bind authoritative route-design facts required by the planner contract through an ordered planning-context bundle. Each entry MUST carry a stable context identity, semantic role, provenance and digest, canonical target locator when applicable, layer or region, source revision, declared facts, and independent freshness state. Exact cell coordinates and occupancy facts required by the planner MUST remain runtime bound. Write-critical atlas resolution MUST remain in the deterministic compiler/writer path and MUST NOT rely on compressed conversation context or planning-reference authority.

#### Scenario: Planner context bundle is constructed
- **WHEN** a typed planner child starts for a multi-layer or multi-background objective
- **THEN** its contract identifies every required context entry and each entry includes the route-design fields and provenance declared for its semantic role

#### Scenario: A required context entry is absent or stale
- **WHEN** the planner Frame lacks a required semantic role, field, digest, context identity, or current source revision for that entry
- **THEN** the runtime requests targeted reader recovery or returns a typed missing-input outcome before provider invocation without discarding unrelated current entries

#### Scenario: One background entry is refreshed
- **WHEN** a reader publishes a current replacement for one stale background context
- **THEN** the context registry replaces only that stable entry and preserves all unrelated planning contexts

#### Scenario: Writer batch is compiled
- **WHEN** an approved route candidate is converted into tile mutations
- **THEN** deterministic compiler output creates one or more immutable operations, each with one canonical target, layer, expected revision, and exact atlas/cell write parameters, rather than asking the planner or map owner to reconstruct them from prose

### Requirement: Planning references and execution scopes have separate authority
Planning-context entries SHALL provide read authority only. Deterministic compiler output, approved batches, writer calls, and reviewer evidence MUST bind and validate each concrete execution scope independently. Internal compound index keys MUST NOT satisfy canonical target fields.

#### Scenario: Planner contexts use different revisions
- **WHEN** gameplay and background facts are current for their own independently versioned scopes
- **THEN** the planner may use both contexts and the compiler resolves revision guards separately for generated execution operations

#### Scenario: Decorated key is presented as a target
- **WHEN** an internal key such as `TileMap::map_layer=1` or `TileMap::revision=0` is supplied where a canonical `target_path` is required
- **THEN** execution-scope validation rejects it rather than interpreting the decoration as part of the target locator

#### Scenario: One operation has a stale revision
- **WHEN** an approved multi-operation batch contains one execution scope whose current revision no longer matches
- **THEN** the runtime blocks that operation or the batch according to its atomicity contract without treating unrelated planning contexts as stale

### Requirement: The map owner advances a typed internal stage graph
The map domain workflow SHALL use explicit reader, planner, deterministic compile/validate, publication, approval, writer, and reviewer stages with frozen transitions. The owner MUST NOT replace a failed typed stage with an untyped reasoning loop.

#### Scenario: Planner child is requested from read stage
- **WHEN** the current owner has valid planning contexts and requests a planner child while the persisted task stage is `read`
- **THEN** the runtime preflights the legal `read -> plan` transition, validates the child using binding stage `plan`, and atomically commits the transition with child lineage after construction succeeds

#### Scenario: Planner child construction fails
- **WHEN** its role, input, Skill, prompt, or Frame contract fails before child-start commit
- **THEN** the task stage and child lineage remain unchanged and no planner provider call occurs

#### Scenario: Planner candidate fails deterministic validation
- **WHEN** the compiler or validator rejects a candidate and the existing bounded planner retry budget remains
- **THEN** the owner schedules the next typed repair attempt with the validator diagnostics and preserves the same task lineage

#### Scenario: Planner attempts are exhausted
- **WHEN** the third permitted planning attempt cannot pass deterministic validation
- **THEN** the existing planner publication policy emits its required final typed planning outcome and the owner publishes a resumable or terminal status instead of continuing until chat timeout

#### Scenario: Candidate is approved and written
- **WHEN** publication, user approval, and writer contracts reference the same immutable operation or batch identities and every execution scope passes its own revision guard
- **THEN** the owner advances to writer and reviewer without asking the macro scheduler to create those stages

### Requirement: Map owner status is machine actionable
The map owner MUST publish `preview_ready`, `awaiting_confirmation`, `completed`, or `blocked` with stable task, owner, checkpoint, output or batch references, applicable execution-scope revisions, and recovery fields. A publication MUST NOT require one target or revision when the outcome contains several scopes. Chat prose MUST NOT be the source of map workflow status.

#### Scenario: Preview is ready
- **WHEN** a candidate and deterministic validation publication are durable
- **THEN** the owner publishes `preview_ready` or `awaiting_confirmation` with the candidate artifact and approval identity

#### Scenario: Verification completes
- **WHEN** writer and reviewer evidence satisfy the completion gate
- **THEN** the owner publishes `completed` with committed operation or batch identities, their resulting revisions, and evidence references

### Requirement: Route guards distinguish owner and worker contract kinds
The runtime MUST determine specialist result-schema assignment and route-guard behavior from an explicit versioned contract kind and a closed worker-stage set. It MUST NOT classify a Frame as a map worker solely because a map-related contract dictionary is non-empty.

#### Scenario: Owner is incorrectly bound to a worker contract
- **WHEN** a Frame with owner role or `map_stage=orchestrator` carries a worker-stage contract or `map_worker_result_v1`
- **THEN** the runtime rejects it before provider invocation and records the exact role/contract mismatch

#### Scenario: Typed child has a matching contract
- **WHEN** a reader, planner, writer, validator, repairer, or reviewer child has a matching worker-stage contract and owner lineage
- **THEN** the runtime assigns the specialized worker result schema and permits the stage according to its remaining capability checks

#### Scenario: Owner contract is non-empty
- **WHEN** an owner contract contains task and lineage fields
- **THEN** the runtime does not derive a worker schema, worker identity, or worker transitions from its truthiness

### Requirement: Internal routing failures are backend recoverable
A route-contract violation with no side effect MUST produce a backend-owned machine-actionable recovery disposition. It MUST NOT pause for the user or instruct the coordinator to create a specialist child that only the current map owner is authorized to create.

#### Scenario: Malformed owner Frame is detected before a provider call
- **WHEN** the owner was incorrectly assigned worker-only fields and no map side effect occurred
- **THEN** backend recovery discards or repairs the malformed Frame and resumes the recorded owner checkpoint without asking the user to retry the request

#### Scenario: Worker routing cannot be repaired safely
- **WHEN** persisted state cannot prove the owner lineage or side-effect state required for automatic reconstruction
- **THEN** the runtime records a typed backend routing problem and blocks mutation without representing missing internal routing as missing user intent

### Requirement: Public-route coverage executes owner and child creation
Automated public-route verification MUST execute coordinator planning, `create_plan`, macro delegation, map-owner Frame creation, the owner's first orchestration turn, and typed planner-child creation. Tests that stop at plan normalization or call the route guard in isolation SHALL NOT satisfy this requirement.

#### Scenario: A pure map request follows the public route
- **WHEN** the integration fixture submits a complex map-edit request through the chat entry point
- **THEN** it observes one map owner without a worker result schema, one planner child with a matching worker contract, child-local `plan` Skill binding, a multi-context planning bundle, and provider calls under the expected Frame identities

#### Scenario: Skill catalog is rendered for owner and planner
- **WHEN** public-route prompts are built for the coordinator, map owner, and planner child
- **THEN** planner-only Skills are advertised as loadable only to the compatible planner and unavailable-tool Skills are not advertised as directly loadable

#### Scenario: Owner is misclassified as a worker
- **WHEN** a regression assigns `map_worker_result_v1` to the owner before its first turn
- **THEN** the public-route test fails before it can be reported as passing owner-to-planner delegation coverage

### Requirement: Visible map planning is decided from structured task attributes
The runtime MUST classify a requested Map operation from normalized attributes including read-only versus mutation intent, explicit target, bounded operation count and extent, known resource/cell inputs, dependency on current Map facts, validation need, and approval need. It MUST NOT decide plan requirements by counting language-specific substrings.

#### Scenario: Explicit atomic map edit is requested
- **WHEN** the request resolves to one explicit target, one bounded mutation, known resource and cell inputs, and no read, planning, validation, or multi-scope dependency
- **THEN** runtime classification may permit delegation without a visible macro plan while retaining ordinary approval and revision guards

#### Scenario: Complex or ambiguous mutation is requested
- **WHEN** the request affects multiple cells or objects, depends on current Map facts, needs route or layout design, spans scopes, requires validation, or lacks enough attributes to prove atomicity
- **THEN** runtime classification requires `create_plan` before map-owner delegation

#### Scenario: LLM labels a complex task atomic
- **WHEN** model-proposed routing attributes conflict with deterministic operation facts or omit required fields
- **THEN** runtime validation rejects atomic classification and requires a plan without granting additional authority

#### Scenario: Non-mutating map request is submitted
- **WHEN** the user asks only to inspect, explain, or analyze Map state
- **THEN** classification does not manufacture edit intent or mutation authorization

### Requirement: Map orchestration is isolated behind a domain policy
Map routing, stage capabilities, persistent budgets, structured completion, validation, write guards, workflow continuation, and recovery MUST be implemented behind a small `MapTurnPolicy` adapter and cohesive subordinate Map-domain transition handlers. Frame lifecycle, structured completion, planning, delegation, tool arguments, tool guards, tool dispatch classification, budgets, and Map event projection MUST each have one explicit owner. Generic TurnDriver code MUST depend only on the domain-policy interface, execute the returned typed directives through declared ports, and MUST NOT inspect concrete Map tool names or Map-specific Session fields. `MapTurnPolicy` MUST NOT own a second complete generic model/tool loop.

#### Scenario: Generic turn driver dispatches a Map operation
- **WHEN** model output contains a Map-domain tool or completion decision
- **THEN** generic classification delegates the domain decision to MapTurnPolicy and applies the returned typed directive

#### Scenario: Domain rule leaks into generic core
- **WHEN** an architecture check finds a Map implementation import, Map tool literal, or Map-specific state branch in the turn core
- **THEN** the check fails and identifies the dependency violation

#### Scenario: Shared model behavior changes
- **WHEN** model, effort, provider, permission, or generic tool-protocol behavior changes
- **THEN** the shared turn core changes once without creating a separate complete Map pipeline

#### Scenario: One Map transition is evaluated
- **WHEN** a model response or committed tool result requires Map routing, planning, delegation, validation, completion, or recovery behavior
- **THEN** the policy selects the owning transition handler and returns a typed directive or domain result without hiding the effect inside a monolithic run method

#### Scenario: Map handler dependency direction is inspected
- **WHEN** architecture checks inspect the Map turn package
- **THEN** policy and execution adapters may depend on leaf handlers, leaf handlers do not import those adapters, and no old `map_turn_pipeline` compatibility module or re-export remains