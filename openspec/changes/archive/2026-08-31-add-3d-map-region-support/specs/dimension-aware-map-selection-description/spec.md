## ADDED Requirements

### Requirement: Tilemap selection description supports 2D and 3D map node types
`describe_tilemap_selection` SHALL accept a selected `TileMapLayer`, legacy `TileMap`, or `GridMap` and SHALL return the resolved path, concrete node type, and map dimension. The tool SHALL retain its existing public name for compatibility.

#### Scenario: Describe a selected TileMapLayer
- **WHEN** the selected node is a TileMapLayer
- **THEN** the tool SHALL return its path, node type, and a two-dimensional map classification

#### Scenario: Describe a selected legacy TileMap
- **WHEN** the selected node is a legacy TileMap
- **THEN** the tool SHALL return its path, node type, and a two-dimensional map classification

#### Scenario: Describe a selected GridMap
- **WHEN** the selected node is a GridMap
- **THEN** the tool SHALL return its path, node type, and a three-dimensional map classification

### Requirement: Tilemap selection description provides dimension-matched next-step guidance
The tool result and map-agent guidance SHALL state the appropriate bounded-region parameters for the selected dimension. Two-dimensional maps SHALL be guided to x, y, width, height and applicable layer selection; GridMap SHALL be guided to x, y, z, width, height, depth without `map_layer`.

#### Scenario: GridMap selection informs later region capture
- **WHEN** a GridMap is selected and described
- **THEN** the result SHALL identify the six three-dimensional cell-bound fields required by region inspection and screenshot capture

#### Scenario: Non-map selection is described
- **WHEN** no map node is selected or the selected node is not a supported map type
- **THEN** the tool SHALL return a machine-readable unsupported-selection result and identify the supported map node types
