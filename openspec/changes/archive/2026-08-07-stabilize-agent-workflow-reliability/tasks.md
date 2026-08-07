## 1. Policy, Baseline, and Irreversible Boundary

- [x] 1.1 Revise `.claude/skills/coding-habits/SKILL.md` to permit and require proportionate test creation while retaining typing, async, documentation, and safety requirements.
- [x] 1.2 Inventory all unchecked tasks in archived `resolve-map-agent-remediation-backlog` and map each verification item to this change or an explicitly named follow-up.
- [x] 1.3 Record current Python tests, OpenSpec validation, Godot test availability, route/settings inventory, architecture symbols, line endings, workflow sizes, and performance baselines.
- [x] 1.4 Declare the new Session schema epoch and document that legacy workflow Sessions return `unsupported_session_schema` and require a new Session.
- [x] 1.5 Add a release check proving no converter, compatibility reader, dual writer, feature flag, polling fallback, or rollback exporter is introduced for the Turn, QueryEngine, Verify, embedded workflow, event-transport, or frontend-state surfaces replaced here.
- [x] 1.6 Add validated service settings for provider attempts, workflow snapshot event/byte thresholds, completion continuations, map compaction, hot-response cache, WebSocket backpressure, and reconnect policy.
- [x] 1.7 Add validated Godot settings for WebSocket acknowledgement, reconnect, heartbeat, and per-frame rendering without defining or migrating polling settings.
- [x] 1.8 Expose effective non-secret operational policy through startup diagnostics and `/doctor`, rejecting unsafe values and removed polling settings.

## 2. Canonical Contracts and Dependency Rules

- [x] 2.1 Define canonical `TurnCommand`, closed `TurnDirective`, and `TurnOutcome` contracts without `StepResult` compatibility types.
- [x] 2.2 Define `TurnDependencies`, minimal `TurnRuntime`, model/tool/event ports, and domain-policy interfaces without an all-purpose mutable Stage context.
- [x] 2.3 Define the sole versioned `VerifyOutcome`, reason codes, retry identity, and closed recovery-action schemas without a boolean projection.
- [x] 2.4 Define current-schema workflow manifest, snapshot, immutable segment, event sequence, lineage, digest, and schema-epoch models.
- [x] 2.5 Define WebSocket `resume`, `hello`, `event_batch`, `ack`, `epoch_changed`, `snapshot_required`, liveness, and typed-close messages.
- [x] 2.6 Define application use-case commands/results for user submission, tool-result submission, resume, interruption, reset, recovery, history, compaction, and response mapping.
- [x] 2.7 Define frontend Session/turn state, event-acceptance, submission-client, event-socket, controller, and presentation interfaces.
- [x] 2.8 Add architecture tests for package dependency direction and forbidden imports, symbols, concrete Map tool names, polling paths, and compatibility adapters.

## 3. Single-Owner LLM Retry and Partial Streams

- [x] 3.1 Construct the OpenAI-compatible SDK client with `max_retries=0` and one wire-attempt counter shared by primary and configured fallback requests.
- [x] 3.2 Remove independent stream reconnection attempts and route every pre-stream retry through the provider's bounded policy.
- [x] 3.3 Track whether text, reasoning, tool-call, or usage output has been accepted and return `partial_stream_interrupted` after that boundary without another completion.
- [x] 3.4 Preserve and resolve matching provisional preview identity when a partial stream is interrupted so unrelated previews are untouched.
- [x] 3.5 Keep bounded backoff and first fallback notification semantics while reporting actual model and wire-attempt count in redacted diagnostics.
- [x] 3.6 Add provider tests for SDK retry disablement, exact wire-call maximum, primary/fallback order, terminal status codes, pre-stream retry, and no retry after every partial-chunk kind.
- [x] 3.7 Add integration coverage proving a partial stream cannot concatenate independent completions or duplicate provider/tool effects.

## 4. Canonical Verification Outcomes and Agent Recovery

- [x] 4.1 Implement canonical VerifyOutcome validation and reject boolean-only, compatibility-projected, or otherwise unsupported verification payloads with `unsupported_verify_schema`.
- [x] 4.2 Convert syntax success/failure, target read errors, missing Frames, exhausted budgets, provider errors/timeouts, missing validators, and malformed model responses into exact `passed`, `failed`, or `unavailable` outcomes.
- [x] 4.3 Persist verification attempt identity, target content identity, root cause, consumed actions, and remaining budget so equivalent retries cannot reset the loop.
- [x] 4.4 Implement validated `reread_target`, `rediscover_target`, `run_deterministic_check`, `retry_verifier`, `use_configured_fallback`, and `pause_unverified` actions without widening authority.
- [x] 4.5 Inject the exact outcome and permitted actions into the owning Frame and guide the agent to select at most one action and never call unavailable verification passed.
- [x] 4.6 Make required verification pause at an unverified checkpoint while advisory verification may continue only with explicit unverified evidence and user text.
- [x] 4.7 Update event/history schemas and Godot presentation for all statuses, typed causes, attempts, and supported actions; expose no legacy `passed` field.
- [x] 4.8 Add tests for every unavailable cause, legacy-payload rejection, deterministic alternatives, repeated-action rejection, required/advisory policy, UI presentation, and restart persistence.

## 5. Durable Completed-Turn Identity

- [x] 5.1 Implement the compact completed-turn identity ledger scoped by new-schema Session epoch with fingerprint, outcome kind, commit digest, and response/checkpoint locator.
- [x] 5.2 Keep full responses in a configurable hot cache while eviction leaves the ledger authoritative.
- [x] 5.3 Load or reconstruct the original outcome for an identical retry after eviction without reapplying messages, grants, artifacts, reducer events, approvals, or mutations.
- [x] 5.4 Reject a conflicting fingerprint from the durable ledger and preserve the original committed result.
- [x] 5.5 Start the ledger only at the new Session schema boundary and add no reader or converter for legacy completed-response caches.
- [x] 5.6 Add restart, beyond-cache-size, identical retry, conflict, reconstruction, coordinated publication, and reset-isolation tests.

## 6. Sole Durable Map Workflow Store

- [x] 6.1 Implement project-confined workflow paths and atomic canonical snapshot, immutable segment, and manifest operations with deterministic hashing.
- [x] 6.2 Replace list-length event identity with a persisted monotonic high-water allocator and stage reducer events with the active Session working copy.
- [x] 6.3 Extend coordinated publication to prepare Session data, workflow segment, map artifacts, completed-turn identity, and manifest switch under one recoverable commit identity.
- [x] 6.4 Implement startup loading that validates schema epoch, manifest, snapshot and segment digests, chain continuity, epoch/lineage, and reducer replay before publication.
- [x] 6.5 Return `unsupported_session_schema` for embedded legacy MapTaskState and perform no migration, provider call, workflow mutation, dual read, or fallback load.
- [x] 6.6 Emit typed read-only recovery problems for missing, duplicate, reordered, corrupt, or digest-mismatched current-schema data while preserving diagnostic files.
- [x] 6.7 Implement event-count/byte-triggered compaction that verifies a snapshot, switches the manifest atomically, and only afterward garbage-collects covered segments.
- [x] 6.8 Add restart reconciliation for orphan prepared segments, interrupted publication, interrupted compaction, manifest-switch failure, and cleanup failure.
- [x] 6.9 Delete the 512-entry in-state workflow tail, slicing, embedded workflow persistence, legacy readers, converters, diagnostic-tail projection, dual writers, and old-format exporter.
- [x] 6.10 Add replay tests beyond 512 events, sequence uniqueness, target/revision isolation, unsupported-schema rejection, crash boundaries, corruption, compaction, and restart restoration.

## 7. Turn State Machine and Domain Policies

- [x] 7.1 Create the `orchestrator/turn` package for contracts, runtime, frame gates, model request construction, model invocation, response classification, tool dispatch, delegation, and event projection.
- [x] 7.2 Implement TurnDriver as the sole bounded model/tool/Frame loop that executes typed directives and returns canonical outcomes.
- [x] 7.3 Implement explicit Frame completion, forced structured completion, child return, budget exhaustion, workflow pause, and frontend-suspension transitions.
- [x] 7.4 Implement generic model/effort/thinking/tool visibility, cache, permission, protocol parsing, concurrent/sequential tool execution, and event behavior behind turn-core services.
- [x] 7.5 Create MapTurnPolicy for structured routing, stage capabilities, persistent budgets, structured completion, validation, write guards, workflow continuation, and recovery.
- [x] 7.6 Connect Verify behavior through its policy and canonical VerifyOutcome rather than generic error/result branches.
- [x] 7.7 Prove TurnDriver has no Map implementation import, concrete Map tool literal, Map-specific Session field branch, or duplicated shared model/tool pipeline.
- [x] 7.8 Update all backend callers and tests to TurnCommand/TurnOutcome and delete `agent.py`, top-level `run_turn()`, `StepResult`, and their compatibility aliases in the same integration cut.
- [x] 7.9 Add transition tests covering nested Frames, delegate/plan paths, model failure, no-tool final, structured correction, server tools, frontend tools, pauses, and exhaustion.
- [x] 7.10 Record a post-cut characterization inventory for `map_turn_pipeline.py`, its private test imports, every TurnDirective/TurnRuntime consumer, function spans, dependencies, and current behavior before moving code.
- [x] 7.11 Implement exhaustive TurnDriver directive application so every retained continuation, Frame, delegation, planning, tool, suspension, pause, completion, and failure variant has a reachable executor and test; delete decorative variants instead of leaving dead contracts.
- [x] 7.12 Move the generic model cycle, model/effort/thinking selection, cache behavior, permission evaluation, protocol parsing, server-tool execution, and generic event behavior into the shared turn core without Map imports.
- [x] 7.13 Create the dependency-directed `orchestrator/map_turn` package with a typed per-run runtime, one-transition execution adapter, and leaf owners for Frame lifecycle, structured completion, planning, delegation, tool arguments, tool guards, tool dispatch, budgets, and events.
- [x] 7.14 Move Frame completion, forced completion, child return, exhaustion, structured parsing/repair, reader/planner/writer result application, planning, and delegation into their owning handlers with focused unit tests.
- [x] 7.15 Move Map argument normalization, stage/revision/validation/write/follow-up guards, cache decisions, server/front classification, batch queuing, and suspension into focused tool-policy handlers while preserving reducer ownership.
- [x] 7.16 Reduce `MapTurnPolicy` to a domain adapter and make `MapTransitionEngine` coordinate one explicit transition without an all-purpose mutable Stage context or per-handler deep copies.
- [x] 7.17 Update all production and test imports to the owning modules, delete `map_turn_pipeline.py`, and add no old-module re-export, import alias, feature flag, or dual execution path.
- [x] 7.18 Add architecture checks for exhaustive directive reachability, generic-core/Map dependency direction, circular handler imports, forbidden duplicate model/tool loops, old module paths, and the declared module/method size budgets.

## 8. Application Use Cases and Atomic Submission

- [x] 8.1 Implement independently injected use cases for user submission, tool-result submission, resume, interruption, reset, recovery, history, compaction, and response mapping.
- [x] 8.2 Move Session locking, request identity, working-copy construction, persistence, publication buffering, and response creation into the appropriate use-case boundaries.
- [x] 8.3 Move atomic tool-result validation, canonical fingerprinting, grants, artifacts, workflow segments, completed-turn identity, and response locator into one recoverable submission coordinator.
- [x] 8.4 Preserve provisional preview publication before commit and exact commit/discard resolution without publishing buffered business events early.
- [x] 8.5 Replace map continuation helpers in the old engine with MapTurnPolicy/application coordination using reducer-owned events.
- [x] 8.6 Wire FastAPI routes and runtime composition directly to use-case interfaces and canonical results.
- [x] 8.7 Delete `QueryEngine`, forwarding methods, old response converters, direct imports, and compatibility facade after all callers switch.
- [x] 8.8 Remove legacy per-call map artifact readers; return only current canonical artifacts and typed integrity/missing results.
- [x] 8.9 Add atomicity, idempotency, recovery failpoint, cancellation, interruption, restart, conflict, preview lifecycle, and route-contract tests against the use cases.
- [x] 8.10 Record a post-cut characterization inventory for `AgentApplication`, forwarding-only use cases, global publication context, wildcard helper dependencies, route wiring, transaction boundaries, and private test imports.
- [x] 8.11 Implement explicit `SessionUnitOfWork`, `TurnExecutionService`, `TurnProgressRegistry`, and cohesive dependency ports without introducing a replacement god dependency bag.
- [x] 8.12 Replace the global publication `ContextVar` with one typed explicit `SubmissionScope` carrying the working Session, identities, staged artifacts, preview lifecycle, and buffered publisher through Turn and subordinate handlers.
- [x] 8.13 Implement `UserSubmissionUseCase` as the owner of command validation, Session lock/load, schema and epoch checks, request idempotency, request scope, TurnDriver invocation, persistence, recovery disposition, and response publication.
- [x] 8.14 Implement `ToolResultSubmissionUseCase` as the sole owner of batch validation, durable fingerprint/ledger checks, grants, artifact/workflow coordination, response locator, preview resolution, commit, retry, and discard.
- [x] 8.15 Implement concrete resume, interruption, reset, recovery, history, compaction, session-settings, Map-task-control, and response-mapping use cases with narrow injected dependencies and focused tests.
- [x] 8.16 Move Map result projection and continuation decisions behind application/Map domain services that submit reducer events and cannot directly assign reducer-owned MapTaskState.
- [x] 8.17 Make the composition root construct and inject concrete use cases directly; retain `ApplicationUseCases` only as an immutable route-facing bundle with no `.compose(AgentApplication)` path.
- [x] 8.18 Update FastAPI routes, lifecycle startup, commands, and tests to concrete use cases and remove every direct dependency on a general application facade.
- [x] 8.19 Delete `AgentApplication`, `application/service.py`, forwarding-only wrappers, wildcard helper imports, global publication state, and old private-module imports without re-exports, feature flags, or dual paths.
- [x] 8.20 Add architecture checks proving application modules do not import FastAPI, use cases do not forward to a general facade, only the owning use case commits a submission scope, and declared module/method size budgets hold.

## 9. Authenticated WebSocket-Only Event Backend

- [x] 9.1 Add the WebSocket event route with bearer-token handshake authentication and ensure credentials never enter URLs or logs.
- [x] 9.2 Bind each connection to one Session epoch and accepted cursor and deliver ordered bounded batches from the authoritative event store.
- [x] 9.3 Implement cumulative acknowledgements, unacknowledged event/byte bounds, delivery pause, and typed stalled-client closure.
- [x] 9.4 Implement reconnect resume, duplicate handling, stale/invalid epoch rejection, sequence-gap detection, and `snapshot_required` disposition.
- [x] 9.5 Send transport ping/pong independently from request-correlated `turn_progress` heartbeats.
- [x] 9.6 Integrate reset so the old epoch emits `epoch_changed` when possible, closes, and cannot deliver accepted events into the new epoch.
- [x] 9.7 Keep a bounded authoritative HTTP snapshot-recovery command that cannot operate as continuous polling.
- [x] 9.8 Delete `/chat/events`, polling response schemas, polling configuration, polling metrics, and backend compatibility handlers before integration acceptance.
- [x] 9.9 Add protocol tests for authentication, batching, acknowledgements, backpressure, stalled clients, reconnect, duplicates, gaps, reset races, liveness, retention, snapshot recovery, and absence of polling routes.

## 10. Godot Transport, State, Controllers, and Presentation

- [x] 10.1 Split `agent_http_client.gd` into an HTTP command-submission client and an authenticated WebSocket event socket.
- [x] 10.2 Implement one SessionTurnState owner for epoch, active turn, accepted/acknowledged cursor, reset, reconnect, and suppression state.
- [x] 10.3 Implement ordered ChatEventAcceptor validation for protocol, epoch, sequence, duplicates, gaps, reset, and snapshot-required recovery before presentation.
- [x] 10.4 Implement capped WebSocket reconnect with jitter from the last acknowledgement without replaying submissions, tool results, model selection, or committed UI state.
- [x] 10.5 Separate socket liveness from application idle progress so ping/pong cannot mask a stalled provider turn.
- [x] 10.6 Introduce submission, tool-approval, history, recovery, and streaming controllers that coordinate services/state and expose presentation-ready signals.
- [x] 10.7 Remove every ChatPanel and transport direct assignment to another component's epoch, cursor, active-turn, reset, or reconnect fields.
- [x] 10.8 Feed accepted live and historical events through ChatTimelineProjector and ChatTimelineStore before the frame-budgeted renderer, preserving preview lifecycle, stream, reasoning, follow-mode, and scroll-anchor semantics.
- [x] 10.9 Reduce ChatPanel to user-intent binding and view hosting by removing projection, Timeline identity, direct item mutation, node construction, final deduplication, and bypass insertion responsibilities.
- [x] 10.10 Delete `_event_http`, polling timer/cadence code, polling setting, migration flag, and polling fallback from the Godot release artifact.
- [x] 10.11 Build and cache project-file context from `EditorFileSystem`, invalidating it on relevant create/remove/move/rename signals.
- [x] 10.12 Move all user-visible command, preview, reset, recovery, pause, WebSocket, and Verify text into `ChatPanelText` for every locale.
- [x] 10.13 Remove or demote temporary raw-call inspection logs and redact retained DEBUG identifiers and values.
- [x] 10.14 Project current-schema hydration integrity problems into persisted recovery events and ChatPanel history without legacy repair.
- [x] 10.15 Add Godot tests for socket startup/auth, batching, slow rendering, reconnect, reset races, stale frames, gaps, snapshot recovery, canonical Timeline identity/lifecycle, controller boundaries, localization, logging, cached context, scrolling, and editor responsiveness.
- [x] 10.16 Define serializable `ChatTimelineItem`, typed `ContentBlock`, stable source identity/order key, lifecycle/status/style fields, and the closed `ChatTimelineMutation` union.
- [x] 10.17 Implement a pure `ChatTimelineProjector` that maps accepted WebSocket and canonical history records to identical validated mutations without creating Godot nodes.
- [x] 10.18 Replace backend `_history_log_text`, `_history_thought`, `_history_code`, and `_history_front_tool_result` generation with canonical history event records and delete their frontend special branches.
- [x] 10.19 Implement `ChatTimelineStore` insert, patch, finalize, discard, remove, prepend-page, and reset-epoch operations with fail-closed identity, epoch, order, and lifecycle validation.
- [x] 10.20 Implement `ChatItemRendererRegistry` as the sole item-node factory with shared Markdown, truncation, copy-text, theme, indentation, reasoning, status-color, tool-result, and diff policies.
- [x] 10.21 Migrate stream text, reasoning, final, system, error, tool call/result, preview lifecycle, and history restoration to canonical Timeline mutations and stable item identities.
- [x] 10.22 Replace prebuilt tool-preview `Control` storage with serializable render descriptors or artifact references that render identically for live and historical items.
- [x] 10.23 Delete `_rendered_assistant_keys`, text-fingerprint deduplication, `_queue_external_message`, `external: true`, prebuilt-node MessageStore entries, and every parallel append/render helper.
- [x] 10.24 Make VirtualScroller subscribe only to ChatTimelineStore mutations and preserve the visible anchor across prepend-page, patch, finalize, discard, and removal.
- [x] 10.25 Route non-presentation event state only through SessionTurnStateReducer and prove that the reducer, controllers, transports, and ChatPanel create or mutate no Timeline UI nodes.
- [x] 10.26 Add live/history structural and visual-equivalence tests, stream-to-final single-item tests, reasoning order tests, tool-preview parity tests, pagination-anchor tests, preview lifecycle tests, mutation-failure tests, and render-budget tests.
- [x] 10.27 Add architecture acceptance that rejects any visible UI creation or insertion path bypassing ChatTimelineStore and ChatItemRendererRegistry.

## 11. Runtime Composition, Map Routing, and Measured Performance

- [x] 11.1 Remove module-level runtime construction and provide explicit managed-CLI and external ASGI factory entry points.
- [x] 11.2 Prove importing the factory has no provider/store/watcher/task side effects and managed startup creates one composition and lifespan.
- [x] 11.3 Define MapTaskRoutingAssessment from mutation intent, explicit target, operation count/extent, known inputs, current-fact dependency, validation, and approval attributes.
- [x] 11.4 Replace keyword counting with conservative MapTurnPolicy classification where only proven atomic edits may skip a visible plan.
- [x] 11.5 Update prompts and Map errors to describe structured plan requirements without making model prose authoritative.
- [x] 11.6 Add multilingual, atomic, ambiguous, multi-scope, read-only, validation-dependent, and model-underclassification routing tests.
- [x] 11.7 Add representative small/large MapTaskState benchmarks and aliasing checks for reducer and dispatch behavior.
- [x] 11.8 Remove the second whole-state `deepcopy` by transferring the reducer's independent state and prove nested mutation isolation.
- [x] 11.9 Record before/after time and allocations and defer broader copy-on-write unless profiling and a later design justify it.

## 12. Repository Text Policy and Mechanical Normalization

- [x] 12.1 Add `.gitattributes` and `.editorconfig` rules for LF text, declared encodings, indentation, and binary exclusions without semantic edits.
- [x] 12.2 Inventory surviving files after clean-cut deletion and normalize only reported mixed/CRLF files in a dedicated mechanical step.
- [x] 12.3 Verify repository EOL state and ensure the normalization diff contains no semantic content changes.

## 13. Clean-Cut Acceptance and Release

- [x] 13.1 Run formatting, compile, type, focused reliability tests, architecture checks, the complete Python suite, and OpenSpec validation with no unexplained regressions.
- [x] 13.2 Run a successful clean-Session Godot Map flow through HTTP submission, accepted WebSocket events, canonical Timeline projection/store/rendering, reader, planner, preview, approval, writer, verifier/reviewer, workflow replay, and completion.
- [x] 13.3 Run failure flows for unavailable Verify recovery, unsupported Verify payload, partial provider interruption, provider exhaustion, WebSocket loss/backpressure, invalid Timeline mutation, invalid tool result, commit crash, stale revision, and corrupt replay data.
- [x] 13.4 Run reset/restart flows proving old epochs, WebSocket frames, Timeline items, completed identities, previews, workflow segments, artifacts, and recovery tokens cannot affect a new conversation.
- [x] 13.5 Prove a legacy Session is rejected with `unsupported_session_schema`, remains unmodified, causes no provider/tool action or Timeline mutation, and offers only a new-Session action.
- [x] 13.6 Verify large-project context collection, large workflow dispatch, live/history rendering parity, localized UI, diagnostic redaction, Timeline render budget, turn-handler dispatch overhead, application-use-case transaction overhead, and editor responsiveness against baselines.
- [x] 13.7 Inventory release routes, settings, modules, DTOs, serializers, Timeline mutations, frontend nodes, persisted writers, Turn directives, Map handlers, application use cases, and publication owners and prove no `/chat/events`, polling code, `passed` projection, embedded workflow writer, `run_turn`, `StepResult`, `QueryEngine`, `AgentApplication`, `map_turn_pipeline.py`, `application/service.py`, forwarding facade, global publication ContextVar, wildcard helper import, compatibility reader, feature flag, dual writer, rollback exporter, `_history_*` pseudo-event, text-fingerprint identity, prebuilt Control storage, or rendering bypass remains.
- [x] 13.8 Reconcile every mapped archived verification item, reopened Canonical Timeline task, reopened Turn/use-case task, and post-cut architecture finding using actual evidence; do not treat symbol deletion, forwarding wrappers, decorative contracts, or earlier fragmented tests as completion evidence.
- [x] 13.9 Validate backend and Godot frontend as one release unit in an isolated project after the Map/application clean cut, record optional backup guidance, and document that rollback across the new data and Timeline boundary is unsupported.
