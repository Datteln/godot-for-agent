## MODIFIED Requirements

### Requirement: Coordinated publication is idempotent
The coordinated Session/artifact/workflow commit MUST use the canonical completed-turn identity and submission fingerprint stored in the durable identity ledger. Prepared coordinated commits and completed retries MUST resolve through that one identity algorithm; a bounded response hot cache is only an optimization and never an authority.

#### Scenario: Client retries an interrupted identical submission
- **WHEN** a retry has the same turn identity and canonical fingerprint as a prepared coordinated commit
- **THEN** the runtime resumes or returns that commit without duplicating artifact entries, messages, grants, workflow events, approvals, or mutations

#### Scenario: Retry conflicts with prepared content
- **WHEN** a retry reuses a known turn identity with a different canonical fingerprint
- **THEN** the runtime rejects the conflict and preserves the original prepared or committed data for recovery

#### Scenario: Retry matches a completed turn after hot-cache eviction
- **WHEN** a retry has the same identity and fingerprint as a durable completed-turn ledger entry whose response left the hot cache
- **THEN** the runtime loads or reconstructs its canonical outcome without starting another coordinated commit

### Requirement: Reset creates an event-stream synchronization boundary
Event sequence numbers for a reused `session_id` SHALL remain monotonic across reset. The service MUST discard prior-epoch event content, emit a new-epoch WebSocket reset boundary, and return the authoritative `session_epoch` and `last_event_seq` in the reset acknowledgement. WebSocket, snapshot, and history readers MUST reject or omit old-epoch content.

#### Scenario: An old WebSocket frame arrives after reset
- **WHEN** a prior-epoch event or control frame arrives after reset acknowledgement
- **THEN** the event acceptor rejects it, adopts no cursor from it, and cannot append prior-conversation content

#### Scenario: A client reconnects after reset
- **WHEN** the client resumes WebSocket delivery using the acknowledged epoch and `last_event_seq`
- **THEN** it receives only new-epoch events above the acknowledged high-water sequence

### Requirement: Reset acknowledgement controls the frontend transition
The frontend SHALL enter a non-sendable `resetting` state before requesting reset and SHALL clear Session-owned presentation and safety state only while atomically adopting a successful reset acknowledgement. It MUST close the old WebSocket, clear pending tool batches, undo presentation state, recovery UI, and per-Session caches, and reconnect under the returned epoch before resuming input.

#### Scenario: Reset succeeds
- **WHEN** the server acknowledges a durable new epoch
- **THEN** the state owner clears the prior conversation, closes recovery UI, adopts epoch and cursor, reconnects the event socket, and then enables new input

#### Scenario: Reset fails
- **WHEN** the server returns a typed reset failure or the request is interrupted
- **THEN** the frontend does not present an empty successful conversation, keeps input blocked until prior state is retained or reloaded, and shows the reset error

## ADDED Requirements

### Requirement: Completed tool-result identity outlives the response hot cache
For the lifetime of a new-schema Session epoch, every committed tool-result batch MUST retain a compact durable identity containing turn id, canonical fingerprint, outcome kind, commit digest, and a durable response or checkpoint locator. Evicting a full response from a bounded hot cache MUST NOT remove that identity or permit the batch to be applied again.

#### Scenario: Identical retry arrives after hot-cache eviction
- **WHEN** a client resubmits the same committed turn id and canonical fingerprint after its full response body was evicted
- **THEN** the runtime reconstructs or loads the original outcome and applies no messages, grants, artifacts, reducer events, approvals, or mutations again

#### Scenario: Conflicting retry arrives after hot-cache eviction
- **WHEN** an old committed turn id is submitted with a different canonical fingerprint after hot-cache eviction
- **THEN** the durable identity ledger rejects it as a typed conflict and preserves the original commit

#### Scenario: Session is reset
- **WHEN** reset establishes a new Session epoch
- **THEN** old-epoch completed identities are unreachable from the new epoch even if cleanup files still exist

### Requirement: Submission publication scope is explicit and singular
User and tool-result submission MUST carry one typed per-submission scope containing the working Session, request and turn identities, staged artifact turn, preview lifecycle, and buffered event publisher. This scope MUST be passed explicitly to subordinate services and MUST NOT be discovered through a module-global `ContextVar`, singleton application facade, or ambient mutable global. Only the owning use case may commit or resolve the scope.

#### Scenario: Turn execution emits provisional and transactional events
- **WHEN** a submission invokes TurnDriver and subordinate Map handlers emit events
- **THEN** the bound buffered publisher records them in that submission scope and exposes only permitted provisional events before durable commit

#### Scenario: Coordinated commit succeeds
- **WHEN** Session, workflow, artifacts, completed identity, and response locator commit successfully
- **THEN** the owning submission use case flushes the one scope exactly once and resolves matching previews as committed

#### Scenario: Submission fails or is cancelled
- **WHEN** validation, provider execution, reducer application, persistence, or coordinated publication fails
- **THEN** the owning use case discards or recovers the same scope without leaking buffered business events or resolving another submission's previews

#### Scenario: Publication ownership is inspected
- **WHEN** architecture tests inspect application publication state
- **THEN** no global publication `ContextVar`, `AgentApplication` event indirection, or second commit owner exists

## REMOVED Requirements

### Requirement: Legacy per-call artifacts are read-only migration inputs
**Reason**: The clean-cut runtime has no compatibility readers and accepts only canonical Session artifact storage.

**Migration**: No runtime migration is provided. Start a new Session and regenerate any required map context through current tools.
