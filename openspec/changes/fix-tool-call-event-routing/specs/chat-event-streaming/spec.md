## ADDED Requirements

### Requirement: Display-only orchestration events never trigger execution
The client MUST treat `agent_tool_calls` and other orchestration or progress events as presentation-only. Only canonical `tool_calls` events carrying a non-empty `calls` array SHALL dispatch front-tool execution or tool-result submission. Event routing into the execution path MUST NOT depend on payload keys that display events lack.

#### Scenario: agent_tool_calls arrives
- **WHEN** the client accepts an `agent_tool_calls` event for a sub-agent frame
- **THEN** no front-tool execution, tool-result submission, synthetic error, or state transition occurs

#### Scenario: tool_calls with empty calls arrives
- **WHEN** the client accepts a `tool_calls` event whose `calls` array is empty
- **THEN** the client performs no execution and no submission, and chat state is unchanged

#### Scenario: tool_calls with front calls arrives
- **WHEN** the client accepts a `tool_calls` event with a non-empty `calls` array
- **THEN** the existing execution and result-submission pipeline processes the batch exactly as before
