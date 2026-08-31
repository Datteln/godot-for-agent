# Map Visual Verification Gate Spec

## Purpose

Define the deterministic map-region evidence required before a map task may report tile-placement completion, and the distinct representation of capture, visual-observation, and deterministic verification status in map results.

## Requirements

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

### Requirement: Focused map screenshots resolve the declared layer and bounds
For map-verification evidence, the system SHALL require a TileMap path, an explicit valid map layer, and finite cell bounds. The frontend SHALL return the requested and resolved layer and bounds, map-local rectangle, and viewport/crop rectangle. A missing layer SHALL be rejected and MUST NOT default to layer 0.

#### Scenario: Mid-layer evidence is captured
- **WHEN** a caller requests map-verification evidence for `TileMap`, layer `1`, and a finite cell rectangle
- **THEN** the result SHALL report layer `1` as both the requested and resolved layer
- **AND** the result SHALL retain the requested/resolved bounds and capture geometry

#### Scenario: Map layer is omitted
- **WHEN** a caller requests map-verification evidence without a map layer
- **THEN** the system SHALL return a validation failure
- **AND** it SHALL NOT report capture or verification success

#### Scenario: Generic viewport misses the requested cells
- **WHEN** a captured viewport does not intersect the requested map-local rectangle after padding
- **THEN** the system SHALL retain it only as diagnostic evidence and return failed focused-map validation

### Requirement: Map rebuilds report mutation and focused evidence scope
The system SHALL classify a map rebuild that can clear, set, or erase TileMap cells as mutating. The rebuild result SHALL include changed-cell bounds or an explicit unavailable status. A generic editor viewport screenshot MUST NOT satisfy focused map-verification evidence.

#### Scenario: Rebuild changes tiles
- **WHEN** a map rebuild clears, sets, or erases TileMap cells
- **THEN** the tool metadata SHALL mark the operation as mutating
- **AND** the result SHALL include changed-cell bounds or `changed_bounds_unavailable`

#### Scenario: Rebuild returns only a generic screenshot
- **WHEN** a map rebuild returns a whole-editor viewport image without a passed focused-map capture
- **THEN** the workflow SHALL report the rebuild as executed but visually unverified