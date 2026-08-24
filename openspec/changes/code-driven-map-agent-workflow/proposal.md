## Why

The current map agent edits TileMap and GridMap content through several Godot-specific mutation tools. That makes map work a separate imperative tool protocol, while the desired authoring model is for the LLM to change the project's readable source, scene, and configuration files like a programmer, then let Godot reload and visually verify the result.

Map work remains distinct from ordinary code work: it needs map-oriented planning, explicit target and acceptance criteria, and an editor feedback loop. The change therefore deletes map-specific mutation while retaining the dedicated map-agent workflow.

## What Changes

- Introduce a code-driven map-authoring workflow. The map agent will plan map changes in map terms, edit approved readable project files through the common code-edit path, and report file-level evidence rather than issuing `edit_map`/`fill_rect`/`paint_from_image_grid` mutations.
- Introduce a minimal Godot observation bridge that reloads explicitly named changed resources or scenes and captures a viewport screenshot for visual verification.
- **BREAKING**: Delete `edit_map`, `fill_rect`, and `paint_from_image_grid` from tool registration, executor dispatch, implementations, prompts, previews, and tests. No compatibility mode or legacy routing remains. Retain map-specific read tools as general read-only observation capabilities, available to every agent whose effective tools permit them, and use them as the authoritative source of existing map target, layer, coordinate, and tile facts.
- Preserve the existing confirmation, transcript, permission, path-boundary, stale-file, diff, and Undo behavior of the common code-edit path.
- Define clear outcomes for reload failure, unavailable visual verification, and successful reload with only advisory screenshot evidence. A screenshot alone must not claim gameplay or semantic correctness.

## Capabilities

### New Capabilities

- `code-driven-map-authoring`: Dedicated map-agent planning and approved source-file editing for map changes, without map-specific mutation tools.
- `editor-reload-and-visual-verification`: Reload explicitly affected Godot resources/scenes and capture bounded visual evidence for a completed code-edit batch.

### Modified Capabilities

- None.

## Impact

- Map agent definition, coordinator routing, front-tool registry, tool executor, map-mutation UI previews, and map-tool tests.
- A new narrow frontend reload operation, using Godot editor APIs and scoped to explicitly approved project-relative paths.
- Existing generic code-edit tools (`read_file`, `apply_text_edit`, file proposals/writes), confirmation UI, transcript entries, and file-state cache become the map authoring path.
- Map-specific mutation implementations, registrations, UI branches, and related configuration are deleted. Existing read-only map inspection remains registered as a general observation capability, with the map agent as its primary map-authoring consumer.
