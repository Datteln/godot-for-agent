## 1. Map selection discovery

- [x] 1.1 Extend `MapTools.describe_selection` to classify selected TileMapLayer, legacy TileMap, and GridMap nodes with their concrete type and dimension.
- [x] 1.2 Return dimension-matched next-step bounds guidance and typed unsupported-selection results while retaining the `describe_tilemap_selection` tool name.
- [x] 1.3 Add focused tests for 2D TileMapLayer, legacy TileMap, GridMap, and non-map selections.

## 2. GridMap region resolution and capture

- [x] 2.1 Verify the supported Godot GridMap coordinate APIs and implement an isolated conversion from finite 3D cell bounds to local and world AABBs.
- [x] 2.2 Extend the 3D screenshot target resolver to accept `map_region` only for GridMap, then feed its world AABB into the existing camera capture lifecycle.
- [x] 2.3 Validate GridMap target fields and return typed failures for non-GridMap targets, missing or invalid x/y/z/width/height/depth, and inapplicable `map_layer`.
- [x] 2.4 Return capture-scope metadata, requested cell bounds, computed world bounds, and explicit empty/visibility warnings without treating screenshots as semantic verification.

## 3. Tool contract and agent guidance

- [x] 3.1 Update `front_tools.py` and `tool_markdown.py` to document separate 2D TileMap and 3D GridMap `map_region` contracts.
- [x] 3.2 Update `map-agent.md` to discover map dimension first and use the matching selection, region-description, and screenshot inputs.
- [x] 3.3 Preserve the existing public 2D map-region and 3D node_3d contracts in schemas and tool result protocol.

## 4. Verification

- [x] 4.1 Add resolver tests for valid GridMap cuboids, transformed world bounds, invalid targets, missing z/depth, invalid dimensions, and rejected `map_layer`.
- [x] 4.2 Add an integration regression that verifies 3D GridMap capture restores the prior camera state and reports bounded evidence metadata.
- [x] 4.3 Run the relevant Godot headless tests and existing screenshot/map-tool regressions, and record the results.

## 5. Builder compile diagnostics before lifecycle operations

- [x] 5.1 Normalize builder compilation failures from write validation, reload, and rebuild into a common result with error code, path, parsed diagnostics, bounded raw text, and `next_action=fix_builder_script`.
- [x] 5.2 Before loading a builder during reload or invoking one during rebuild, freshly compile its current on-disk script and stop the operation on failure without adding persisted validation state.
- [x] 5.3 Make debugger-log collection distinguish collection success from the presence of returned errors, including localized Godot error prefixes where possible.
- [x] 5.4 Add regressions covering: failed write validation returns diagnostics; failed reload/rebuild does not proceed; a script modified after prior success is freshly revalidated; and a log-read result with items is not rendered as “no errors”.
