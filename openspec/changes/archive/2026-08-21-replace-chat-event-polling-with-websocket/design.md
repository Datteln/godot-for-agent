## Context

The active baseline delivers live chat events by repeatedly calling `GET /chat/events` with a session identifier and cursor. The front end owns a timer and a second `HTTPRequest`, while the backend keeps a bounded in-memory event list. This results in polling latency and makes live Timeline behavior depend on repeated request/response cycles.

This change replaces that route entirely with a WebSocket subscription dedicated to server-to-client chat and task events. HTTP continues to own commands and history retrieval. It exposes normalized event and recovery signals for a later Timeline integration but does not require the Timeline UI to complete.

## Goals / Non-Goals

**Goals:**

- Deliver live session events over one authenticated WebSocket transport.
- Define stable event identity and a monotonic resume cursor per session.
- Resume a reconnecting client from its last contiguous event cursor without replaying commands.
- Provide explicit resynchronization when the retained event history cannot satisfy a resume request.
- Bound slow-client memory and preserve a valid resume path under backpressure.
- Remove the HTTP event polling endpoint and all polling client behavior.

**Non-Goals:**

- Move `/chat`, approval, interrupt, reset, or history commands to WebSocket.
- Make event history durable across service restarts.
- Redesign Timeline rendering, message formatting, or virtual scrolling.
- Support cross-project or unauthenticated remote event subscriptions.
- Maintain an HTTP events fallback after migration.

## Decisions

### 1. WebSocket is event-only; HTTP remains command-oriented

The service exposes one WebSocket endpoint for live session events. The plugin connects, authenticates, and sends a subscribe/resume message. User messages, confirmation actions, interruption, reset, and history requests remain HTTP calls.

This keeps command idempotency and request/response errors independent from long-lived transport state.

Alternative considered: move all chat traffic to bidirectional WebSocket RPC. Rejected because it expands the change into command semantics, cancellation, and idempotency redesign that Timeline does not require.

### 2. Subscribe/resume begins every connection

The first application message after a successful WebSocket handshake is `subscribe` with protocol version, session ID, and `after_seq`, the highest contiguous event sequence accepted by the client. The server validates authorization before replaying retained events in ascending sequence and then attaches the connection to the live session publisher.

Alternative considered: session ID in the WebSocket URL only. Rejected because a protocol message makes versioning, resume, errors, and later session switching explicit.

### 3. Event identity derives from an immutable session sequence

Every persisted transport event has a session-local monotonic `seq`, an `event_id` derived as `session_id:seq`, a type, task/turn association when known, and payload. A sequence is never overwritten or reused.

The existing store's in-place stream-event coalescing cannot remain at the persisted-event layer because replacement creates cursor holes. Stream-rate limiting occurs before event publication; every emitted event is retained as a distinct sequence until retention pruning.

Alternative considered: coalesce stored stream events in place and allow clients to skip sequence values. Rejected because it makes resume and duplicate suppression ambiguous.

### 4. Slow clients receive an explicit resynchronization requirement

Each active subscription has a bounded outbound queue. When a client cannot keep up, the server sends `resync_required` when possible and closes that subscription. The client then reconnects and resumes; if the retained sequence range has been pruned, it uses the existing history endpoint to hydrate its Timeline.

Alternative considered: unbounded per-client queues. Rejected because one stalled Godot editor could exhaust service memory.

### 5. Heartbeats distinguish transport health from task progress

Both endpoints exchange protocol heartbeat messages while idle. A heartbeat confirms connection liveness only; it does not extend, complete, cancel, or otherwise alter a chat task.

Alternative considered: infer connection health from absence of chat events. Rejected because silent tasks and broken connections are indistinguishable.

### 6. No HTTP event-polling compatibility path

`GET /chat/events`, the front-end event timer, `event_poll_interval_sec`, event `HTTPRequest`, and polling switches are removed in the same change. The only recovery path for event delivery is WebSocket resume followed by normal history hydration for a reported retention gap.

Alternative considered: retain polling as a fallback. Rejected by product decision; dual transports would create divergent ordering and duplicate-event behavior.

### 7. Authentication and session authorization match existing local-service policy

The WebSocket handshake and subscribe message use the same configured authentication policy as HTTP. A connection may subscribe only to an authorized session/project. Authentication errors use typed protocol errors and never disclose another session's state.

## Risks / Trade-offs

- [Godot WebSocket lifecycle differs from HTTPRequest] → Isolate it in a dedicated event-socket client with explicit connect, subscribe, poll, reconnect, and shutdown states.
- [In-memory retention is insufficient after long disconnects] → Send a typed retention-gap response and hydrate through the existing history API; do not silently skip events.
- [Fast text streams overwhelm the client] → Rate-limit before publication, use bounded queues, and require resync rather than dropping events invisibly.
- [Endpoint removal breaks older plugins] → Version the WebSocket protocol, update plugin configuration in the same rollout, and intentionally reject old polling clients rather than preserve a fallback.
- [Reconnect causes duplicate visible output] → Event identity is deterministic and the Timeline intake de-duplicates by `event_id`/sequence.
- [Authentication differs between HTTP and WebSocket] → Share one authentication helper and cover both accepted and rejected connection tests.

## Migration Plan

1. Define the protocol envelope, handshake/subscribe messages, and backend event-publication invariants.
2. Add server WebSocket endpoint and session publisher while leaving the old polling route temporarily unused only during development.
3. Add a Godot WebSocket event-socket client that emits normalized events and reconnect state, without changing command HTTP behavior.
4. Integrate the Timeline intake adapter with the WebSocket client and verify ordered live events.
5. Enable resume, heartbeat, slow-client resync, and retention-gap hydration.
6. Remove the polling route, timer, configuration, event HTTP request, and tests that assert polling behavior.
7. Verify no source path calls or documents `GET /chat/events`.

Rollback: revert the entire change before rollout. There is deliberately no runtime fallback to HTTP polling after the change is accepted.

## Open Questions

- What retention duration/count is sufficient before a reconnect must hydrate full history?
- What server-side per-session and per-connection queue limits fit expected Godot editor throughput?
- Does the configured HTTP authentication mechanism support WebSocket handshake headers directly, or is a short-lived subscription token required?
- Should a single session allow multiple simultaneous editor subscribers, and if so, what acknowledgement semantics are required?
