## MODIFIED Requirements

### Requirement: Event delivery applies bounded backpressure
The WebSocket event channel MUST send ordered bounded batches, limit unacknowledged event count and bytes per connection, and use cumulative client acknowledgements to advance delivery. Authoritative events MUST remain in the event store rather than depending on the socket queue.

#### Scenario: More events exist than one batch limit
- **WHEN** the number or size of events after the accepted cursor exceeds one WebSocket batch limit
- **THEN** the server sends an ordered prefix with its ending sequence and retains the remainder for later batches

#### Scenario: Client acknowledges a batch
- **WHEN** the client has accepted an ordered batch into its frame-budgeted UI queue
- **THEN** it sends a cumulative acknowledgement and the server may release that connection's acknowledged buffer and send the next batch

#### Scenario: Client reaches the unacknowledged bound
- **WHEN** unacknowledged count or bytes reaches the configured maximum
- **THEN** the server pauses delivery and, if the stall exceeds its bound, closes with a typed retryable reason that resumes from the last acknowledgement

### Requirement: Event rendering is frame-budgeted
The chat UI MUST process accepted WebSocket events across frames within a configured item or elapsed-time budget while preserving accepted sequence order.

#### Scenario: One batch exceeds the render budget
- **WHEN** an accepted WebSocket batch contains enough events to exceed the per-frame render budget
- **THEN** the UI renders only the allowed prefix in the current frame and continues the remaining ordered events in later frames without blocking editor interaction

#### Scenario: Snapshot deltas can be coalesced
- **WHEN** multiple replaceable snapshot deltas address the same Frame and message before rendering
- **THEN** the UI may retain only the newest snapshot but MUST NOT drop append-only delta fragments or lifecycle boundaries

## ADDED Requirements

### Requirement: Live and historical content share one canonical Timeline projection
Accepted WebSocket chat events and canonical history event pages MUST enter the same pure `ChatTimelineProjector` and produce the same `ChatTimelineItem` structure and stable identities for equivalent content. Every item MUST carry a stable `item_id`, Session epoch, deterministic order key, closed kind and role, typed content blocks, lifecycle, status, copy text, style token, and applicable Frame, message, tool, artifact, and preview source identities. Rendered text MUST NOT be used as identity.

#### Scenario: Live stream is restored from history
- **WHEN** an assistant or reasoning item first arrives through WebSocket and is later loaded from a history page
- **THEN** both inputs project to the same item identity, content-block schema, lifecycle, copy text, style token, and renderer selection

#### Scenario: History page is prepended
- **WHEN** an older canonical event page is projected and prepended before existing Timeline items
- **THEN** existing item identities and content remain unchanged, duplicate items are not inserted, and the VirtualScroller preserves the visible scroll anchor

#### Scenario: Backend history is requested
- **WHEN** the frontend loads canonical history for a Session epoch
- **THEN** the backend returns canonical event records and does not generate `_history_log_text`, `_history_thought`, `_history_code`, or `_history_front_tool_result`

### Requirement: Streaming and preview lifecycles mutate stable Timeline items
Text and reasoning deltas MUST patch their existing stable Timeline items. A final event MUST finalize the matching assistant item rather than append or deduplicate by text. Preview commit or discard MUST update only the matching preview-backed item. Reasoning and body ordering MUST derive from Timeline order keys, not UI-node creation timing.

#### Scenario: Stream reaches final
- **WHEN** one assistant item receives one or more deltas followed by its final event
- **THEN** the projector emits patches followed by finalize for the same `item_id`, and exactly one assistant item remains visible

#### Scenario: Matching preview is discarded
- **WHEN** a discard boundary names one provisional preview identity
- **THEN** the store applies discard only to the item carrying that identity and leaves unrelated provisional or committed items unchanged

#### Scenario: Reasoning and body interleave
- **WHEN** reasoning and assistant-body events arrive in one or more accepted batches
- **THEN** their canonical order keys produce the same relative order in live rendering and history restoration

### Requirement: Tool previews and all visible nodes use the renderer registry
Tool calls, results, diffs, reasoning disclosures, Markdown, system messages, errors, and finals MUST store serializable content blocks, render descriptors, or artifact references. TimelineStore MUST NOT contain a prebuilt Godot `Control`, and no visible node may bypass `ChatItemRendererRegistry`. Markdown, truncation, copy text, theme, indentation, lifecycle status, and status-color policy MUST be shared by live and historical rendering.

#### Scenario: Tool diff appears live and in history
- **WHEN** the same tool result is rendered first from a WebSocket event and later from canonical history
- **THEN** both use the same descriptor, renderer, structure, copy text, theme policy, and visible diff semantics without reusing a prebuilt node

#### Scenario: Unknown mutation is received
- **WHEN** projection produces an unknown mutation kind, invalid lifecycle transition, ambiguous item identity, or epoch mismatch
- **THEN** validation fails closed before TimelineStore or any UI node changes, and unvalidated content is not rendered

#### Scenario: ChatPanel attempts direct insertion
- **WHEN** release architecture checks inspect ChatPanel and VirtualScroller integration
- **THEN** ChatPanel has no direct append or external-node insertion path and VirtualScroller subscribes only to TimelineStore mutations

### Requirement: Accepted WebSocket events are the sole live presentation authority
HTTP chat and tool-result responses MUST be command acknowledgements only and MUST NOT create, patch, finalize, discard, or deduplicate a live Timeline item. Live `tool_calls`, `final`, `error`, text, reasoning, preview, system, and tool-result presentation MUST originate from accepted WebSocket events and enter ChatTimelineProjector exactly once. Bounded history or snapshot recovery MAY hydrate canonical events through the same projector but MUST NOT establish a second live response-rendering path.

#### Scenario: HTTP acknowledgement arrives before its terminal event
- **WHEN** a chat command returns `tool_calls`, `final`, or `error` acknowledgement before the matching accepted WebSocket event
- **THEN** no live item is rendered from the HTTP body and the later event creates or finalizes the canonical item once

#### Scenario: HTTP acknowledgement arrives after its terminal event
- **WHEN** the matching WebSocket event has already updated the Timeline before the HTTP command finishes
- **THEN** the acknowledgement does not append, patch, fingerprint-deduplicate, or otherwise change the Timeline

#### Scenario: Snapshot recovery restores visible content
- **WHEN** a typed `snapshot_required` disposition triggers bounded HTTP recovery
- **THEN** recovered canonical events enter the same projector and stable identities, while continuous HTTP polling and direct snapshot rendering remain absent

### Requirement: Chat events use only an authenticated resumable WebSocket
Chat event delivery MUST use a WebSocket authenticated by the bearer token in handshake headers. After connection, the client MUST bind the stream with `session_id`, `session_epoch`, and `after_seq`; credentials MUST NOT appear in URLs or logs. No HTTP event-polling route or transport fallback may exist.

#### Scenario: Valid client resumes
- **WHEN** an authenticated client sends a resume message for the current epoch and accepted cursor
- **THEN** the server returns protocol and backpressure bounds and begins ordered delivery after that cursor

#### Scenario: Authentication fails
- **WHEN** the handshake omits or supplies an invalid bearer token
- **THEN** the server rejects the connection without exposing Session existence or event data

#### Scenario: Cursor is duplicated on reconnect
- **WHEN** the client reconnects from its last acknowledgement and receives an already accepted event
- **THEN** sequence deduplication prevents duplicate rendering while later events remain ordered

### Requirement: WebSocket reconnect never replays chat submission
WebSocket loss MUST trigger bounded reconnect with jitter and cursor resume. The frontend MUST NOT replay `/chat`, resubmit tool results, choose another model, clear committed state, or switch to polling because event transport disconnected.

#### Scenario: Socket disconnects during an active turn
- **WHEN** `/chat` execution continues but the event WebSocket closes
- **THEN** the client reconnects from the last acknowledgement and the backend task continues under its existing request and turn identities

#### Scenario: Resume history is unavailable
- **WHEN** the requested cursor predates retained events or a sequence gap is detected
- **THEN** the server returns a typed snapshot-required disposition and the client performs bounded authoritative HTTP snapshot recovery without enabling polling

### Requirement: Epoch reset is a WebSocket synchronization boundary
Session reset MUST invalidate the old-epoch WebSocket stream. The client MUST adopt the reset acknowledgement before connecting with the new epoch and MUST reject all old-epoch event or control frames.

#### Scenario: Reset occurs with an open socket
- **WHEN** reset durably commits a new Session epoch
- **THEN** the old connection receives an epoch-changed control frame when possible and closes, and no later old-epoch event is accepted

### Requirement: Transport and application liveness remain distinct
The WebSocket MUST use ping/pong or equivalent control frames for transport liveness, while `turn_progress` remains the request-correlated application progress signal used by the chat idle watchdog.

#### Scenario: Socket is alive but provider is stalled
- **WHEN** ping/pong succeeds but no application progress arrives within request policy
- **THEN** the client applies application idle policy rather than treating socket liveness as task progress

### Requirement: Polling transport is unsupported
The backend MUST NOT expose `/chat/events`, and the Godot client MUST NOT create an event-polling request, timer, cadence setting, migration flag, or fallback path.

#### Scenario: Release routes and settings are inspected
- **WHEN** the release candidate inventories backend routes and frontend settings
- **THEN** no polling endpoint, timer, interval setting, or polling fallback selector exists

#### Scenario: Old client requests polling
- **WHEN** a client requests the removed `/chat/events` path
- **THEN** no compatibility handler serves event content
