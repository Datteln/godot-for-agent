## Why

The current coordinator can create a plan and delegate specialist work, but plan dependencies are informational and execution is driven by a model-selected sequential `delegate_many` call. Complex cross-domain requests can therefore run in the wrong order, cannot safely hand structured results to a dependent specialist, and provide no single authoritative view of which task is runnable, blocked, or awaiting confirmation.

This change upgrades the existing Agent workflow rather than introducing a free-form agent swarm. It makes cross-domain work predictable while preserving the current single-Agent path for simple requests.

## What Changes

- Add a durable, validated plan graph for complex requests, with stable steps, declared dependencies, owner Agents, and typed lifecycle outcomes.
- Add a deterministic scheduler that selects runnable steps from dependency state instead of asking the coordinator to sequence every delegation through `delegate_many`.
- Introduce domain-owned workflows: the coordinator owns routing and macro planning; each specialist Agent owns its internal tool workflow and publishes bounded structured results.
- Permit only the coordinator to create macro plans. Specialist Agents cannot create arbitrary sibling Agents or recursively expand an unbounded swarm.
- Carry named, structured result artifacts across dependencies; prohibit passing full worker conversation history as a dependency input.
- Preserve existing permission confirmation. A write-capable step remains blocked until confirmation is resolved, and write-capable steps for one project execute serially.
- Emit plan and step progress through the existing transcript/event path so the current Timeline remains the single user-visible display surface.
- Keep direct coordinator execution, single delegation, and existing `delegate_many` behavior available for simple or legacy requests during migration.

## Capabilities

### New Capabilities

- `dependency-aware-agent-plans`: Validated macro plans, deterministic dependency scheduling, lifecycle outcomes, and bounded failure propagation.
- `domain-owned-agent-workflows`: Coordinator-to-domain ownership boundary, structured owner publications, and controlled delegation authority.
- `agent-workflow-progress-projection`: Projecting durable plan and step progress into the existing chat transcript and event transport.

### Modified Capabilities

<!-- None. Existing chat transcript and WebSocket requirements remain unchanged; this change consumes their established event path. -->

## Impact

- Affects `ai_agent_service/app/orchestrator/agent.py`, Agent/Frame and session persistence models, tool registration for planning/delegation, and orchestration tests.
- Adds an orchestration scheduler and plan/owner contracts without replacing the current chat transcript, WebSocket, permission engine, or front-end confirmation flow.
- The Godot front end should render newly projected plan/progress entries using the existing transcript renderer and does not gain Agent execution authority.
