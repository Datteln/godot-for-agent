# CodeAct rollout and retained-diff recovery

CodeAct is the default execution path. `AI_AGENT_CODEACT_ENABLED=false` disables the Gateway. Legacy frontend execution cannot be re-enabled; retired calls are rejected with migration guidance. Editor observation remains opt-in through `AI_AGENT_CODEACT_EDITOR_RPC_ENABLED=true` and accepts only loopback Plugin registrations.

Configure `AI_AGENT_CODEACT_MAP_VALIDATOR_COMMAND` as a JSON argv list for the project-owned range, semantic, and target-region validator. The command runs inside the task worker and receives `--codeact-scope-json` and `--changed-paths-json`. An absent command produces a typed `unavailable` result; it is never treated as validation success.

During rollout, monitor the authenticated `/codeact/audit/{task_execution_id}` timeline for authorization denials, worker unavailability, validation status and retry counts, Editor RPC outcomes, artifact truncation, and final `failed_validation` outcomes. Live tool events include the execution id and audit reference. Compare task-owned diff evidence with the task-start baseline before accepting a result.

If a map task ends as `failed_validation`, the current project diff is intentionally retained. Review `diff_artifact`, `validation`, `repair_context`, and `recovery_disposition=retain_diff`. Continue the same task execution when a safe repair is possible, edit the files manually, or use an explicit Git operation outside CodeAct to discard the diff. Neither restart nor legacy transaction recovery automatically rolls project files back.

Rollback of the feature means disabling the Gateway or the optional Editor observation flag. Files already written by a worker remain visible and must still be reviewed explicitly. Legacy frontend execution cannot be re-enabled and is not a rollback mechanism.
