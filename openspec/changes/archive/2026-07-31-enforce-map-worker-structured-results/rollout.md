## Provider capability verification

1. Keep `AI_AGENT_MAP_WORKER_RESPONSE_CONTRACT_MODE=prompt_only` for the
   compatibility baseline.
2. Run `tests/test_map_structured_results.py` against the target endpoint and
   confirm one complete `map_worker_result_v1` object passes both canonical
   Schema validation and frozen Frame validation.
3. Set the mode to `json_object`; verify the endpoint accepts
   `response_format={"type":"json_object"}` and that ordinary tool turns are
   unchanged.
4. Set the mode to `json_schema`; verify the endpoint accepts the specialized
   strict wire Schema. A 400/422 response-format rejection may perform one
   same-model downgrade. Malformed model content must not trigger a capability
   downgrade.
5. Observe safe diagnostics for correction success, local exhaustion,
   response mode, model, finish reason, raw length/digest, and semantic
   attempts. Normal logs and delegate artifacts must not contain raw rejected
   output.

Capability selection is explicit configuration. It is never inferred from the
model name.

## Staged rollout

- Start with `prompt_only`, correction enabled, limit `1`, and thinking budget
  `0`.
- Move a verified endpoint to `json_object`, then to `json_schema` only after
  its provider contract test passes.
- Compare correction success, local exhaustion, task pauses, latency, and
  provider downgrade counts before broadening the rollout.
- Keep task-level semantic retry thresholds unchanged during the first rollout
  so local formatting corrections and semantic convergence remain separable.

## Rollback

1. Set `AI_AGENT_MAP_WORKER_RESPONSE_CONTRACT_MODE=prompt_only` to disable
   native response formatting while retaining canonical prompt guidance and
   local validation.
2. Set `AI_AGENT_MAP_WORKER_STRUCTURED_OUTPUT_ENABLED=false` to disable
   same-Frame correction and return to fail-closed conservative repair.
3. If needed, set
   `AI_AGENT_MAP_WORKER_STRUCTURED_CORRECTION_LIMIT=0`.

Rollback does not remove the canonical local validator. Invalid results remain
completion-blocking.
