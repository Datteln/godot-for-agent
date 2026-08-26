## MODIFIED Requirements

### Requirement: Rendered progress is not advanced before viewport acceptance
The client SHALL advance `projected_seq` only after the Projector has accepted the revision into the canonical Store, and SHALL advance `rendered_seq` and the transport `committed_seq` only after the renderer/viewport has accepted the corresponding entry revision for presentation. A streaming patch held by a projection batcher is neither projected, rendered, nor committed. A renderer rejection, an undrained projection batch, or a mismatch between Store and viewport watermarks MUST preserve the uncommitted event for replay and enter the bounded recovery path with a redacted stage-specific diagnostic.

The client MUST regard the persisted transcript Store and a successfully applied history snapshot as authoritative over any transient socket receipt bookkeeping. It MUST permit an uncommitted replayed event to reach the Projector, and it MUST use Store event-ID and revision idempotency rather than transport receipt history to prevent duplicate visible entries.

For recovery hydration, the client MUST request a complete snapshot and treat its cursor as an atomic delivery cut. It MUST invalidate queued live projection work only for a prior session or hydration generation. Valid current-generation transcript events received while the snapshot is in flight MUST be retained in an ordered buffer; they MUST NOT be discarded merely because the Projector is hydrating. After the snapshot replaces and renders the Store, the client MUST discard buffered events at or below the cut and apply or replay the buffered events above it before creating a replacement subscription after the cut. It MUST NOT render an event at or below that cursor both from live delivery and from the snapshot. This recovery-only tail reveal MUST override a prior manual browsing anchor; ordinary pagination MAY preserve that anchor.

#### Scenario: Streaming Thought remains in a projection batch
- **WHEN** the transport has received a Thought patch but the projection batcher has not applied it to the Store
- **THEN** `received_seq` may advance while `projected_seq`, `rendered_seq`, and `committed_seq` do not, and the client does not report the transcript as healthy

#### Scenario: Replay reaches a previously unrendered entry
- **WHEN** a resumed socket replays an event whose packet was received before but whose transcript entry was never rendered
- **THEN** the Projector receives the event and the viewport can commit it, while an already applied event remains harmless through Store idempotency

#### Scenario: Snapshot and live delivery overlap
- **WHEN** recovery obtains a complete snapshot with `upto_event_seq=80` while events 79 through 82 are available from the socket
- **THEN** the viewport renders events through 80 from the snapshot exactly once and the replacement subscription delivers only events after 80

#### Scenario: Recovery snapshot restores an off-screen latest response
- **WHEN** the user has manually scrolled above the transcript tail, delivery recovery replaces the Store with a complete snapshot containing the missing latest assistant response
- **THEN** the viewport completes its replacement layout and scrolls to the reconciled tail so the restored response is visible without another user action

#### Scenario: Thought continues while history hydration is slow
- **WHEN** the Projector requests authoritative hydration and the server has not yet returned the snapshot while a current-generation Thought stream continues
- **THEN** the client retains the ordered live patches, applies the snapshot cut once it arrives, and presents every post-cut patch without rejecting it as projector-not-ready
