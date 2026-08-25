## ADDED Requirements

### Requirement: Selected front-tool execution produces a complete result envelope
For every selected front-tool call, the frontend SHALL submit exactly one result envelope containing the originating non-empty `tool_use_id`, `frame_id`, current `turn_id`, and a terminal `status` of `applied`, `rejected`, or `error`. It MUST validate the envelope before HTTP serialization.

#### Scenario: Executor returns a malformed dictionary
- **WHEN** a selected tool executor returns a dictionary lacking any required result-envelope identity or status field
- **THEN** the frontend MUST replace it with an `error` result tied to the original tool call and MUST NOT submit a malformed `tool_results` request

#### Scenario: Executor reports a local tool failure
- **WHEN** a selected front tool fails locally
- **THEN** the frontend MUST preserve the original call identity and submit a complete `status="error"` result with a typed error code and bounded diagnostic result

### Requirement: Error results resume agent decision making
The service SHALL accept a complete `status="error"` front-tool result for a pending call, append it as error tool evidence to the originating agent frame, clear the resolved pending call, and resume the agent turn. The system MUST NOT turn a valid tool error into an HTTP validation failure or an implicit request termination.

#### Scenario: Precise edit becomes stale
- **WHEN** an approved generic edit returns `file_stale` as a complete error result
- **THEN** the agent receives that outcome and continues with a user-facing explanation, a fresh read, or a permitted alternative rather than terminating the pending request

#### Scenario: Error result in a multi-call batch
- **WHEN** a pending batch contains a complete error result for one call and complete results for every other pending call
- **THEN** the service resolves the batch, records every outcome, and resumes the agent with the error evidence included

### Requirement: Result protocol failures remain observable and fail closed
The frontend SHALL record bounded diagnostic metadata when it synthesizes a protocol-error result. The service MUST continue to reject unknown, mismatched, or incomplete tool identities and MUST NOT infer a pending call identity from an incomplete request.

#### Scenario: Result references an unknown pending call
- **WHEN** a complete result contains a tool-call id that does not match the session's pending tool-call ids
- **THEN** the service rejects the result without clearing pending state or assigning it to a different call
