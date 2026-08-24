## 1. Measure and define the realtime contract

- [ ] 1.1 Add redacted server/client diagnostics for realtime payload bytes, queue item/byte depth, coalesced patches, socket send/receive/ack timings, projection/render timings, recovery attempts, and final timeout reason.
- [ ] 1.2 Establish deterministic long-stream fixtures and baseline tests that reproduce cumulative Thought/assistant growth without recording full model text in logs.
- [ ] 1.3 Define versioned full-patch, append-delta, and bounded-preview payload shapes; document revision/base-revision validation and feature-compatible fallback to snapshot hydration.

## 2. Bound server-side streaming and subscriber backpressure

- [ ] 2.1 Update `TranscriptWriter`/event publication to retain full authoritative entries while emitting bounded realtime representations for growing Thought and assistant content.
- [ ] 2.2 Extend `EventStore` subscriptions with byte accounting and entry-keyed latest-wins coalescing for unsent replaceable stream updates; preserve ordered tool, approval, error, and terminal states.
- [ ] 2.3 Update the WebSocket sender to issue typed resynchronization promptly when the subscription cannot retain a valid ordered stream, without blocking other subscriptions or cancelling the active turn.
- [ ] 2.4 Add server tests for cumulative long streams, terminal-state ordering after coalescing, byte-bound overflow, replay/resume behavior, and redacted diagnostics.

## 3. Batch frontend projection and rendering

- [ ] 3.1 Extend `ChatEventSocket`/projection validation to accept bounded stream payloads, enforce base-revision continuity, and request resume or snapshot hydration on a detected gap.
- [ ] 3.2 Route replaceable stream patches through an entry-keyed pending set in `ChatPanel`; acknowledge delivery immediately but apply only the newest eligible revision for each entry per projection window.
- [ ] 3.3 Update viewport/render scheduling so each projection window performs bounded entry updates and at most one automatic follow-mode scroll while retaining anchors and terminal state ordering.
- [ ] 3.4 Add Godot tests for same-frame revision coalescing, terminal-over-stream ordering, disabled-follow anchoring, preview/full rendering, and bounded scroll behavior.

## 4. Recover before cancelling an idle request

- [ ] 4.1 Add a small active-turn progress/keepalive event that is distinct from WebSocket transport heartbeat and contains only redacted turn/session progress metadata.
- [ ] 4.2 Replace direct `/chat` idle-timeout interruption with a bounded recovery state machine: reconnect from the acknowledged cursor, hydrate after typed gaps, confirm active-turn state, and continue waiting on success.
- [ ] 4.3 Preserve the existing hard cap and issue `/chat/interrupt` only after failed recovery, absent/failed active turn, or hard-cap expiry; make the resulting local UI state and diagnostics explicit.
- [ ] 4.4 Add end-to-end tests for temporary socket loss during a healthy turn, slow-client resynchronization, genuine stalled backend timeout, and no duplicate command submission during recovery.

## 5. Make map-region limit recovery explicit

- [ ] 5.1 Expose the 400-cell `describe_map_region` bound in the tool schema and agent guidance, including a safe width/height constraint in structured errors.
- [ ] 5.2 Implement and test semantics-preserving bounded partitioning where map-region requests can safely be combined; retain `region_too_large` for unsupported shapes or dimensions.
- [ ] 5.3 Verify a first oversized map read is rendered as a recoverable tool error and a subsequent bounded retry can complete without terminating the parent turn.

## 6. Validation and rollout

- [ ] 6.1 Run the long-stream stress fixture against the service and Godot frontend; assert bounded payload/queue/render metrics and final transcript equivalence with history hydration.
- [ ] 6.2 Perform editor acceptance with a long Thought, multi-tool retry, intentional WebSocket delay, and an actual interrupt; confirm that only the explicit/terminal condition stops the turn.
- [ ] 6.3 Document configuration defaults, rollout flags, fallback behavior, and operational thresholds derived from the baseline measurements.
