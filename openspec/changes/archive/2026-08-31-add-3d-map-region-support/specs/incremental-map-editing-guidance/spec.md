## ADDED Requirements

### Requirement: Builder lifecycle operations compile the current script before proceeding
For a code-driven map builder, the frontend SHALL compile the current on-disk builder script after a script write and immediately before `reload_map_targets` loads a builder script or `rebuild_map_builder` invokes a builder. If compilation fails, the operation SHALL stop before reload or rebuild and SHALL return `ok: false` with a stable compile error code, the resource path, available line/column diagnostics, bounded raw diagnostic text, and a repair-oriented next action.

#### Scenario: Builder write produces a compilation error
- **WHEN** an approved builder script edit leaves the current on-disk script uncompilable
- **THEN** the write result SHALL report `builder_script_compile_failed` and diagnostics, and the agent SHALL repair the script before requesting reload or rebuild

#### Scenario: Builder changed after a previous successful compilation
- **WHEN** reload or rebuild is requested for a builder whose current on-disk script is uncompilable
- **THEN** that operation SHALL freshly compile the current script, return the compile failure, and SHALL not rely on a prior successful validation

#### Scenario: Log collection returns compiler items
- **WHEN** `read_debugger_errors` collects one or more diagnostics
- **THEN** its tool-call success SHALL mean the collection completed and SHALL not be presented as evidence that Godot has no errors

### Requirement: Map agent selects dimension-matched region evidence
For a bounded local map edit, the map agent SHALL select region-inspection and screenshot inputs that match the observed map type. It SHALL use two-dimensional bounds and applicable layer selection for TileMap-based targets, and three-dimensional `GridMap` cell bounds for GridMap targets.

#### Scenario: Agent reviews a bounded GridMap edit
- **WHEN** the agent has observed that the edited target is a GridMap
- **THEN** it SHALL describe or capture the affected region with x, y, z, width, height, and depth rather than a two-dimensional layer-based target

#### Scenario: Agent reviews a bounded TileMap edit
- **WHEN** the agent has observed that the edited target is a TileMap or TileMapLayer
- **THEN** it SHALL retain the existing two-dimensional region and layer-aware evidence workflow

### Requirement: Map agent states the limits of visual map evidence
The map agent SHALL describe screenshots of either map dimension as visual evidence of the requested capture scope. It SHALL not treat a successful screenshot, reload, or camera framing result as semantic proof that map cells were authored or unrelated map content was preserved.

#### Scenario: Screenshot follows a local GridMap edit
- **WHEN** the agent reports a screenshot of a three-dimensional map region after an edit
- **THEN** it SHALL state the bounded region shown and distinguish visual observation from semantic verification
