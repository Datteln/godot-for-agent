## 1. Measure and define the realtime contract

- [x] 1.1 Add redacted server/client diagnostics for realtime payload bytes, queue item/byte depth, coalesced patches, socket send/receive/ack timings, projection/render timings, recovery attempts, and final timeout reason.
- [x] 1.2 Establish deterministic long-stream fixtures and baseline tests that reproduce cumulative Thought/assistant growth without recording full model text in logs.
- [x] 1.3 Define versioned full-patch, append-delta, and bounded-preview payload shapes; document revision/base-revision validation and feature-compatible fallback to snapshot hydration.

## 2. Bound server-side streaming and subscriber backpressure

- [x] 2.1 Update `TranscriptWriter`/event publication to retain full authoritative entries while emitting bounded realtime representations for growing Thought and assistant content.
- [x] 2.2 Extend `EventStore` subscriptions with byte accounting and entry-keyed latest-wins coalescing for unsent replaceable stream updates; preserve ordered tool, approval, error, and terminal states.
- [x] 2.3 Update the WebSocket sender to issue typed resynchronization promptly when the subscription cannot retain a valid ordered stream, without blocking other subscriptions or cancelling the active turn.
- [x] 2.4 Add server tests for cumulative long streams, terminal-state ordering after coalescing, byte-bound overflow, replay/resume behavior, and redacted diagnostics.

## 3. Batch frontend projection and rendering

- [x] 3.1 Extend `ChatEventSocket`/projection validation to accept bounded stream payloads, enforce base-revision continuity, and request resume or snapshot hydration on a detected gap.
- [x] 3.2 Route replaceable stream patches through an entry-keyed pending set in `ChatPanel`; acknowledge delivery immediately but apply only the newest eligible revision for each entry per projection window.
- [x] 3.3 Update viewport/render scheduling so each projection window performs bounded entry updates and at most one automatic follow-mode scroll while retaining anchors and terminal state ordering.
- [x] 3.4 Add Godot tests for same-frame revision coalescing, terminal-over-stream ordering, disabled-follow anchoring, preview/full rendering, and bounded scroll behavior.

## 4. Recover before cancelling an idle request

- [x] 4.1 Add a small active-turn progress/keepalive event that is distinct from WebSocket transport heartbeat and contains only redacted turn/session progress metadata.
- [x] 4.2 Replace direct `/chat` idle-timeout interruption with a bounded recovery state machine: reconnect from the acknowledged cursor, hydrate after typed gaps, confirm active-turn state, and continue waiting on success.
- [x] 4.3 Preserve the existing hard cap and issue `/chat/interrupt` only after failed recovery, absent/failed active turn, or hard-cap expiry; make the resulting local UI state and diagnostics explicit.
- [x] 4.4 Add end-to-end tests for temporary socket loss during a healthy turn, slow-client resynchronization, genuine stalled backend timeout, and no duplicate command submission during recovery.

## 5. Make map-region limit recovery explicit

- [x] 5.1 Expose the 400-cell `describe_map_region` bound in the tool schema and agent guidance, including a safe width/height constraint in structured errors.
- [x] 5.2 Implement and test semantics-preserving bounded partitioning where map-region requests can safely be combined; retain `region_too_large` for unsupported shapes or dimensions.
- [x] 5.3 Verify a first oversized map read is rendered as a recoverable tool error and a subsequent bounded retry can complete without terminating the parent turn.

## 6. Validation and rollout

- [x] 6.1 Run the long-stream stress fixture against the service and Godot frontend; assert bounded payload/queue/render metrics and final transcript equivalence with history hydration.
- [ ] 6.2 Perform editor acceptance with a long Thought, multi-tool retry, intentional WebSocket delay, and an actual interrupt; confirm that only the explicit/terminal condition stops the turn. _(Requires a live Godot editor + configured LLM; automated headless coverage is in place and a manual checklist is documented in STREAMING_BACKPRESSURE.md.)_
- [x] 6.3 Document configuration defaults, rollout flags, fallback behavior, and operational thresholds derived from the baseline measurements.

## 7. Preserve Thought duration across empty-answer recovery

- [x] 7.1 Thread a unique `response_attempt_id` through each provider invocation, orchestrator delta/end event, transcript patch metadata, and diagnostic record without exposing model text.
- [x] 7.2 Classify an empty-final-answer stream end as provisional when a recovery will start; delay logical Thought terminalization until the recovery outcome is known.
- [x] 7.3 Update `TranscriptWriter` identity and lifecycle handling so recovery deltas cannot append to an already-complete Thought with a stale `duration_seconds`; preserve the original logical `started_at` and an authoritative cumulative token count.
- [x] 7.4 Add service tests for the observed sequence: first reasoning-only attempt lasts 79.33 seconds, recovery continues the same logical Thought, and the only final duration spans the complete approximately 130-second lifecycle.
- [x] 7.5 Add protocol/projection tests proving that provisional ends do not render a terminal Thought, terminal revisions remain monotonic, and delayed patches from the old response attempt cannot overwrite the recovery attempt.
- [x] 7.6 Extend the editor acceptance checklist with an empty-final-answer recovery case and verification that the rendered Thought duration equals the service's final authoritative duration.
