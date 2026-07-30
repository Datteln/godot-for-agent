## MODIFIED Requirements

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

## ADDED Requirements

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

### Requirement: Structured-output contracts remain runtime-owned
Worker task text MUST contain objective and input references only, while the runtime contract supplies the schema, immutable Frame constraints, response-format mode, and correction rules.

#### Scenario: Automatic worker Frame is created
- **WHEN** the scheduler creates a dynamic or recovery map worker
- **THEN** its task text does not duplicate a hand-maintained result field list or provider-format instruction

#### Scenario: Prompt-only provider is selected
- **WHEN** the configured provider cannot accept a native structured-response contract
- **THEN** the runtime serializes the same specialized schema and a minimal valid example into a system contract rather than modifying the task text
