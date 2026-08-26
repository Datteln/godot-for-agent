# Code-driven map workflow

Map and level changes use the `map-agent` workflow rather than map-cell mutation tools.

1. Inspect map facts with `describe_tilemap_selection` or `describe_map_region` and read the relevant generator/configuration source.
2. Publish a plan containing the inspected target/layer facts, intended project-relative files, and visual acceptance intent.
3. Apply normal confirmed text edits. The first supported map-authoring targets are `.gd`, `.tscn`, `.tres`, `.cfg`, `.json`, `.csv`, and `.txt`; the workflow does not hand-assemble serialized TileMap/GridMap cell data.
4. Reload only changed, approved project-relative `.gd`, `.tscn`, or `.tres` files with `reload_map_targets`. A reloaded `.gd` resource is not evidence that an existing builder instance ran.
5. After an approved layout edit for an established builder, call `rebuild_map_builder` with the already attached builder's scene-relative node path and the approved paths. Godot's editor invokes only its fixed `rebuild_from_layout()` method; it does not start the game, execute arbitrary scripts, or invoke lifecycle callbacks. Only `rebuilt` is evidence that the generated-only target was rebuilt.
6. Record independent edit, reload/rebuild, and screenshot outcomes. A screenshot is advisory visual evidence and never proves runtime, gameplay, collision, or semantic correctness.

Reload never saves, discards, or overwrites dirty target scenes. `blocked`, `failed`, and `unavailable` reload/rebuild outcomes are returned to the agent as typed errors, so it can explain the blocker rather than blindly replacing the builder script. Runtime-only generation returns visual evidence as unavailable because this workflow does not execute the game.

There is no in-product fallback for legacy map mutation. Roll back this migration only by reverting its source-control change.
