## 1. Baseline, Workflow Lifecycle, and Hydration

- [x] 1.1 Reproduce and classify the current 177-pass/9-fail service-test baseline, updating only assertions made stale by the earlier map-agent refactor
- [x] 1.2 Add exhaustive machine-readable lifecycle metadata for every `MapTaskState` field, including task/revision/session scope, reset/default factory, and resume policy, and define the `task_epoch_started` event payload from that metadata
- [x] 1.3 Implement one closed hydration path that migrates raw persisted dictionaries, validates and normalizes the complete value, constructs `MapTaskState` once, and rejects already-live state inputs or post-construction hydration writes
- [x] 1.4 Implement `task_epoch_started` so reducer reset behavior is derived from lifecycle metadata rather than a second hand-maintained field enumeration
- [x] 1.5 Route new-task creation through `task_epoch_started` while preserving only metadata-declared checkpoint state when the same task is explicitly resumed
- [x] 1.6 Replace `dedicated_resume_authorized` persistence-window behavior with a reducer-owned one-shot authorization that the next request atomically captures and clears before fallible processing
- [x] 1.7 Replace direct Agent and QueryEngine mutations of reducer-owned scalars and nested containers with typed workflow events
- [x] 1.8 Activate and extend the direct-state-write repository check with an exact allowlist for the pre-construction hydration function, not module-wide migration or deserialization exceptions
- [x] 1.9 Add exhaustive classification, schema-migration, round-trip, distinct-task reset, same-task resume, restart-before-consumption, early-return, missing-Frame, and exception-window tests that prove resume authorization cannot leak to a later ordinary message

## 2. Deterministic Failure-Injection Infrastructure

- [x] 2.1 Inventory and name every durable Godot transaction and Python coordinated-publication boundary required by the recovery specifications, including prepare, write/flush, rename, apply, commit, resource publication, cleanup, and process exit
- [x] 2.2 Introduce a narrow Godot journal/filesystem adapter with the existing engine APIs as the production implementation and deterministic test-only failpoints for journal writes, snapshot operations, commit boundaries, and deletion
- [x] 2.3 Build versioned journal/snapshot fixture helpers and a separate-process Godot headless restart driver that can terminate at a named boundary and resume from the exact durable state
- [x] 2.4 Add equivalent test-only named failpoints to the Python Session/artifact coordinated-commit storage boundary without changing the existing production turn-identity algorithm
- [x] 2.5 Add guard tests proving production composition disables failpoints and no map tool or submission payload can enable, select, or configure them

## 3. Godot Transaction Recovery and Revision History

- [x] 3.1 Introduce versioned checksummed persisted journal states `prepared`, `applying`, `committing`, `committed`, and `rolled_back` with before/after fingerprints and revision metadata; represent logical `cleaned` only by absence of the matching terminal journal
- [x] 3.2 Persist `committed` after the Undo action commits and `rolled_back` after before-state restoration, allow deletion only after either terminal marker is durable, and leave the terminal marker intact for retry when cleanup fails
- [x] 3.3 Implement startup recovery rules for provably uncommitted, committed, rolled-back, ambiguous, corrupt, oversized, and legacy journals
- [x] 3.4 Start recovery eagerly at editor startup, make `ensure_recovered()` single-flight for all callers, expose progress/blocking transaction diagnostics, and enforce configured journal, snapshot, and operation bounds
- [x] 3.5 Benchmark the maximum supported recovery fixture against a configured startup/frame-latency policy and chunk or yield recovery work as needed while keeping all map mutation blocked
- [x] 3.6 Add the recovery barrier to every first map-mutation path, then reload authoritative revision metadata and expose the mutation-boundary expected-revision CAS before journal creation, Undo batch start, or content mutation
- [x] 3.7 Update Undo and Redo callbacks to reload authoritative revision files and recapture content fingerprints under internal-change suppression, routing keyboard and programmatic history through the same synchronization path
- [x] 3.8 Using the section 2 harness, add Godot headless tests for cleanup failure after both terminal states, restart in every journal state, single-flight recovery, configured bounds/latency/progress, recovery-before-revision-check ordering, stale writes rejected before mutation, Ctrl+Z/Redo, programmatic history, and one-time external revision bumps

## 4. Scoped Validation and Completion Gate

- [x] 4.1 Add typed boundary normalization for validator and reviewer payloads, including null and malformed `issues` and `structured_issues`
- [x] 4.2 Implement blocker upsert/remove operations keyed by task, target, revision, source, and stable issue identity
- [x] 4.3 Update validator/reviewer failure handling so it changes only its own scoped blocker and preserves unrelated blockers
- [x] 4.4 Require an exact non-null target and revision match for validation, evidence, and Completion Gate satisfaction
- [x] 4.5 Convert missing/malformed validation scope into typed fail-closed outcomes instead of exceptions or wildcard matches
- [x] 4.6 Implement total Completion Gate lifecycle handling: running allowed transitions once, completed replay is idempotent, paused remains blocked, and idle/cancelled invalidate stale completion candidates
- [x] 4.7 Add Gate and reducer tests for null issue lists, malformed lists, stale revisions, missing revisions, multiple simultaneous targets, every workflow status, candidate invalidation, and duplicate completion effects

## 5. Platform Approval and Commit Coupling

- [x] 5.1 Define immutable platform approval records containing approval id, target, expected revision, and canonical batch fingerprint
- [x] 5.2 Change platform-validation success to retain the approved batch without popping it or advancing workflow revision
- [x] 5.3 Include approval identity and committed revision in the durable Godot write-transaction result
- [x] 5.4 Consume an approval and advance workflow state only from the matching successful committed-result reducer event
- [x] 5.5 After the section 3 recovery barrier, compare approval/request expected revisions with the authoritative Godot revision immediately before mutation and reject conflicts before journal creation, Undo batch start, map mutation, or approval consumption
- [x] 5.6 Reduce trusted revision-conflict results into `latest_revisions`, invalidate stale approvals, and require fresh target reads and validation before retry
- [x] 5.7 Preserve current approvals after rejection, cancellation, persistence failure, or rollback only while their expected authoritative revision remains current
- [x] 5.8 Using the section 2 harness, add idempotency and failure-injection tests proving retry cannot double-consume a batch or double-increment revision, including Godot commit at `N+1` followed by service exit before the committed-result reducer event

## 6. Coordinated Session and Artifact Publication

- [x] 6.1 Define a versioned coordinated-commit record containing Session, turn, entry, fingerprint, old/new document digests, temporary paths, and lifecycle state
- [x] 6.2 Prepare Session and artifact documents without mutating active Session state and retain transaction-local staged-artifact reads
- [x] 6.3 Publish the artifact document and Session locator through the documented artifact-first coordinated sequence and mark the commit durable before cleanup
- [x] 6.4 Make locator resolution verify Session, turn, entry, and canonical fingerprint while hiding unreferenced prepared artifact blocks
- [x] 6.5 Implement startup reconciliation for exits before publication, between resource publications, after Session publication, and during cleanup
- [x] 6.6 Verify and harden the existing completed-turn id plus canonical-fingerprint idempotency path so prepared coordinated commits resume through it, identical completed retries still return cached results, and conflicting fingerprints remain rejected without reimplementing the identity algorithm
- [x] 6.7 Using the section 2 failpoints, test every coordinated-commit boundary and assert no observable Session contains a dangling artifact locator or duplicate message, grant, artifact, or workflow mutation
- [x] 6.8 Make the session turn counter monotonic across rollbacks and restarts: persist `max(persisted, in-memory)` on save (mirroring the history event counter) and ensure a failed request's snapshot rollback never lowers the persisted counter, so a committed turn id can never be reallocated
- [x] 6.9 Convert a staged-vs-committed turn-identity conflict (same turn id, different canonical fingerprint) into a typed integrity failure that preserves the committed turn and allows retry under a fresh strictly-greater turn id, without raising an uncaught exception or wedging the session, and without reimplementing the existing identity algorithm
- [x] 6.10 Add tests proving turn-counter monotonicity after rollback-then-save and after restart, and proving a conflicting submission returns the typed integrity failure, retries under a new turn id, and never exposes a dangling locator or duplicate message

## 7. Typed Plan Failures and Retry Circuit Breaker

- [x] 7.1 Move dependency-result lookup, `task_payload()` binding, worker-contract creation, and stage transition into one scheduler error boundary
- [x] 7.2 Return stable typed blocked results for failed predecessors and missing/malformed dependency binding paths without creating child Frames
- [x] 7.3 Convert illegal worker-stage transitions and payload-contract failures into `blocked` or `replan_required` outcomes without partial workflow changes
- [x] 7.4 Remove the legacy delegate-group `remaining` queue and consumer, derive every next child from scheduler graph state, and migrate legacy persisted groups to one graph or a typed blocked outcome
- [x] 7.5 Add exact semantic plan-attempt keys from task, stage, target, revision, operation, and root error code for idempotence and same-state retry accounting
- [x] 7.6 Add a reducer-owned task-convergence key and configurable count scoped by stable task lineage, target, operation, and root-error family, excluding sub-step revision
- [x] 7.7 Preserve existing running and terminal plan outcomes and trip the exact-attempt circuit breaker for unchanged repeated `create_plan` attempts
- [x] 7.8 Permit a new exact attempt after authoritative input, revision, predecessor progress, or root-error semantics change without treating revision advancement or partial predecessor success alone as a task-convergence reset
- [x] 7.9 Preserve task-convergence state across restart and same-task resume; reset it only for a distinct `task_epoch_started` lineage or an explicit convergence checkpoint/terminal outcome
- [x] 7.10 Add scheduler tests for binding failures, invalid review-to-write transitions, legacy `remaining` migration/removal, graph-only child selection, repeated identical plans, `N -> N+1 -> N+2` partial-success thrash, legitimate multi-revision convergence, reset semantics, retained diagnostics, and absence of HTTP 500 responses

## 8. Boundary Hardening and End-to-End Verification

- [x] 8.1 Enforce string and per-element type validation for ordinary project paths and list-valued path arguments before normalization
- [x] 8.2 Keep `user://` support restricted to screenshot/image-review contracts and reject traversal, OS-absolute paths, unknown schemes, non-string values, and malformed `user:/`, `user:`, `res:/`, and `res:` spellings before project-relative fallback, for both scalar and list-valued arguments
- [x] 8.3 Remove or integrate unreachable scope-patch and unused direct-write/single-tool helper paths so they cannot bypass reducer or transaction contracts
- [x] 8.4 Run the complete Python service suite and reach a clean baseline with the new workflow, artifact, scheduler, and deterministic fault-injection tests
- [x] 8.5 Run Godot headless transaction/revision suites and an end-to-end map task covering validate, approve, commit, completion, Undo, Redo, restart, and retry
- [x] 8.6 Verify compatibility with `stream-chat-events-during-atomic-submissions`: heartbeats remain non-mutating and buffered business events publish only after the coordinated commit
- [x] 8.7 Update map-agent operational/recovery documentation with typed error codes, journal diagnostics, configured recovery bounds/progress, manual ambiguous-state recovery, and rollback steps
