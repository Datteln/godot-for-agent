## ADDED Requirements

### Requirement: Map requests use a dedicated code-driven workflow
The system SHALL route map and level authoring requests to a dedicated map-agent workflow. The workflow MUST inspect authoritative map facts and relevant readable project files, use its curated Godot map-authoring guide and required class-documentation reads to select an authoring strategy, publish a map-oriented plan identifying the map target/layer, target files, and visual acceptance intent, obtain normal write approval, apply approved generic code/configuration edits, and then request reload and visual verification. It MUST preserve the generic code-edit path's project boundary, prior-read, stale-file, confirmation, transcript, and Undo guarantees.

#### Scenario: Supported map generator change
- **WHEN** a user asks to change a map whose generator or configuration is represented by supported readable project files
- **THEN** the map agent reads the existing map facts, presents the target/layer, affected files, and map-oriented plan, applies only approved generic file edits, and continues to reload and visual verification

#### Scenario: Stale target file
- **WHEN** a target file changes after the map agent reads it and before an approved precise edit is applied
- **THEN** the edit is rejected through the existing stale-file outcome and the map workflow does not continue to reload or claim the map changed

#### Scenario: Hand-painted map has no authoring source
- **WHEN** the selected TileMap, TileMapLayer, or GridMap has no readable generator or layout configuration
- **THEN** the map agent MUST emit the first ordinary approval-gated bootstrap edit that creates a `@tool` builder or readable layout source, targets a generated-only layer, and preserves the existing hand-painted layer until an explicit later migration is approved; it MUST NOT end with a prose-only request for additional confirmation because the edit's inline approval card is the user confirmation

### Requirement: Map observation remains generally available and map authoring uses authoritative facts
The system SHALL retain `describe_tilemap_selection` and `describe_map_region` as general read-only observation tools. Any agent whose effective read-only tool set includes these tools MUST be able to use them without map-write authority. Before source changes that depend on an existing map target, layer, coordinates, existing cells, or tile identity, the map-agent workflow MUST obtain those facts through the inspection tools and MUST NOT infer them from screenshots or source text alone.

#### Scenario: Existing TileMapLayer target
- **WHEN** a requested map change applies to an existing TileMapLayer or legacy TileMap with potentially multiple layers
- **THEN** the map agent uses read-only inspection to identify the target and intended layer before it publishes an editable source-file plan

#### Scenario: Scene workflow aligns content to a map
- **WHEN** a non-map authoring workflow with read-only map-observation permission needs to align scene content with an existing TileMap, TileMapLayer, or GridMap
- **THEN** it can read the target's coordinate and layer facts without being granted any map mutation tool

#### Scenario: Source and screenshot leave tile identity ambiguous
- **WHEN** readable project files and screenshots do not establish the required TileSet source or atlas identity
- **THEN** the map agent reads the relevant map facts or reports missing facts, and does not guess a tile identity

### Requirement: Map authoring does not use map-specific mutation tools
The system MUST delete `edit_map`, `fill_rect`, and `paint_from_image_grid` from the tool registry, frontend executor dispatch, `MapTools` mutation implementations, agent definitions, routing, `edit_map`-specific budgets, previews, and mutation-specific tests. The system MUST NOT retain a feature flag, disabled compatibility mode, legacy route, or callable implementation for these tools. General read-only map inspection remains available.

#### Scenario: Map request after migration
- **WHEN** the user requests a supported map change after the code-driven workflow is enabled
- **THEN** the pending approval contains generic source/configuration edits and no map-cell mutation call, while preceding map inspection results remain visible as read-only facts

#### Scenario: Legacy mutation tool is requested
- **WHEN** any agent or frontend payload refers to `edit_map`, `fill_rect`, or `paint_from_image_grid` after the migration
- **THEN** the name cannot resolve to a registered or dispatchable tool and no map mutation implementation is invoked

#### Scenario: Serialized map has no semantic authoring entry point
- **WHEN** inspection finds a hand-painted serialized TileMap/GridMap with no readable authoring source
- **THEN** the workflow uses the preloaded `@tool` bootstrap recipe and reads the selected Godot class documentation before proposing the builder and layout assets; it MUST NOT guess or hand-assemble raw cell serialization

### Requirement: Map workflow reports evidence without overstating completion
The map workflow SHALL report distinct outcomes for file edits, reload, screenshot capture, and visual expectation. It MUST NOT describe a map request as semantically or gameplay verified solely because a file edit succeeded, a target reloaded, or a screenshot was captured.

#### Scenario: Screenshot captured after reload
- **WHEN** an approved code-edit batch reloads successfully and produces a screenshot
- **THEN** the final transcript identifies the edited files, reload result, screenshot scope, and that the evidence is visual only

#### Scenario: Reload or screenshot unavailable
- **WHEN** the edit succeeds but reload fails or screenshot capture is unavailable
- **THEN** the workflow records the specific unavailable or failure result and does not claim visual verification or task completion

### Requirement: Map observation is bounded and progressive
The map workflow SHALL treat `describe_map_region` as bounded observation. It MUST cap returned cell detail, summarize empty or repetitive areas, and report truncation and the observed bounds when the requested region exceeds the result budget. The map agent MUST use focused follow-up queries rather than requesting an unbounded map export.

#### Scenario: Large TileMap region request
- **WHEN** a map-agent region request exceeds the observation cell budget
- **THEN** the result returns bounded detail and compact summary metadata with `truncated=true`, and the agent narrows its next query to the relevant target boundary or layer

### Requirement: Map tool failures remain recoverable evidence
The map workflow MUST receive a complete typed error outcome for a failed approved edit and continue the agent turn. It MUST use the typed failure to explain the outcome, inspect when necessary, or propose a safe alternative. For a hand-painted map without an authoring source, it MUST propose the curated `@tool` bootstrap path rather than terminate the request as unsupported.

#### Scenario: Builder bootstrap cannot be applied
- **WHEN** an approved `@tool` builder or layout creation step fails locally
- **THEN** the frontend returns a complete typed error result, the map agent continues the conversation, and it reports or proposes a safe next step without an HTTP validation failure

### Requirement: Map builder execution is explicit, approval-linked, and observable
The system SHALL treat a `@tool` builder rebuild as a distinct editor operation, not as an implied consequence of a text write or resource reload. Before executing a rebuild, it MUST verify the open selected map scene, the builder node, the generated-only target, and the builder/layout identities. The rebuild operation MUST ask the Godot editor engine to invoke only the curated fixed builder interface on that already attached node instance; it MUST NOT start the game, invoke an arbitrary lifecycle callback, or accept an arbitrary method name or script path. It MUST return a typed `rebuilt`, `blocked`, `failed`, or `unavailable` result with bounded affected-target evidence. A generic resource reload MUST NOT be reported as builder execution.

#### Scenario: Approved layout change rebuilds the generated-only layer
- **WHEN** an approved readable layout change belongs to an established map-authoring entry point and its selected builder is available in the open scene
- **THEN** the Godot editor engine invokes the fixed rebuild interface against that attached builder node, regenerates only its generated-only target, and reports a typed rebuild result before requesting visual evidence without starting the game

#### Scenario: Scene reload is not in the current approved batch
- **WHEN** a map agent tries to reload a scene merely to trigger a builder, but the scene is not a path in the current approved batch
- **THEN** the generic reload operation remains blocked and the workflow uses the bounded established-builder rebuild operation or reports the required approval, rather than claiming the map changed

#### Scenario: Empty or malformed layout source
- **WHEN** a builder or layout source created or read by the workflow is empty or malformed
- **THEN** the workflow returns typed validation failure, does not invoke the builder, and the map agent reads the source and proposes one bounded repair rather than repeatedly rewriting lifecycle callbacks

### Requirement: Map tool semantic failures remain outer typed errors
The frontend SHALL convert a map editor result whose semantic status is `blocked`, `failed`, or `unavailable` into a complete outer typed error tool result. The error result MUST retain the inner status, error code, and bounded diagnostics so the map agent receives recoverable evidence. A transport-level execution completion MUST NOT be represented as a successful map edit, reload, or rebuild when its inner result is non-success.

#### Scenario: Reload target is not approved
- **WHEN** `reload_map_targets` rejects a scene because it is absent from the current approved paths
- **THEN** the map agent receives a complete typed error containing `unapproved_reload_target` and does not blindly issue another builder-script replacement

### Requirement: Interrupted map bootstrap does not leave empty text artifacts
The generic text-file undo record used by map authoring SHALL retain whether a file existed before the batch. On local interrupt, reset, or abort, it MUST restore contents for previously existing files and remove files created by the interrupted batch. It MUST NOT leave an empty `.gd`, `.json`, or other readable map-authoring placeholder in place of a newly created file.

#### Scenario: Interrupt after creating layout and builder
- **WHEN** a user interrupts a bootstrap batch after it has created a layout and `@tool` builder
- **THEN** both newly created files are removed by rollback, no empty placeholders remain, and the next map request observes that no authoring entry point exists

### Requirement: Builder diagnostics gate map execution and enable source repair
After an approved map-builder `.gd` write, the system SHALL collect Godot editor compiler/script diagnostics for that exact project-relative script before a scene reload or builder rebuild. The map agent MUST receive bounded, repairable diagnostic evidence containing an error code, path, line and column when available, and redacted message, and MUST have read-only access to inspect current builder diagnostics. If the builder contains compiler errors, is empty, or its required layout source is empty or malformed, the system MUST return a typed failure and MUST NOT reload the scene, invoke the builder, or request a success-oriented screenshot. The map agent MUST read the indicated source/diagnostic and make one ordinary approval-gated repair proposal rather than repeatedly invoking reload/rebuild.

#### Scenario: Godot reports a syntax error in the approved builder
- **WHEN** Godot reports a compiler error for the approved `map_builder.gd`
- **THEN** the map agent receives `builder_script_compile_failed` with the affected path and available location/message, no rebuild is invoked, and its next authoring action is a bounded approved source repair

#### Scenario: Existing bootstrap files are empty
- **WHEN** the selected builder or readable layout file exists but has zero content
- **THEN** the workflow returns `authoring_entry_point_missing`, does not treat it as an established builder, and plans a fresh approval-gated bootstrap or repair rather than attempting execution

### Requirement: Builder diagnostics identify the current source failure
The builder diagnostic returned after an approved builder write or before a rebuild MUST be correlated with that validation operation and its exact canonical `res://` script resource. Each available compiler diagnostic MUST include its source, severity, resource path, source line and column when Godot reports them, complete bounded message, and bounded raw diagnostic text. The system MUST NOT present the path or line of a general editor log as the builder source location, and MUST NOT select a stale, unrelated, or different-resource log entry as the current builder failure.

When Godot's editor diagnostic API does not expose the parser details, the frontend MUST perform a bounded, controlled, non-mutating validation capture for the approved script and parse its compiler output. This validation MUST NOT execute the builder's rebuild method, normal lifecycle callbacks, or the game.

#### Scenario: Current builder parser error has an editor location
- **WHEN** the approved builder contains a Godot parser error with a reported resource, line, column, and message
- **THEN** `builder_script_compile_failed` returns that exact current diagnostic to the map agent, which reads the affected source and proposes a bounded approval-gated repair before another validation or rebuild

#### Scenario: Historical command error mentions the builder path
- **WHEN** an older command log contains an error mentioning `map_builder.gd`, but the current builder validation produces a different diagnostic or none
- **THEN** the historical error is not returned as the current builder compiler diagnostic and does not determine the repair proposal

### Requirement: An unchanged failed builder cannot be blindly retried
The system SHALL attach a failed-builder fingerprint based on the current builder source, layout source, and relevant scene identity to every non-successful builder validation or rebuild result. Until an approved source, layout, or scene edit changes that fingerprint, a repeated reload or rebuild attempt MUST return `builder_repair_required` without invoking the Godot editor. This guard MUST preserve the agent's ability to choose and propose a repair; it MUST NOT automatically rewrite source or permanently prohibit retry after changed evidence exists.

#### Scenario: Rebuild fails and the model repeats the same request
- **WHEN** a builder rebuild has failed and no approved relevant file or scene edit has changed its fingerprint
- **THEN** a subsequent rebuild request returns `builder_repair_required`, includes the prior repair target, and does not execute the builder again

### Requirement: A successful builder write is distinct from post-write validation
When an approved code-driven map write has successfully persisted a builder `.gd` file, the system SHALL retain and return `write_applied=true` even if the subsequent Godot resource scan or compile validation fails. Before classifying a newly written builder as missing or compiling it, the frontend MUST update and observe the Godot resource filesystem. Every non-successful post-write validation result MUST include the raw script resource path, normalized project-relative path, file-existence observation, and bounded diagnostics. It MUST use `builder_script_missing` only when those facts demonstrate absence; a parse/load failure with an existing script MUST use `builder_script_compile_failed` with available compiler location/message evidence.

#### Scenario: Fresh builder is awaiting Godot filesystem observation
- **WHEN** an approved builder write has completed but Godot has not yet scanned the changed resource
- **THEN** the frontend scans/observes the resource before validation and does not report the successful write as a missing file

#### Scenario: Existing builder has a parser error
- **WHEN** the newly written builder file exists but Godot cannot parse or compile it
- **THEN** the map agent receives `write_applied=true`, `builder_script_compile_failed`, path observations, and available compiler diagnostics, then proposes a bounded source repair

### Requirement: Bootstrap scene configuration establishes a usable builder
A code-driven map bootstrap SHALL configure the builder scene node with its attached script, generated target path, readable layout path, and explicit generated-only ownership before it is considered an established authoring entry point. The system MUST NOT treat a scene node with only a script attachment as rebuildable.

#### Scenario: Bootstrap adds only the script attachment
- **WHEN** a scene edit attaches the builder script but omits any required generated target, layout path, or generated-only ownership property
- **THEN** rebuild returns a typed configuration failure naming the missing property/scene target and the map agent proposes the bounded scene repair instead of retrying the script

### Requirement: Godot resource URIs and project-relative paths share one safe identity
The frontend SHALL canonicalize both a valid project-relative path and a valid Godot `res://` resource URI to the same `res://` project identity before applying project-boundary, file-existence, approval, stale-read, resource-reference, or diagnostic logic. It SHALL also accept and normalize valid `user://` paths within their separate per-project user-data namespace. Attached scripts, scenes, reload targets, approval batches, and versioned map builder/layout sources MUST require `res://`; generic file/output tools may explicitly permit `user://`. It MUST reject operating-system absolute paths, empty paths, and traversal outside either namespace. A Godot-originated resource URI MUST NOT be classified as missing merely because normalization returned an empty or malformed path.

#### Scenario: Attached builder reports a Godot resource URI
- **WHEN** an attached builder script has `script.resource_path = res://scripts/map_builder.gd`
- **THEN** post-write validation and rebuild use `res://scripts/map_builder.gd` as the normalized identity, observe its actual existence, and only report `builder_script_missing` when that observed file is absent

#### Scenario: Bootstrap uses an exported layout URI
- **WHEN** a configured builder exposes `layout_path = res://map_layouts/ground_extension.json`
- **THEN** the rebuild checks the actual layout file and approval batch rather than rejecting the valid URI as an invalid layout path

#### Scenario: User-data output is supplied to an eligible generic tool
- **WHEN** an eligible generic file or output tool receives `user://ai_agent_outputs/preview.json`
- **THEN** it retains the `user://` namespace, applies its user-data boundary and confirmation policy, and does not convert the path to `res://`

#### Scenario: User-data URI is supplied to map reload or builder execution
- **WHEN** a reload target, attached builder script, scene path, or versioned map builder/layout source uses `user://`
- **THEN** the workflow returns a typed project-resource-path rejection and does not scan, reload, compile, or invoke the builder

#### Scenario: Unsafe external path is supplied
- **WHEN** a tool receives an operating-system absolute path or traversal path
- **THEN** it rejects the request before reading, writing, reloading, resolving a resource, or mutating editor state
