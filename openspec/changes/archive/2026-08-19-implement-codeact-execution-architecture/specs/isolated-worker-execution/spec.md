## ADDED Requirements

### Requirement: Worker is task-scoped and isolated
For every `task_execution_id`, the system MUST create one rootless Docker worker reused by that task's Shell, headless, and temporary-script calls. The worker MUST run as a non-root user with no network, host user directory, Docker socket, Git credentials, or long-lived credentials and MUST enforce configured CPU, memory, process, duration, and output limits.

#### Scenario: Commands run during one task
- **WHEN** a task invokes `shell.run` and later `godot.headless`
- **THEN** both calls use the task's isolated worker while each call runs as an independent process

#### Scenario: Task ends
- **WHEN** a task completes, is cancelled, or times out
- **THEN** the system destroys its worker, task temporary directory, and isolated cache and leaves no worker child process running

### Requirement: Worker mounts only resolved allowed roots and isolated Godot cache
The worker MUST mount only the resolved project root and explicit allowed link targets at `/workspace`; all file and cwd checks MUST use resolved real paths. It MUST use a task-specific volume for `/workspace/.godot` that is not shared with the Windows Editor and does not enter the project diff.

#### Scenario: Project path follows an unauthorized link
- **WHEN** resolving a logical project path reaches a target outside the configured allowed roots
- **THEN** worker creation is rejected before any mount or command execution

#### Scenario: Worker imports a project
- **WHEN** Godot headless writes import data
- **THEN** it writes to the task-specific `.godot` volume rather than the host Editor cache

### Requirement: Temporary scripts remain inside the worker execution boundary
The system MUST allow a temporary GDScript only in the task directory and execute it only with worker Godot headless. It MUST collect the project diff and run target validation after execution, then delete the temporary script when the task ends unless it was explicitly retained through `project.edit` as a project file.

#### Scenario: Scene transformation needs a script
- **WHEN** an agent creates a temporary scene transformation script
- **THEN** it is executed with `godot --headless --path /workspace --script` in the worker and cannot become a host Editor execution request

