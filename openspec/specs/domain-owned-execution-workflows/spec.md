# domain-owned-execution-workflows Specification

## Purpose

Define the boundary between coordinator macro plans and specialist-owned internal workflows, including durable domain ownership and typed owner outcomes.

## Requirements

### Requirement: Create-plan expresses macro outcomes only
The system MUST define `create_plan` steps as domain-owned outcomes with stable identity, domain owner, objective, acceptance contract, dependencies, declared predecessor bindings, and optional display milestones. A `create_plan` step MUST NOT construct or schedule a specialist-internal worker, stage, tool call, retry, or approval transition.

#### Scenario: Coordinator plans a map edit
- **WHEN** a user request requires reading, route planning, preview, approval, writing, and verification within one map task
- **THEN** the coordinator creates one executable map-domain outcome and may expose those internal phases only as non-executable display milestones

#### Scenario: Coordinator plans cross-domain work
- **WHEN** a request requires both code and map outcomes
- **THEN** the coordinator creates domain-owned macro steps and expresses only the artifact dependencies between their owner publications

#### Scenario: Internal worker specification is submitted
- **WHEN** a caller includes `worker_spec`, an internal map stage, or a tool instruction in a `create_plan` step
- **THEN** the runtime rejects the plan with a typed macro-plan contract violation and creates no worker Frame

### Requirement: Display milestones are never executable scheduler nodes
The system SHALL retain display milestones as presentation metadata associated with a macro step and MUST NOT assign them scheduler status, dependencies, attempts, Frames, or tool authority.

#### Scenario: UI renders map progress
- **WHEN** a map owner publishes progress for read, plan, preview, approval, write, or verify
- **THEN** the UI may update the matching display milestones while the scheduler continues to track one executable macro step

### Requirement: Every macro step has one durable domain owner
The macro scheduler MUST create or resume exactly one owner identity for a domain task, scoped by session epoch, durable task, domain, and domain task id. Retries, approval continuation, reconnect, and recovery MUST resume that owner lineage rather than create a sibling owner.

#### Scenario: Map preview waits for confirmation
- **WHEN** the map owner publishes `awaiting_confirmation` and the user later approves the preview
- **THEN** the scheduler resumes the same map owner and domain workflow at the approval-bound continuation

#### Scenario: Chat transport closes during domain work
- **WHEN** the response connection closes after the owner checkpoint is durable
- **THEN** recovery resolves the existing owner identity and does not submit the macro objective to a new sibling agent

### Requirement: Domain owners control their internal workflows
A domain owner SHALL be the only component that creates, resumes, or transitions its specialist-internal workflow. The generic macro scheduler MUST NOT schedule an internal child stage directly and MUST NOT infer internal workflow state from natural-language objectives. For CodeAct work, the owner MUST select actions only from its role-scoped unified tool protocol, while the coordinator serializes write-capable owners for the same project.

#### Scenario: A map outcome becomes runnable
- **WHEN** all macro predecessors of a map outcome are satisfied
- **THEN** the macro scheduler invokes the map owner with the outcome contract and the map owner chooses its next allowed CodeAct action or typed internal stage

#### Scenario: Objective text mentions planning
- **WHEN** a macro objective contains words such as plan, read, write, or verify
- **THEN** those words do not grant a Frame an internal role, stage, capability, or tool

#### Scenario: Another owner is writing the same project
- **WHEN** a domain owner becomes runnable while a write-capable owner for that project is active
- **THEN** the scheduler keeps it pending without creating a concurrent write-capable action

### Requirement: Owner publications govern macro completion
Each domain owner MUST publish a bounded typed result containing owner identity, domain task identity, macro step identity, status, produced outputs or artifact references, and recovery disposition. Internal child completion MUST NOT directly complete the macro step.

#### Scenario: Planner delivers a valid candidate
- **WHEN** a map planner child completes and the owner publishes `preview_ready` or `awaiting_confirmation`
- **THEN** the macro step remains non-terminal and dependent macro steps remain pending unless their contracts explicitly accept that publication status

#### Scenario: Domain outcome is complete
- **WHEN** the owner publishes `completed` with outputs satisfying the macro acceptance contract
- **THEN** the scheduler marks the macro step succeeded and may bind its declared outputs into successors

#### Scenario: Domain workflow cannot continue
- **WHEN** the owner publishes a terminal `blocked`, `cancelled`, or proven permanent failure result
- **THEN** the scheduler records that typed terminal result without reconstructing the internal failure from chat text

### Requirement: Owner contracts are distinct from worker-stage contracts
A domain owner Frame MUST carry an explicit owner contract containing its macro step, durable task, domain task, owner identity, lineage, and accepted publication statuses. Owner identity MUST NOT require one map target, layer, revision, specialist planning context, or execution scope. The owner MUST NOT receive a specialist worker result schema, worker instance identity, worker-stage transition set, or specialist input binding merely because its agent has domain-stage metadata.

#### Scenario: Map owner Frame is created
- **WHEN** the macro scheduler dispatches a map outcome to `map-agent` with `map_stage=orchestrator`
- **THEN** the Frame carries a domain-owner contract, has no `map_worker_result_v1` response contract, and is eligible to enter its first orchestration turn

#### Scenario: Non-empty owner contract is inspected
- **WHEN** Frame construction receives a valid non-empty domain-owner contract
- **THEN** contract non-emptiness alone does not create a worker identity, worker result schema, or worker next-stage set

#### Scenario: Owner creates a specialist child
- **WHEN** the owner selects the next internal reader or planner stage
- **THEN** that child receives its own worker-stage contract without replacing or widening the owner's contract

#### Scenario: One owner coordinates several map contexts
- **WHEN** one map outcome requires gameplay, multiple backgrounds, decoration, or reference regions
- **THEN** the same owner lineage coordinates those internal contexts without creating target-specific sibling owners

#### Scenario: Owner starts before a concrete map target is resolved
- **WHEN** the durable map objective and owner lineage are valid but reader work has not yet resolved canonical targets
- **THEN** owner creation and its first orchestration turn remain valid while mutation stays unavailable until concrete execution scopes are compiled