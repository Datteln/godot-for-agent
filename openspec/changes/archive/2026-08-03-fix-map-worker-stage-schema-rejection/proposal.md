## Why

The specialized map-worker result schema can be internally unsatisfiable: `specialized_map_worker_schema` adds a `const` for the `stage` field (pinned to the frame's stage) but leaves the base schema's static `enum` (which excludes `"orchestrator"`). For an orchestrator frame whose stage is `"orchestrator"`, the worker correctly outputs `stage: "orchestrator"` (matching both the `const` and the frozen frame contract), but the schema's `enum` check fails because `"orchestrator"` is not in the static enum. The result is a false `frame_contract` rejection on every orchestrator completion, conservatively repaired to `forced_validation_failure` + `disabled_completion`. In a recorded session, 5+ map-agent frames (f2/f6/f8/f9/f13) all hit this, thrashing ~40 minutes without ever completing — not because the worker was wrong, but because the specialized schema was unsatisfiable.

## What Changes

- **Reconcile `const` and `enum` in the specialized result schema.** `specialized_map_worker_schema` MUST NOT produce a field where a `const` value fails a co-existing `enum`. When a frozen frame constraint pins a field to a `const`, the specialization MUST drop or widen the static `enum` so the `const` value is admissible. This makes the specialized schema always satisfiable for any valid frame stage (including `"orchestrator"`).

## Capabilities

### New Capabilities

_(none)_

### Modified Capabilities

- `map-workflow-state-and-evidence`: the specialized result contract SHALL be internally satisfiable — a frozen `const` constraint SHALL NOT contradict a co-existing `enum`, so a worker that outputs the frozen frame values always passes schema validation regardless of the frame's stage.

## Impact

- **Python**: `app/orchestrator/map_contracts.py` (`specialized_map_worker_schema` — drop/replace the stage `enum` when the `const` is set, applied generally to any specialized field with a `const`).
- **No public API change**; orchestrator/map-worker completions that were falsely rejected now pass.
- **Out of scope**: changing the base `stage` enum's content; restructuring the schema; the worker correction-limit / thinking-budget items (covered by `relax-map-platform-plan-gates`).
