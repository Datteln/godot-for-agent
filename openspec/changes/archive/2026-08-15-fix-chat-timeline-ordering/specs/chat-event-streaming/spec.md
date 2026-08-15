## MODIFIED Requirements

### Requirement: Live and historical content share one canonical Timeline projection
Accepted WebSocket chat events and canonical history event pages MUST enter the same pure `ChatTimelineProjector` and produce the same `ChatTimelineItem` structure and stable identities for equivalent content. Every item MUST carry a stable `item_id`, Session epoch, deterministic order key, closed kind and role, typed content blocks, lifecycle, status, copy text, style token, and applicable Frame, message, tool, artifact, and preview source identities. Rendered text MUST NOT be used as identity. All order keys MUST be drawn from a single totally ordered integer sequence space shared by event, message, tool, and local items; item comparison MUST NOT mix key types, and locally created items MUST be keyed relative to the accepted sequence high-water mark instead of a fixed large integer offset.

#### Scenario: Live stream is restored from history
- **WHEN** an assistant or reasoning item first arrives through WebSocket and is later loaded from a history page
- **THEN** both inputs project to the same item identity, content-block schema, lifecycle, copy text, style token, and renderer selection

#### Scenario: History page is prepended
- **WHEN** an older canonical event page is projected and prepended before existing Timeline items
- **THEN** existing item identities and content remain unchanged, duplicate items are not inserted, and the VirtualScroller preserves the visible scroll anchor

#### Scenario: Backend history is requested
- **WHEN** the frontend loads canonical history for a Session epoch
- **THEN** the backend returns canonical event records and does not generate `_history_log_text`, `_history_thought`, `_history_code`, or `_history_front_tool_result`

#### Scenario: Local and server items interleave chronologically
- **WHEN** a local notice or optimistic user item is inserted between accepted server events
- **THEN** it is positioned by its chronological order key relative to the accepted sequence high-water mark, and is never grouped above or below server items merely because of its origin

#### Scenario: Mixed-type order keys fail closed
- **WHEN** projection or insertion produces an order key whose element types differ from an existing item's at the same comparison position
- **THEN** the store rejects the mutation with a typed reason instead of silently falling back to string comparison

### Requirement: Accepted WebSocket events are the sole live presentation authority
HTTP chat and tool-result responses MUST be command acknowledgements only and MUST NOT create, patch, finalize, discard, or deduplicate a live Timeline item. Live `tool_calls`, `final`, `error`, text, reasoning, preview, system, and tool-result presentation MUST originate from accepted WebSocket events and enter ChatTimelineProjector exactly once. Bounded history or snapshot recovery MAY hydrate canonical events through the same projector but MUST NOT establish a second live response-rendering path. As a narrow exception for submission latency, the client MUST render the user's own submission immediately at send time as a local provisional item; the accepted `user_submitted` event MUST reconcile that provisional item exactly once (replace or discard) so that exactly one user item remains.

#### Scenario: HTTP acknowledgement arrives before its terminal event
- **WHEN** a chat command returns `tool_calls`, `final`, or `error` acknowledgement before the matching accepted WebSocket event
- **THEN** no live item is rendered from the HTTP body and the later event creates or finalizes the canonical item once

#### Scenario: HTTP acknowledgement arrives after its terminal event
- **WHEN** the matching WebSocket event has already updated the Timeline before the HTTP command finishes
- **THEN** the acknowledgement does not append, patch, fingerprint-deduplicate, or otherwise change the Timeline

#### Scenario: Snapshot recovery restores visible content
- **WHEN** a typed `snapshot_required` disposition triggers bounded HTTP recovery
- **THEN** recovered canonical events enter the same projector and stable identities, while continuous HTTP polling and direct snapshot rendering remain absent

#### Scenario: Optimistic user bubble at send time
- **WHEN** the user submits a chat message
- **THEN** the client immediately renders a local provisional user item at the timeline tail without waiting for any server event

#### Scenario: user_submitted echo reconciles once
- **WHEN** the accepted `user_submitted` event arrives while its provisional user item is visible
- **THEN** the client replaces or discards the provisional item exactly once and exactly one user item remains for that submission

#### Scenario: Echo is lost but history restores
- **WHEN** the `user_submitted` echo is never accepted but canonical history for the epoch contains the submission
- **THEN** history restoration shows exactly one user item and no duplicate provisional item survives an epoch reset
