## MODIFIED Requirements

### Requirement: Staged map artifacts follow the Session transaction
The system SHALL aggregate map results for the active submission as one staged turn block, allow the active transaction to read that block, and coordinate publication of the artifact block and Session locator through a recoverable, idempotent commit.

#### Scenario: Agent reads an artifact during the same submission
- **WHEN** a server-side map artifact reader requests an entry produced by front-tool results in the active uncommitted submission
- **THEN** the reader resolves the entry from the transaction-local staged turn block without requiring the persistent file to contain it

#### Scenario: Session commit succeeds
- **WHEN** the Session working copy and complete staged artifact turn block can both be persisted
- **THEN** the runtime makes the artifact entry and its Session locator jointly visible under the same turn identity and canonical fingerprint

#### Scenario: Artifact publication fails
- **WHEN** artifact preparation or publication fails before the coordinated commit completes
- **THEN** the active Session remains equal to its pre-request state, exposes no locator for the artifact, and keeps the submission safely retryable

#### Scenario: Submission is interrupted or rolled back
- **WHEN** the request is interrupted, cancelled, rejected, or fails reducer, artifact, or Session persistence
- **THEN** no committed locator or readable residual turn block remains, and recovery can discard or reconcile prepared data using the commit record

#### Scenario: Process stops between resource publications
- **WHEN** a process exit occurs after one coordinated resource is written but before the complete commit is marked durable
- **THEN** startup recovery uses recorded identities and digests to finish or roll back the known state without exposing a dangling locator or guessing content

## ADDED Requirements

### Requirement: Artifact publication never leaves a dangling locator
Every committed map-artifact locator MUST resolve to the exact committed artifact entry identified by Session, turn, entry, and canonical content fingerprint.

#### Scenario: A committed locator is read
- **WHEN** an Agent resolves a map-artifact locator from committed Session state
- **THEN** the artifact document contains the matching entry and fingerprint or the system reports a typed integrity failure and blocks dependent mutation

#### Scenario: An unreferenced prepared artifact exists
- **WHEN** recovery finds an artifact turn block that is not referenced by a matching committed Session turn
- **THEN** normal readers cannot observe it and recovery either reuses it for the identical retry or removes it after reconciliation

### Requirement: Coordinated publication is idempotent
The coordinated Session/artifact commit MUST preserve the existing completed-turn identity and canonical submission-fingerprint semantics, extending that path to prepared coordinated commits rather than replacing its identity algorithm.

#### Scenario: Client retries an interrupted identical submission
- **WHEN** a retry has the same turn identity and canonical fingerprint as a prepared coordinated commit
- **THEN** the runtime resumes or returns that commit without duplicating artifact entries, messages, grants, or workflow mutations

#### Scenario: Retry conflicts with prepared content
- **WHEN** a retry reuses a known turn identity with a different canonical fingerprint
- **THEN** the runtime rejects the conflict and preserves the original prepared or committed data for recovery

#### Scenario: Retry matches an existing completed turn
- **WHEN** a retry has the same turn identity and canonical fingerprint as a result already held by the existing completed-turn cache
- **THEN** the runtime returns that cached result through the existing identity path without starting a new coordinated commit

### Requirement: Coordinated commit boundaries are deterministically testable
The coordinated Session/artifact implementation MUST expose test-only named failpoints at each durable preparation, resource-publication, commit-marker, and cleanup boundary, and the production composition MUST keep them disabled and unreachable from submission payloads.

#### Scenario: Process exit is injected between publications
- **WHEN** a test process exits at the named boundary after artifact publication and before Session publication
- **THEN** restart reconciliation deterministically proves that no committed Session exposes a dangling locator

#### Scenario: Production submission is processed
- **WHEN** an ordinary client submits tool results
- **THEN** no request field can activate or select a coordinated-commit failpoint

### Requirement: Session turn identity is monotonic and never reused
The session turn counter SHALL be monotonic and non-decreasing across request failures, in-memory snapshot rollbacks, and process restarts. A failed request that rolls back to its pre-request snapshot SHALL NOT lower the persisted turn counter: the next successful session save SHALL persist `max(persisted_counter, in_memory_counter)`, the same monotonic rule already applied to the history event counter. Once a turn id has been committed in the coordinated commit record or `map_artifacts.json`, it SHALL never be reallocated, so a later submission always receives a turn id strictly greater than every previously committed turn id.

#### Scenario: Request fails after allocating a turn id
- **WHEN** a request allocates one or more turn ids and then fails, rolling the in-memory session back to its pre-request snapshot
- **THEN** the persisted turn counter is not lowered, and the next successful save persists `max(persisted, in-memory)` so a later submission cannot receive a turn id already committed

#### Scenario: Session is restored after a restart
- **WHEN** the session is loaded from disk after a process restart
- **THEN** the restored turn counter exceeds every turn id committed in `map_artifacts.json`, and the next allocated turn id is strictly greater than all of them

### Requirement: A turn-identity conflict is a typed, recoverable failure
When a staged submission reuses a turn id whose committed fingerprint differs (a state that can only arise from prior counter divergence or data corruption), the runtime SHALL reject the conflicting submission with a typed integrity failure instead of raising an uncaught exception or wedging the session. The rejection SHALL preserve the original committed data and SHALL allow the submission to be retried under a fresh, strictly-greater turn id without requiring manual deletion of the committed turn. The identity and canonical-fingerprint algorithm is unchanged; only the failure's observability and recovery path change.

#### Scenario: Staged turn conflicts with a committed turn
- **WHEN** a staged submission carries a turn id already committed with a different canonical fingerprint
- **THEN** the runtime returns a typed integrity problem with disposition `retry_new_turn`, leaves the committed turn intact, preserves the task checkpoint, and provides a bound recovery identity through which the backend allocates a strictly greater turn rather than wedging or requiring text-driven manual reconstruction

#### Scenario: Conflict does not bypass the idempotency algorithm
- **WHEN** the conflict is reported
- **THEN** the existing completed-turn identity and canonical-fingerprint semantics remain the source of truth, and no second identity algorithm is introduced

### Requirement: User reset establishes a complete session-epoch boundary
Every logical conversation SHALL be identified by `(session_id, session_epoch)`. A successful user reset MUST durably establish a fresh collision-resistant epoch before acknowledging success, make every prior-epoch session-owned resource immediately unreachable, and eventually remove it through idempotent cleanup. Session ids allocated for new conversations MUST also be collision-resistant. Reset SHALL preserve committed Godot project content, authoritative map revisions, transaction journals required for crash recovery, project indexes and registries, and user-global configuration or memory.

#### Scenario: A reused Session id starts after reset
- **WHEN** reset succeeds and a later request reuses the same `session_id`
- **THEN** the request runs under the new epoch with no Session state, turn allocation, completed-turn result, idempotency entry, map artifact, delegate artifact, recovery pointer, history projection, file-read authorization, or frontend session state inherited from the prior epoch

#### Scenario: Reset cleanup is interrupted
- **WHEN** the process exits or physical deletion fails after the new epoch boundary is durable
- **THEN** prior-epoch data remains unreadable, restart retries cleanup from the reset record, and no authoritative project or crash-recovery journal is deleted

#### Scenario: The epoch boundary cannot be persisted
- **WHEN** reset cannot durably establish the new epoch
- **THEN** the runtime returns a typed reset failure, does not acknowledge a new conversation, and leaves the prior conversation consistently active

### Requirement: Reset isolates turn and artifact identity
All turn allocation, completed-turn lookup, canonical-fingerprint idempotency, map-artifact lookup, delegate-artifact lookup, staged publication, and recovery reconciliation MUST be scoped or guarded by `session_epoch`. A locator or retry from an older epoch MUST NOT resolve or reserve identity in the new epoch.

#### Scenario: A prior conversation committed turn t4
- **WHEN** the Session is reset and the new conversation later allocates a turn whose display identifier is `t4`
- **THEN** the prior epoch's committed `t4` cannot conflict with, satisfy, or expose content to the new submission

#### Scenario: An old artifact locator is replayed
- **WHEN** a request in the new epoch presents a map or delegate artifact locator created in an older epoch
- **THEN** the runtime rejects it with a typed stale-epoch failure and performs no dependent mutation

### Requirement: Reset creates an event-stream synchronization boundary
Event sequence numbers for a reused `session_id` SHALL remain monotonic across reset. The service MUST discard prior-epoch event content, emit a new-epoch reset boundary, and return the authoritative `session_epoch` and `last_event_seq` in the reset acknowledgement. Event and history readers MUST reject or omit old-epoch content even when an earlier poll completes late.

#### Scenario: An event request is in flight during reset
- **WHEN** a prior-epoch event poll completes after reset acknowledgement
- **THEN** the client ignores that response, adopts the acknowledged epoch and cursor, and cannot append prior-conversation events to the reset conversation

#### Scenario: A client reconnects after reset
- **WHEN** a client resumes event polling using the acknowledged epoch and `last_event_seq`
- **THEN** it receives only new-epoch events with sequence numbers above the acknowledged high-water and cannot replay prior-epoch history

### Requirement: Reset acknowledgement controls the frontend transition
The frontend SHALL enter a non-sendable `resetting` state before requesting reset and SHALL clear session-owned presentation and safety state only as part of adopting a successful reset acknowledgement. It MUST cancel or replace old event polling, clear pending tool batches, undo presentation state, recovery UI, and per-session caches, and resume input only after adopting the returned epoch and cursor.

#### Scenario: Reset succeeds
- **WHEN** the server acknowledges a durable new epoch
- **THEN** the frontend clears the prior conversation, closes recovery UI, switches its event cursor and epoch, and then enables new input

#### Scenario: Reset fails
- **WHEN** the server returns a typed reset failure or the request is interrupted
- **THEN** the frontend does not present an empty successful conversation, keeps input blocked until the prior state is retained or reloaded, and shows the reset error

### Requirement: File safety and derived caches do not cross reset
Per-session file-read authorization and derived Session caches, including history-block and recovery caches, MUST be keyed by epoch or explicitly invalidated during reset. A read, cache signature, or recovery pointer from an older epoch SHALL NOT authorize or satisfy an operation in the new epoch.

#### Scenario: A file was read before reset
- **WHEN** the new conversation attempts an edit without reading that file in its own epoch
- **THEN** the edit is rejected by the read-before-edit guard even if the same path was read before reset

#### Scenario: A new conversation reproduces an old cache signature
- **WHEN** its frame count, event count, cursor, or other weak cache dimensions equal those of the prior conversation
- **THEN** history and recovery lookup return only new-epoch data rather than the prior cached projection

### Requirement: Problem responses carry machine-actionable recovery disposition
Every non-terminal request, model, tool, plan, front-result, and coordinated-publication problem MUST expose a stable structured payload containing `task_id`, `attempt_id`, `checkpoint_id`, `error_code`, `disposition`, `retryable`, `side_effect_state`, optional `retry_token`, and `next_action`. Recovery MUST NOT depend on matching localized response text. The closed disposition set SHALL be `continue_agent`, `retry_same_attempt`, `retry_new_attempt`, `retry_new_turn`, `refresh_and_replan`, `wait_frontend`, `pause_for_user`, and `terminal`.

#### Scenario: A server tool raises before producing a side effect
- **WHEN** the tool failure is safe to return to the active agent
- **THEN** the problem uses `continue_agent`, is appended as a typed tool result, and the task continues without frontend terminal cleanup

#### Scenario: A front-tool result must be resubmitted
- **WHEN** a transport or persistence failure leaves the original pending result batch safely retryable
- **THEN** the problem uses `wait_frontend` or `retry_same_attempt`, preserves pending call identity, and supplies the exact idempotent next action without clearing the batch

#### Scenario: An effect is ambiguous
- **WHEN** reconciliation cannot prove whether a mutation committed or rolled back
- **THEN** the problem uses `pause_for_user`, retains the checkpoint and diagnostic identity, and prohibits automatic replay

### Requirement: Recovery tokens are bound and single-use
A recovery token MUST be opaque, single-use, and bound to session epoch, durable task, checkpoint, canonical attempt identity, expected side-effect state, and permitted disposition. Consuming it MUST NOT widen target, tool, write, approval, or permission scope.

#### Scenario: A fresh-turn retry is authorized
- **WHEN** the Recovery Supervisor consumes a valid `retry_new_turn` token after reconciling the preserved committed turn
- **THEN** it allocates a strictly greater turn, invalidates that token, and resumes from the bound checkpoint without duplicating messages, artifacts, grants, approvals, or mutations

#### Scenario: A token is replayed or belongs to an old epoch
- **WHEN** a recovery token is reused, expired, mismatched to side-effect state, or belongs to another session epoch
- **THEN** the runtime rejects it with a typed stale-recovery failure and performs no task or project mutation

### Requirement: Backend supervision owns automatic recovery
The backend MUST apply recovery dispositions only after the failed attempt's transaction and side effects are reconciled. It SHALL own provider fallback, bounded backoff, fresh-turn allocation, authoritative refresh, replan, and restart continuation. The frontend MUST NOT replay an entire `/chat` request, choose a fallback model, or reconstruct retry identity independently.

#### Scenario: The client loses the chat response
- **WHEN** the durable task and attempt outcome were persisted but the response transport closes
- **THEN** reconnect or event recovery observes the same task/checkpoint/attempt state and the backend continues or pauses according to the persisted disposition without duplicating the request

#### Scenario: The service restarts during recovery
- **WHEN** a non-terminal disposition was persisted before process exit
- **THEN** startup supervision resumes from that checkpoint and retry budget rather than treating the task as idle or completed

### Requirement: Frontend cleanup follows disposition and side-effect state
The frontend MUST represent `running`, `recovering`, `waiting_frontend`, `paused`, and terminal states separately. Receiving a non-terminal problem MUST NOT invoke unconditional error cleanup. Pending calls, approval presentation, recovery identity, and an open Undo batch SHALL be preserved, resolved, or aborted according to the structured disposition and `side_effect_state`.

#### Scenario: A recoverable problem is received
- **WHEN** the problem disposition is not `terminal`
- **THEN** the frontend shows recovery progress or a typed pause, retains the state required by `next_action`, and does not clear pending calls or switch the durable task to idle

#### Scenario: A terminal outcome is received
- **WHEN** Completion Gate success, explicit cancellation, confirmed rollback/discard, reset, or proven permanent failure authorizes cleanup
- **THEN** the frontend resolves the matching attempt and clears only the state owned by that terminal transition
