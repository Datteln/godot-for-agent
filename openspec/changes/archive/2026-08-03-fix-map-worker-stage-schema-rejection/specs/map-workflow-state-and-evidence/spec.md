## ADDED Requirements

### Requirement: Specialized result schema is satisfiable
The runtime SHALL ensure that a specialized map-worker result schema is internally consistent: when a frozen Frame constraint pins a field to a `const`, the specialization SHALL remove or widen any co-existing `enum` so the `const` value is admissible. A worker that outputs the frozen Frame values SHALL pass schema validation regardless of the frame's stage, including the `orchestrator` stage.

#### Scenario: Orchestrator frame completes
- **WHEN** a map-agent orchestrator frame with `stage = "orchestrator"` produces a final result whose `stage` equals the frozen frame value
- **THEN** the specialized schema accepts the result and the runtime does not flag `stage` as a contract violation

#### Scenario: Const contradicts the base enum
- **WHEN** a frozen frame constraint sets a `const` whose value is not in the field's base `enum`
- **THEN** the specialization drops or widens the `enum` so the `const` is admissible, rather than producing an unsatisfiable field
