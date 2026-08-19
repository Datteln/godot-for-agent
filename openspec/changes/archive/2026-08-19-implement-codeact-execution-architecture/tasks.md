## 1. Execution contracts and policy foundations

- [x] 1.1 Define versioned request/result DTOs, error codes, timeout defaults, and per-role tool visibility for the unified CodeAct protocol.
- [x] 1.2 Extend `app/security/paths.py` and settings to resolve logical project roots, allowed symlink targets, resolved worker roots, and repository roots without widening the write boundary.
- [x] 1.3 Extend the permission engine and rules with allow/ask/deny policies for project-external access, network, high-risk commands, dependency installation, bulk writes, sensitive data, and Editor UI reload.
- [x] 1.4 Add tests for path resolution, symlink escape rejection, repository-root read-only handling, request schema validation, and policy decisions.

## 2. Task-scoped isolated worker

- [x] 2.1 Create the WSL2 rootless Docker worker image and launcher with non-root execution, no network, no credentials, no Docker socket, and configured CPU, memory, process, duration, and output limits.
- [x] 2.2 Implement a `task_execution_id` worker lifecycle that reuses one container and task directory within a task but starts independent processes for each Shell/headless call.
- [x] 2.3 Mount only the resolved allowed project root at `/workspace` and provision a task-specific volume over `/workspace/.godot`; prevent its cache files from appearing in task Git diff.
- [x] 2.4 Implement cancellation, timeout, and final cleanup for worker processes, containers, task directories, and cache volumes.
- [x] 2.5 Test worker isolation, denied mounts, no-network policy, cache isolation, resource limits, worker reuse, and cleanup after completion/cancellation/timeout.

## 3. CodeAct tool gateway and workspace safety

- [x] 3.1 Implement the backend Execution Gateway that authenticates task/role scope, dispatches unified tool calls, records timeouts/cancellation, and returns typed results to the originating agent loop.
- [x] 3.2 Consolidate `server_tools` file listing, reading, grep/search, artifact readers, skill loading, and tool discovery into `project.read/search`, `skill.load`, and `tool.search` while preserving artifact session/epoch/type/paging checks.
- [x] 3.3 Implement worker-backed `project.edit`, `shell.run`, `godot.headless`, and read-only `git.status/diff`, including command-prefix restrictions and post-action diff collection.
- [x] 3.4 Record task-start Git status and pre-existing diff summaries; implement first-touch content digests, pre-edit rechecks, `workspace_conflict`, and task-owned diff attribution.
- [x] 3.5 Add a configurable per-action file-count/write-size guard and require small patch-oriented edits by default.
- [x] 3.6 Test tool authorization, artifact URI constraints, worker-only execution, blocked destructive commands, conflict detection, and separation of existing versus task-created diffs.

## 4. Targeted CodeAct validation and map workflow migration

- [x] 4.1 Implement a validation selector that runs matching tests/static checks for scripts, safe `PackedScene` load/instantiation for scenes, `ResourceLoader.load` plus type checks for resources, and reports typed unavailable results when no verifier can run.
- [x] 4.2 Add worker-only temporary GDScript execution in the task directory, including script hashing, command/artifact capture, post-run diff collection, validation, and cleanup.
- [x] 4.3 Adapt `app/orchestrator/map_validation.py` and `map_request_scope.py` to run after every map-affecting file, shell, or temporary-script action.
- [x] 4.4 Persist map CodeAct validation reports, repair context, retry budget, execution id, diff artifact reference, and `failed_validation` state in the map workflow reducer.
- [x] 4.5 Feed bounded map repair reports back to the same map owner, and on exhaustion/cancel/unrecoverable failure retain the current visible diff without auto-rollback or successful completion publication.
- [x] 4.6 Retire/disable map Editor transaction and automatic rollback paths that conflict with worker file CodeAct behavior, with explicit migration coverage for existing state.
- [x] 4.7 Test validation selection, repair retries, state recovery after compaction/restart, retained-diff failure semantics, and no rollback on terminal map validation failure.

## 5. Roles, coordinator, and legacy tool migration

- [x] 5.1 Bind programming, map, scene, advisor, and coordinator roles to the unified protocol and enforce advisor/coordinator read-only defaults.
- [x] 5.2 Update coordinator scheduling so only one write-capable agent runs per project while compatible read-only subtasks may run concurrently.
- [x] 5.3 Migrate `front_tools` file-writing operations to `project.edit` or worker temporary-script/headless paths, reusing domain algorithms where applicable.
- [x] 5.4 Remove or reject LLM access to arbitrary host Shell, Editor map/scene/resource write APIs, and arbitrary host Editor GDScript execution; add compatibility errors and migration notices for callers.
- [x] 5.5 Test role capability matrices, serialized writers, permitted read-only parallelism, and rejection of legacy write/execution tools.

## 6. Editor observation RPC

- [x] 6.1 Implement EditorPlugin registration and revocation with project id, instance id, short-lived backend token, liveness tracking, and a locally restricted IPC transport validated by a technical spike.
- [x] 6.2 Implement Gateway-to-Plugin request routing with task/call ids, allowlisted method names, timeouts, cancellation, project matching, latest-instance selection, and late-result audit handling.
- [x] 6.3 Implement `godot.editor.status`, conflict-safe approved `reload_for_validation`, viewport capture, runtime-state, debugger-error, and profiler-snapshot methods with artifact references.
- [x] 6.4 Add Editor-open prechecks for `.tscn`, `.tres`, and other configured Editor-managed files so `project.edit` returns `editor_open_conflict` for clean and dirty open targets.
- [x] 6.5 Update frontend surfaces to display Gateway logs, diff, artifacts, validation evidence, and reload approvals without executing or forwarding tool results.
- [x] 6.6 Test token expiry/revocation, project mismatch, dirty/open-file conflicts, approval gating, busy/cancel/timeout outcomes, and the absence of frontend tool-result reinjection.

## 7. Audit, rollout, and acceptance verification

- [x] 7.1 Implement the bounded CodeAct audit timeline keyed by task execution id, covering edit digests, temporary scripts, commands, worker results, Editor RPCs, approvals, validation, retries, artifacts, and final outcome.
- [x] 7.2 Apply artifact size limits and sensitive-content filtering before persisting complete command output, scripts, and screenshots.
- [x] 7.3 Add configuration defaults and operational diagnostics for map retry budget, action file limits, artifact retention, redaction, worker policy, and Editor RPC availability.
- [x] 7.4 Add end-to-end acceptance tests for programming, scene, and map read-edit-verify loops; map terminal `failed_validation`; worker cleanup; cache separation; Editor opt-in observation; and task-owned Git diff.
- [x] 7.5 Roll out behind feature flags, migrate eligible tool callers incrementally, monitor audit/validation outcomes, and document recovery for retained failed diffs.

## 8. Review remediation and hard gates

- [x] 8.1 Derive task execution and per-call identities exclusively from trusted backend session/frame/call context; reject model identity overrides and restore the scene role projection.
- [x] 8.2 Connect CodeAct `ask` decisions to explicit trusted policy pre-approval and cover approved versus unapproved execution without accepting model-supplied approval fields.
- [x] 8.3 Replace whole-diff prefix subtraction with per-path task-start snapshots and cover pre-existing changes that sort before and after task-owned changes.
- [x] 8.4 Normalize `res://` paths before permission checks, Editor-open matching, host digests, and worker writes; cover clean and dirty open targets.
- [x] 8.5 Treat unavailable map validation as terminal failure, record passed validation as `validated`, and block final map success unless CodeAct execution is validated.
- [x] 8.6 Align all agent prompts with the unified tool protocol and remove the non-functional legacy frontend execution flag/path.
- [x] 8.7 Harden worker writable temporary locations and command policy, remove stale imports/config-only test drift, and run focused plus full regression suites.

## 9. Lifecycle, deferred observations, and audit closure

- [x] 9.1 Track task executions by session epoch and invoke idempotent Gateway cleanup on normal completion, terminal error, cancellation, timeout, session reset, map-task cancellation, and service shutdown; remove worker, cache, task directory, baseline, lock, owner, and active audit state.
- [x] 9.2 Register `godot.editor.*` as deferred tools in the effective scopes of the programming, scene, map, and advisor roles, preserving read-only authority and reload approval boundaries.
- [x] 9.3 Persist bounded, redacted audit timelines at task finalization, expose them through an authenticated read-only API, and attach execution/audit references to the live CodeAct result summary without relying on a dead event type.
- [x] 9.4 Reconcile CodeAct operations guidance and agent prompts with the actual worker/deferred protocol, and emit an explicit startup warning when the required map validator command is absent.
- [x] 9.5 Add lifecycle, deferred capability, audit persistence/API, event projection, prompt, and operational-diagnostic regression coverage; run focused and full backend/frontend validation.
