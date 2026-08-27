## Context

`Session.agent_stack[*].messages` currently serves both as the OpenAI-compatible model history and as the source from which historical UI views can be reconstructed. Before automatic compaction, it retains all user messages, assistant messages, tool calls, and serialized tool results. The only short-lived exception is `read_class_docs`; visible Thought already lives only in the transcript/event path.

The result is a mixed-purpose message list: durable UI history is correct and complete, but prompt history accumulates stale editor snapshots and verbose JSON results. The existing 200k-token automatic compaction retains a fixed recent message count and summarizes older messages, which can split a logical tool protocol group and does not model fact freshness.

The design must preserve the authoritative transcript, existing session persistence, OpenAI tool-call ordering, prompt-cache compatibility, active delegated frames, and current HTTP/WebSocket contracts. It must also avoid making resident session memory a second unbounded copy of project files, editor snapshots, diagnostics, or tool output.

## Goals / Non-Goals

**Goals:**

- Maintain a bounded, actionable model context independently from the complete visible transcript.
- Preserve readable tool information through Markdown tool memory while never placing a raw tool-result JSON object in model context.
- Retain recent history in complete user-turn and tool-protocol groups.
- Make long-lived context explicit: goals, constraints, decisions, verified facts, completed/pending work, current editor targets, and freshness metadata.
- Make actual outgoing model messages inspectable through redacted structural diagnostics and deterministic tests.
- Make source retrieval evidence-led and selector-based so the model receives the smallest useful source fragment, not an opaque or complete payload by default.
- Keep full volatile evidence recoverable without keeping it resident in memory or model context.
- Enforce the 200k model-context budget as a pre-provider-request hard gate.

**Non-Goals:**

- Changing transcript/history API payloads, WebSocket replay, or frontend rendering semantics.
- Persisting or replaying private model reasoning as model context.
- Guaranteeing that a compressed fact replaces a fresh source read; high-risk actions must still re-read current project state.
- Introducing a vector database or cross-session memory.
- Retaining all project files, serialized scene payloads, or tool returns in session RAM merely because they were observed once.

## Decisions

### 1. Split visible transcript from model conversation state

Add persisted frame-owned **Markdown conversation memory** and **Markdown tool memory** beside recent raw messages. These serialize as one named system-layer block and contain goals/constraints, decisions, verified facts, completed work, pending work, current editor targets, readable tool outcomes, and source freshness. “Bounded” means bounded by the projected overall context budget, not by a new per-tool-category length cap.

The authoritative transcript remains complete and is never compacted for prompt purposes. This chooses an explicit model-only state over deriving prompt history from transcript entries, because the latter would reintroduce Thought and UI-only events.

Alternative: retain the current `CompactSnapshot` as unstructured prose. Rejected because it cannot replace/expire individual facts or distinguish current editor state from prior snapshots.

### 2. Treat a tool call and its results as an atomic protocol group

Introduce message-group discovery over OpenAI messages. A group begins at an assistant message with `tool_calls` and ends only after every matching `role=tool` result has appeared. An incomplete/pending group is never compacted, removed, or reordered. A completed group can be replaced only as a whole after its following model request has consumed the results. Cancellation, client rejection, timeout, reset, and similar terminal outcomes MUST first produce a matching Markdown `role=tool` terminal result; the now-complete group then follows the same retention/merge rules.

If an assistant tool-call message contains user-facing text, preserve that text as an assistant fact/answer before removing the protocol group. This avoids orphaning tool messages or losing an assistant commitment.

Alternative: delete only old `role=tool` entries. Rejected because it violates the provider protocol and makes the historical assistant tool call invalid.

### 3. Render results as Markdown and merge them into tool memory

Tool handlers or a context-normalization layer render every result as a Markdown section before appending it to `role=tool.content`. The section identifies the tool, target/source, outcome, freshness/verification state, and result content in lists, tables, or fenced source blocks as appropriate. File reads retain useful source excerpts or code blocks; mutations retain target, applied change, and verification; searches retain query, selected paths, and excerpts; commands retain purpose, status, and diagnostics. This change introduces no artificial per-tool-category Markdown length cap; existing safety bounds for unbounded source material remain in force. If one result cannot fit the remaining hard model-context window, retain an identity-bearing Markdown range/continuation record and fetch the next bounded range on demand; never bypass the hard window by storing an unbounded object.

The assistant `tool_calls` object remains structured only for the duration required by the OpenAI protocol. It is the sole unavoidable structured protocol data; a tool's returned object is never serialized as JSON for the model. Unknown tools receive a generic Markdown section. `read_class_docs` retains only the exact queried members, constants, or search matches (and the query mode) as Markdown tool memory; an overview request must also use an explicitly selected bounded subset and never retains a complete ClassDB document.

The current-turn tool memory is a Markdown document. It contains results from every completed group in the turn: the newest configured active-group-window value (default 12) retain their matching protocol groups, while older groups contribute their existing Markdown sections after the protocol pair is removed. At turn completion, all protocol groups leave model context and the Markdown document merges mechanically into durable tool memory for later turns without an additional LLM request. When a delegated child frame ends, its durable and current-turn Markdown memories are folded into the parent-facing delegation result before the child frame is discarded.

The durable memory is injected into outgoing requests as one named `[conversation_memory]` system-layer block, adjacent to other stable/project/RAG layers and before recent conversational turns. It is never inserted between an assistant `tool_calls` message and its matching `role=tool` messages.

Alternative: retain JSON result objects for the recent protocol window. Rejected because JSON adds protocol noise and token overhead without improving readability; Markdown is valid string content for `role=tool` and preserves the useful result information.

### 4. Treat tool output as source-aware evidence, not default prompt text

Every tool result becomes a normalized Markdown evidence record with a stable evidence id, source kind, target/locator, freshness or source fingerprint, concise facts, and an optional sidecar reference. A locator is the exact way to retrieve more evidence: for example a code symbol or line range, configuration key/JSON path, scene node/property/resource id, map bounds/layer, log error id/time window, or runtime node/property set.

The first result from an inspect/search operation is a manifest or summary containing these locators. The model asks for an exact locator only when its current task needs the detail. `read_file` remains the precise text tool for an already located ordinary text range; it SHALL not become an implicit full-document/large-physical-line reader. A structured, generated, or machine-dense source (for example a long `Packed*Array`, `tile_data`, minified document, or opaque diagnostic dump) yields an identity-bearing Markdown notice and supported follow-up selectors rather than the complete physical line.

This is a source-semantics policy rather than a new per-tool-category Markdown length cap. The first model-visible representation must be structurally useful and retrievable; a later exact selector is allowed to return the specific bounded evidence necessary to finish the task.

Alternative: rely only on agent prompt wording to request smaller reads. Rejected because prompt guidance cannot make line paging safe for one-line generated data and cannot guarantee that all tool renderers expose their useful fields.

### 5. Use a hybrid, session-scoped Markdown evidence store

The selected storage policy is hybrid:

- Reproducible sources (project files, resources that can be read again, RAG/search candidates) retain in the session only Markdown facts, locator, source fingerprint, and freshness. Their complete content is re-read from the current source when needed.
- Volatile or non-repeatable sources (front-end/editor observations, unsaved runtime state, debugger/profiler snapshots, diagnostics, and command output) write normalized Markdown evidence to a session-scoped sidecar. Resident session state keeps only the evidence index, summary, locator, freshness, content hash, and sidecar path.
- Sidecars contain Markdown rather than raw tool-result JSON. Identical normalized content is content-hash deduplicated. They are deleted with a reset/deleted session and are not cross-session memory.

Map observations are volatile evidence. A `describe_map_region` record stores its target path, map layer, observed bounds, scene/editor freshness marker, and semantic Markdown. Before any map mutation the relevant bounds MUST be observed again; previous sidecar evidence can explain prior reasoning but cannot establish current write facts.

Alternative: RAM-only LRU. Rejected because it recreates memory growth and loses evidence after restart. Alternative: write every reproducible file body to disk. Rejected because a locator plus fingerprint preserves recoverability without duplicating the project on disk. Alternative: add SQLite/vector storage. Rejected as unnecessary complexity and cross-session scope expansion.

### 6. Normalize editor context into replaceable current evidence

Before prompt projection, normalize the current editor payload into a Markdown editor-evidence manifest: current selection, scene/node identity, relevant diagnostics count, known files, engine/language facts, and supported front-tool locators. The raw transport payload remains available to frontend/transcript handling but is not copied into the model user message. Facts with the same scene/node/file identity are overwritten by newer evidence.

Alternative: retain every editor JSON snapshot until global compaction. Rejected because it repeats transient inspector state, can be arbitrarily large even in the active request, and makes current targets ambiguous.

### 7. Retain recent complete turns, then compact by a hard outgoing budget

Replace fixed "recent messages" retention with configurable complete user/assistant-turn retention plus a token budget. Prior completed turns contain no raw tool protocol groups; their tool information resides in durable Markdown tool memory. Within the active turn, the system preserves a configurable active-group window (default 12) and folds older groups into current-turn Markdown memory. If the projected overall context exceeds its budget, automatic compact invokes the quick model to merge older Markdown conversation/tool memory by target and freshness; manual compact invokes the same path on demand. No per-tool-category Markdown cap triggers this work. The system prompt, current request, and any pending tool group are protected. The settings also select the quick-consolidation model/feature behavior; disabling it uses mechanical merging only.

Before **every** provider request in an agent loop, calculate a provider-compatible or conservative upper-bound token count over the exact outgoing projection: system layers, tool schemas, RAG, editor evidence, conversation/evidence memory, ordinary messages, and protected protocol groups. If it exceeds the configured limit, first evict prompt detail that has an evidence locator while retaining the fact card and reference; then consolidate older Markdown memory. If it still exceeds the limit, the service fails safely instead of sending an over-budget request. The count is rechecked after every compaction/consolidation.

Normal-turn records are never deduplicated merely due to a count window; only an overall-budget compaction may merge records by identity and freshness, after preserving parsed tool arguments needed to identify their targets. A projection with a protocol violation is never sent to the provider: stale terminalization is persisted first, and any remaining violation fails the turn safely. Matching current-turn evidence MUST NOT be injected into the memory block while its same result remains in a retained raw protocol group, preventing a second model-visible copy.

The pre-existing raw-message threshold may still initiate a **mechanical** early cleanup for anomalously large histories, but it never authorizes quick-model semantic consolidation; that model remains limited to projected-budget overflow and explicit manual compact.

The default projected context budget is 200,000 estimated tokens. It matches the legacy
mechanical early-cleanup threshold while keeping their distinct roles: projected-budget
overflow controls semantic consolidation, whereas the legacy threshold can only cause
mechanical cleanup.

### 8. Keep diagnostics structural even at DEBUG level

Context audits and internal event logs serve different debugging needs. Context audits retain
only counts and identities. Generic event emission logging likewise records only the event
type and top-level payload keys: it must never serialize a transcript patch, cumulative
Thought text, tool-result summary, editor JSON, or other nested payload to the service log.
The original event object remains unchanged for persistence and WebSocket delivery.

The OpenAI SDK and its HTTP dependencies are pinned to `WARNING` even when the application
logger is at `DEBUG`, because their DEBUG records can serialize complete outgoing requests.
This is both a log-volume guard and a prompt/tool-payload confidentiality boundary.

### 9. Make map selection an optional accelerator and map regions semantic evidence

`describe_tilemap_selection` has no target-path parameter and is implemented by the Godot
front end against the currently selected `TileMapLayer`; it cannot discover a map node and
does not cover legacy `TileMap` or `GridMap`. Map-agent guidance must therefore choose it
only when the current editor state explicitly identifies a selected `TileMapLayer`.

Otherwise the agent first confirms a node path from scene facts and issues bounded
`describe_map_region(target_path=...)`. The same fallback applies after the selection tool
returns its known `Select a TileMapLayer first` error, preventing repeated zero-argument
calls. The error remains a normal Markdown tool result so the OpenAI protocol stays valid.

`describe_map_region` is already a bounded front-end observation; its Markdown renderer MUST preserve the semantic fields that an agent needs: target/type/dimension/layer, coordinate basis, node position, tile/cell size, requested and observed bounds, truncation and next-query metadata, legacy layer metadata, tile identities, and compact row runs. It MUST render a successful observation as map evidence rather than only `ok: true`, and it MUST NOT expose serialized `.tscn` `tile_data` or front-end result JSON. Per-cell detail is only included when the requested bounded region needs exact cell inspection; row runs are the default representation for terrain shape and reuse of real tile identities.

## Risks / Trade-offs

- [A Markdown tool record is stale or omits a detail] → attach source/freshness metadata, merge records by target, preserve useful excerpts, and require fresh reads before edits or high-risk actions.
- [Grouping bug leaves an invalid OpenAI tool sequence] → use one shared group parser for retention, compaction, cancellation, and tests; assert every retained call has matching result(s).
- [Context becomes larger due to duplicate state and raw turns] → deduplicate records by identity during overall-budget compaction and measure outgoing message/token count; do not impose new per-tool-category Markdown caps.
- [Tool-specific extractors are incomplete] → use bounded generic facts and introduce extractors incrementally with tool-level tests.
- [A cancellation or client-side terminal path leaves a pending tool call] → synthesize the matching terminal Markdown result before retention/compaction and validate the resulting group.
- [A child-frame result is lost when its frame is popped] → merge child Markdown memory into the parent delegation result before disposal.
- [One otherwise-valid result exceeds the hard remaining context window] → retain an identity-bearing range/continuation record and require a bounded follow-up read.
- [A compact renderer hides the facts that made a tool useful] → define semantic Markdown fields and renderer tests for every structured high-value source; successful map observations must expose bounds and row runs, not just transport status.
- [Evidence sidecars grow without bound] → store only volatile normalized Markdown, keep reproducible sources as locators, content-hash deduplicate, and remove sidecars when a session is reset or deleted.
- [The local token estimate is lower than provider accounting] → include tool schemas and all projection layers in a conservative pre-request count, reserve margin, and fail safely if the hard gate cannot make the request fit.

## Open Questions

- Which edit verification outcomes are sufficient to promote a mutation fact from observed to verified?
- Should user-visible assistant text emitted alongside tool calls become a separate durable assistant fact or be attached to the tool-group summary?
