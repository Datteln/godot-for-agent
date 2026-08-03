## ADDED Requirements

### Requirement: Map planning skills consume an authoritative snapshot contract
A map planning skill that designs routes against existing map content MUST declare and receive a compatible authoritative snapshot input. Its instructions MUST distinguish snapshot-provided facts, planner-owned design fields, validator/compiler-owned write fields, and writer-owned transaction effects.

#### Scenario: Map-area expansion skill is bound
- **WHEN** a planner loads the map-area expansion skill for an existing map
- **THEN** the binding supplies a snapshot projection containing target, layer, revision, coverage, traversal profile, entry, boundary, frontier, and semantic resource references

#### Scenario: Required snapshot is absent
- **WHEN** a planning worker is created without a compatible snapshot artifact
- **THEN** worker construction or scheduling returns a typed missing-input outcome instead of asking the planner to infer exact facts from summaries

### Requirement: Planner scope cannot become atlas write authority
The effective planner contract MUST NOT authorize planner output as the source of naked per-operation atlas or GridMap item values. Exact resource facts SHALL remain available to the reader and validator/compiler scopes, and writer authority SHALL remain limited to approved compiled artifacts.

#### Scenario: Planner has context-read capability
- **WHEN** a planner can inspect a semantic resource or reference-cell fact for design purposes
- **THEN** that read does not authorize planner-authored raw atlas operations for writing

#### Scenario: Tool search discovers edit or resource-write tools
- **WHEN** the planning worker searches for globally registered write tools
- **THEN** binding reports them unavailable in planner scope and does not expand runtime authority

### Requirement: Planning skills request typed fact refreshes
When snapshot facts are incomplete or stale, the planning skill MUST return a typed refresh or recompute request that identifies the missing field and required coverage. It MUST NOT establish an untracked second fact baseline through ad hoc reads.

#### Scenario: Reachable frontier is missing
- **WHEN** the planner projection lacks a complete frontier required for connecting the route
- **THEN** the worker requests deterministic frontier recomputation for the snapshot target and revision

#### Scenario: Exact resource evidence is stale
- **WHEN** a semantic resource reference cannot be compiled against the snapshot
- **THEN** the worker requests reader/resource refresh and preserves the route candidate instead of guessing atlas coordinates
