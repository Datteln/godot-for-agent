## ADDED Requirements

### Requirement: Every provider-emitted Thought has an eventual presentation source
For every thinking token or Thought content emitted by the model provider, the authoritative transcript SHALL retain sufficient complete entry state for a client to present it after live delivery, replay, or atomic snapshot hydration. The service MUST persist provider-emitted reasoning without filtering it by a user-visible flag, and MUST NOT fabricate content that the provider did not emit.

#### Scenario: Recovering provider reasoning whose live patch was missed
- **WHEN** a client misses one or more live patches for persisted provider-emitted Thought content
- **THEN** a later atomic history snapshot contains the same Thought entry with its latest persisted content, revision, state, token count, and terminal duration when complete
