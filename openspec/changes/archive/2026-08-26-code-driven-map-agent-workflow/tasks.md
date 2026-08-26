## 1. Establish the code-driven map contract

- [x] 1.1 Define the first supported readable map-authoring file types and route hand-painted/serialized TileMap/GridMap targets to a code-driven bootstrap plan rather than raw cell-data authoring.
- [x] 1.2 Retain and document `describe_tilemap_selection` and `describe_map_region` as general read-only fact tools; grant them to every applicable agent through its effective tool set while keeping map-write authority absent.
- [x] 1.3 Add a map-workflow plan/evidence contract that records inspected map facts, target files, visual acceptance intent, edit outcome, reload outcome, and screenshot availability without claiming semantic completion.
- [x] 1.4 Update the map-agent prompt and effective tools to use map inspection plus generic code inspection/editing while retaining its map-specific planning and acceptance responsibilities.
- [x] 1.5 Update coordinator routing so supported map requests enter the code-driven map workflow rather than a generic programming workflow.

## 2. Implement safe code authoring for map requests

- [x] 2.1 Reuse the existing read-before-edit, path-boundary, stale-file, confirmation, diff, and Undo path for map-authoring edits, with a map-specific editable-target allowlist; retain text-file pre-existence so aborting a newly created builder or layout removes it instead of leaving an empty placeholder.
- [x] 2.2 Ensure approval and transcript payloads identify a code-driven map batch and its affected project-relative files without exposing full sensitive file content.
- [x] 2.3 Complete the ordinary approval-gated `@tool` bootstrap so its scene edit configures the attached script, generated target path, readable layout path, and generated-only ownership rather than merely attaching the script.

## 3. Add bounded editor reload and visual verification

- [x] 3.1 Spike and select the Godot editor APIs required to scan/import/reload supported `.gd`, `.tscn`, and `.tres` targets while preserving dirty editor state; codify the supported reload modes.
- [x] 3.2 Update the project-scoped reload frontend operation to normalize script/resource-before-scene dependency order, preserve bounded approved targets and typed outcomes, distinguish a stale builder instance from missing on-disk source methods, compare canonical `res://` identities consistently, and reject `user://` reload targets with a typed project-resource-path error.
- [x] 3.3 Fail closed when a reload would discard or overwrite dirty editor state; never save or discard it automatically.
- [x] 3.4 Separate successful map-builder writes from post-write validation: scan/observe the new resource before parsing, return raw/normalized/existence path facts plus compiler diagnostics, and preserve `write_applied` while gating reload/rebuild/visual capture on the result.
- [x] 3.5 Report runtime-only generation as visually unavailable when editor reload cannot execute it.

## 4. Delete legacy map mutation surfaces

- [x] 4.1 Delete `edit_map`, `fill_rect`, and `paint_from_image_grid` registrations, frontend executor dispatch, and their `MapTools` mutation implementations while retaining the read-only inspection functions.
- [x] 4.2 Delete `edit_map`-specific budgets, map-agent mutation permissions, coordinator and map-agent mutation prompt instructions, mutation previews, mutation-specific UI/result formatting, and obsolete tests while retaining map fact rendering, generic approval, and transcript rendering.
- [x] 4.3 Add regression tests proving the deleted names cannot register, route, or dispatch; document source-control revert as the only rollback mechanism.

## 5. Verify the workflow

- [x] 5.1 Add backend tests for map routing, retained general read-only fact inspection across authorized agents, allowed source targets, hand-painted-map bootstrap planning, approval gating, and evidence outcome honesty.
- [x] 5.2 Add Godot tests for reload path validation, supported reload modes, dirty-editor blocking, reload diagnostics, screenshot-unavailable outcomes, canonical equivalence of relative and `res://` resource paths, and typed rejection of `user://` reload targets.
- [x] 5.3 Add end-to-end fixtures for a supported editor-visible map generator/configuration change and a runtime-only generator that remains visually unavailable.
- [ ] 5.4 Run targeted backend and Godot suites plus a manual editor smoke test covering code-edit confirmation, map observation, reload, screenshot, and failure behavior.

## 6. Harden the observed map workflow failure path

- [x] 6.1 Integrate the generic `tool-error-continuation` result-envelope guard so failed map edits return typed evidence to the map agent rather than an HTTP 422.
- [x] 6.2 Bound `describe_map_region` cell output, add compact empty/repeated-area summaries and truncation metadata, and update the map-agent prompt to query progressively.
- [x] 6.3 Replace the serialized-map unsupported-target branch with a `@tool` bootstrap plan that creates a readable layout source and preserves the original hand-painted layer.
- [x] 6.4 Add and preload a curated Godot map-authoring guide with a version-checked `@tool` builder template, selected-class documentation reads, generated-layer ownership, and migration guidance.
- [x] 6.5 Add regression coverage for hand-painted-map bootstrap planning, builder-creation failure continuation, and large-region observation bounds.
- [ ] 6.6 Repeat the manual editor smoke test from a selected hand-painted TileMap: approve the bootstrap batch, verify the generated-only layer reloads, then issue a second request that edits the readable layout source.
- [x] 6.7 Preserve complete typed post-write, reload, and rebuild results through the frontend DTO and service boundary (never `{}`), including map-scoped diagnostics, canonical path observations, failed-builder fingerprints, and `builder_repair_required` loop prevention.
- [x] 6.8 Add regression coverage for interrupted bootstrap rollback (no empty `.gd`/`.json` placeholders), bootstrap scene-property completeness, approved-layout builder rebuild, resource-reload-without-execution honesty, unapproved-scene reload continuation, canonical resource-reference handling, permitted `user://` generic output handling, and complete applied-result delivery.
- [ ] 6.9 Repeat the manual editor smoke test: create the bootstrap, interrupt a second bootstrap to confirm files are removed, approve a layout-only change, invoke the established builder, and verify a generated-only layer changes before visual capture.
- [x] 6.10 Add regression coverage for fresh post-write resource scanning, raw/normalized/existence path diagnostics, existing-script compile errors, zero-content builder/layout files, script-before-scene reload ordering, stale attached builder instances, and repeated failed rebuild requests that must require changed approved repair evidence.

## 7. Make Godot diagnostics source-accurate and execution-correlated

- [x] 7.1 Define and carry a bounded `GodotDiagnostic` contract containing source, severity, canonical affected resource path, source line/column when available, complete bounded message, raw diagnostic text, and operation/execution correlation identity; never substitute a log-file path/line for source location.
- [x] 7.2 Replace builder validation's global/stale-log fallback with a current-operation compiler diagnostic capture for the exact approved script; when editor APIs are insufficient, use a controlled non-mutating Godot validation invocation that captures and parses compiler output without running the builder or game.
- [x] 7.3 Correlate and normalize structured diagnostics for supported `.gd`/`.tscn`/`.tres` reload, `.gdshader` loading, test/headless execution, controlled GDScript execution, system-command execution, and export; preserve their bounded raw output and leave non-source file/permission/transport failures unlocated.
- [x] 7.4 Update map-agent repair rules and tool-result/protocol handling so a builder compile failure carries the exact current diagnostic and source identity, requires a targeted approved repair before retry, and cannot be repaired from stale or unrelated log evidence.
- [ ] 7.5 Add Godot and backend regression coverage for parser diagnostics with path/line/column/message, stale-log exclusion, operation/resource correlation, raw-output retention, shader/reload/headless/export normalization, and complete delivery to the next model turn.
- [ ] 7.6 Run targeted backend and Godot suites plus a manual editor smoke test that deliberately introduces a builder syntax error, confirms the displayed Godot diagnostic reaches map-agent, approves its targeted repair, and verifies rebuild succeeds only after compilation is clean.
