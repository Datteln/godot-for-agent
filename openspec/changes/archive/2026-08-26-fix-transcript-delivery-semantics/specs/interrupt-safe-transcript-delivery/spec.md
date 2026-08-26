## ADDED Requirements

### Requirement: Stop followed by a new message preserves transcript delivery
The system SHALL treat Stop as cancellation of the active model turn, not as cancellation or completion of transcript synchronization. After the backend acknowledges the interrupt, the client MUST retain its last contiguous presented cursor, replay baseline, and recovery escalation state, and MUST continue to receive, project, and render the next user turn's persisted transcript events. It MUST NOT require the user to create a new session or restart the editor to see the next response.

#### Scenario: User sends after stopping an active turn
- **WHEN** the user stops an active turn and then submits a new message in the same session
- **THEN** the backend accepts the new message normally and the frontend displays its persisted user, Thought, assistant, and tool transcript entries as they arrive

### Requirement: Accepted visible transcript survives cancellation and restart
Once the backend accepts a user message and creates its visible transcript entry, it SHALL durably persist that entry and its matching event cursor before model execution can begin. High-frequency Thought and assistant updates MAY be coalesced by an asynchronous per-session durability writer, but the system MUST publish a coalesced visible update only after its matching durable checkpoint succeeds. Stop, normal completion, and controlled shutdown MUST await the final writer flush. Stop MAY discard only incomplete execution fragments that would violate the model tool-call protocol. It MUST NOT roll back accepted user entries, already-visible Thought, assistant, or tool entries, their monotonically allocated identities, or the cursor needed to recover them. The backend MUST persist the interrupted terminal state and cursor before acknowledging Stop.

#### Scenario: User stops after a visible Thought begins
- **WHEN** the user entry and a partial Thought have become visible and the user presses Stop
- **THEN** the next history snapshot contains the accepted user entry and the durable interrupted visible entries in their original order

#### Scenario: Service restarts after Stop
- **WHEN** the user stops a turn after its user entry or visible output was persisted and the service restarts
- **THEN** the restored history contains those entries and a cursor no older than the entries it returns

#### Scenario: Streaming updates are coalesced without becoming non-durable
- **WHEN** a Thought produces many updates inside the configured coalescing interval
- **THEN** the backend may write and publish only the latest coalesced state for that interval, and it publishes that state only after the matching durable checkpoint succeeds

### Requirement: Each accepted user turn has stable visible identity
The backend SHALL allocate one stable turn identity when it accepts a user message and persist that identity on the user entry and all Thought, assistant, tool, approval, and progress entries caused by that turn. Entry IDs, ordinals, revisions, and event sequences MUST remain monotonic and MUST NOT be reused after Stop. The client SHALL use the stable turn identity to isolate late live control effects without deleting ordered durable history.

#### Scenario: New turn follows an interrupted turn
- **WHEN** the backend accepts user turn B after interrupted turn A
- **THEN** B and all of its visible entries use B's distinct stable turn identity and do not reuse an A entry ID or revision

### Requirement: Delivery recovery never replays commands
The system SHALL repair a transcript-delivery discrepancy solely by replaying persisted events or hydrating persisted transcript history. It MUST NOT resend a user message, approval decision, tool result, reset, or interrupt as part of that repair.

#### Scenario: Recovery occurs while a new request is running
- **WHEN** the frontend discovers a delivery discrepancy after a new request has started
- **THEN** it reconciles visible transcript state without submitting a duplicate command or changing the new request's backend execution

### Requirement: Cancelled turns cannot control the next turn
The client SHALL use the persisted event or transcript entry `turn_id` to isolate live control effects. A late event belonging to a cancelled turn MUST NOT change the active new turn's state, pending approval/tool state, or execute a local action. A durable transcript entry that the server has persisted as visible history MUST remain reconcilable through replay or snapshot in ordinal order; the client MUST NOT use a global interrupt flag to erase that authoritative history.

#### Scenario: Late control event from a cancelled turn
- **WHEN** a user stops turn A, starts turn B in the same session, and a late turn-A approval or tool-control event arrives
- **THEN** the event cannot change turn B or execute a local action, while any durable turn-A transcript entry is represented only through the ordered canonical transcript
