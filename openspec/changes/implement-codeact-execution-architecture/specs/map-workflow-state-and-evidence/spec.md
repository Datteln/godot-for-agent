## MODIFIED Requirements

### Requirement: Planning and execution statuses are stored independently
Workflow state MUST represent planning delivery separately from map CodeAct execution, validation, and task diff evidence. Delivering a final candidate MUST NOT imply validation success, successful worker file modification, completion evidence, or revision advancement. A terminal `failed_validation` outcome MUST retain its execution id, diff artifact reference, validation failures, retry count, and recovery disposition.

#### Scenario: Final validation attempt fails
- **WHEN** a map CodeAct task exhausts its validation repair budget after modifying project files
- **THEN** state records `planning_status=delivered`, `execution_status=failed_validation`, the current task diff reference, validation evidence, and no completion publication

#### Scenario: Map write validates successfully
- **WHEN** the worker-applied map change passes the required range and semantic validators
- **THEN** execution state records the validation and diff evidence without changing the historical planning publication record

## ADDED Requirements

### Requirement: Map CodeAct repair context survives continuation
The reducer MUST persist each map CodeAct validation result, repair report, retry budget identity, task execution id, and task diff artifact reference so the same map owner receives an actionable bounded repair context after transport interruption, compaction, or restart.

#### Scenario: Conversation compacts after a map failure
- **WHEN** the most recent map validator report is removed from model message history
- **THEN** the resumed map owner receives the persisted repair report and diff reference without rerunning the failed validator

