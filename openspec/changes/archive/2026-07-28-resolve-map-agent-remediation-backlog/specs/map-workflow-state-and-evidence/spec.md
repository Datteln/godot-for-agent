## ADDED Requirements

### Requirement: Map workflow state changes only through events
The system MUST route stage, blocker, checkpoint, batch, validation, and no-progress changes through a single Map Workflow reducer.

#### Scenario: Agent requests a stage transition
- **WHEN** Agent orchestration submits a valid map workflow event
- **THEN** the reducer applies the transition and records the event without direct state-field assignment by the Agent

#### Scenario: QueryEngine requests a state update
- **WHEN** QueryEngine receives a map tool result
- **THEN** it submits an event instead of directly modifying MapTaskState fields

### Requirement: Workflow state is scoped by target and revision
The reducer SHALL organize blockers, checkpoints, batches, evidence, and progress under a canonical `(target, revision)` scope.

#### Scenario: A new revision is observed
- **WHEN** a canonical target advances to a new revision
- **THEN** state from the previous revision cannot satisfy gates for the new revision

### Requirement: Worker results match the frozen Frame contract
The runtime MUST validate result schema, stage, target, revision, worker instance, and next stage against the Frame contract before consuming a worker result.

#### Scenario: Worker spoofs its stage
- **WHEN** a worker result declares a stage different from its Frame contract
- **THEN** the runtime returns a typed contract violation and does not advance workflow state

#### Scenario: Worker returns an illegal next stage
- **WHEN** a result's `next_stage` is not allowed by the frozen contract
- **THEN** the runtime rejects the result and records the violation

#### Scenario: Worker returns the wrong revision
- **WHEN** a result revision differs from the Frame target revision
- **THEN** the result cannot update checkpoints, blockers, validation, or completion

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
