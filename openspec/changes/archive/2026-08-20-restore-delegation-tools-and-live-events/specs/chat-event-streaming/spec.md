## ADDED Requirements

### Requirement: Streaming deltas render smoothly in the chat UI
The chat UI MUST present incoming text and reasoning deltas with a small per-frame reveal instead of replacing the whole block at once, while keeping event acceptance order intact and never blocking on rendering. The reveal queue MUST be bounded by the item lifecycle: finalize drains any pending characters immediately, and discard/remove/reset drops the queue without side effects.

#### Scenario: Delta arrives mid-stream
- **WHEN** a text or reasoning delta patch targets a streamed block
- **THEN** the UI reveals the newly added characters incrementally across frames (≤ a few characters per frame) rather than instantly replacing the block

#### Scenario: Finalize drains the reveal queue
- **WHEN** the streamed block transitions to `complete` via finalize
- **THEN** any still-pending characters are shown immediately in the same update, so no text is left unrendered

#### Scenario: Discard abandons pending reveal
- **WHEN** the submission boundary discards or removes the streamed item while characters are pending in the reveal queue
- **THEN** the queue is dropped with the item and no further frame updates reference it

### Requirement: Run-phase tool events stream in throttled batches
While an atomic submission is running, the service SHALL publish frontend-visible run-phase events (`agent_tool_calls`, `server_tool_start`, `server_tool_result`, `agent_step`) to the event store in small throttled batches instead of holding them until the submission commits. Each published event MUST carry provisional preview identity so the submission boundary (commit or discard) resolves it exactly once, and MUST NOT be re-published by the commit flush.

#### Scenario: Agent executes a tool mid-submission
- **WHEN** the agent issues a `server_tool_start` / `server_tool_result` pair while the submission is still running
- **THEN** the event channel exposes the tool item to the client within a throttle batch (≤ configured count or ≤ configured time window after the previous flush), while Session persistence is still uncommitted

#### Scenario: Commit flush does not duplicate live events
- **WHEN** the submission commits after run-phase events were already published live
- **THEN** the flush publishes only events not yet published live, so the client renders each tool item exactly once

#### Scenario: Failure boundary retains live tool events
- **WHEN** the submission fails with an agent-level error (error response committed to the Session)
- **THEN** the client retains the streamed tool items and reasoning previews, finalized with the error code as status, instead of removing them

#### Scenario: Rollback removes streamed tool events
- **WHEN** the submission fails before Session persistence succeeds (infrastructure rollback)
- **THEN** the discard boundary removes the streamed tool items together with the reasoning previews, so no provisional content survives a rolled-back turn

## MODIFIED Requirements

### Requirement: Provisional previews have an explicit lifecycle
Every preview emitted before atomic commit MUST carry stable submission identity and MUST be resolved by a commit or discard boundary without being duplicated as committed text. Interrupt and cancel boundaries MUST also resolve pending provisional tool items: every tool item left in a non-terminal status at interrupt SHALL be finalized with an interrupted status or discarded, and no item may remain permanently `pending`. Commit boundaries triggered by a failed submission MUST retain the provisional previews and mark them with the failure reason instead of discarding them; discard boundaries are reserved for infrastructure rollback, interrupt, and rejection before persistence.

#### Scenario: Submission commits
- **WHEN** Session persistence and transactional publication succeed after provisional previews were emitted
- **THEN** the service emits a matching preview-committed boundary and the client retains the displayed text without appending it again

#### Scenario: Submission fails with an agent-level error
- **WHEN** the submission completes with an error response that is durably committed to the Session (e.g. `agent_turn_budget_exhausted`, model failure, rejected request)
- **THEN** the service emits a preview-committed boundary carrying the `reason` error code and the client retains the preview text and thought trace, marked as failed

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