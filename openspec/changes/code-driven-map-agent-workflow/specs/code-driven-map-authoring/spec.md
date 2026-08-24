## ADDED Requirements

### Requirement: Map requests use a dedicated code-driven workflow
The system SHALL route map and level authoring requests to a dedicated map-agent workflow. The workflow MUST inspect authoritative map facts and relevant readable project files, publish a map-oriented plan identifying the map target/layer, target files, and visual acceptance intent, obtain normal write approval, apply approved generic code/configuration edits, and then request reload and visual verification. It MUST preserve the generic code-edit path's project boundary, prior-read, stale-file, confirmation, transcript, and Undo guarantees.

#### Scenario: Supported map generator change
- **WHEN** a user asks to change a map whose generator or configuration is represented by supported readable project files
- **THEN** the map agent reads the existing map facts, presents the target/layer, affected files, and map-oriented plan, applies only approved generic file edits, and continues to reload and visual verification

#### Scenario: Stale target file
- **WHEN** a target file changes after the map agent reads it and before an approved precise edit is applied
- **THEN** the edit is rejected through the existing stale-file outcome and the map workflow does not continue to reload or claim the map changed

### Requirement: Map observation remains generally available and map authoring uses authoritative facts
The system SHALL retain `describe_tilemap_selection` and `describe_map_region` as general read-only observation tools. Any agent whose effective read-only tool set includes these tools MUST be able to use them without map-write authority. Before source changes that depend on an existing map target, layer, coordinates, existing cells, or tile identity, the map-agent workflow MUST obtain those facts through the inspection tools and MUST NOT infer them from screenshots or source text alone.

#### Scenario: Existing TileMapLayer target
- **WHEN** a requested map change applies to an existing TileMapLayer or legacy TileMap with potentially multiple layers
- **THEN** the map agent uses read-only inspection to identify the target and intended layer before it publishes an editable source-file plan

#### Scenario: Scene workflow aligns content to a map
- **WHEN** a non-map authoring workflow with read-only map-observation permission needs to align scene content with an existing TileMap, TileMapLayer, or GridMap
- **THEN** it can read the target's coordinate and layer facts without being granted any map mutation tool

#### Scenario: Source and screenshot leave tile identity ambiguous
- **WHEN** readable project files and screenshots do not establish the required TileSet source or atlas identity
- **THEN** the map agent reads the relevant map facts or reports missing facts, and does not guess a tile identity

### Requirement: Map authoring does not use map-specific mutation tools
The system MUST delete `edit_map`, `fill_rect`, and `paint_from_image_grid` from the tool registry, frontend executor dispatch, `MapTools` mutation implementations, agent definitions, routing, `edit_map`-specific budgets, previews, and mutation-specific tests. The system MUST NOT retain a feature flag, disabled compatibility mode, legacy route, or callable implementation for these tools. General read-only map inspection remains available.

#### Scenario: Map request after migration
- **WHEN** the user requests a supported map change after the code-driven workflow is enabled
- **THEN** the pending approval contains generic source/configuration edits and no map-cell mutation call, while preceding map inspection results remain visible as read-only facts

#### Scenario: Legacy mutation tool is requested
- **WHEN** any agent or frontend payload refers to `edit_map`, `fill_rect`, or `paint_from_image_grid` after the migration
- **THEN** the name cannot resolve to a registered or dispatchable tool and no map mutation implementation is invoked

#### Scenario: Opaque serialized map data
- **WHEN** the only apparent target is an opaque or serialized TileMap/GridMap cell-data blob rather than an allowed readable authoring file
- **THEN** the workflow returns a typed unsupported-authoring-target outcome and does not modify the blob

### Requirement: Map workflow reports evidence without overstating completion
The map workflow SHALL report distinct outcomes for file edits, reload, screenshot capture, and visual expectation. It MUST NOT describe a map request as semantically or gameplay verified solely because a file edit succeeded, a target reloaded, or a screenshot was captured.

#### Scenario: Screenshot captured after reload
- **WHEN** an approved code-edit batch reloads successfully and produces a screenshot
- **THEN** the final transcript identifies the edited files, reload result, screenshot scope, and that the evidence is visual only

#### Scenario: Reload or screenshot unavailable
- **WHEN** the edit succeeds but reload fails or screenshot capture is unavailable
- **THEN** the workflow records the specific unavailable or failure result and does not claim visual verification or task completion
