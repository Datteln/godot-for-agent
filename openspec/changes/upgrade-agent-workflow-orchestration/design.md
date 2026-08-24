## Context

The service already has specialist Agent definitions, a coordinator that can call `create_plan`, and persisted `Frame`, `delegate_groups`, and `pending_plan` state. Today, however, `create_plan` is a presentation and prompting aid: after it returns, the coordinator model must call sequential `delegate_many`. `depends_on` is not used to select work, child results are returned as free-form summaries, and the active frame stack is not an authoritative graph of task ownership or dependency state.

The current transcript/WebSocket path already supports durable plans, approvals, tool activity, progress, errors, and final results. The existing permission engine already keeps mutating front tools behind confirmation. The design must build on both systems rather than add another chat display or authorization path.

## Goals / Non-Goals

**Goals:**

- Make complex, cross-domain work execute from a validated, durable dependency graph.
- Keep the coordinator responsible for routing and macro planning while assigning one owner Agent to each executable domain outcome.
- Pass bounded, named artifacts across dependencies and make blocked/failed outcomes visible and auditable.
- Preserve direct execution and legacy delegation for simple requests during rollout.
- Preserve confirmation-before-write and serialize write-capable work for one project.

**Non-Goals:**

- A free swarm in which specialists can recursively create unrestricted Agents.
- Replacing a domain Agent's internal read/plan/write/verify loop with global scheduler nodes.
- Automatic retry/replanning, cross-restart continuation, context compaction, CodeAct workers, or multi-project scheduling.
- Replacing the authoritative transcript, WebSocket transport, or permission engine.

## Decisions

### 1. Add a macro PlanGraph beside the existing frame stack

`PlanGraph` is persisted in the session and contains stable step IDs, domain owner, objective, dependencies, declared input bindings, bounded attempt count, lifecycle status, and owner publication. The existing `Frame` stack remains the execution-local LLM context and is not repurposed as graph storage.

This avoids a risky replacement of the current direct/delegation runtime and gives recovery, transcript projection, and tests one compact source of macro truth. A full frame-as-DAG redesign was rejected because a Frame is a conversational execution context, while one macro outcome can create several internal turns.

### 2. The scheduler, not the coordinator model, selects runnable work

After plan validation, the scheduler evaluates dependencies and starts only one eligible owner per scheduling pass. A step becomes runnable only after all predecessor publications are successful and its declared input bindings resolve. Failed, cancelled, blocked, or confirmation-waiting predecessors do not unlock downstream work.

The initial scheduler is serial for all work. This establishes deterministic behavior and simple compatibility with session locking. A later change may permit parallel read-only steps only after isolated result storage and concurrency semantics are proven.

### 3. One domain owner owns one macro outcome

The coordinator is the only Agent that creates a macro plan. A step owner is selected from the registered specialist Agents and receives an owner contract containing its identity, objective, input artifacts, and accepted result statuses. The owner may use its existing tools and internal workflow, but it cannot create arbitrary sibling owners or mutate graph dependencies.

This is preferred over free recursive delegation because ownership, permission attribution, and failure responsibility remain inspectable. It also lets a map owner remain responsible for map-specific internal stages without exposing them as generic graph nodes.

### 4. Dependency transfer uses immutable, bounded artifacts

Owners publish a typed result: owner/step identity, terminal or nonterminal status, summary, output artifacts, diagnostics, and next disposition. Each successor explicitly declares which predecessor artifact it consumes. The scheduler rejects missing, undeclared, malformed, or oversized bindings rather than passing a worker's whole message history.

Structured artifacts make dependencies testable and keep future compaction separate from correctness. A shared mutable blackboard was rejected because it obscures writer ownership and makes cross-Agent conflicts difficult to reproduce.

### 5. Confirmation suspends the owning step

When the owner reaches a mutating tool that needs confirmation, its macro step becomes `awaiting_confirmation`; the owner is retained as the only continuation authority. Approval resumes the same step; rejection yields a typed terminal rejected/cancelled publication. Downstream steps cannot start until the owner publishes the appropriate successful result.

This consumes the existing front-tool confirmation path rather than introducing scheduler-managed permission grants.

### 6. Project progress is projected through the existing transcript

The scheduler emits plan-created, step-runnable, step-started, awaiting-confirmation, succeeded, blocked, failed, and plan-completed events. The existing transcript writer maps them to typed plan/progress/status entries. The Godot client remains a renderer and command sender; it does not schedule or execute Agent work.

## Risks / Trade-offs

- [A model produces an invalid graph] → Validate owner names, unique IDs, acyclicity, dependencies, and bindings before persisting or starting a step.
- [A write waits for confirmation] → Suspend the same owner and block all dependents; do not create a duplicate owner on later user input.
- [Legacy and graph paths diverge] → Keep graph execution opt-in for plans and cover direct/delegate behavior with regression tests during migration.
- [Artifact payloads become oversized or contain unsafe data] → Enforce schema, size limits, project path validation, and artifact references rather than raw histories.
- [One failed precursor cascades confusingly] → Store the root failure once and project typed blocked outcomes for dependents.
- [Serial scheduling reduces apparent parallelism] → Accept this first; it yields predictable safety and can be relaxed only for read-only steps in a later change.

## Migration Plan

1. Introduce contracts, persistence, validation, and scheduler behind the existing complex-plan path without changing direct requests.
2. Have `create_plan` produce and validate a PlanGraph while retaining legacy `delegate_many` fallback until the graph path passes integration tests.
3. Route eligible complex plans through the scheduler and project their events through the existing transcript.
4. Retain persisted legacy plan data read compatibility; invalid/incomplete legacy plan state fails closed and does not start new work.
5. Roll back by disabling graph scheduling and continuing direct/legacy delegation. No graph step may issue a write after scheduler disablement unless its existing confirmation and owner continuation are still valid.

## Open Questions

- Which user-visible task classes should opt into PlanGraph first: all complex requests, only explicit multi-agent requests, or a narrow cross-domain allowlist?
- Should a declined confirmation publish `cancelled` or `rejected` as the canonical step status, and how should the final user summary word that outcome?
- What artifact size and retention bounds fit the current JSON session store before durable external artifact storage is needed?
