## 1. Protocol and event-publication foundation

- [x] 1.1 Inventory the current `/chat/events` route, in-memory event store, stream coalescing behavior, event consumers, polling settings, and event HTTP request lifecycle.
- [x] 1.2 Define the versioned WebSocket protocol messages for subscribe, event, acknowledgement, heartbeat, protocol error, `history_gap`, and `resync_required`.
- [x] 1.3 Extend the canonical event schema with deterministic `event_id`, task/turn association where available, and immutable sequence semantics.
- [x] 1.4 Replace persisted in-place stream-event coalescing with publication-rate limiting that preserves one valid immutable sequence for every emitted event.
- [x] 1.5 Add backend unit tests for event identity, monotonic sequence assignment, stream rate limiting, and retained-range detection.

## 2. Backend WebSocket subscription service

- [x] 2.1 Add an authenticated WebSocket route dedicated to live chat-event subscriptions.
- [x] 2.2 Implement subscribe validation for protocol version, session identifier, authorization scope, and resume cursor.
- [x] 2.3 Implement retained-event replay in ascending sequence before attaching a subscription to live publication.
- [x] 2.4 Implement per-session live subscription registration and event fan-out with bounded outbound queues.
- [x] 2.5 Implement acknowledgement handling, heartbeat scheduling, typed protocol errors, slow-client `resync_required`, and cleanup on disconnect.
- [x] 2.6 Implement `history_gap` behavior when the requested resume cursor precedes the earliest retained event.
- [x] 2.7 Add backend integration tests for authorized/unauthorized subscribe, initial replay, live delivery, reconnect resume, duplicate replay identity, queue overflow, heartbeat, and retention gaps.

## 3. Godot WebSocket event client

- [x] 3.1 Create a dedicated Godot event-socket client with explicit disconnected, connecting, subscribed, reconnecting, and stopped states.
- [x] 3.2 Implement handshake authentication, subscribe/resume messages, normalized event emission, acknowledgement of the highest contiguous sequence, and heartbeat handling.
- [x] 3.3 Implement reconnect backoff and resume from the locally stored contiguous cursor without re-sending chat commands.
- [x] 3.4 Emit typed connection, protocol-error, `history_gap`, and `resync_required` signals for the chat Timeline adapter.
- [x] 3.5 Add Godot client tests using a controllable socket/protocol fixture for ordered delivery, delayed events, reconnect, duplicate replay, and no-command-replay behavior.

## 4. Transport-level recovery verification

- [x] 4.1 Expose normalized event, connection-state, `history_gap`, and `resync_required` signals from the Godot event-socket client without depending on a Timeline UI consumer.
- [x] 4.2 Implement transport-level resynchronization state: stop live delivery after a gap and require the consumer to hydrate through the normal history HTTP API; do not call an HTTP event endpoint.
- [x] 4.3 Add end-to-end tests with a protocol test receiver for active streamed chat, tool events, reconnect resume, retention gaps, and task commands remaining HTTP-only.

## 5. Remove HTTP event polling

- [x] 5.1 Remove `GET /chat/events` routing, DTOs used only by that endpoint, and backend tests that assert polling behavior.
- [x] 5.2 Remove the front-end event `HTTPRequest`, event timer, `poll_events`, polling cursor plumbing, polling configuration keys, migration entries, and polling-only tests.
- [x] 5.3 Update plugin startup, reset, interrupt, and shutdown paths to manage the WebSocket client without leaving a live subscription behind.
- [x] 5.4 Search the repository for `/chat/events`, event polling settings, and polling code paths; remove or update every remaining reference.
- [x] 5.5 Add a regression test proving live events remain functional while `/chat/events` is absent.

## 6. Acceptance and operational verification

- [x] 6.1 Verify a connected editor receives text, tool progress, approval, error, and final events with no polling requests.
- [x] 6.2 Verify an interrupted network reconnects from the last contiguous cursor without duplicate Timeline items or duplicate command submission.
- [x] 6.3 Verify a retention gap produces explicit recovery state and history hydration rather than a silent event skip.
- [x] 6.4 Verify slow-client queue overflow cannot block unrelated sessions or grow memory without bound.
- [x] 6.5 Document protocol version, authentication requirements, message types, cursor semantics, queue limits, heartbeat expectations, and the intentional removal of the HTTP events endpoint.
