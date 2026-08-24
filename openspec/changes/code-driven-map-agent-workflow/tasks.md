## 1. Establish the code-driven map contract

- [ ] 1.1 Define the first supported readable map-authoring file types and reject opaque/binary TileMap/GridMap serialized data as a write target with a typed outcome.
- [ ] 1.2 Retain and document `describe_tilemap_selection` and `describe_map_region` as general read-only fact tools; grant them to every applicable agent through its effective tool set while keeping map-write authority absent.
- [ ] 1.3 Add a map-workflow plan/evidence contract that records inspected map facts, target files, visual acceptance intent, edit outcome, reload outcome, and screenshot availability without claiming semantic completion.
- [ ] 1.4 Update the map-agent prompt and effective tools to use map inspection plus generic code inspection/editing while retaining its map-specific planning and acceptance responsibilities.
- [ ] 1.5 Update coordinator routing so supported map requests enter the code-driven map workflow rather than a generic programming workflow.

## 2. Implement safe code authoring for map requests

- [ ] 2.1 Reuse the existing read-before-edit, path-boundary, stale-file, confirmation, diff, and Undo path for map-authoring edits, with a map-specific editable-target allowlist.
- [ ] 2.2 Ensure approval and transcript payloads identify a code-driven map batch and its affected project-relative files without exposing full sensitive file content.
- [ ] 2.3 Return a typed unsupported-authoring-target result when a request requires direct TileMap/GridMap cell-data editing rather than an allowed generator or configuration target.

## 3. Add bounded editor reload and visual verification

- [ ] 3.1 Spike and select the Godot editor APIs required to scan/import/reload supported `.gd`, `.tscn`, and `.tres` targets while preserving dirty editor state; codify the supported reload modes.
- [ ] 3.2 Implement the project-scoped reload frontend operation with bounded approved targets, typed `reloaded`/`failed`/`blocked`/`unavailable` outcomes, and redacted diagnostics.
- [ ] 3.3 Fail closed when a reload would discard or overwrite dirty editor state; never save or discard it automatically.
- [ ] 3.4 Orchestrate reload followed by target-scoped screenshot capture only for eligible successful reloads, and record screenshot evidence as advisory visual evidence.
- [ ] 3.5 Report runtime-only generation as visually unavailable when editor reload cannot execute it.

## 4. Delete legacy map mutation surfaces

- [ ] 4.1 Delete `edit_map`, `fill_rect`, and `paint_from_image_grid` registrations, frontend executor dispatch, and their `MapTools` mutation implementations while retaining the read-only inspection functions.
- [ ] 4.2 Delete `edit_map`-specific budgets, map-agent mutation permissions, coordinator and map-agent mutation prompt instructions, mutation previews, mutation-specific UI/result formatting, and obsolete tests while retaining map fact rendering, generic approval, and transcript rendering.
- [ ] 4.3 Add regression tests proving the deleted names cannot register, route, or dispatch; document source-control revert as the only rollback mechanism.

## 5. Verify the workflow

- [ ] 5.1 Add backend tests for map routing, retained general read-only fact inspection across authorized agents, allowed source targets, rejection of serialized map data as a write target, approval gating, and evidence outcome honesty.
- [ ] 5.2 Add Godot tests for reload path validation, supported reload modes, dirty-editor blocking, reload diagnostics, and screenshot-unavailable outcomes.
- [ ] 5.3 Add end-to-end fixtures for a supported editor-visible map generator/configuration change and a runtime-only generator that remains visually unavailable.
- [ ] 5.4 Run targeted backend and Godot suites plus a manual editor smoke test covering code-edit confirmation, map observation, reload, screenshot, and failure behavior.
