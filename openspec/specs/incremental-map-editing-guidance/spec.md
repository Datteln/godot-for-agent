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