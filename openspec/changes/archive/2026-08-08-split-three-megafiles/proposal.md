## Why

Three runtime modules have grown to 2.7k–4.9k lines unchecked because the accepted `agent-runtime-maintainability` budget (≤700 logical lines per orchestration module) is only *enforced* inside `app/application/` and `app/orchestrator/map_turn/`. `map_progress.py` (2774 lines, an orchestration module by location) is already non-compliant with the spec text but escapes the net; `query/helpers.py` (4853 lines, 97 functions) and `front_tools.py` (4747 lines, a 4477-line single function holding 85 `ToolDef` data blocks) escape entirely. They are unmaintainable god modules and they block the enforceable-boundaries standard the repository already committed to. Decomposing them now is cheapest before they accrete more.

## What Changes

- **`map_progress.py` — spec-compliant decomposition (no facade).** Delete the monolith; split into 7 cohesive submodules each ≤700 logical lines: `map_state` (root: `MapTaskState` + types + lifecycle), `map_validation`, `map_platform_planning`, `map_plan_progress`, `map_write_authorization` (cluster C three-way split along the producer→consumer edge: planning records approvals, plan-progress publication recording stays isolated, write-stage consumes them), `map_failure_guard`, `map_context`. Migrate all 16+ internal importers and 12 test files to direct submodule imports — **no re-export facade, no wildcard imports**. Add an architecture check enforcing the ≤700-line budget on the new `app/orchestrator/map_*.py` modules.
- **`front_tools.py` — package, not facade.** Convert the 4477-line single function into a `front_tools/` package mirroring the existing `server_tools/` pattern: a real `register_front_tools()` coordinator that calls 6 per-domain registration modules (`core`, `program`, `scene`, `project`, `resource`, `map`) plus `_shared`. Promote the ~100-line shared `placement_profile_properties` dict from a function-local to a module constant in `_shared` (its 4 dependents are all `map`-domain, so they stay together). Callers (~12, all by the single name `register_front_tools`) are unchanged.
- **`query/helpers.py` — facade, non-orchestration.** Decompose the 97-function monolith into 5 submodules (`message_utils`, `tool_summary`, `map_session_state`, `map_deferral`, `history_blocks`) plus `_text_utils` and a shared derivation leaf `_map_derivation` (implementation-time deviation that breaks three import cycles); keep `query/helpers.py` as a thin re-export facade replicating the existing `__all__` underscore-export comprehension (self-propagating across the submodules) so the 7+ importers and 5 test files are unchanged. Not subject to the orchestration-module budget; the facade is an accepted transitional layer.
- **BREAKING (internal only):** import paths for `app.orchestrator.map_progress` symbols change to their submodule paths; 16+ internal modules and 12 test files must update imports. No external API, protocol, config, or persistence change.

## Capabilities

### New Capabilities

(none — pure structural decomposition; no new behavior is introduced)

### Modified Capabilities

- `agent-runtime-maintainability`: extend the "Replacement orchestration modules have enforceable cohesive boundaries" requirement so its ≤700-logical-line budget enforcement — currently scoped to `app/application/` and `app/orchestrator/map_turn/` — also covers the decomposed `app/orchestrator/map_*.py` submodules, via a new architecture check. `map_progress.py` is brought from 2774 unenforced lines into compliance.

## Impact

- **Code:** `app/orchestrator/map_progress.py` (deleted → 7 submodules); `app/tools/front_tools.py` (module → package); `app/query/helpers.py` (→ 5 submodules + `_text_utils` + `_map_derivation` + facade).
- **Callers (map_progress migration):** `tool_result_processor`, `turn_service`, `tool_evidence`, `map_result_projection`, `lifecycle`, `request_scope`, `response_policy`, `history`, `query/helpers`, `sessions/store`, `workflow/store`, `map_turn/{tool_cycle,tool_guards,tool_dispatch,delegation,model_cycle,budgets,delegation_group}` — all repoint `map_progress` imports at the new submodules.
- **Tests:** new architecture-boundary test enforcing ≤700 on `app/orchestrator/map_*.py`; the 12 `map_progress` test files update imports; the ~10 `front_tools` and ~5 `query/helpers` test files are unchanged. All 27+ affected tests must stay green.
- **Benchmarks:** `refactor_split_map.py` and `map_workflow_dispatch.py` (both import `map_progress`) update imports.
- **No external API, WebSocket protocol, config, persistence, or frontend change.**
