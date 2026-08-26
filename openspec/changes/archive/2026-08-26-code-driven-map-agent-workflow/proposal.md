## Why

The current map agent edits TileMap and GridMap content through several Godot-specific mutation tools. That makes map work a separate imperative tool protocol, while the desired authoring model is for the LLM to change the project's readable source, scene, and configuration files like a programmer, then let Godot reload and visually verify the result.

Map work remains distinct from ordinary code work: it needs map-oriented planning, explicit target and acceptance criteria, and an editor feedback loop. The change therefore deletes map-specific mutation while retaining the dedicated map-agent workflow.

## What Changes

- Introduce a code-driven map-authoring workflow. The map agent will plan map changes in map terms, edit approved readable project files through the common code-edit path, and report file-level evidence rather than issuing `edit_map`/`fill_rect`/`paint_from_image_grid` mutations. When a selected hand-painted map lacks a generator, the workflow will first propose a `@tool` builder plus readable layout source instead of ending the request as unsupported.
- Introduce a minimal Godot observation bridge that reloads explicitly named changed resources or scenes and captures a viewport screenshot for visual verification.
- **BREAKING**: Delete `edit_map`, `fill_rect`, and `paint_from_image_grid` from tool registration, executor dispatch, implementations, prompts, previews, and tests. No compatibility mode or legacy routing remains. Retain map-specific read tools as general read-only observation capabilities, available to every agent whose effective tools permit them, and use them as the authoritative source of existing map target, layer, coordinate, and tile facts.
- Preserve the existing confirmation, transcript, permission, path-boundary, stale-file, diff, and Undo behavior of the common code-edit path.
- Bound map-region observation output and ensure a typed tool failure is returned to the map agent as recoverable evidence, rather than ending the pending request at the transport boundary.
- Add a bounded, approval-linked editor builder-rebuild step. Godot's editor engine invokes one fixed rebuild method on an already attached `@tool` builder node; this is neither a successful file write nor a resource reload, does not start the game, and is not arbitrary script execution. The workflow must return explicit rebuild evidence or a typed failure before it can claim a map changed.
- Preserve file existence through interruption rollback: aborting a batch that created a text layout or builder must remove it rather than leaving an empty `.json` or `.gd` placeholder.
- Make Godot compiler diagnostics for an approved map-builder script a required pre-execution gate. The frontend must scan the newly written resource before classifying it, preserve the successful write fact separately from any subsequent compilation failure, return the normalized/raw path facts and compiler diagnostics to the map agent, reload scripts before their scenes, and stop repeating reload/rebuild attempts for an unchanged failed builder until an approved source or scene repair changes that state. A successful reload result must remain complete end-to-end rather than becoming `{}`.
- Require a bootstrap scene edit to configure the attached builder's generated target, readable layout path, and generated-only ownership as one approved scene contract; merely attaching a script is not an authoring entry point.
- Establish one safe Godot-path contract for the complete frontend. A model-supplied project-relative path and a Godot-returned `res://` URI normalize to the same project resource identity; a valid `user://` URI is also accepted and normalized within its separate per-project user-data namespace. Operating-system absolute paths and traversal remain rejected. Project-resource-only operations such as attached scripts, scenes, reload targets, and map authoring sources remain restricted to `res://`; generic file and output operations may explicitly opt into `user://`.
- Define clear outcomes for reload failure, unavailable visual verification, and successful reload with only advisory screenshot evidence. A screenshot alone must not claim gameplay or semantic correctness.
- Establish a common Godot diagnostic contract for script validation, resource/scene reload, shader loading, headless/test execution, system-command execution, and export. Diagnostics must be correlated with the current execution and affected resource rather than inferred from stale global logs; when an engine/compiler reports them, the model receives the affected resource path, line, column, complete bounded message, and raw diagnostic text.

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
- A curated map-agent authoring guide supplies an editor-safe `@tool` builder recipe, layout format expectations, and required Godot class-documentation checks; the workflow does not rely on latent model knowledge of Godot APIs.
- Map observation serialization and the generic front-tool error-continuation boundary require additional regression coverage.
- Map-specific mutation implementations, registrations, UI branches, and related configuration are deleted. Existing read-only map inspection remains registered as a general observation capability, with the map agent as its primary map-authoring consumer.
