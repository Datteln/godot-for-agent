## Context

The current map agent has a dedicated prompt and routes to `edit_map`, `fill_rect`, `paint_from_image_grid`, and map inspection tools. The mutation tools change editor-owned TileMap/GridMap state; the inspection tools return the target, layer, coordinate system, and actual tile facts needed to avoid guessing. Ordinary programming work instead uses readable source-file edits protected by a prior-read requirement, path boundaries, stale-file detection, user confirmation, and Godot Undo.

The product direction is to retain the map agent's domain workflow while making map changes through code and readable project configuration. Godot is retained only as the observation boundary needed to reload changed editor-visible assets and provide a screenshot. This change is cross-cutting because it changes agent routing, tool registration/execution, approval presentation, and verification outcomes.

## Goals / Non-Goals

**Goals:**

- Preserve a dedicated map-agent workflow that understands map intent, target scenes, generator/configuration code, and visual acceptance criteria.
- Retain read-only map inspection as a general observation capability so every authorized agent can identify a TileMap/GridMap target, layer, existing cells, and tile identities; the map agent uses those facts before it plans source changes.
- Make approved edits to readable project files the only normal map-authoring mutation path.
- Provide a narrow, explicit reload operation and screenshot-based visual verification after a code-edit batch.
- Preserve existing generic edit protections: project path boundary, read-before-precise-edit, stale-file conflict rejection, confirmation, transcript visibility, and Undo.
- Make verification outcomes honest: reload and screenshot evidence are distinct from semantic or gameplay correctness.

**Non-Goals:**

- Editing TileMap/GridMap cells through `edit_map`, `fill_rect`, `paint_from_image_grid`, or direct manipulation of serialized `tile_map_data`.
- Recreating CodeAct, multi-agent delegation, map revision tracking, geometry/pathfinding validators, transactions, or automatic recovery.
- Automatically running the game, asserting gameplay behavior, or treating a screenshot as proof of it.
- Supporting arbitrary binary asset mutation through the code-authoring path.

## Decisions

### 1. Retain map intent, replace map mutation

The map agent remains the route for map and level requests. It will use a constrained lifecycle:

```text
inspect authoritative map facts and readable map-related source/config
  -> publish a map-oriented change plan and acceptance view
  -> approved generic file edits
  -> reload explicitly affected targets
  -> capture screenshot
  -> report evidence and limitations
```

The agent differs from the programming agent in its prompt, planning template, map-oriented interpretation of read-only facts, expected targets, and acceptance language; it does not differ by owning a TileMap mutation protocol. `describe_tilemap_selection` and `describe_map_region` remain general read-only fact providers, available to every agent that has them in its effective tool set, because source files and screenshots alone cannot reliably identify an existing layer, cell coordinates, or TileSet atlas identity. This keeps user-visible planning suitable for map work without preserving the special-purpose write layer.

Alternative considered: route every map request to the programming agent. Rejected because it loses map-specific target discovery, generator/configuration conventions, and visual acceptance framing.

### 2. Use the existing generic code-edit authority

Map authoring will reuse the existing generic read/edit/write proposals and their approval flow instead of granting an LLM unmediated filesystem access. In this product an LLM needs a concrete capability to alter a workspace; the generic code-edit authority is that capability. “No map tools” therefore means no map-specific mutation tools, not no controlled file-write capability at all.

Initial editable targets are readable, project-relative text files such as `.gd`, `.tscn`, `.tres`, and explicitly configured text map data. The map agent MUST not edit opaque/binary resources or serialized TileMap cell blobs. It can generate or modify GDScript and text configuration that populate a map when Godot loads or runs it.

Before planning a change that depends on an existing map, the map agent reads map facts through the retained inspection tools and binds its source-level plan to the returned target and layer. The generic code-edit path remains responsible for writing files; the map read path never grants write authority.

Alternative considered: backend worker direct filesystem writes. Deferred because it would duplicate the current confirmation, stale-file, editor-memory conflict, and Undo guarantees. It can be reconsidered only as a separately specified execution-boundary change.

### 3. Add a narrow editor reload operation

Add a front-side reload operation that accepts a bounded, project-relative set of changed targets and an explicit reload intent. It scans/imports changed files as necessary, reloads the currently relevant scene or resource without silently saving unrelated dirty editor state, waits for an observable completion/failure result, and returns typed diagnostics.

It must not accept arbitrary filesystem paths, run arbitrary scripts, or replace the generic file-edit confirmation. A reload does not by itself prove that a normal runtime-only generator ran; that condition is reported as unavailable unless the selected reload mode can observe it. Editor-visible generation requires an editor-executable implementation such as an intentionally designed `@tool` path; normal runtime code requires a later run-mode capability that is outside this change.

Alternative considered: reuse `open_scene`. Rejected because opening a scene is not an explicit reload contract and its current behavior can discard unsaved in-editor state.

### 4. Screenshot is visual evidence only

After a successful eligible reload, the map workflow requests a screenshot of the intended editor viewport. The final task state distinguishes: reload failed, screenshot unavailable, screenshot captured, and visual expectation not established. A screenshot never establishes collision, reachability, runtime initialization, or gameplay success.

### 5. Delete map-specific mutation surfaces as one vertical slice

The map mutation tool definitions, registrations, executor dispatch, `MapTools` mutation implementations, map-agent mutation permissions, `edit_map`-specific budgets, preview rendering branches, prompt instructions, and tests are deleted together. There is no feature flag, disabled compatibility mode, or legacy map-mutation route. Read-only map inspection remains registered as a general observation tool and is granted through each agent's effective read-only tool set. Generic transcript renderers continue to show map fact reads, code edit approvals, reload results, screenshots, and typed errors, so the chat timeline remains the sole visible workflow surface.

## Risks / Trade-offs

- [Existing hand-painted maps are difficult to alter through source code] → Use retained map inspection to identify the real target/layer/tile facts, then require the first supported use cases to name a generator or readable configuration target; report an unsupported write target rather than editing serialized cell data.
- [A reload can overwrite or conflict with unsaved editor memory] → Reload checks open/dirty state and fails closed with a clear user action; it never auto-saves or silently discards edits.
- [A screenshot may show an unrelated viewport or omit runtime-only output] → Bind screenshot requests to the selected target/mode where possible and report its scope; do not claim semantic success.
- [Deleting map mutation tools breaks existing prompts or callers] → Delete registrations, implementations, metadata, routing, previews, and prompts atomically; preserve read-only inspections and add regression tests that the deleted tool names cannot be resolved. Operational rollback is a source-control revert of the entire change, not an in-product compatibility path.
- [Generic file edits are broader than map edits] → Keep existing path, stale-file, confirmation, and diff guardrails; use an explicit map-authoring editable-file allowlist.

## Migration Plan

1. Implement and test the source-file map workflow, reload contract, and retained read-only inspection path.
2. Delete the legacy mutation implementations and every route, budget, prompt, preview, and test that refers to them in the same implementation change.
3. Run migration verification against supported fixtures and ensure the deleted tool names cannot be registered or dispatched.
4. Roll back only by reverting the complete source-control change; source-file edits made by the new workflow remain ordinary project changes and are recoverable through existing Undo/version control.

## Open Questions

- Which project file types, beyond `.gd`, `.tscn`, and `.tres`, are in the first map-authoring allowlist?
- Which Godot editor reload APIs and modes correctly refresh supported `@tool` generator scenes without losing dirty editor state?
- Does the first supported map use case require only editor-visible `@tool` generation, or will a later change add an explicit run-scene verification mode?
