# editor-observation-rpc Specification

## Purpose

Define authenticated local Editor observation RPC endpoints, opt-in reload semantics, and non-authoritative failure handling.

## Requirements

### Requirement: EditorPlugin exposes only authenticated local observation methods
The EditorPlugin MUST register a project identifier, Editor instance identifier, and backend-issued short-lived token over an IPC endpoint restricted to the local machine. Each RPC MUST include task execution id, call id, method, parameters, and timeout; the Plugin MUST accept only allowlisted status, reload-for-validation, viewport capture, runtime-state, debugger-error, and profiler-snapshot methods.

#### Scenario: Plugin token expires
- **WHEN** the registration token expires or the Editor changes project, exits, or restarts
- **THEN** the registration is revoked and subsequent calls return a typed unavailable result

#### Scenario: Gateway selects an Editor
- **WHEN** a task requests an Editor method
- **THEN** the Gateway sends it only to the latest live Plugin instance registered for the task's project

### Requirement: Reload is opt-in, conflict-safe, and approval-gated
`reload_for_validation` MUST only reload a specified file from disk when it is already open and has no unsaved in-memory changes. The Gateway MUST call it only when an online validation need exists and MUST obtain policy approval because reload changes the user's Editor UI; otherwise it MUST return `editor_dirty_conflict` or a typed unavailable result without overwriting memory.

#### Scenario: Online screenshot needs fresh data
- **WHEN** validation requires a viewport screenshot for an open clean target and reload approval is granted
- **THEN** the Gateway reloads that target and then obtains the requested observation

#### Scenario: Target is dirty
- **WHEN** the requested reload target has unsaved Editor changes
- **THEN** the Plugin returns `editor_dirty_conflict` and does not reload or save the target

### Requirement: Editor failures and late results are non-authoritative observations
The Gateway MUST return typed `editor_unavailable`, `editor_busy`, `editor_cancelled`, timeout, or project-mismatch outcomes for failed observation calls. A result that arrives after cancellation MUST be audit-only and MUST NOT resume the cancelled task; runtime, debugger, profiler, screenshot, and log contents MUST be treated as untrusted data.

#### Scenario: Request is cancelled while Editor is busy
- **WHEN** an Editor RPC is cancelled before the Plugin responds
- **THEN** the task receives `editor_cancelled` and any later Plugin result is recorded without driving a new agent action