## Context

The compactor (`SessionCompactor`) only compacts `frame.messages` and never touches `session.map_task_state`, so structured map facts (revision, layer, `planning_attempts`, approved plans, pending batches) survive compaction — proven by the recorded session where the revision counter persisted across two compactions. However, two gaps cause the LLM to lose critical information despite surviving state: (1) the per-field `repair_plan` is stored only in tool-result messages and a truncated recovery text, while `failure_frontier` holds only `error_code`; (2) no map state is injected into the per-turn agent context (`app/prompt` has zero map-state references), so the LLM cannot see the survived state without calling a tool. The generic LLM summarizer runs at `thinking_budget=0` and is not map-aware, so it cannot reliably preserve exact map geometry.

## Goals / Non-Goals

**Goals:**
- Make the actionable failure details (`repair_plan`) and current map progress compaction-proof by persisting them in state and re-injecting them each turn.

**Non-Goals:**
- Changing `keep_recent`, the summarizer model, or the compaction threshold.
- Restructuring `map_task_state` field lifecycle metadata.
- Surfacing full `collision_facts`/cell data (only summaries + artifact re-read).

## Decisions

### Decision 1: Persist repair_plan in failure_frontier
Store the validator's `repair_plan`/`issue_details` in `failure_frontier` (scoped to target+revision), not just `error_code`. The recovery-guidance builder then reads `repair_plan` from state, so it can re-surface the actionable details even after the original tool-result message is compacted.

- **Why:** `failure_frontier` already survives compaction (it is in state); extending its content to carry `repair_plan` is the minimal change that makes the actionable failure details compaction-proof.
- **Alternative considered:** store `repair_plan` in `latest_validations`. Rejected — `failure_frontier` is already the field consulted by the write-gate and recovery guidance; putting `repair_plan` there keeps a single source of truth for "the current failure."

### Decision 2: Inject a map-progress digest each turn from state (do NOT fix the summarizer)
Re-derive a compact digest (revision, stage, latest failure `error_code` + `repair_plan`) from `map_task_state` and inject it into the per-turn agent context (in `app/prompt`). Rely on this digest, not on the LLM summarizer, to carry map-critical fields across compaction.

- **Why:** the summarizer is generic and runs at `thinking_budget=0`; making it map-aware is fragile and hard to test. A state-derived digest is deterministic, cheap, and compaction-proof by construction (re-derived every turn).
- **Alternative considered:** make the summarizer map-aware (preserve exact revision/anchor/repair fields). Rejected — generic-summarizer map-awareness is brittle; the digest subsumes its value for map-critical fields. The summarizer still summarizes the narrative history; the digest only guarantees the authoritative current map state is visible.

### Decision 3: Ease read_map_artifact re-read post-compaction
Auto-inject the persisted map-tool artifact reference (path + fingerprint) into the digest/recovery context when a map task has unread or stale map facts, so the LLM can re-read `map_artifacts.json` via `read_map_artifact` without tracking the fingerprint itself.

- **Why:** the artifact store already persists map tool results; the only barrier is the fingerprint gate, which the LLM loses post-compaction. Auto-injecting the reference closes the loop.
- **Alternative considered:** relax the fingerprint requirement entirely. Rejected — the fingerprint guards against stale-wrong reads; injecting the correct reference is safer than dropping the check.

## Risks / Trade-offs

- **[Risk] Digest bloats every agent turn's context.** → **Mitigation:** keep the digest compact (revision, stage, error_code, `repair_plan` truncated to ~6 items); inject only when a map task is active.
- **[Risk] `failure_frontier` grows with large repair_plans.** → **Mitigation:** cap the stored `repair_plan` to the first N issue_details, matching the existing recovery-text truncation.
- **[Risk] State-derived digest could disagree with the actual Godot revision if state is stale.** → **Mitigation:** the digest carries the state's recorded revision; the existing authoritative-revision checks at write time still catch drift before mutation.
- **[Trade-off] The summarizer may still garble narrative map history; the digest only guarantees the current state, not the full history.** Acceptable — the state is authoritative for "where the task is now."

## Migration Plan

- No data migration; `failure_frontier` gains an optional `repair_plan` sub-field. Old persisted state without it is treated as an empty repair plan (graceful degradation).
- Deploy is additive (new state sub-field + new context injection); rollback is `git revert`.

## Open Questions

- Should the digest also include the approved plan summary (for in-progress writes) or just failure state? Default: current stage + revision + failure; approved-plan summary can be a follow-up.
- Where in `app/prompt` to inject (system layer vs dynamic context)? Default: dynamic context (per-turn, cache-stable-prefix), so the system layer stays cache-stable.
