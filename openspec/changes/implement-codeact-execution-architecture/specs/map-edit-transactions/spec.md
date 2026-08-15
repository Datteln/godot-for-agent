## MODIFIED Requirements

### Requirement: Map transaction boundaries are explicit
Every map mutation MUST be performed as a worker file CodeAct action associated with a stable `task_execution_id`, task diff evidence, and required write-after validation. It MUST NOT require an Editor Undo transaction, an approved write-group transaction, or a pre-write proposal solely to make an ordinary project-file change.

#### Scenario: Standalone map mutation succeeds
- **WHEN** a map agent applies a project-file edit and its required validation passes
- **THEN** the system records the task diff and successful validation as the mutation evidence without creating an Editor Undo action

#### Scenario: Map transformation uses a temporary script
- **WHEN** a map transformation is performed by a temporary GDScript
- **THEN** it runs only in the task worker, and the system collects diff and validation evidence after the script exits

### Requirement: Validation failure rolls back the write group
The system MUST retain the current project diff when map write-after validation fails, is cancelled, or exhausts its repair budget. It MUST record the typed failure and end the task as `failed_validation` when it cannot continue; it MUST NOT automatically restore map content, revisions, or indexes to a before snapshot.

#### Scenario: Later validation fails
- **WHEN** a map write has changed files and final validation fails after repair budget exhaustion
- **THEN** the system preserves the affected diff for user review and reports `failed_validation` instead of rolling it back

