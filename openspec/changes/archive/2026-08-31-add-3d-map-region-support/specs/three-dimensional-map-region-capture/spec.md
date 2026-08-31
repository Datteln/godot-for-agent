## ADDED Requirements

### Requirement: Screenshot capture accepts a bounded GridMap region in 3D mode
The screenshot tool SHALL accept `mode: "3d"` with `target.type: "map_region"` only when `target.path` resolves to a `GridMap`. The target SHALL include integer `cell_bounds.x`, `cell_bounds.y`, `cell_bounds.z`, `cell_bounds.width`, `cell_bounds.height`, and `cell_bounds.depth`, with all dimensions greater than zero.

#### Scenario: Capture a valid GridMap cell cuboid
- **WHEN** a caller supplies a GridMap path and valid finite three-dimensional cell bounds
- **THEN** the tool SHALL capture the requested cuboid through the 3D viewport path and return the requested bounds and computed world capture bounds

#### Scenario: Reject a non-GridMap map-region target
- **WHEN** a 3D `map_region` target resolves to a node other than GridMap
- **THEN** the tool SHALL return a machine-readable invalid-target error without attempting capture

#### Scenario: Reject incomplete or invalid three-dimensional bounds
- **WHEN** a 3D `map_region` target omits z or depth, uses a non-integer bound, or uses a non-positive dimension
- **THEN** the tool SHALL return a machine-readable bounds validation error without attempting capture

### Requirement: 3D map-region capture has an explicit dimension-specific input contract
The tool SHALL require no `map_layer` for a 3D GridMap region and SHALL reject a supplied `map_layer` as inapplicable. The tool schema and map-agent guidance SHALL distinguish this contract from the existing 2D TileMap map-region contract.

#### Scenario: Caller supplies map_layer for a GridMap region
- **WHEN** a 3D `map_region` request includes `map_layer`
- **THEN** the tool SHALL return a machine-readable inapplicable-field error that identifies `map_layer`

#### Scenario: Existing two-dimensional request is used
- **WHEN** a caller submits a valid 2D TileMap or TileMapLayer `map_region` request
- **THEN** the tool SHALL preserve the existing two-dimensional parsing and capture behavior

### Requirement: 3D map-region capture preserves camera state and evidence scope
The tool SHALL reuse the 3D capture lifecycle, including viewport selection, camera state restoration, and screenshot output behavior. Its result SHALL identify the bounded region and SHALL not represent a screenshot as semantic proof of map-cell placement or map preservation.

#### Scenario: Capture completes after temporary camera framing
- **WHEN** a valid GridMap region is captured with a pre-existing editor camera
- **THEN** the tool SHALL restore the camera state after capture and return the screenshot result with bounded-region evidence metadata

#### Scenario: Requested region has no visible content
- **WHEN** a valid requested GridMap cuboid has no visible mesh content
- **THEN** the tool SHALL return an explicit visibility or occupancy warning and SHALL not claim that an edit was verified
