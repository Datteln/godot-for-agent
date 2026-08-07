## Why

The agent workflow currently conflates unavailable verification with success, truncates a workflow log that is described as replayable, multiplies provider retries across layers, and delivers frontend events through polling. Its chat UI also has several competing rendering authorities: live text mutates temporary log items, reasoning creates controls directly, history uses private pseudo-events, tool diffs inject prebuilt nodes, and finals rely on text fingerprints. These correctness gaps are reinforced by oversized orchestration modules, a monolithic query engine, and frontend components that mix transport, Session state, event acceptance, projection, storage, and presentation. A post-implementation architecture audit also found that deleting `agent.py` and `QueryEngine` was insufficient: their responsibilities were largely moved into a 5,599-line `map_turn_pipeline.py` with a 1,104-line `MapTurnPolicy.run()`, and a 4,180-line `application/service.py` whose nominal use cases only forward to `AgentApplication`. Incremental compatibility layers or symbol-only architecture checks would preserve the same ownership ambiguity and leave multiple implementations to drift, so this change makes one coordinated breaking cut to a single explicit architecture.

## What Changes

- **BREAKING** Replace boolean semantic verification with the sole `VerifyOutcome` contract: `passed`, `failed`, or `unavailable`, plus typed causes, summaries, retry identity, and closed LLM-facing recovery actions. Remove the legacy `passed: bool` payload and every compatibility projection or reader.
- **BREAKING** Replace the bounded in-Session map workflow event tail with an authoritative manifest, immutable committed event segments, versioned snapshots, monotonic sequence allocation, and post-snapshot replay. Legacy workflow Sessions are unsupported after cutover and require a new Session; there is no runtime migration, dual-read, dual-write, or rollback exporter.
- **BREAKING** Replace `/chat/events` polling with the sole authenticated, epoch-aware WebSocket event protocol. Remove the polling route, timer, interval setting, migration flag, and fallback path in the same cutover while retaining HTTP only for command submissions and bounded authoritative snapshot recovery.
- **BREAKING** Replace every live, historical, reasoning, tool, system, error, preview, and final rendering path with one canonical chat timeline: `ChatTimelineProjector → ChatTimelineStore → ChatItemRendererRegistry → VirtualScroller`. Remove `_history_*` pseudo-events, text-fingerprint deduplication, prebuilt `Control` storage, external-node queue insertion, and direct ChatPanel rendering decisions.
- Consolidate LLM retry ownership: disable SDK retries, bound total provider wire attempts, distinguish pre-response failures from interrupted partial streams, and never start a new completion automatically after accepted stream output.
- Preserve committed tool-result idempotency independently of the bounded full-response cache through durable compact identities and response/checkpoint locators.
- Replace keyword-count map complexity detection with structured task attributes and a conservative rule that only proven atomic map edits may skip a visible plan.
- Replace the monolithic turn loop with a typed `TurnDriver` state machine whose `TurnDirective` outcomes control frame completion, delegation, planning, tool dispatch, frontend suspension, workflow pause, completion, and failure.
- Isolate Map and Verify behavior behind domain policies. The generic turn driver must not inspect Map tool names or own Map-specific routing, budget, validation, completion, or recovery rules.
- Replace the monolithic `QueryEngine` with independently injected application use cases for user submission, tool-result submission, recovery, interruption, reset, history, and response mapping.
- **BREAKING** Delete `map_turn_pipeline.py` after moving Map behavior into cohesive state-machine transition handlers for Frame lifecycle, structured completion, planning, delegation, tool policy/dispatch, budgets, and event projection. `MapTurnPolicy` becomes a small adapter; every declared `TurnDirective` is either executed by the real driver path or removed from the contract.
- **BREAKING** Delete `AgentApplication`, `application/service.py`, forwarding-only use-case wrappers, wildcard helper imports, and the global publication `ContextVar`. Concrete application use cases own their locks, unit-of-work, recovery, and transaction boundaries and receive an explicit per-submission publication scope.
- Split frontend ownership into an HTTP submission client, WebSocket event stream, one Session/turn state owner, ordered event acceptor, controllers, and presentation-only ChatPanel.
- Move workflow retry, compaction, cache, event retention, WebSocket backpressure, rendering, and related operational thresholds into validated observable configuration.
- Remove duplicate whole-state copying from map workflow dispatch, then profile before considering broader copy-on-write state.
- Remove module-import application construction and duplicate runtime composition; expose one explicit application factory and one runtime composition per process.
- Cache frontend project-file discovery through Godot editor filesystem state and invalidation signals rather than rescanning `res://` for every message.
- Complete frontend localization, remove or demote temporary INFO-level inspection logs, and expose hydration repair/block diagnostics through recovery presentation.
- Revise repository Python coding instructions so accepted changes can add proportionate regression coverage, and reconcile the unchecked verification work from the archived remediation change.
- Add repository line-ending policy and isolate any mechanical normalization from functional changes.
- Explicitly exclude cross-session rate limiting and speculative multi-thread protection for `SessionStore`.

## Capabilities

### New Capabilities

- `verification-outcomes`: Defines the sole typed verification result, exact unavailable causes, bounded recovery actions, observability, and agent behavior when verification cannot run.
- `agent-runtime-maintainability`: Defines single runtime composition, an exhaustively executed typed turn state machine, cohesive Map transition handlers, concrete application use cases, explicit transaction scopes, frontend ownership, configurable policy, cached project discovery, localized UI, and architectural enforcement.

### Modified Capabilities

- `llm-fallback-retry`: Makes one provider layer own a bounded request budget and prohibits automatic regeneration after partial streamed output.
- `map-workflow-state-and-evidence`: Replaces bounded event truncation with the sole durable event/snapshot representation and rejects legacy workflow Sessions after cutover.
- `chat-event-streaming`: Replaces polling with the sole authenticated resumable WebSocket event transport and defines the canonical Timeline projection, mutation, identity, lifecycle, storage, and rendering contract shared by live and historical content.
- `atomic-tool-result-submission`: Makes completed tool-result identity durable beyond the bounded full-response cache and requires one explicit, non-global submission publication scope.
- `map-domain-orchestration`: Replaces keyword routing with structured conservative plan requirements and isolates Map rules behind a small domain-policy adapter plus cohesive subordinate transition handlers.

## Impact

- Python service: verification contracts and runner, TurnDriver directive execution, Map transition-handler modules, concrete application use cases, explicit Session unit-of-work/publication scopes, provider construction and retry loop, map workflow persistence, Session schema boundary, FastAPI startup and WebSocket route, event store, recovery, configuration, and module layout.
- Godot frontend: submission client, new WebSocket event stream, Session/turn state reducer, event acceptance, canonical Timeline projector/store, renderer registry, controllers, presentation-only ChatPanel, VirtualScroller integration, context collection, reset/reconnect behavior, localization, and logging.
- **Breaking runtime boundary**: `run_turn()`, `StepResult`, `QueryEngine`, `map_turn_pipeline.py`, `AgentApplication`, `application/service.py`, forwarding-only application use cases, legacy Verify payloads, legacy workflow Session fields, `/chat/events`, polling settings, `_history_*` presentation pseudo-events, text-fingerprint rendering identity, external prebuilt controls, parallel append paths, and direct ChatPanel/client state mutation are removed rather than adapted.
- **Breaking data boundary**: existing Sessions using the legacy workflow representation are rejected and must be replaced by new Sessions. Legacy data may remain untouched on disk for manual backup but is never read by the new runtime.
- Deployment: backend, Godot frontend, protocol schemas, and persistent schema epoch cut over together. No production release contains both old and new runtime paths, and rollback across the data boundary is unsupported.
- Quality gates: desired business invariants—atomic submission, event ordering, approval boundaries, replay integrity, recovery, idempotency, and reset isolation—remain tested against the new architecture. Architecture tests also forbid legacy symbols, routes, settings, and dependency violations.
- Repository policy and planning records: `.claude/skills/coding-habits/SKILL.md`, line-ending policy, and the archived remediation verification backlog are reconciled as part of the change.
