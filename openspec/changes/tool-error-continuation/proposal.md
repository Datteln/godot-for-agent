## Why

A front-tool execution failure can currently be followed by a malformed tool-result request that FastAPI rejects with HTTP 422 before the result reaches the orchestration loop. The agent is then left with a pending tool call and cannot explain the failure, choose a safe fallback, or continue the user's request.

Tool failures are normal control-flow outcomes, especially where an operation is intentionally rejected by project or workflow safeguards. They must be reported to the LLM as structured evidence rather than terminating the request at the transport boundary.

## What Changes

- Normalize every frontend-confirmed tool outcome into a complete tool-result envelope before it is submitted, including when a local executor returns an error or an incomplete dictionary.
- Preserve the originating tool-call and frame identities when synthesizing a frontend protocol-error outcome, and log bounded diagnostics that make result-field loss diagnosable.
- Continue the pending agent turn after a valid `status: error` result, so the LLM can return a user-facing explanation, select an allowed fallback, or issue a new safe tool request.
- Add regression coverage for malformed frontend outcomes, error-result continuation, and pending-turn cleanup.

## Capabilities

### New Capabilities

- `front-tool-error-continuation`: Complete, protocol-safe frontend tool error reporting that resumes the agent decision loop instead of ending the request with a transport validation error.

### Modified Capabilities

- None.

## Impact

- Frontend confirmation and HTTP submission boundaries, frontend result DTO handling, and structured diagnostic logging.
- Service tool-result validation/continuation tests and agent prompts that guide safe recovery from tool failures.
- No expansion of tool permissions, path authority, approval scope, or automatic retries.
