## 1. Baseline and Shared Contracts

- [x] 1.1 Record the current failing/passing Python and Godot test baseline and map every existing failure to this change or to an unrelated known issue
- [x] 1.2 Add typed schemas for plan step status/result, Skill binding result, Map Workflow event, Frame contract violation, evidence reference, retry category, and map transaction journal
- [x] 1.3 Add schema-version migration readers for new Session workflow state and Skill metadata without enabling new execution paths
- [x] 1.4 Add feature flags for strict worker contract validation, Completion Gate enforcement, event-reducer writes, and grouped map transactions

## 2. Atomic Tool-Result Submission

- [x] 2.1 Extract a pure batch preflight validator for tool id, turn id, frame ownership, result status, pending metadata, and authorization
- [x] 2.2 Make preflight validate the complete batch before message, grant, cache, checkpoint, event, or persistence mutation
- [x] 2.3 Apply validated results to a deep Session working copy and atomically replace the active Session only after persistence succeeds
- [x] 2.4 Defer artifact/event publication until Session commit and attach request/turn idempotency keys
- [ ] 2.5 Add regression tests proving an invalid later result leaves earlier messages, grants, pending metadata, map state, cache, and disk unchanged
- [ ] 2.6 Add regression tests for frame/pending metadata mismatch, reducer failure rollback, persistence failure rollback, and idempotent retry
- [ ] 2.7 Introduce the versioned Session-level `map_artifacts.json` schema with `turns[turn_id].entries[tool_use_id]`, canonical fingerprints, and typed artifact locators
- [ ] 2.8 Replace per-call `_StagedArtifact` files with one transaction-local staged turn block that aggregates all large map-tool results in the submitted batch
- [ ] 2.9 Implement a map artifact reader that resolves committed and current-transaction staged entries by turn/tool-use id while keeping delegate-schema reads isolated
- [ ] 2.10 Atomically merge staged turns only after Session persistence, discard them on interrupt/rollback, reject conflicting turn fingerprints, and retain read-only compatibility for existing legacy per-call artifacts
- [ ] 2.11 Add regression coverage for multiple map results producing one persistent file, staged read-your-writes, successful commit, interrupt rollback, missing legacy files, and idempotent/conflicting retry

## 3. Dependency-Aware Plan Scheduler

- [x] 3.1 Introduce immutable plan step records with stable ids, `depends_on`, typed input bindings, expected result schema, and terminal status
- [x] 3.2 Preserve dependency edges from `create_plan` through serialization, restoration, history, and scheduler execution
- [x] 3.3 Implement deterministic runnable-step selection that unlocks a step only after all predecessors succeed
- [x] 3.4 Propagate failed/cancelled/blocked predecessor results to dependent steps without starting child Frames
- [x] 3.5 Bind predecessor typed results and artifact references into successor inputs and persist those bindings for resume
- [x] 3.6 Change `delegate_many` to execute only scheduler-unlocked steps and remove its independent dependency interpretation
- [x] 3.7 Require writer steps to consume planner/validator approved batch artifacts for the same target and revision
- [x] 3.8 Delete `_platform_validation_args` and replace service-side plan synthesis with a typed rejection that routes unapproved writes back to planning
- [ ] 3.9 Add DAG tests for parallel roots, chained success, diamond dependencies, predecessor failure, cancellation, resume, and typed input propagation

## 4. Skill Binding and Tool Reachability

- [x] 4.1 Implement `SkillBindingResolver` with `resolved`, `missing`, and `incompatible` results and structured reason codes
- [x] 4.2 Resolve effective tools from Agent Interface, stage/mode Capability Contract, permissions, and Skill required capabilities
- [x] 4.3 Update `SkillCatalog` and `load_skill` to return current-context binding results instead of global registry projections
- [x] 4.4 Reject dynamic Worker creation when a requested Skill is missing, disabled, role-incompatible, stage-incompatible, or mode-incompatible
- [x] 4.5 Migrate bundled map Skills from duplicated `allowed-tools` names to versioned semantic `required_capabilities`
- [x] 4.6 Remove dynamic Worker request `allowed_tools` and derive the callable set exclusively from the resolved binding
- [ ] 4.7 Add tests for global-extra-tool exclusion, disabled Skill, unknown Skill, mode mismatch, stage mismatch, permission restriction, and legacy metadata migration
- [ ] 4.8 Generate artifact-reading hints from artifact kind plus the current effective tool set and remove the hard-coded `read_file` hint from map result summaries
- [ ] 4.9 Route complete map-context, scene-tree, and exact-fact requests from map orchestrator to a compatible reader, and keep `search_tools` unable to activate scope-excluded tools
- [ ] 4.10 Add contract tests for map/delegate reader separation, scope-aware hints, reader routing, excluded-tool search, and mixed server/front tool batches without classifying server tools as missing

## 5. Event-Owned Map Workflow State

- [x] 5.1 Implement canonical `(target, revision)` scope keys for blockers, checkpoints, batches, validation, evidence, retry state, and progress
- [x] 5.2 Implement a pure `MapWorkflowReducer` that owns all legal stage and scoped-state transitions
- [x] 5.3 Route `agent.py` stage, blocker, checkpoint, batch, and no-progress writes through Map Workflow events
- [x] 5.4 Route `query/engine.py` and `query/helpers.py` map-result state changes through Map Workflow events
- [x] 5.5 Add a static/runtime guard that detects direct writes to reducer-owned MapTaskState fields
- [x] 5.6 Migrate legacy unscoped workflow state into canonical target/revision scopes and invalidate stale-revision gates
- [ ] 5.7 Add reducer tests for legal transitions, illegal transitions, target isolation, revision invalidation, event replay, and restart restoration

## 6. Worker Provenance and Completion Evidence

- [x] 6.1 Freeze contract id, worker instance id, stage, target, revision, result schema, and allowed next stages when each child Frame is created
- [x] 6.2 Implement a Frame result validator that rejects stage spoofing, wrong target, wrong revision, schema mismatch, worker mismatch, and illegal `next_stage`
- [x] 6.3 Give dynamic Workers reserved non-colliding instance identities and reject attempts to shadow permanent Agent definitions
- [x] 6.4 Simplify automatic child task payloads to objective plus typed input/artifact references, with role/schema/recovery rules supplied only by runtime contracts
- [x] 6.5 Implement an Evidence Registry that verifies screenshot tool_use_id ownership, success status, artifact readability, target, and revision
- [x] 6.6 Implement one Completion Gate Module that combines validation result, reviewer issues, scoped evidence, blockers, and workflow state
- [x] 6.7 Change validator and reviewer results to observations/issues/evidence only and stop consuming their legacy `completion_allowed` field
- [x] 6.8 Remove duplicate completion decisions from map-agent, validator, and reviewer prompts after strict gate enforcement is enabled
- [ ] 6.9 Add tests for stage spoof, stale revision, wrong target, illegal next stage, permanent-name collision, missing screenshot, failed screenshot, cross-Frame evidence, and valid completion

## 7. Grouped Map Undo Transactions

- [x] 7.1 Define transaction policy for standalone tools versus planner-approved write groups, including maximum tools, duration, and snapshot size
- [x] 7.2 Add stable `map_transaction_id` propagation from approved plan batch through ToolExecutor, revision updates, validation, and result events
- [x] 7.3 Extend UnifiedUndoManager to append map mutations, revision files, and related index writes to one open write-group action
- [x] 7.4 Commit an open write group only after its required validator succeeds for the same target and revision
- [x] 7.5 Abort and restore the complete write group on validation failure, cancellation, interruption, contract violation, or persistence error
- [x] 7.6 Persist a checksummed transaction journal with before snapshots and recover incomplete groups before accepting new map writes after restart
- [x] 7.7 Block automatic map writes and present recovery details when a journal is corrupt or incomplete
- [ ] 7.8 Add Godot integration tests for edit→validation failure→abort, successful commit, Ctrl+Z, Redo, plugin restart recovery, and revision/content consistency

## 8. Authoritative Platform Traversal Validation

- [x] 8.1 Remove public `_collision_cells` authority and read collision facts from the canonical editor target or a target/revision/digest-bound reader artifact
- [x] 8.2 Reject stale, cross-target, malformed, or unverifiable collision fact artifacts before platform validation
- [x] 8.3 Validate every segment's from/to ids, endpoint coordinates, direction, ordering, and agreement with platform geometry
- [x] 8.4 Sample leap trajectories using explicit actor footprint and check intermediate collision, headroom, landing width, and landing clearance
- [x] 8.5 Keep platform plans non-executable when movement or actor-size values come from defaults and return structured `missing_inputs`
- [x] 8.6 Unify planner preflight and final map validation on the same platform traversal Module and result schema
- [ ] 8.7 Add deterministic 2D tests for clear arcs, overhead obstruction, insufficient landing width, insufficient headroom, endpoint mismatch, stale facts, and explicit ability requirements

## 9. Structured Recovery and No-Progress Control

- [x] 9.1 Return structured original issue categories, safe diagnostics, applied repair actions, and attempt number from map structured-output repair
- [x] 9.2 Persist a semantic retry key using stage, target, revision, normalized operation signature, and error category
- [x] 9.3 Stop retrying after the configured same-category threshold and return a typed repair/retry-exhausted result
- [x] 9.4 Convert structured `missing_inputs` into a reader plan step and bind its typed result into the retried planner/validator step
- [x] 9.5 Block the original step when reader recovery cannot supply compatible facts instead of repeating the same call
- [x] 9.6 Aggregate no-progress counts by scoped error category and retain the earliest root cause
- [x] 9.7 Include first root cause, category counts, stage, target, revision, last attempt, and recovery guidance in pause results and history
- [ ] 9.8 Add tests for semantically equivalent retries, changed error categories, repair circuit breaking, reader recovery success/failure, and root-cause pause reporting
- [ ] 9.9 Preserve map NodePath semantics so omitted `target_path` performs documented compatible-map inference while `"."` remains the scene root and yields structured target recovery when it is not a map
- [ ] 9.10 Add recovery tests for omitted-target inference, invalid dot target candidates/hints, and suppression of identical no-progress target retries

## 10. Prompt and Duplicate-Contract Cleanup

- [x] 10.1 Split dynamic Worker prompt templates by mode and retain only task-specific guidance in each template
- [x] 10.2 Remove stage transitions, tool whitelists, result schema, resource rules, and recovery state machines from dynamic Worker prompt text
- [x] 10.3 Remove stage handoff, revision recovery, and completion-gate descriptions from `map-procedural-generation` and `map-area-expansion` Skills
- [x] 10.4 Remove duplicate pipeline-order and completion rules from map-agent/planner/validator/reviewer prompts after their runtime contracts are enforced
- [ ] 10.5 Add contract-parity tests proving Agent/Skill metadata cannot expand Capability Contract reachability
- [ ] 10.6 Add prompt snapshot checks that reject reintroduction of schema instructions, duplicated tool lists, and automatic child role rules

## 11. Migration, Integration, and Acceptance

- [x] 11.1 Run Session and Skill metadata migrations against representative legacy fixtures and verify one-time canonical rewrite
- [x] 11.2 Run Python compileall, focused mypy, OpenSpec validation, and the complete pytest suite with no new unexplained failures
- [x] 11.3 Run Godot headless script parsing and editor-plugin initialization after each GDScript transaction/platform slice
- [ ] 11.4 Run an end-to-end map flow covering reader→planner→writer→validator→reviewer with artifact inputs, evidence gate, and successful grouped commit
- [ ] 11.5 Run an end-to-end failure flow covering invalid later tool result, failed predecessor, stale worker result, failed validation rollback, retry exhaustion, and paused root-cause output
- [x] 11.6 Remove migration-only legacy fields and feature-flag observation paths after parity tests pass
- [x] 11.7 Update `未修复问题清单.md` and `整改审查报告.md` with implemented task ids, verification evidence, and any intentionally deferred limits
- [ ] 11.8 Run an end-to-end mixed server/front map-read flow proving one Session artifact file, staged same-turn reads, committed historical reads, reader delegation, and interrupt cleanup

## 12. Request-Scoped Map-Edit Gate

- [x] 12.1 Introduce a structured request intent that defaults to non-edit and becomes `map_edit` only when the current user request explicitly authorizes semantic map-content creation, modification, expansion, deletion, placement, painting, or repair
- [x] 12.2 Stop unconditionally calling `reset_map_task_progress` for every new user message; create or replace a map task only for an explicit `map_edit` request and bind its origin request id
- [x] 12.3 Propagate stable map-edit lineage through the root turn, plan steps, child Frames, pending front-tool metadata, tool-result continuations, and the final map completion candidate
- [x] 12.4 Replace the historical `task_id` Completion Gate condition with current-request `map_edit` lineage plus a completion-candidate marker; return ordinary, plan-only, read-only, validation-only, and `missing_inputs` responses unchanged
- [x] 12.5 Keep historical running/paused/completed map tasks dormant during unrelated requests, and allow lineage inheritance only for an explicit map-edit continuation or the dedicated `resume_map_task` command
- [ ] 12.6 Add regression coverage for greeting after stale task state, map read/analysis, plan-only requests, missing target/revision, unrelated new requests, bare "continue", explicit map-edit continuation, tool-result lineage, and a valid gated completion
- [x] 12.7 Reproduce the reported swallowed-chat case and rerun Python compileall, focused mypy, full pytest baseline, OpenSpec strict validation, and Godot headless plugin initialization
