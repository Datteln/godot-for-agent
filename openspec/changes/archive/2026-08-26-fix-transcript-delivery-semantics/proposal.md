## Why

After a user stops a chat request, the backend can roll the whole in-memory turn back even after it has emitted the user entry and streamed visible model output. Those records are not durably checkpointed, can be recreated with the same entry identities by the next request, and disappear on service restart. In parallel, a frontend recovery hydration can wait on the backend long-held turn lock and reject every live patch while it waits. This makes Stop → Send appear stuck, misorders output around the user message, and loses later history after restart.

## What Changes

- Make the persisted server transcript the authoritative source for chat content; WebSocket delivery accelerates presentation but does not decide whether content exists.
- Persist an accepted user message and its transcript entry before model execution starts. Coalesce high-frequency Thought and assistant stream updates through an asynchronous per-session durability writer, then publish each coalesced visible update only after its durable checkpoint completes. Stop, normal completion, and controlled shutdown MUST wait for that writer to flush its final state and cursor. Stop may discard only protocol-incomplete tool-call state; it MUST NOT erase an accepted user message or an already-visible transcript entry.
- Give every accepted user turn a stable turn identity and attach it to its user, Thought, assistant, and tool transcript entries. A later turn MUST NOT reuse cancelled-turn entry identities or revisions.
- Make complete history snapshots obtainable while an LLM turn is running, without waiting for the lock that serializes mutable turn execution.
- Separate transport receipt from successful presentation. An event is skippable on replay only after its transcript state has been accepted and rendered, not merely because its network packet was previously received.
- Change resume handling to re-deliver every event after the last committed presentation cursor, including events seen on an earlier socket connection.
- Preserve unresolved transcript synchronization state across a user interrupt; stopping a model turn must not erase the need to reconcile the chat panel while the next turn runs.
- Define recovery success as actual contiguous committed-cursor progress, not merely packet receipt or Store mutation.
- Bind live control effects to the existing `turn_id` so a cancelled turn cannot alter the next turn's state, while durable transcript history remains server-authoritative.
- Escalate one replay that cannot advance its captured committed cursor by its own bounded deadline to a complete, atomic transcript-history hydration, with typed diagnostics and no replay of user commands or tool decisions.
- While a recovery snapshot is in flight, fence stale generations but retain current-generation live transcript patches. After the snapshot cut is applied, merge or replay only the patches newer than that cut; do not discard live output merely because the Projector is hydrating.
- After a recovery hydration replaces the transcript, reveal the recovered tail in the chat viewport so a user does not mistake an off-screen successful recovery for a continued stall.

## Capabilities

### New Capabilities

- `interrupt-safe-transcript-delivery`: Keeps chat content visible and recoverable across a stopped turn followed by a new message.

### Modified Capabilities

- `chat-event-websocket`: Changes replay and acknowledgement semantics so receipt alone cannot suppress unpresented events.
- `chat-event-resume`: Changes interrupted/resume recovery to preserve synchronization debt and hydrate when replay cannot close it.
- `chat-transcript-projection`: Requires the presentation pipeline to expose a committed rendered cursor independently from network receipt.

## Impact

- Godot frontend: `chat_event_socket.gd`, `chat_panel.gd`, transcript projector/store/recovery, and their tests.
- Existing transcript/history and WebSocket contracts: add durable-turn and snapshot-read semantics, carry a stable transcript turn identity, and maintain a complete snapshot cursor consistently; no change to map tools, LLM routing, or command semantics.
- User-visible behavior: Stop followed by a new message remains usable; accepted messages and visible partial output survive recovery and restart, and a successful recovery brings the newest reconciled response into view.
