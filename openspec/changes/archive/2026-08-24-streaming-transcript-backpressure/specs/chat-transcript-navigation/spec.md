## ADDED Requirements

### Requirement: Live stream rendering has bounded frame work
The transcript navigation path SHALL batch live replaceable revisions so that a projection window performs at most one viewport update and one automatic follow-mode scroll request for a given set of changed entries. It MUST preserve final entry state, viewport anchor behavior, and return-to-latest semantics while avoiding one synchronous reflow per received stream packet.

#### Scenario: Long response while following the tail
- **WHEN** a long assistant response emits many realtime updates while follow mode is enabled
- **THEN** the viewport displays advancing recent content with bounded per-window rendering work and does not enqueue one independent bottom-scroll operation per update

#### Scenario: Reader is reviewing earlier content
- **WHEN** a long assistant response emits many realtime updates while follow mode is disabled
- **THEN** batched updates preserve the reader's existing anchor and expose the current return-to-latest affordance
