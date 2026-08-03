## 1. Remove the platform-plan hard revision cap (via general no-progress pause)

- [x] 1.1 In `app/orchestrator/map_progress.py::map_platform_plan_call_error`, removed the `attempts >= MAP_PLATFORM_PLAN_MAX_ATTEMPTS` branch and the `MAP_PLATFORM_PLAN_MAX_ATTEMPTS = 2` constant; kept the `planning_fingerprints` dedup branch.
- [x] 1.2 Wired the general no-progress pause to the planner validate loop (option 3): `remember_map_plan_progress` records a `validation_failure` semantic retry on failure (scope-based operation, per-error-code streak, threshold `SEMANTIC_RETRY_MAX_ATTEMPTS`) and returns the retry entry; `engine.py` planner loop now force-completes on `retry["exhausted"]` instead of the plan-specific count; updated the give-up message.
- [x] 1.3 `planning_attempts` still incremented for observability; no code path rejects on count. (No existing test asserted the =2 cap; new-behavior test added in task 6.3.)

## 2. Demote subjective design-quality checks to advisory

- [x] 2.1 In `addons/ai_agent/tools/map_platform_plan_validator.gd::_score_level`, tag `platform_too_wide`, `challenge_roles_repeated`, and `route_too_short` issue_details with `advisory: true`.
- [x] 2.2 Compute `passed` from objective issues only (`has_blocking` = any non-advisory issue_detail); subjective issues no longer set `blocked_reason = "score_issues"` or empty `edit_map_batches`.
- [x] 2.3 Advisory issues still carried into `repair_plan`/`issues` (issue_details unchanged in shape).
- [x] 2.4 `platform_transition_unreachable` (objective) still blocks via jump_graph / score path (no advisory tag).

## 3. Fix entry-anchor and field-presence parsing

- [x] 3.1 In `_entry_anchor_from_input`, guard `cell`-unwrapping with `raw.has("cell")` so a flat `{x, y, role}` dict is consumed directly and not discarded as empty.
- [x] 3.2 In the `map_tools.gd` staleness checker, replaced `entry.get("coords", {}) is Dictionary` with `not entry.has("coords") or not (entry.get("coords") is Dictionary)` so an absent `coords` is rejected, not silently read as (0, 0).
- [x] 3.3 In `_entry_2d_tile_signature` / `_cell_2d_tile_signature`, fixed the `atlas_coords` default-empty guards to reject absent fields (`has("atlas_coords")`) instead of emitting bogus `atlas_x: -1` signatures.
- [x] 3.4 Audited remaining `X.get(key, {}) is Dictionary` gate sites in `map_tools.gd` (registry-lookup at ~1831/1839/1845); they are rescued by the downstream `_validate_resource_contract_shape` check, so no fix needed.

## 4. Remove the fabricated manhattan repair hint

- [x] 4.1 In `addons/ai_agent/tools/map_validator.gd`, removed `manhattan_path` and its call in `build_connectivity_repair_plan`; the repair plan now uses only the A*-validated `routed` path.
- [x] 4.2 `build_connectivity_repair_plan` still returns a non-empty typed repair plan: when A* finds no reachable path it returns a `connectivity_unreachable`/`replan` entry (empty cells, no fabricated path) instead of a manhattan trace.

## 5. Relax the worker structured-output gauntlet

- [x] 5.1 In `app/orchestrator/agent.py`, raised the direct `run_turn` `map_worker_structured_correction_limit` default from 1 to 2 and passed it through `arm_map_worker_structured_completion`. Post-implementation verification found that production engine paths explicitly override this with the still-`1` settings default; follow-up task 9.1 closes that integration gap.
- [x] 5.2 In `app/orchestrator/agent.py`, the final structured turn's `thinking_budget` now falls back to `resolve_thinking_budget(effort, …)` when `map_worker_structured_thinking_budget <= 0`, so the default 0 yields a non-zero effort-tier budget instead of zero.
- [x] 5.3 Python test (`test_correction_floor_two_allows_second_correction`): a worker whose first structured correction still fails gets a second correction attempt before fail-closed (correction floor = 2).
- [x] 5.4 Python test (`test_final_structured_turn_thinking_budget_falls_back_to_effort_tier`): with param=0 the final structured turn uses the effort-tier thinking budget (non-zero), not 0.

## 6. Tests

- [x] 6.1 GDScript test (`addons/ai_agent/tests/test_relax_validators.gd`, run via `godot --headless`): a flat `{x, y, role}` `entry_anchor` is accepted (not discarded).
- [x] 6.2 GDScript test (same file): an over-wide non-rest platform is flagged `advisory: true` and does not block `passed`.
- [x] 6.3 Python test: a distinct 3rd platform plan is accepted; an identical resubmission is rejected by fingerprint dedup.
- [x] 6.4 GDScript test (same file): absent `atlas_coords` returns `{}` (no bogus `atlas_x: -1`).
- [x] 6.5 Regression (`test_relax_validators.gd::_test_recorded_plan_replay_ships`, godot headless): the recorded 2nd-attempt plan (flat `entry_anchor` + corrected endpoint `y = platform.y - 1` + full ability) now passes `validate_platform_level_plan` (`executable=true`, no `entry_anchor_not_found`).

## 7. Validation

- [x] 7.1 Run `openspec validate relax-map-platform-plan-gates` → valid.
- [x] 7.2 Ran the Python map-progress / structured-results suite → 96 passed (GDScript suite N/A — no frontend harness).
- [x] 7.3 `openspec status` shows 4/4 artifacts complete.

## 8. Clean up the 1.2 refactor and pin the success-path advance

- [x] 8.1 In `app/orchestrator/map_progress.py::remember_map_plan_progress`, remove the stranded `locked_scope = next(...)` block (~1651-1664) left unreachable after `return retry_entry` by the 1.2 edit (Decision 6); do NOT restore the `if tool_name not in PLATFORM_PLAN_TOOL_NAMES:` success-path guard.
- [x] 8.2 Python test: a successful `plan_map_layout` (or `plan_map_algorithms`) call transitions the map task to `write` even when a sibling scope for the same target + revision has a workflow with `next_stage == "planner"` (the cross-scope stay-in-plan hold is gone).
- [x] 8.3 Re-run the Python map-progress / structured-results suite and `openspec validate relax-map-platform-plan-gates`.

## 9. Close post-implementation verification gaps

- [x] 9.1 In `app/config.py`, make the enabled production `map_worker_structured_correction_limit` default and minimum at least 2; align `_arm_map_reader_text_completion`'s local default, and verify all explicit engine call paths receive 2 without an environment override.
- [x] 9.2 Add a Python integration test that constructs production settings with no correction-limit override and proves a worker receives a second local correction attempt.
- [x] 9.3 In `map_platform_plan_validator.gd`, when score validation blocks, select the first non-advisory `issue_details` entry as the top-level `error_code` instead of using `front()`.
- [x] 9.4 Add a GDScript regression with an advisory issue plus `finish_buffer_too_short`; assert the plan blocks and reports `finish_buffer_too_short` as its primary error while retaining the advisory in `issue_details`/`repair_plan`.
- [x] 9.5 In `agent.py`, compute the final turn's effective structured thinking budget once, pass it to the provider, and store that same value in `frame.structured_thinking_budget` for persistence and diagnostics.
- [x] 9.6 Extend the Python thinking-budget regression to assert provider arguments, frame evidence, session serialization, and structured diagnostic payload all report the same effective non-zero fallback budget.
- [x] 9.7 In `remember_map_plan_progress`, record planner `validation_failure` semantic retries only for `PLATFORM_PLAN_TOOL_NAMES`; keep successful non-platform plan tools advancing to `write`.
- [x] 9.8 Add Python coverage proving failed `plan_map_layout` / `plan_map_algorithms` calls do not affect the platform-validation retry streak, while failed platform-plan validation calls still do and can exhaust it.
- [x] 9.9 Run the focused Python and GDScript suites, then run the full Python suite, the full headless GDScript suite, and `openspec validate relax-map-platform-plan-gates --strict`.
