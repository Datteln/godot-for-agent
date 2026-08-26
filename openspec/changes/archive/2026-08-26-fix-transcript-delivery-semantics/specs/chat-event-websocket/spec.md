## MODIFIED Requirements

### Requirement: Visible events are acknowledged only after presentation commit
The client SHALL maintain separate contiguous `received_seq` and `committed_seq` cursors for each session subscription. It MAY advance `received_seq` after validating ordered packet receipt, but it MUST NOT use that cursor for an ACK or `after_seq` resume request. The client SHALL advance `committed_seq` and ACK only after the corresponding visible event revision has been accepted by the canonical transcript Store and the renderer/viewport has accepted it for presentation. The client MUST subscribe from `committed_seq` after reconnecting.

Packet receipt and presentation commitment are distinct. During a reconnect or replay, every event with `seq > committed_seq` MUST be eligible for delivery even if an earlier socket connection recorded receipt of its `event_id`. Transport-level duplicate suppression MUST be scoped to one connection or replay epoch so it cannot suppress an uncommitted replayed event. Store-level event ID and revision idempotency SHALL prevent duplicate rendered transcript entries.

If a decoded event is held by a projection batch, rejected by the Projector, rejected by the renderer, or invalidated by the active generation before commit, the client MUST NOT ACK it as committed. It SHALL retain the ordered uncommitted event where possible or reconnect from the prior committed cursor so the service can replay it. Diagnostics MUST distinguish received and committed cursors without recording payload text.

The server MUST publish only from a durable transcript timeline. An event sequence advertised through WebSocket or a history snapshot MUST remain reconstructable after service restart; a process-local replay buffer MAY accelerate delivery but MUST NOT be the only copy of a user-visible event or cursor.

#### Scenario: Projector fails after packet receipt
- **WHEN** the socket receives and validates visible event sequence 42 but the Projector rejects its entry revision before the viewport accepts it
- **THEN** `received_seq` MAY be 42, `committed_seq` remains 41, no ACK greater than 41 is sent, and a reconnect subscribes with `after_seq=41`

#### Scenario: Replayed uncommitted event was received on the prior socket
- **WHEN** a prior socket received sequence 42 but it was never committed, and a new socket resumes from `after_seq=41`
- **THEN** the client re-delivers sequence 42 into the projection path instead of discarding it solely because its `event_id` was received before

#### Scenario: Streaming patch awaits its projection window
- **WHEN** a streamed Thought patch is ordered and held by the projection batcher
- **THEN** it is not acknowledged as committed until the batch has applied it and the viewport has accepted the resulting revision

#### Scenario: Service restarts after publishing a visible event
- **WHEN** the server publishes a visible user or transcript event and then restarts before a model turn normally completes
- **THEN** history reconstruction includes the corresponding durable entry and does not reuse or regress its advertised cursor
