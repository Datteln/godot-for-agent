## MODIFIED Requirements

### Requirement: The service maintains an authoritative visible transcript
The service SHALL create and persist a versioned visible transcript for every new chat session. Each visible transcript entry MUST contain a stable `entry_id`, immutable display `ordinal`, typed `kind`, `state`, monotonic `revision`, and typed payload. The transcript MUST be the sole source for new-session history presentation. The service MUST maintain model conversation context independently from this visible transcript; compacting or excluding an entry from a later LLM request MUST NOT delete, alter, or omit the entry from transcript history.

#### Scenario: Persisting a visible workflow
- **WHEN** a user message, Thought, assistant answer, tool result, approval, task progress, verification result, or error becomes visible
- **THEN** the service records a typed transcript entry or revision before it can be returned in history or emitted as a visible live patch

#### Scenario: Model context compaction preserves visible history
- **WHEN** an older turn or completed tool group is consolidated out of model context
- **THEN** history responses continue to contain its user-visible transcript entries in their original order and revisions
