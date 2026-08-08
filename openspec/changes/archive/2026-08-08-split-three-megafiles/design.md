## Context

Three runtime modules have accreted to 2.7k–4.9k lines because the accepted `agent-runtime-maintainability` ≤700-logical-line budget is only *enforced* by architecture checks inside `app/application/` and `app/orchestrator/map_turn/`. The target files escape:

- `app/orchestrator/map_progress.py` — 2774 lines, 51 functions + 4 classes. An orchestration module by location, so already non-compliant with the spec *text*; the check (`test_map_turn_handler_module_size_budgets`) globs only `app/orchestrator/map_turn/*.py`, so it slipped through.
- `app/tools/front_tools.py` — 4747 lines, a single 4477-line function `register_front_tools` that is a flat sequence of 85 `register(ToolDef(...))` data blocks (zero comments, zero nested defs, zero logic). Not an orchestration module.
- `app/query/helpers.py` — 4853 lines, 97 functions, 0 classes. A utility module with an unusual `__all__` that auto-exports every `_`-prefixed name, so callers import private symbols across module boundaries. Not an orchestration module.

Structural maps (function inventories, cohesion clusters, call edges, reverse-dependency surfaces) were produced by three parallel investigators and are summarized in the proposal. Key facts the design rests on: the three files form a directional (acyclic) coupling triangle (`query/helpers` imports 4 symbols from `map_progress`; `front_tools` and `query/helpers` share only a tool-name *vocabulary*, not imports); `front_tools` is pure data with a working precedent (`server_tools/` already split per-tool); `query/helpers` is spatially well-clustered (related functions already adjacent); `map_progress` decomposes into 5 cohesion clusters with an acyclic internal dependency graph.

## Goals / Non-Goals

**Goals:**
- Bring `map_progress.py` into compliance with the accepted `agent-runtime-maintainability` ≤700-logical-line budget, with an enforceable architecture check that prevents regression.
- Decompose `front_tools.py` and `query/helpers.py` into cohesive submodules without raising their caller churn.
- Land each file's decomposition independently, green tests, no behavior change.

**Non-Goals:**
- Internal refactoring of the giant functions themselves (`_front_tool_summary`'s 40-branch if/elif → dispatch table; `remember_map_plan_progress`'s success/failure tree → extracted helpers; `MapTaskState` field-enumeration → `MAP_TASK_FIELD_LIFECYCLE`-driven generation). These are valuable follow-ups but are independent of the structural split and out of scope here.
- Migrating `query/helpers` or `front_tools` callers off the facade/package entry. The facade is an accepted transitional layer; retirement is a later change.
- Any external API, protocol, config, or persistence change.

## Decisions

### D1. Hybrid policy per file (spec-compliant for map_progress, facade for the other two)

`map_progress.py` is the only one of the three that is an orchestration module under the accepted spec's intent, so only it is bound by the no-facade / ≤700 rule. `front_tools.py` (tool registry) and `query/helpers.py` (utility) are not orchestration modules, so the spec does not compel a no-facade treatment for them.

- **Alternatives considered:** (a) facade for all three — relaxes the accepted spec for `map_progress`, rejected; (b) spec-compliant for all three — migrates `query/helpers`'s 7+ callers and `front_tools`'s ~12 callers for no spec benefit, rejected.
- **Why hybrid:** applies the spec where it binds, avoids churn where it doesn't.

### D2. `front_tools` becomes a package with a real coordinator, not a re-export facade

The 4477-line `register_front_tools` is 85 pure-data `ToolDef` blocks with no logic. The natural shape is a `front_tools/` package whose `__init__` defines `register_front_tools()` as a **coordinator** that calls 6 per-domain registration modules (`core`, `program`, `scene`, `project`, `resource`, `map`) plus `_shared`. This is genuine *composition*, not forwarding — so it is spec-clean even hypothetically, and it mirrors the existing `server_tools/` package exactly.

- The one cross-tool coupling, the ~100-line `placement_profile_properties` dict, is promoted from a function-local to a module constant in `_shared`; its 4 dependents are all `map`-domain and land together in `map_tools.py`.
- Callers (`main.py` + ~11 tests) all import the single name `register_front_tools`; the package `__init__` re-exports that name, so **zero caller change**.
- **Alternatives:** (a) data-driven table + registration loop — rejected, because the `register()` wrapper does conditional field injection (`requires_map_revision`, `MAP_TARGET_REQUIRED_TOOL_NAMES`) on constructed `ToolDef` objects, so objects are still built; a table adds indirection without removing them; (b) per-tool classes — rejected, pure data has no behavior, class ceremony is waste.

### D3. `map_progress` decomposes into 7 submodules ≤700 logical lines, no facade, callers migrate

The 5 cohesion clusters (A state ~650, B validation ~250, C platform-planning+write ~1100, D failure-guard ~230, E context ~250) are each ≤700 except C. Cluster C is split **three ways** (not two) along the actual call edges, because (a) the ~1100-line cluster must fit the ≤700 budget and (b) the producer/consumer seam is crossed in both directions:

- `map_platform_planning` — the planning **lifecycle**: parse outcome, scope/fingerprint helpers, call gate, snapshot binding/evidence, attempt tracking.
- `map_plan_progress` — `remember_map_plan_progress` (393 lines) plus its exclusive helpers `_platform_edit_batches`, `_semantic_plan`, `_record_planning_publication`. Keeping the 393-line function **intact** (pure slicing) avoids a risky internal extraction; the three-way split is what makes each module ≤700.
- `map_write_authorization` — the write gate + approval consumption: `map_write_stage_error`, batch matching, `_platform_batch_fingerprint`, `_platform_approval_records`, `_looks_like_platform_route_write`, `platform_write_requires_validation`, `consume_committed_platform_approvals`.

Two shared helpers are relocated to leaves to keep the graph acyclic with `map_state` as the root:
- `active_planning_snapshot` → `map_context` (it calls `latest_map_revision`, which lives there; moving it to `map_state` would have made `map_state` import `map_context`, breaking the root invariant).
- `_revision` → `map_context` (pairs with `latest_map_revision` / `map_revision_scope_key`; this breaks the `map_validation ↔ map_failure_guard` cycle that `record_no_progress` ↔ `_revision` would otherwise create).
- `_minimal_pause_report` → `map_state` (it is called by `MapTaskState` itself, so it must sit below the state machine, not in `map_failure_guard`).

**Verified at implementation time:** the call-edge map showed `map_platform_planning ↔ map_write_authorization` (planning used `_platform_batch_fingerprint`/`_platform_edit_batches`/`_record_planning_publication`/`_semantic_plan`; write used `active_planning_snapshot`) and `map_validation ↔ map_failure_guard` — both resolved by the relocations above. The final graph is acyclic: `map_state` (root) ← `map_context` ← `{map_validation, map_failure_guard, map_platform_planning, map_plan_progress, map_write_authorization}`, plus `map_platform_planning` ← `map_plan_progress` and `map_write_authorization` ← `map_plan_progress`.

- **No facade, no wildcard re-exports** — `map_progress.py` is deleted; all importers repoint to the new submodule paths.
- Source-line cluster estimates overstate the budget: budgets are in *logical* lines (excluding blank/comment lines); all seven modules measured 197–583 logical lines at split time.
- **Alternatives:** keep C as one ~1100 module — violates ≤700, rejected; a two-way split with an internal extraction of `remember_map_plan_progress`'s approval tail — would preserve 6 modules but requires a ~13-parameter extraction that mutates `state`/`result`, a behavior risk the three-way mechanical split avoids.

### D4. `query/helpers` facade with self-propagating `__all__`

`query/helpers` is non-orchestration, so a re-export facade is spec-permitted. The 97 functions are spatially clustered into **5 submodules** (`message_utils`, `tool_summary`, `map_session_state`, `map_deferral`, `history_blocks`) plus `_text_utils`, **plus a shared derivation leaf `_map_derivation`** — a deviation discovered at implementation time. `query/helpers.py` becomes a thin facade.

- The existing `__all__ = [n for n in globals() if n.startswith("_") and not n.startswith("__") ...]` comprehension is **self-propagating**: each submodule re-runs the same comprehension over its own globals (so `from .sub import *` carries the submodule's underscore names), and the facade re-runs it again after importing all submodules — preserving the exact import surface with zero caller change. **Verified: the facade re-exports exactly the original 145-name underscore surface.**
- **Why `_map_derivation`:** the 5-cluster cut assumed tool_summary / map_session_state / map_deferral were separable, but the actual call edges showed three cycles: the result-extractors (`_map_revision_from_result`, `_map_layer_from_result`, `_map_target_from_result`, …) live in tool_summary yet are used by the state and deferral clusters; the region derivation (`_map_region_from_tool_args`, `_map_region_from_write_args`, …) and region-read signatures are shared by both state and deferral; the map constants (`_MAP_CONTEXT_MAX_*`, `_MAP_REGION_READ_GUARDED_TOOL_NAMES`, …) are used by all three. The fix: extract the **pure derivation core** (extractors + region derivation + signatures + shared constants — no state, no side effects) into a dedicated leaf `_map_derivation.py`. The resulting graph is acyclic: `_text_utils` and `_map_derivation` are leaves; `message_utils` and `tool_summary` depend only on leaves; `map_session_state` depends on `tool_summary` + leaves; `map_deferral` depends on `map_session_state`, `tool_summary`, `message_utils` + leaves; `history_blocks` depends on `tool_summary`, `message_utils` + leaves. Verified by import tests: no module cycle.
- The Cluster C/D blocker boundary is fuzzy (blocker *production* in `map_session_state` vs blocker *consumption* in `map_deferral`); the split places production (`_map_completion_blocker`, `_blocker_revision`, `_same_map_target`) in `map_session_state` and consumption (`_has_review_blocker`, `_review_required_blocker`, `_clear_validation_blockers`) in `map_deferral`.
- The two async `_schedule_*` functions stay together in `map_deferral`.
- **Naming:** the query-side map-state module is named `map_session_state.py` to avoid confusion with the orchestrator's `map_state.py` (different packages, but the collision reads as a bug).

### D5. Shared text utilities extracted to `_text_utils`

`_truncate_text`, `_truncate_oversized_message`, `_json_object`, `_count_lines`, `_parse_tool_call_arguments` are used across `query/helpers` clusters; placing them in one submodule avoids circular imports between `tool_summary`, `map_deferral`, and `history_blocks`.

## Risks / Trade-offs

- **[Cycle risk in the `map_progress` C-split]** → The producer→consumer direction is a hypothesis from structural inspection, not verified against every call edge. **Mitigation:** during implementation, run an import-graph check on the new `app/orchestrator/map_*.py` modules; if `map_write_authorization` calls back into `map_platform_planning`, either merge the offending pair or extract the shared helper into `map_state`/a leaf. The `test_turn_core_does_not_import_map_turn_package`-style check is the model.
- **[Missed `map_progress` importer → runtime ImportError]** → 16+ internal modules + 12 tests + 2 benchmarks import `map_progress` symbols. **Mitigation:** grep every `map_progress` import path, migrate, then run the full suite including `refactor_split_map.py` and `map_workflow_dispatch.py`.
- **[New arch check breaks existing oversize modules]** → `app/orchestrator/` already contains `map_workflow.py`, `map_artifacts.py`, etc.; a blanket `map_*.py` ≤700 check could red-flag pre-existing modules outside this change's scope. **Mitigation:** scope the new check to the decomposed `map_progress` submodules (enumerate the 6 new names, or check `app/orchestrator/map_*.py` excluding the pre-existing non-`map_progress`-derived modules) — see Open Questions.
- **[`query/helpers` facade hides true source from greps/IDE]** → callers still resolve symbols through `helpers.py`. **Trade-off:** accepted as transitional; the facade is cheap to keep and retirement is a tracked follow-up, not part of this change.
- **[Logical-line vs source-line accounting]** → budgets are logical lines; cluster sizes in the structural map are source lines and overstate. **Mitigation:** verify each new submodule's logical-line count (mirror the existing `test_*_module_size_budgets` counting: non-blank, non-`#`-prefixed) before declaring done.

## Migration Plan

Each file is an independent unit of work, landed in this order (lowest risk first, validates the pattern before the hard one):

1. **`front_tools` → package.** Create `front_tools/` package (6 domain modules + `_shared` + `__init__` coordinator), promote `placement_profile_properties` to `_shared`, delete the old `front_tools.py`. Run `front_tools` tests. *Zero caller change.*
2. **`map_progress` → 7 submodules + caller migration + arch check.** Extract clusters into submodules, split C along the producer→consumer edge, delete `map_progress.py`, migrate 16+ importers + 12 tests + 2 benchmarks to direct paths, add the `app/orchestrator/map_*.py` ≤700 check. Run full suite + benchmarks.
3. **`query/helpers` → 5 submodules + `_text_utils` + `_map_derivation` + facade.** Extract clusters, replicate `__all__` comprehension per submodule, build the facade, delete the monolith body (keep the facade). Run `query/helpers` tests. *Zero caller change.*

**Rollback:** each unit is independent; revert one without touching the others. `map_progress` rollback is heaviest (must also revert the caller migrations and remove the arch check).

## Open Questions

- **Exact C-split point.** Which functions land in `map_platform_planning` vs `map_write_authorization` is finalized by reading the actual call edges during implementation; the producer→consumer hypothesis is sound but unverified. (Resolved by the implementation task that runs the import-graph check.)
- **Scope of the new arch check.** Whether to enumerate the 6 new `map_progress` submodules by name, or check all `app/orchestrator/map_*.py` and fix/scope any pre-existing oversize modules (`map_workflow.py`, `map_artifacts.py`, `map_planning_snapshots.py`, …). To be settled when measuring their current logical-line counts; if any pre-existing module is already over 700, the check should either exempt it explicitly or the change should fold its fix in.
