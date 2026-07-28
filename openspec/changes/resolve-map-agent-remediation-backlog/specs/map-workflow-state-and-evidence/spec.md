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

#### Scenario: Bare continuation text is ambiguous
- **WHEN** the user says only a generic phrase such as "continue" without identifying the map edit and does not invoke the dedicated resume command
- **THEN** the runtime does not infer map-edit authorization solely from historical Session state

#### Scenario: Gate receives a valid completion candidate
- **WHEN** a response belongs to the current explicit map-edit lineage, has a canonical target and revision, and is marked as a map completion candidate
- **THEN** the Completion Gate evaluates validation, reviewer issues, blockers, workflow state, and scoped evidence

#### Scenario: Final response has no current map-edit lineage
- **WHEN** any `ChatFinalResponse` is produced without the current request's map-edit lineage and completion-candidate marker
- **THEN** the runtime MUST NOT call the Completion Gate or replace the response text

### Requirement: Task text does not duplicate runtime contracts
Automatically created child Frame task text MUST contain objective and input references only; role rules, schema instructions, stage transitions, and recovery rules SHALL come from structured runtime contracts.

#### Scenario: Automatic reader Frame is created
- **WHEN** runtime recovery creates a reader Frame
- **THEN** its task payload does not repeat the reader system prompt or result schema instructions
