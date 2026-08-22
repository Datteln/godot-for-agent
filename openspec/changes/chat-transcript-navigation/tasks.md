## 1. Paging and navigation contracts

- [ ] 1.1 Define transcript history page request/response fields, stable older-page cursor semantics, limits, and end-of-history indication.
- [ ] 1.2 Implement service-side ordered older-page retrieval without changing entry identity, revision, or visible semantics.
- [ ] 1.3 Extend the transcript Store to merge older pages by entry ID and ordinal while rejecting duplicate or conflicting entries.
- [ ] 1.4 Capture long-session fixtures covering page overlap, older-page exhaustion, streamed tail updates, and entries with highly variable Markdown heights.

## 2. Virtual transcript viewport

- [ ] 2.1 Build a TranscriptViewport with bounded contiguous window selection, configurable overscan, and top/bottom spacer controls.
- [ ] 2.2 Mount and evict typed renderer roots by entry ID while retaining every loaded entry in Store state.
- [ ] 2.3 Add revision- and width-aware height estimates and measured-height cache invalidation.
- [ ] 2.4 Enforce a configurable mounted-root resource bound and record diagnostics for mount count, estimated range height, and evictions.

## 3. Anchor, follow, and remount behavior

- [ ] 3.1 Preserve the first visible entry ID and intra-entry offset across measurement, streaming revision, page merge, and window changes.
- [ ] 3.2 Implement explicit follow mode, tail proximity detection, user-scroll opt-out, and return-to-latest affordance.
- [ ] 3.3 Trigger older-page loading at the leading threshold without rebuilding mounted roots or duplicating Store entries.
- [ ] 3.4 Define reset-safe pooling or freeing of evicted renderer roots, including cleanup of callbacks, selection, expansion, and approval controls.

## 4. Long-session acceptance

- [ ] 4.1 Exercise virtual-window selection, root bounds, height invalidation, anchor recovery, follow mode, and older-page merge with deterministic fixtures.
- [ ] 4.2 Exercise tool/approval/error remount and copy behavior through the real typed renderers.
- [ ] 4.3 Run a long streaming task in the editor and verify bounded control count, stable scrolling, no forced tail jump while reviewing history, and no Godot crash.
- [ ] 4.4 Headless-load the plugin and record navigation diagnostics for a multi-page transcript with streamed Markdown and tool cards.
