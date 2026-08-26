## Context

The current service has two incompatible timelines. It appends visible transcript entries and WebSocket events in memory during a request, but saves the session JSON only after normal request completion. Stop cancels the request and restores a whole pre-turn in-memory snapshot. That erases an already accepted user entry and already-visible Thought or assistant entries, lets the next request reuse their identities and revisions, and leaves the on-disk cursor old. The EventStore is also process memory, so a service restart exposes the old JSON snapshot.

The recovery path has a second failure. A chat request holds the per-session mutation lock for the whole LLM turn. History reads need that same lock, so recovery cannot read the authoritative snapshot while a model is streaming. The frontend enters HYDRATING immediately and rejects incoming patches until the history request returns. A healthy live stream can therefore look frozen until the user presses Stop.

## Goals / Non-Goals

**Goals:**

- Keep every accepted user message and already-visible transcript entry durable across Stop and service restart.
- Keep transcript entry identities, revisions, ordinals, and cursors monotonic across cancellation and the next turn.
- Let recovery obtain an internally consistent history snapshot while an LLM turn continues.
- Keep current-generation live transcript delivery safe while hydration is in flight.
- Preserve existing receipt-versus-presentation commit semantics.

**Non-Goals:**

- Change model routing, map authoring, or tool approval policy.
- Continue a model response after the user stopped it.
- Make backend processing depend on a frontend render acknowledgement.

## Decisions

### 1. Durable transcript state is not rollbackable turn state

When the service accepts a user message, it allocates a stable turn identity, appends the user transcript entry, and waits for the matching durable checkpoint before model streaming begins. This is a short critical boundary, not a write for every model token.

High-frequency Thought and assistant updates enter a per-session asynchronous durability writer. The writer coalesces the latest state of each streaming entry for a bounded interval, performs file work off the request event loop, and serializes one checkpoint containing the matching transcript state and cursor. Only after that checkpoint succeeds may the corresponding coalesced visible event be published. Stop, normal completion, and controlled shutdown enqueue the terminal state then await the writer flush before returning or exiting.

The cancellation rollback must preserve only the mutable execution fragments that would make the next model call invalid, such as an incomplete front-tool-call exchange. It must not restore the entire pre-turn session. This prevents deletion of visible history and prevents a later request from recreating old entry IDs or revisions.

Alternative rejected: full pre-turn rollback. It protects the tool protocol but deletes records the user already saw.

### 2. Give visible entries one stable user-turn identity

The service creates one stable turn identity when it accepts a user message. The user entry and every Thought, assistant, tool, approval, and progress entry caused by that turn carry the same identity. Entry IDs, ordinals, revisions, and event sequences are allocated monotonically and never reused after Stop.

The frontend uses this identity only to isolate live control effects and maintain user-to-response association. Ordered durable history remains authoritative.

### 3. History reads use a short atomic snapshot, not the full LLM lock

Mutable agent execution stays serialized, but history reads must not wait for provider streaming, tool work, or retries. The service exposes a versioned immutable transcript snapshot and its matching cursor under a short critical section, then serializes the response outside the LLM execution lock. The response is an atomic cut: it never advertises a cursor beyond the durable entries included in it.

Alternative rejected: sharing the full-turn agent lock. Recovery would be unavailable precisely while live output needs it.

### 4. Replay still begins from the presentation commit cursor

The resume cursor remains the highest contiguous event whose visible consequence the Store and viewport accepted. During reconnect, every event above that cursor is eligible for delivery even when an earlier connection received it. Store event identity and revision rules prevent duplicate rendering.

### 5. Hydration fences stale work but buffers live work

The complete history snapshot cursor is an atomic cut. While waiting for it, the client drops stale session or stale-generation work but buffers valid current-generation transcript envelopes in order; it does not send them to a Projector that is deliberately not ready.

After the snapshot replaces the Store, the client discards buffered events at or below the cut and applies or replays buffered events above it. If that range cannot be applied, it reconnects from the snapshot cursor. It then resubscribes after the cut and forces the existing recovery tail scroll.

Alternative rejected: rejecting every live patch during HYDRATING. A slow snapshot then becomes a visible black hole.

### 6. Preserve recovery state across Stop

Stop remains a turn-control boundary, not a transcript-delivery reset. The client retains uncommitted delivery state, replay baseline, and escalation budget. A later user turn can proceed normally while durable history is reconciled.

## Risks / Trade-offs

- Frequent streamed updates increase writes. Mitigation: a per-session asynchronous writer coalesces updates for a bounded interval and performs file work away from the request event loop; it flushes before publish, Stop, normal completion, and controlled shutdown so a visible entry is never only in memory.
- A history reader could observe torn mutable state. Mitigation: read a versioned immutable snapshot with its matching cursor.
- Buffered patches can overlap a snapshot. Mitigation: retain only events above the snapshot cut and rely on entry identity and revision idempotency.
- Stop can race old controls. Mitigation: use the durable turn identity for control isolation without deleting transcript history.

## Migration Plan

1. Add backend tests for durable accepted-user entries, interrupted visible output, monotonic identities/cursors, and history reads during an active turn.
2. Split rollbackable execution state from durable transcript state and add a per-session asynchronous durability writer with bounded stream coalescing and explicit flush boundaries.
3. Publish visible stream events only after their coalesced durable checkpoint, and keep history snapshots lock-independent.
4. Update frontend hydration to buffer valid live patches and merge post-cut patches after snapshot application.
5. Run backend and frontend suites plus Stop, Send, recovery, restart smoke tests.
6. Keep session JSON backward-readable; roll back only while new durable fields retain safe defaults.

## Open Questions

- What bounded coalescing interval is acceptable for streamed Thought durability without unnecessary JSON writes?
- Should the UI display a temporary syncing notice while a history snapshot is being applied?
