## ADDED Requirements

### Requirement: Active visible transcript stalls recover without replaying commands
While a chat turn remains active, the client SHALL start one bounded recovery attempt for a visible transcript stall when the service reports a newer visible sequence than the minimum of the client's received, committed, projected, and rendered continuous watermarks, and that lagging visible stage has not advanced within the configured interval. It MUST first reconnect and subscribe from its highest contiguous committed cursor. It MUST NOT resubmit a user message, tool approval, reset, cancellation, or interruption as part of recovery.

#### Scenario: ClassInfo is followed by an unseen bootstrap approval
- **WHEN** the client last rendered a ClassInfo tool result and the service reports later persisted Thought and approval entries for the same active turn
- **THEN** the client reconnects from its contiguous cursor and continues the existing turn without submitting the map request again

#### Scenario: WebSocket accepts an event that never reaches the viewport
- **WHEN** `received_seq` has advanced after a streamed Thought patch but `projected_seq` or `rendered_seq` remains behind the service `visible_seq` beyond the configured interval
- **THEN** the client treats the lagging stage as a visible transcript stall and performs bounded resume/snapshot recovery even though no transport sequence gap exists

### Requirement: Active recovery detects stalled projection and silent subscriptions
The client SHALL track the age of its oldest pending streaming transcript patch and the freshness of an active WebSocket subscription. A non-empty pending projection set that does not advance projected visible progress within the configured interval, or an Open subscription that exceeds its freshness interval without an expected heartbeat or visible-progress confirmation, MUST trigger one bounded recovery confirmation through the compatible recovery-pointer or history-probe path. The recovery MUST preserve the existing active turn and MUST NOT replay a command.

#### Scenario: Projection window never drains
- **WHEN** a replaceable streaming patch remains pending past the configured interval while its event was already acknowledged by the transport
- **THEN** the client requests authoritative recovery and eventually renders the durable entry from replay or snapshot

#### Scenario: Open socket stops delivering progress
- **WHEN** an active WebSocket remains Open but does not deliver a fresh heartbeat or visible-progress confirmation for the configured interval
- **THEN** the client probes the server and resumes or hydrates if the server confirms an active turn or a newer visible cursor

### Requirement: Reset establishes an interruption boundary
When a user resets a session with an active or queued chat/tool-result request, the client SHALL cancel its in-flight request and discard queued chat/tool-result requests, request interruption of the old active turn, then perform reset and history hydration. It MUST associate asynchronous responses and events with session, turn, and generation and MUST ignore late data from the pre-reset turn.

#### Scenario: A tool result is queued when Reset is pressed
- **WHEN** the frontend has a queued or in-flight tool-result `/chat` request and the user resets the session
- **THEN** the old request cannot start or continue the old turn after reset, and its late response/event cannot add entries to the reset transcript

### Requirement: Failed replay falls back to an atomic history snapshot
If typed gap signaling, replay exhaustion, or a completed bounded resume attempt cannot restore a contiguous visible event stream, the client SHALL hydrate the authoritative session history snapshot atomically, set its resume cursor to the snapshot's `upto_event_seq`, and then resubscribe. It MUST retain the active request when the recovered snapshot identifies the same active turn.

#### Scenario: Retained events cannot close a gap
- **WHEN** a client reconnects after a missing patch and the service reports `history_gap`
- **THEN** the client replaces its transcript from the history snapshot and resumes only after the snapshot cursor

#### Scenario: Recovery does not duplicate a command
- **WHEN** a recovery occurs while an HTTP chat command is still waiting for completion
- **THEN** the client preserves that command's waiting state and does not issue a second chat command
