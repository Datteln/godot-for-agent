## MODIFIED Requirements

### Requirement: Event publication preserves resumable sequences
The service MUST rate-limit streaming publication before assigning event sequences when necessary. It MUST NOT replace an already assigned event with a later sequence in a way that creates an unreported gap in the resumable event log. For replaceable growing Thought and assistant content, the service SHALL publish a bounded incremental or bounded-preview patch and MAY coalesce an unassigned older patch for the same entry; non-streaming state transitions and terminal entry states MUST remain ordered and replayable.

#### Scenario: High-frequency text streaming
- **WHEN** assistant text is produced faster than the configured publication interval
- **THEN** the service emits rate-limited bounded incremental or preview events with valid monotonically resumable sequences

#### Scenario: Terminal state follows coalesced stream updates
- **WHEN** a subscriber has not yet received several replaceable patches for one growing assistant entry and that entry completes
- **THEN** the service delivers a replayable terminal patch after any required latest stream state without replacing the terminal state with a later streaming update

#### Scenario: Provisional model-stream end before empty-answer recovery
- **WHEN** an underlying model stream ends without assistant text and its orchestrator begins a recovery stream for the same logical Thought
- **THEN** the service does not publish a terminal transcript patch for the provisional stream end, and subsequent patches identify the recovery response attempt without violating entry revision order

## ADDED Requirements

### Requirement: Realtime transcript payloads remain reconstructable and bounded
For a growing visible transcript entry, the WebSocket payload SHALL identify whether it is a full patch, an append delta, or a bounded preview. The client MUST reconstruct ordered updates when all required patches are present and MUST request the existing resume or snapshot path when it detects a representation or revision gap.

#### Scenario: Client receives a text append delta
- **WHEN** the client receives an append delta whose base revision matches its accepted entry revision
- **THEN** it applies the delta as the next revision without requiring the service to resend the complete accumulated body
