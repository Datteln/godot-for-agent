## 1. Baseline and Workflow State Ownership

- [ ] 1.1 Reproduce and classify the current 177-pass/9-fail service-test baseline, updating only assertions made stale by the earlier map-agent refactor
- [ ] 1.2 Document every MapTaskState field as task-scoped, revision-scoped, or session-scoped and define the `task_epoch_started` event payload
- [ ] 1.3 Implement the reducer branch that atomically resets all task-scoped counters, blockers, validations, evidence, scopes, revisions, reads, batches, retries, transaction references, and context
- [ ] 1.4 Route new-task creation through `task_epoch_started` while preserving checkpoint state when the same task is explicitly resumed
- [ ] 1.5 Replace direct Agent and QueryEngine mutations of reducer-owned scalars and nested containers with typed workflow events
- [ ] 1.6 Activate and extend the direct-state-write repository check, with a narrow audited allowlist for hydration and migration boundaries
- [ ] 1.7 Add tests proving exhausted `auto_iterations` and all other task-owned state do not leak into a distinct task but survive same-task resume

## 2. Scoped Validation and Completion Gate

- [ ] 2.1 Add typed boundary normalization for validator and reviewer payloads, including null and malformed `issues` and `structured_issues`
- [ ] 2.2 Implement blocker upsert/remove operations keyed by task, target, revision, source, and stable issue identity
- [ ] 2.3 Update validator/reviewer failure handling so it changes only its own scoped blocker and preserves unrelated blockers
- [ ] 2.4 Require an exact non-null target and revision match for validation, evidence, and Completion Gate satisfaction
- [ ] 2.5 Convert missing/malformed validation scope into typed fail-closed outcomes instead of exceptions or wildcard matches
- [ ] 2.6 Add Gate and reducer tests for null issue lists, malformed lists, stale revisions, missing revisions, and multiple simultaneous targets

## 3. Platform Approval and Commit Coupling

- [ ] 3.1 Define immutable platform approval records containing approval id, target, expected revision, and canonical batch fingerprint
- [ ] 3.2 Change platform-validation success to retain the approved batch without popping it or advancing workflow revision
- [ ] 3.3 Include approval identity and committed revision in the durable Godot write-transaction result
- [ ] 3.4 Consume an approval and advance workflow state only from the matching successful committed-result reducer event
- [ ] 3.5 Preserve current approvals after rejection, cancellation, persistence failure, or rollback, and invalidate them after authoritative revision drift
- [ ] 3.6 Add idempotency and failure-injection tests proving retry cannot double-consume a batch or double-increment revision

## 4. Godot Transaction Recovery and Revision History

- [ ] 4.1 Introduce the versioned checksummed journal states `prepared`, `applying`, `committing`, `committed`, and `rolled_back` with before/after fingerprints and revision metadata
- [ ] 4.2 Persist `committed` after the Undo action commits and before journal cleanup, and make cleanup retry-safe
- [ ] 4.3 Implement startup recovery rules for provably uncommitted, committed, ambiguous, corrupt, and legacy journals
- [ ] 4.4 Add a synchronous `ensure_recovered()` barrier to every first map-mutation path before authorization or approval consumption
- [ ] 4.5 Update Undo and Redo callbacks to reload authoritative revision files and recapture content fingerprints under internal-change suppression
- [ ] 4.6 Route keyboard and programmatic history operations through the same tracker synchronization path
- [ ] 4.7 Add Godot headless tests for cleanup failure after commit, restart in every journal state, first-write recovery races, Ctrl+Z/Redo, programmatic history, and one-time external revision bumps

## 5. Coordinated Session and Artifact Publication

- [ ] 5.1 Define a versioned coordinated-commit record containing Session, turn, entry, fingerprint, old/new document digests, temporary paths, and lifecycle state
- [ ] 5.2 Prepare Session and artifact documents without mutating active Session state and retain transaction-local staged-artifact reads
- [ ] 5.3 Publish the artifact document and Session locator through the documented artifact-first coordinated sequence and mark the commit durable before cleanup
- [ ] 5.4 Make locator resolution verify Session, turn, entry, and canonical fingerprint while hiding unreferenced prepared artifact blocks
- [ ] 5.5 Implement startup reconciliation for exits before publication, between resource publications, after Session publication, and during cleanup
- [ ] 5.6 Resume identical retries by turn/fingerprint and reject conflicting fingerprints without duplicate messages, grants, artifacts, or workflow mutations
- [ ] 5.7 Add fault-injection tests at every coordinated-commit boundary and assert that no observable Session contains a dangling artifact locator

## 6. Typed Plan Failures and Retry Circuit Breaker

- [ ] 6.1 Move dependency-result lookup, `task_payload()` binding, worker-contract creation, and stage transition into one scheduler error boundary
- [ ] 6.2 Return stable typed blocked results for failed predecessors and missing/malformed dependency binding paths without creating child Frames
- [ ] 6.3 Convert illegal worker-stage transitions and payload-contract failures into `blocked` or `replan_required` outcomes without partial workflow changes
- [ ] 6.4 Add semantic plan-attempt keys from task, stage, target, revision, operation, and root error code
- [ ] 6.5 Preserve existing running and terminal plan outcomes and trip a configurable circuit breaker for unchanged repeated `create_plan` attempts
- [ ] 6.6 Permit a new attempt only after authoritative input, revision, predecessor progress, or root-error semantics change, retaining prior outcomes for diagnosis
- [ ] 6.7 Add scheduler tests for binding failures, invalid review-to-write transitions, repeated identical plans, progress resets, and absence of HTTP 500 responses

## 7. Boundary Hardening and End-to-End Verification

- [ ] 7.1 Enforce string and per-element type validation for ordinary project paths and list-valued path arguments before normalization
- [ ] 7.2 Keep `user://` support restricted to screenshot/image-review contracts and reject traversal, OS-absolute paths, unknown schemes, and non-string values with typed results
- [ ] 7.3 Remove or integrate unreachable scope-patch and unused direct-write/single-tool helper paths so they cannot bypass reducer or transaction contracts
- [ ] 7.4 Run the complete Python service suite and reach a clean baseline with the new workflow, artifact, scheduler, and fault-injection tests
- [ ] 7.5 Run Godot headless transaction/revision suites and an end-to-end map task covering validate, approve, commit, completion, Undo, Redo, restart, and retry
- [ ] 7.6 Verify compatibility with `stream-chat-events-during-atomic-submissions`: heartbeats remain non-mutating and buffered business events publish only after the coordinated commit
- [ ] 7.7 Update map-agent operational/recovery documentation with typed error codes, journal diagnostics, manual ambiguous-state recovery, and rollback steps
