## ADDED Requirements

### Requirement: Reload targets are explicit, bounded, and project-scoped
The editor reload operation SHALL accept only a bounded list of explicitly named project-relative targets produced by the approved code-edit batch. It MUST validate every path against the project boundary and supported reloadable target types, and MUST reject arbitrary paths, unapproved targets, and arbitrary script execution requests.

#### Scenario: Reload approved changed scene
- **WHEN** an approved map code-edit batch changes a supported project-relative scene or script target
- **THEN** the reload operation receives that target identity and reload intent and returns a typed result for that target

#### Scenario: Reload request includes an external path
- **WHEN** a reload request contains a path outside the project boundary
- **THEN** the operation rejects the request before scanning, importing, opening, or reloading any file

### Requirement: Reload protects unsaved editor state
The reload operation MUST inspect editor state relevant to the target and MUST NOT silently save, discard, or overwrite unrelated unsaved scene edits. If safe reload is not possible, it SHALL return a typed blocked result explaining the affected target and required user action.

#### Scenario: Target scene is dirty in the editor
- **WHEN** the requested target scene has unsaved in-editor changes that a reload would replace
- **THEN** the operation returns `reload_blocked_dirty_editor_state` and leaves the editor state unchanged

### Requirement: Reload results are observable and do not imply runtime execution
The reload operation SHALL return one of `reloaded`, `failed`, `blocked`, or `unavailable`, with bounded diagnostics and affected target identities. It MUST distinguish editor resource/scene reload from executing normal runtime-only scripts.

#### Scenario: Runtime-only generator after scene reload
- **WHEN** a changed map generator runs only during normal game execution and the selected editor reload cannot execute it
- **THEN** the operation returns an outcome that records reload success but runtime visual verification as unavailable

### Requirement: Screenshot verification is target-scoped and advisory
After a successful eligible reload, the system SHALL capture a screenshot only through the existing screenshot capability and record its target, mode, dimensions, and availability. Screenshot capture MUST NOT mutate project content and MUST NOT be treated as proof of collision, reachability, semantic correctness, or gameplay completion.

#### Scenario: Screenshot shows editor-visible map output
- **WHEN** an editor-visible map target reloads successfully and the requested viewport can be captured
- **THEN** the workflow records the screenshot as visual evidence for that target without claiming semantic verification

#### Scenario: Screenshot cannot show the target
- **WHEN** the editor viewport cannot display the reloaded target or capture fails
- **THEN** the workflow records visual evidence as unavailable and does not replace it with inferred success

#### Scenario: Reloaded builder resource is not executed
- **WHEN** a reload updates a `.gd` builder resource without reloading or invoking its existing editor instance
- **THEN** the result identifies only the resource reload and the workflow does not claim that map cells were rebuilt or that visual map output changed

#### Scenario: Editor invokes an attached tool builder
- **WHEN** the bounded builder rebuild operation has verified an open selected scene and its already attached eligible `@tool` builder node
- **THEN** the Godot editor engine invokes only that builder's fixed public rebuild method in the editor, does not start the game, and returns typed rebuild evidence

### Requirement: Builder rebuild results gate visual verification
The workflow SHALL request map visual verification after an editor-visible builder only when the dedicated builder rebuild operation returned `rebuilt`. A `blocked`, `failed`, or `unavailable` reload or rebuild result MUST be retained as typed failure evidence and MUST NOT trigger a screenshot that is presented as validation of the requested map change.

#### Scenario: Builder rebuild is blocked
- **WHEN** the selected builder cannot be safely executed because its target, approval linkage, or generated-only ownership cannot be verified
- **THEN** the workflow records the typed blocker and does not capture a success-oriented screenshot or claim the map changed

### Requirement: Reload dependencies in script/resource-before-scene order
For an approved batch containing both a map-builder script/resource and a dependent scene, the reload operation SHALL reload and observe the script/resource before it reloads the dependent scene, regardless of tool-call target order. It MUST validate the on-disk builder source before inspecting the attached node's fixed rebuild method. If the source contains the required method but the attached node does not, it SHALL return `builder_instance_stale` rather than `builder_method_missing` and require the bounded current-scene reload path.

#### Scenario: Builder and scene are both reloaded
- **WHEN** a map workflow submits a changed `map_builder.gd` and its dependent scene in either order
- **THEN** the editor reloads the script/resource first, reloads the scene second, and validates the attached builder only after that current-instance path completes

#### Scenario: Attached instance is older than its source
- **WHEN** the on-disk builder source defines `rebuild_from_layout()` but the currently attached editor node does not expose it
- **THEN** the result is a typed `builder_instance_stale` outcome with the scene/script repair target, not a claim that the source lacks the method

### Requirement: Reload and rebuild evidence survives every protocol boundary
The complete bounded result returned by a reload, rebuild, or post-write script validation SHALL be carried through the frontend DTO, HTTP request schema, service-side tool-result append, and next model message. Existing redaction may remove sensitive content but MUST NOT replace a successful result with an empty dictionary. A successful reload result MUST retain its status, requested/ordered/reloaded/unavailable targets, reload mode, visual-evidence state, and bounded diagnostics; an error MUST retain its path observations and repair target.

#### Scenario: Script resource reload completes
- **WHEN** `reload_map_targets` successfully reloads an approved builder script
- **THEN** the following map-agent message contains the typed reload result and target identities rather than `result: {}`, so it can decide whether a dependent scene reload is still required

### Requirement: Godot execution diagnostics are structured and correlated
Every supported Godot-producing operation—script/resource/scene reload, builder validation, shader loading, headless/test execution, controlled GDScript execution, system-command execution, and project export—SHALL retain bounded raw output and return structured diagnostics when engine/compiler output is available. A structured diagnostic MUST identify its source, severity, canonical affected resource path when determinable, source line and column when reported, complete bounded message, bounded raw text, and operation/execution correlation identity.

The frontend MUST correlate diagnostic collection with the operation's start/end window and declared affected resources. It MUST NOT report a global historical log entry as the current operation's failure merely because its text contains a matching filename. File, permission, timeout, and transport failures that have no compiler/runtime source location MUST remain explicitly unlocated rather than receiving fabricated positions.

#### Scenario: Headless run reports a GDScript compile error
- **WHEN** a controlled headless GDScript or test operation emits a compiler error for a project script
- **THEN** the tool result preserves bounded stdout/stderr and includes a structured diagnostic with the affected resource and available source location/message for the next model turn

#### Scenario: Shader resource cannot compile
- **WHEN** an approved shader write or resource reload emits a Godot shader compiler error
- **THEN** the result includes the shader resource and available location/message as structured diagnostic evidence instead of only `shader_load_failed` or `resource_reload_failed`

#### Scenario: Old editor log is unrelated to a reload
- **WHEN** a current reload fails or succeeds while a prior editor log contains an error for another operation
- **THEN** the current result retains only diagnostics correlated to the reload and may expose the old log only as explicitly historical inspection data, never as its failure cause

### Requirement: Reload uses canonical project resource identities
Before comparing reload targets with approved paths, dirty scenes, open scenes, or resource filesystem entries, the frontend SHALL canonicalize valid project-relative paths and valid `res://` resource URIs to the same `res://` identity. It MUST apply the existing project boundary after canonicalization and MUST NOT treat a Godot-returned `res://` path as an external absolute path. `user://` paths may be normalized by generic tools but MUST be rejected as reload targets because they are not project resources.

#### Scenario: Godot returns an open scene URI
- **WHEN** the editor reports an open or dirty scene as `res://scenes/game.tscn`
- **THEN** a matching approved/reload target is compared using the same canonical identity and dirty-state protection remains effective

#### Scenario: Reload request uses a valid resource URI
- **WHEN** a bounded approved reload request provides `res://scripts/map_builder.gd`
- **THEN** the frontend reloads or reports the true typed resource outcome; it does not return `invalid_reload_targets` because of URI normalization
