## Context

The map-agent runtime already has the intended architectural pieces—dependency-aware plans, a workflow reducer, a Completion Gate, grouped map transactions, Session working copies, and a consolidated `map_artifacts.json`. The review found that several boundary conditions still bypass those pieces:

- task-owned state can survive into a later map task or be mutated outside the reducer;
- validation results with missing revisions or nullable issue arrays can bypass or crash the Completion Gate;
- platform approvals are consumed before their write is durably committed;
- the Godot journal cannot distinguish an uncommitted edit from a committed edit whose journal cleanup failed;
- Undo/Redo restores revision metadata, then the external-change scanner treats the restored content as a new edit and increments it again;
- Session persistence and artifact publication can expose a locator before its artifact is durable;
- scheduler binding and worker-stage errors escape as exceptions, while repeated `create_plan` calls can overwrite useful terminal state;
- deferred recovery and permissive path argument handling leave first-write and file-boundary races.

The change crosses the Python orchestration service, Godot editor integration, and both sides' persistence formats. It must remain compatible with existing Session documents, `map_artifacts.json`, and HTTP contracts. A separate active change owns streaming liveness during atomic submissions; this design does not redefine that capability.

## Goals / Non-Goals

**Goals:**

- Make every map task start from a complete, reducer-owned task epoch while retaining only explicitly session-scoped state.
- Make validation, evidence, blockers, and completion decisions exact for `(target, revision)` and safe for nullable external payloads.
- Couple approval consumption and revision advancement to successful durable map commits.
- Make transaction recovery distinguish prepared, ambiguous, committed, and rolled-back edits without guessing.
- Keep Undo/Redo content, revision metadata, and the external-change fingerprint synchronized.
- Coordinate Session locator and map-artifact publication so readers never observe a dangling locator.
- Convert orchestration boundary failures into typed plan outcomes and bound repeated plan creation.
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

State fields will be classified as task-scoped, revision-scoped, or session-scoped next to the state schema. A static test will reject direct writes to reducer-owned fields and nested containers outside reducer code and approved deserialization/migration boundaries.

This is preferred over scattered resets in Agent and QueryEngine because a single event gives restart/resume code the same initialization semantics and makes omissions testable.

### 2. Normalize validation at ingestion and require exact scope identity

Validator/reviewer payloads will be parsed into typed internal values before entering the reducer or Completion Gate. Missing or null issue collections normalize to empty lists; malformed collections produce a typed validation failure rather than an exception. A validation can satisfy a gate only when both canonical target and concrete revision exactly match. A missing revision is never a wildcard.

Blockers will be updated with scoped upsert/remove operations keyed by task, target, revision, source, and stable issue identity. A validator transport failure adds or replaces only its own scoped blocker and cannot erase blockers for other targets or revisions.

This is preferred over defensive `list(value)` calls at each consumer because one boundary normalizer keeps all downstream logic total and consistent.

### 3. Consume an approved batch from the committed result

Validation creates an immutable approval record containing approval id, target, expected revision, and canonical batch fingerprint. Preflight verifies that record but does not remove it or advance revision. The Godot write transaction returns the approval id and committed revision in its durable result. Only the reducer event for that successful committed result consumes the matching approval and advances workflow state.

Rejected, cancelled, failed, or rolled-back writes retain the approval while its expected revision remains current. A changed authoritative revision invalidates it. Replayed committed results are idempotent by transaction/approval identity.

This is preferred over a temporary “claimed” pop because a claim introduces another recovery state and can still lose the batch when the process exits before write execution.

### 4. Use an explicit durable map transaction journal state machine

The Godot journal will use versioned, checksummed states:

`prepared -> applying -> committing -> committed -> cleaned`

Rollback records `rolled_back` before cleanup. Snapshots and affected revision metadata are durable before `applying`. The journal records the transaction id, approval id where applicable, before/after fingerprints, and expected revisions.

- `prepared` or `applying` is recoverably uncommitted and rolls back from the before snapshot.
- `committed` is never rolled back; restart only verifies the after fingerprint and retries cleanup.
- `committing` is ambiguous. Automatic writes remain blocked until deterministic evidence proves the before or after state, or the user follows explicit recovery instructions.
- checksum, schema, or snapshot failures fail closed.

The committed marker is persisted after the Undo action commits and before journal deletion. A synchronous `ensure_recovered()` barrier runs before the first map mutation, even if background recovery was scheduled earlier.

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

Readers resolve a locator only when its Session turn identity and artifact entry fingerprint agree. A crash before Session publication leaves at most an unreferenced artifact block, which recovery removes or safely reuses. A crash after Session publication can complete the committed marker from matching digests; it must not roll the Session forward or backward by guesswork. Submission retry uses the existing turn id plus canonical fingerprint to resume the same coordinated commit without duplicating entries.

This write-ahead coordination is preferred over merging artifacts after Session persistence, which can create dangling locators, and over embedding all large artifacts in the Session document, which would undo the existing storage separation.

### 7. Convert plan-boundary exceptions into typed outcomes

Task-payload binding, dependency-result lookup, worker contract construction, and stage transition will execute inside one scheduler boundary. Missing paths, invalid result shapes, or illegal stage transitions produce a typed `blocked` or `replan_required` result containing step id, dependency id/path where relevant, target, revision, and stable error code. No child Frame is created after binding failure.

`create_plan` attempts use a semantic key `(task, stage, target, revision, operation, root_error_code)`. Repeated attempts with no new revision, input, or successful predecessor preserve the current/terminal plan and trip a bounded circuit breaker. New authoritative information starts a new attempt while retaining prior outcomes for diagnosis.

This is preferred over catching errors only at HTTP boundaries because a generic 500 loses the plan state needed for deterministic recovery.

### 8. Enforce recovery and path contracts at tool boundaries

All map mutations call the synchronous recovery barrier before authorization or batch consumption. Ordinary project-path parameters accept only strings in their documented project scheme. Screenshot/image-review parameters use their separate `res://`, `user://`, and project-relative contract. Lists such as `all_paths` validate each element before normalization. Operating-system absolute paths, traversal, unknown schemes, and non-string values return structured invalid-argument results.

Dead helpers and unreachable patch branches found by the review will be removed or made subject to the same reducer/path tests, preventing them from becoming alternate mutation paths later.

## Risks / Trade-offs

- **[Risk] Existing persisted journals do not have the new state field** → Add a versioned migration that treats only provably open legacy journals as uncommitted; ambiguous legacy data blocks writes with recovery guidance.
- **[Risk] Coordinated publication adds filesystem writes and startup recovery work** → Keep one compact journal per active Session submission, fsync only state boundaries, and clean committed records eagerly.
- **[Risk] Artifact-first publication can leave unreferenced turn blocks after a crash** → Hide unreferenced entries from readers and garbage-collect them only after journal/retry reconciliation.
- **[Risk] Stricter revision matching exposes callers that omitted revisions** → Return a typed stale/missing-revision result and update all internal callers before enabling strict enforcement.
- **[Risk] A complete task reset could remove information intended for resume** → Resume rehydrates the same epoch from its checkpoint; only creation of a distinct task emits a fresh epoch.
- **[Risk] Fail-closed recovery can temporarily block editing** → Surface transaction id, affected targets, digests, and deterministic recovery actions in the editor instead of a generic failure.
- **[Risk] Static direct-write detection can flag migration or deserialization code** → Maintain a narrow allowlist for audited hydration boundaries and require reducer events after hydration.
- **[Trade-off] The circuit breaker can pause a plan that might succeed on another blind retry** → Progress signals and revision/input changes reset it; unchanged retries are deliberately stopped to avoid loops and overwritten diagnostics.

## Migration Plan

1. Add state classification, boundary normalizers, typed outcomes, and regression tests while retaining compatibility reads.
2. Introduce the versioned Godot journal writer and legacy-journal migration; gate all writes on synchronous recovery.
3. Update Undo/Redo revision synchronization and validate it with headless Godot restart/history tests.
4. Add immutable approval ids and post-commit consumption, initially logging mismatches before enforcing rejection.
5. Introduce coordinated Session/artifact commit records and startup recovery; migrate no artifact content because existing committed documents remain readable.
6. Enable strict revision, path, and direct-write checks after internal call sites and stale tests are updated.
7. Run unit, integration, failure-injection, restart, and end-to-end suites, including interaction with the separate streaming-liveness change.

Rollback keeps the compatibility readers for old journals and artifact documents. The new writers can be disabled together only before any new-format ambiguous transaction exists; otherwise recovery must first reach `committed`, `rolled_back`, or a verified clean state.

## Open Questions

- Should ambiguous Godot `committing` recovery expose only manual restore/accept choices, or also a deterministic “verify from Git/editor import state” assistant?
- How long should unreferenced artifact blocks and completed commit records be retained for retry reconciliation and diagnostics?
- Should the direct-state-write scanner remain a test-only rule or become a repository lint command used by pre-commit/CI?
