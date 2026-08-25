## Context

Front tools run in the Godot editor, while the service owns the pending turn and routes returned results to an agent frame. `ToolResult` requires a tool-call id, frame id, turn id, and terminal status. A malformed result is currently rejected by HTTP schema validation before the service can append it to the model conversation. The original call is still available in the frontend confirmation loop, so the frontend can recover the required identity without widening authority.

## Goals / Non-Goals

**Goals:**

- Ensure every selected front-tool execution produces a complete, serializable result envelope.
- Convert local executor failures and malformed executor outputs into typed error evidence for the originating call.
- Preserve the pending turn through an error result so the LLM can make a subsequent safe decision.
- Make protocol failures observable without logging unbounded tool payloads or file contents.

**Non-Goals:**

- Automatically retry a failed destructive or approval-gated operation.
- Recover missing tool identity server-side by guessing among pending calls.
- Relax schema validation, confirmation, path, or permission rules.
- Promise that an LLM retries; it decides whether to explain, inspect, or issue a safe alternative call.

## Decisions

### 1. Normalize at the frontend confirmation boundary

The confirmation loop has both the original front-tool call and the executor return value. It SHALL validate the return envelope immediately before decorating and appending it. If the envelope lacks a non-empty `tool_use_id`, `frame_id`, or an allowed terminal `status`, it SHALL replace the value with `AgentDTO.error_result(original_call, ..., "front_tool_result_protocol_invalid")` and retain `decision_source="execute"`.

This is preferred over a permissive service model because only the frontend can authoritatively associate the local execution with the selected call. A fallback result is an error result, never an inferred success.

### 2. Keep HTTP submission defensive but identity-preserving

The HTTP client SHALL receive only normalized result objects and assert the required envelope fields before serializing. If an invalid item still reaches this boundary, it SHALL return control to the chat panel with a local protocol-failure signal rather than transmit a request that will receive HTTP 422. The chat panel can reconstruct the error result from its pending-call snapshot.

This second boundary catches future call paths without trying to make the service guess identity. It keeps transport failure separate from an actual project-edit failure.

### 3. Treat a valid error as ordinary agent evidence

The service SHALL append a valid `status="error"` result as an error tool message, clear the matching pending call, and resume `run_turn`. Agent prompts SHALL tell agents to avoid repeating a rejected or unsupported operation blindly: explain the typed error, inspect when more facts are needed, or propose a permitted alternative.

Existing continuation behavior is retained; this change makes it reachable for all frontend failure outcomes.

### 4. Verify the failure path end-to-end

Tests SHALL cover a malformed local executor value becoming a complete error result, a valid error result reaching the service without a 422 response, and a stub LLM receiving the error tool message on the next agent loop. Tests shall also verify identity mismatches remain rejected rather than silently reassigned.

## Risks / Trade-offs

- [A normalization bug hides the raw executor failure] → Include bounded metadata such as present keys, tool name, call id, and protocol error code in frontend diagnostics and the synthesized error result.
- [The LLM repeats a failed action] → Include the typed error in the tool message and explicitly guide recovery prompts toward explanation, inspection, or an allowed alternative.
- [A client bypasses the confirmation loop] → Keep service-side strict validation and add the HTTP submission guard; do not weaken the API contract.
- [A partial multi-tool batch is malformed] → Return a per-call error envelope for the affected selected call and preserve all other complete results so the service can resolve the entire pending set deterministically.

## Migration Plan

1. Add the frontend normalization and HTTP submission guard with focused Godot tests.
2. Add service continuation tests using valid error envelopes and verify pending state clears only after all expected results arrive.
3. Update recovery prompt guidance and run an editor smoke test that forces a local edit failure.
4. Deploy frontend and service together; rollback is a source-control revert of both guards, without replaying any pending requests.

## Open Questions

- Whether the HTTP client should expose an explicit typed local callback or reuse the existing request-error signal for the submission guard.
