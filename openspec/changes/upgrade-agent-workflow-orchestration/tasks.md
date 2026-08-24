## 1. Plan graph contracts and persistence

- [ ] 1.1 Define typed PlanGraph, plan-step, owner-contract, artifact, publication, lifecycle-status, and dependency-binding contracts.
- [ ] 1.2 Add PlanGraph validation for registered owners, unique stable IDs, non-empty objectives, dependency existence, self-dependencies, cycles, and binding declarations.
- [ ] 1.3 Persist and restore graph-managed plan state independently from execution-local Frames, with safe validation of malformed legacy session data.
- [ ] 1.4 Add contract and persistence tests for valid graphs, malformed graphs, cycles, and restored graph state.

## 2. Deterministic graph scheduling

- [ ] 2.1 Implement scheduler selection of one dependency-ready step from persisted graph state, replacing model-selected sequencing only for graph-managed plans.
- [ ] 2.2 Resolve declared predecessor artifact bindings before owner start and produce typed blocked outcomes for missing or invalid bindings.
- [ ] 2.3 Implement step lifecycle transitions, terminal failure propagation, and one truthful terminal plan outcome.
- [ ] 2.4 Preserve direct coordinator, single-delegate, and legacy `delegate_many` paths for requests that do not use graph scheduling.
- [ ] 2.5 Add scheduler tests for ordered dependencies, independent precursors, failure blocking, invalid bindings, and direct-path regression.

## 3. Domain-owner execution boundary

- [ ] 3.1 Create owner execution contexts that bind one registered specialist Agent to one graph step without granting graph-edit or unrestricted delegation authority.
- [ ] 3.2 Implement bounded structured owner publications with identity validation, declared artifacts, diagnostics, summary, and disposition.
- [ ] 3.3 Integrate existing front-tool confirmation so an awaiting mutation suspends and resumes the same owner step, while dependent steps remain blocked.
- [ ] 3.4 Enforce serial scheduling for all graph-managed work in the first release and explicitly guard against concurrent write-capable owners.
- [ ] 3.5 Add integration tests for owner ownership, publication validation, approval/resume, rejection blocking, and write serialization.

## 4. Transcript progress and migration

- [ ] 4.1 Map PlanGraph and owner lifecycle events to existing authoritative transcript plan, progress, status, approval, error, and final-result entries.
- [ ] 4.2 Ensure WebSocket replay/history hydration renders graph progress exclusively through revision-aware transcript patches.
- [ ] 4.3 Add front-end projection/rendering coverage for graph plan creation, step transitions, blocked dependencies, confirmation waiting, and terminal outcome.
- [ ] 4.4 Add an opt-in migration path from `create_plan` to graph scheduling, with legacy fallback and a configuration or routing gate for rollout.
- [ ] 4.5 Run focused backend and Godot plugin regression suites; document the rollout gate and rollback behavior.
