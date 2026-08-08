## 1. front_tools → package (lowest risk, first)

- [x] 1.1 Create `app/tools/front_tools/` package skeleton: `__init__.py`, `_shared.py`, and six domain modules `core_tools.py`, `program_tools.py`, `scene_tools.py`, `project_tools.py`, `resource_tools.py`, `map_tools.py`
- [x] 1.2 Move the module-level helpers (`_object_schema`, `_authoritative_snapshot_binding_properties`, `_worker_spec_schema`, the `register` wrapper) from `front_tools.py` into `_shared.py`
- [x] 1.3 Distribute the 85 `register(ToolDef(...))` blocks into the six domain modules as `register_<domain>_tools()` functions, grouped by each block's `domain=` field (core 3, program 13, scene 24, project 9, resource 8, map 28)
- [x] 1.4 Promote the ~100-line `placement_profile_properties` dict from a function-local in `register_front_tools` to a module constant in `_shared.py`; repoint its dependents (`place_map_objects`, `validate_object_placements`, `repair_placements`, `find_placement_anchors`) — all in `map_tools.py` — to import it from `_shared`
- [x] 1.5 Write the thin `register_front_tools()` coordinator in `__init__.py` that calls the six `register_<domain>_tools()` functions in order; re-export `register_front_tools`
- [x] 1.6 Delete the old `app/tools/front_tools.py`; verify there is no `front_tools.py`/`front_tools/` ambiguity
- [x] 1.7 Run the front_tools test suite (46 tests pass); confirm `from app.tools.front_tools import register_front_tools` resolves, `main.py` startup is intact, and zero caller changed

## 2. map_progress → 7 submodules + caller migration + arch check (spec-compliant, second)

- [x] 2.1 Measure logical-line counts of pre-existing `app/orchestrator/map_*.py` modules; **decision recorded: cover ALL `map_*.py` with ≤700 budget, no exemptions** (max pre-existing is `map_workflow.py` at 690)
- [x] 2.2 Create `app/orchestrator/map_state.py` (cluster A + `_minimal_pause_report`); ≤700 logical lines
- [x] 2.3 Create `app/orchestrator/map_validation.py` (cluster B); ≤700 logical lines
- [x] 2.4 Create `app/orchestrator/map_platform_planning.py` (cluster C planning lifecycle, without `remember_map_plan_progress`); ≤700 logical lines
- [x] 2.5 Create `app/orchestrator/map_plan_progress.py` **(deviation from design: cluster C three-way split)** holding `remember_map_plan_progress` + `_platform_edit_batches`/`_semantic_plan`/`_record_planning_publication` intact; ≤700 logical lines
- [x] 2.6 Create `app/orchestrator/map_write_authorization.py` (cluster C write gate + approval consumption); ≤700 logical lines
- [x] 2.7 Create `app/orchestrator/map_failure_guard.py` (cluster D) and `map_context.py` (cluster E + `_revision` + `active_planning_snapshot`); both ≤700 logical lines
- [x] 2.8 Run an import-graph check on the new modules; **resolved two cycles** (`map_validation ↔ map_failure_guard` via `_revision`→`map_context`; `map_platform_planning ↔ map_write_authorization` via `active_planning_snapshot`→`map_context`); confirmed acyclic with `map_state` as root
- [x] 2.9 Migrate the 16+ internal importers to direct submodule paths (tool_result_processor, turn_service, tool_evidence, map_result_projection, lifecycle, request_scope, response_policy, query/helpers, sessions/store, workflow/store, map_turn/*)
- [x] 2.10 Migrate the 12 `map_progress` test files plus benchmarks to direct submodule imports
- [x] 2.11 Delete `app/orchestrator/map_progress.py` (no facade, no re-export, no wildcard); update the `DIRECT_WRITE_HYDRATION_ALLOWLIST` path string to `map_state.py`
- [x] 2.12 Add architecture-boundary tests: ≤700 logical lines on ALL `app/orchestrator/map_*.py`, `map_progress.py` absent, no wildcard re-exports, acyclic imports with `map_state` as root
- [x] 2.13 Run the full `ai_agent_service` test suite (**648 passed + 43 subtests**) plus `map_workflow_dispatch.py` benchmark; green

## 3. query/helpers → 6 submodules + _text_utils + facade (last)

- [x] 3.1 Create `app/query/_text_utils.py` with the cross-cluster utilities (`_truncate_text`, `_truncate_oversized_message`, `_json_object`, `_count_lines`, `_parse_tool_call_arguments`, `logger`, oversized constants)
- [x] 3.2 Create `app/query/_map_derivation.py` **(deviation from design: shared derivation leaf)** holding the pure result-extractors, region derivation, region-read signatures, and map constants shared across tool_summary / map_session_state / map_deferral — this breaks three import cycles the design's 5-cluster cut would have created
- [x] 3.3 Create `app/query/message_utils.py` (cluster A: message/verify/prompt helpers + `_VERIFY_SYSTEM_PROMPT`)
- [x] 3.4 Create `app/query/tool_summary.py` (clusters B+F: `_map_result_summary`, `_front_tool_result_summary`, `_front_tool_summary`, `_display_tool_content`, `_compact_tool_summary`, `_history_payload_for_front_tool`)
- [x] 3.5 Create `app/query/map_session_state.py` (cluster C: session-scoped map state incl. `_remember_latest_map_revision`, `_remember_map_validation`, blocker *production* helpers)
- [x] 3.6 Create `app/query/map_deferral.py` (clusters D+E: defer/resume/schedule + completion-gate + blocker *consumption* helpers)
- [x] 3.7 Create `app/query/history_blocks.py` (cluster G: history block assembly + compact-snapshot helpers)
- [x] 3.8 In each new submodule replicate the `__all__ = [n for n in globals() if n.startswith("_") and not n.startswith("__") and n not in {"_MODEL_LOG_FIELDS"}]` comprehension so `from .sub import *` carries the underscore names
- [x] 3.9 Convert `app/query/helpers.py` into a thin re-export facade (`from .<sub> import *` for all six, then re-run the `__all__` comprehension); **verified the facade re-exports exactly the original 145-name underscore surface**
- [x] 3.10 Run the query/helpers test suite (`test_history_blocks`, `test_history_repairs`, `test_map_region_read_guard`, `test_map_workflow_hardening`, `test_runtime_hardening` → **68 passed**); confirm the 7+ importers and 5 test files are unchanged and green

## 4. Final verification

- [x] 4.1 Run the entire `ai_agent_service` test suite (**648 passed + 43 subtests**) plus the `map_workflow_dispatch.py` benchmark end-to-end; zero regressions across all three decompositions
- [x] 4.2 Confirm the `agent-runtime-maintainability` scenarios added by this change pass: `map_progress.py` absent, every decomposed `app/orchestrator/map_*.py` module ≤700 logical lines, no wildcard re-export or old-surface facade in the orchestrator, and acyclic imports with `map_state` as root — **all verified programmatically and by the new architecture tests**