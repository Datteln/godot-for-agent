## ADDED Requirements

### Requirement: Every Thought token is immediately projected after recovery
The client SHALL ensure that every received or snapshot-recovered provider-emitted Thought token with a newer valid revision is immediately present in the canonical Store and offered to the viewport. The client MUST not batch, coalesce, skip, or defer a valid received Thought-token revision for visual work. If live projection cannot establish this condition, it MUST recover through the event-resume or snapshot path without surfacing a user-facing delivery-failure state.

#### Scenario: Snapshot repairs a dropped Thought stream
- **WHEN** history hydration returns a newer valid provider-emitted Thought revision than the Store holds
- **THEN** the Store replaces the stale Thought revision and the viewport receives the recovered entry without requiring a new model response
