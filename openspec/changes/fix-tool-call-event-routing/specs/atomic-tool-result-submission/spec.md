## ADDED Requirements

### Requirement: Empty tool-result batches are a silent no-op
The frontend MUST treat an empty front-tool result batch as a no-op: it SHALL NOT queue an HTTP `/chat` submission, SHALL NOT emit a synthetic error response, and SHALL NOT mutate chat state. Synthetic error responses carrying recovery dispositions MUST be reserved for genuinely malformed non-empty batches.

#### Scenario: Empty batch is suppressed silently
- **WHEN** the execution path finishes with zero results to submit
- **THEN** no HTTP request is queued, no synthetic error response is emitted, and no error presentation or state change occurs

#### Scenario: Malformed non-empty batch still reports
- **WHEN** a non-empty result batch fails required-field validation
- **THEN** the batch is preserved, the invalid entries are dropped with a warning, and a typed synthetic error response with recovery disposition is emitted as before
