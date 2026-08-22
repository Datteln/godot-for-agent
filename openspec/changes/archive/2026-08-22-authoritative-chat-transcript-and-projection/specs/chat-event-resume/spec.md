## MODIFIED Requirements

### Requirement: Retention gaps are explicit
If the server cannot replay all retained events after the requested cursor, it SHALL send a typed `history_gap` or `resync_required` response containing the affected session and cursor information. The client MUST stop accepting visible live patches, hydrate an atomic transcript snapshot, replace transcript state, and only then resume from the snapshot's `upto_event_seq`.

#### Scenario: Client reconnects after event retention expiry
- **WHEN** the requested `after_seq` precedes the earliest retained event
- **THEN** the server reports a typed gap and the client reloads the session transcript before resubscribing from the snapshot cursor

## ADDED Requirements

### Requirement: Initial hydration precedes visible patch delivery
When a chat surface opens or switches sessions, the client SHALL hydrate the transcript snapshot before subscribing for visible patches. It MUST NOT render replayed WebSocket patches while its transcript is in the hydrating state.

#### Scenario: Opening a session with retained events
- **WHEN** a session has both historical entries and retained WebSocket events
- **THEN** the client renders the snapshot once and subscribes with `after_seq` equal to the snapshot `upto_event_seq`, so retained entries represented by the snapshot are not appended again
