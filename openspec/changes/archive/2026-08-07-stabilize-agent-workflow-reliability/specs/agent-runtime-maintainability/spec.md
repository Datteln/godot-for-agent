## ADDED Requirements

### Requirement: Service composition occurs exactly once per process
Importing application modules MUST NOT construct providers, Session stores, watchers, application use cases, `TurnDriver`, domain policies, WebSocket services, or FastAPI runtime. Managed CLI and external ASGI launch paths MUST each invoke one explicit composition root and create exactly one application lifespan per process.

#### Scenario: Managed service starts through the module entry point
- **WHEN** Godot launches `python -m app.main` with its stdin token
- **THEN** one runtime composition containing the stores, application use cases, TurnDriver, domain policies, transports, and lifespan is constructed

#### Scenario: Tests import the application factory
- **WHEN** a test imports `create_app` without invoking it
- **THEN** no runtime service, filesystem watcher, provider client, Session state, or background task is constructed

### Requirement: Turn orchestration is a typed state machine
The runtime MUST drive model/tool/Frame cycles through one `TurnDriver` and a closed `TurnDirective` union covering continuation, Frame completion, delegation, planning, tool dispatch, frontend suspension, workflow pause, completion, and failure. TurnDriver MUST exhaustively execute or delegate every retained directive through its declared port; domain code MUST NOT perform the directive effect invisibly and return a different terminal outcome. The driver MUST NOT use a linear one-pass pipeline or an all-purpose mutable Stage context to hide control flow.

#### Scenario: A tool result requires another model cycle
- **WHEN** executing a directive produces committed tool messages and another model decision is required
- **THEN** TurnDriver transitions through an explicit continuation directive and retains the same bounded runtime identity

#### Scenario: Frontend tools suspend execution
- **WHEN** tool dispatch requires approval or frontend execution
- **THEN** TurnDriver returns the canonical awaiting-frontend outcome without encoding suspension as an error or continuing the model loop

#### Scenario: Directive inventory is checked
- **WHEN** architecture and transition tests inspect the closed TurnDirective union
- **THEN** every retained variant has a reachable executor and behavioral test, and any decorative or unreachable variant fails acceptance

### Requirement: Generic turn core is isolated from domain policy
The turn core MUST depend only on domain-policy interfaces and MUST NOT import Map implementations, inspect concrete Map tool names, or own Map routing, persistent budgets, structured completion, validation, write guards, workflow continuation, or recovery rules. Generic model selection, effort, thinking budget, provider invocation, permission evaluation, tool protocol parsing, concurrent/sequential server-tool execution, and generic event projection MUST execute through shared turn-core services rather than a second complete loop inside `MapTurnPolicy`. Verify behavior MUST likewise enter through its canonical policy and outcome contract.

#### Scenario: Map-specific behavior is added
- **WHEN** a new Map routing, validation, completion, or recovery rule is introduced
- **THEN** the change is implemented behind `MapTurnPolicy` without adding a Map tool identifier or Map implementation import to the turn core

#### Scenario: Architecture boundary is checked
- **WHEN** repository architecture tests inspect turn-core dependencies and forbidden identifiers
- **THEN** any direct domain implementation dependency fails with the violating module and symbol

#### Scenario: Map policy is inspected
- **WHEN** architecture tests inspect MapTurnPolicy and its subordinate handlers
- **THEN** the policy contains domain classification and transition handling but no duplicate generic model/tool loop

### Requirement: Application use cases own command boundaries
User submission, tool-result submission, resume, interruption, reset, recovery, history, compaction, settings, Map task control, and response mapping MUST be implemented as independently injected concrete application use cases. Each use case MUST own its command validation, Session lock/unit-of-work, idempotency, recovery disposition, and response boundary or delegate cohesive portions through explicit ports while retaining commit ownership. HTTP routes MUST depend on those use cases rather than a general-purpose `QueryEngine` or `AgentApplication`, and no compatibility facade, forwarding-only use case, wildcard helper import, or old-path re-export may remain.

#### Scenario: User turn is submitted
- **WHEN** the HTTP route accepts a valid user-turn command
- **THEN** it invokes the user-submission use case, which owns the transaction boundary and calls TurnDriver through its canonical contract

#### Scenario: Removed legacy symbol is referenced
- **WHEN** a source or test imports `QueryEngine`, `AgentApplication`, `application/service.py`, top-level `run_turn()`, or `StepResult`
- **THEN** the architecture check fails rather than resolving a compatibility adapter

#### Scenario: Use-case ownership is inspected
- **WHEN** architecture tests inspect concrete application use cases and runtime composition
- **THEN** no use case stores a general application facade merely to forward calls, and the composition root injects the real use-case dependencies directly

### Requirement: Replacement orchestration modules have enforceable cohesive boundaries
The accepted runtime MUST delete `map_turn_pipeline.py` and `application/service.py` after callers move to cohesive Map transition handlers and concrete application use cases. Production orchestration modules MUST have one declared reason to change, use dependency direction from adapters toward leaf handlers, and avoid circular imports, forwarding facades, generic utility dumping grounds, and hidden mutable globals. Architecture checks MUST enforce default budgets of at most 700 logical lines per production orchestration module, 250 logical lines per concrete use-case entry method, and 200 logical lines for the `MapTurnPolicy` adapter unless a later accepted design explicitly changes a budget.

#### Scenario: Replacement modules are inspected
- **WHEN** release architecture checks inventory runtime modules and imports
- **THEN** `map_turn_pipeline.py`, `application/service.py`, `AgentApplication`, old-module re-exports, wildcard imports, and circular Map-handler dependencies are absent

#### Scenario: A new god module grows
- **WHEN** a production orchestration module or entry method exceeds its declared budget
- **THEN** architecture acceptance fails with the file, symbol, measured size, and required design action

#### Scenario: Handler dependencies are inspected
- **WHEN** Map transition-handler imports are analyzed
- **THEN** leaf handlers do not import the policy or execution adapter and shared turn-core modules do not import Map handlers

### Requirement: Operational policy is validated and observable
Retry budgets, workflow snapshot thresholds, completion continuation limits, map context compaction threshold, completed-response hot-cache size, WebSocket batch and backpressure bounds, client render budget, and reconnect policy MUST be validated configuration rather than unreported module constants. Effective non-secret values MUST be available through structured startup diagnostics and the doctor surface. Removed polling settings MUST be rejected.

#### Scenario: Invalid operational threshold is configured
- **WHEN** an operator supplies a value outside its declared safe bounds
- **THEN** startup rejects the configuration with the exact setting and permitted range

#### Scenario: Removed polling setting is configured
- **WHEN** configuration contains the obsolete event-poll interval or a polling fallback flag
- **THEN** startup reports the setting as unsupported and does not silently migrate or ignore it

#### Scenario: Doctor is requested
- **WHEN** the authenticated client requests runtime diagnostics
- **THEN** it receives effective operational policy values without secrets, tokens, prompts, or complete user content

### Requirement: Project file discovery is cached and invalidated by editor state
The Godot client MUST build project-file context from a cached, filtered editor filesystem view and MUST invalidate that cache when relevant filesystem or resource change signals occur. Sending a message without a relevant change MUST NOT recursively rescan `res://`.

#### Scenario: Consecutive messages have no filesystem change
- **WHEN** two user messages are submitted after one completed project-file discovery and no invalidation signal occurs
- **THEN** both messages use the same immutable cached path list without another recursive directory scan

#### Scenario: A relevant project file changes
- **WHEN** the editor filesystem reports a created, removed, moved, or renamed supported project file
- **THEN** the next context collection rebuilds or incrementally updates the cache before submitting the message

### Requirement: User-visible frontend text is localized and diagnostics are appropriately leveled
User-visible ChatPanel status and error text MUST resolve through the frontend localization catalog. Temporary raw tool-call inspection MUST NOT execute at INFO level, and DEBUG diagnostics MUST redact values not required to correlate the call.

#### Scenario: Frontend locale changes
- **WHEN** resetting, recovering, paused, invalid command parameter, discarded preview, WebSocket, or Verify text is rendered
- **THEN** the text is selected through the localization catalog for the active locale

#### Scenario: Normal tool execution runs at INFO level
- **WHEN** the frontend executes a tool under normal logging configuration
- **THEN** temporary entry/exit field inspection is not emitted and secrets or complete arguments are not logged

### Requirement: Frontend transport, Session state, acceptance, and presentation have separate owners
HTTP command submission, WebSocket event protocol/reconnect, Session epoch and turn state, ordered event acceptance, controller behavior, Timeline projection/storage/rendering, and ChatPanel view hosting MUST be owned by separate components. ChatPanel and transports MUST use state-owner operations rather than assigning each other's fields, and ChatPanel MUST NOT own event ordering, transport lifecycle, Timeline identity, Timeline mutations, or item-node construction. Controllers and the Session state reducer MUST NOT create or modify UI nodes.

#### Scenario: Session reset is acknowledged
- **WHEN** the backend returns a successful reset acknowledgement
- **THEN** the state owner atomically adopts the new epoch and cursor, clears the old active turn, and notifies controllers, ChatPanel, and WebSocket transport

#### Scenario: ChatPanel renders status
- **WHEN** ChatPanel needs active-turn information
- **THEN** it reads the state owner and performs no transport or cursor mutation

#### Scenario: WebSocket batch arrives
- **WHEN** the transport receives a valid event batch
- **THEN** the event acceptor validates epoch, sequence, duplication, and gaps before the state reducer or ChatTimelineProjector observes the events

### Requirement: Canonical Timeline components are the only presentation authority
Every visible live or historical chat element MUST be represented by a canonical serializable `ChatTimelineItem` and changed only through a validated `ChatTimelineMutation` applied to `ChatTimelineStore`. MessageStore, controllers, ChatPanel, NodeFactory, LogRenderer, and VirtualScroller MUST NOT maintain a parallel append, identity, lifecycle, or node-insertion authority. `ChatItemRendererRegistry` MUST be the only component that creates item UI nodes.

#### Scenario: A controller receives presentation-ready data
- **WHEN** submission, approval, history, recovery, or streaming coordination produces data that may become visible
- **THEN** the controller sends canonical input to ChatTimelineProjector and does not append text, create a Control, or mutate a rendered node

#### Scenario: A Timeline item is rendered
- **WHEN** ChatTimelineStore applies a valid insert, patch, finalize, discard, remove, prepend-page, or reset-epoch mutation
- **THEN** VirtualScroller observes the store mutation and obtains any required node only from ChatItemRendererRegistry

#### Scenario: A legacy rendering bypass is inspected
- **WHEN** architecture tests inspect ChatPanel, MessageStore, NodeFactory, LogRenderer, controllers, and VirtualScroller
- **THEN** `external: true`, prebuilt Control storage, `_queue_external_message`, text-fingerprint identity, private history presentation events, and parallel append paths are absent

### Requirement: Hydration problems reach recovery presentation without legacy repair
Every blocked invalid current-schema hydration outcome MUST produce a typed backend diagnostic that is persisted with the Session and projected to recovery events and UI history. The runtime MUST NOT repair or continue unsupported legacy Session schemas.

#### Scenario: Current-schema state fails integrity validation
- **WHEN** current manifest-selected state has ambiguous lineage, invalid owner state, or evidence of an uncommitted side effect
- **THEN** mutation remains blocked and recovery UI displays the typed cause, affected identity, checkpoint, and supported read-only action

#### Scenario: Legacy Session is presented
- **WHEN** Session data lacks the current schema epoch or uses a removed workflow representation
- **THEN** the runtime returns `unsupported_session_schema` with a new-Session action and performs no migration or provider call

### Requirement: Clean cut leaves no compatibility path for replaced surfaces
For the Turn, QueryEngine, Verify, embedded workflow, event-transport, and frontend-state surfaces replaced by this change, the accepted runtime MUST contain only the new turn, application, workflow, verification, WebSocket, state-owner, and presentation paths. Their compatibility readers, projections, feature flags, polling fallback, dual writes, rollback exporters, and old-path forwarding modules MUST be absent.

#### Scenario: Release artifact is inspected
- **WHEN** architecture and route inventories run against the release candidate
- **THEN** no legacy symbol, polling endpoint, polling setting, Verify boolean projection, workflow dual reader, or old/new implementation selector exists

### Requirement: Repository text policy produces stable diffs
The repository MUST declare LF policy for supported text files and MUST isolate mechanical normalization from functional changes.

#### Scenario: Text policy is introduced
- **WHEN** surviving mixed or CRLF files are normalized
- **THEN** normalization occurs in a dedicated change step with no semantic code edits and repository EOL inspection reports the declared form

### Requirement: Repository coding policy permits specification-required tests
Repository-owned agent instructions MUST permit and require proportionate regression, protocol, recovery, architecture, and characterization tests needed to satisfy accepted specifications. A general coding skill MUST NOT prohibit all test-file creation.

#### Scenario: A Python reliability change is applied
- **WHEN** implementation changes retry, persistence, recovery, API protocol, or architecture behavior
- **THEN** active coding instructions permit the tests required by the corresponding OpenSpec scenarios

#### Scenario: Archived verification work is reconciled
- **WHEN** this change reaches final verification
- **THEN** every unchecked verification task in the archived remediation change is mapped to completed coverage here or an explicitly identified remaining change
