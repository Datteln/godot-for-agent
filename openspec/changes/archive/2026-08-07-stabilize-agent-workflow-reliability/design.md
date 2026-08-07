## Context

The service persists a complete `MapTaskState` inside the Session while retaining at most 512 reducer events in that state. The reducer and tests describe those events as replayable, but slicing destroys required history and event ids derive from bounded list length. A compatibility migration would preserve both representations and force every future read, write, recovery, and compaction path to reason about two authorities.

Semantic Verify represents every outcome with `passed: bool`; read errors, provider exhaustion, malformed model output, missing Frames, and exhausted retries can be recorded as success or skipped. A compatibility projection would retain the ambiguous field that caused the defect.

The OpenAI-compatible provider combines SDK retries, stream reconnections, and outer attempts. A reconnect after partial output starts a different completion even though provisional output may already be visible. Provider endpoints do not share one portable idempotency contract.

The Godot client submits work over HTTP and polls `/chat/events`. Event semantics already include epoch, sequence, provisional preview identity, commit/discard boundaries, and bounded rendering. The polling implementation lives in `agent_http_client.gd`, while ChatPanel also mutates client turn state and owns event acceptance and presentation concerns. Rendering itself is fragmented: live text updates temporary log messages, reasoning constructs external controls, history becomes `_history_*` pseudo-events with special branches, tool diffs enqueue prebuilt preview nodes, final deduplication depends on text fingerprints, and ChatPanel, MessageStore, NodeFactory, and LogRenderer all make overlapping rendering decisions.

The architecture concentrates unrelated responsibilities in three oversized units: `agent.py` contains a 1,000-line multi-turn driver mixed with Map routing and tool-specific policy; `query/engine.py` combines application commands, atomic submission, recovery, history, and response mapping; and `chat_panel.gd` combines interaction, state transitions, event acceptance, tool approval, history, streaming, scrolling, and rendering. Preserving their existing internal DTOs and entry points would constrain the replacement architecture to the same ownership model.

A post-implementation audit found that the first backend cut removed the old names without completing the ownership transfer. `map_turn_pipeline.py` grew to 5,599 lines and retained a 1,104-line `MapTurnPolicy.run()` containing the model cycle, permission checks, delegation, planning, server/front tool handling, budgets, events, and Frame completion. `application/service.py` remained 4,180 lines, including 642-line user submission, 659-line tool-result submission, and 426-line locked submission methods. The classes in `application/use_cases.py` only forward to `AgentApplication`; `TurnRuntime`, `TurnDependencies`, and several directive variants are defined but do not participate in execution. This is a semantic failure of the intended boundaries, not merely a source-formatting issue, and the affected tasks must be reopened.

The remaining work also includes bounded completed-turn response caching, duplicate runtime composition, repeated project scans, redundant reducer copying, text-keyword Map routing, hidden operational constants, incomplete localization, recovery diagnostics, INFO-level inspection logs, line-ending drift, and repository instructions that conflict with required tests.

## Goals / Non-Goals

**Goals:**

- Make `VerifyOutcome` the only verification contract and expose exact causes plus bounded recovery actions to both LLM and UI.
- Make manifest-selected snapshots and committed event segments the only authoritative Map workflow representation.
- Make authenticated resumable WebSocket the only chat event transport; keep HTTP only for commands and bounded authoritative snapshot recovery.
- Make one canonical Timeline projector, mutation model, store, renderer registry, and VirtualScroller subscription the only path for every visible live or historical chat item.
- Replace the monolithic turn loop with a typed state-machine driver and isolate Map and Verify policy from the generic core.
- Replace `QueryEngine` with application use cases whose transaction and recovery ownership is explicit.
- Delete the giant replacement modules `map_turn_pipeline.py` and `application/service.py`; do not preserve them as import aliases, forwarding facades, or re-export modules.
- Make every retained `TurnDirective`, `TurnRuntime`, dependency port, and application use-case interface participate in the real execution path rather than serving as a decorative contract shell.
- Preserve atomic publication by passing one explicit per-submission scope rather than relying on a process-global publication `ContextVar`.
- Make ChatPanel a presentation component with no transport, Session cursor, or event-ordering ownership.
- Remove presentation identity based on text content and prohibit prebuilt UI nodes, private history pseudo-events, or parallel append paths from bypassing the Timeline store and renderer registry.
- Ensure one provider-owned retry budget maps to a bounded number of wire attempts and never regenerates after accepted partial output.
- Preserve committed tool-result identity for the lifetime of a Session epoch without retaining every full response indefinitely.
- Address every remaining source-analysis finding except cross-session rate limiting and speculative `SessionStore` thread safety.
- Cut backend, frontend, protocols, and persistent schema over together and leave no compatibility reader, projection, feature flag, dual-write, fallback transport, or rollback exporter for the Turn, QueryEngine, Verify, workflow, event-transport, or frontend-state surfaces replaced by this change.
- Allow reviewable implementation steps on a development branch while shipping only the single new architecture.

**Non-Goals:**

- Preserving old internal Python/GDScript entry points, DTO shapes, module paths, or direct test imports.
- Migrating or continuing legacy Sessions that contain the bounded workflow representation.
- Changing HTTP command submission to WebSocket.
- Adding cross-session rate limiting or multi-thread synchronization to `SessionStore`.
- Introducing an external workflow engine, database, message broker, or distributed transaction.
- Treating LLM verification as deterministic proof or automatically rolling back an edit solely because semantic verification is unavailable.
- Maintaining a deployable intermediate version that contains both old and new implementations.
- Splitting files only by line ranges while retaining a central god object, forwarding-only use cases, a generic mutable context bag, or circular handler dependencies.
- Introducing broad copy-on-write state without profiling and explicit ownership tests.

## Decisions

### 1. Verification has one canonical outcome and closed recovery contract

Define one `VerifyOutcome` schema with `schema_version`, `status: passed | failed | unavailable`, `phase`, `reason_code`, `summary`, `issues`, `attempt`, `max_attempts`, `retryable`, and `recovery_actions`. There is no `VerifyOutcomeV2` name, derived `passed` field, legacy payload reader, or persisted boolean projection.

`failed` means an executed verifier found an issue. Unavailable causes include `target_unreadable`, `frame_missing`, `retry_budget_exhausted`, `provider_unavailable`, `response_malformed`, `validator_missing`, and `validator_timeout`. Recovery actions are closed values such as `reread_target`, `rediscover_target`, `run_deterministic_check`, `retry_verifier`, `use_configured_fallback`, and `pause_unverified`, each carrying only validated minimum inputs.

The exact outcome is persisted, emitted to UI history, and appended to the owning Frame as a system message. Agent guidance permits at most one applicable recovery action per unavailable outcome. Attempt identity and budget persist so rephrasing cannot reset the loop. Required verification pauses at a typed unverified checkpoint when exhausted; advisory verification may continue only with explicit unverified evidence and user text.

Legacy boolean payloads are rejected as `unsupported_verify_schema`. They are never guessed into a status because `passed=false` cannot distinguish a defect from an unavailable verifier and historical `passed=true` may not prove that a verifier executed.

### 2. Workflow persistence has one manifest-selected authority

Store each Session epoch's Map workflow under a project-confined directory:

```text
manifest.json
snapshots/snapshot-<sequence>-<digest>.json
segments/events-<first>-<last>-<commit-id>.json
```

Every event receives a strictly increasing `event_seq` from a persisted high-water mark independent of collection length. Segments are immutable canonical JSON documents with schema version, Session epoch, lineage, sequence range, predecessor digest, events, and content digest. A snapshot contains complete reducer-owned state, snapshot sequence, state digest, event schema version, and lineage.

Events produced against an atomic Session working copy remain staged. Coordinated publication prepares the Session payload, event segment, artifacts, and manifest switch under one commit identity. Immutable files are renamed first and the manifest is atomically replaced last. Readers follow only the committed manifest, so orphan preparations are invisible and recoverable.

Startup validates schema, epoch, lineage, digests, sequence continuity, and reducer replay before publishing state. Missing, duplicated, reordered, or corrupt content produces a typed read-only recovery problem and prohibits mutation.

Compaction writes and verifies a complete snapshot at the current high-water sequence, switches the manifest atomically, and only then removes covered segments after proving no active manifest references them. No in-memory or durable event list is sliced before a durable snapshot exists.

The new runtime never reads the legacy embedded workflow state. Loading a legacy Session returns `unsupported_session_schema` and requires a new Session. Legacy files remain untouched only as user-managed backup; there is no baseline converter, dual-read comparison, dual-write period, diagnostic legacy tail, or rollback exporter.

Immutable segments plus a manifest are preferred over append-only JSONL because they avoid partially written lines and provide a clear file-based commit visibility boundary.

### 3. WebSocket is the only event transport

Add an authenticated WebSocket endpoint for downstream events while retaining HTTP for `/chat`, commands, tool results, approvals, reset, history, and bounded authoritative snapshot recovery. The bearer token is supplied in the WebSocket handshake header; credentials never enter URLs or logs.

HTTP chat and tool-result bodies are command acknowledgements, not live presentation inputs. They never insert, patch, finalize, discard, or text-deduplicate a Timeline item, regardless of whether the acknowledgement arrives before or after its matching event. Accepted WebSocket events are the sole live presentation authority. Bounded history and snapshot responses provide canonical event records to the same Timeline projector and do not create a second direct-render path.

The client sends `resume` with `session_id`, `session_epoch`, and `after_seq`. The server responds with `hello` containing protocol version, accepted epoch, high-water sequence, backpressure limits, heartbeat interval, and resume disposition. Ordered `event_batch` frames use cumulative acknowledgements after the client accepts a batch into its ordered UI queue. Delivery pauses at configured unacknowledged count/byte bounds; a stalled connection closes with a typed retryable reason and resumes from its last acknowledgement.

Ping/pong represents transport liveness, while `turn_progress` remains request-correlated application progress. Reset commits a new epoch, emits `epoch_changed` when possible, and closes the old connection. The client adopts the reset acknowledgement before reconnecting and rejects all old-epoch frames.

Reconnect uses capped exponential backoff with jitter and never replays `/chat` or tool results. Sequence gaps, invalid epochs, and cursors older than retained events yield typed `snapshot_required` control messages and bounded authoritative HTTP recovery.

The same cutover deletes `/chat/events`, `_event_http`, polling timers, polling cadence settings, migration flags, and transport fallback code. Unsupported polling settings are rejected rather than migrated. WebSocket is selected over SSE because Godot supports it natively and acknowledgements, resume control, and backpressure are bidirectional.

### 4. One provider layer owns all wire retries

Construct the SDK client with retries disabled. `chat()` owns one configurable total attempt budget including primary and fallback models. A typed retryable failure before any response chunk may consume another attempt. Once any text, reasoning, tool-call fragment, or usage chunk has been accepted, a transport failure returns `partial_stream_interrupted` with the accumulated provisional identity and never invokes another completion automatically.

HTTP 429/5xx, connect timeout, and pre-stream transport failure remain retryable within budget; authentication and request validation are terminal. Endpoint-specific idempotency headers may be enabled only by an adapter whose declared endpoint contract guarantees their semantics.

### 5. Durable completed-turn identities are separate from hot response caching

Persist a compact identity for every committed tool-result turn in the active Session epoch: turn id, canonical fingerprint, outcome kind, durable response/checkpoint locator, and commit digest. Full responses may live in a bounded hot cache, but eviction never removes identity. An identical retry loads or reconstructs the original outcome without side effects; a different fingerprint is rejected. Reset creates a new epoch and makes old identities unreachable.

Legacy cache entries are not migrated. The durable ledger begins with the new Session schema boundary.

### 6. The turn core is a state machine driven by typed directives

Replace `agent.py` and top-level `run_turn()` with a `turn` package. The public application layer submits a `TurnCommand` to `TurnDriver.run()` and receives a canonical `TurnOutcome`. Internal decisions are a closed union:

```text
Continue
FinishFrame
StartDelegate
CreatePlan
DispatchTools
AwaitFrontendTools
PauseWorkflow
Complete
Fail
```

The package separates contracts, runtime state, frame gates, model request construction, model invocation, response classification, tool dispatch, delegation, and event projection. `TurnDependencies` holds read-only ports; `TurnRuntime` holds only per-run counters and temporary resources. Session and Frame remain referenced aggregates. A single mutable all-purpose `StageContext` and per-stage deep copies are prohibited.

`TurnDriver` must execute the closed directive union exhaustively. `ContinueModel`, `CompleteFrame`, `StartDelegation`, `ExecutePlan`, `DispatchTools`, `SuspendForFrontend`, `PauseWorkflow`, `CompleteTurn`, and `FailTurn` cannot be retained as unused declarations while a domain callback performs their effects invisibly. Generic model invocation and server-tool execution run once in the shared core. Domain handlers may validate, normalize, authorize, and classify a Map action, but they return typed directives or domain results instead of owning a second complete model/tool loop.

The driver owns only the loop and state transitions. It depends on domain protocols and cannot import Map implementations, inspect concrete Map tool names, or encode Map budgets, routing, structured completion, validation, write guards, and recovery.

`TurnOutcome` replaces `StepResult` with explicit completed, awaiting-frontend-tools, paused, and failed outcomes. All callers and tests move to the new contract in the same cutover; no facade preserves `run_turn()` or `StepResult`.

This state machine is preferred over a linear Resolve/Prepare/Call/Parse pipeline because the runtime has nested Frames, repeated model/tool cycles, frontend suspension, forced completion, pauses, and recovery transitions.

### 7. Map and Verify behavior enter through domain policies

Map implements a `TurnDomainPolicy`/`MapTurnPolicy` adapter responsible for structured routing assessment, visible-plan requirements, stage tools, persistent budgets, structured completion, validation guards, workflow continuation, and typed recovery. Verify implements a policy/handler that produces canonical `VerifyOutcome` and recovery directives.

`MapTurnPolicy` is a small adapter over cohesive transition handlers rather than a complete pipeline. The target package is:

```text
app/orchestrator/map_turn/
├── policy.py
├── execution.py
├── runtime.py
├── frame_lifecycle.py
├── structured_completion.py
├── planning.py
├── delegation.py
├── tool_arguments.py
├── tool_guards.py
├── tool_dispatch.py
├── budgets.py
└── events.py
```

`execution.py` coordinates one explicit transition. `runtime.py` owns only per-run artifact stores and budget counters. Frame completion/exhaustion, structured result parsing and repair, planning, delegation, tool argument normalization, Map guards, dispatch classification, budget accounting, and event projection each have one module owner. Leaf modules never import `policy.py` or `execution.py`; the dependency direction points from the adapter toward handlers and from handlers toward existing Map contracts/reducers. `map_turn_pipeline.py` is deleted after all production and test imports move to the owning modules, with no old-module re-export.

Generic model selection, effort, provider invocation, tool protocol parsing, permission evaluation, and event emission remain in shared core services. Separate complete Map/chat/Verify pipelines are rejected because they would duplicate this shared kernel and drift.

Architecture tests enforce dependency direction and scan the turn core for forbidden domain imports and concrete Map tool identifiers.

### 8. Application use cases replace QueryEngine

Replace `QueryEngine` and the replacement `AgentApplication` facade with independently injected application use cases for user submission, tool-result submission, turn resume, interruption, reset, recovery, history, compaction, settings, Map task control, and response mapping. FastAPI routes depend on these concrete use-case interfaces rather than a general-purpose engine or a forwarding wrapper.

The target application layout is:

```text
app/application/
├── composition.py
├── session_uow.py
├── progress.py
├── turn_execution.py
├── publication.py
├── submission/
│   ├── user_submission.py
│   ├── tool_result_submission.py
│   ├── map_result_projection.py
│   └── preview_lifecycle.py
└── use_cases/
    ├── reset.py
    ├── interruption.py
    ├── recovery.py
    ├── history.py
    ├── compaction.py
    ├── session_settings.py
    └── map_task_control.py
```

Each use case owns its command validation, Session lock/unit-of-work, idempotency, recovery disposition, and response boundary. Shared behavior is injected through cohesive services such as `SessionUnitOfWork`, `TurnExecutionService`, `SubmissionPublisher`, and `TurnProgressRegistry`; a use case does not hold an `AgentApplication` reference. `ApplicationUseCases` may remain only as an immutable composition-root bundle of concrete use cases.

Atomic tool-result submission owns its Session working copy, canonical fingerprint, completed-turn identity, workflow publication, artifacts, grants, events, and response locator under one transaction/recovery boundary. Event publication remains buffered until durable commit. A typed `SubmissionScope` carries the working Session, request/turn identity, staged artifact turn, preview lifecycle, and buffered event publisher explicitly through that boundary. A module-global `ContextVar` is rejected because it hides transaction ownership from signatures and tests. Application services may call `TurnDriver` but cannot absorb domain routing or transport presentation.

All route wiring and tests switch to the new use cases together. `QueryEngine`, `AgentApplication`, `application/service.py`, forwarding-only use-case methods, wildcard helper imports, hidden publication globals, and old response conversion helpers are deleted rather than wrapped.

Module-size limits are secondary enforcement for the ownership design: production orchestration modules default to at most 700 logical lines, concrete use-case entry methods to at most 250 logical lines, and the `MapTurnPolicy` adapter to at most 200 logical lines. Architecture tests report the violating file or method. A limit increase requires an explicit design update; generated files and declarative schema tables are not silently exempted. The architecture checks enforce these budgets on the surfaces this change redesigns — every module under `app/application/` and every `app/orchestrator/map_turn/` handler. Orchestrator, query, and tool modules outside that cut (for example `map_progress.py` or `query/helpers.py`) keep their pre-existing sizes until a later redesign explicitly applies the same budget to them; they are out of enforcement scope, not silently exempt within it.

### 9. Complex Map routing is structured and conservative

Replace keyword counts with `MapTaskRoutingAssessment` based on normalized mutation/read-only intent, explicit target, operation count and extent, known resource/cell inputs, dependency on current facts, planning/validation need, and approval need. Only one explicit target, one bounded mutation, known inputs, and no read/plan/validation dependency qualifies as `atomic_edit`; every other mutation requires a visible macro plan. Ambiguity requires planning.

The LLM may propose attributes, but runtime validation owns classification and cannot be weakened by prose. The implementation lives in `MapTurnPolicy`, not in the generic driver.

### 10. Frontend state and every visible message use one Canonical Chat Timeline

Split frontend responsibilities into two non-overlapping flows:

```text
WebSocket ChatEvent ─┐
                     ├─→ ChatTimelineProjector
History Event Page ──┘             │
                                   ▼
                         ChatTimelineMutation
                                   │
                                   ▼
                          ChatTimelineStore
                                   │
                                   ▼
                       ChatItemRendererRegistry
                                   │
                                   ▼
                           VirtualScroller

ChatEvent → SessionTurnStateReducer
          → no UI-node creation or mutation
```

`ChatSubmissionClient` owns HTTP commands only. `AgentEventSocket` owns WebSocket protocol, authentication, reconnect, and transport liveness. `ChatEventAcceptor` validates protocol, epoch, sequence, duplication, and gaps before either reducer or projector observes an event. `SessionTurnStateReducer` owns non-visible epoch, cursor, active-turn, reset, reconnect, and suppression state. Submission, approval, history, recovery, and streaming controllers send user intent and coordinate these owners; they do not create Timeline nodes.

`ChatTimelineItem` is the sole serializable presentation model. It contains a stable `item_id`, Session epoch, deterministic order key, closed `kind` and `role`, typed content blocks, provisional/committed/discarded lifecycle, status, copy text, style token, and source identities for Frame, message, tool call, artifact, and preview. Visible identity never derives from rendered text.

`ChatTimelineMutation` is a closed union of `insert`, `patch`, `finalize`, `discard`, `remove`, `prepend_page`, and `reset_epoch`. Unknown mutations, invalid lifecycle transitions, epoch mismatches, and ambiguous identities fail closed before changing the store. The projector is pure: equivalent canonical live and historical inputs produce the same items and mutations without reading or creating Godot controls.

WebSocket live events and backend history event pages enter the same projector. The history API returns canonical domain/event records and never manufactures `_history_log_text`, `_history_thought`, `_history_code`, or `_history_front_tool_result`. A stream delta patches the stable assistant or reasoning item; final finalizes that same item. Preview commit/discard changes only the matching item lifecycle. Reasoning and body order use the Timeline order key rather than node-creation timing. Prepending a history page preserves existing identities and a stable scroll anchor.

Tool previews use serializable render descriptors or artifact references. No store entry contains a prebuilt `Control`, and no controller or ChatPanel path may insert an external node. `ChatItemRendererRegistry` is the only component allowed to create presentation nodes. It provides one policy for Markdown, truncation, copying, themes, indentation, status colors, reasoning disclosure, tool results, and diffs, so live and historical forms are structurally and visually equivalent.

`VirtualScroller` subscribes only to TimelineStore mutations. It never accepts direct ChatPanel insertion or a prebuilt external node. ChatPanel sends user intent, binds view interactions, and hosts the view; it does not project events, choose rendering identity, mutate TimelineStore outside controller operations, construct item nodes, or deduplicate finals. `MessageStore`, NodeFactory, LogRenderer, legacy append helpers, and text-fingerprint sets are removed or reduced to implementation details behind the canonical Store/Registry boundary, never parallel authorities.

This design is preferred over merely merging `_append_message()` helpers because a shared helper would leave identity, lifecycle, history, reasoning, tool-preview, and node-ownership decisions distributed across the same competing components.

### 11. Operational policy, performance work, composition, and repository policy are explicit

Move provider attempt budget, workflow snapshot thresholds, completion continuations, map compaction, hot-cache size, WebSocket backpressure, render budget, and reconnect policy into validated settings. `/doctor` and structured startup diagnostics expose effective non-secret values. Removed polling settings are invalid.

`reduce_map_workflow` continues to produce independent state; dispatch transfers it without a second whole-state `deepcopy`, verified by aliasing tests and representative benchmarks. Broader copy-on-write requires a later accepted design if profiling still identifies a material bottleneck.

Project discovery uses the Godot `EditorFileSystem` tree, caches filtered immutable paths, and invalidates on relevant filesystem/resource signals.

Importing application modules constructs nothing. Managed CLI and external ASGI entry points invoke one explicit composition root that creates providers, stores, application use cases, `TurnDriver`, domain policies, WebSocket services, and lifespan exactly once.

User-visible status/errors resolve through `ChatPanelText`; retained DEBUG diagnostics redact values. Hydration repairs and blocked outcomes are persisted and projected to recovery UI. Repository coding instructions permit required regression files. LF/encoding policy is declared and mechanical normalization is isolated from semantic work.

## Risks / Trade-offs

- **[Clean cut invalidates existing Sessions]** → Surface `unsupported_session_schema` with an explicit new-Session action, never mutate legacy files, and document the boundary before release.
- **[No runtime rollback after new-format writes]** → Treat cutover as a declared irreversible data boundary; take optional filesystem backups before deployment and validate the complete replacement in an isolated test project first.
- **[Large coordinated rewrite increases integration risk]** → Implement reviewable dependency-ordered commits on a branch, keep main/release artifacts unchanged until acceptance, and switch all callers once; never ship runtime dual paths.
- **[Cross-file workflow commit can expose orphan files]** → Manifest replacement is the sole visibility point; coordinated-journal recovery reconciles or removes preparations.
- **[Snapshot mismatch blocks an otherwise readable Session]** → Preserve files, emit a typed recovery problem, allow read-only diagnostics, and never guess authority.
- **[WebSocket disconnects in editor/network edge cases]** → Resume from cumulative acknowledgement, use bounded backoff, and recover authoritative state through the dedicated snapshot endpoint rather than polling.
- **[Timeline unification can change live/history appearance or scroll behavior]** → Characterize existing visible semantics, project live and history fixtures through the same pure projector, compare canonical structures and rendered output, and preserve anchors through Store mutations before deleting old paths.
- **[A hidden direct-render path survives the migration]** → Make RendererRegistry the only node factory, make VirtualScroller consume only Store mutations, and add source architecture checks for pseudo-events, text fingerprints, external controls, and direct append or insertion helpers.
- **[Slow clients retain server memory]** → Bound unacknowledged count and bytes, pause then close stalled connections, and retain authority in the event store.
- **[Unavailable verification loops]** → Persist attempt identity/budget and allow one closed recovery action per outcome.
- **[No retry after partial stream reduces automatic recovery]** → Preserve and visibly invalidate or present matching partial output, return a typed problem, and require a new continuation identity.
- **[Domain rules leak back into the turn core]** → Enforce package dependency tests and forbidden identifier checks in CI.
- **[File extraction preserves the same god object through imports]** → Reject `AgentApplication`, forwarding-only use cases, old-module re-exports, wildcard imports, circular handler dependencies, and size-budget violations in architecture tests.
- **[Splitting application code fragments the atomic submission boundary]** → Keep one explicit `SubmissionScope` and `SessionUnitOfWork`; subordinate services prepare data, while only the owning use case commits or resolves publication.
- **[Additional handler calls add hot-path overhead]** → Pass Session and Frame aggregate references plus immutable dependency ports without per-stage deep copies; provider and tool I/O dominate dispatch cost, and representative benchmarks guard regressions.
- **[Line-ending normalization obscures review]** → Isolate normalization from functional edits and avoid normalizing files that are deleted by the clean cut.
- **[Repository instructions block required tests]** → Correct the repository-owned rule before Python implementation and verify active instructions permit planned coverage.

## Cutover Plan

1. Correct repository coding instructions, inventory archived verification work, record baselines, and declare the unsupported legacy Session/protocol boundary.
2. Define the canonical contracts: `VerifyOutcome`, workflow manifest/snapshot/segments, WebSocket frames, `TurnCommand`, `TurnDirective`, `TurnOutcome`, domain ports, and application use-case interfaces.
3. Implement provider retry ownership, durable completed-turn identity, workflow persistence, and canonical Verify behavior only against the new Session schema.
4. Implement exhaustive TurnDriver directive execution and move Map behavior into the dependency-directed `map_turn` handler package; update all callers and tests, then delete `agent.py`, `run_turn()`, `StepResult`, `map_turn_pipeline.py`, and old private-module imports.
5. Implement explicit Session unit-of-work, progress, turn-execution, and submission-publication services; move command ownership into concrete application use cases, update route composition, then delete `QueryEngine`, `AgentApplication`, `application/service.py`, forwarding-only wrappers, wildcard imports, and the global publication context.
6. Implement WebSocket backend, frontend socket, Session state reducer, event acceptor, controllers, canonical Timeline contracts/projector/store/renderer registry, Store-driven VirtualScroller, and view-host-only ChatPanel.
7. Migrate live streams, reasoning, finals, tools, diffs, previews, system/error content, history, and snapshot recovery to the Timeline, then delete `/chat/events`, polling code/settings, `_history_*` pseudo-events, text fingerprints, external controls, parallel append paths, old Verify readers, legacy workflow readers, migration helpers, direct state mutation, and all feature flags before acceptance.
8. Apply routing, caching, composition, configuration, localization, diagnostics, logging, and measured copy-cost changes.
9. Run full clean-project, restart, recovery, corruption, reset, WebSocket, approval, Map, Verify, architecture, Python, Godot, and E2E acceptance against only the new architecture.
10. Add text policies and perform any needed mechanical normalization of surviving files in an isolated step.
11. Release backend, frontend, protocol, and schema epoch as one irreversible cutover. Legacy Sessions receive only the typed unsupported-schema response and new-Session action.

There is no production compatibility window. Source rollback is possible only before cutover. Once new-format Sessions are created, an older runtime is not a supported reader and data is not exported back to legacy form.

## Open Questions

- What default event-count and byte thresholds should trigger workflow snapshots after representative benchmarks?
- Which deterministic validators qualify as recovery actions for each edited file type when semantic Verify is unavailable?
- Should partial streamed output remain visibly marked for inspection or be discarded immediately after `partial_stream_interrupted`?
