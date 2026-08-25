## 1. Establish the code-driven map contract

- [x] 1.1 Define the first supported readable map-authoring file types and route hand-painted/serialized TileMap/GridMap targets to a code-driven bootstrap plan rather than raw cell-data authoring.
- [x] 1.2 Retain and document `describe_tilemap_selection` and `describe_map_region` as general read-only fact tools; grant them to every applicable agent through its effective tool set while keeping map-write authority absent.
- [x] 1.3 Add a map-workflow plan/evidence contract that records inspected map facts, target files, visual acceptance intent, edit outcome, reload outcome, and screenshot availability without claiming semantic completion.
- [x] 1.4 Update the map-agent prompt and effective tools to use map inspection plus generic code inspection/editing while retaining its map-specific planning and acceptance responsibilities.
- [x] 1.5 Update coordinator routing so supported map requests enter the code-driven map workflow rather than a generic programming workflow.

## 2. Implement safe code authoring for map requests

- [x] 2.1 Reuse the existing read-before-edit, path-boundary, stale-file, confirmation, diff, and Undo path for map-authoring edits, with a map-specific editable-target allowlist.
- [x] 2.2 Ensure approval and transcript payloads identify a code-driven map batch and its affected project-relative files without exposing full sensitive file content.
- [x] 2.3 Emit the first ordinary approval-gated `@tool` bootstrap edit when a request needs a semantic authoring entry point for an existing hand-painted TileMap/GridMap, without a prose-only confirmation gate.

## 3. Add bounded editor reload and visual verification

- [x] 3.1 Spike and select the Godot editor APIs required to scan/import/reload supported `.gd`, `.tscn`, and `.tres` targets while preserving dirty editor state; codify the supported reload modes.
- [x] 3.2 Implement the project-scoped reload frontend operation with bounded approved targets, typed `reloaded`/`failed`/`blocked`/`unavailable` outcomes, and redacted diagnostics.
- [x] 3.3 Fail closed when a reload would discard or overwrite dirty editor state; never save or discard it automatically.
- [x] 3.4 Orchestrate reload followed by target-scoped screenshot capture only for eligible successful reloads, and record screenshot evidence as advisory visual evidence.
- [x] 3.5 Report runtime-only generation as visually unavailable when editor reload cannot execute it.

## 4. Delete legacy map mutation surfaces

- [x] 4.1 Delete `edit_map`, `fill_rect`, and `paint_from_image_grid` registrations, frontend executor dispatch, and their `MapTools` mutation implementations while retaining the read-only inspection functions.
- [x] 4.2 Delete `edit_map`-specific budgets, map-agent mutation permissions, coordinator and map-agent mutation prompt instructions, mutation previews, mutation-specific UI/result formatting, and obsolete tests while retaining map fact rendering, generic approval, and transcript rendering.
- [x] 4.3 Add regression tests proving the deleted names cannot register, route, or dispatch; document source-control revert as the only rollback mechanism.

## 5. Verify the workflow

- [x] 5.1 Add backend tests for map routing, retained general read-only fact inspection across authorized agents, allowed source targets, hand-painted-map bootstrap planning, approval gating, and evidence outcome honesty.
- [x] 5.2 Add Godot tests for reload path validation, supported reload modes, dirty-editor blocking, reload diagnostics, and screenshot-unavailable outcomes.
- [x] 5.3 Add end-to-end fixtures for a supported editor-visible map generator/configuration change and a runtime-only generator that remains visually unavailable.
- [ ] 5.4 Run targeted backend and Godot suites plus a manual editor smoke test covering code-edit confirmation, map observation, reload, screenshot, and failure behavior.

## 6. Harden the observed map workflow failure path

- [x] 6.1 Integrate the generic `tool-error-continuation` result-envelope guard so failed map edits return typed evidence to the map agent rather than an HTTP 422.
- [x] 6.2 Bound `describe_map_region` cell output, add compact empty/repeated-area summaries and truncation metadata, and update the map-agent prompt to query progressively.
- [x] 6.3 Replace the serialized-map unsupported-target branch with a `@tool` bootstrap plan that creates a readable layout source and preserves the original hand-painted layer.
- [x] 6.4 Add and preload a curated Godot map-authoring guide with a version-checked `@tool` builder template, selected-class documentation reads, generated-layer ownership, and migration guidance.
- [x] 6.5 Add regression coverage for hand-painted-map bootstrap planning, builder-creation failure continuation, and large-region observation bounds.
- [ ] 6.6 Repeat the manual editor smoke test from a selected hand-painted TileMap: approve the bootstrap batch, verify the generated-only layer reloads, then issue a second request that edits the readable layout source.
