## 1. Canonical Result Contract

- [x] 1.1 Define the versioned `map_worker_result_v1` JSON Schema in `map_contracts.py`, including all required top-level fields, nested validation fields, enums, and array/object types.
- [x] 1.2 Add a schema specialization helper that applies only known immutable Frame stage, worker, target, revision, and allowed-next-stage constraints without inventing unknown scope values.
- [x] 1.3 Replace the hand-maintained map-worker required-field set with validation derived from the canonical schema.
- [x] 1.4 Add unit tests for a valid result, every required-field class, nested validation types, stage enums, multi-layer values, and specialized `const`/`enum` constraints.

## 2. Runtime Contract Delivery

- [x] 2.1 Extend Frame/runtime-contract construction to carry the specialized schema identity and response-contract metadata without copying schema instructions into child task text.
- [x] 2.2 Refactor the map-reader text-completion arm so `force_text_only` uses the canonical schema renderer and a stage-correct minimal example.
- [x] 2.3 Ensure intermediate worker turns retain their tools and normal sampling policy while the final structured turn exposes no tools.
- [x] 2.4 Add tests proving tool turns do not receive the final-result response contract and the `force_text_only` turn does.

## 3. Provider Structured-Response Modes

- [x] 3.1 Add an optional typed response contract and per-call deterministic overrides to the LLM provider interface and update all production implementations and test doubles.
- [x] 3.2 Add explicit provider/model capability configuration for `json_schema`, `json_object`, and `prompt_only` modes without model-name inference.
- [x] 3.3 Implement strict `json_schema` request construction using the full specialized wire schema.
- [x] 3.4 Implement `json_object` and `prompt_only` request construction using the same specialized schema and compact runtime system guidance.
- [x] 3.5 Implement one narrow response-mode downgrade when a provider rejects the response-format feature before producing content, without replaying tool turns or changing Frame identity.
- [x] 3.6 Add provider contract tests for all three modes, unsupported-feature downgrade, and preservation of existing primary/fallback model behavior.

## 4. Deterministic Final Turn

- [x] 4.1 Apply temperature zero, the configured bounded thinking policy, and an empty tool set only to final structured map-worker calls.
- [x] 4.2 Verify per-call overrides do not mutate Agent definitions or leak into sibling Frames, later worker turns, or unrelated agents.
- [x] 4.3 Add concurrency tests covering parallel delegate workers with different response-contract modes and isolated final-turn settings.

## 5. Same-Frame Structured Correction

- [x] 5.1 Add frame-local structured-attempt state with backward-compatible defaults for persisted Frames.
- [x] 5.2 Change the finish path so an invalid result with remaining local budget keeps the child Frame instead of publishing a repaired artifact or popping the Frame.
- [x] 5.3 Build a safe corrective system message from schema version, stable error category, missing/invalid fields, and frozen Frame constraints without echoing raw model content.
- [x] 5.4 Reinvoke the same Frame as a deterministic text-only turn while preserving its prior tool results and gathered facts.
- [x] 5.5 Publish exactly one normal delegate artifact when correction succeeds, with diagnostics recording that a provisional result was corrected.
- [x] 5.6 Add scripted-provider tests for incomplete JSON followed by success, malformed JSON followed by success, and contract-invalid JSON followed by success, asserting that map reads are not repeated.

## 6. Exhaustion and Semantic Retry Accounting

- [x] 6.1 Apply conservative fail-closed repair only after the frame-local correction bound is exhausted, preserving `validation.passed=false` and completion blocking.
- [x] 6.2 Increment task-level semantic structured failure once per exhausted Frame rather than once per local corrective call.
- [x] 6.3 Ensure equivalent replacement workers share the same reducer-owned semantic retry identity and cannot reset convergence by starting at local attempt one.
- [x] 6.4 Route semantic exhaustion to a typed resumable pause containing the first root cause, scoped counts, last attempt, and recovery guidance.
- [x] 6.5 Add tests for local exhaustion, repeated replacement-worker exhaustion, changed error categories, distinct revisions, task-epoch reset, and independent parallel operations.

## 7. Validation and Safe Observability

- [x] 7.1 Run canonical local Schema and frozen Frame validation for every response mode, including provider-declared native schema success.
- [x] 7.2 Add structured diagnostics for schema version, response mode, model, deterministic overrides, finish reason, raw length/digest, parse offset where available, local attempt, and semantic attempt.
- [x] 7.3 Verify normal logs and delegate artifacts never contain full untrusted raw model output.
- [x] 7.4 Add regression tests confirming invalid provisional results cannot update facts, checkpoints, stages, blockers, validation, artifacts, or Completion Gate state.

## 8. Rollout and End-to-End Verification

- [x] 8.1 Add service configuration for the local correction bound, response-contract mode, and staged feature enablement with safe compatibility defaults.
- [x] 8.2 Add migration coverage for persisted Frames created before structured-attempt and response-contract metadata existed.
- [x] 8.3 Run the focused provider, orchestration, map recovery, delegate-group, persistence, and Completion Gate test suites.
- [x] 8.4 Reproduce the original incomplete-object and invalid-JSON log scenarios with a scripted provider and verify same-Frame correction succeeds or produces one typed exhausted pause without replacement-worker thrash.
- [x] 8.5 Document provider capability verification and rollout/rollback procedures, then validate the complete OpenSpec change.
