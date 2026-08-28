## ADDED Requirements

### Requirement: Tile-placement completion requires deterministic map evidence
For a map task that claims tiles were added, removed, or changed, the system SHALL require a successful deterministic map-region observation for the declared target node, layer, bounds, and expected cell result before reporting completion. A screenshot observation MUST be classified as advisory and MUST NOT independently satisfy this requirement.

#### Scenario: Region evidence confirms a generated floor
- **WHEN** a rebuild reports cells written and `describe_map_region` confirms the expected cells in the declared layer and bounds
- **THEN** the map task may report deterministic placement verification and include any visual observation as supplementary evidence

#### Scenario: Screenshot succeeds without region evidence
- **WHEN** a map rebuild produces a screenshot with capture or visual-observation success but no matching map-region result
- **THEN** the task remains unverified and MUST NOT report tile-placement completion

### Requirement: Map results distinguish capture, observation, and verification
The system SHALL present map evidence using distinct labels for capture status, visual-observation status, and deterministic verification status. A capture result of `ok: true` MUST NOT be rendered or summarized as visual verification or map completion.

#### Scenario: Visual analysis is unavailable
- **WHEN** a map rebuild captures a screenshot but its visual analysis is unavailable
- **THEN** the map result reports successful capture, unavailable visual observation, and the independent deterministic verification status
