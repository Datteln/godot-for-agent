## 1. Correct replay delivery semantics

- [x] 1.1 Refactor `ChatEventSocket` receipt and de-duplication bookkeeping so that every replayed event whose sequence is greater than the committed presentation cursor can be delivered again after reconnect, even when the previous socket already received it.
- [x] 1.2 Preserve contiguous commit semantics: advance the presentation cursor only after the transcript Store and viewport accept the event, and expose payload-safe diagnostics for the first sequence blocking progress.
- [x] 1.3 Capture the committed cursor when recovery begins, start a monotonic deadline once the replacement subscription is established, and let replay finish only after that exact cursor advances; receipt, Store mutation, and server progress must not close recovery by themselves.

## 2. Isolate Stop from the next turn and converge recovery to history

- [x] 2.1 Replace global interrupt-based event suppression with `turn_id`-aware control isolation: cancelled-turn events cannot change the next turn's status, approval/tool state, or trigger local actions, while durable transcript history remains projectable.
- [x] 2.2 Preserve the session's replay baseline and resume-to-hydrate escalation state across Stop; do not reset them when a new message begins.
- [x] 2.3 Make the recovery state machine itself transition from RESUMING to hydration when its deadline expires without baseline committed-cursor progress; hydrate the complete (`limit=0`) canonical history snapshot behind a projection-generation fence, render it atomically, reset cursors from `upto_event_seq`, and resubscribe without replaying commands.
- [x] 2.4 Preserve optimistic user messages during snapshot replacement through `client_message_id` reconciliation.

## 3. Add regression coverage and verify manually

- [x] 3.1 Add socket tests for an event received but not committed before reconnect: replay re-delivers it, progress commits, and the Store remains idempotent.
- [x] 3.2 Add a recovery test where Store/projection progresses but `committed_seq` does not and no second recovery trigger arrives; expiry of the resume deadline must hydrate instead of declaring replay healthy or remaining in RESUMING.
- [x] 3.3 Add a Stop-then-send integration test: the server receives the new request, its persisted response becomes visible, and no command is repeated.
- [x] 3.4 Add tests that a late cancelled-turn event cannot alter the next turn's control state, and that a durable cancelled-turn transcript entry is reconciled only as ordinary ordered history.
- [x] 3.5 Add a snapshot-fence test: events at or before `upto_event_seq` render once from the complete snapshot and later events arrive from the replacement subscription.
- [x] 3.6 After successful recovery hydration, force a deferred tail scroll that overrides a prior manual browsing anchor, and add a regression test showing the reconciled latest response is visible after layout settles.
- [x] 3.7 Run a Godot editor smoke test with an intentionally delayed or rejected transcript patch, then Stop and send a new message; verify the new response appears without restarting the editor. _(Passed by manual verification.)_

## 4. Make server transcript history durable across Stop and restart

- [x] 4.1 Split rollbackable agent execution state from durable transcript/history state; persist the accepted user message and its transcript entry before any model streaming begins.
- [x] 4.2 Add a per-session asynchronous durability writer that coalesces high-frequency Thought and assistant updates for a bounded interval, performs file work away from the request event loop, and publishes each coalesced visible update only after its matching checkpoint succeeds.
- [x] 4.3 Await the durability writer at accepted-user-message, Stop, normal-completion, and controlled-shutdown boundaries; flush the final interrupted state/cursor before acknowledging Stop.
- [x] 4.4 Prevent cancellation from reusing transcript entry IDs, ordinals, revisions, or event sequences; give each accepted user turn a stable turn identity carried by user, Thought, assistant, and tool entries.
- [x] 4.5 Expose a complete atomic history snapshot during an active LLM turn without waiting on the long-held mutable-turn lock; the snapshot cursor MUST match its durable entries.
- [ ] 4.6 Add backend regression tests for bounded stream coalescing, publish-after-checkpoint ordering, Stop after user acceptance, Stop during streamed Thought, Stop then Send, controlled restart, and service restart. Verify accepted user entries, visible partial output, entry identity, and sequence cursors remain durable.

## 5. Keep frontend presentation live while authoritative hydration runs

- [x] 5.1 Replace the HYDRATING black-hole behavior with a generation-fenced ordered buffer for current-session live transcript patches; do not reject valid patches solely because the snapshot request is in flight.
- [x] 5.2 On snapshot completion, render the atomic cut once, discard buffered events at or below its cursor, and apply or replay the buffered post-cut range before resubscribing.
- [ ] 5.3 Add projection/recovery tests for a slow history snapshot while live Thought patches continue, a Stop during that wait, and a post-snapshot patch. Verify no patch is silently lost and no entry is rendered twice.
- [x] 5.4 Extend the live Godot smoke test: Stop a streaming map request, send two ordinary messages, force recovery if available, restart the plugin/service, and verify both user messages and all persisted visible transcript entries remain in order. _(Passed by manual verification.)_
