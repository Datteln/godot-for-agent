## 1. Persist the repair_plan in failure_frontier

- [x] 1.1 In `remember_map_plan_progress` (map_progress.py), stored `result.repair_plan`/`issues` into the scoped `failure_frontier` alongside `error_code`.
- [x] 1.2 Capped the stored `repair_plan` to the first 6 issue_details.
- [x] 1.3 `_append_platform_planning_failure_hint` (helpers.py) now reads `repair_plan` from `failure_frontier` state as a fallback when the tool-result message lacks it.
- [x] 1.4 Covered by `test_build_map_progress_digest_surfaces_failure_and_repair_plan` (failure_frontier.repair_plan surfaces via the per-turn digest, post-compaction).

## 2. Inject a map-progress digest each turn

- [x] 2.1 Added `build_map_progress_digest` (map_progress.py) deriving revision + latest failure `error_code` + `repair_plan` from `map_task_state`; appended to `dynamic_context` at both `ContextBuilder().build` call sites (engine.py:2117, :2187).
- [x] 2.2 Digest returns `""` when no map state (revision None + no failure_frontier), so non-map turns are unaffected.
- [x] 2.3 Digest is re-derived every turn from authoritative state (not message history), so it survives compaction.
- [x] 2.4 `test_build_map_progress_digest_*` verifies the digest carries revision + failure + repair_plan from state (incl. post-compaction).

## 3. Ease read_map_artifact re-read post-compaction

- [x] 3.1 `build_map_progress_digest` now injects `map_artifacts_ref=<relative_ref>` (the `map_artifacts.json` path) when a map task is active and `project_root` is passed; the main coordinator context (`engine.py:2117`) passes `security.project_root`. (Full fingerprint surfacing deferred — path lets the LLM locate the store post-compaction.)
- [x] 3.2 `test_build_map_progress_digest_surfaces_map_artifacts_ref` verifies the digest carries `map_artifacts_ref=` post-compaction.

## 4. Validation

- [x] 4.1 `openspec validate preserve-map-context-across-compaction` → valid.
- [x] 4.2 Ran the Python map-progress / structured-results suite → 162 passed.
- [x] 4.3 `openspec status` shows 4/4 artifacts complete.
