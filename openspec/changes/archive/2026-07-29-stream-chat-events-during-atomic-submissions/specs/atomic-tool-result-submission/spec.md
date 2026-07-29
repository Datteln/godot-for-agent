## MODIFIED Requirements

### Requirement: Atomic publication remains externally live
The system MUST expose non-mutating request-liveness progress and provisional model previews while Session facts and artifacts remain buffered for atomic commit. Provisional previews MUST NOT be treated as committed, recoverable Session state until persistence succeeds.

#### Scenario: A valid tool-result submission runs longer than the client idle timeout
- **WHEN** the backend is still processing the submission but no committed transactional event can yet be published
- **THEN** it emits an out-of-band `turn_progress` heartbeat containing request/turn identity and phase but no tool result, grant, artifact locator, or recoverable Session mutation

#### Scenario: The model streams during a valid tool-result submission
- **WHEN** assistant text or reasoning chunks are produced against the isolated Session working copy
- **THEN** the backend publishes them as request-correlated provisional previews while continuing to buffer transactional events and artifacts

#### Scenario: A buffered submission rolls back
- **WHEN** the Session working copy is rejected, interrupted, or fails persistence after heartbeats or provisional previews were emitted
- **THEN** no buffered business event or artifact becomes visible, earlier heartbeats cannot be replayed as committed state, and matching previews are explicitly discarded

#### Scenario: A buffered submission commits
- **WHEN** the working Session is persisted successfully
- **THEN** buffered business events and artifacts are published once and matching previews are confirmed without duplicating their text

#### Scenario: The client receives progress
- **WHEN** either a progress heartbeat, a provisional preview, or a committed event arrives for the active request
- **THEN** the client refreshes its idle watchdog without resubmitting the chat request or selecting a model
