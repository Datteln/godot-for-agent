# Incremental Map Editing Guidance Spec

## Purpose

Define when and how the map agent performs local incremental map edits rather than full regeneration, and the preservation, explanation, and post-edit review obligations that accompany such edits.

## Requirements

### Requirement: Map agent classifies editing intent before selecting an authoring strategy
The map agent SHALL interpret an existing-map request in terms of its intended scope before choosing a builder, layout, or local editing approach. The guidance SHALL present local incremental editing as the default interpretation for a bounded addition, removal, repair, or movement request, while retaining builders as an option for explicit generation, regeneration, migration, or an established dedicated generated target.

#### Scenario: User extends an existing floor
- **WHEN** a user asks to extend an observed floor by a bounded number of tiles
- **THEN** the agent SHALL frame the work as a local incremental edit and SHALL not infer that the existing layer must be reconstructed merely because it lacks a readable builder

#### Scenario: User explicitly requests procedural regeneration
- **WHEN** a user asks to regenerate a map area from parameters or identifies a dedicated generated target
- **THEN** the agent SHALL be permitted to select a layout and builder approach and SHALL explain why that scope is generative

### Requirement: Local map plans preserve observed authored context
For a local map request, the agent SHALL treat observed scene content as canonical and SHALL identify the nearby structures that must remain unchanged. The agent SHALL use a new layout or builder representation only as a proposed implementation artifact, not as proof that unobserved authored cells may be replaced.

#### Scenario: Extension meets an existing tower
- **WHEN** the requested floor extension reaches an observed tower or platform
- **THEN** the agent SHALL preserve that structure as a map fact and SHALL present a local connection, stopping point, or clarification rather than deleting the structure to make a straight extension fit

#### Scenario: Partial map observations are insufficient for reconstruction
- **WHEN** the agent has observed only bounded regions of an existing TileMap
- **THEN** the agent SHALL not describe a complete-layer rebuild from those observations as a preservation-safe local edit

### Requirement: Map agent explains its delta and preservation reasoning
Before proposing a mutating map edit, the agent SHALL state the target map and layer, the intended addition or modification, the local observations supporting it, the authored context it intends to preserve, and why the selected strategy matches the user's requested scope.

#### Scenario: Bounded floor extension plan
- **WHEN** the agent has inspected the end of a floor and its target cells
- **THEN** its plan SHALL identify the extension region and neighboring structures that will remain unchanged before it proposes the edit

### Requirement: Map agent performs a preservation-oriented post-edit review
After a local map edit, the agent SHALL review the changed region and immediately surrounding observed context, then report the requested delta separately from any unrelated difference it observes. The agent SHALL not represent a successful write, reload, or screenshot as proof of unrelated map preservation without corresponding observations.

#### Scenario: Review reveals unrelated terrain disappearance
- **WHEN** the post-edit review shows an unrelated platform, tower, or terrain segment is absent
- **THEN** the agent SHALL report the discrepancy as a preservation failure and SHALL not claim that the local request was completed correctly

#### Scenario: Review confirms only the intended local change
- **WHEN** the changed region and its observed surroundings match the stated delta and preservation intent
- **THEN** the agent SHALL report the edit as a local change with the evidence scope stated explicitly

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