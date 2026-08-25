## ADDED Requirements

### Requirement: Subscription discontinuities are explicit and diagnosable
The service SHALL NOT silently discard an ordered user-visible transcript event for one subscriber while leaving that subscriber apparently healthy. If a subscriber's retained replay or outbound delivery cannot preserve a contiguous sequence, the service MUST send a typed `resync_required` or `history_gap` response containing the session ID and applicable cursor information before resetting or closing that subscription. The client MUST record a redacted diagnostic containing session ID, expected/received sequence, event ID when available, and recovery reason; it MUST NOT record transcript payload text.

#### Scenario: Subscriber queue cannot retain a later tool approval
- **WHEN** a subscriber falls behind and its bounded outbound queue cannot preserve a pending approval event after earlier visible events
- **THEN** the service marks the subscription for resynchronization instead of silently omitting the approval event

#### Scenario: Client receives an unexpected sequence
- **WHEN** the client receives a visible event whose sequence is greater than the next expected contiguous sequence
- **THEN** it records a sequence-gap diagnostic and starts the resume path without acknowledging that later sequence as contiguous

### Requirement: Visible events are acknowledged only after presentation commit
The client SHALL maintain separate contiguous `received_seq` and `committed_seq` cursors for each session subscription. It MAY advance `received_seq` after validating ordered packet receipt, but it MUST NOT use that cursor for an ACK or `after_seq` resume request. The client SHALL advance `committed_seq` and ACK only after the corresponding visible event revision has been accepted by the canonical transcript Store and the renderer/viewport has accepted it for presentation. The client MUST subscribe from `committed_seq` after reconnecting.

If a decoded event is held by a projection batch, rejected by the Projector, rejected by the renderer, or invalidated by the active generation before commit, the client MUST NOT ACK it as committed. It SHALL retain the ordered uncommitted event where possible or reconnect from the prior committed cursor so the service can replay it. Diagnostics MUST distinguish received and committed cursors without recording payload text.

#### Scenario: Projector fails after packet receipt
- **WHEN** the socket receives and validates visible event sequence 42 but the Projector rejects its entry revision before the viewport accepts it
- **THEN** `received_seq` MAY be 42, `committed_seq` remains 41, no ACK greater than 41 is sent, and a reconnect subscribes with `after_seq=41`

#### Scenario: Streaming patch awaits its projection window
- **WHEN** a streamed Thought patch is ordered and held by the projection batcher
- **THEN** it is not acknowledged as committed until the batch has applied it and the viewport has accepted the resulting revision

### Requirement: Active session exposes visible progress for stall detection
For an active subscribed session, the service SHALL make the latest persisted visible event sequence and its update time available to the client through the existing subscription-compatible protocol. These progress fields MUST contain only identifiers, counters, and timestamps, not message, Thought, or tool payload content.

#### Scenario: Thought persists while no patch reaches the client
- **WHEN** the service persists a later visible Thought revision while the client's acknowledged visible sequence remains unchanged
- **THEN** subsequent subscription-compatible progress reports expose a higher visible sequence that permits the client to detect the stall

### Requirement: Realtime ClassDB results are bounded and safe
`read_class_docs` MUST emit only the bounded result of an explicit `overview`, `search`, `members`, or `constants` query and MUST NOT send a complete ClassDB/API document through WebSocket. The service SHALL NOT impose a new common realtime byte ceiling on other tool-result patches as part of this change.

#### Scenario: Exact TileMap member query
- **WHEN** the model requests the `set_cell` and `clear_layer` members of legacy `TileMap`
- **THEN** the WebSocket tool activity contains only the ceiling-compliant visible metadata and no full class member, property, signal, or constant enumeration

### Requirement: Search results and terminal tool patches are bounded before transport
The service SHALL normalize search-like tool results before they are appended to model messages, persisted, or published. `grep_code` MUST exclude runtime log, cache, service-state, and other configured non-source directories before scanning; each returned match MUST contain only a bounded excerpt with path, line number, and truncation metadata. It MUST NOT return an unbounded full source line. This requirement does not impose one common 4 KiB output ceiling on all tools.

Before WebSocket fan-out, the service SHALL enforce a serialized-byte budget for every non-streaming terminal `transcript_patch`, including resolved and failed tool activity. A patch that cannot fit after its tool-specific normalization MUST be replaced with a payload-free safe summary or cause a typed `resync_required`; its raw payload MUST NOT be sent. The client SHALL reject an inbound packet exceeding its compatible packet-size threshold before `JSON.parse_string`, record a redacted size-only diagnostic, and enter bounded resume/snapshot recovery.

#### Scenario: Broad Grep would match a service log
- **WHEN** `grep_code` is requested with `include="**/*"` and the project contains `logs/service.log`
- **THEN** the scan excludes that log path and returns only eligible source/config matches, without placing any service-log line in the LLM message, transcript, or WebSocket packet

#### Scenario: A malformed compatibility result remains oversized
- **WHEN** a terminal tool result bypasses normal search normalization and would exceed the terminal patch byte budget
- **THEN** the subscriber receives a typed safe-summary or `resync_required`, and Godot does not invoke JSON parsing on the oversized raw packet
