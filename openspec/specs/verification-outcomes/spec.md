# verification-outcomes Specification

## Purpose
TBD - created by archiving change stabilize-agent-workflow-reliability. Update Purpose after archive.

## Requirements

### Requirement: Verification distinguishes passed, failed, and unavailable
Every syntax or semantic verification attempt MUST produce the canonical versioned `VerifyOutcome` whose status is exactly `passed`, `failed`, or `unavailable`. `failed` MUST mean an executed verifier found an issue; read failure, missing dependencies, provider failure, timeout, malformed output, missing owning Frame, and exhausted budget MUST be `unavailable`.

#### Scenario: Semantic verifier finds a defect
- **WHEN** the semantic verifier executes successfully and returns blocking issues
- **THEN** the outcome is `failed`, contains normalized issues, and identifies the verifier phase

#### Scenario: Edited target cannot be read
- **WHEN** verification cannot read the target within the project security boundary
- **THEN** the outcome is `unavailable` with `target_unreadable` and does not report success

#### Scenario: Verifier response is malformed
- **WHEN** provider content cannot be parsed or validated
- **THEN** the outcome is `unavailable` with `response_malformed` and preserves safe diagnostics without inventing issues

### Requirement: Unavailable verification supplies closed recovery actions
An unavailable outcome MUST include a typed reason, retry permission, persisted attempt/budget identity, and a closed list of applicable recovery actions. Actions MUST be runtime validated and MUST NOT grant new file, tool, model, or mutation authority. Selection-dependent map reads (such as `describe_tilemap_selection`) MUST NOT surface a bare failure when the editor selection is empty; they SHALL deterministically fall back to the primary or first compatible TileMapLayer and record the fallback in the result, or, when no compatible layer exists, return a typed unavailable outcome with closed recovery actions.

#### Scenario: Target path may be stale
- **WHEN** verification is unavailable because the target cannot be read and budget permits recovery
- **THEN** the outcome offers only applicable actions such as `reread_target` or `rediscover_target` under the original project boundary

#### Scenario: Provider is unavailable
- **WHEN** the verifier and configured fallback are exhausted
- **THEN** the outcome may offer deterministic checks or `pause_unverified` but not an unconfigured model or reset budget

#### Scenario: Recovery action is repeated
- **WHEN** the same action is requested under an exhausted verification identity
- **THEN** the runtime rejects it and returns the terminal unavailable outcome without another provider call

#### Scenario: Empty editor selection falls back deterministically
- **WHEN** `describe_tilemap_selection` is invoked while no TileMapLayer is selected and the scene contains at least one compatible layer
- **THEN** the tool describes the primary or first compatible TileMapLayer and the result records the fallback target and reason

#### Scenario: No compatible layer exists
- **WHEN** `describe_tilemap_selection` is invoked with empty selection and no compatible TileMapLayer in the edited scene
- **THEN** the tool returns a typed unavailable result with a closed recovery action list instead of a bare error string

### Requirement: The agent receives the exact verification cause and bounded alternatives
The owning agent MUST receive the same status, reason, summary, issues, and permitted recovery actions emitted to UI. Guidance MUST permit at most one applicable recovery action per unavailable outcome and prohibit claiming successful verification while unavailable.

#### Scenario: Deterministic alternative is available
- **WHEN** semantic verification is unavailable but a configured deterministic validator applies
- **THEN** the agent may select that typed action and receives its outcome under the same verification identity

#### Scenario: Required verification remains unavailable
- **WHEN** completion requires verification and all actions are exhausted
- **THEN** the workflow pauses at a typed unverified checkpoint and reports cause and next action

#### Scenario: Advisory verification remains unavailable
- **WHEN** verification is advisory and policy permits continuation
- **THEN** final evidence and user text explicitly mark the artifact unverified

### Requirement: VerifyOutcome is the only accepted verification protocol
The runtime MUST NOT emit or accept a legacy `passed: bool` verification payload, derived compatibility projection, `VerifyOutcomeV2` adapter, or ambiguous persisted pass record. Unsupported payloads MUST fail closed with a typed schema error.

#### Scenario: Legacy boolean payload is received
- **WHEN** a component submits a verification object containing only `passed`
- **THEN** the runtime returns `unsupported_verify_schema` and does not infer passed, failed, or unavailable

#### Scenario: Release protocol inventory runs
- **WHEN** schemas, serializers, history projections, and frontend DTOs are inspected
- **THEN** only canonical VerifyOutcome fields exist and no boolean compatibility field is exposed