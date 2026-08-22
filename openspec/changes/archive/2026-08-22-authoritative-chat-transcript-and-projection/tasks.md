## 1. Contract and regression baseline

- [x] 1.1 Define the versioned transcript snapshot, entry, patch, revision, and `client_message_id` contracts shared by history, HTTP command acknowledgement, and WebSocket payloads.
- [x] 1.2 Capture contract fixtures for duplicate final delivery, two identical answers, tool/approval/progress persistence, stale session history, reconnect replay, and retention-gap hydration.
- [x] 1.4 Add contract fixtures for live Thought token updates, completion duration, collapse/expand, history reload, and reconnect replay.
- [x] 1.5 Add regression fixtures for a late reasoning delta after Thought completion, a thinking-budget boundary followed by original-stream content or tool calls, an empty-content model response recovered by one final-answer retry, and a retry that remains empty.
- [x] 1.3 Decide and document whether a completed tool updates its activity entry or produces a linked result entry; use the decision consistently in every contract.

## 2. Authoritative backend transcript

- [x] 2.1 Add durable session-owned transcript storage with stable entry IDs, immutable ordinals, typed state, revisions, and legacy-conversion metadata.
- [x] 2.2 Add a single TranscriptWriter that records every user-visible user, assistant, tool, approval, progress, verification, and error transition at the moment it occurs.
- [x] 2.3 Route assistant delta and final output through one assistant entry identity; remove all user-visible Thought and text-prefix inference from new-session transcript production.
- [x] 2.4 Return a transcript snapshot and its atomic `upto_event_seq` from history, without rebuilding new-session presentation from frames or transport events.
- [x] 2.5 Implement and persist one-time, non-inventive conversion for legacy sessions that lack a transcript.
- [x] 2.6 Persist each explicitly user-visible Thought as one `kind=thought` entry with cumulative content, token count, start time, completion duration, and revision updates.
- [x] 2.7 Make the Thought `complete` state one-way and defer its settlement until the original model response stream is fully consumed; retain the final accumulated reasoning even after a thinking-budget boundary.
- [x] 2.8 Use `enable_thinking: true` without a default hard thinking-token cap; reject a no-tool model response with empty assistant content only after its original stream ends, then perform exactly one no-thinking/no-tool final-answer recovery attempt and persist a typed error if it remains empty.

## 3. WebSocket and command-boundary integration

- [x] 3.1 Publish idempotent visible transcript patches through the existing WebSocket event envelope, preserving immutable event sequence and acknowledgement behavior.
- [x] 3.2 Make HTTP final command responses confirmation-only for presentation, with a history resynchronization fallback when the matching transcript completion cannot be accepted live.
- [x] 3.3 Ensure tool, approval, plan, verification, and error producers publish only their declared transcript entry transition rather than a second generic display event.
- [x] 3.4 Verify snapshot cursor boundaries, reconnect replay, retention gaps, and slow-client resynchronization against the transcript patch contract.
- [x] 3.5 Publish Thought start, content/token revisions, and completion as patches for the same transcript entry identity.
- [x] 3.6 Publish completed Thought only after the original stream ends, plus empty-final recovery/error transitions through the same transcript patch contract; never publish an empty assistant completion as a successful final.

## 4. Godot transcript state and rendering

- [x] 4.1 Build a session/generation-scoped TranscriptStore that atomically replaces snapshots and applies event-ID-deduplicated, revision-aware patches.
- [x] 4.2 Build a TranscriptProjector that validates session, hydration state, entry identity, ordinal, and revision before changing the Store.
- [x] 4.3 Replace direct history/event/HTTP message appends in ChatPanel with Store projection; preserve optimistic user submission only through `client_message_id` reconciliation.
- [x] 4.4 Gate WebSocket subscription and patch acceptance behind `HYDRATING → REPLACE_SNAPSHOT → READY`, including session switch, recovery, gap, and resync paths.
- [x] 4.5 Adapt existing Markdown, log, tool-preview, approval, progress, and error controls into kind-only transcript renderers; remove text-fingerprint deduplication.
- [x] 4.6 Add `kind=thought` projection and renderer binding that preserves a Thought entry's collapse state across revisions and reconstructs it from persisted data after history hydration or reconnect.
- [x] 4.7 Treat a confirmation-only empty HTTP final as terminal when a valid assistant completion patch was accepted, and otherwise resync without a false 60-second timeout.

## 5. Acceptance and migration verification

- [x] 5.1 Exercise backend transcript contracts for live-to-history equivalence, stable ordering, one assistant entry per response, and legacy conversion stability.
- [x] 5.2 Exercise Godot Store/Projector behavior for duplicate replay, lower revision delivery, delayed prior-session responses, two equal-text answers, and snapshot replacement.
- [x] 5.3 Exercise end-to-end WebSocket/history recovery with streaming text, tool activity, approval, progress, verification, error, session switch, reconnect, and retention gap.
- [ ] 5.4 Headless-load the plugin and manually verify one long real session, history reload, copying, Markdown, tool decisions, follow behavior, Thought expansion, and no duplicate assistant entry.
- [x] 5.5 Exercise end-to-end Thought rendering: while active it shows `Thinking {token_count} Tokens >`; after completion it shows `Thought for {duration_seconds}s >`; its complete persisted content expands after live updates, history reload, and reconnect.
- [x] 5.6 Verify no completed Thought regresses after late deltas, a thinking-budget boundary still accepts later original-stream content/tool calls, empty-content recovery produces one non-empty assistant entry, and unrecoverable empty content shows an error without the timeout warning.
