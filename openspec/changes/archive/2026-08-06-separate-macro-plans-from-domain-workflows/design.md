## Context

The coordinator currently uses `create_plan` to describe both cross-domain execution and the internal phases of a map edit. A complex map request is commonly expanded into sibling steps such as read, plan, preview, write, and verify, all assigned to `map-agent`. Because the public `create_plan` contract cannot carry the typed worker contract used by the map pipeline, the nominal planning step starts another map orchestrator rather than a `map-planner-agent`. It then returns ordinary prose where the runtime expects a typed worker result and can spend the entire model budget retrying or reasoning until the chat request times out.

The first implementation corrected the macro-plan shape but conflated owner and worker contracts during Frame creation. The production path created `f2` as `agent=map-agent, map_stage=orchestrator`, attached an owner-stage contract, and then assigned `map_worker_result_v1` solely because that contract was non-empty. The route guard observed `orchestrator + worker result schema` and stopped the Frame before its first agent turn. Consequently the legitimate owner could not invoke `delegate` to create its reader or planner child. The existing guard test used an unrealistic contract-free owner, while the test named as a public-route test stopped at plan normalization and never executed delegation or `run_turn`.

Later production traces exposed two additional modeling faults. A planner and its lifecycle events were forced to carry one non-empty `target_path/map_layer/revision`, although the requested route depended simultaneously on Mid, Background, decoration, and regional reference facts. Separately, child prompt construction validated planner-only Skills against the persisted map task stage, which was still `read`, instead of the requested child's frozen planner stage. The prompt advertised those same Skills to coordinator and owner Frames that could never bind them. This produced defensive `role_incompatible` and `no_effective_tools` failures followed by a real `stage_incompatible` rejection of a legitimate planner delegation.

The repository already has useful typed map stages, immutable Frame contracts, authoritative snapshots, deterministic validation, bounded planning attempts, publication, approval, writing, review, and recovery semantics. This change does not replace that data-plane work. It changes the routing and ownership model so the public path actually enters it.

The architecture must support map work, code work, and plans that connect multiple domains. It must also preserve completed front-tool and workflow facts when a later orchestration/model continuation is cancelled.

## Goals / Non-Goals

**Goals:**

- Make `create_plan` describe large domain-owned outcomes and cross-domain dependencies only.
- Give each executable macro step one durable domain owner that controls its internal workflow.
- Ensure one `map-agent` owns a map task and creates a typed planner child for route design.
- Allow that planner to consume multiple independently versioned map planning contexts without treating any one context as the workflow identity or write authority.
- Keep domain-owner contracts and specialist worker-stage contracts structurally distinct throughout creation, persistence, hydration, and recovery.
- Resolve Skill authority from the specialist child's contract stage, role, mode, effective tools, and permissions rather than from the map task's previous global stage.
- Commit a child-start stage transition and its lineage atomically only after all contract, Skill, prompt, and Frame preflight checks succeed.
- Make illegal planner routing fail before an LLM request consumes time or tokens.
- Allow a legitimate map owner to complete its first orchestration turn and create the next typed child.
- Keep macro-plan state and map-domain workflow state independently durable and explicitly linked.
- Preserve completed stage evidence across a later model timeout, client disconnect, or cancellation.
- Test the real public route rather than only constructing an ideal internal worker graph.

**Non-Goals:**

- Moving route geometry design into the coordinator.
- Making the generic PlanGraph understand map cells, atlas coordinates, occupancy, platform geometry, or writer batches.
- Replacing the existing deterministic planner compiler, validator, three-attempt policy, publication policy, approval gate, writer, or reviewer.
- Allowing the map orchestrator to fall back to prose route planning when typed planner creation fails.
- Weakening worker role, lineage, planning-context provenance, execution-scope, or result-schema validation in order to let the owner pass the guard.
- Allowing planner reference contexts to grant write authority or requiring unrelated contexts to share one target, layer, or revision.
- Inferring authoritative stages, roles, or permissions from natural-language task text.

## Decisions

### 1. Introduce two explicit plan levels

`MacroPlan` is coordinator-owned. A `MacroPlanStep` contains a stable id, `owner_agent`, domain, objective, acceptance contract, dependencies, predecessor bindings, and optional `display_milestones`. The milestones are presentation metadata and never become scheduler nodes or Frames.

`DomainWorkflow` is owner-owned. Its stages, children, retries, artifacts, approval gates, and tools use the domain's typed contracts. The generic scheduler can observe owner publications but cannot schedule an internal domain stage directly.

This separation is preferred over adding `worker_spec` to `create_plan`: exposing internal worker construction to the coordinator would preserve the original responsibility leak and make every domain's private workflow part of the public planning schema.

### 2. Dispatch one durable owner per domain task

The macro scheduler creates or resumes one owner Frame identified by `(session_epoch, durable_task_id, domain, domain_task_id)`. For one map-edit objective, the coordinator emits one executable map macro step. Re-entry, approval, retries, and recovery resolve that identity and resume the same `map-agent` lineage.

The scheduler treats `preview_ready` and `awaiting_confirmation` as non-terminal owner publications. Only `completed`, terminal `blocked`, explicit `cancelled`, or proven permanent `failed` determines the macro step's terminal state. Internal child completion never completes the macro step by itself.

This is preferred over sibling `map-agent` steps because sibling Frames have no authoritative parent-child contract and cannot prove that a planner belongs to the active map task.

Owner identity uses a `DomainOwnerContract` containing the macro step, durable task, domain task, owner Frame, lineage, and accepted publication statuses. It does not contain a worker instance id, `map_worker_result_v1`, worker-stage transitions, planner context binding, execution scope, or a specialist result contract. A map owner can carry `map_stage=orchestrator` as agent metadata without becoming a map worker.

### 3. Make map workflow routing structural and fail closed

The map owner advances this typed internal graph:

`reader -> planner -> deterministic compile/validate -> publication -> approval -> writer -> reviewer`

Repair and refresh edges remain governed by the existing bounded planner/recovery specifications. A runtime planning entry point accepts work only when all of these facts match persisted state:

- the caller Frame has planner role and `map_stage=planner`;
- its parent is the current map owner;
- its task, workflow lineage, and worker instance match the frozen contract;
- every required planning-context reference is present, current for its own scope, and provenance-bound to the contract.

Failure produces `map_route_contract_violation` before provider invocation. Neither an agent name nor task prose can establish planning authority.

`MapWorkerStageContract` is a separate closed contract whose stage is one of `reader`, `planner`, `writer`, `validator`, `repairer`, or `reviewer`. Only this contract assigns a worker instance identity, `map_worker_result_v1`, a specialized response schema, and allowed worker next stages. Frame construction derives worker output behavior from membership in this closed stage set, never from the weaker predicate “a map contract is non-empty.”

The route guard evaluates an explicit matrix:

- owner role/stage plus `DomainOwnerContract`: allow the owner orchestration turn;
- owner role/stage plus a worker-stage contract or worker result schema: reject before provider invocation;
- specialist worker plus a matching worker-stage contract, owner lineage, Skill binding stage, and required planning or execution inputs: allow;
- specialist worker with a missing or mismatched contract: reject before provider invocation.

The guard that protects the planning operation remains separate from the guard that validates Frame construction. Allowing an owner Frame to run does not grant it route-design capability; it grants only the ability to select and create the next typed child.

The planner receives route-design facts from a frozen planning-context bundle, including exact cell coordinates, occupancy, boundaries, reachable frontiers, and other facts required by its contract. Entries may represent different targets, layers, revisions, roles, or regions and are validated independently. Write-only atlas resolution remains compiler/writer-owned unless explicitly declared as a non-authoritative route-design reference. This prevents context compression or a planner context from becoming the authority for write-critical data.

### 4. Keep macro state and domain state separate but linked

Persist `MacroPlanState` and `MapDomainWorkflowState` independently. The link is a typed tuple of macro step id, durable task id, domain task id, and owner Frame id. Map state records the current internal stage, child Frame contracts, attempt counters, a planning-context registry and bundle references, per-operation execution scopes, artifacts, approval, publications, blockers, and evidence through reducer events. Owner and child lifecycle events are keyed by workflow/task/lineage identities; target and revision are required only for facts that actually describe a concrete map scope.

The owner publishes a bounded `domain_owner_result_v1`. Macro predecessor binding consumes only declared fields or artifact references from that publication. It never reaches into the map reducer to reconstruct a writer or planner result.

This preserves domain encapsulation while still allowing, for example, a completed code-generation outcome to feed a map outcome or a completed map artifact to feed a later code step.

### 5. Commit stage facts before model continuation

Submitting a valid front-tool result remains atomic at the batch level, but the durable commit boundary ends after validation, reducers, artifacts, and stage publication succeed. Any subsequent agent/model continuation starts from a fresh working copy based on that committed checkpoint.

If the continuation times out, only its unfinished output is discarded. The accepted tool result, authoritative snapshot, candidate, validation, publication, approval state, and owner checkpoint remain recoverable. Idempotency continues to use the existing session epoch, turn identity, and canonical fingerprint.

This is preferred over holding the whole tool-result-plus-orchestration cycle in one rollback snapshot, which converts a presentation/model timeout into loss of already completed machine work.

### 6. Version and migrate the public plan contract without text inference

New plans use `plan_kind=macro_v2`. Existing `agent` and `task` fields can be accepted as aliases for `owner_agent` and `objective` during a compatibility window, but internal fields such as `worker_spec` are not accepted by `create_plan`.

Legacy persisted plans continue through their existing loader when safe. A legacy map plan containing multiple internal sibling stages is not collapsed based on keywords; it is paused with a typed migration outcome and regenerated as `macro_v2`. New coordinator instructions and schema examples produce one map owner step with display milestones.

### 7. Verify through the public route

Integration tests begin at chat/coordinator input, execute `create_plan` and `delegate_many`, run the resulting owner through at least its first orchestration turn, and observe creation of the typed planner child. Tests assert persisted contract kinds, multi-context bindings, local Skill-binding stage, filtered Skill visibility, result-schema assignment, provider-call counts, and provider-call identities so a legitimate owner is not rejected and an orchestrator cannot consume the planner budget. A test that calls only plan normalization, scheduler serialization, or the route-guard function is not public-route coverage. Existing direct typed-pipeline tests remain as lower-level coverage.

### 8. Keep routing recovery backend owned

`map_route_contract_violation` represents a backend construction or scheduling defect, not missing user intent. Its recovery disposition must remain machine actionable and owned by backend orchestration. It must not use `pause_for_user` or instruct the coordinator to create a planner child when dynamic map workers can only be created by the current map owner.

When no side effect occurred, recovery either discards the malformed child and resumes the correct owner checkpoint or returns the typed failure to the owner/scheduler so it can rebuild the child under the frozen contract. It never retries the same malformed Frame and never asks the user to repair internal routing.

### 9. Repair persisted malformed owner Frames explicitly

Hydration detects the exact legacy-invalid combination `role=map_orchestrator`, `map_stage=orchestrator`, and `result_schema=map_worker_result_v1` or a worker-stage payload. If task and owner lineage are intact and no worker side effect occurred, migration removes worker-only fields and reconstructs a `DomainOwnerContract`. Otherwise it records a backend-owned typed recovery problem and does not invoke a provider or perform a map write.

This targeted migration is preferred over treating every persisted map contract as corrupt or silently deleting the task.

### 10. Separate planning contexts from execution scopes

Introduce a `MapPlanningContextEntry` with a stable context id, semantic role, canonical target locator when one exists, layer/region coordinates, source revision, snapshot or artifact reference, digest, declared fact fields, and freshness status. A planner contract binds an ordered `MapPlanningContextBundle` containing the context ids and required roles. Upserting or refreshing one entry replaces only that entry and preserves unrelated backgrounds and reference contexts.

Planning contexts are read authority only. They may disagree in target, layer, revision, coverage, or semantic role without making the planner contract invalid. The deterministic compiler resolves a candidate into one or more `MapExecutionOperation` records; each operation carries exactly one canonical execution target, layer, expected revision, and write-critical atlas/item data. Approval, writer, and reviewer contracts refer to immutable operation or batch identities and validate every execution scope independently.

Canonical target locators are stored as data fields, never inferred from compound dictionary keys. Decorated internal keys such as `TileMap::map_layer=1` or `TileMap::revision=0` remain storage/index details and cannot satisfy a required `target_path` field.

This is preferred over choosing a single "primary" snapshot because a primary scope cannot represent a route whose gameplay, background, decoration, and reference facts live in different scopes. It is also preferred over copying atlas identities into planner prose because doing so widens write authority and makes context compression safety-critical.

### 11. Use child-local Skill stages and atomic child start

Define two explicit stage concepts: `task_stage`, the persisted coarse progress of the owner workflow, and `worker_binding_stage`, the immutable capability stage derived from the requested `MapWorkerStageContract`. Existing closed mapping from worker stages to runtime stages is the sole conversion authority. A planner child therefore binds Skills with `worker_binding_stage=plan` even when its owner's persisted `task_stage` is still `read`.

Child dispatch performs four phases: derive the intended worker binding/task stage; preflight the current task-stage transition without mutating state; validate role, mode, tools, permissions, planning or execution inputs, Skill bindings, prompt, and child contract; then commit a single reducer-owned child-start event that records both the task-stage transition and child lineage. Failure before commit leaves task stage, lineage, and provider-call count unchanged. The same mechanism covers reader, planner, writer, validator, repairer, and reviewer children rather than adding mode-specific transition branches.

Skill prompt summaries use the same binding resolver and effective permission context as `load_skill`. A Skill is advertised as directly loadable only when it resolves for the current Frame. A domain owner may receive a non-loadable routing hint such as "delegate to planner" but not a false claim that planner-only Skills are currently available. Runtime `load_skill` validation remains fail closed.

This is preferred over transitioning the global stage before prompt construction, which leaves dirty state when binding or Frame creation fails. It is also preferred over widening planner Skills to the `read` stage, which would hide the ownership error and grant planning capabilities in the wrong execution context.

## Risks / Trade-offs

- [Risk] A macro step objective can still be phrased too narrowly. → Mitigation: schema examples, coordinator rules, one-open-map-objective invariant, and public-route contract tests; do not use prose classification as an authority boundary.
- [Risk] Durable owners increase persistence and recovery complexity. → Mitigation: reuse reducer events, frozen Frame contracts, session epochs, and existing idempotent recovery identities.
- [Risk] Splitting commit boundaries exposes a committed intermediate state that older clients did not render. → Mitigation: publish explicit owner/stage status events and keep UI milestones display-only and resumable.
- [Risk] Cross-domain consumers may depend on private map details. → Mitigation: allow bindings only against `domain_owner_result_v1` publications and declared artifact locators.
- [Risk] Legacy multi-step map plans cannot be safely auto-collapsed. → Mitigation: fail with a typed migration disposition and regenerate; never infer execution authority from descriptions.
- [Risk] Contract types remain represented as untagged dictionaries and regress to truthiness checks. → Mitigation: add an explicit contract kind/version, closed constructors, exhaustive validation, and prohibit result-schema derivation from dictionary non-emptiness.
- [Risk] A route guard failure pauses a task that the backend could repair. → Mitigation: assign internal routing failures to backend recovery with side-effect-aware reconstruction and reserve user pauses for genuinely ambiguous effects or missing intent.
- [Risk] Multiple planning contexts can contain different revisions or overlapping facts. → Mitigation: identify and refresh entries independently, declare required semantic roles, retain provenance/digests, and resolve conflicts in deterministic compilation rather than requiring global equality.
- [Risk] Preflight and commit observe different task stages. → Mitigation: include the expected task-stage/checkpoint version in the atomic child-start event and retry from the new checkpoint without invoking a provider.
- [Risk] Filtering Skill summaries hides useful delegation knowledge from owners. → Mitigation: distinguish directly loadable Skills from typed delegation hints; never label an incompatible Skill as loadable.

## Migration Plan

1. Add versioned macro-plan and domain-owner result models behind a compatibility flag.
2. Add separate persisted domain workflow identity and reducer events, including owner publications.
3. Introduce distinct owner and worker-stage contract kinds, migrate malformed persisted owner Frames, and update map owner dispatch.
4. Add the independently keyed planning-context registry and migrate valid legacy singleton snapshots into one-entry bundles without converting decorated keys into targets.
5. Add deterministic per-operation execution scopes and keep approval/write/review validation scoped to their immutable batches.
6. Derive Skill binding from child contracts, filter prompt summaries through the binding resolver, and replace mode-specific stage mutation with atomic child-start events.
7. Enforce the owner/worker guard matrix and planner-child routing before provider calls, with backend-owned recovery dispositions.
8. Move valid tool-result/stage commits ahead of subsequent model continuation.
9. Update coordinator guidance and examples to emit one map owner step and display milestones.
10. Add executable multi-context public-route, Skill-binding, rollback, and recovery tests, then enable `macro_v2` by default.
11. Retain the legacy loader for existing sessions; pause and regenerate unsafe multi-sibling map plans.

Rollback disables new plan creation and owner dispatch while leaving versioned persisted records readable. Already committed map transactions and stage artifacts are not deleted or rewritten.

## Open Questions

- Whether the compatibility aliases `agent` and `task` should be removed in the next schema version or retained as serialization aliases.
- Whether UI clients need a distinct visual treatment for domain-owner status versus display milestones in the first rollout.
