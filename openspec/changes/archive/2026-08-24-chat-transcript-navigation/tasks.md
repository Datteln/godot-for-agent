## 1. Paging and navigation contracts

- [x] 1.1 Define transcript history page request/response fields: session, version, `upto_event_seq`, stable older-page cursor, limit, `has_more`, in-flight deduplication, retry, and end-of-history indication.
- [x] 1.2 Implement service-side ordered older-page retrieval without changing entry identity, revision, or visible semantics.
- [x] 1.3 Extend the READY-state transcript Store to merge older pages by entry ID and ordinal while rejecting duplicate, lower-revision, conflicting, or terminal-state-regressing entries; preserve atomic replacement for initial hydration/resync.
- [x] 1.4 Capture long-session fixtures covering page overlap, page/live-patch races, page/hydration races, older-page exhaustion/retry, streamed tail updates, oversized preview-to-complete entries, complete-content eviction/remount, and entries with highly variable Markdown heights.

## 2. Virtual transcript viewport

- [x] 2.1 Build a TranscriptViewport with bounded contiguous window selection, configurable overscan, and top/bottom spacer controls.
- [x] 2.2 Mount and evict typed renderer roots by entry ID while retaining every loaded entry in Store state.
- [x] 2.3 Add revision-, width-, presentation-epoch-, and preview/complete-mode-aware height estimates and measured-height cache invalidation.
- [x] 2.4 Enforce a configurable mounted-root resource bound, consume renderer initial preview budgets, allow explicit complete-content mounts, and record diagnostics for mount count, mounted rich-text characters, estimated range height, content modes, and evictions.

## 3. Anchor, follow, and remount behavior

- [x] 3.1 Preserve the first visible entry ID and intra-entry offset across measurement, streaming revision, page merge, and window changes, with successor/predecessor/estimated-position fallback when the anchor is unavailable.
- [x] 3.2 Implement explicit follow mode, tail proximity detection, user-scroll/selection/copy/detail-interaction opt-out, and return-to-latest affordance.
- [x] 3.3 Trigger older-page loading at the leading threshold without rebuilding mounted roots or duplicating Store entries.
- [x] 3.4 Define reset-safe pooling or freeing of evicted renderer roots, including cleanup of callbacks, selection, expansion, and approval controls.
- [x] 3.5 Keep local transient notices outside viewport ownership and directly discard them on local-state change, hydration, resync, or session switch.
- [x] 3.6 Define and implement one-owner handoff for execution-before preview controls between the inline confirmation host and approval/tool activity renderers; prevent preview caches from retaining controls queued for host disposal, and fall back to durable summaries when no live preview is available.

## 4. Long-session acceptance

- [x] 4.1 Exercise virtual-window selection, root bounds, height invalidation, anchor recovery, follow mode, and older-page merge with deterministic fixtures.
- [x] 4.2 Exercise tool/approval/error remount, resolved-approval text nodes, preview/complete/evict behavior, copy behavior, and transient-notice discard through the real typed renderers.
- [x] 4.3 Run a long streaming task in the editor and verify bounded control count, stable scrolling, no forced tail jump while reviewing history, and no Godot crash.
- [x] 4.4 Headless-load the plugin and record navigation diagnostics for a multi-page transcript with streamed Markdown and tool cards.
- [x] 4.5 Add regression coverage for apply/reject, confirmation disposal, interruption/session switch, and delayed approval updates, proving no renderer path accesses a previously freed preview instance.

## 5. Patch outcome diagnostics

- [x] 5.1 Add redacted structured diagnostics for every rejected live `transcript_patch`, including projector-not-ready, generation/session mismatch, invalid payload, duplicate/non-newer revision and renderer rejection; add fixtures proving a server-generated assistant stream cannot silently disappear during hydration, page merge or viewport window changes.
- [x] 5.2 Distinguish per-call allow selection, batch execution, and explicit rejection in the inline confirmation UI; preserve executor `error` results as errors in approval/tool-activity rows and model tool messages, add redacted decision-source diagnostics, and cover allowed-invalid-input, explicit-reject, and mixed-batch outcomes.
