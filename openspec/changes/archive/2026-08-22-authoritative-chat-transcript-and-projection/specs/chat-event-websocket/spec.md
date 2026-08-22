## ADDED Requirements

### Requirement: User-visible WebSocket events carry transcript patches
For each user-visible chat change, the service SHALL publish an immutable WebSocket event whose payload contains an idempotent transcript patch with the target entry ID, revision, kind, state, ordinal, and typed payload. The client MUST apply a final assistant result only through this patch contract.

#### Scenario: Final assistant response
- **WHEN** the assistant completes a streamed response
- **THEN** the WebSocket sends a patch marking the existing assistant entry complete rather than a separate unkeyed final message

#### Scenario: Visible tool result
- **WHEN** a tool completes with success, rejection, or failure
- **THEN** the WebSocket sends a patch for the corresponding typed transcript entry with its resolved state

#### Scenario: Streaming and completing a Thought
- **WHEN** a user-visible Thought starts, receives content/token updates, and completes
- **THEN** the WebSocket sends revision-increasing patches for one `kind=thought` entry, whose complete patch contains the final content and `duration_seconds`

#### Scenario: Waiting for the original stream before recovering an empty final
- **WHEN** a reasoning token count reaches the configured thinking budget
- **THEN** the WebSocket continues to receive original-stream Thought, assistant, or tool patches until that stream ends; only a completed stream without assistant content or tool calls may be followed by one non-empty recovered assistant completion patch or one typed error patch
