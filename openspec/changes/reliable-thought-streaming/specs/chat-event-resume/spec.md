## ADDED Requirements

### Requirement: Active Thought-token stalls trigger silent bounded recovery
While an active turn continues to publish a newer provider-emitted Thought-token watermark, the client SHALL detect when its accepted/projected watermark does not advance within a configured bounded interval. It MUST attempt cursor resume and, on an explicit gap or unrecoverable projection mismatch, atomic snapshot hydration. Generic heartbeat, tool, or other-session events MUST NOT by themselves prove that Thought tokens have progressed. The client MUST perform this recovery without displaying a user-facing recovery notice.

#### Scenario: Tool events continue while Thought is absent
- **WHEN** the service publishes a newer Thought token revision but the client continues receiving only unrelated transport progress
- **THEN** the client performs one bounded Thought-delivery recovery instead of indefinitely extending its wait solely from unrelated events

#### Scenario: Resume restores a stalled Thought
- **WHEN** reconnect replay supplies the missing contiguous Thought patch for the active turn
- **THEN** the client advances its Thought watermark, keeps the request active, and does not submit an interruption
