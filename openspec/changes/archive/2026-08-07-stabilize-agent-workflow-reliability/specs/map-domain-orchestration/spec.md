## ADDED Requirements

### Requirement: Visible map planning is decided from structured task attributes
The runtime MUST classify a requested Map operation from normalized attributes including read-only versus mutation intent, explicit target, bounded operation count and extent, known resource/cell inputs, dependency on current Map facts, validation need, and approval need. It MUST NOT decide plan requirements by counting language-specific substrings.

#### Scenario: Explicit atomic map edit is requested
- **WHEN** the request resolves to one explicit target, one bounded mutation, known resource and cell inputs, and no read, planning, validation, or multi-scope dependency
- **THEN** runtime classification may permit delegation without a visible macro plan while retaining ordinary approval and revision guards

#### Scenario: Complex or ambiguous mutation is requested
- **WHEN** the request affects multiple cells or objects, depends on current Map facts, needs route or layout design, spans scopes, requires validation, or lacks enough attributes to prove atomicity
- **THEN** runtime classification requires `create_plan` before map-owner delegation

#### Scenario: LLM labels a complex task atomic
- **WHEN** model-proposed routing attributes conflict with deterministic operation facts or omit required fields
- **THEN** runtime validation rejects atomic classification and requires a plan without granting additional authority

#### Scenario: Non-mutating map request is submitted
- **WHEN** the user asks only to inspect, explain, or analyze Map state
- **THEN** classification does not manufacture edit intent or mutation authorization

### Requirement: Map orchestration is isolated behind a domain policy
Map routing, stage capabilities, persistent budgets, structured completion, validation, write guards, workflow continuation, and recovery MUST be implemented behind a small `MapTurnPolicy` adapter and cohesive subordinate Map-domain transition handlers. Frame lifecycle, structured completion, planning, delegation, tool arguments, tool guards, tool dispatch classification, budgets, and Map event projection MUST each have one explicit owner. Generic TurnDriver code MUST depend only on the domain-policy interface, execute the returned typed directives through declared ports, and MUST NOT inspect concrete Map tool names or Map-specific Session fields. `MapTurnPolicy` MUST NOT own a second complete generic model/tool loop.

#### Scenario: Generic turn driver dispatches a Map operation
- **WHEN** model output contains a Map-domain tool or completion decision
- **THEN** generic classification delegates the domain decision to MapTurnPolicy and applies the returned typed directive

#### Scenario: Domain rule leaks into generic core
- **WHEN** an architecture check finds a Map implementation import, Map tool literal, or Map-specific state branch in the turn core
- **THEN** the check fails and identifies the dependency violation

#### Scenario: Shared model behavior changes
- **WHEN** model, effort, provider, permission, or generic tool-protocol behavior changes
- **THEN** the shared turn core changes once without creating a separate complete Map pipeline

#### Scenario: One Map transition is evaluated
- **WHEN** a model response or committed tool result requires Map routing, planning, delegation, validation, completion, or recovery behavior
- **THEN** the policy selects the owning transition handler and returns a typed directive or domain result without hiding the effect inside a monolithic run method

#### Scenario: Map handler dependency direction is inspected
- **WHEN** architecture checks inspect the Map turn package
- **THEN** policy and execution adapters may depend on leaf handlers, leaf handlers do not import those adapters, and no old `map_turn_pipeline` compatibility module or re-export remains
