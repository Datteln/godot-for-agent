## ADDED Requirements

### Requirement: Model previews remain incremental during atomic submissions
The system MUST publish assistant text and reasoning preview deltas incrementally while an atomic tool-result submission is running, without waiting for the Session transaction to commit.

#### Scenario: Model emits chunks inside a tool-result transaction
- **WHEN** the provider emits multiple text or reasoning chunks while a validated tool-result working copy is active
- **THEN** the event channel exposes corresponding provisional preview updates before the provider stream completes or the Session is saved

#### Scenario: Initial user turn has no tool-result transaction
- **WHEN** the provider emits chunks for a normal user-message submission
- **THEN** the event channel continues to expose those chunks incrementally without adding a second delivery path

### Requirement: Provisional previews have an explicit lifecycle
Every preview emitted before atomic commit MUST carry stable submission identity and MUST be resolved by a commit or discard boundary without being duplicated as committed text.

#### Scenario: Submission commits
- **WHEN** Session persistence and transactional publication succeed after provisional previews were emitted
- **THEN** the service emits a matching preview-committed boundary and the client retains the displayed text without appending it again

#### Scenario: Submission rolls back
- **WHEN** the submission is cancelled, rejected, or fails before Session persistence succeeds
- **THEN** the service emits a matching preview-discarded boundary and the client removes or clearly invalidates only that submission's provisional output

#### Scenario: Service restarts before a lifecycle boundary
- **WHEN** a process-local preview has no matching committed Session identity after restart
- **THEN** Session history does not restore that preview as committed conversation content

#### Scenario: A stale boundary arrives
- **WHEN** the client receives a commit or discard boundary for a request older than the active preview
- **THEN** it applies the boundary only to matching preview identity and does not alter the active request

### Requirement: Event delivery applies bounded backpressure
The event endpoint MUST return an ordered, bounded page and MUST tell the client whether more accepted events remain.

#### Scenario: More events exist than one response limit
- **WHEN** the number of events after the supplied cursor exceeds the configured page limit
- **THEN** the endpoint returns at most the limit in sequence order with `has_more=true` and a cursor covering only the returned events

#### Scenario: Client receives a backlog page
- **WHEN** a successful event response has `has_more=true`
- **THEN** the client requests the next page as soon as the event HTTP connection is available instead of waiting for the idle polling interval

#### Scenario: No backlog remains
- **WHEN** the response contains the last currently available event
- **THEN** it reports `has_more=false` and the client resumes the configured idle polling cadence

### Requirement: Event rendering is frame-budgeted
The chat UI MUST process an event backlog across frames within a configured item or elapsed-time budget while preserving accepted sequence order.

#### Scenario: One poll returns a large event page
- **WHEN** the client receives enough events to exceed the per-frame render budget
- **THEN** the UI renders only the allowed prefix in the current frame and continues the remaining ordered events in subsequent frames without blocking editor interaction

#### Scenario: Snapshot deltas can be coalesced
- **WHEN** multiple replaceable snapshot deltas address the same frame and message before rendering
- **THEN** the UI may retain only the newest snapshot but MUST NOT drop append-only delta fragments

### Requirement: Follow mode reflects user intent
The chat UI MUST remain at the newest streamed content while follow mode is enabled and MUST disable follow mode only in response to an identified user navigation away from the bottom.

#### Scenario: Content growth changes the scrollbar maximum
- **WHEN** streamed layout growth makes the current scrollbar value temporarily lower than the new maximum while follow mode is enabled
- **THEN** the UI keeps follow mode enabled and scrolls to the new bottom after layout settles

#### Scenario: User scrolls upward
- **WHEN** mouse, touchpad, keyboard, or scrollbar interaction intentionally moves the viewport away from the bottom
- **THEN** the UI disables follow mode and does not pull the viewport back for ordinary preview deltas

#### Scenario: User returns to the bottom
- **WHEN** the user navigates back within the bottom threshold
- **THEN** the UI re-enables follow mode for subsequent streamed content

#### Scenario: A preview batch is rendered while following
- **WHEN** one frame processes multiple preview events and follow mode is enabled
- **THEN** the UI schedules one bottom-scroll correction after the batch layout instead of toggling follow state between individual events

### Requirement: Streaming latency and backlog are observable
The system SHALL record structured diagnostics that distinguish provider streaming, server preview publication, client receipt, backlog paging, UI drain, and preview resolution.

#### Scenario: Streaming is diagnosed from logs
- **WHEN** a chat request completes or is interrupted
- **THEN** logs provide request-correlated first-chunk, first-publication, batch/backlog, and commit-or-discard evidence without logging secrets or full prompt content
