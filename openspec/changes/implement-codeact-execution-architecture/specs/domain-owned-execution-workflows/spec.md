## MODIFIED Requirements

### Requirement: Domain owners control their internal workflows
A domain owner SHALL be the only component that creates, resumes, or transitions its specialist-internal workflow. The generic macro scheduler MUST NOT schedule an internal child stage directly or infer internal workflow state from natural-language objectives. For CodeAct work, the owner MUST select actions only from its role-scoped unified tool protocol, while the coordinator serializes write-capable owners for the same project.

#### Scenario: A map outcome becomes runnable
- **WHEN** all macro predecessors of a map outcome are satisfied
- **THEN** the macro scheduler invokes the map owner with the outcome contract and the map owner chooses its next allowed CodeAct action or typed internal stage

#### Scenario: Objective text mentions planning
- **WHEN** a macro objective contains words such as plan, read, write, or verify
- **THEN** those words do not grant a Frame an internal role, stage, capability, or tool

#### Scenario: Another owner is writing the same project
- **WHEN** a domain owner becomes runnable while a write-capable owner for that project is active
- **THEN** the scheduler keeps it pending without creating a concurrent write-capable action
