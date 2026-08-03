## Why

The map-task state machine (`session.map_task_state`) correctly survives compaction — the compactor only touches `frame.messages`, never state (proven by the recorded session where the revision counter persisted across two compactions). But two gaps mean the LLM still loses critical map information when the conversation compacts: (1) the per-field `repair_plan` (actionable failure details: `actual`/`required`/`action`) lives only in tool-result messages and a truncated recovery text, while `failure_frontier` state stores only `error_code` — once those messages scroll past `keep_recent=12`, the LLM is left with the error code but not how to fix it; (2) `app/prompt` injects no map state into the per-turn context, so the LLM does not see the survived state (current revision, stage, latest failure) unless it calls a tool or trips the write gate. The LLM summarizer is generic and runs at `thinking_budget=0`, so it cannot reliably preserve exact map geometry across compaction.

## What Changes

- **Persist the repair_plan in failure_frontier state.** `failure_frontier` SHALL store the validator's `repair_plan`/`issue_details` (not just `error_code`/`blocked_reason`), scoped to `(target, revision)`, so the actionable failure details survive compaction independently of the tool-result message.
- **Inject a compact map-progress digest into the agent context each turn.** The runtime SHALL re-derive a map-progress digest (current revision, stage, latest failure `error_code` + persisted `repair_plan`) from authoritative `map_task_state` and surface it to the agent every turn, so it survives compaction and does not depend on the LLM summarizer preserving tool-result history.
- **Ease `read_map_artifact` re-read post-compaction.** When a map task has unread or stale map facts, auto-inject the persisted map-tool artifact reference (path + fingerprint) into the digest/recovery context, so the LLM can re-read `map_artifacts.json` without tracking the fingerprint itself.

## Capabilities

### New Capabilities

_(none)_

### Modified Capabilities

- `map-workflow-state-and-evidence`: `failure_frontier` persists the structured `repair_plan` (not just the error code); a compact map-progress digest is surfaced to the agent context every turn (contextual continuation), re-derived from authoritative state so it is compaction-proof rather than dependent on the summarizer.

## Impact

- **Python**: `app/query/helpers.py` (write `repair_plan` into `failure_frontier` in the validation-result handler; recovery guidance reads it from state), `app/prompt/` context builder (inject the map-progress digest each turn), `app/orchestrator/map_progress.py` / `map_artifacts.py` (`failure_frontier` content; optional `read_map_artifact` reference injection). `app/query/compactor.py` needs no change — the digest is re-injected each turn, making summarizer map-awareness unnecessary.
- **No public API change**; the LLM sees more stable, authoritative map context across compaction.
- **Out of scope**: changing `keep_recent` or the summarizer model/threshold; restructuring `map_task_state` field lifecycle metadata; surfacing full collision_facts/cell data (only summaries + artifact re-read).
