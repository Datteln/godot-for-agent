# Code-driven map workflow

Map and level changes use the `map-agent` workflow rather than map-cell mutation tools.

1. Inspect map facts with `describe_tilemap_selection` or `describe_map_region` and read the relevant generator/configuration source.
2. Publish a plan containing the inspected target/layer facts, intended project-relative files, and visual acceptance intent.
3. Apply normal confirmed text edits. The first supported map-authoring targets are `.gd`, `.tscn`, `.tres`, `.cfg`, `.json`, `.csv`, and `.txt`; files containing serialized TileMap/GridMap cell data are rejected with `unsupported_map_authoring_target`.
4. Reload only the changed, approved project-relative `.gd`, `.tscn`, or `.tres` files with `reload_map_targets`.
5. Record independent edit, reload, and screenshot outcomes. A screenshot is advisory visual evidence and never proves runtime, gameplay, collision, or semantic correctness.

Reload never saves, discards, or overwrites dirty target scenes. Runtime-only generation returns visual evidence as unavailable because this workflow does not execute the game.

There is no in-product fallback for legacy map mutation. Roll back this migration only by reverting its source-control change.
