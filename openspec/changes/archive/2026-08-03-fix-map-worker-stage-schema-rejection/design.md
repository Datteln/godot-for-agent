## Context

`specialized_map_worker_schema(frame)` ([map_contracts.py:139](../../app/orchestrator/map_contracts.py)) deep-copies the base `map_worker_result_v1` schema and pins certain fields (`stage`, `worker`, `contract_id`, …) to the frame's frozen values via `const`. The base schema's `stage` field has a static `enum: ["reader", "planner", "writer", "validator", "repairer", "reviewer"]` that does not include `"orchestrator"` (base schema [map_contracts.py:45-48](../../app/orchestrator/map_contracts.py)). When a frame's stage is `"orchestrator"` (the map-agent is the orchestrator child of the top coordinator), the specialization adds `const = "orchestrator"` ([:146](../../app/orchestrator/map_contracts.py)) but leaves the static `enum`, making the field unsatisfiable. `validate_map_worker_schema` checks both `const` and `enum` ([:295-299](../../app/orchestrator/map_contracts.py)), so the worker's correct `stage: "orchestrator"` fails the `enum` check and is conservatively repaired to a forced failure. A recorded session showed 5+ orchestrator frames (f2/f6/f8/f9/f13) all falsely rejected this way, thrashing ~40 minutes.

## Goals / Non-Goals

**Goals:**
- Make the specialized schema always satisfiable for any valid frame stage, so a worker that outputs the frozen frame values passes schema validation.

**Non-Goals:**
- Changing the base `stage` enum's content; restructuring the schema.
- The worker correction-limit / thinking-budget items — separate change `relax-map-platform-plan-gates`.

## Decisions

### Decision 1: Drop/replace the enum when a const is set during specialization
In `specialized_map_worker_schema`, when a frozen constraint pins a field to a `const`, remove the field's static `enum` (or replace it with one that includes the `const` value). The `const` is the tighter constraint; the `enum` is a base-schema guard that becomes contradictory when the `const` is a value outside the base enum (e.g., `"orchestrator"`).

- **Why:** the `const` already pins the exact value; the `enum` is redundant for the specialized field and harmful when it contradicts the `const`. Dropping it makes the field satisfiable with zero behavior change for values that were already in the `enum`.
- **Alternative considered:** add `"orchestrator"` to the base `stage` enum. Rejected — piecemeal (only fixes `stage`; other specialized `const` fields could hit the same contradiction); the general fix (drop `enum` when `const` is set) covers all fields.
- **Alternative considered:** make `validate_map_worker_schema` ignore `enum` when `const` is present. Rejected — the wire schema should be self-consistent for any consumer; fixing it at specialization keeps the validator simple and the serialized schema correct.

## Risks / Trade-offs

- **[Risk] Dropping the `enum` weakens base-schema guards for specialized fields.** → **Mitigation:** the `const` is stricter than the `enum` (pins exactly one value), so dropping the `enum` when `const` is set loses no validation strength; for fields without a `const`, the `enum` stays.
- **[Risk] A frame stage not in the base enum but also lacking a `const`.** → **Mitigation:** `const` is always set for `stage` during specialization ([:146](../../app/orchestrator/map_contracts.py)); if absent, the base `enum` still applies.

## Migration Plan

- No data migration; pure schema-construction change. Rollback is `git revert`.
- After deploy, replay the recorded session's orchestrator completion and confirm `stage: "orchestrator"` passes (no `forced_validation_failure`).

## Open Questions

- Should the same const/enum reconciliation apply to `next_stage` (which already gets a dynamic `enum` from `allowed_next_stages` at [:159-163](../../app/orchestrator/map_contracts.py))? Default: audit it, but `next_stage` already replaces the `enum` so it is consistent; only `stage` has the const + static-enum contradiction.
