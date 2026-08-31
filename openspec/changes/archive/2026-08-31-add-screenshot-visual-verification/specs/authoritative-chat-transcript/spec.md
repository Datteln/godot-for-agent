## ADDED Requirements

### Requirement: Transcript preserves terminal visual-observation outcomes
The authoritative visible transcript SHALL represent each persisted visual observation with its capture locator, source tool, terminal observation state, source-derived spatial facts, and bounded summary or reason. A pending visual-observation entry MUST be revised in place to a terminal state after completion, unavailability, failure, cancellation, reset, or timeout.

#### Scenario: Recovery after interrupted visual analysis
- **WHEN** history is requested after a screenshot-analysis turn was interrupted
- **THEN** the transcript returns the screenshot observation as `cancelled` or another terminal outcome and never as a permanently running verification

#### Scenario: Live observation completion
- **WHEN** visual analysis succeeds after its capture was already shown in the transcript
- **THEN** the service emits a higher revision of the same visual-observation entry with `observed` state and its bounded summary
