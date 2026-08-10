## MODIFIED Requirements

### Requirement: Provisional previews have an explicit lifecycle
Every preview emitted before atomic commit MUST carry stable submission identity and MUST be resolved by a commit or discard boundary without being duplicated as committed text. Interrupt and cancel boundaries MUST also resolve pending provisional tool items: every tool item left in a non-terminal status at interrupt SHALL be finalized with an interrupted status or discarded, and no item may remain permanently `pending`.

#### Scenario: Submission commits
- **WHEN** Session persistence and transactional publication succeed after provisional previews were emitted
- **THEN** the service emits a matching preview-committed boundary and the client retains the displayed text without appending it again

#### Scenario: Submission rolls back
- **WHEN** the submission is cancelled, rejected, or fails before Session persistence succeeds
- **THEN** the service emits a matching preview-discarded boundary and the client removes or clearly invalidates only that submission's provisional output

#### Scenario: Service restarts before a lifecycle boundary
- **WHEN** a process-local preview has no matching committed Session identity after restart
- **THEN** Session history does not restore that preview as committed conversation content

#### Scenario: A stale boundary arrives
- **WHEN** the client receives a commit or discard boundary for a request older than the active preview
- **THEN** it applies the boundary only to matching preview identity and does not alter the active request

#### Scenario: Interrupt leaves no zombie tool block
- **WHEN** the user interrupts a turn while front tools are executing or awaiting results
- **THEN** every tool item of that turn left in a non-terminal status is finalized with an interrupted status or discarded, and no tool block remains `pending` after the interrupt boundary
