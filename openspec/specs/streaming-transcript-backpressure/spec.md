# Streaming Transcript Backpressure Spec

## Purpose

Bounded realtime transcript delivery and recovery: per-entry streaming work is bounded, live subscriptions carry count and byte budgets with coalescing, active turns are recovered before they are cancelled, diagnostics are redacted, and recoverable model attempts preserve one logical Thought lifecycle.

## Requirements

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

### Requirement: Recoverable model attempts preserve one logical Thought lifecycle
For a user-visible Thought that spans an empty-final-answer recovery, the service SHALL assign a unique response-attempt identity to every underlying model stream while retaining one logical Thought identity. It MUST NOT publish or persist a completed Thought merely because an intermediate attempt has ended when the orchestrator will immediately recover that attempt. The final persisted `duration_seconds` MUST span from the logical Thought's first visible Thinking update through its final logical terminal outcome, and no late patch from an earlier attempt may change that terminal entry.

#### Scenario: Empty final answer starts a recovery attempt
- **WHEN** a model attempt produces visible reasoning but ends without assistant text and the orchestrator starts its no-thinking recovery
- **THEN** the existing Thought remains non-terminal, the recovery deltas are associated with a new response-attempt identity, and exactly one final Thought patch contains the duration measured from the first Thinking update to the recovery outcome

#### Scenario: Older attempt emits a delayed patch after recovery begins
- **WHEN** a delayed delta or terminal notification from an earlier response attempt arrives after a recovery attempt is active
- **THEN** it does not overwrite the active recovery content, token count, state, or final duration

### Requirement: Map region reads expose their cardinality limit
The `describe_map_region` interface SHALL make its maximum readable cell count discoverable to the model. For a request exceeding that limit, it MUST either perform a semantics-preserving bounded partition and identify the partitions in its result, or return a structured `region_too_large` error that includes the applicable limit and a safe smaller-request constraint.

#### Scenario: Requested map rectangle exceeds 400 cells
- **WHEN** a caller requests a two-dimensional map region whose width multiplied by height exceeds 400
- **THEN** the caller receives a bounded partitioned result or a structured error stating the 400-cell limit and how to reduce the request