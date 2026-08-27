## ADDED Requirements

### Requirement: Model context is independent from visible transcript history
The service SHALL build each LLM request from a model-context projection rather than from every visible transcript entry. The projection MUST include the active frame system prompt, current user request, current Markdown editor-evidence manifest, protected pending tool protocol groups, a named system-layer Markdown conversation/tool-memory block, and configured recent complete turns. The Markdown memory block MUST be placed outside the ordered assistant-tool protocol sequence. It MUST NOT include user-visible Thought, transcript-only progress/approval/verification entries, WebSocket replay payloads, or a raw editor-context JSON payload unless they are represented as an explicit bounded conversation fact.

#### Scenario: Visible Thought does not enter a later model request
- **WHEN** a completed user-visible Thought exists in the authoritative transcript before the next user message
- **THEN** the next LLM request excludes the Thought content while the transcript/history response continues to contain that Thought entry

### Requirement: Tool results are Markdown from their first model use
The service SHALL render every tool return value as a Markdown string before it enters LLM context, including results in the active protocol window. It MUST NOT serialize a tool return object as JSON in `role=tool.content`. Each Markdown result MUST identify the tool, relevant target/source, outcome, and freshness or verification state; it MAY use lists, tables, and fenced code blocks. The service MUST NOT introduce a per-tool-category Markdown length cap under this requirement; existing safety restrictions for inherently unbounded source material remain applicable.

#### Scenario: Current tool result contains no raw JSON object
- **WHEN** a tool returns a structured result during an active user turn
- **THEN** its matching `role=tool` message contains Markdown and no serialized raw result object

#### Scenario: Structured tool result preserves useful semantic fields
- **WHEN** a structured tool returns successful domain facts
- **THEN** its Markdown result contains the fields required to act on those facts and does not reduce the result to transport status alone

### Requirement: Tool protocol groups are temporary and retained atomically
The service SHALL identify an OpenAI tool protocol group as an assistant message containing one or more tool calls plus all matching `role=tool` messages. It MUST preserve an incomplete or pending group unchanged. Cancellation, rejection, timeout, reset, or equivalent terminal outcome MUST add a matching Markdown `role=tool` terminal result before the group can be compacted or removed. During an active user turn it MUST retain no more than the configured active-group-window value (default 12) of completed groups in their OpenAI-protocol form. The assistant `tool_calls` fields remain structured only while their matching results must be retained for protocol validity.

#### Scenario: Completed tool group is consolidated without orphaned calls
- **WHEN** a thirteenth completed tool group is produced in one active user turn
- **THEN** the oldest complete protocol group is removed without leaving an orphaned tool call or result, and its Markdown result remains in current-turn tool memory

#### Scenario: Pending front tool result remains protocol-valid
- **WHEN** the model requests a front tool and the client has not returned every pending result
- **THEN** context retention MUST preserve the assistant tool-call message and MUST NOT consolidate or reorder any part of that pending group

#### Scenario: Timed-out tool group becomes protocol-valid before retention
- **WHEN** a requested tool times out before returning its normal result
- **THEN** the service appends a matching Markdown terminal result, completes the protocol group, and only then applies normal group retention or consolidation

#### Scenario: Current evidence is not duplicated beside its protocol group
- **WHEN** a completed tool protocol group remains in the outgoing request
- **THEN** the matching current-turn evidence content is not also injected through the conversation-memory block

### Requirement: Tool results persist as Markdown tool memory
The service SHALL merge completed tool results into Markdown tool memory instead of discarding their information. The resident memory MUST preserve readable tool outcome, relevant target/source, freshness/verification state, evidence id, locator, source fingerprint when available, and optional sidecar reference; it MUST NOT retain complete evidence bodies merely because they were observed. At successful user-turn completion, every raw OpenAI tool-protocol group from that turn MUST be removed from the next turn's model context after its Markdown result has been mechanically merged into durable tool memory. Repeated records are merged by target and freshness only when projected overall context compaction is required. If one Markdown result cannot fit the remaining hard context window, the memory MUST retain an identity-bearing bounded range/continuation record rather than an unbounded result.

#### Scenario: Old file read remains readable without a JSON object
- **WHEN** an old `read_file` result is consolidated
- **THEN** later model context retains a Markdown section with its path, bounded location/symbol information, useful bounded source excerpt when available, and freshness state but excludes a raw result JSON object

#### Scenario: Unknown tool uses bounded Markdown
- **WHEN** a completed result has no dedicated Markdown renderer
- **THEN** the service retains a bounded generic Markdown outcome section and never carries an unbounded raw payload or serialized result object into long-term model context

#### Scenario: Next user turn has tool information but no tool protocol
- **WHEN** a user turn with completed tool calls reaches its final assistant response and the user submits the next message
- **THEN** the next LLM request contains the merged Markdown tool memory but none of the preceding turn's `assistant.tool_calls` or `role=tool` protocol messages

#### Scenario: Bounded ClassDB query remains in tool memory
- **WHEN** `read_class_docs` returns a bounded query result and the user turn completes
- **THEN** durable Markdown tool memory retains the queried class, query mode, and bounded returned members, constants, or search results without retaining a complete ClassDB document

#### Scenario: Oversized source result remains recoverable within the hard context window
- **WHEN** a single tool result is larger than the remaining model-context window
- **THEN** the context retains a Markdown record identifying its source and retained range or continuation, and the omitted range is available only through a bounded follow-up read

#### Scenario: Child-frame tool memory reaches the parent
- **WHEN** a delegated child frame completes after producing durable or current-turn Markdown tool memory
- **THEN** its parent-facing delegation result includes the relevant Markdown memory before the child frame is removed

### Requirement: Evidence retrieval is source-aware and selector-based
The service SHALL represent tool, editor, and RAG evidence with Markdown facts plus a source-specific locator. It SHALL expose a structural manifest before requiring a complete source payload by default. A locator MUST identify an exact retrievable part appropriate to the source, including code symbol/line range/match context, configuration key or JSON path, scene node/property/resource, map layer/bounds, log error id/time window, or runtime node/property. A model request for further detail MUST retrieve only the selected bounded evidence.

`read_file` SHALL remain a precise reader for an already located ordinary text range. When an otherwise requested text range contains a generated, structured, minified, opaque, or exceptionally long physical line, the service MUST return an identity-bearing Markdown notice and supported follow-up locator instead of exposing the entire physical line as source text.

#### Scenario: Code detail follows a discovered symbol
- **WHEN** an inspect/search result identifies a code symbol and the model needs its implementation
- **THEN** the follow-up read returns the selected symbol or bounded location rather than the complete file by default

#### Scenario: Generated scene field is not read as ordinary text
- **WHEN** a `.tscn` or comparable structured source contains a large serialized `Packed*Array` or `tile_data` field in a requested line range
- **THEN** the model receives a Markdown locator and supported semantic follow-up rather than the complete serialized field

### Requirement: Hybrid session-scoped Markdown evidence storage
The service SHALL use the selected hybrid evidence storage policy. Reproducible project/RAG/search sources MUST retain only Markdown facts, locator, freshness, and source fingerprint in session state and MUST be re-read from the current source when detail is needed. Volatile or non-repeatable front-end/editor observations, unsaved runtime state, debugger/profiler snapshots, diagnostics, and command output MUST be normalized to Markdown and persisted in a session-scoped sidecar; resident state MUST retain only an evidence index, summary, locator, freshness, content hash, and sidecar reference. Sidecars MUST NOT store raw tool-result JSON, MUST content-hash deduplicate identical normalized content, and MUST be removed when their session is reset or deleted.

#### Scenario: Reproducible file result does not occupy resident evidence memory
- **WHEN** a project file result has been recorded as evidence
- **THEN** session memory retains its facts, locator, and source fingerprint without retaining the complete file body

#### Scenario: Volatile diagnostic remains recoverable after memory eviction
- **WHEN** a debugger, runtime, front-end, or command observation completes
- **THEN** its normalized Markdown evidence is recoverable from the session sidecar while resident memory contains only its index and summary

### Requirement: Selection-dependent map discovery has a target-path fallback
The service SHALL describe `describe_tilemap_selection` as a selection-dependent front tool
that is valid only when the editor has selected a `TileMapLayer`. A map agent MUST NOT use
that tool to discover an unknown target. When editor context does not establish an eligible
selection, or the target is legacy `TileMap` or `GridMap`, the map agent SHALL confirm the
node path from scene facts and call `describe_map_region` with that explicit `target_path`.

#### Scenario: No TileMapLayer is selected
- **WHEN** a map agent needs map facts and editor context does not identify a selected `TileMapLayer`
- **THEN** it confirms the map node path and uses bounded `describe_map_region(target_path=...)` instead of calling `describe_tilemap_selection`

#### Scenario: Selection tool reports no eligible selection
- **WHEN** `describe_tilemap_selection` returns the known no-selection error
- **THEN** the resulting Markdown tool outcome states that selection is required and the next map-discovery attempt uses the target-path fallback rather than retrying the same zero-argument call

### Requirement: Map-region results are semantic Markdown evidence
The service SHALL render a successful `describe_map_region` result as semantic Markdown evidence rather than a transport-status-only record. The rendered evidence MUST include target, map type/dimension, selected legacy layer when applicable, coordinate basis, node position, tile/cell size, requested and observed bounds, truncation/next-query metadata, legacy layer metadata when returned, observed tile identities, and compact row runs. It MUST NOT expose raw front-end result JSON or serialized `.tscn` `tile_data`. Per-cell detail MUST appear only for an explicitly bounded exact-cell observation; compact row runs are the default terrain representation.

#### Scenario: Successful region observation gives terrain facts to the model
- **WHEN** `describe_map_region` returns a bounded 2D or 3D observation with row runs
- **THEN** the matching `role=tool` Markdown lets the model identify the observed bounds and tile/cell identities without reading the map's serialized source data

#### Scenario: Map evidence is refreshed before mutation
- **WHEN** a map mutation depends on an earlier observed region
- **THEN** the service or workflow obtains a fresh bounded observation of the relevant target/layer/bounds before treating the map facts as current write evidence

### Requirement: Editor context is normalized to current evidence
The service SHALL normalize the current editor-context transport payload into a Markdown editor-evidence manifest before model projection. The active model request MUST receive current selection, scene/node identity, relevant diagnostic counts, known files, engine/language facts, and supported front-tool locators as applicable, but MUST NOT receive the complete raw editor-context JSON payload. The raw payload remains available to the frontend/transcript contract. Newer facts for the same target identity MUST supersede older facts.

#### Scenario: Repeated selection snapshots do not accumulate
- **WHEN** successive user requests provide editor context for the same scene and selected node
- **THEN** model context contains the latest Markdown scene/node evidence and excludes the raw editor JSON snapshots

### Requirement: Structured conversation state is bounded and auditable
The service SHALL persist Markdown conversation and tool memory with distinguishable goals/constraints, decisions, verified facts, completed work, pending work, current targets, evidence locators, and tool outcomes. It MUST emit redacted diagnostics sufficient to report outgoing message count, conservative outgoing-context tokens, retained turn count, protocol-group count, Markdown tool-memory count, and evidence-reference count without logging prompts, full tool results, sidecar bodies, or Thought content.

#### Scenario: Context audit exposes compaction without content leakage
- **WHEN** a request consolidates older turns or tool groups
- **THEN** the service records a redacted audit with counts and identifiers, and the audit contains no unbounded message body, editor JSON, tool payload, or Thought text

#### Scenario: Debug logging does not dump event or model payloads
- **WHEN** the service runs with application logging at DEBUG level
- **THEN** internal event records contain only structural metadata, and OpenAI/HTTP dependency loggers do not emit complete outgoing LLM requests, Thought text, editor JSON, or tool-result payloads

### Requirement: Semantic consolidation occurs only at a compaction boundary
The service MUST NOT invoke an additional summarization model solely because a user turn with tools completed. Before every provider request, the service SHALL calculate a provider-compatible or conservative upper-bound token count for the exact projection including system layers, tool schemas, RAG, editor evidence, conversation/evidence memory, ordinary messages, and protected tool groups. If the result exceeds the configured budget, it MUST first remove prompt detail recoverable through an evidence locator while retaining its fact/reference, then invoke the configured quick-model semantic consolidation for older Markdown memory or fall back to deterministic mechanical merging. If the projection still cannot fit, the service MUST fail safely and MUST NOT send the request.

#### Scenario: Ordinary successful tool turn avoids an extra model call
- **WHEN** a user turn with completed tool calls finishes and its projected next-turn context remains within budget
- **THEN** the service mechanically merges its Markdown tool results and does not make a summarization-model request

#### Scenario: Overall context budget triggers semantic consolidation
- **WHEN** the projected model context exceeds its configured token budget before an LLM request
- **THEN** the service removes locator-recoverable prompt detail, invokes semantic consolidation for older Markdown conversation/tool memory or falls back to deterministic mechanical merging, and rechecks the complete outgoing projection before sending it

#### Scenario: Tool schemas count toward the hard budget
- **WHEN** the messages alone fit the configured context budget but messages plus available tool schemas do not
- **THEN** the service applies the same evidence-detail reduction and consolidation path before the provider request and never sends the over-budget projection
