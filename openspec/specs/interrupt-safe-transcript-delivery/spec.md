# Interrupt-Safe Transcript Delivery Spec

## Purpose

Ensure Stop cancels model execution without deleting durable conversation history or allowing cancelled turns to control later turns.

## Requirements

### Requirement: Stop followed by a new message preserves transcript delivery
The system SHALL treat Stop as cancellation of the active model turn, not as cancellation or completion of transcript synchronization. After acknowledgement, the client MUST retain its contiguous presentation cursor, replay baseline, and recovery escalation state, and continue to receive, project, and render the next user turn's persisted entries without creating a new session or restarting the editor.

#### Scenario: User sends after stopping an active turn
- **WHEN** the user stops a turn and submits a message in the same session
- **THEN** the backend accepts it and the frontend displays its persisted user, Thought, assistant, and tool entries

### Requirement: Accepted visible transcript survives cancellation and restart
Once a user message and visible transcript entry are accepted, the backend SHALL durably persist them and their matching cursor before model execution. Coalesced stream updates MUST be checkpointed before publication; Stop, normal completion, and controlled shutdown MUST flush final state. Stop MAY discard only incomplete execution fragments that would violate the tool-call protocol and MUST NOT roll back visible entries, identities, or recovery cursor.

#### Scenario: Service restarts after Stop
- **WHEN** a user stops after persisted user or visible output and the service restarts
- **THEN** restored history contains those entries and a cursor no older than them

### Requirement: Each accepted user turn has stable visible identity
The backend SHALL allocate and persist one stable turn identity for every accepted user message, and user, Thought, assistant, tool, approval, and progress entries caused by it SHALL carry that identity. Entry IDs, ordinals, revisions, and event sequences MUST remain monotonic and never be reused after Stop.

#### Scenario: New turn follows an interrupted turn
- **WHEN** the backend accepts turn B after interrupted turn A
- **THEN** B uses a distinct identity and does not reuse A entry IDs or revisions

### Requirement: Cancelled turns cannot control the next turn
The client SHALL use persisted event or transcript `turn_id` to isolate late live control effects. A late cancelled-turn event MUST NOT change the new turn's state, pending approval/tool state, or execute a local action, while durable transcript entries remain reconcilable in ordinal order.

#### Scenario: Late control event from a cancelled turn
- **WHEN** turn A is stopped, turn B starts, and a late A approval or tool-control event arrives
- **THEN** it cannot control B and any durable A transcript is represented only through ordered canonical history
