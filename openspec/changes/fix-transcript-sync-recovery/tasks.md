## 1. Establish the transcript recovery contract

- [x] 1.1 Trace service event publication, retention, subscriber queue, history snapshots, and Godot received/projected/rendered cursor ownership; document the concrete drop path found in the failing ClassInfo workflow.
- [x] 1.2 Add typed, payload-free visible-progress and resynchronization protocol fields, including session ID, event sequence/cursor, update timestamp, and recovery reason.
- [x] 1.3 Make subscriber overflow, replay retention gaps, and server-side subscription resets emit explicit `resync_required` or `history_gap` rather than silently omitting visible events.
- [x] 1.4 Verify history snapshots atomically contain every durable visible entry kind and a cursor from which live delivery can resume.

## 2. Implement client continuity and recovery

- [x] 2.1 Track per-session received, projected, and rendered contiguous visible-event watermarks in the Godot chat transport and transcript Store.
- [x] 2.2 Detect sequence gaps, patch decoding errors, invalid revision transitions, projector rejection, and renderer-routing failures; log redacted typed diagnostics without entry payload text.
- [x] 2.3 Detect active visible-transcript stalls from service progress watermarks while avoiding false completion or cancellation caused by heartbeats and unrelated transport traffic.
- [x] 2.4 Implement one bounded recovery state machine: reconnect from the highest contiguous cursor, then atomically hydrate history and resubscribe from `upto_event_seq` if replay cannot close the gap.
- [x] 2.5 Ensure recovery neither resubmits chat/tool/approval commands nor allows stale session/generation data to overwrite the active transcript.
- [x] 2.6 Rebuild the viewport from the canonical Store after recovery so Thought, assistant, tool activity, approval, progress, verification, and error entries all render in ordinal order.
- [x] 2.7 Stabilize virtual viewport height measurement across layout, width, and display-mode changes; place transient waiting/error/report notices in a dedicated visible mount so spacer estimates cannot hide them.
- [x] 2.8 Change active-stall detection to compare service `visible_seq` with the minimum of received, projected, and genuinely rendered watermarks; emit redacted diagnostics identifying the lagging stage.
- [x] 2.9 Track pending streaming-patch count and oldest enqueue time; if a projection window cannot advance the projected watermark within the bounded interval, route to the existing resume/snapshot recovery rather than leaving an acknowledged patch invisible.
- [x] 2.10 Add WebSocket freshness detection for Open-but-silent subscriptions and use a bounded recovery-pointer/history probe to confirm or recover an active turn without replaying commands.
- [x] 2.11 Make Reset an interruption barrier: cancel in-flight and queued chat/tool-result requests, send the server interrupt before reset, and reject late responses/events by session, turn, and generation.
- [x] 2.12 Split transport receipt from presentation commit: preserve ordered uncommitted events, ACK and subscribe only from a contiguous committed cursor after Store and viewport acceptance, and retain/replay events that fail before commit.

## 3. Test and validate failure recovery

- [x] 3.1 Add service tests for explicit subscription resync signaling, replay cursor behavior, progress watermarks, and atomic mixed-entry history snapshots.
- [x] 3.2 Add Godot tests for sequence gaps, malformed/rejected patches, projection/render failure diagnostics, replay success, and snapshot fallback without command replay.
- [x] 3.3 Add an end-to-end regression that drops updates after `read_class_docs` during a long Thought/map-agent workflow and verifies later bootstrap approval and tool activity become visible in order.
- [x] 3.4 Run the focused Python and Godot test suites plus `openspec validate fix-transcript-sync-recovery --strict`.
- [ ] 3.5 Perform a manual editor smoke test with a TileMap/TileMapLayer request that uses bounded ClassInfo queries, a long Thought, at least one approval, and a post-approval tool result; confirm no raw API text or visible transcript gap.
- [x] 3.6 Add Godot regression coverage for a long/streaming Thought whose first layout measurement is unstable, and for an error/report transient notice after a virtualized entry; verify the bottom scroll target remains visible.
- [x] 3.7 Add regressions for (a) received watermark ahead of projected/rendered watermark, (b) a stuck pending projection window, (c) an Open-but-silent WebSocket while the server advances, and (d) Reset racing a queued tool result; verify history hydration restores ordered entries without resubmitting a command.
- [x] 3.8 Add a transport-to-viewport commit regression: force a Projector or viewport failure after a packet arrives, assert no ACK advances past the last committed event, reconnect from that cursor, and verify the server replay—not a manual history reload—renders the missing Thought/tool/approval entries once and in order.



## 4. Bound ClassDB facts and visible tool results at the source

- [x] 4.1 Extend `read_class_docs` with bounded `overview`, `search`, `members`, and `constants` query modes, explicit member/constant/result limits, and a structured `class_docs_query_too_large` response with a narrowing hint.
- [x] 4.2 Change the Godot ClassDB reader and front-tool executor to return only the requested signatures/constants; do not enumerate a complete class by default.
- [x] 4.3 Ensure `read_class_docs` facts are ephemeral to the current model step and sanitize session frames, transcripts, history, and WebSocket payloads so no complete ClassDB/API document is saved or sent.
- [x] 4.4 Do not introduce a common byte ceiling for visible tool-result patches; preserve existing per-tool display boundaries while keeping ClassDB queries bounded.
- [x] 4.5 Update map/programming agent guidance and Godot authoring skills to search first when necessary and then request only the exact API members required for the intended action.
- [x] 4.6 Render a successful ClassDB query as exactly `ClassInfo <class_name>`; do not append member counts, byte counts, API JSON, or a detail expander.
- [x] 4.7 Add service and Godot tests for bounded member search, oversized-query rejection, no durable/raw ClassDB payload, and the exact ClassInfo title.
- [x] 4.8 Re-run the ClassInfo regression with a legacy TileMap authoring request; assert Thought/approval entries remain visible in order.

## 5. Eliminate recursive search payloads before they reach the UI

- [x] 5.1 Define source/config eligibility and runtime-directory exclusions for `grep_code`; normalize every match at the tool boundary to path, line, bounded excerpt, truncation metadata, and counts. Ensure the normalized representation—not raw matches—is what reaches the model, transcript, history, and server-tool summary.
- [x] 5.2 Add a terminal `transcript_patch` serialized-byte budget in server publication. For an oversized resolved/failed tool patch, emit only a safe metadata summary or typed `resync_required`; do not add a global 4 KiB visible-tool-result cap.
- [x] 5.3 Add a front-end WebSocket packet-size guard before JSON parsing. Route rejection through the existing typed diagnostics and resume/snapshot recovery path, without storing packet text.
- [x] 5.4 Update map-agent/RAG execution guidance so editor-context paths and RAG file candidates are read directly before any search; fallback search must use eligible source/config globs rather than `**/*`.
- [x] 5.5 Add service regressions with a giant `logs/service.log` line and a broad `grep_code`: assert the log is not scanned, no raw line reaches the model/transcript/WebSocket, and source/config matches retain bounded excerpts.
- [x] 5.6 Add WebSocket and Godot regressions for an oversized terminal tool patch: assert the main thread never calls JSON parsing for the packet, the event is not committed, and replay/snapshot recovers ordered later Thought/tool/approval entries.
- [ ] 5.7 Perform a manual editor smoke test for a map edit with `map_layouts/ground_extension.json` present and a large `logs/service.log`; verify the agent reads the known layout, subsequent Thought/approval render live, and no panel remains at `Grep · map-agent …`.
