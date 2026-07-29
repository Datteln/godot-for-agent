## 1. Backend event protocol and regression harness

- [x] 1.1 Add a regression test that pauses a streamed provider inside a tool-result submission and proves preview deltas are observable before provider completion and Session save
- [x] 1.2 Extend the chat-event DTO and `/chat/events` response with backward-compatible preview identity, bounded-page cursor, and `has_more` fields
- [x] 1.3 Add EventStore paging tests for sequence ordering, page limits, coalesced snapshot replacement, append-only fragments, and cursor advancement
- [x] 1.4 Implement bounded EventStore reads and route-level limit validation without changing idle `turn_progress` behavior

## 2. Transaction-safe preview publication

- [x] 2.1 Define and test an explicit event delivery classification separating provisional previews, transactional publications, and out-of-band liveness
- [x] 2.2 Publish text/reasoning previews immediately from an active publication buffer with stable preview, request, turn, frame, and message identity
- [x] 2.3 Keep preview history changes isolated in the working Session and prevent publication flush from appending the same preview text a second time
- [x] 2.4 Emit and test `submission_preview_committed` only after successful Session persistence and transactional publication
- [x] 2.5 Emit and test `submission_preview_discarded` for cancellation, reducer failure, and Session persistence failure without exposing staged tool, grant, workflow, or artifact events
- [x] 2.6 Add request-correlated first-provider-chunk, first-preview-publication, commit/discard, batch-size, and backlog diagnostics that exclude secrets and full prompts

## 3. Godot event client backpressure

- [x] 3.1 Add client tests for bounded event pages, missing optional fields from an older backend, accepted-event cursor advancement, and `has_more`
- [x] 3.2 Update `agent_http_client.gd` to request and parse bounded pages and immediately repoll while `has_more=true`
- [x] 3.3 Ensure immediate backlog polling never overlaps the active event HTTPRequest and returns to the configured idle cadence when drained
- [x] 3.4 Route preview commit/discard boundaries by stable identity and ignore stale boundaries from older request generations
- [x] 3.5 Verify provisional previews refresh the `/chat` idle watchdog but never trigger request replay, tool-result resubmission, or client-side model fallback

## 4. Frame-budgeted rendering and follow mode

- [x] 4.1 Add headless chat-panel tests reproducing a 203-event burst, append-delta preservation, editor-frame yielding, and ordered completion
- [x] 4.2 Replace synchronous full-queue draining with a per-frame item or elapsed-time budget while retaining compatible snapshot coalescing
- [x] 4.3 Track explicit user scroll/drag/navigation intent separately from scrollbar value changes caused by content growth, virtualization, and programmatic scrolling
- [x] 4.4 Coalesce bottom-scroll requests to one post-layout correction per rendered batch while follow mode is enabled
- [x] 4.5 Add tests that content growth preserves follow mode, user upward navigation disables it, returning to bottom re-enables it, and final Markdown layout still settles at the bottom
- [x] 4.6 Implement and test the selected discarded-preview UX without affecting committed or newer active preview messages

## 5. End-to-end verification

- [x] 5.1 Run backend unit and integration suites covering normal user turns, atomic tool-result commit, rollback, interrupt, retry/idempotency, and EventStore overflow
- [x] 5.2 Run Godot client and chat-panel suites covering old/new backend compatibility, large backlogs, interruption, session switching, and virtualized history
- [ ] 5.3 Reproduce a long map-agent continuation and verify bounded first-preview latency, no 203-event single-frame render, continued `/chat` liveness, and automatic scrolling until explicit user navigation
- [x] 5.4 Document the chosen page/render limits and compare the new correlated timeline against the 19:20:47 and 19:21:22 burst pattern from the captured logs
