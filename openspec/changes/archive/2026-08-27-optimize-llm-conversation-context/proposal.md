## Why

The service currently sends almost every persisted OpenAI message in an active agent frame back to the LLM until a very high token threshold is reached. This retains stale editor-context JSON and already-consumed tool-result JSON long after they cease to be useful, increasing latency and cost while making later turns less focused.

The visible transcript must remain complete and recoverable, but the model needs a compact, current, actionable context rather than a replay of UI history and raw protocol payloads.

## What Changes

- Add a model-only conversation-context policy that separates durable user-visible transcript history from LLM prompt context.
- Replace message-count-only retention with complete-turn and complete tool-protocol-group retention, so an assistant tool call and its matching tool results are never separated.
- Render every tool result as Markdown before it enters LLM context; never serialize a tool return object as raw JSON in a model message.
- Introduce source-aware, selector-based evidence retrieval: the model first receives a small source manifest and task-relevant evidence, then requests an exact symbol, range, property, node, map region, log cluster, or runtime field instead of reading complete source payloads by default.
- Keep evidence out of the resident model context by using a hybrid store: reproducible project/RAG sources retain only Markdown facts, locators, and fingerprints; volatile front-end, runtime, diagnostic, and command outcomes retain normalized Markdown in session sidecars while memory holds only their index and summary.
- Replace the current full editor-context JSON injection with a current Markdown editor-evidence manifest and on-demand locators; preserve the frontend/transcript contract without treating the current raw payload as model context.
- Make the outgoing-context budget a pre-request hard gate that counts system layers, tool schemas, RAG, memory, messages, and protected protocol groups before every provider request.
- Retain at most 12 complete OpenAI tool-protocol groups during one active user turn. When the window overflows or the turn completes, remove the protocol groups while merging their Markdown results into the current-turn or durable tool memory.
- Inject durable Markdown conversation/tool memory as a named system-layer block, outside the ordered assistant-tool protocol sequence; pending, cancelled, timed-out, or rejected tool groups are terminalized before they can leave that sequence.
- Preserve a delegated child frame's Markdown memory by folding it into its parent-facing delegation result when the child frame ends, and use bounded continuation/range records for a single result that cannot fit the remaining context budget.
- Merge Markdown tool memory with a quick model only when the projected overall context exceeds its token budget or the user explicitly runs compact; ordinary successful tool turns do not trigger an extra model call.
- Do not impose new per-tool-category Markdown length caps; apply compaction only against the projected overall context budget while retaining existing safety bounds for inherently unbounded sources.
- Preserve bounded `read_class_docs` query results as Markdown tool memory rather than replacing all queried API facts with an opaque expiry placeholder.
- Normalize prior editor-context payloads into current, durable editor facts instead of retaining repeated historical JSON blobs.
- Evolve compact summaries into structured Markdown conversation and tool memory containing goals, constraints, decisions, verified facts, completed work, pending work, current targets, and readable tool outcomes.
- Continue excluding model reasoning/Thought from LLM context while preserving it in the authoritative visible transcript.
- Add context-audit diagnostics and tests that prove later requests exclude stale payloads without breaking OpenAI tool-call protocol validity.
- Keep debug diagnostics structural: event logs and third-party SDK/HTTP logs must not dump Thought text, tool payloads, or complete outgoing LLM requests.
- Make selection-dependent map discovery explicit: `describe_tilemap_selection` is only valid for an editor-selected `TileMapLayer`; map agents must use a confirmed `target_path` with `describe_map_region` when selection is absent or the target is legacy `TileMap`/`GridMap`.
- Render `describe_map_region` as semantic Markdown evidence (target/layer, coordinate basis, bounds, tile identity and row runs) so map agents receive usable region facts without raw serialized `tile_data`; do not silently reduce a successful map observation to `ok: true`.

## Capabilities

### New Capabilities

- `llm-conversation-context`: Builds, persists, compacts, and audits a token-efficient model context independent of the complete visible transcript.

### Modified Capabilities

- `authoritative-chat-transcript`: Clarify that preserving complete visible transcript entries does not require preserving their raw payloads in subsequent LLM requests.

## Impact

- Affects session/frame persistence, session-scoped Markdown evidence sidecars, prompt construction, editor/RAG ingestion, query orchestration, automatic compaction, tool-result handling, map-agent tool guidance, and context-cache decisions in `ai_agent_service`.
- Preserves HTTP, WebSocket, transcript, and history API contracts for the Godot frontend.
- Adds no external dependency or public endpoint requirement; session persistence adopts the new conversation-context format directly, with the overall context budget, retained complete-turn count, and active-group window exposed as settings (default active-group window: 12).
