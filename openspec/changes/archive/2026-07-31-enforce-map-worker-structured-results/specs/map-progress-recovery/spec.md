## MODIFIED Requirements

### Requirement: Structured-output repair is observable
The runtime MUST attempt bounded correction of malformed or contract-invalid worker output inside the same Frame before applying conservative repair. It MUST preserve structured validation issues, response-format mode, frame-local correction attempt, task-level semantic attempt, repair actions, and a stable error category without exposing unsafe raw content.

#### Scenario: Worker output is corrected successfully
- **WHEN** a final map-worker result fails structured validation and a remaining frame-local correction attempt is available
- **THEN** the runtime keeps the Frame and gathered tool facts, supplies a safe validation diagnostic, performs another deterministic text-only final turn, and publishes the corrected result without rerunning map reads

#### Scenario: Worker output requires conservative repair
- **WHEN** malformed structured output remains invalid after the frame-local correction bound
- **THEN** the runtime produces one typed fail-closed partial result whose validation blocks completion and records the original issue categories and applied repair actions

#### Scenario: Same repair failure repeats
- **WHEN** semantically equivalent worker Frames exhaust structured correction beyond the configured task-level threshold
- **THEN** the runtime stops creating replacement workers and returns a typed repair-exhausted pause with the first root cause and recovery guidance

#### Scenario: Structured issues are null or malformed
- **WHEN** worker output contains `validation.structured_issues=null`, a non-array value, or omits the field
- **THEN** correction reports the schema violation and conservative repair normalizes it to an empty issue list so later category aggregation cannot raise an iteration error

## ADDED Requirements

### Requirement: Final structured generation is deterministic and capability-aware
The backend SHALL perform final text-only map-worker generation with deterministic per-call settings and SHALL select `json_schema`, `json_object`, or `prompt_only` response mode from explicit provider/model capabilities. Unsupported native response formatting MUST fall back narrowly without replaying completed tool turns.

#### Scenario: Provider supports strict JSON Schema
- **WHEN** the selected provider/model is configured for `json_schema`
- **THEN** the final text-only request carries the full specialized schema as a strict native response contract and local validation remains authoritative

#### Scenario: Provider supports JSON objects only
- **WHEN** the selected provider/model is configured for `json_object`
- **THEN** the request requires one JSON object and the runtime system contract supplies the specialized schema for semantic guidance

#### Scenario: Provider has no native structured-output support
- **WHEN** the selected provider/model is configured for `prompt_only`
- **THEN** the runtime supplies the specialized schema and minimal valid example in the system contract and validates the returned text locally

#### Scenario: Provider rejects a response-format feature
- **WHEN** the provider rejects the configured native response-format parameter before producing content
- **THEN** the backend retries the same final turn once in the next configured response mode without repeating tools, changing Frame identity, or consuming semantic retry budget

#### Scenario: Final structured turn is issued
- **WHEN** any response mode generates the final map-worker object
- **THEN** the call has no tools, uses temperature zero and the configured bounded thinking policy, and does not mutate the Agent's sampling settings for other turns

### Requirement: Structured retry identities distinguish correction from no progress
The runtime MUST count frame-local formatting correction separately from task-level semantic repetition and MUST correlate both identities in diagnostics and persisted recovery state.

#### Scenario: Same Frame needs one correction
- **WHEN** a worker's first final response is invalid and its next final response is valid
- **THEN** the frame-local attempt records the correction while the task does not consume a semantic replacement-worker attempt

#### Scenario: One Frame exhausts correction
- **WHEN** every allowed final response in the same Frame remains invalid
- **THEN** the runtime increments the matching task-level semantic failure once and emits one conservative partial result

#### Scenario: Parent creates equivalent replacement workers
- **WHEN** multiple replacement workers have the same task lineage, stage, target, revision, operation, and structured error category
- **THEN** their exhausted Frames increment the same task-level semantic counter and cannot restart the convergence budget at local attempt one

#### Scenario: Parallel workers correct independently
- **WHEN** a delegate group contains multiple map-worker Frames
- **THEN** each Frame has an isolated local correction count while task-level semantic accounting preserves each worker's scoped operation identity
