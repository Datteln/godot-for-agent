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