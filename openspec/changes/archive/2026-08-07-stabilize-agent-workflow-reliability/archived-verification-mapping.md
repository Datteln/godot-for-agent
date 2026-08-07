# Archived Remediation Verification Mapping

Source: `openspec/changes/archive/2026-07-28-resolve-map-agent-remediation-backlog/tasks.md`.

This inventory maps every unchecked archived task to executable coverage in this change or to the explicitly named follow-up change `complete-archived-remediation-verification`. Carrying an item forward is not evidence of completion; task 13.8 must close it with recorded results or create that follow-up before this change finishes.

| Archived task | Current destination | Disposition |
|---|---|---|
| 2.5 invalid later result atomicity | 8.9, 13.3 | Current change |
| 2.6 metadata/reducer/persistence rollback and retry | 8.9, 13.3 | Current change |
| 2.11 Session artifact staging and retry matrix | 8.8, 8.9, 13.2, 13.3 | Current change |
| 3.9 scheduler DAG matrix | `complete-archived-remediation-verification` | Follow-up: dependency scheduler is not redesigned here |
| 4.7 Skill binding and legacy metadata matrix | `complete-archived-remediation-verification` | Follow-up: current clean cut does not retain legacy metadata migration |
| 4.10 reader/tool reachability contracts | `complete-archived-remediation-verification` | Follow-up |
| 5.7 reducer transition/replay/restart matrix | 6.10 | Current change |
| 6.9 worker provenance and evidence gate matrix | 7.9, 13.3 | Current change |
| 7.8 grouped Undo transaction integration | `complete-archived-remediation-verification` | Follow-up: Godot Undo transaction subsystem is not redesigned here |
| 8.7 deterministic platform traversal matrix | `complete-archived-remediation-verification` | Follow-up |
| 9.8 semantic retry/no-progress/recovery matrix | 7.9, 13.3 | Current change |
| 9.10 target inference and duplicate suppression | 11.6, 13.3 | Current change |
| 10.5 capability-contract parity | 2.8, 7.7 | Current change architecture checks |
| 10.6 prompt snapshot duplication guards | `complete-archived-remediation-verification` | Follow-up |
| 11.2 compile/type/OpenSpec/full pytest | 13.1 | Current change |
| 11.4 successful map E2E | 13.2 | Current change |
| 11.5 failure map E2E | 13.3 | Current change |
| 11.7 remediation/status evidence documents | 13.8 | Current change closure evidence |
| 11.8 mixed server/front artifact flow | 8.9, 13.2, 13.3 | Current change |
| 12.6 request-scoped map gate matrix | 7.9, 11.6, 13.3 | Current change |
| 13.4 malformed structured issue recovery | 4.8, 7.9 | Current change |
| 14.5 support-data self-healing matrix | `complete-archived-remediation-verification` | Follow-up |
| 15.7 liveness/fallback/resume E2E | 3.7, 9.9, 10.15, 13.3 | Current change |

## Follow-up boundary

The named follow-up contains only coverage for already implemented subsystems that this clean-cut change does not redesign: dependency scheduling, Skill binding, grouped Godot Undo, deterministic platform traversal, prompt snapshots, and support-data self-healing. It must not be used to defer any contract introduced by `stabilize-agent-workflow-reliability`.

## Reopened Canonical Timeline verification

Tasks 10.8, 10.9, and 10.15 through 10.27 were reopened after the earlier fragmented rendering implementation was found to retain parallel live/history/Reasoning/tool paths. They are closed only by the new canonical history tests, `test_chat_timeline.gd`, `test_chat_timeline_rendering.gd`, updated backlog/stream tests, syntax-aware architecture guards, the complete Python suite, and the complete headless Godot inventory recorded in `implementation-results.md`. No earlier `_append_message` or pseudo-event test is counted as completion evidence.
