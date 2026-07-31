## Why

Map workers currently receive only the name of `map_worker_result_v1`, not its complete machine-readable contract. Their final response is generated as unconstrained streaming text at the inherited sampling temperature, so otherwise successful workers can return an incomplete object or malformed JSON; the runtime then discards the collected facts into a conservative partial result and the parent creates another worker instead of correcting the same frame.

## What Changes

- Establish one canonical `map_worker_result_v1` JSON Schema and derive required-field validation, per-frame stage/target/revision constraints, prompt summaries, and provider response contracts from it.
- Deliver the specialized schema to the LLM only for the final text-only worker turn: use native `json_schema` or `json_object` response formatting when configured for the model, and a schema-in-prompt fallback otherwise.
- Make the structured final turn deterministic and tool-free without changing the sampling policy used while the worker is gathering facts.
- On malformed or contract-invalid output, keep the same worker frame and its gathered facts, provide a safe validation diagnostic, and perform a bounded corrective retry before conservative repair.
- Separate frame-local formatting attempts from task-level semantic retry accounting so repeated replacement workers cannot evade the convergence budget.
- Preserve fail-closed repair and Completion Gate behavior after retry exhaustion, while adding typed pause/recovery outcomes and safe diagnostics for response-format mode and validation failures.

## Capabilities

### New Capabilities

<!-- None. This change strengthens existing map-worker contracts and recovery behavior. -->

### Modified Capabilities

- `map-workflow-state-and-evidence`: Require a single canonical result schema, per-frame schema specialization, delivery of that contract to the final worker turn, and validation against the same schema before consuming worker output.
- `map-progress-recovery`: Require bounded same-frame structured-output correction, provider capability fallback, separate local and semantic retry identities, and fail-closed exhaustion behavior.

## Impact

- Service orchestration: child Frame contracts, final-turn control, structured validation, artifact publication, retry accounting, pause/recovery reporting, and map-worker observability.
- LLM provider abstraction: optional structured-response contract, model capability selection, and deterministic final-turn overrides.
- Compatibility: no public HTTP or Godot tool contract break; providers without native structured output remain supported through prompt-only fallback and local validation.
- Tests: provider request construction, schema derivation, malformed-output correction, retry exhaustion, delegate-group isolation, and preservation of already collected map facts.
