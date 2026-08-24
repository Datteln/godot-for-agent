# Chat Event Resume Spec

## Purpose

Sequence-based resume, acknowledgement, gap reporting, and non-executing reconnect behavior.

## Requirements

### Requirement: Every connection explicitly subscribes with a resume cursor
After WebSocket authentication, the client SHALL send a versioned subscribe message containing the target session identifier and its highest contiguous accepted sequence. The server MUST replay retained later events in ascending sequence before delivering subsequent live events.

#### Scenario: Initial subscription
- **WHEN** a client subscribes with `after_seq` equal to zero
- **THEN** the server sends retained session events in ascending sequence and then begins live delivery

#### Scenario: Reconnection after accepted events
- **WHEN** a client reconnects with `after_seq` equal to 41
- **THEN** the server sends retained events with sequence greater than 41 before later live events

### Requirement: Client acknowledgement tracks contiguous delivery
The client SHALL acknowledge the highest contiguous sequence it has accepted. It MUST NOT acknowledge a later sequence while an earlier required sequence remains missing.

#### Scenario: Delayed event delivery
- **WHEN** a client receives sequence 44 while sequence 43 is not accepted
- **THEN** the client does not acknowledge sequence 44 as its contiguous cursor until sequence 43 is accepted or resynchronization occurs

### Requirement: Reconnection never replays commands
The WebSocket reconnect path MUST only connect, authenticate, subscribe, and process events. It MUST NOT resend a user message, approval, cancellation, reset, or tool command.

#### Scenario: Connection loss during an active task
- **WHEN** the socket disconnects while a task is still producing events
- **THEN** the client reconnects and subscribes from its saved cursor without submitting the task again

### Requirement: Retention gaps are explicit
If the server cannot replay all retained events after the requested cursor, it SHALL send a typed `history_gap` or `resync_required` response containing the affected session and cursor information. The client MUST stop accepting visible live patches, hydrate an atomic transcript snapshot, replace transcript state, and only then resume from the snapshot's `upto_event_seq`.

#### Scenario: Client reconnects after event retention expiry
- **WHEN** the requested `after_seq` precedes the earliest retained event
- **THEN** the server reports a typed gap and the client reloads the session transcript before resubscribing from the snapshot cursor

### Requirement: Initial hydration precedes visible patch delivery
When a chat surface opens or switches sessions, the client SHALL hydrate the transcript snapshot before subscribing for visible patches. It MUST NOT render replayed WebSocket patches while its transcript is in the hydrating state.

#### Scenario: Opening a session with retained events
- **WHEN** a session has both historical entries and retained WebSocket events
- **THEN** the client renders the snapshot once and subscribes with `after_seq` equal to the snapshot `upto_event_seq`, so retained entries represented by the snapshot are not appended again

### Requirement: Slow subscriptions are bounded
The service SHALL use a bounded outbound queue for every subscribed connection, with both event-count and serialized-byte limits. It MUST coalesce replaceable, not-yet-sent streaming updates for the same transcript entry, while preserving non-streaming and terminal states in order. If a subscriber cannot keep up after that coalescing, the service MUST report resynchronization and close or reset that subscription instead of silently dropping ordered events or blocking other clients' event delivery.

#### Scenario: Stalled editor client
- **WHEN** one subscribed plugin stops draining its event queue until the configured count or byte bound is exceeded
- **THEN** the service coalesces obsolete same-entry stream updates where possible, then marks that subscription for resynchronization and preserves other clients' event delivery

### Requirement: Heartbeats report transport liveness only
The client and server SHALL exchange heartbeat messages while a subscription is idle. A missing heartbeat MUST change transport connection state but MUST NOT mutate task completion, cancellation, or timeout state by itself.

#### Scenario: Idle but healthy task session
- **WHEN** no chat event is produced during a heartbeat interval and heartbeat messages continue successfully
- **THEN** the plugin reports an active transport connection without changing the task state

### Requirement: Idle request recovery does not cancel a healthy active turn
The client SHALL treat a lack of usable live patches as a recovery condition before treating it as a cancellation condition. It MUST attempt a bounded reconnect and cursor resume, or snapshot hydration after a typed gap, and SHALL continue waiting when the recovered session reports the same active turn. It MUST only issue an automatic interruption after recovery fails, the active turn is absent, or the chat hard cap expires.

#### Scenario: Resume after a temporary event-stream interruption
- **WHEN** an active chat request becomes idle locally and the WebSocket reconnect resumes from the acknowledged cursor
- **THEN** the client keeps the existing chat request active and does not submit `/chat/interrupt`