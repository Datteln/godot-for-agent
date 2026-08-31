## Context

`capture_viewport_screenshot` already has separate 2D and 3D capture paths. The 2D resolver accepts `map_region` for TileMap-based content, whereas the 3D resolver accepts only `node_3d`. `MapTools.describe_map_region` can already inspect `GridMap` cells, so the missing capability is bounded visual evidence for an observed 3D grid region.

The change must preserve the existing capture result protocol, camera leasing/restoration guarantees, and the meaning of screenshots: they are visual evidence, not proof of semantic map placement or preservation.

## Goals / Non-Goals

**Goals:**

- Accept a finite `GridMap` cell cuboid as a 3D screenshot target.
- Frame that cuboid deterministically with the existing 3D camera capture flow and restore editor state afterwards.
- Give the agent a dimension-specific, schema-validated contract and actionable error codes.
- Let selection discovery expose the map dimension before an agent chooses a region tool.
- Preserve existing 2D `map_region` and 3D `node_3d` callers unchanged.

**Non-Goals:**

- Editing GridMap cells, building meshes, or proving that a screenshot semantically validates an edit.
- Supporting arbitrary spatial nodes as 3D map regions.
- Changing the existing region-description API beyond related discovery/guidance updates.

## Decisions

### Resolve a 3D map region only for GridMap

For `mode: "3d"` and `target.type: "map_region"`, the resolver SHALL require a `GridMap`. It converts the requested `x/y/z/width/height/depth` cell cuboid to a local AABB, transforms it through the GridMap global transform, and passes the resulting world AABB to the existing camera framing flow.

Using `GridMap` rather than generic `Node3D` gives the cell bounds a stable meaning. Treating generic nodes as map regions would make coordinate semantics ambiguous and duplicates the existing `node_3d` target type.

### Keep two-dimensional and three-dimensional target contracts explicit

2D `map_region` retains its TileMap/TileMapLayer requirements, including `map_layer` where applicable and `x/y/width/height`. The 3D contract requires `path` and all six cuboid fields, and rejects `map_layer` rather than silently ignoring a likely erroneous 2D argument.

The schema and agent guidance will describe these branches separately. This avoids a permissive hybrid input whose interpretation depends on incidental target type.

### Preserve the selection-tool name while broadening its result

`describe_tilemap_selection` remains the public entry point so existing agents and transcripts stay compatible. Its accepted selection set expands from TileMapLayer-focused discovery to TileMapLayer, legacy TileMap, and GridMap. The result carries a concrete node type, a `dimension` value, and dimension-matched next-step bounds guidance.

Renaming it to a generic map-selection tool was considered, but would create unnecessary call-site and model-prompt migration. A typed result improves discovery without that break.

### Frame the requested cell volume, not only visible mesh bounds

The camera shall frame the requested cuboid even when the GridMap has missing mesh-library entries or no occupied cells in part of it. The result can report occupancy/visibility warnings, but capture remains available because reviewing an empty intended region is useful. Screenshot output MUST NOT claim semantic verification.

### Reuse the existing camera lifecycle

The new resolver supplies a world AABB to `_capture_3d_target`; it does not add a second screenshot or camera path. Existing viewport selection, camera creation/lease, rendering wait, screenshot write, and state restoration remain authoritative.

### Validate the current builder script immediately before lifecycle operations

Builder script writes remain ordinary approved edits. After a write, the frontend SHALL scan and compile the newly written on-disk file and return its diagnostics. `reload_map_targets` and `rebuild_map_builder` SHALL also compile the current on-disk builder script immediately before loading or invoking it; on failure, they stop and return the same structured failure rather than proceeding.

This design intentionally does not persist a validation token, hash, or cross-call gate. The authoritative decision is a fresh Godot compilation of the current file at each lifecycle boundary. This avoids stale validation state when a script changes outside the agent flow while still ensuring that reload and rebuild never run after a failed compilation.

### Preserve compile diagnostics as operation results

Compilation failures SHALL include `ok: false`, a stable error code such as `builder_script_compile_failed`, the affected resource path, parsed diagnostics when Godot provides them, and a repair-oriented next action. A log-reading tool succeeding means only that log collection completed; it is not an assertion that no debugger items exist.

## Risks / Trade-offs

- [GridMap coordinate conversion differs across Godot versions] → Verify the available GridMap/ClassDB APIs in the supported editor version and isolate conversion in a focused helper with unit coverage.
- [A requested volume is visually empty] → Return an explicit warning/occupancy facts and retain the evidence-scope disclaimer.
- [Perspective framing may include more than the requested cuboid] → Return the requested cell bounds and computed world AABB so agents can state the capture scope precisely.
- [Schema callers send 2D fields to a 3D target] → Validate required z/depth and reject `map_layer` with typed errors before capture.
- [Godot emits localized or unparseable compiler text] → Return the raw bounded diagnostic alongside parsed path/line fields when available; never collapse it into a generic successful log-read result.

## Migration Plan

The API addition is backward compatible. Deploy with the plugin update, reload the editor plugin, and run the new resolver tests plus existing 2D/3D screenshot regressions. Rollback consists of reverting the plugin change; no scene or persisted map data is migrated.

## Open Questions

- Whether occupancy facts should enumerate only a count or include a bounded sample of occupied cells; default to count to keep tool results small.
