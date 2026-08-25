## ADDED Requirements

### Requirement: Every Thought token is published and observable end to end
For every thinking token or Thought content emitted by the model provider, the service SHALL publish an ordered transcript patch and make its entry ID, revision and event sequence available to the client. The client MUST record whether that patch was received, accepted by projection and routed for rendering; diagnostics MUST NOT record duplicate full Thought content.

#### Scenario: One Thought token is delivered and projected
- **WHEN** the service receives and publishes one newer Thought token from the model provider to a subscribed client
- **THEN** the client records matching received and projected watermark metadata and routes the current entry revision to the transcript viewport

#### Scenario: Thought token cannot be projected
- **WHEN** a delivered provider-emitted Thought token is rejected by projection or rendering
- **THEN** the client records a redacted typed failure containing event, entry, revision and sequence metadata and begins the applicable recovery path
