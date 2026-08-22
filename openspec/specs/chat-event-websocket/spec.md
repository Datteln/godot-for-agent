# Chat Event WebSocket Spec

## Purpose

A single authenticated, resumable WebSocket transport for all live chat and task events.

## Requirements

### Requirement: WebSocket is the only live chat-event transport
The service SHALL deliver live chat and task events through an authenticated WebSocket endpoint. The Godot plugin MUST receive live events from that endpoint and MUST NOT poll an HTTP events endpoint.

#### Scenario: Live assistant response
- **WHEN** a connected subscribed session produces assistant text or tool activity
- **THEN** the plugin receives the event through the WebSocket subscription without issuing an HTTP event-poll request

### Requirement: HTTP remains the command transport
The plugin SHALL continue to use HTTP for user chat submission, confirmation, interruption, reset, and history retrieval. The WebSocket endpoint MUST NOT execute user commands received as event-subscription messages.

#### Scenario: User submits a chat message while connected
- **WHEN** the user sends a new chat message
- **THEN** the plugin submits the command through the existing HTTP command path and receives its resulting live events through WebSocket

### Requirement: Subscription is authenticated and session-scoped
The WebSocket connection and subscribe message MUST satisfy the configured service authentication policy. The service SHALL permit subscription only to the requested authorized session and project scope.

#### Scenario: Unauthorized session subscription
- **WHEN** a client attempts to subscribe to a session outside its authorized scope
- **THEN** the service returns a typed authorization error and does not deliver session events

### Requirement: Event envelope has immutable identity
Each delivered event SHALL include protocol version, `event_id`, `session_id`, monotonically increasing session `seq`, event type, and payload. `event_id` MUST be deterministic from the session and sequence, and a sequence MUST NOT be overwritten or reused.

#### Scenario: Re-delivering an event after reconnect
- **WHEN** the server replays a previously delivered event during a resume
- **THEN** the event has the same `event_id` and `seq` as its original delivery

### Requirement: Event publication preserves resumable sequences
The service MUST rate-limit streaming publication before assigning event sequences when necessary. It MUST NOT replace an already assigned event with a later sequence in a way that creates an unreported gap in the resumable event log.

#### Scenario: High-frequency text streaming
- **WHEN** assistant text is produced faster than the configured publication interval
- **THEN** the service emits rate-limited snapshot or delta events with valid monotonically resumable sequences

### Requirement: HTTP event polling is removed
The service MUST NOT expose `GET /chat/events`. The plugin MUST NOT contain an event poll timer, event poll interval setting, or an HTTP request path for retrieving live chat events.

#### Scenario: Requesting the removed endpoint
- **WHEN** a client requests `GET /chat/events`
- **THEN** the service does not route it as a chat-event endpoint

### Requirement: User-visible WebSocket events carry transcript patches
For each user-visible chat change, the service SHALL publish an immutable WebSocket event whose payload contains an idempotent transcript patch with the target entry ID, revision, kind, state, ordinal, and typed payload. The client MUST apply a final assistant result only through this patch contract.

#### Scenario: Final assistant response
- **WHEN** the assistant completes a streamed response
- **THEN** the WebSocket sends a patch marking the existing assistant entry complete rather than a separate unkeyed final message

#### Scenario: Visible tool result
- **WHEN** a tool completes with success, rejection, or failure
- **THEN** the WebSocket sends a patch for the corresponding typed transcript entry with its resolved state

#### Scenario: Streaming and completing a Thought
- **WHEN** a user-visible Thought starts, receives content/token updates, and completes
- **THEN** the WebSocket sends revision-increasing patches for one `kind=thought` entry, whose complete patch contains the final content and `duration_seconds`

#### Scenario: Waiting for the original stream before recovering an empty final
- **WHEN** a reasoning token count reaches the configured thinking budget
- **THEN** the WebSocket continues to receive original-stream Thought, assistant, or tool patches until that stream ends; only a completed stream without assistant content or tool calls may be followed by one non-empty recovered assistant completion patch or one typed error patch