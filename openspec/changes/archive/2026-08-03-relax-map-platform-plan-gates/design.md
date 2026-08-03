## Context

The map-platform-plan validator (`validate_platform_level_plan` in `map_platform_plan_validator.gd`) and its Python orchestrator gates (`map_progress.py`) reject LLM-authored plans on a mix of objective correctness and subjective design quality, and impose a plan-specific hard revision cap. A recorded production session showed the LLM fix its one real mistake on the second attempt yet fail to ship: a parsing bug in `_entry_anchor_from_input` discarded its valid flat anchor (`entry_anchor_not_found`), and the two-revision cap then made the situation terminal. The same `X.get(key, {}) is Dictionary` idiom that caused the anchor discard recurs across several staleness guards in `map_tools.gd`.

The current specs (`platform-traversal-validation`, `map-progress-recovery`) already require objective correctness (collision facts, leap trajectory, segment geometry, ability) and semantic retry identity, but do not pin the advisory/blocking boundary, the flat-anchor acceptance, or the absent-field semantics. This change adds those requirements and implements them.

Post-implementation verification found three integration gaps in those decisions: the production `Settings` default still overrides the corrected `run_turn` correction default with `1`; mixed advisory/blocking score details report the first appended advisory as the top-level error; and the effective effort-tier thinking budget is sent to the provider but the raw configured `0` is persisted as evidence. It also found that platform-validation retry accounting is applied to every map plan tool even though only platform-plan failures are consumed by the planner exhaustion path. These are treated as completion gaps in this change rather than new intent.

## Goals / Non-Goals

**Goals:**
- Stop the validator layer from rejecting objectively valid plans: no hard count cap, subjective checks advisory only, flat anchors and field-presence parsed correctly.
- Pin the corrected behavior in spec so it is not silently re-introduced.

**Non-Goals:**
- Lightening the write-authorization gate's scope for non-jump writes (`map_write_stage_error`) — separate change.
- Consolidating the six `_append_*_protocol_errors` choreography appenders in `agent.py` — separate change.
- Changing the leap movement model, the segment-endpoint actor-cell convention (`y = platform.y - 1`), or the connectivity/A* algorithms (all remain).
- Any public API or persisted-state schema change.

## Decisions

### Decision 1: Anti-thrash is semantic-fingerprint-only; drop the hard count cap
Remove `MAP_PLATFORM_PLAN_MAX_ATTEMPTS` and the `attempts >= N` branch in `map_platform_plan_call_error`. Keep `planning_fingerprints` dedup (reject identical resubmissions, require concrete field changes). Distinct revised plans flow into the existing general no-progress pause.

The `validation_failure` semantic retry that drives planner exhaustion is recorded only for failed tools in `PLATFORM_PLAN_TOOL_NAMES`. Sibling plan-category tools (`plan_map_layout`, `plan_map_algorithms`) do not contribute to this platform-validation retry identity; they retain their own normal failure handling.

- **Why fingerprint-only:** the dedup already prevents "luck-hunting" (identical resubmission); the count cap adds only a false ceiling that turns recoverable situations (one real fix + one validator-side failure) into terminal ones.
- **Alternative considered:** raise the cap to N=5. Rejected — any magic number has the same failure mode on the (N+1)th distinct attempt; the principled bound is the semantic no-progress pause, not a per-plan count.

### Decision 2: Split `_score_level` into objective-blocking and subjective-advisory
Keep `_score_level` reporting all issue_details, but tag subjective issues (`platform_too_wide`, `challenge_roles_repeated`, `route_too_short`) as `advisory: true`. The blocked_reason chain consults objective issues only (reachability — also reported by `_build_jump_graph`/`_jump_edge`); subjective issues are carried in `repair_plan`/`issues` for the LLM and user but do not empty `edit_map_batches`.

When `_score_level.passed` is false, the top-level `error_code` is selected from the first non-advisory `issue_details` entry, not from `issue_details.front()`. Thus a preceding advisory cannot become the retry identity or persisted primary failure when a later objective traversal or safety issue (for example `finish_buffer_too_short`) is what actually blocks execution.

- **Why split, not delete:** the subjective checks still carry useful design guidance the LLM can act on; the problem is them being load-bearing for execution, not their existence.
- **Alternative considered:** delete the subjective checks entirely. Rejected — they are cheap signals the LLM benefits from seeing; demotion preserves the signal while removing the false block.

### Decision 3: Fix anchor/field parsing with `has(key)` + null-default guards
In `_entry_anchor_from_input`, guard `cell`-unwrapping with `raw.has("cell")` and consume a flat `{x,y,role}` dict directly. Across the staleness guards in `map_tools.gd` (coords, atlas signature, and the registry-lookup sites), replace the `X.get(key, {}) is Dictionary` gate with a `has(key)` check or a `null` default so absent keys are rejected, not silently treated as present-empty.

- **Why minimal guard fix:** the same file already uses the correct `null`-default idiom (e.g. `entry.get("coords", null) is Dictionary`), proving the `{}`-default form is a bug, not a convention. A mechanical, localized fix keeps blast radius small.
- **Alternative considered:** normalize all anchors to a canonical `{cell: {x,y}}` wrapper on entry. Rejected — pushes complexity onto every caller; accepting flat dicts is the lighter contract.

### Decision 4: Remove the fabricated manhattan repair hint
Drop `manhattan_path` and its use in `build_connectivity_repair_plan`; rely on the typed `required_unreachable_edges` list already produced by the jump graph to tell the LLM which transitions to fix.

- **Why remove:** the manhattan trace is not validated for reachability and can suggest impossible paths, misleading the LLM. The unreachable-edges list already conveys the actionable failure.
- **Alternative considered:** validate the manhattan path before suggesting. Rejected — the jump graph already enumerates the real unreachable edges; recomputing a path is redundant.

### Decision 5 (secondary/defensive): Raise the worker structured-output correction floor and final-turn thinking budget
Raise `map_worker_structured_correction_limit` default from 1 to at least 2, and set `map_worker_structured_thinking_budget` default to a non-zero value derived from the effort tier (reuse `resolve_thinking_budget`) instead of 0.

The production source of truth is `Settings.map_worker_structured_correction_limit`, because the engine explicitly passes it to every `run_turn` path. Its enabled minimum and default are therefore at least `2`; the `run_turn` signature and `_arm_map_reader_text_completion` helper use the same default so direct and production calls cannot silently diverge.

The final structured turn computes one `effective_structured_thinking_budget`: the explicit positive configured value, otherwise `resolve_thinking_budget(effort, selector)`. That exact value is passed to the provider and recorded on the frame, so persisted sessions and structured diagnostic events describe the budget actually used.

- **Scope note (reframe after log review):** log review of the recorded session showed the PRIMARY worker-output failure is NOT worker incompetence. The worker correctly output `stage: "orchestrator"` and was falsely rejected by a specialized-schema `const`/`enum` contradiction — addressed by the separate change `fix-map-worker-stage-schema-rejection`. This decision is therefore a **secondary defensive floor**: it only helps with genuine output malformation (e.g., the `normalized_required_arrays` repair action seen in the logs), not with the false stage rejection. The schema fix is the primary cure; A+B here is a defensive backstop.
- **Why (defensive):** even after the schema bug is fixed, a worker may still occasionally produce genuinely malformed structured output (missing/malformed required arrays). For those cases a one-shot correction at `thinking_budget = 0` is thin; raising the correction floor to ≥ 2 and giving the final turn a non-zero effort-tier thinking budget gives the model room to self-correct genuine malformation. The `map-progress-recovery` spec already permits a "configured bounded thinking policy" and "bounded correction" — this tunes the configured bounds and pins a floor.
- **Alternative considered:** drop A+B entirely and rely on the schema fix. Rejected — the schema fix removes false rejections but does not help genuine malformation; the defensive floor is cheap and independent.

### Decision 6: A successful non-platform-plan tool advances to write; drop the cross-scope stay-in-plan hold
`remember_map_plan_progress` previously guarded its success path with `if tool_name not in PLATFORM_PLAN_TOOL_NAMES:`, which called `transition_stage("plan")` to hold the whole map task in the plan stage whenever a sibling scope (same target + revision) carried a pending planner workflow (`next_stage == "planner"`, typically left by a prior failed platform plan). That hold is dropped: a successful `plan_map_layout` / `plan_map_algorithms` — the two `MAP_PLAN_TOOL_NAMES` tools not in `PLATFORM_PLAN_TOOL_NAMES` — now advances the task to `write` regardless of sibling pending planner workflows.

- **Why drop the hold:** it turned a recovered plan into more thrash — one scope's success was gated on another scope's pending re-plan, so a single stale sibling workflow kept re-locking the whole task to `plan` and starving it of write progress. A plan that passes objective validation should advance; sibling re-planning is bounded independently by the semantic no-progress pause (Decision 1) and the per-scope write gate.
- **Refactor side effect (now intentional):** inserting `record_semantic_retry` + `return retry_entry` into the failure branch stranded the old guard's body (`locked_scope = next(...)`) as unreachable dead code after the new return. The dead code is removed; the guard is not restored.
- **Alternative considered:** restore the guard and keep the hold. Rejected — it re-introduces the cross-scope thrash this change exists to remove, and the hold is redundant with the per-scope write gate and the semantic-retry pause.
- **Pinned by test:** a successful `plan_map_layout` with a sibling pending planner workflow transitions the task to `write`, not `plan`.

## Risks / Trade-offs

- **[Risk] Aesthetically poor levels may execute.** Demoting subjective checks lets through plans that previously failed on width/role-repetition. → **Mitigation:** issues remain visible to the LLM (repair_plan) and are advisory; playability (objective reachability) is unaffected. Acceptable: a playable-but-unstylish level beats no level.
- **[Risk] Longer loops without the count cap.** A stuck LLM could iterate more distinct plans. → **Mitigation:** fingerprint dedup prevents identical resubmission and the general no-progress pause still bounds total no-progress by threshold, so loops remain bounded — just not at 2. Net: bounded, not unbounded.
- **[Risk] Broad guard-idiom change.** The `is Dictionary`/`is Array` default-empty idiom recurs across `map_tools.gd`; a careless fix could change benign sites. → **Mitigation:** scope to validator/staleness guard sites identified in the audit (`_entry_anchor_from_input`, the coords/atlas staleness checker, the tile-signature helpers); add a regression test per fixed site that asserts absent-key rejection.
- **[Risk] Advisory ordering hides the blocking cause.** A mixed issue list can place advisory entries before the actual blocker. → **Mitigation:** derive the primary error from the first non-advisory detail and test an advisory-plus-`finish_buffer_too_short` result end to end.
- **[Risk] Runtime behavior and recorded evidence diverge.** A fallback budget can differ from its raw configuration value. → **Mitigation:** compute one effective budget and assert the provider call, frame state, persisted session, and diagnostic payload agree.
- **[Trade-off] `platform_transition_unreachable` is reported twice** (once by `_build_jump_graph` as `jump_graph_failed`, once by `_score_level`). Harmless redundancy; left as-is to avoid reordering the blocked_reason chain.

## Migration Plan

- No data migration; `planning_attempts` may remain tracked (observability) but the `>= N` cap check is removed. No persisted state depends on the cap value.
- Deploy is a code+test change; rollback is `git revert` (no schema/data conversion).
- After deploy, re-run the recorded session's plan (the 40-cell rightward extension) and confirm it ships on the second attempt once the anchor bug is fixed.

## Open Questions

- Does the existing general no-progress threshold need tuning now that plan resubmission is no longer capped at 2? Default: leave as-is, verify during implementation against the recorded session.
- Should advisory subjective issues surface in the user-facing chat panel, or only in the LLM-facing repair_plan? Default: same channel as repair_plan (LLM-facing); user sees the result, not the advisory nits.
