## ADDED Requirements

### Requirement: Map progress orchestration is decomposed within enforceable boundaries
The `map_progress` orchestration surface MUST be decomposed into cohesive submodules, each at most 700 logical lines, that own one declared reason to change and depend only on `map_state` and sibling leaf modules in the dependency direction. The decomposition MUST NOT introduce a forwarding facade, wildcard re-export, or old-module re-export that preserves the `app.orchestrator.map_progress` import path; `app/orchestrator/map_progress.py` MUST be absent. Architecture checks MUST enforce the at-most-700-logical-line budget on every `app/orchestrator/map_*.py` module produced by this decomposition, MUST fail when `map_progress.py` or any old-surface re-export returns, and MUST fail when the decomposed modules form an import cycle.

#### Scenario: Decomposed map progress modules are inspected
- **WHEN** release architecture checks inventory the `app/orchestrator/map_*.py` modules that replaced `map_progress.py`
- **THEN** each decomposed module measures at most 700 logical lines, `app/orchestrator/map_progress.py` is absent, and no module performs a wildcard import or re-exports the old module surface

#### Scenario: A map progress submodule exceeds its budget
- **WHEN** a decomposed `app/orchestrator/map_*.py` module exceeds 700 logical lines
- **THEN** architecture acceptance fails naming the file, its measured size, and the required design action

#### Scenario: Map progress submodule dependencies form a cycle
- **WHEN** architecture checks analyze imports among the decomposed `app/orchestrator/map_*.py` modules
- **THEN** no module imports a sibling in a direction that closes a cycle, and the dependency graph is acyclic with `map_state` as the root
