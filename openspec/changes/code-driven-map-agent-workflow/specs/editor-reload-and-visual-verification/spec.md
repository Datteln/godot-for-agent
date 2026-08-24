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
