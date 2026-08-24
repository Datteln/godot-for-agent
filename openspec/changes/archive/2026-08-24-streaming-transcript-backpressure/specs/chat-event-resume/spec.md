## MODIFIED Requirements

### Requirement: Slow subscriptions are bounded
The service SHALL use a bounded outbound queue for every subscribed connection, with both event-count and serialized-byte limits. It MUST coalesce replaceable, not-yet-sent streaming updates for the same transcript entry, while preserving non-streaming and terminal states in order. If a subscriber cannot keep up after that coalescing, the service MUST report resynchronization and close or reset that subscription instead of silently dropping ordered events or blocking other clients' event delivery.

#### Scenario: Stalled editor client
- **WHEN** one subscribed plugin stops draining its event queue until the configured count or byte bound is exceeded
- **THEN** the service coalesces obsolete same-entry stream updates where possible, then marks that subscription for resynchronization and preserves other clients' event delivery

## ADDED Requirements

### Requirement: Idle request recovery does not cancel a healthy active turn
The client SHALL treat a lack of usable live patches as a recovery condition before treating it as a cancellation condition. It MUST attempt a bounded reconnect and cursor resume, or snapshot hydration after a typed gap, and SHALL continue waiting when the recovered session reports the same active turn. It MUST only issue an automatic interruption after recovery fails, the active turn is absent, or the chat hard cap expires.

#### Scenario: Resume after a temporary event-stream interruption
- **WHEN** an active chat request becomes idle locally and the WebSocket reconnect resumes from the acknowledged cursor
- **THEN** the client keeps the existing chat request active and does not submit `/chat/interrupt`
