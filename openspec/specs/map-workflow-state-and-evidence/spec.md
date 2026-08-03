# map-workflow-state-and-evidence Specification

## Purpose

Define authoritative map-workflow state, scoped evidence, completion gating, contextual continuation, and deterministic support-data recovery.
## Requirements
### Requirement: Map workflow state changes only through events
The system MUST route task-epoch initialization and all stage, blocker, checkpoint, batch, validation, evidence, scope, revision, retry, transaction-reference, and no-progress changes through a single Map Workflow reducer. Every state field MUST declare machine-readable lifecycle metadata containing its task, revision, or session scope, reset/default factory, and resume policy, and epoch initialization MUST be derived from that metadata.

#### Scenario: Agent requests a stage transition
- **WHEN** Agent orchestration submits a valid map workflow event
- **THEN** the reducer applies the transition and records the event without direct state-field assignment by the Agent

#### Scenario: QueryEngine requests a state update
- **WHEN** QueryEngine receives a map tool result
- **THEN** it submits an event instead of directly modifying MapTaskState fields or reducer-owned nested containers

#### Scenario: A distinct map task begins
- **WHEN** the runtime creates a new map task rather than resuming the current task lineage
- **THEN** one `task_epoch_started` event atomically resets every task-scoped field, including automatic iterations, blockers, validations, evidence, scopes, revisions, layers, region reads, pending batches, retries, transaction references, and contextual task data

#### Scenario: A workflow field is added
- **WHEN** a new field is introduced without complete lifecycle metadata or its reset/resume behavior differs from that metadata
- **THEN** the exhaustive workflow-state check fails before the field can silently leak across task epochs

#### Scenario: Code bypasses reducer ownership
- **WHEN** a repository check finds a direct write to a reducer-owned scalar or nested container outside the reducer or the exact audited pre-construction hydration boundary
- **THEN** the check fails and identifies the bypassing location

### Requirement: Workflow state is scoped by target and revision
The reducer SHALL organize blockers, checkpoints, batches, evidence, validation, and progress under a canonical `(target, revision)` scope, and a gate match SHALL require an exact non-null target and revision.

#### Scenario: A new revision is observed
- **WHEN** a canonical target advances to a new revision
- **THEN** state from the previous revision cannot satisfy gates for the new revision

#### Scenario: Validation omits its revision
- **WHEN** a validation result has a missing or null revision
- **THEN** it cannot satisfy a gate for any concrete target revision and the runtime returns a typed missing-scope result

#### Scenario: Validator failure updates one target
- **WHEN** a validator or reviewer fails for one target and revision
- **THEN** the reducer upserts only the matching scoped blocker and preserves blockers belonging to every other target, revision, and source

### Requirement: Worker results match the frozen Frame contract
The runtime MUST define `map_worker_result_v1` through one canonical, versioned JSON Schema and MUST derive required fields, local type validation, and per-Frame result constraints from that schema. Before consuming a worker result, the runtime MUST validate it against both the canonical schema and the frozen Frame's stage, target, revision, worker instance, and allowed next stages, regardless of any provider-side validation.

#### Scenario: Worker spoofs its stage
- **WHEN** a worker result declares a stage different from its Frame contract
- **THEN** the runtime returns a typed contract violation and does not advance workflow state

#### Scenario: Worker returns an illegal next stage
- **WHEN** a result's `next_stage` is not allowed by the frozen contract
- **THEN** the runtime rejects the result and records the violation

#### Scenario: Worker returns the wrong revision
- **WHEN** a result revision differs from the Frame target revision
- **THEN** the result cannot update checkpoints, blockers, validation, or completion

#### Scenario: Provider reports schema success but local validation fails
- **WHEN** a provider returns content under native structured-output mode but the content violates the canonical schema or frozen Frame contract
- **THEN** the runtime treats the result as invalid and routes it through structured correction without consuming it

#### Scenario: Required fields evolve
- **WHEN** the canonical schema adds or changes a required field
- **THEN** prompt summaries, provider response contracts, and local required-field validation derive the same updated requirement without a separately maintained field list

### Requirement: Final map-worker generation receives a specialized result contract
The runtime MUST specialize the canonical result schema with every known immutable Frame constraint and MUST deliver that specialized contract to the LLM for the final text-only worker turn. It MUST NOT force the final-result schema on intermediate turns that still permit tool calls.

#### Scenario: Worker is still gathering facts
- **WHEN** a map worker may call tools to obtain required map facts
- **THEN** the provider request permits normal tool calls and does not require the worker's final result schema

#### Scenario: Worker enters text-only completion
- **WHEN** the runtime marks a map-worker Frame `force_text_only`
- **THEN** the next provider request has no tools, carries the specialized `map_worker_result_v1` contract, and requires exactly one result object

#### Scenario: Frame has known immutable scope
- **WHEN** the Frame contract contains a canonical stage, target, revision, worker instance, or allowed next-stage set
- **THEN** the specialized schema constrains the corresponding output fields to those values

#### Scenario: Frame scope is not yet known
- **WHEN** target or revision is legitimately unknown when the final contract is built
- **THEN** the runtime preserves the base field type without inventing a constant and local validation still requires any stage-specific discovered scope

### Requirement: Dynamic Worker identities cannot collide
The system MUST assign each dynamic Worker an instance identity that cannot replace or shadow a permanent Agent definition.

#### Scenario: Requested name matches a permanent Agent
- **WHEN** dynamic worker creation receives a display name equal to a permanent Agent name
- **THEN** the runtime keeps the permanent Agent intact and creates or rejects the worker using a reserved dynamic identity

### Requirement: Completion requires verified evidence
The Completion Gate SHALL be the only component allowed to produce `completion_allowed`.

#### Scenario: Reviewer claims success without screenshot evidence
- **WHEN** reviewer text or payload reports success but has no successful screenshot evidence for the same Frame, target, and revision
- **THEN** the Completion Gate rejects completion

#### Scenario: Evidence reference belongs to another Frame
- **WHEN** `evidence_refs` contains a tool_use_id from another Frame or failed tool call
- **THEN** the evidence is invalid and cannot satisfy the gate

#### Scenario: Validator and reviewer provide valid evidence
- **WHEN** required validation passes and all required screenshot references resolve to successful artifacts for the scoped revision
- **THEN** the Completion Gate may allow completion

### Requirement: Screenshot scratch paths are scheme-aware
Screenshot capture and image review MUST support validated `res://`, `user://`, and project-relative paths without widening the path syntax accepted by unrelated project tools.

#### Scenario: Screenshot uses an explicit user scratch path
- **WHEN** `capture_viewport_screenshot.output_path` is a normalized `user://` path without `..`
- **THEN** the screenshot is written to Godot user scratch storage and its returned absolute path can be consumed by `read_image_metadata`

#### Scenario: Capture path attempts traversal or an unknown scheme
- **WHEN** a screenshot or image-review path contains `..`, is an operating-system absolute path, or uses a scheme other than `res://` or `user://`
- **THEN** the request is rejected before file access with a structured invalid-path result

#### Scenario: Unrelated project tool receives user scratch path
- **WHEN** a tool governed by the ordinary project-path contract receives `user://`
- **THEN** it remains rejected; screenshot support does not alter the behavior of the shared project-only path normalizer

### Requirement: Completion Gate activates only for an explicit current map-edit request
The runtime MUST create a request-scoped `map_edit` intent only when the current user request explicitly asks to create, modify, expand, delete, place, paint, or repair map content. Persisted map state, editor selection, prior map tools, or a non-empty historical `task_id` MUST NOT activate the Completion Gate.

#### Scenario: Ordinary greeting follows a historical map task
- **WHEN** a user sends an ordinary greeting or general chat message while the Session retains a previous map task id, blockers, checkpoint, or revision state
- **THEN** the runtime returns the ordinary assistant response unchanged and does not start, resume, complete, or gate a map task

#### Scenario: User requests map facts without editing
- **WHEN** the current request asks to read, explain, analyze, inspect, or validate a map without asking to modify it
- **THEN** map tools may return observations, but the final response does not pass through the Completion Gate

#### Scenario: User requests a plan without execution
- **WHEN** the current request asks for a map design or modification plan and explicitly does not authorize execution
- **THEN** the request is plan-only and does not activate the Completion Gate

#### Scenario: User explicitly requests a map edit
- **WHEN** the current request explicitly asks the system to create or change map content
- **THEN** the runtime creates a map-edit task bound to that request and may activate its Completion Gate when the task produces a completion candidate

#### Scenario: Related script or scene control request
- **WHEN** a request edits map-related code, saves a scene, or invokes Undo/Redo without requesting semantic map-content generation or repair
- **THEN** that request does not independently activate the map Completion Gate

### Requirement: Completion Gate evaluates only a response from the active map-edit lineage
The runtime SHALL bind the originating user request, map task, child Frames, pending tool turns, and completion candidate with a stable request/task lineage. A final response MUST be gated only when it belongs to that lineage and is explicitly classified as a map completion candidate.

#### Scenario: Map edit is still collecting missing inputs
- **WHEN** an explicit map-edit request still lacks target, layer, resource, revision, or another required input
- **THEN** the assistant's clarification or `missing_inputs` response is returned unchanged instead of being replaced by `completion_target_missing` or other Completion Gate text

#### Scenario: Front tool results continue the same map-edit turn
- **WHEN** pending front tool results are submitted for Frames owned by the active map-edit lineage
- **THEN** the continued response retains the same map task identity and may be gated only after it becomes a completion candidate

#### Scenario: New unrelated request arrives after a map task
- **WHEN** a new user request is not an explicit continuation of the previous map edit
- **THEN** the previous task remains dormant in its persisted lifecycle state and cannot gate, rewrite, or auto-continue the new response

#### Scenario: User explicitly resumes the previous map edit
- **WHEN** the user explicitly asks to continue the previous map edit or invokes the dedicated map-task resume command
- **THEN** the runtime may bind the new request to the existing map-edit lineage and restore its checkpoint

#### Scenario: Generic continuation resolves the unique focused task
- **WHEN** the user says a continuation phrase such as "继续任务" or "continue", the current Session has exactly one resumable task, that task remains the current conversational focus, its checkpoint is valid, and its originating lineage already has explicit map-edit authorization
- **THEN** the runtime resolves the reference to that task, restores its checkpoint, and inherits only the original task's target and authorization scope

#### Scenario: Generic continuation has no unique focused referent
- **WHEN** there is no resumable current task, multiple tasks are plausible, the previous task is completed or abandoned, or conversation focus has moved elsewhere
- **THEN** the runtime asks for task disambiguation or treats the request as general and MUST NOT infer map-edit authorization from arbitrary historical Session state

#### Scenario: Continuation resolver identifies a task but cannot widen it
- **WHEN** contextual resolution binds a continuation request to an existing map-edit task
- **THEN** the request may resume that task but cannot add targets, tools, write modes, or permissions absent from the originating task lineage

#### Scenario: Gate receives a valid completion candidate
- **WHEN** a response belongs to the current explicit map-edit lineage, has a canonical target and revision, and is marked as a map completion candidate
- **THEN** the Completion Gate evaluates validation, reviewer issues, blockers, workflow state, and scoped evidence

#### Scenario: Final response has no current map-edit lineage
- **WHEN** any `ChatFinalResponse` is produced without the current request's map-edit lineage and completion-candidate marker
- **THEN** the runtime MUST NOT call the Completion Gate or replace the response text

#### Scenario: Historical paused task is unrelated to the current request
- **WHEN** a paused map task remains in Session state but contextual task-reference resolution does not bind the current request to it
- **THEN** the paused task remains dormant and its paused-state guard cannot emit an error or rewrite the current response

### Requirement: Task text does not duplicate runtime contracts
Automatically created child Frame task text MUST contain objective and input references only; role rules, schema instructions, stage transitions, and recovery rules SHALL come from structured runtime contracts.

#### Scenario: Automatic reader Frame is created
- **WHEN** runtime recovery creates a reader Frame
- **THEN** its task payload does not repeat the reader system prompt or result schema instructions

### Requirement: Structured-output contracts remain runtime-owned
Worker task text MUST contain objective and input references only, while the runtime contract supplies the schema, immutable Frame constraints, response-format mode, and correction rules.

#### Scenario: Automatic worker Frame is created
- **WHEN** the scheduler creates a dynamic or recovery map worker
- **THEN** its task text does not duplicate a hand-maintained result field list or provider-format instruction

#### Scenario: Prompt-only provider is selected
- **WHEN** the configured provider cannot accept a native structured-response contract
- **THEN** the runtime serializes the same specialized schema and a minimal valid example into a system contract rather than modifying the task text

### Requirement: Missing map support data self-heals inside context reads
`describe_map_context` MUST directly and deterministically rebuild a missing resource registry or spatial index from canonical editor map facts in the same tool invocation, without creating a plan step, child Frame, planner transition, or writer transition.

#### Scenario: Both support files are missing
- **WHEN** `describe_map_context` finds compatible map nodes and neither `resource_registry.json` nor `spatial_index.json` exists
- **THEN** it scans the real TileMapLayer, TileMap, or GridMap resources and cells, atomically writes both fixed internal files, rereads them, and returns their rebuilt status in the same tool result

#### Scenario: One valid support file already exists
- **WHEN** one support file is missing and the other exists with a valid structure
- **THEN** the tool rebuilds only the missing file and does not overwrite the existing file or its manually maintained semantic aliases

#### Scenario: No compatible map exists
- **WHEN** the edited scene contains no compatible TileMapLayer, TileMap, or GridMap
- **THEN** the tool creates no empty support files and returns structured `rebuild_skipped=no_compatible_map`

### Requirement: Direct support-data rebuild is deterministic and bounded
Support-data rebuild MUST use only canonical editor facts, MUST NOT use an LLM to invent resource semantics, and MUST expose any loss of semantic aliases or index coverage.

#### Scenario: A resource has no stable display name
- **WHEN** registry rebuild encounters a verified TileSet source, atlas tile, or MeshLibrary item without a usable name
- **THEN** it generates a stable technical key from the verified source/item/atlas signature and reports `semantic_aliases_recovered=false`

#### Scenario: Spatial index exceeds its configured cap
- **WHEN** a canonical map scan contains more indexable cells than the spatial-index capacity
- **THEN** the rebuilt result records `complete=false`, included and skipped counts, and coverage information, and later index queries retain an incomplete-index warning

### Requirement: Context self-healing has a fixed internal-cache effect
The runtime SHALL model direct support-data rebuild as `writes_internal_cache`, restricted to the two fixed `.ai_agent_service/map_agent/` files, without granting the reader general project or map-content write authority.

#### Scenario: Reader triggers first-use rebuild
- **WHEN** a read-only map worker invokes `describe_map_context` and a support file is missing
- **THEN** the enclosed internal-cache effect may rebuild the fixed file without exposing writer tools, activating the Completion Gate, opening an approved map transaction, or creating an Undo action

#### Scenario: Concurrent first reads observe a missing file
- **WHEN** multiple context reads reach first-use initialization concurrently
- **THEN** rebuild is serialized, the missing condition is checked again inside the lock, and only a fully validated atomic replacement becomes visible

#### Scenario: Existing support data is malformed
- **WHEN** a support file exists but cannot be parsed or fails its entry contract
- **THEN** the context read returns a structured corruption diagnostic and does not silently replace potentially recoverable user-maintained data

### Requirement: Persisted workflow state hydrates through a closed construction boundary
The runtime MUST migrate raw persisted data, validate and normalize the complete value, construct `MapTaskState` once, and publish it as live reducer-owned state. The hydration boundary MUST NOT accept or mutate an already-live `MapTaskState`.

#### Scenario: Persisted state requires schema migration
- **WHEN** an older persisted workflow document is loaded
- **THEN** migration operates on raw data before construction and the complete migrated value passes schema and lifecycle validation before publication

#### Scenario: Hydration completes
- **WHEN** a validated `MapTaskState` has been constructed and published
- **THEN** every later state change, including a migration correction, is represented by a reducer event rather than a hydration allowlist write

#### Scenario: Persisted state round-trips
- **WHEN** a supported workflow state is serialized and hydrated
- **THEN** its task, revision, and session fields preserve the declared resume policies and no field is omitted from classification

### Requirement: Validation inputs are normalized defensively
The runtime MUST normalize validator and reviewer payloads into typed internal values before they reach workflow state or the Completion Gate.

#### Scenario: Issues collection is null
- **WHEN** a validator or reviewer returns `issues=null` or `structured_issues=null`
- **THEN** the boundary normalizer produces an empty collection and Completion Gate evaluation does not raise an exception

#### Scenario: Issues collection is malformed
- **WHEN** an issues field has a value that violates its collection contract
- **THEN** the runtime records a typed validation-contract blocker and fails closed without replacing unrelated scoped blockers

### Requirement: Automatic completion repair budget is task-local
The automatic completion-repair iteration count MUST belong to one task epoch and MUST NOT leak into a distinct map task.

#### Scenario: A task exhausts its repair budget
- **WHEN** one map task reaches its configured automatic iteration limit
- **THEN** that task pauses or fails with a typed budget outcome without changing the budget available to a later task

#### Scenario: The same task resumes
- **WHEN** a paused task is explicitly resumed from its checkpoint
- **THEN** its existing iteration count is restored rather than reset as a new task

### Requirement: Dedicated resume authorization is one-shot and request-scoped
The dedicated map-task resume command MUST create one authorization bound to the paused task lineage, and the next user request MUST atomically capture and clear it before fallible request processing. A failure, rejection, or early return MUST NOT authorize a later request.

#### Scenario: The authorized resume request is accepted
- **WHEN** the next user request consumes a dedicated resume authorization for the same resumable task lineage
- **THEN** that request may restore the checkpoint exactly once without widening target, tool, write, or permission scope

#### Scenario: The consuming request exits early
- **WHEN** the request captures the authorization and then fails classification, has no active Frame, raises an exception, or returns early
- **THEN** the authorization remains consumed and the following ordinary message is not classified as a map edit from historical state

#### Scenario: A persisted authorization is loaded
- **WHEN** a Session restarts after the dedicated command but before the next user request
- **THEN** the authorization remains bound to the same task lineage until one request atomically captures it

### Requirement: Completion lifecycle semantics cover every workflow status
Completion-candidate eligibility and an allowed Completion Gate outcome MUST have explicit behavior for every workflow status.

#### Scenario: Running task passes the Gate
- **WHEN** an active `running` task has a current completion candidate and the Gate allows completion
- **THEN** the reducer transitions the task to `completed` exactly once

#### Scenario: Completed outcome is replayed
- **WHEN** the identical committed completion outcome is replayed for a task already in `completed`
- **THEN** the task remains completed without another state transition or duplicated completion effects

#### Scenario: Paused task is evaluated
- **WHEN** a paused task reaches Completion Gate evaluation
- **THEN** the Gate returns a workflow-paused blocker and does not complete the task

#### Scenario: Idle or cancelled task retains a stale candidate
- **WHEN** an `idle` or `cancelled` task still carries a historical completion-candidate marker
- **THEN** the marker is invalidated and the response cannot be reported as successful task completion

#### Scenario: Task lifecycle invalidates an old candidate
- **WHEN** the task is cancelled, replaced, or starts a distinct epoch
- **THEN** the prior lineage's completion-candidate identity is cleared

### Requirement: Attempt failure does not implicitly terminate its task
The runtime MUST model a durable task separately from its request, model, tool, plan-step, and publication attempts. Every non-terminal attempt problem MUST preserve a task checkpoint and transition the task only to `running`, `recovering`, `waiting_frontend`, or `paused`. It MUST NOT implicitly transition the task to `idle`, `cancelled`, `completed`, or `failed_permanently`.

#### Scenario: A provider attempt is exhausted
- **WHEN** the configured provider attempt and fallback fail while a map task is active
- **THEN** the runtime persists the attempt outcome and a resumable task checkpoint, then enters bounded recovery or a typed provider-exhausted pause without completing or cancelling the task

#### Scenario: The response transport closes
- **WHEN** the HTTP response body or event delivery closes after task execution has begun
- **THEN** transport loss is recorded against the affected attempt identity and cannot by itself change durable task lifecycle or discard the task checkpoint

#### Scenario: A recoverable coordinated-publication conflict occurs
- **WHEN** Session or artifact publication returns a typed conflict whose original committed data is preserved
- **THEN** the task enters recovery with the preserved checkpoint and the required fresh-turn action rather than becoming idle or terminal

### Requirement: Task terminal transitions are closed and explicit
A task SHALL enter `completed` only through an allowed Completion Gate outcome, `cancelled` only through explicit cancellation, and `failed_permanently` only after the runtime proves that no safe automatic or user-directed recovery is available. User interruption, provider exhaustion, retry-budget exhaustion, ambiguous commit state, transport loss, and recoverable integrity conflicts MUST produce a checkpointed recovery or pause state.

#### Scenario: The user stops current execution
- **WHEN** the user interrupts an active task without explicitly cancelling it
- **THEN** the task persists a resumable pause and does not enter `cancelled` or `failed_permanently`

#### Scenario: Automatic recovery reaches its bound
- **WHEN** the applicable persisted recovery budget is exhausted
- **THEN** the task pauses with the first root cause, attempt history, side-effect state, and recovery guidance instead of reporting completion or looping indefinitely

#### Scenario: Completion Gate succeeds
- **WHEN** the current task lineage satisfies the allowed Completion Gate outcome
- **THEN** the reducer performs the single authoritative transition to `completed`

### Requirement: Capture paths reject malformed Godot scheme spellings
Screenshot and image-review path validation MUST reject malformed lookalikes of the accepted `res://` and `user://` schemes before project-relative path resolution.

#### Scenario: Capture path uses a single-slash pseudo-scheme
- **WHEN** a capture or image-review path begins with `user:/` or `res:/` but not the valid double-slash form
- **THEN** the runtime returns a structured invalid-path result and does not reinterpret it below the project root

#### Scenario: Capture path uses a colon-only pseudo-scheme
- **WHEN** a capture or image-review path begins with `user:` or `res:` without `//`
- **THEN** the runtime rejects it before filesystem access

#### Scenario: A path list contains a malformed scheme
- **WHEN** any element of a list-valued capture path argument is non-string or uses a malformed Godot scheme
- **THEN** the entire path validation fails with a typed invalid-argument result

### Requirement: Specialized result schema is satisfiable
The runtime SHALL ensure that a specialized map-worker result schema is internally consistent: when a frozen Frame constraint pins a field to a `const`, the specialization SHALL remove or widen any co-existing `enum` so the `const` value is admissible. A worker that outputs the frozen Frame values SHALL pass schema validation regardless of the frame's stage, including the `orchestrator` stage.

#### Scenario: Orchestrator frame completes
- **WHEN** a map-agent orchestrator frame with `stage = "orchestrator"` produces a final result whose `stage` equals the frozen frame value
- **THEN** the specialized schema accepts the result and the runtime does not flag `stage` as a contract violation

#### Scenario: Const contradicts the base enum
- **WHEN** a frozen frame constraint sets a `const` whose value is not in the field's base `enum`
- **THEN** the specialization drops or widens the `enum` so the `const` is admissible, rather than producing an unsatisfiable field

### Requirement: Failure frontier persists the structured repair plan
The reducer SHALL store the validator's full `repair_plan`/`issue_details` alongside `error_code` and `blocked_reason` in the scoped failure frontier, and SHALL NOT reduce a validation failure to its error code alone. The persisted repair plan SHALL survive message compaction because it lives in `map_task_state`, not in the conversation history.

#### Scenario: Validation failure is recorded
- **WHEN** a platform plan validation fails with a per-field `repair_plan`
- **THEN** the reducer stores the repair plan in the scoped failure frontier and the actionable failure details persist independently of the tool-result message

#### Scenario: Conversation compacts after a failure
- **WHEN** the tool-result message carrying the repair plan scrolls out of the recent message window and is summarized
- **THEN** the repair plan remains available in the failure frontier and can be re-surfaced without re-running validation

### Requirement: Map-progress digest is surfaced to the agent context each turn
The runtime SHALL re-derive a compact map-progress digest from authoritative `map_task_state` — current revision, stage, and the latest scoped failure `error_code` plus its persisted `repair_plan` — and SHALL inject it into the agent's per-turn context. The digest SHALL be re-derived from state on every turn, including the turn immediately following compaction, so it does not depend on the LLM summarizer preserving tool-result history.

#### Scenario: Agent turn begins after compaction
- **WHEN** a new agent turn begins after conversation compaction removed older tool-result messages
- **THEN** the agent context still carries the current map revision, stage, and latest failure repair plan, re-derived from state

#### Scenario: No active map task
- **WHEN** no map task is active in the session
- **THEN** the runtime injects no map-progress digest and the agent context is unchanged

