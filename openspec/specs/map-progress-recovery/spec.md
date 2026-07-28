# map-progress-recovery Specification

## Purpose

Define measurable map-task progress, bounded recovery, typed pauses, model fallback, and resumable checkpoints.

## Requirements

### Requirement: Structured-output repair is observable
The runtime MUST preserve structured validation issues, repair actions, repair attempt number, and a stable error category whenever it repairs worker output.

#### Scenario: Worker output is repaired successfully
- **WHEN** malformed structured output can be repaired
- **THEN** the parent result and logs include the original issue categories and applied repair actions without exposing unsafe raw content

#### Scenario: Same repair failure repeats
- **WHEN** the same structured issue category repeats beyond its configured threshold
- **THEN** the runtime stops retrying and returns a typed repair-exhausted result

#### Scenario: Structured issues are null or malformed
- **WHEN** worker output contains `validation.structured_issues=null`, a non-array value, or omits the field
- **THEN** repair normalizes it to an empty issue list and all later category aggregation consumes only that normalized list without raising an iteration error

### Requirement: Retry identity is semantic and scoped
The system SHALL aggregate retries by stage, target, revision, normalized operation signature, and error category.

#### Scenario: Equivalent requests differ only in formatting
- **WHEN** two attempts have semantically identical inputs under the same scope
- **THEN** they increment the same retry counter

#### Scenario: Error category changes
- **WHEN** a later attempt fails for a different structured category
- **THEN** it is tracked separately while preserving the original root cause

### Requirement: Missing inputs trigger reader recovery
Structured `missing_inputs` MUST create a reader step whose typed result is bound to the retried step.

#### Scenario: Planner lacks current collision facts
- **WHEN** planner returns `missing_inputs` for canonical region facts
- **THEN** the scheduler runs a reader step and passes its result into a new planner attempt

#### Scenario: Reader cannot provide required facts
- **WHEN** the reader returns a typed missing or incompatible result
- **THEN** the original step becomes blocked instead of repeating the same planner call

### Requirement: No-progress pause reports the first root cause
When progress thresholds are exceeded, the pause result MUST include the first root cause, per-category counts, stage, target, revision, last attempt, and recovery guidance.

#### Scenario: Multiple failures lead to pause
- **WHEN** retries across one scoped operation reach the no-progress threshold
- **THEN** the task pauses with the earliest causal failure rather than only the final symptom

### Requirement: Pause causes are typed and truthfully rendered
Every paused map task MUST record a typed pause kind and produce a non-empty recovery report whose user-facing message matches the actual cause.

#### Scenario: The client watchdog interrupts an otherwise progressing request
- **WHEN** `/chat/interrupt` is received with `cause=client_timeout`
- **THEN** the checkpoint records `pause_kind=client_timeout`, preserves the resumable task state, and reports a client-wait timeout rather than continuous no-progress

#### Scenario: The user explicitly stops a task
- **WHEN** the user invokes stop and interrupt carries `cause=user_interrupted`
- **THEN** the task reports that it was paused by the user and does not describe the pause as model failure or no-progress exhaustion

#### Scenario: A pause has no specialized report payload
- **WHEN** a paused checkpoint lacks a category-specific report
- **THEN** the runtime synthesizes a minimal structured report from pause kind, stage, checkpoint, and unresolved issues and never renders an empty `{}` as recovery guidance

#### Scenario: No-progress actually reaches its threshold
- **WHEN** semantic no-progress counters reach the configured threshold
- **THEN** the task records `pause_kind=no_progress_exhausted` and only this pause kind may use continuous-no-progress wording

### Requirement: Model timeout fallback is owned by the backend attempt
The backend LLM provider SHALL retry a failed or timed-out primary model attempt with the configured fallback model at most once before reporting provider exhaustion.

#### Scenario: Primary model attempt times out before producing a durable result
- **WHEN** a fallback model is configured and differs from the primary model
- **THEN** the provider discards provisional output from the failed attempt, emits `agent_model_fallback`, and retries the same messages and tools with the fallback model

#### Scenario: The outer chat watchdog expires
- **WHEN** the frontend stops receiving both committed events and backend liveness heartbeats
- **THEN** it may interrupt with `cause=client_timeout` but MUST NOT replay `/chat`, resubmit tool results, or choose the fallback model itself

#### Scenario: Fallback is unavailable or also fails
- **WHEN** no distinct fallback is configured or the fallback attempt fails
- **THEN** the runtime returns a typed provider-exhausted result and preserves a resumable checkpoint when a map task is active

### Requirement: Worker prompts contain mode-specific task guidance only
Dynamic worker prompts SHALL be selected by worker mode and SHALL NOT duplicate stage transitions, tool whitelists, result schema, resource rules, or recovery state machines owned by runtime contracts.

#### Scenario: Write worker prompt is generated
- **WHEN** a write-mode worker is created
- **THEN** its prompt contains write-task guidance while structured contracts provide stage, tools, schema, resources, and recovery rules

### Requirement: Map target recovery preserves NodePath semantics
The runtime MUST treat an omitted `target_path` as a request for compatible-map inference and MUST treat `"."` as the actual scene-root NodePath rather than an automatic-selection marker.

#### Scenario: Target path is omitted
- **WHEN** the selected node is a compatible map or the scene contains exactly one compatible map node
- **THEN** the map read resolves that node according to the tool's documented inference rules

#### Scenario: Dot resolves to a non-map scene root
- **WHEN** `target_path="."` resolves to a node that is not a TileMapLayer, TileMap, or GridMap
- **THEN** the tool returns structured `unsupported_map_type` with a safe compatible-node candidate or guidance to omit `target_path`

#### Scenario: The same invalid dot target is retried
- **WHEN** an operation has already failed because `"."` resolved to a non-map root and no new target fact was obtained
- **THEN** no-progress control prevents an identical retry and routes to compatible target discovery or reports the missing input
