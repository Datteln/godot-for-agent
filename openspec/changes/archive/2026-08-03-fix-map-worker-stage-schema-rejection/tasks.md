## 1. Reconcile const and enum in specialized_map_worker_schema

- [x] 1.1 In `app/orchestrator/map_contracts.py::specialized_map_worker_schema`, when a frozen constraint sets a `const` on a field (`stage`, `worker`, `contract_id`, …), drop or replace the field's static `enum` so the `const` value is admissible.
- [x] 1.2 Specifically fix the `stage` field: when `const` is set to the frame's stage, ensure the `enum` no longer excludes it (drop the `enum`, or include the `const` value).
- [x] 1.3 Audit other specialized `const` fields for the same const/enum contradiction (e.g., `result_schema`, `worker`).

## 2. Tests

- [x] 2.1 Python test: an orchestrator frame (`stage = "orchestrator"`) whose worker outputs `stage: "orchestrator"` passes `validate_map_worker_schema` against the specialized schema (no `stage` error).
- [x] 2.2 Python test: a specialized field with a `const` not in the base `enum` is satisfiable (the `const` is admissible).
- [x] 2.3 Regression: replay the recorded session's map-agent completion and confirm no `forced_validation_failure` from a `stage` false-rejection.

## 3. Validation

- [x] 3.1 Run `openspec validate fix-map-worker-stage-schema-rejection` and fix any delta-spec lint errors.
- [x] 3.2 Run the Python map-contracts test suite.
- [x] 3.3 Confirm `openspec status --change fix-map-worker-stage-schema-rejection` shows all artifacts complete before `/opsx:apply`.
