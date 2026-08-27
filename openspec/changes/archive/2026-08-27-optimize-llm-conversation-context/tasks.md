## 1. Context model and persistence

- [x] 1.1 Define persisted frame-owned Markdown conversation/tool memory models, including record identity, source, freshness, verification, and range/continuation fields, without new per-tool-category length caps.
- [x] 1.2 Add session/frame serialization for the new Markdown conversation/tool-memory format and child-to-parent delegation-memory transfer.
- [x] 1.3 Add system-layer Markdown renderers for bounded conversation and tool memory without mutating transcript entries or persisted raw prompt-cache markers.

## 2. Protocol-safe message grouping

- [x] 2.1 Implement a pure OpenAI-message grouping helper that recognizes user turns, assistant tool calls, matching tool results, terminal cancellation/rejection/timeout/reset results, and pending/incomplete groups.
- [x] 2.2 Add validation that every projected LLM request has no orphaned tool call or tool result and preserves pending front-tool groups unchanged.
- [x] 2.3 Replace message-count retention boundaries with complete user/assistant-turn retention, a configurable active-turn protocol-window setting (default 12), and a protected current-request/pending-group boundary.

## 3. Markdown tool memory and context normalization

- [x] 3.1 Implement Markdown renderers for file reads, search results, mutations, scene/node operations, system commands, and generic unknown tools; `role=tool.content` must never contain serialized result JSON or gain a new per-tool-category length cap.
- [x] 3.2 Merge every completed tool result into current-turn Markdown tool memory; when the 12-group window overflows, remove the oldest protocol group atomically while preserving its Markdown section.
- [x] 3.3 At final turn completion, mechanically merge current-turn Markdown tool memory into durable tool memory and remove all completed OpenAI tool-protocol messages from the next turn's context without an extra summarization-model call.
- [x] 3.4 Normalize historical editor-context payloads into replaceable current editor facts while preserving the full context for the active user request.
- [x] 3.5 Render only exact bounded `read_class_docs` members, constants, or search matches (including overview subsets) into Markdown tool memory while retaining the prohibition on complete ClassDB documents.
- [x] 3.6 Convert a single result that exceeds the remaining hard model-context window into an identity-bearing Markdown range/continuation record with bounded follow-up retrieval.

## 4. Prompt projection and compaction

- [x] 4.1 Build outgoing LLM messages from system layers, a named Markdown conversation/tool-memory system block, recent user/assistant turns, protected active-turn tool groups, and the current request; prevent the memory block from splitting an assistant-tool protocol sequence.
- [x] 4.2 Update automatic and manual compaction to merge older turns and Markdown tool records by identity and freshness instead of retaining raw message previews or tool JSON; invoke the quick model only at overall-budget or explicit manual compact boundaries, with a deterministic mechanical fallback.
- [x] 4.3 Preserve existing prompt-cache breakpoint behavior for layered system context and invalidate cache state when projected context changes.
- [x] 4.4 Add settings for overall model-context budget, retained complete-turn count, active-group window (default 12), and quick-consolidation model/feature behavior; provide deterministic mechanical Markdown fallback when semantic consolidation fails or is disabled.

## 5. Transcript isolation and observability

- [x] 5.1 Ensure prompt compaction never mutates, deletes, reorders, or omits authoritative transcript/history entries.
- [x] 5.2 Emit redacted context-audit diagnostics for outgoing message count, estimated tokens, retained turns, temporary tool protocol groups, and Markdown tool-memory records.
- [x] 5.3 Verify diagnostics never include prompt text, editor JSON, Thought content, or full tool-result payloads.

## 6. Tests and verification

- [x] 6.1 Add unit tests for grouping, terminal tool outcomes, protocol validation, Markdown tool rendering without per-category caps, range/continuation records, editor-state supersession, and overall-budget compaction.
- [x] 6.2 Add orchestration tests showing Thought and transcript-only entries are excluded from later LLM requests while transcript history remains complete.
- [x] 6.3 Add integration tests proving current `role=tool.content` is Markdown rather than result JSON, the thirteenth group remains in tool memory, completed protocol messages do not enter the next turn, terminal front/server tool outcomes, delegated-frame memory transfer, hard-window range continuation, and prompt-cache markers remain valid.
- [x] 6.4 Run the focused service test suite and the full relevant regression suite; record token/context audit assertions for representative long conversations.

## 7. Review remediation

- [x] 7.1 Replace heuristic projected-token accounting with a provider-compatible or conservative pre-request hard gate that includes system layers, tool schemas, RAG, editor evidence, memory, messages, and protected groups; reapply it after every reduction or consolidation.
- [x] 7.2 Preserve every normal-turn tool record and its parsed tool arguments until the overall-budget compaction boundary performs identity/freshness consolidation.
- [x] 7.3 Enforce protocol validation before a model request, persist terminalization of stale tool groups, and prevent raw JSON strings from bypassing the Markdown renderer.
- [x] 7.4 Include source, freshness, and verification metadata in first-use tool Markdown and persisted tool records.
- [x] 7.5 Add exact-boundary and regression coverage for the remediation, then obtain a green focused and full relevant test suite.
- [x] 7.6 Keep DEBUG diagnostics structural: event logs must not serialize payload bodies, and OpenAI/HTTP dependency request dumps must be suppressed.
- [x] 7.7 Set the default projected model-context budget to 200,000 estimated tokens, aligned with the existing mechanical early-cleanup threshold.
- [x] 7.8 Document and implement target-path fallback for selection-dependent map discovery; avoid repeated zero-argument `describe_tilemap_selection` calls when no `TileMapLayer` is selected or the target is legacy `TileMap`/`GridMap`.

## 8. Evidence-controlled retrieval and hybrid storage

- [x] 8.1 Add persisted evidence-index models (id, source kind, target/locator, facts, freshness/fingerprint, content hash, and optional sidecar reference) while ensuring resident session state never contains complete evidence bodies by default.
- [x] 8.2 Implement the selected hybrid Markdown evidence store: retain reproducible project/RAG/search sources as locator-plus-fingerprint records; persist volatile front-end/editor, runtime, diagnostic, and command evidence as session-scoped Markdown sidecars; content-hash deduplicate and clean sidecars on session reset/deletion.
- [x] 8.3 Normalize the active editor transport payload and RAG candidates into Markdown evidence manifests with supported locators before model projection; remove the raw current editor JSON from LLM messages without changing frontend/transcript contracts.
- [x] 8.4 Add source-aware inspect/select retrieval contracts for code, structured configuration, scenes/resources, logs/diagnostics, and runtime state; make `read_file` return a locator/continuation notice for generated, minified, opaque, or exceptionally long physical lines instead of returning the full line.
- [x] 8.5 Render and persist `describe_map_region` as semantic Markdown evidence containing target/layer, coordinate basis, observed bounds, tile identities, and compact row runs; omit raw JSON and serialized map payloads, and expose exact per-cell detail only for bounded explicit observations.
- [x] 8.6 Update projection so retained raw tool protocol groups and current evidence are not double-injected; reduce locator-recoverable detail before semantic compaction, then apply the hard outgoing budget before every agent-loop provider request.
- [x] 8.7 Add focused tests for evidence sidecar lifecycle/deduplication, reproducible-source references, editor JSON exclusion, selector-based retrieval, long generated-line protection, semantic map-region Markdown, duplicate-free projection, and tool-schema-inclusive hard-budget enforcement; run focused and full relevant regressions.
