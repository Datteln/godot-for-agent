## MODIFIED Requirements

### Requirement: Map workflow state changes only through events
The system MUST route task-epoch initialization and all owner identity, macro-step link, planning-context registry, child lineage, stage, blocker, checkpoint, operation, batch, validation, evidence, execution scope, revision, retry, transaction-reference, publication, approval, and no-progress changes through a single Map Workflow reducer. Every state field MUST declare machine-readable lifecycle metadata containing its task, context, operation, revision, or session scope, reset/default factory, and resume policy, and epoch initialization MUST be derived from that metadata.

#### Scenario: Agent requests a stage transition
- **WHEN** Agent orchestration submits a valid map workflow event
- **THEN** the reducer applies the transition and records the event without direct state-field assignment by the Agent

#### Scenario: QueryEngine requests a state update
- **WHEN** QueryEngine receives a map tool result
- **THEN** it submits an event instead of directly modifying MapTaskState fields or reducer-owned nested containers

#### Scenario: A distinct map task begins
- **WHEN** the runtime creates a new map task rather than resuming the current task lineage
- **THEN** one `task_epoch_started` event atomically resets every task-scoped field, including owner and macro links, planning-context entries and bundles, child lineage, automatic iterations, blockers, validations, evidence, execution scopes, revisions, layers, region reads, pending operations and batches, retries, transaction references, publications, approvals, and contextual task data

#### Scenario: A workflow field is added
- **WHEN** a new field is introduced without complete lifecycle metadata or its reset/resume behavior differs from that metadata
- **THEN** the exhaustive workflow-state check fails before the field can silently leak across task epochs

#### Scenario: Code bypasses reducer ownership
- **WHEN** a repository check finds a direct write to a reducer-owned scalar or nested container outside the reducer or the exact audited pre-construction hydration boundary
- **THEN** the check fails and identifies the bypassing location

## ADDED Requirements

### Requirement: Macro and map workflow states are separately persisted
The system MUST persist macro-plan state and map-domain workflow state as separate state machines linked by stable macro step, durable task, domain task, and owner Frame identities. Neither state machine SHALL infer the other's state from chat text or transient Frame order.

#### Scenario: Planner child completes
- **WHEN** the map reducer records a valid planner publication
- **THEN** the internal map stage advances while the linked macro step remains owned and non-terminal until an owner publication changes it

#### Scenario: Session reloads during approval wait
- **WHEN** persisted state is hydrated after restart while a map preview awaits confirmation
- **THEN** the macro step, owner identity, map checkpoint, approval identity, and child lineage are restored without creating a new owner

### Requirement: Map owner publications are reducer-owned evidence
Every map owner status publication MUST be produced from reducer-owned workflow facts and carry the linked macro step, owner, task, checkpoint, outputs or immutable batch references, applicable execution-scope revisions, and recovery disposition. A publication MUST NOT require one target or revision for a multi-scope outcome and MUST NOT claim completion beyond the current completion-gate evidence.

#### Scenario: Owner publishes awaiting confirmation
- **WHEN** the reducer contains a valid candidate, deterministic validation results, and an approval request for an immutable set of execution operations or batches
- **THEN** it can produce a matching `awaiting_confirmation` publication with recoverable artifact references

#### Scenario: Owner attempts premature completion
- **WHEN** writer or reviewer evidence for any required execution operation or resulting revision is absent
- **THEN** the reducer refuses a `completed` publication even if agent prose claims the map edit is finished

### Requirement: Approval resumes the recorded owner checkpoint
An approval or rejection MUST reference the persisted session epoch, durable map task, owner, checkpoint, candidate, and immutable operation or batch identities including their execution scopes. A valid decision SHALL transition the existing domain workflow and MUST NOT start a new macro step or sibling map owner.

#### Scenario: Matching approval is received
- **WHEN** the user approves the currently published candidate and its unchanged operation or batch identities
- **THEN** the reducer records the decision and authorizes the recorded owner to continue to the writer stage

#### Scenario: Stale approval is received
- **WHEN** an approval refers to an older candidate, operation fingerprint, execution-scope revision, owner, or session epoch
- **THEN** the reducer rejects it with a typed stale-approval result and performs no write

### Requirement: Planning contexts are independently reducer owned
The map workflow SHALL store planning-context entries under stable context identities and store planner bundles as ordered references to those entries. Each entry MUST declare its semantic role, provenance, digest, canonical target when applicable, layer or region, source revision, fact fields, freshness, and lifecycle metadata. Updating one entry MUST NOT replace unrelated contexts or make context equality a workflow invariant.

#### Scenario: Mid and Background contexts are recorded
- **WHEN** reader results publish gameplay and multiple background facts for one durable task
- **THEN** the reducer preserves each entry independently and can bind one planner bundle containing all required roles

#### Scenario: One context becomes stale
- **WHEN** only one entry's source scope advances to a newer revision
- **THEN** the reducer marks or replaces that entry without invalidating unrelated current contexts

#### Scenario: Compound storage key is hydrated
- **WHEN** a legacy record uses a key containing layer or revision decorations
- **THEN** hydration keeps the key as an index detail and requires a separately validated canonical target field rather than copying the decorated key into `target_path`

### Requirement: Specialist child start is one workflow event
Starting a specialist child MUST use the requested child's frozen worker stage to derive its Skill-binding stage. After side-effect-free preflight, one reducer event SHALL validate the expected workflow checkpoint, transition the persisted task stage, and append child lineage atomically. Lifecycle identity MUST be based on workflow, task, owner lineage, and child identity rather than a mandatory map target.

#### Scenario: Child preflight fails
- **WHEN** Skill binding, prompt construction, input validation, or Frame construction fails
- **THEN** no stage transition or child-lineage entry is committed

#### Scenario: Child start races with another checkpoint
- **WHEN** the expected task stage or checkpoint changes between preflight and commit
- **THEN** the reducer rejects the stale child-start event and orchestration retries from the current checkpoint without invoking the child provider

#### Scenario: Planner spans several targets
- **WHEN** one planner child binds contexts from different map targets or layers
- **THEN** its lifecycle event remains valid under the owner lineage without inventing one target to identify the child

### Requirement: Hydration repairs or blocks malformed owner contracts
Session hydration MUST detect a persisted map owner carrying a specialist worker result schema, worker identity, or worker-stage transitions. It SHALL repair the Frame only when the durable owner/task lineage is intact and recorded side-effect state proves that no specialist mutation occurred; otherwise it MUST record a backend-owned typed recovery problem and perform no provider call or map mutation.

#### Scenario: Recoverable malformed owner is loaded
- **WHEN** hydration finds `role=map_orchestrator`, `map_stage=orchestrator`, and `result_schema=map_worker_result_v1` with intact owner lineage and side-effect state `none`
- **THEN** migration removes worker-only fields, reconstructs the versioned owner contract, and resumes the same owner checkpoint

#### Scenario: Malformed owner has ambiguous state
- **WHEN** hydration cannot prove the owner lineage or whether a worker side effect occurred
- **THEN** the runtime preserves diagnostic evidence, blocks automatic mutation, and emits a backend routing recovery problem rather than silently deleting or replaying the task

#### Scenario: Valid worker child is loaded
- **WHEN** hydration finds a specialist child with a matching worker-stage contract, owner lineage, and result schema
- **THEN** it preserves that contract without converting the child into an owner Frame
