## ADDED Requirements

### Requirement: Project roots, logical paths, and Git roots are distinct
The system MUST derive and retain `logical_project_root`, `resolved_project_root`, and `repository_root` independently. Worker writes MUST be limited to the resolved root; Git status and diff MUST be read-only at the repository root unless that root is explicitly approved for mounting.

#### Scenario: Service root is linked below a repository
- **WHEN** the logical project root resolves to a linked `ai_agent_frontend` directory inside a larger Git repository
- **THEN** the worker receives only the allowed resolved project root while Git evidence is calculated from the independently discovered repository root

### Requirement: File edits detect concurrent workspace drift
The system MUST record task-start Git status and existing-diff summaries and record a content digest when a task first touches a file. Before applying a patch it MUST recheck that digest; a mismatch MUST return `workspace_conflict` without overwriting the file. It MUST attribute diffs observed after a task shell or temporary-script call separately from pre-existing diff.

#### Scenario: User changes a touched file
- **WHEN** a user or another process changes a file after the task recorded its digest and before its next edit
- **THEN** the edit is rejected with `workspace_conflict` and the task does not overwrite the external change

### Requirement: Open Editor files are protected from worker writes
When an EditorPlugin is connected, `project.edit` MUST perform a read-only status precheck for editable Editor-managed targets such as `.tscn` and `.tres`. If the target is open in the Editor, whether clean or dirty, it MUST return `editor_open_conflict` and skip the write; if no Plugin is connected, the system MAY write but MUST log that memory-state conflict detection was unavailable.

#### Scenario: Clean scene is open in Editor
- **WHEN** an agent tries to edit an open clean `.tscn` file
- **THEN** the system skips the disk write so a later user save cannot overwrite a worker change with stale Editor memory

