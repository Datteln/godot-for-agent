## ADDED Requirements

### Requirement: Thought-token delivery failures are observable without a notice
The transcript navigation path SHALL emit a redacted structured diagnostic when a provider-emitted Thought token is published but not received, accepted, projected or routed to the viewport within the configured delivery interval. It MUST identify the failure stage and available session, entry, revision and sequence metadata, and MUST NOT contain duplicate Thought text, prompts, tool results or secrets. It MUST NOT display a user-facing delivery or recovery notice.

#### Scenario: Renderer rejects one Thought token
- **WHEN** a valid newer provider-emitted Thought token is accepted by the Store but its renderer rejects the entry
- **THEN** the client records `thought-renderer-rejected` and silently attempts the applicable recovery
