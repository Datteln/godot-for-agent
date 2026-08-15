## MODIFIED Requirements

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
