## ADDED Requirements

### Requirement: Map requests use a dedicated code-driven workflow
The system SHALL route map and level authoring requests to a dedicated map-agent workflow. The workflow MUST inspect authoritative map facts and relevant readable project files, use its curated Godot map-authoring guide and required class-documentation reads to select an authoring strategy, publish a map-oriented plan identifying the map target/layer, target files, and visual acceptance intent, obtain normal write approval, apply approved generic code/configuration edits, and then request reload and visual verification. It MUST preserve the generic code-edit path's project boundary, prior-read, stale-file, confirmation, transcript, and Undo guarantees.

#### Scenario: Supported map generator change
- **WHEN** a user asks to change a map whose generator or configuration is represented by supported readable project files
- **THEN** the map agent reads the existing map facts, presents the target/layer, affected files, and map-oriented plan, applies only approved generic file edits, and continues to reload and visual verification

#### Scenario: Stale target file
- **WHEN** a target file changes after the map agent reads it and before an approved precise edit is applied
- **THEN** the edit is rejected through the existing stale-file outcome and the map workflow does not continue to reload or claim the map changed

#### Scenario: Hand-painted map has no authoring source
- **WHEN** the selected TileMap, TileMapLayer, or GridMap has no readable generator or layout configuration
- **THEN** the map agent MUST propose a bootstrap batch that creates a `@tool` builder and readable layout source, targets a generated-only layer, and preserves the existing hand-painted layer until an explicit later migration is approved

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

#### Scenario: Serialized map has no semantic authoring entry point
- **WHEN** inspection finds a hand-painted serialized TileMap/GridMap with no readable authoring source
- **THEN** the workflow uses the preloaded `@tool` bootstrap recipe and reads the selected Godot class documentation before proposing the builder and layout assets; it MUST NOT guess or hand-assemble raw cell serialization

### Requirement: Map workflow reports evidence without overstating completion
The map workflow SHALL report distinct outcomes for file edits, reload, screenshot capture, and visual expectation. It MUST NOT describe a map request as semantically or gameplay verified solely because a file edit succeeded, a target reloaded, or a screenshot was captured.

#### Scenario: Screenshot captured after reload
- **WHEN** an approved code-edit batch reloads successfully and produces a screenshot
- **THEN** the final transcript identifies the edited files, reload result, screenshot scope, and that the evidence is visual only

#### Scenario: Reload or screenshot unavailable
- **WHEN** the edit succeeds but reload fails or screenshot capture is unavailable
- **THEN** the workflow records the specific unavailable or failure result and does not claim visual verification or task completion

### Requirement: Map observation is bounded and progressive
The map workflow SHALL treat `describe_map_region` as bounded observation. It MUST cap returned cell detail, summarize empty or repetitive areas, and report truncation and the observed bounds when the requested region exceeds the result budget. The map agent MUST use focused follow-up queries rather than requesting an unbounded map export.

#### Scenario: Large TileMap region request
- **WHEN** a map-agent region request exceeds the observation cell budget
- **THEN** the result returns bounded detail and compact summary metadata with `truncated=true`, and the agent narrows its next query to the relevant target boundary or layer

### Requirement: Map tool failures remain recoverable evidence
The map workflow MUST receive a complete typed error outcome for a failed approved edit and continue the agent turn. It MUST use the typed failure to explain the outcome, inspect when necessary, or propose a safe alternative. For a hand-painted map without an authoring source, it MUST propose the curated `@tool` bootstrap path rather than terminate the request as unsupported.

#### Scenario: Builder bootstrap cannot be applied
- **WHEN** an approved `@tool` builder or layout creation step fails locally
- **THEN** the frontend returns a complete typed error result, the map agent continues the conversation, and it reports or proposes a safe next step without an HTTP validation failure
