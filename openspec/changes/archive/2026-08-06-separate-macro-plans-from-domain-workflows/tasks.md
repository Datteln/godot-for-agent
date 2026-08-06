## 1. Versioned Macro Contracts

- [x] 1.1 Add versioned `macro_v2` plan, macro-step, display-milestone, predecessor-binding, and domain-owner-result models with canonical validation and serialization tests.
- [x] 1.2 Update the public `create_plan` schema and normalization path to accept domain owner, objective, acceptance, dependencies, bindings, and display milestones while rejecting `worker_spec` and executable internal-stage fields.
- [x] 1.3 Add compatibility aliases for legacy `agent`/`task` fields and typed migration outcomes for unsafe legacy multi-sibling map plans without natural-language stage inference.
- [x] 1.4 Add macro-plan validation that rejects multiple sibling map-agent owners for one open durable map task and creates no partial Frames on failure.

## 2. Domain Owner Scheduling

- [x] 2.1 Extend durable scheduler state with domain task identity, owner Frame identity, owner publication status, and separately stored display milestones.
- [x] 2.2 Implement create-or-resume owner dispatch keyed by session epoch, durable task, domain, and domain task id.
- [x] 2.3 Change successor binding to consume declared `domain_owner_result_v1` fields or artifact locators and reject access to private internal child results.
- [x] 2.4 Keep macro steps non-terminal for `preview_ready` and `awaiting_confirmation`, and implement typed completion, blocking, cancellation, and permanent-failure transitions from owner publications.
- [x] 2.5 Add scheduler tests proving milestones never become PlanGraph nodes and internal child completion never completes a macro step.

## 3. Map Domain Ownership and Routing

- [x] 3.1 Persist one map owner identity and its macro-step/domain-task links when a map macro outcome first starts.
- [x] 3.2 Refactor map owner continuation to create or resume reader, planner, validator/publication, approval, writer, and reviewer work through its internal typed stage graph.
- [x] 3.3 Separate owner-Frame validation from the planner-operation guard so a valid owner contract reaches its first orchestration turn while planning still requires the current owner's matching planner child, Skill, and snapshot contract.
- [x] 3.4 Return typed `map_route_contract_violation` and zero provider calls only for actual owner/worker role-contract mismatches, sibling or stale planners, and generic nodes attempting route design; add a regression assertion that a valid owner is not rejected.
- [x] 3.5 Bind exact coordinates, occupancy, and other declared route-design facts from the authoritative snapshot to the planner, while keeping exact atlas write resolution in the deterministic compiler/writer contract.
- [x] 3.6 Connect the existing deterministic validator, three-attempt planner retry, and final publication rules to the owner workflow so exhausted planning publishes a typed result instead of continuing until timeout.
- [x] 3.7 Route valid approval or rejection to the persisted owner/checkpoint/candidate/revision and reject stale approval without creating a sibling map-agent or performing a write.

## 4. Reducer-Owned Workflow State

- [x] 4.1 Add reducer fields and lifecycle metadata for owner identity, macro link, child lineage, internal stage, owner publication, and approval identity.
- [x] 4.2 Add reducer events and transition validation for owner creation/resume, child start/result, candidate publication, approval, writer/reviewer progress, and owner result publication.
- [x] 4.3 Persist and hydrate macro-plan state and map-domain workflow state separately while validating their stable typed link.
- [x] 4.4 Extend reducer ownership/exhaustiveness checks to reject direct writes or missing reset/resume metadata for every new field.
- [x] 4.5 Add restart and reconnect tests proving an approval-waiting or retrying task restores the same owner, checkpoint, child lineage, snapshot revision, and attempt budget.

## 5. Durable Stage Commit Boundaries

- [x] 5.1 Split valid front-tool batch/stage persistence from subsequent agent/model continuation so the committed batch becomes active before the continuation starts.
- [x] 5.2 Start post-commit continuation from a fresh working copy linked to the committed checkpoint and preserve the checkpoint if continuation times out, is cancelled, or loses transport.
- [x] 5.3 Extend idempotency and recovery records so reconnect or retry resumes continuation without duplicating tool results, artifacts, reducer events, approvals, or owner publications.
- [x] 5.4 Add named failpoint tests before stage commit, after stage commit, and during continuation to prove rollback affects only the correct boundary and never leaves dangling artifact locators.

## 6. Coordinator and Client Integration

- [x] 6.1 Rewrite coordinator map-planning guidance and examples to emit one executable map-domain outcome with optional read/plan/preview/approval/write/verify display milestones.
- [x] 6.2 Update plan and progress events to distinguish executable macro-step status, domain-owner status, and non-executable milestone presentation.
- [x] 6.3 Update the Godot client plan view and idle/reconnect handling to render owner progress and resume a durable task without replaying the entire chat request.
- [x] 6.4 Add compatibility UI behavior for legacy plan records and typed plan-migration pauses.

## 7. Public-Route Verification and Rollout

- [x] 7.1 Add an executable integration test from `/chat` through coordinator, `create_plan`, `delegate_many`, map-owner first turn, and planner-child creation, asserting contract kinds, result schemas, Frame identities, and provider-call identities; normalization-only coverage is insufficient.
- [x] 7.2 Add public-route tests for mixed code/map dependencies, owner publication bindings, preview approval, write/review completion, and display-only milestones.
- [x] 7.3 Add regression tests for invalid planner routing, stale snapshot recovery, three failed deterministic validations, planner final publication, and bounded provider-call/token behavior.
- [x] 7.4 Add timeout/cancellation tests proving completed snapshot, candidate, validation, publication, and approval facts survive while unfinished chat/model output is discarded.
- [x] 7.5 Enable `macro_v2` behind a rollout flag, exercise legacy session migration and rollback, then make it the default after public-route and recovery suites pass.

## 8. Owner and Worker Contract Regression

- [x] 8.1 Add explicit versioned `DomainOwnerContract` and `MapWorkerStageContract` models or discriminators with closed constructors, serialization validation, and tests that reject fields belonging to the other contract kind.
- [x] 8.2 Update map child-contract construction and the Frame factory so only reader/planner/writer/validator/repairer/reviewer contracts assign worker instance identity, `map_worker_result_v1`, specialized schemas, and worker next stages; remove every truthiness-based worker classification.
- [x] 8.3 Add a route-guard matrix test using Frames produced by the real child factory, covering valid map owner, owner with planner contract, valid planner child, mismatched worker role/stage, stale lineage, and missing snapshot.
- [x] 8.4 Add deterministic hydration migration for persisted `map_orchestrator + orchestrator + map_worker_result_v1` Frames when lineage is intact and side-effect state is `none`, and a typed no-mutation backend block for ambiguous cases.
- [x] 8.5 Change `map_route_contract_violation` recovery from user-owned `pause_for_user` to a side-effect-aware backend correction path that resumes or reconstructs the recorded owner checkpoint and never asks the coordinator to create an owner-only child.
- [x] 8.6 Replay the captured `delegate_many -> f2 map-agent -> immediate route violation` scenario and verify that the owner now enters its first turn, creates the expected typed child, performs no route design itself, and emits no user-facing routing pause.

## 9. Multi-Context Planning and Scoped Execution

- [x] 9.1 Add typed `MapPlanningContextEntry` and `MapPlanningContextBundle` models with stable context identity, semantic role, provenance/digest, optional canonical target, layer/region, source revision, declared fact fields, and freshness validation.
- [x] 9.2 Replace planner contracts that require one authoritative snapshot/target/revision with ordered required context references, and reject only missing, stale, or provenance-mismatched required entries rather than cross-entry scope differences.
- [x] 9.3 Extend reducer state and events with an independently keyed planning-context registry and bundle references; prove that refreshing one gameplay or background entry preserves every unrelated context.
- [x] 9.4 Migrate valid legacy singleton snapshots into one-entry context bundles and keep decorated keys such as `TileMap::map_layer=1` as index details instead of accepting them as canonical `target_path` values.
- [x] 9.5 Make deterministic compilation emit immutable `MapExecutionOperation` records with one canonical target, layer, expected revision, and write-critical cell/atlas data per operation; bind approval, writer, and reviewer contracts to operation or batch identities.
- [x] 9.6 Add unit tests for Mid plus multiple Background contexts, independently stale revisions, targeted refresh, overlapping reference regions, decorated-key rejection, and multi-operation revision guards.

## 10. Child-Local Skill Binding and Atomic Dispatch

- [x] 10.1 Introduce explicit `task_stage` and `worker_binding_stage` concepts and derive the latter only from the closed worker-stage contract using the canonical worker-to-runtime-stage mapping.
- [x] 10.2 Change child prompt and Skill binding to use the requested child's binding stage, role, mode, effective tools, and permissions instead of `session.map_task_state.stage`; cover reader, planner, writer, validator, repairer, and reviewer uniformly.
- [x] 10.3 Add a side-effect-free task-stage transition preflight and one reducer-owned child-start event that atomically validates the expected checkpoint, transitions task stage, and records child lineage after contract, Skill, prompt, and Frame construction succeeds.
- [x] 10.4 Remove mode-specific pre-prompt or post-prompt global stage mutation paths and add failure tests proving rejected Skill binding, prompt construction, Frame validation, or stale checkpoint leaves stage, lineage, context state, and provider-call count unchanged.
- [x] 10.5 Filter system-prompt Skill summaries through the same binding resolver and permission context as `load_skill`; distinguish directly loadable Skills from owner delegation hints and keep runtime binding fail closed.
- [x] 10.6 Add regressions for coordinator `role_incompatible`, map-owner planner-Skill incompatibility, `no_effective_tools`, legitimate `read -> planner(plan)` delegation, and writer/reviewer child-stage binding without broadening Skill frontmatter stages.

## 11. Public-Route Recovery and Rollout Delta

- [x] 11.1 Replay the captured production request through `/chat -> create_plan -> delegate_many -> map owner -> planner child` with Mid and Background planning contexts and assert the first planner provider call uses child-local `plan` Skill binding.
- [x] 11.2 Assert coordinator and map-owner prompts do not advertise planner-only or tool-incompatible Skills as directly loadable, while the planner prompt advertises and successfully loads its compatible planning Skills.
- [x] 11.3 Add public-route failpoint coverage before and during child-start commit to prove no dirty task-stage transition, orphan lineage, duplicate context entry, or provider call survives failure or checkpoint races.
- [x] 11.4 Add approval, restart, reconnect, and reviewer coverage for one candidate compiled into several independently scoped operations, preserving the same owner/workflow lineage without requiring one task-level target.
- [x] 11.5 Update legacy fixtures that inject top-level target JSON or a contract-free owner so they exercise natural-language macro tasks, runtime-bound context bundles, real Frame construction, and reducer-owned child-start events.
