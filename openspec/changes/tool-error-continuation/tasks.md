## 1. Normalize frontend tool results

- [ ] 1.1 Add a single result-envelope validator/normalizer at the confirmed front-tool execution boundary, using the original call to synthesize typed protocol errors for malformed executor output.
- [ ] 1.2 Preserve `decision_source`, session-allow intent, and bounded failure diagnostics without overwriting the normalized identity, status, or result fields.
- [ ] 1.3 Add a defensive HTTP submission guard that prevents a malformed `tool_results` payload from being serialized and routes the failure back to the call-aware confirmation layer.

## 2. Continue the agent turn after tool failure

- [ ] 2.1 Verify and, where needed, adjust service pending-result handling so complete `status="error"` outcomes are appended to the originating frame, clear pending state, and resume `run_turn`.
- [ ] 2.2 Update recovery guidance for agents so unsupported/rejected operations produce an explanation, inspection, or safe alternative rather than a blind repeat.
- [ ] 2.3 Retain strict rejection for mismatched call ids, frame ids, and turn ids; do not infer missing identity server-side.

## 3. Test and verify

- [ ] 3.1 Add frontend tests for malformed executor dictionaries and local execution failures, asserting a complete error envelope is sent for the original call.
- [ ] 3.2 Add service integration tests proving a complete error result continues to the next LLM decision loop, including a multi-call batch with an error outcome.
- [ ] 3.3 Add regression coverage that invalid identities remain rejected and cannot clear a pending call.
- [ ] 3.4 Run targeted frontend and service suites, then perform an editor smoke test that forces an approved edit to fail and confirms a user-facing LLM recovery response.
