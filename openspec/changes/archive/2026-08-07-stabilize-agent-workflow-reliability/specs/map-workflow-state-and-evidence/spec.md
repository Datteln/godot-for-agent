## MODIFIED Requirements

### Requirement: Map workflow state changes only through events
The system MUST route task-epoch initialization and all owner identity, macro-step link, planning-context registry, child lineage, stage, blocker, checkpoint, operation, batch, validation, evidence, execution scope, revision, retry, transaction-reference, publication, approval, and no-progress changes through one Map Workflow reducer. Every field MUST declare machine-readable lifecycle metadata, and no application use case, TurnDriver component, domain policy, or transport may directly assign reducer-owned state.

#### Scenario: Map policy requests a stage transition
- **WHEN** MapTurnPolicy submits a valid workflow event
- **THEN** the reducer applies the transition and records the event without direct state assignment by orchestration

#### Scenario: Tool-result use case requests a state update
- **WHEN** the tool-result submission use case receives a Map result
- **THEN** it submits an event instead of modifying MapTaskState or reducer-owned nested containers

#### Scenario: A distinct Map task begins
- **WHEN** the runtime creates a new Map task rather than resuming current lineage
- **THEN** one `task_epoch_started` event atomically resets every task-scoped field according to lifecycle metadata

#### Scenario: A workflow field is added
- **WHEN** a field lacks complete lifecycle metadata or runtime behavior differs from it
- **THEN** the exhaustive workflow-state check fails

#### Scenario: Code bypasses reducer ownership
- **WHEN** a repository check finds a direct write outside the reducer
- **THEN** the check fails and identifies the bypassing location

### Requirement: Persisted workflow state hydrates through a closed construction boundary
The runtime MUST accept only the current manifest-selected workflow schema, validate and normalize the complete snapshot-plus-events result, construct `MapTaskState` once, and publish it as live reducer-owned state. Hydration MUST NOT migrate an older schema or accept an already-live `MapTaskState`.

#### Scenario: Current state hydrates successfully
- **WHEN** manifest, snapshot, segments, schema epoch, lineage, sequence, and digests all validate
- **THEN** the complete state is constructed once and every later change is represented by a reducer event

#### Scenario: Unsupported persisted schema is loaded
- **WHEN** workflow data lacks the current manifest schema or uses the removed embedded representation
- **THEN** hydration returns `unsupported_session_schema`, performs no migration, and requires a new Session

#### Scenario: Current state round-trips
- **WHEN** a current workflow state is serialized and hydrated
- **THEN** its task, revision, Session, and lifecycle fields round-trip without omission

### Requirement: Planning contexts are independently reducer owned
The Map workflow SHALL store planning-context entries under stable identities and planner bundles as ordered references. Each entry MUST declare semantic role, provenance, digest, canonical target when applicable, layer or region, source revision, facts, freshness, and lifecycle metadata. Updating one entry MUST NOT replace unrelated contexts.

#### Scenario: Mid and Background contexts are recorded
- **WHEN** reader results publish gameplay and multiple background facts for one durable task
- **THEN** the reducer preserves each entry independently and binds a planner bundle containing required roles

#### Scenario: One context becomes stale
- **WHEN** only one entry's source scope advances
- **THEN** the reducer marks or replaces that entry without invalidating unrelated current contexts

#### Scenario: Current context key is loaded
- **WHEN** a current-schema entry is hydrated
- **THEN** its storage key remains an index detail and its separately validated canonical target supplies target identity

## ADDED Requirements

### Requirement: Workflow events are durably sequenced independently of memory retention
Every committed Map Workflow event MUST have a strictly increasing sequence within its Session epoch and lineage. Sequence allocation MUST use a persisted high-water mark and MUST NOT derive identity from an in-memory collection length.

#### Scenario: More than 512 events commit
- **WHEN** a workflow commits beyond the previous in-memory limit
- **THEN** every event remains durably addressable and no slicing removes replay history

#### Scenario: Process restarts
- **WHEN** the workflow is restored
- **THEN** the next sequence exceeds every committed sequence selected by the manifest

### Requirement: Workflow restart replays snapshot plus committed increments
Authoritative state MUST be reconstructable from one manifest-selected current-schema snapshot and every committed later event. Digests, lineage, schema, and sequence continuity MUST validate before publication.

#### Scenario: Restart follows several event segments
- **WHEN** a process loads snapshot N and committed segments N+1 through M
- **THEN** it replays them through the reducer and publishes only after validating the resulting digest

#### Scenario: A sequence gap or digest mismatch exists
- **WHEN** selected content is missing, duplicated, reordered, corrupt, or fails chaining
- **THEN** the runtime emits a typed recovery problem, preserves files, and prohibits mutation

#### Scenario: A segment was prepared but not committed
- **WHEN** restart finds a segment not referenced by the manifest
- **THEN** normal replay ignores it and coordinated recovery finishes or removes it

### Requirement: Workflow compaction is snapshot gated and crash safe
Compaction MUST create and verify a complete snapshot at the high-water mark, atomically switch the manifest, and only then remove covered segments. Event count and byte thresholds MUST be validated configuration.

#### Scenario: Compaction crashes before manifest switch
- **WHEN** a new snapshot is written but not selected
- **THEN** restart uses the previous snapshot and segments without losing events

#### Scenario: Compaction completes
- **WHEN** the new snapshot and manifest are durable and verified
- **THEN** covered segments may be garbage-collected without changing replayed state

### Requirement: Legacy workflow Sessions are unsupported
The runtime MUST recognize only the new persistent schema epoch and MUST NOT contain an embedded-state reader, baseline converter, dual-read comparison, dual writer, legacy diagnostic-tail projection, or rollback exporter.

#### Scenario: Legacy Session is loaded
- **WHEN** a Session contains the bounded embedded MapTaskState representation and no current manifest
- **THEN** the runtime returns `unsupported_session_schema`, performs no provider or mutation action, and offers creation of a new Session

#### Scenario: Release artifact is inspected
- **WHEN** persistence architecture checks scan the runtime
- **THEN** no legacy workflow reader, converter, dual-write branch, or old-format exporter exists

## REMOVED Requirements

### Requirement: Hydration repairs or blocks malformed owner contracts
**Reason**: The clean-cut runtime rejects all unsupported legacy Session schemas instead of repairing old owner contracts.

**Migration**: No runtime migration is provided. Create a new Session under the current schema; retain old files only as an unread backup if desired.
