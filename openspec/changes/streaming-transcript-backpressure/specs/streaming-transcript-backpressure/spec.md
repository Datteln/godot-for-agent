## ADDED Requirements

### Requirement: Realtime streaming work is bounded per transcript entry
The service SHALL preserve complete visible entry payloads in the authoritative transcript and history snapshots. For a growing Thought or assistant entry, the realtime transport MUST use a bounded incremental or bounded-preview representation after the entry's initial state, and MUST retain the entry ID, monotonic revision, and sufficient stream metadata for a client to detect an unrecoverable gap.

#### Scenario: Long Thought stream
- **WHEN** a visible Thought grows through many model updates
- **THEN** the persisted completed Thought contains its complete content while the total realtime payload does not repeatedly contain an unbounded cumulative copy for every update

### Requirement: Slow subscribers recover without losing a healthy turn
The service SHALL impose both item-count and byte-size bounds on each live subscription. It MUST coalesce replaceable streaming updates by transcript entry, preserve ordered non-streaming and terminal states, and emit a typed resynchronization outcome when the subscriber cannot receive a valid ordered view within those bounds.

#### Scenario: Subscriber falls behind a long answer
- **WHEN** a client cannot drain realtime text updates before its subscription budget is exceeded
- **THEN** the service stops queuing stale replaceable text updates, reports resynchronization, and continues the active backend turn without silently dropping its terminal transcript state

### Requirement: Active turn recovery precedes automatic cancellation
The service SHALL expose a bounded, redacted active-turn progress signal independent of transport heartbeats. When the client observes request idleness during an active turn, it MUST attempt resume or snapshot recovery before issuing an interruption; it SHALL interrupt only after recovery fails, the service reports no active turn, or the configured hard cap is reached.

#### Scenario: Realtime channel stalls while the model is working
- **WHEN** a model continues producing or the service continues holding an active turn but the client receives no usable realtime patch for its idle interval
- **THEN** the client reconnects or resynchronizes and continues the turn when recovery succeeds rather than sending `/chat/interrupt`

### Requirement: Backpressure diagnostics are redacted and actionable
The service and client SHALL record structured diagnostics for realtime payload size, stream coalescing, subscription budget exhaustion, last received/acknowledged/projected/rendered sequence, recovery attempt, and final timeout decision. Diagnostics MUST NOT include unbounded transcript text, prompts, secrets, or full tool results.

#### Scenario: Request times out after recovery failure
- **WHEN** the client ultimately interrupts a timed-out chat request
- **THEN** diagnostics identify whether the cause was missing service progress, socket delivery failure, resynchronization failure, or the hard-cap expiry

### Requirement: Map region reads expose their cardinality limit
The `describe_map_region` interface SHALL make its maximum readable cell count discoverable to the model. For a request exceeding that limit, it MUST either perform a semantics-preserving bounded partition and identify the partitions in its result, or return a structured `region_too_large` error that includes the applicable limit and a safe smaller-request constraint.

#### Scenario: Requested map rectangle exceeds 400 cells
- **WHEN** a caller requests a two-dimensional map region whose width multiplied by height exceeds 400
- **THEN** the caller receives a bounded partitioned result or a structured error stating the 400-cell limit and how to reduce the request
