## Context

The current map agent has a dedicated prompt and routes to `edit_map`, `fill_rect`, `paint_from_image_grid`, and map inspection tools. The mutation tools change editor-owned TileMap/GridMap state; the inspection tools return the target, layer, coordinate system, and actual tile facts needed to avoid guessing. Ordinary programming work instead uses readable source-file edits protected by a prior-read requirement, path boundaries, stale-file detection, user confirmation, and Godot Undo.

The product direction is to retain the map agent's domain workflow while making map changes through code and readable project configuration. Godot is retained only as the observation boundary needed to reload changed editor-visible assets and provide a screenshot. This change is cross-cutting because it changes agent routing, tool registration/execution, approval presentation, and verification outcomes.

## Goals / Non-Goals

**Goals:**

- Preserve a dedicated map-agent workflow that understands map intent, target scenes, generator/configuration code, and visual acceptance criteria.
- Retain read-only map inspection as a general observation capability so every authorized agent can identify a TileMap/GridMap target, layer, existing cells, and tile identities; the map agent uses those facts before it plans source changes.
- Make approved edits to readable project files the only normal map-authoring mutation path.
- Give the map agent a curated, version-checked `@tool` builder recipe and a bootstrap workflow for hand-painted maps that lack a semantic authoring source.
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

Initial editable targets are readable, project-relative text files such as `.gd`, `.tscn`, `.tres`, and explicitly configured text map data. The map agent does not construct or guess raw serialized TileMap cell blobs. For a hand-painted target with no semantic source, it first proposes a migration that creates an editor-visible `@tool` builder and readable layout data; it can then generate or modify GDScript and text configuration that populate a map when Godot loads or runs it.

Before planning a change that depends on an existing map, the map agent reads map facts through the retained inspection tools and binds its source-level plan to the returned target and layer. The generic code-edit path remains responsible for writing files; the map read path never grants write authority.

Alternative considered: backend worker direct filesystem writes. Deferred because it would duplicate the current confirmation, stale-file, editor-memory conflict, and Undo guarantees. It can be reconsidered only as a separately specified execution-boundary change.

### 2.1 Give map-agent an explicit `@tool` bootstrap recipe

The map agent will preload a maintained Godot map-authoring guide. It must supply a concrete `@tool` builder skeleton, the role of exported target-layer and layout-path properties, deferred rebuild triggering, `Engine.is_editor_hint()` guarding, a generated-only output layer, and a readable layout representation. The guide will require `read_class_docs` for the selected target type before the agent fills in TileMapLayer, legacy TileMap, or GridMap API calls; the template is a safe starting shape, not an assumed API contract.

For a selected hand-painted map, the first plan is a bootstrap batch: create the builder, create a readable layout/configuration asset, and create or identify a generated-only layer without automatically clearing the original hand-painted layer. Once the facts and required class documentation are available, the agent starts that batch by emitting the first ordinary proposal/edit tool call; the existing inline approval card is the sole user confirmation. It must not finish with a prose-only “continue?” gate. After approved creation and editor-visible reload, the agent reports the new authoring entry point. A later approved migration may copy or replace the old layer only after the user has reviewed the explicit conversion plan.

Every newly written layout and builder is read back before it can be used. The layout must be non-empty and parse as the selected readable format; the builder must be non-empty and contain the documented rebuild entry point. A failed post-write verification is typed failure evidence, not a reason to keep rewriting alternate lifecycle callbacks.

### 3. Add a narrow editor reload operation

Add a front-side reload operation that accepts a bounded, project-relative set of changed targets and an explicit reload intent. It scans/imports changed files as necessary, reloads the currently relevant scene or resource without silently saving unrelated dirty editor state, waits for an observable completion/failure result, and returns typed diagnostics.

It must not accept arbitrary filesystem paths, run arbitrary scripts, or replace the generic file-edit confirmation. A reload does not by itself prove that a normal runtime-only generator ran; that condition is reported as unavailable unless the selected reload mode can observe it. Editor-visible generation requires an editor-executable implementation such as an intentionally designed `@tool` path; normal runtime code requires a later run-mode capability that is outside this change.

Alternative considered: reuse `open_scene`. Rejected because opening a scene is not an explicit reload contract and its current behavior can discard unsaved in-editor state.

### 3.1 Treat map-builder execution as a distinct, bounded editor action

Reloading `scripts/map_builder.gd` only reloads a resource; it does not establish that an already-instanced `@tool` node rebuilt the map. Reloading `scenes/game.tscn` is also not valid when that scene was not part of the approved batch, and must remain blocked by the generic reload target guard.

The workflow therefore adds a narrow map-builder rebuild operation. It operates only on an open selected map scene and an existing builder node that implements the curated, documented rebuild interface. It verifies the builder node, map target, generated-only output layer, builder script, and readable layout from editor/scene facts. The builder script and layout must be in the current approved map-authoring batch or be the previously established active entry point for the selected map. The operation accepts neither an arbitrary method name nor an arbitrary script path, invokes only the fixed rebuild interface, and returns typed `rebuilt`/`blocked`/`failed`/`unavailable` evidence with bounded affected-target counts.

The initial curated builder contract uses a public, idempotent `rebuild_from_layout()` entry point and explicit exported target/layout properties. After the bounded identity checks, the frontend asks the Godot editor engine to invoke that method on the already attached scene-node instance; it does not launch the game, invoke a normal runtime lifecycle callback, or execute an arbitrary script or method. It clears and regenerates only its generated-only target, so a second rebuild is observable and cannot accumulate duplicate cells. Exact Godot API calls remain class-documentation driven. A bootstrap that adds the builder node must include the scene edit in its own approved batch; later layout-only edits may use the already established builder node without reloading an unrelated scene. Screenshot capture occurs only after `rebuilt`; a generic resource reload is never reported as builder execution.

### 4. Screenshot is visual evidence only

After a successful eligible reload, the map workflow requests a screenshot of the intended editor viewport. The final task state distinguishes: reload failed, screenshot unavailable, screenshot captured, and visual expectation not established. A screenshot never establishes collision, reachability, runtime initialization, or gameplay success.

### 3.2 Gate rebuild on current builder diagnostics and require a repairable next action

An approved `.gd` builder write is not eligible for scene reload or `rebuild_from_layout()` until the frontend has asked the Godot editor for compiler/script diagnostics for that exact project-relative script. The result is map-scoped, read-only evidence: error code, path, line, column when available, and a bounded/redacted message. The map agent receives this evidence automatically as the post-write result and can also inspect the current builder diagnostics through a read-only map-agent diagnostic capability.

If the builder has compiler errors, is empty, or its readable layout is empty/malformed, the frontend returns a typed `builder_script_compile_failed` or `authoring_entry_point_missing` failure. It does not reload the scene, invoke the builder, or request a success-oriented screenshot. The prompt directs the map agent to read the reported source and diagnostics and issue one ordinary, approval-gated repair proposal; it must not silently alter the map or invent alternate lifecycle callbacks.

Reload target order is normalized by type, irrespective of the order supplied by the model: changed scripts/resources are reloaded and observed first, then dependent scenes. Builder validation reads the on-disk script source before asking the attached instance whether it has the fixed method. A source that contains the fixed method while the attached node does not yields `builder_instance_stale`, not the misleading `builder_method_missing`; the normalized scene reload then creates the current instance before another bounded rebuild is attempted.

Every non-successful builder validation/rebuild result carries a failed-builder fingerprint derived from the approved builder source, layout, and relevant scene identity. Until an approved file/scene edit changes that fingerprint, a further reload/rebuild request returns `builder_repair_required` and does not invoke Godot again. This is a loop guard, not an automatic repair policy: it preserves the model's choice of repair while requiring new source evidence before execution can be retried.

### 3.3 Treat post-write script validation as a separate observable phase

The generic text write remains a successful, Undo-recorded file operation once the filesystem write completed. For a code-driven map builder, the frontend then performs an explicit post-write resource phase: update the resource filesystem, wait for scanning/import observation, and only then request Godot's script parse/compile result. The post-write validation cannot erase the fact that the write occurred or cause the generic batch to be represented as a missing file.

Every post-write result includes `write_applied`, raw script resource path, normalized project-relative path, globalized path only when safe to disclose, and the boolean file-existence observation used for classification. A parse failure returns `builder_script_compile_failed` plus actual compiler diagnostics where Godot exposes them; a missing path remains `builder_script_missing` only when those path facts demonstrate absence. This makes an empty path normalization, delayed filesystem scan, and a genuinely deleted file distinguishable to both logs and the model.

The complete typed dictionary from reload, rebuild, and post-write validation must survive the frontend DTO, HTTP schema, service append, and model message unchanged except for existing bounded redaction. An `applied` reload may never be collapsed to `{}`: it includes status, ordered/reloaded/unavailable targets, reload mode, visual-evidence state, and diagnostics. This is required evidence for the model to decide whether to repair source, reload a dependent scene, or rebuild.

Finally, a bootstrap scene edit is atomic in meaning even when ordinary approvals occur one at a time: the scene node must attach the builder script and configure its generated target path, readable layout path, and generated-only ownership. The rebuild operation rejects a partially configured node with its missing-property target, and the map agent treats that as a scene configuration repair rather than a script retry.

### 3.4 Canonicalize Godot resource URIs at the project boundary

Godot reports attached script paths, scene paths, exported file properties, and resource references as `res://` URIs. Generic authoring tools commonly receive project-relative paths from model tool calls, while user-data outputs and approved user-data artifacts use `user://`. The frontend has one path-boundary utility, and it must make a relative path and its equivalent `res://` URI identical before any file existence, approval-batch, dirty-scene, read-state, resource-reference, or diagnostic comparison occurs. It must accept a valid `user://` URI as a separate namespace, never convert it to `res://`, and never first classify either Godot URI as an operating-system absolute path or simplify it into a malformed scheme.

The canonical output preserves its namespace: a clean `res://` project path or a clean `user://` per-project user-data path. Operating-system absolute paths, empty paths, and any traversal path remain invalid. Consumers explicitly declare their permitted namespace: attached scripts, scenes, resource reloads, approval batches, and versioned map builder/layout sources require `res://`; generic text/output tools may allow `user://`. Every relevant call site continues using this one boundary rather than creating local exceptions for builders. Tests cover both project representations, valid user-data paths, reject unsafe variants, and exercise Godot-originated paths through builder script/layout, open/dirty scene sets, resource reference resolution, text-tool read state, reload approval matching, and preview lookup.

This fixes a blocking observed failure: a valid `script.resource_path` of `res://scripts/map_builder.gd` was normalized to an empty string and incorrectly reported as `builder_script_missing`, before scanning or compilation. After the canonicalization fix, a true zero-content file, a compile error, and a missing file must remain distinct typed results. A bootstrap node that contains only a script attachment then correctly reaches the separate scene-property configuration blocker rather than being concealed by the path error.

### 3.5 Correlate Godot diagnostics with the current operation

The frontend needs one bounded `GodotDiagnostic` contract with `source`, `severity`, `resource_path`, `line`, `column`, `message`, `raw_text`, and an operation/execution correlation identity. `resource_path` is the affected `res://` resource when it can be determined, while `line` and `column` are source locations rather than offsets within an editor log. Values that Godot does not expose remain explicitly unavailable; they must not be fabricated from a log file path or its line number.

Builder validation must obtain diagnostics from the compilation action itself, or from a fresh, operation-correlated editor/compiler record for that exact script. It must not return a stale `user://logs` entry such as an unrelated `--script` base-class error as the builder's parse error. If direct editor APIs do not expose the parser text, the implementation must use a bounded controlled Godot validation invocation that captures compiler stderr for the target resource, then parses the affected resource, line, column, complete diagnostic message, and raw text. The validator remains non-mutating: it validates the already approved on-disk source and must not run the builder lifecycle or game.

The same normalization applies to supported Godot-producing operations: script/resource/scene reload, `.gdshader` loading, `run_tests`/headless self-test, controlled GDScript execution, system-command execution, and project export. Their raw bounded output is retained, but detected compiler/runtime diagnostics are additionally returned as structured evidence. Generic file, permission, and transport errors do not pretend to have source locations. Each result filters or tags diagnostics by its operation identity and affected resources so a model never repairs a script based on an earlier unrelated error.

All structured diagnostics survive DTO, HTTP, service, and next-model-message boundaries. The map agent uses the exact builder diagnostic to read the affected source and submit a narrowly scoped approval-gated repair; it may retry validation/rebuild only after approved relevant evidence changes. This strengthens the existing loop guard without imposing a guessed repair or suppressing legitimate new diagnostics.

### 5. Delete map-specific mutation surfaces as one vertical slice

The map mutation tool definitions, registrations, executor dispatch, `MapTools` mutation implementations, map-agent mutation permissions, `edit_map`-specific budgets, preview rendering branches, prompt instructions, and tests are deleted together. There is no feature flag, disabled compatibility mode, or legacy map-mutation route. Read-only map inspection remains registered as a general observation tool and is granted through each agent's effective read-only tool set. Generic transcript renderers continue to show map fact reads, code edit approvals, reload results, screenshots, and typed errors, so the chat timeline remains the sole visible workflow surface.

### 6. Bound map facts and preserve typed failure evidence

`describe_map_region` is an observation tool, not a bulk map export. It will return a bounded cell budget and compact summaries for empty/repeating regions, together with `truncated` metadata and a suggested smaller follow-up query. The map agent will query progressively around the requested boundary instead of submitting thousands of cells to the model context.

When a map-authoring edit fails with a typed outcome, the generic front-tool continuation contract must return that complete error result to the originating map-agent frame. The agent must then explain the failure, inspect when necessary, or propose a safe alternative. For a hand-painted map without an authoring source, the normal next step is the `@tool` bootstrap plan, not an unsupported-target terminal response. The generic envelope guard is specified in the sibling `tool-error-continuation` change; this change integrates the map-specific prompt, authoring guide, observation, and smoke-test behavior.

An inner editor outcome of `blocked`, `failed`, or `unavailable` is not a successful tool application. The frontend must emit a complete outer typed error result containing the inner status and error code so the map agent can stop, explain the blocker, and choose a bounded next step. It must not repeatedly replace the builder script merely because transport-level tool execution returned normally.

### 6.1 Preserve text-file existence when aborting a map batch

The generic text-write undo record must distinguish a pre-existing empty file from a file that did not exist before the batch. On interrupt, reset, or local abort, it restores the old contents for an existing file and removes a newly created file. This prevents an interrupted map bootstrap from leaving empty builder/layout placeholders that later look like valid authoring sources and cause opaque parse or load errors.

## Risks / Trade-offs

- [Existing hand-painted maps are difficult to alter through source code] → Use retained map inspection and the preloaded `@tool` bootstrap recipe to create a generated-only authoring entry point before any optional migration of the original layer.
- [A reload can overwrite or conflict with unsaved editor memory] → Reload checks open/dirty state and fails closed with a clear user action; it never auto-saves or silently discards edits.
- [A screenshot may show an unrelated viewport or omit runtime-only output] → Bind screenshot requests to the selected target/mode where possible and report its scope; do not claim semantic success.
- [Deleting map mutation tools breaks existing prompts or callers] → Delete registrations, implementations, metadata, routing, previews, and prompts atomically; preserve read-only inspections and add regression tests that the deleted tool names cannot be resolved. Operational rollback is a source-control revert of the entire change, not an in-product compatibility path.
- [Generic file edits are broader than map edits] → Keep existing path, stale-file, confirmation, and diff guardrails; use an explicit map-authoring editable-file allowlist.
- [Large observation payloads cause excessive model latency] → Cap and summarize region observations; require progressive, target-focused reads.
- [Local tool failure leaves map-agent pending] → Require a complete typed error envelope and map-agent recovery response; never submit a partial tool result.
- [The model guesses Godot editor APIs] → Require class-documentation reads before completing the supplied builder template, and test the template against supported node types.
- [A resource reload is mistaken for builder execution] → Use a distinct, approval-linked builder rebuild operation with a fixed public interface and typed rebuild evidence.
- [Interrupt rollback leaves empty bootstrap files] → Record whether each text file existed before the write; delete newly created files on abort and test the exact interruption path.
- [A typed inner editor blocker is wrapped as success] → Map `blocked`, `failed`, and `unavailable` outcomes to complete outer error results and prohibit blind builder rewrites.
- [Godot displays a builder syntax error that the map agent cannot see] → Collect compiler diagnostics for the exact approved builder before reload/rebuild, attach them to the map-agent turn, and expose a read-only diagnostic inspection capability.
- [A stale or unrelated log entry is presented as a current compiler failure] → Correlate structured diagnostics with the current operation and affected resource; preserve a bounded raw record without treating log-file locations as source locations.
- [Other engine-facing tools hide compiler errors in raw output] → Normalize supported script, shader, reload, headless/test, command, and export diagnostics into the shared contract while retaining raw bounded output.
- [Scene reload creates a node from a stale script resource] → Normalize script/resource reload before scene reload and distinguish `builder_instance_stale` from an on-disk missing method.
- [The model repeats an unchanged failing rebuild] → Bind failed states to a source/layout/scene fingerprint and require an approved repair that changes it before another engine invocation.
- [A post-write validator mistakes a freshly written script for a missing file] → Scan/observe the resource before validation, return raw/normalized/existence facts, and preserve `write_applied` independently from the validation result.
- [A successful reload loses its evidence at the protocol boundary] → Contract-test the complete reload result through DTO and service delivery; `{}` is a protocol failure, not successful verification.
- [A builder node has only a script attachment] → Require the exported target/layout/ownership properties in the bootstrap scene contract and return a configuration repair target before any rebuild.

## Migration Plan

1. Implement and test the source-file map workflow, interruption-safe text Undo, approval-linked builder rebuild contract, and retained read-only inspection path.
2. Delete the legacy mutation implementations and every route, budget, prompt, preview, and test that refers to them in the same implementation change.
3. Run migration verification against supported fixtures and ensure the deleted tool names cannot be registered or dispatched.
4. Roll back only by reverting the complete source-control change; source-file edits made by the new workflow remain ordinary project changes and are recoverable through existing Undo/version control.

## Open Questions

- Which project file types, beyond `.gd`, `.tscn`, and `.tres`, are in the first map-authoring allowlist?
- Which Godot editor reload APIs and modes correctly refresh supported `@tool` generator scenes without losing dirty editor state?
- Does the first supported map use case require only editor-visible `@tool` generation, or will a later change add an explicit run-scene verification mode?
- Which fixed builder interface and bounded rebuild result fields should be versioned as the first supported map-authoring contract?
