## Why

`create_plan` currently mixes two different responsibilities: coordinating large runtime steps across domains and describing a specialist's internal execution workflow. For map requests this causes route-planning objectives to be scheduled as ordinary sibling `map-agent` steps, so they can run under the orchestrator role instead of the typed planner pipeline. The result is repeated snapshot reading, unbounded reasoning, invalid worker output, chat timeout, and no map edit.

The first implementation exposed a second routing failure: a legitimate `map-agent` owner was created with an owner-stage contract but the Frame factory treated every non-empty map contract as a worker contract and assigned `map_worker_result_v1`. The pre-provider route guard then rejected the owner before its first agent turn, so it never had an opportunity to create reader or planner children. Unit tests modeled a contract-free owner and therefore did not reproduce the production `delegate_many -> map-agent owner -> run_turn` path.

Production traffic then exposed two deeper contract errors. First, planner and workflow-lifecycle contracts treated one `target_path/map_layer/revision` tuple as the identity of an entire planning task, even though a route can require Mid, Background, decoration, reference, and regional contexts at the same time. This caused missing-target exceptions and encouraged decorated storage keys to masquerade as canonical targets. Second, Skill binding validated a newly requested planner child against the map task's old global stage (`read`) instead of the child's declared planner stage (`plan`). The owner was therefore shown planner-only Skills it could not load, while a correctly typed planner delegation was rejected with `stage_incompatible` before it could start.

The system needs an explicit ownership boundary that works for both map and code tasks: the coordinator plans domain-level outcomes, while each domain owner plans and executes its own internal workflow.

## What Changes

- Redefine `create_plan` as a macro-plan API. Its executable steps describe domain-owned outcomes, dependencies, acceptance criteria, and optional display milestones; they do not describe specialist-internal agents, tools, or stages.
- Dispatch each macro step to one durable domain owner. The domain owner creates and controls any internal specialist workflow and child agents required to deliver the outcome.
- Make one `map-agent` the owner of a map task. Route design is performed only by a typed `map-planner-agent` child of that owner, using a runtime-bound collection of authoritative planning contexts; deterministic compilation and validation remain separate typed stages.
- Allow a planner to receive multiple independently identified planning contexts for gameplay, background, decoration, reference, and regional facts. Context entries may use different targets, layers, and revisions, are refreshed independently, and are reference inputs rather than write authority.
- Bind a concrete target, layer, and revision only when a deterministic compiler produces an execution operation or approved batch. Writer and reviewer stages validate those execution scopes individually; planner and lifecycle identity do not require every context to share one scope.
- Key owner and child lifecycle facts by workflow, durable task, owner lineage, child identity, and context references rather than by a mandatory map target. Internal decorated scope keys such as `TileMap::map_layer=1` MUST NOT be accepted as canonical `target_path` values.
- Represent domain-owner identity/lineage and specialist worker-stage execution with distinct contracts. A `map-agent` owner MUST NOT receive `map_worker_result_v1`, a worker instance identity, or worker-stage next transitions merely because it has `map_stage=orchestrator`.
- Reject map-planning operations before an LLM call when the caller is not the current map task's planner child or lacks the required snapshot binding. The guard must distinguish a legitimate owner startup from an orchestrator incorrectly bound to a planner/worker-stage contract; the `map-agent` orchestrator cannot silently act as the planner, but it must be allowed to run and create the planner child.
- Separate the persisted map-task stage from a specialist child's Skill-binding stage. Resolve planner, reader, writer, validator, repairer, and reviewer Skills from the child's frozen worker-stage contract, preflight the corresponding task-stage transition without mutation, and commit the transition plus child lineage only after child construction succeeds.
- Filter the Skill summaries presented to each Frame by role, worker stage, mode, effective tools, and permissions. Planner-only or tool-incompatible Skills MUST NOT be advertised as loadable by the coordinator or map owner, while runtime binding remains the final fail-closed check.
- Persist the macro plan and each domain workflow as separate state machines. Internal stage completion does not complete the macro step; the domain owner publishes a typed owner result such as `preview_ready`, `awaiting_confirmation`, `completed`, or `blocked`.
- Resume approval, retry, recovery, and continuation on the same map owner and task lineage instead of creating a new sibling agent.
- Treat route-contract violations as backend-owned routing failures with a machine-actionable correction or recovery path. The runtime MUST NOT pause for the user with an instruction that only an agent can perform.
- Commit completed tool results, authoritative snapshots, candidate plans, validation results, and publications at stage boundaries so a later model timeout or client cancellation cannot roll back already completed work.
- Add executable public-route integration coverage for `/chat -> coordinator -> create_plan -> delegate_many -> domain owner first turn -> typed child workflow`, including owner/worker contract separation, provider-call identities, invalid routing, timeout recovery, approval resumption, and cross-domain dependencies. A normalization-only test does not satisfy this coverage.
- **BREAKING**: `create_plan` steps are no longer executable descriptions of specialist-internal phases such as map read/plan/preview/write/verify. Callers that currently encode those phases as sibling plan steps must migrate to one domain-owned macro step with display-only milestones.

## Capabilities

### New Capabilities

- `domain-owned-execution-workflows`: Defines the boundary between coordinator macro plans and specialist-owned internal workflows, including durable domain ownership and typed owner outcomes.
- `map-domain-orchestration`: Defines the single-owner map workflow, multi-context typed planner-child routing, deterministic per-operation execution scopes, approval continuation, and route-contract rejection.

### Modified Capabilities

- `dependency-aware-map-plans`: Changes dependency-aware plans from maps of internal map-edit stages into macro dependencies between domain-owned outcomes, with domain publications satisfying successor inputs.
- `skill-worker-binding`: Strengthens map worker binding so authority comes from the child contract's role, stage, mode, tools, owner lineage, and planning-context bindings rather than the map task's previous global stage.
- `map-workflow-state-and-evidence`: Separates macro-plan state from map-domain workflow state and records owner identity, lineage, multi-context references, internal stages, execution scopes, and publications as reducer-owned facts.
- `atomic-tool-result-submission`: Makes completed stage evidence durable before subsequent model continuation so cancellation or timeout only discards the unfinished continuation.

## Impact

- Coordinator instructions and the public `create_plan` schema/normalization rules.
- Plan scheduling, delegation, domain-owner lifecycle, and cross-domain artifact handoff.
- Map agent definitions, owner/worker contract models, multi-context planning inputs, per-operation execution scopes, frame construction, result-schema assignment, route guards, worker binding, Skill prompt filtering, workflow reducers, approval routing, and deterministic planner validation.
- Recovery policy ownership and dispositions for internal routing failures.
- Tool-result submission and chat working-session transaction boundaries.
- Plan rendering, which must distinguish executable macro steps from display-only domain milestones.
- Unit and integration tests, especially multi-layer/background planning, failed child-construction rollback, Skill visibility/binding, and tests that construct a contract-free owner or normalize a plan without executing the real public chat/delegation route.
