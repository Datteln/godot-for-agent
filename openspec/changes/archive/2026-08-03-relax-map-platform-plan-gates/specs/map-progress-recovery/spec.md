## ADDED Requirements

### Requirement: Platform plan resubmission is anti-thrash by semantic fingerprint only
The runtime SHALL prevent repeated submission of semantically identical platform plans using a stable plan fingerprint and SHALL NOT impose a per-plan hard count ceiling on distinct revised plans. Distinct revised plans (different fingerprint) MUST remain submittable and are governed by the general no-progress pause, not by a plan-specific revision count limit.

#### Scenario: Identical plan is resubmitted
- **WHEN** a platform plan with the same fingerprint as a prior attempt is resubmitted
- **THEN** the runtime rejects it as a duplicate and requires concrete field changes before retrying

#### Scenario: Distinct revised plan is submitted after prior failures
- **WHEN** a plan whose fingerprint differs from every prior attempt is submitted after one or more prior failures
- **THEN** the runtime accepts it for validation regardless of how many prior distinct attempts occurred, until the general no-progress threshold is reached

### Requirement: Platform validation retry identity is platform-tool scoped
The `validation_failure` semantic retry used to exhaust the platform planner SHALL be recorded only for failed platform-plan validation tools. Failures from other map plan-category tools MUST NOT increment or replace that platform-validation retry identity.

#### Scenario: Non-platform plan tool fails
- **WHEN** `plan_map_layout` or `plan_map_algorithms` returns a non-executable outcome
- **THEN** the runtime handles the failure without recording the platform planner's `validation_failure` semantic retry

#### Scenario: Platform plan validation fails
- **WHEN** a platform-plan validation tool returns a non-executable outcome
- **THEN** the runtime records the scoped `validation_failure` retry used by the platform planner exhaustion path

### Requirement: Final structured generation uses a non-zero thinking budget
The final map-worker structured-output turn SHALL use a non-zero thinking budget derived from the active effort tier. A zero thinking budget for the final structured turn is prohibited, so the hardest output task is not starved of reasoning relative to ordinary turns.

#### Scenario: Final structured turn is issued
- **WHEN** a map-worker final text-only structured turn runs
- **THEN** its thinking budget is non-zero and tracks the active effort tier, not zero

#### Scenario: Effective fallback budget is recorded
- **WHEN** the configured structured thinking budget is zero and the final structured turn falls back to the effort-tier budget
- **THEN** the provider call, frame evidence, persisted session, and structured diagnostic event report the same effective non-zero budget

### Requirement: Frame-local structured-output correction floor is at least two
The runtime SHALL allow at least two frame-local correction attempts for malformed or contract-invalid map-worker structured output before applying conservative repair, and SHALL NOT configure a correction floor of one or zero.

#### Scenario: Second correction is issued after a failed first
- **WHEN** a worker's first correction attempt still produces invalid structured output
- **THEN** the runtime issues a second correction attempt rather than failing closed

#### Scenario: Production settings supply the correction floor
- **WHEN** structured map-worker output is enabled and no deployment override is supplied
- **THEN** the production settings and every explicit engine call path supply a correction limit of at least two

#### Scenario: Correction floor is exhausted
- **WHEN** both allowed correction attempts fail to produce valid structured output
- **THEN** the runtime produces one conservative fail-closed partial result with the original issue categories

### Requirement: A successful non-platform-plan plan tool advances to write
When a `plan`-category map plan tool (`plan_map_layout`, `plan_map_algorithms`) completes with an executable outcome, the runtime SHALL advance the map task to the write stage and SHALL NOT hold the task in the plan stage on account of a sibling scope's pending planner workflow.

#### Scenario: Successful plan advances despite a sibling pending planner workflow
- **WHEN** a `plan_map_layout` or `plan_map_algorithms` call produces an executable outcome while another scope for the same target and revision has a workflow with `next_stage == "planner"`
- **THEN** the runtime transitions the map task to the write stage rather than remaining in the plan stage
