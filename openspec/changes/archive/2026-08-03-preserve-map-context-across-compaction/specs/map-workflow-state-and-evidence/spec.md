## ADDED Requirements

### Requirement: Failure frontier persists the structured repair plan
The reducer SHALL store the validator's full `repair_plan`/`issue_details` alongside `error_code` and `blocked_reason` in the scoped failure frontier, and SHALL NOT reduce a validation failure to its error code alone. The persisted repair plan SHALL survive message compaction because it lives in `map_task_state`, not in the conversation history.

#### Scenario: Validation failure is recorded
- **WHEN** a platform plan validation fails with a per-field `repair_plan`
- **THEN** the reducer stores the repair plan in the scoped failure frontier and the actionable failure details persist independently of the tool-result message

#### Scenario: Conversation compacts after a failure
- **WHEN** the tool-result message carrying the repair plan scrolls out of the recent message window and is summarized
- **THEN** the repair plan remains available in the failure frontier and can be re-surfaced without re-running validation

### Requirement: Map-progress digest is surfaced to the agent context each turn
The runtime SHALL re-derive a compact map-progress digest from authoritative `map_task_state` — current revision, stage, and the latest scoped failure `error_code` plus its persisted `repair_plan` — and SHALL inject it into the agent's per-turn context. The digest SHALL be re-derived from state on every turn, including the turn immediately following compaction, so it does not depend on the LLM summarizer preserving tool-result history.

#### Scenario: Agent turn begins after compaction
- **WHEN** a new agent turn begins after conversation compaction removed older tool-result messages
- **THEN** the agent context still carries the current map revision, stage, and latest failure repair plan, re-derived from state

#### Scenario: No active map task
- **WHEN** no map task is active in the session
- **THEN** the runtime injects no map-progress digest and the agent context is unchanged
