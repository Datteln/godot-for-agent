## MODIFIED Requirements

### Requirement: Valid batches commit as one Session transaction
The system SHALL apply a validated front-tool result batch to an isolated Session working copy and make that batch, its reducer events, artifacts, planning-context entries, execution-scope facts, and resulting stage publication active as one durable commit before launching any subsequent agent or model continuation. A continuation SHALL use a fresh working copy based on that committed checkpoint and SHALL NOT be part of the batch's rollback boundary.

#### Scenario: All results are valid
- **WHEN** every result passes preflight and every reducer succeeds on the working copy
- **THEN** the system persists and activates the batch and stage facts as one commit before continuing orchestration

#### Scenario: Applying a result fails
- **WHEN** a reducer, artifact publication, or Session persistence fails before the stage commit
- **THEN** the active Session remains equal to its pre-batch state

#### Scenario: Subsequent model continuation times out
- **WHEN** the valid stage commit succeeds and the following model request times out, is cancelled, or loses its client connection
- **THEN** only the unfinished continuation is discarded and the committed tool results, snapshots, artifacts, workflow checkpoint, and owner publication remain recoverable

## ADDED Requirements

### Requirement: Stage-boundary continuation is idempotent
The runtime MUST associate a post-commit continuation with the committed checkpoint and canonical attempt identity. Retrying or recovering the continuation MUST NOT reapply the preceding front-tool batch or duplicate its artifacts, reducer events, approvals, or owner publication.

#### Scenario: Client reconnects after continuation timeout
- **WHEN** a valid tool batch committed but its subsequent model continuation did not complete
- **THEN** backend recovery resumes from the committed checkpoint under the same task and owner lineage without asking the client to resubmit the batch

#### Scenario: Identical batch is resubmitted
- **WHEN** the client retries a batch already committed at a stage boundary with the same turn identity and fingerprint
- **THEN** the runtime returns the existing committed result and does not start a duplicate internal stage

### Requirement: Committed machine facts outlive provisional chat output
Planning-context entries, candidates, deterministic execution operations and validation results, approval state, transaction results, evidence, and domain-owner publications SHALL be durable machine facts once their stage commit succeeds. Provisional assistant text or reasoning MUST NOT be required to restore those facts.

#### Scenario: Chat output is discarded
- **WHEN** provisional assistant output is cancelled after the stage commit
- **THEN** workflow recovery reconstructs the next action from committed machine facts rather than rereading the map or parsing partial prose

### Requirement: Planning-context and child-start commits preserve isolation
The runtime MUST upsert each planning-context entry by its stable context identity without replacing unrelated entries. Starting a specialist child MUST commit its task-stage transition and child lineage as one reducer-owned event only after role, contract, input, Skill, prompt, and Frame construction preflight succeeds.

#### Scenario: One background context is refreshed
- **WHEN** a reader publishes a newer context entry for one background layer
- **THEN** the commit replaces that entry and preserves gameplay, decoration, and other background context entries

#### Scenario: Planner prompt construction fails
- **WHEN** a requested planner child fails Skill binding or prompt construction before child-start commit
- **THEN** task stage, child lineage, context registry, and provider-call count remain equal to their pre-attempt values

#### Scenario: Planner child starts successfully
- **WHEN** all planner child preflight checks succeed against the expected workflow checkpoint
- **THEN** its task-stage transition and child lineage become visible in one durable commit before the provider call begins
