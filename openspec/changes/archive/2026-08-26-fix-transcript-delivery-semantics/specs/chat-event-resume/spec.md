## MODIFIED Requirements

### Requirement: Active visible transcript stalls recover without replaying commands
While a chat turn remains active, the client SHALL start one bounded recovery attempt for a visible transcript stall when the service reports a newer visible sequence than the minimum of the client's received, committed, projected, and rendered continuous watermarks, and that lagging visible stage has not advanced within the configured interval. It MUST first reconnect and subscribe from its highest contiguous committed cursor. It MUST NOT resubmit a user message, tool approval, reset, cancellation, or interruption as part of recovery.

When replay begins, the client MUST capture its contiguous `committed_seq` as the replay baseline. Once the replacement subscription is established, it MUST start one monotonic resume deadline. Replay succeeds only if that contiguous committed cursor becomes greater than the captured baseline. Packet receipt, Store mutation, projected or rendered watermark movement, heartbeats, or a newer server-visible sequence MUST NOT by themselves finish replay recovery. If the deadline expires without baseline progress, the recovery state machine itself MUST transition from replay to authoritative history hydration; it MUST NOT wait for another sequence gap, packet, or visible-stall trigger. The client MUST then hydrate the complete authoritative history snapshot and resubscribe from its atomic `upto_event_seq` cursor.

Stop MUST NOT erase an unresolved replay obligation, its baseline, or its escalation budget. It MUST retain session delivery state needed to reconcile later persisted entries.

During authoritative hydration for an active turn, the system MUST obtain the history snapshot without waiting for model execution to complete. The returned snapshot cursor MUST be an atomic cut over durable transcript entries: it MUST NOT claim a cursor beyond the durable entries included in that response.

#### Scenario: ClassInfo is followed by an unseen bootstrap approval
- **WHEN** the client last rendered a ClassInfo tool result and the service reports later persisted Thought and approval entries for the same active turn
- **THEN** the client reconnects from its contiguous cursor and continues the existing turn without submitting the map request again

#### Scenario: WebSocket accepts an event that never reaches the viewport
- **WHEN** `received_seq` has advanced after a streamed Thought patch but `projected_seq` or `rendered_seq` remains behind the service `visible_seq` beyond the configured interval
- **THEN** the client treats the lagging stage as a visible transcript stall and performs bounded resume/snapshot recovery even though no transport sequence gap exists

#### Scenario: Resume cannot close a commit gap after Stop
- **WHEN** a user stops a turn with `committed_seq` behind previously received events, then starts a new turn, and replay does not advance the committed cursor
- **THEN** the client hydrates authoritative history and continues showing the new turn without requiring another user command

#### Scenario: Replay changes the Store but cannot advance the commit cursor
- **WHEN** a replayed event is de-duplicated by the Store or a later projected patch changes the Store while the captured committed cursor remains unchanged
- **THEN** the client does not declare recovery healthy and escalates to authoritative snapshot hydration after its bounded replay attempt

#### Scenario: Replay becomes silent after reconnect
- **WHEN** the replacement subscription is established, the captured committed cursor remains unchanged through the resume deadline, and no second recovery trigger occurs
- **THEN** the recovery state machine itself starts authoritative snapshot hydration and does not remain in `RESUMING`

#### Scenario: History is requested while a model turn still streams
- **WHEN** recovery requests a complete transcript snapshot while the service is still producing a visible Thought or assistant stream
- **THEN** the service returns a matching durable transcript cut promptly without waiting for the LLM turn to finish
