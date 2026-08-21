## Why

The current front end polls `GET /chat/events?session_id=…&after=…` on a timer to receive streamed text, tool progress, approvals, and task results. Polling introduces visible latency, unnecessary recurring HTTP requests, and an unreliable foundation for a complete live Timeline.

The event channel must become a resumable WebSocket subscription. The polling endpoint will be removed rather than retained as a fallback, so there is one authoritative live-event transport.

## What Changes

- Add an authenticated WebSocket event endpoint that subscribes a client to one session and streams canonical chat events.
- Define a versioned WebSocket protocol for connection, resume-from-sequence, event delivery, acknowledgement, heartbeat, transport errors, and unrecoverable history gaps.
- Replace the Godot HTTP event timer and event `HTTPRequest` with a WebSocket client that reconnects without resubmitting user commands.
- **BREAKING** Remove `GET /chat/events` and all front-end polling configuration, timers, and fallback behavior.
- Preserve HTTP for command submission, cancellation, confirmation, reset, history retrieval, and other request/response operations; only server-to-client live events move to WebSocket.
- Add bounded event delivery and stream coalescing semantics that maintain a valid client resume cursor.
- Add backend and Godot-plugin tests for ordered delivery, reconnect resume, duplicate suppression, retention gaps, heartbeat, and endpoint removal.

## Capabilities

### New Capabilities

- `chat-event-websocket`: A single authenticated, resumable WebSocket transport for all live chat and task events.
- `chat-event-resume`: Sequence-based resume, acknowledgement, gap reporting, and non-executing reconnect behavior.

### Modified Capabilities

None. The active baseline has no OpenSpec capability for chat event transport.

## Impact

- Backend changes affect API routing, event publication/storage, session lifecycle, configuration, and transport tests.
- Front-end changes affect `agent_http_client.gd`, configuration migrations, plugin lifecycle, and the Timeline intake adapter.
- `GET /chat/events` is removed with no compatibility endpoint; clients must upgrade to the WebSocket protocol.
- The Timeline change depends on this change's canonical event envelope and WebSocket intake being available before it integrates live transport.
