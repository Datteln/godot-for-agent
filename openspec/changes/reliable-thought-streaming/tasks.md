## 1. Visible Thought delivery instrumentation

- [ ] 1.1 Define Thought-token publication, receipt, projection and renderer watermarks keyed by session, entry, revision and event sequence.
- [ ] 1.2 Emit bounded server-side publication/subscription diagnostics without Thought text, prompts or tool results.
- [ ] 1.3 Record client WebSocket receipt, projector acceptance/rejection and viewport routing diagnostics for every model-emitted Thought token.

## 2. Stall detection and recovery

- [ ] 2.1 Detect an active Thought watermark that advances without corresponding client projection within a configurable bounded interval.
- [ ] 2.2 Implement one bounded cursor-resume attempt for a stalled Thought while preserving the active chat request.
- [ ] 2.3 Fall back to atomic transcript hydration for a replay gap or unrecoverable projection mismatch, then resume from the snapshot cursor.
- [ ] 2.4 Record a redacted delivery failure when a Thought still cannot be rendered after recovery, without displaying a user-facing recovery notice.

## 3. Projection and rendering guarantees

- [ ] 3.1 Ensure every live and recovered Thought token revision enters the canonical Store and is immediately routed to the transcript viewport independent of follow/scroll mode.
- [ ] 3.2 Preserve terminal Thought state and prevent pending/coalesced patches from regressing a recovered entry.
- [ ] 3.3 Persist and render all provider-emitted reasoning/Thought content without filtering by a user-visible flag; do not fabricate content absent from the provider stream.
- [ ] 3.4 Remove service and client coalescing/batching for Thought tokens so each received token produces an ordered visible update.

## 4. Verification

- [ ] 4.1 Add service tests for token-level Thought watermark metadata, ordered replay/snapshot recovery and unfiltered provider reasoning persistence.
- [ ] 4.2 Add Godot tests for token-by-token display, dropped Thought patches, projector/renderer rejection, snapshot repair and terminal-state preservation.
- [ ] 4.3 Add an end-to-end long-Thought smoke test proving the UI advances for every token during a slow model turn and remains silently recoverable after interrupted live delivery.
- [ ] 4.4 Run targeted service and Godot test suites, OpenSpec validation, and record any remaining manual editor verification steps.
