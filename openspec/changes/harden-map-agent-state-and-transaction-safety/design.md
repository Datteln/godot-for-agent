## Context

The map-agent runtime already has the intended architectural pieces—dependency-aware plans, a workflow reducer, a Completion Gate, grouped map transactions, Session working copies, and a consolidated `map_artifacts.json`. The review found that several boundary conditions still bypass those pieces:

- task-owned state can survive into a later map task or be mutated outside the reducer;
- validation results with missing revisions or nullable issue arrays can bypass or crash the Completion Gate;
- platform approvals are consumed before their write is durably committed, and a service crash after a Godot commit can leave the persisted service revision behind the authoritative Godot revision;
- the Godot journal cannot distinguish an uncommitted edit from a committed edit whose journal cleanup failed;
- Undo/Redo restores revision metadata, then the external-change scanner treats the restored content as a new edit and increments it again;
- Session persistence and artifact publication can expose a locator before its artifact is durable;
- scheduler binding and worker-stage errors escape as exceptions, while repeated `create_plan` calls can overwrite useful terminal state or keep advancing revisions without converging;
- deferred recovery and permissive path argument handling leave first-write and file-boundary races.

The change crosses the Python orchestration service, Godot editor integration, and both sides' persistence formats. It must remain compatible with existing Session documents, `map_artifacts.json`, and HTTP contracts. A separate active change owns streaming liveness during atomic submissions; this design does not redefine that capability.

## Goals / Non-Goals

**Goals:**

- Make every map task start from a complete, reducer-owned task epoch while retaining only explicitly session-scoped state.
- Make validation, evidence, blockers, and completion decisions exact for `(target, revision)` and safe for nullable external payloads.
- Couple approval consumption and revision advancement to successful durable map commits, and reject stale approvals against the authoritative Godot revision before mutation.
- Make transaction recovery distinguish prepared, ambiguous, committed, and rolled-back edits without guessing.
- Keep Undo/Redo content, revision metadata, and the external-change fingerprint synchronized.
- Coordinate Session locator and map-artifact publication so readers never observe a dangling locator.
- Convert orchestration boundary failures into typed plan outcomes and bound both identical plan retries and cross-revision non-convergence.
- Gate the first write on recovery and enforce typed, scheme-aware path boundaries.

**Non-Goals:**

- Redesigning map generation, platform geometry algorithms, worker prompts, or the public chat API.
- Replacing Godot's UndoRedo system or introducing a general distributed transaction coordinator.
- Changing heartbeat or token-stream semantics owned by `stream-chat-events-during-atomic-submissions`.
- Recovering semantic aliases that cannot be derived from canonical editor facts.
- Automatically resolving an ambiguous journal state by choosing either rollback or commit.

## Decisions

### 1. Model a task epoch as one reducer event

A `task_epoch_started` event will atomically initialize all task-scoped state. The event carries the stable task/lineage identity and canonical target where known. Its reducer branch resets counters, blockers, validations, evidence, scopes, observed revisions, layer/region reads, pending batches, retry state, transaction references, and contextual task data. Session-global configuration and completed historical task summaries remain outside the epoch.

Every state field will carry machine-readable lifecycle metadata next to the state schema: scope (`task`, `revision`, or `session`), reset/default factory, and resume policy. `task_epoch_started` derives its reset behavior from that metadata. An exhaustive test fails when a field is unclassified or when reset/resume behavior diverges from its declared lifecycle, so adding a field cannot silently omit it from epoch initialization.

Persisted state hydrates through one closed construction boundary: migrate the raw persisted dictionary, validate and normalize the entire value, construct `MapTaskState` once, then publish/seal it as live reducer-owned state. The audited allowlist identifies that exact constructor boundary rather than whole modules. It does not accept an already-live `MapTaskState`, and no post-construction mutation is permitted there. Any migration that must change live state emits a reducer event.

The dedicated resume command creates a reducer-owned, one-shot authorization bound to the paused task and lineage. The next user request atomically captures and clears that authorization before any fallible classification, Frame lookup, prompt construction, or early return. Only the captured request may inherit the existing task scope; failure or rejection cannot leave authorization for a later ordinary message. Contextual natural-language resume remains separately constrained by unique task focus and lineage.

This is preferred over scattered resets in Agent and QueryEngine because a single event gives restart/resume code the same initialization semantics and makes omissions testable.

### 2. Normalize validation at ingestion and require exact scope identity

Validator/reviewer payloads will be parsed into typed internal values before entering the reducer or Completion Gate. Missing or null issue collections normalize to empty lists; malformed collections produce a typed validation failure rather than an exception. A validation can satisfy a gate only when both canonical target and concrete revision exactly match. A missing revision is never a wildcard.

Blockers will be updated with scoped upsert/remove operations keyed by task, target, revision, source, and stable issue identity. A validator transport failure adds or replaces only its own scoped blocker and cannot erase blockers for other targets or revisions.

Completion lifecycle behavior is total over workflow status. `running + allowed` transitions once to `completed`; an identical replay of an already `completed` outcome is idempotent. `paused` remains blocked. `cancelled` and `idle` invalidate any completion-candidate marker and cannot be reported as successful task completion. Cancellation, task replacement, and a new epoch clear the prior candidate identity.

This is preferred over defensive `list(value)` calls at each consumer because one boundary normalizer keeps all downstream logic total and consistent.

### 3. Consume an approved batch from the committed result

Validation creates an immutable approval record containing approval id, target, expected revision, and canonical batch fingerprint. Preflight verifies that record but does not remove it or advance revision. Every write follows the ordered boundary `ensure_recovered -> read authoritative Godot revision -> compare approval/request revision and batch fingerprint -> begin transaction -> commit -> reducer event`. The authoritative comparison is a CAS at the mutation boundary, not a comparison against only the service's persisted `latest_revisions`.

If the authoritative revision differs, Godot returns a typed conflict containing the actual revision before creating a journal, opening a batch, mutating content, or consuming approval state. The service reducer records that authoritative revision and invalidates the stale approval before any retry. Rejected, cancelled, failed, or rolled-back writes retain the approval only while its expected revision remains current. The Godot write transaction returns the approval id and committed revision in its durable result; only the reducer event for that successful committed result consumes the matching approval and advances workflow state. Replayed committed results are idempotent by transaction/approval identity.

This ordering also covers a service crash after Godot durably commits revision `N+1` but before the service applies the committed-result event: a retry carrying `N` is rejected before mutation, then reconciles the service to `N+1`. Eager startup synchronization may avoid that failed round trip but is not the safety boundary. This is preferred over a temporary “claimed” pop because a claim introduces another recovery state and can still lose the batch when the process exits before write execution.

### 4. Use an explicit durable map transaction journal state machine

The Godot journal will use the versioned, checksummed persisted states:

`prepared -> applying -> committing -> committed`

Rollback records the persisted terminal state `rolled_back` before cleanup. `cleaned` is a logical lifecycle outcome represented only by the absence of the matching terminal journal; it is never serialized as a journal state. Cleanup may delete only a durably persisted `committed` or `rolled_back` journal. If deletion fails, that terminal marker remains and recovery retries deletion without reapplying or rolling back the transaction. Snapshots and affected revision metadata are durable before `applying`. The journal records the transaction id, approval id where applicable, before/after fingerprints, and expected revisions.

- `prepared` or `applying` is recoverably uncommitted and rolls back from the before snapshot.
- `committed` is never rolled back; restart only verifies the after fingerprint and retries cleanup.
- `committing` is ambiguous. Automatic writes remain blocked until deterministic evidence proves the before or after state, or the user follows explicit recovery instructions.
- checksum, schema, or snapshot failures fail closed.

The committed marker is persisted after the Undo action commits and before journal deletion. Recovery starts eagerly at editor startup and is single-flight: every first-write path awaits the same operation rather than launching a duplicate scan. Mutation remains blocked until recovery finishes cleanly or reports a typed block. Recovery enforces configured journal/snapshot/operation-size limits, reports progress and the current blocking transaction, and rejects oversized or corrupt inputs without partially applying them. The maximum supported recovery fixture is benchmarked against the configured startup/frame-latency policy; implementation may chunk or yield parsing/restoration work while preserving the mutation barrier. Because recovery can restore or confirm revision metadata, the mutation path reloads the authoritative revision after the barrier and performs the approval/request CAS before starting a new journal or Undo batch.

This is preferred over inferring commitment from journal existence because cleanup failure is not evidence that the edit failed.

### 5. Treat Undo/Redo as authoritative history restoration, not external drift

UndoRedo operations restore map content and revision files in the same action. Their completion callback tells `MapRevisionTracker` to reload authoritative revisions and recapture content fingerprints under an internal-change suppression scope. The next scan compares against the restored fingerprint and does not increment the revision.

Only a later content change that did not originate from the tracked transaction/history path is external drift and receives a new revision. Programmatic Undo/Redo uses the same callback path as keyboard actions.

This is preferred over delaying the scanner because timing-based suppression can still double-increment on slow filesystems or editor frames.

### 6. Coordinate artifact and Session publication with a local commit journal

The service will retain the single Session `map_artifacts.json`, but publish a submission through a versioned commit record:

1. Build the Session working copy and staged artifact turn block in memory.
2. Persist prepared temporary documents and a commit record containing Session id, turn id, fingerprint, old/new document digests, and paths.
3. Publish the artifact document first. The new block remains invisible to normal lookup unless it is referenced by the matching committed Session identity.
4. Persist and activate the Session document containing the locator.
5. Mark the coordinated commit `committed`, then clean temporary data and the commit record.

Readers resolve a locator only when its Session turn identity and artifact entry fingerprint agree. A crash before Session publication leaves at most an unreferenced artifact block, which recovery removes or safely reuses. A crash after Session publication can complete the committed marker from matching digests; it must not roll the Session forward or backward by guesswork. The implementation preserves the existing completed-turn identity and canonical-fingerprint cache semantics: an identical turn/fingerprint returns or resumes the same result, while the same turn with a different fingerprint conflicts. Coordinated-commit recovery extends that path to prepared records instead of replacing or reimplementing its identity algorithm.

This write-ahead coordination is preferred over merging artifacts after Session persistence, which can create dangling locators, and over embedding all large artifacts in the Session document, which would undo the existing storage separation.

Turn identity itself must be stable for that idempotency to hold: the session turn counter is monotonic and non-decreasing across request failures, snapshot rollbacks, and restarts. A failed request's rollback restores only the in-memory snapshot and never lowers the persisted counter; the next successful save persists `max(persisted, in-memory)`, mirroring the existing history-event-counter rule. Once a turn id is committed it is never reallocated, so a later submission always receives an id strictly greater than every committed turn. If a conflict nonetheless appears—only possible from prior divergence or corruption—the submission is rejected with a typed integrity failure that preserves the committed turn and lets the retry run under a fresh turn id, rather than wedging the session. The identity algorithm is unchanged; only the counter invariant and the conflict's recovery path are added.

### 7. Convert plan-boundary exceptions into typed outcomes

Task-payload binding, dependency-result lookup, worker contract construction, and stage transition will execute inside one scheduler boundary. Missing paths, invalid result shapes, or illegal stage transitions produce a typed `blocked` or `replan_required` result containing step id, dependency id/path where relevant, target, revision, and stable error code. No child Frame is created after binding failure.

The scheduler graph is the sole source of pending delegate-group work. The legacy `delegate_groups[*].remaining` queue and consumer are removed. Compatibility loading either migrates a legacy group into one scheduler graph before execution or marks it typed-blocked; it never executes both representations or silently revives the legacy queue.

`create_plan` uses two related identities. An exact attempt key `(task, stage, target, revision, operation, root_error_code)` preserves idempotence and bounds unchanged retries at one semantic state. A task-convergence key `(task_lineage, target, operation, root_error_family)` deliberately excludes sub-step revision and counts plan cycles that have not reached the task's terminal success criteria.

A new authoritative input, revision, or successful predecessor may start a new exact attempt, but revision advancement or partial predecessor success alone does not reset the task-convergence count. The count survives same-task resume and is reset only by a distinct task epoch or by authoritative progress that satisfies an explicit convergence checkpoint or terminal outcome. Revision-local LLM/edit budgets may still reset after a productive write, but they cannot clear this independent reducer-owned convergence count. Prior outcomes remain available for diagnosis at both levels.

This two-level model stops both blind identical retries and `create_plan -> partial success/write -> new revision -> later failure -> create_plan` thrash. It is preferred over catching errors only at HTTP boundaries because a generic 500 loses the plan state needed for deterministic recovery, and over treating every new revision as convergence because partial writes can otherwise run until the outer turn limit.

### 8. Enforce recovery and path contracts at tool boundaries

All map mutations call the synchronous recovery barrier before authorization or batch consumption, then reload the authoritative revision and perform the mutation-boundary CAS described above. Ordinary project-path parameters accept only strings in their documented project scheme. Screenshot/image-review parameters use their separate `res://`, `user://`, and project-relative contract. Malformed Godot scheme lookalikes such as `user:/`, `user:`, `res:/`, and `res:` are rejected before any fallback to the project-relative path resolver. Lists such as `all_paths` validate each element before normalization. Operating-system absolute paths, traversal, unknown schemes, malformed schemes, and non-string values return structured invalid-argument results.

Dead helpers and unreachable patch branches found by the review will be removed or made subject to the same reducer/path tests, preventing them from becoming alternate mutation paths later.

### 9. Build deterministic fault seams before failure-injection tests

Journal/filesystem operations will be accessed through a narrow adapter whose production implementation delegates to the existing Godot APIs and whose test implementation exposes deterministic, named failpoints at durable write, rename, fsync/flush, deletion, commit, and recovery boundaries. A journal fixture builder and separate-process restart driver will exercise every persisted state and simulated exit. The Python coordinated-publication path will expose equivalent test-only failpoints at each resource-publication boundary.

Failpoints are construction- or environment-gated test dependencies, are disabled in production, and cannot be selected by tool requests or persisted user data. Tests first prove that the production composition has no enabled failpoints, then use the harness to assert recovery invariants. This makes the failure suites an executable prerequisite rather than assuming that static engine APIs can be forced to fail.

## Risks / Trade-offs

- **[Risk] Existing persisted journals do not have the new state field** → Add a versioned migration that treats only provably open legacy journals as uncommitted; ambiguous legacy data blocks writes with recovery guidance.
- **[Risk] Coordinated publication adds filesystem writes and startup recovery work** → Keep one compact journal per active Session submission, fsync only state boundaries, and clean committed records eagerly.
- **[Risk] Artifact-first publication can leave unreferenced turn blocks after a crash** → Hide unreferenced entries from readers and garbage-collect them only after journal/retry reconciliation.
- **[Risk] Stricter revision matching exposes callers that omitted revisions** → Return a typed stale/missing-revision result and update all internal callers before enabling strict enforcement.
- **[Risk] A complete task reset could remove information intended for resume** → Resume rehydrates the same epoch from its checkpoint; only creation of a distinct task emits a fresh epoch.
- **[Risk] Fail-closed recovery can temporarily block editing** → Surface transaction id, affected targets, digests, and deterministic recovery actions in the editor instead of a generic failure.
- **[Risk] Hydration/migration exceptions become an honor-system reducer bypass** → Limit raw migration to one exact pre-construction boundary, validate the whole value before publishing it, reject live-state inputs, and use reducer events for every post-construction change.
- **[Risk] Godot commits but the service crashes before reducing the committed result** → Keep the approval unconsumed in persisted service state, reject its stale revision at the post-recovery Godot CAS, and reconcile the service from the typed conflict or replayed committed result before retry.
- **[Trade-off] A task-level convergence breaker can pause legitimate multi-revision work** → Count only completed plan cycles without an explicit convergence checkpoint, keep exact attempt history, and make the bound configurable while requiring a distinct task epoch or proven task progress to reset it.
- **[Risk] Synchronous recovery can stall the Godot main thread on a large journal** → Start one recovery operation eagerly, enforce explicit input bounds, expose progress, benchmark the maximum supported fixture, and chunk/yield work where necessary without allowing mutation to overtake recovery.
- **[Risk] Test failpoints leak into production behavior** → Inject adapters only through test composition, make production defaults incapable of enabling failpoints from request data, and test that guard explicitly.
- **[Risk] A rolled-back request lowers the in-memory turn counter and a later save persists it, so reallocated turn ids collide with committed turns** → Persist `max(persisted, in-memory)` on every save (as the history event counter already does) and never lower the counter on rollback, so committed turn ids are never reused; report any residual conflict as a typed integrity failure with a fresh-turn retry path.

## Migration Plan

1. Add machine-readable workflow-field lifecycle metadata, the closed raw hydration boundary, one-shot resume authorization, complete Gate lifecycle semantics, boundary normalizers, and typed outcomes while retaining compatibility reads.
2. Build deterministic Godot filesystem/journal failpoints, fixture/restart drivers, and Python coordinated-commit failpoints; prove they are unavailable in production composition.
3. Introduce the versioned Godot journal writer and legacy-journal migration, define cleanup as terminal-journal absence, and gate all writes on eager single-flight bounded recovery.
4. Update Undo/Redo revision synchronization and validate it with the failure harness and headless restart/history tests.
5. Add immutable approval ids, authoritative mutation-boundary CAS, and post-commit consumption after journal recovery is in place.
6. Introduce coordinated Session/artifact commit records by extending the existing turn/fingerprint idempotency path; migrate no artifact content because existing committed documents remain readable.
7. Remove the legacy delegate `remaining` queue and enable strict revision, malformed-path, and direct-write checks after compatibility loaders, internal call sites, and stale tests are updated.
8. Run unit, integration, deterministic failure-injection, restart, and end-to-end suites, including interaction with the separate streaming-liveness change.

Rollback keeps the compatibility readers for old journals and artifact documents. The new writers can be disabled together only before any new-format ambiguous transaction exists; otherwise recovery must first reach `committed`, `rolled_back`, or a verified clean state.

## Open Questions

- Should ambiguous Godot `committing` recovery expose only manual restore/accept choices, or also a deterministic “verify from Git/editor import state” assistant?
- How long should unreferenced artifact blocks and completed commit records be retained for retry reconciliation and diagnostics?
- Should the direct-state-write scanner remain a test-only rule or become a repository lint command used by pre-commit/CI?
