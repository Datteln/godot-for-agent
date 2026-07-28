## ADDED Requirements

### Requirement: Structured-output repair is observable
The runtime MUST preserve structured validation issues, repair actions, repair attempt number, and a stable error category whenever it repairs worker output.

#### Scenario: Worker output is repaired successfully
- **WHEN** malformed structured output can be repaired
- **THEN** the parent result and logs include the original issue categories and applied repair actions without exposing unsafe raw content

#### Scenario: Same repair failure repeats
- **WHEN** the same structured issue category repeats beyond its configured threshold
- **THEN** the runtime stops retrying and returns a typed repair-exhausted result

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
