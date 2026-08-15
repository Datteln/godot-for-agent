## 1. Execution contracts and policy foundations

- [ ] 1.1 Define versioned request/result DTOs, error codes, timeout defaults, and per-role tool visibility for the unified CodeAct protocol.
- [ ] 1.2 Extend `app/security/paths.py` and settings to resolve logical project roots, allowed symlink targets, resolved worker roots, and repository roots without widening the write boundary.
- [ ] 1.3 Extend the permission engine and rules with allow/ask/deny policies for project-external access, network, high-risk commands, dependency installation, bulk writes, sensitive data, and Editor UI reload.
- [ ] 1.4 Add tests for path resolution, symlink escape rejection, repository-root read-only handling, request schema validation, and policy decisions.

## 2. Task-scoped isolated worker

- [ ] 2.1 Create the WSL2 rootless Docker worker image and launcher with non-root execution, no network, no credentials, no Docker socket, and configured CPU, memory, process, duration, and output limits.
- [ ] 2.2 Implement a `task_execution_id` worker lifecycle that reuses one container and task directory within a task but starts independent processes for each Shell/headless call.
- [ ] 2.3 Mount only the resolved allowed project root at `/workspace` and provision a task-specific volume over `/workspace/.godot`; prevent its cache files from appearing in task Git diff.
- [ ] 2.4 Implement cancellation, timeout, and final cleanup for worker processes, containers, task directories, and cache volumes.
- [ ] 2.5 Test worker isolation, denied mounts, no-network policy, cache isolation, resource limits, worker reuse, and cleanup after completion/cancellation/timeout.

## 3. CodeAct tool gateway and workspace safety

- [ ] 3.1 Implement the backend Execution Gateway that authenticates task/role scope, dispatches unified tool calls, records timeouts/cancellation, and returns typed results to the originating agent loop.
- [ ] 3.2 Consolidate `server_tools` file listing, reading, grep/search, artifact readers, skill loading, and tool discovery into `project.read/search`, `skill.load`, and `tool.search` while preserving artifact session/epoch/type/paging checks.
- [ ] 3.3 Implement worker-backed `project.edit`, `shell.run`, `godot.headless`, and read-only `git.status/diff`, including command-prefix restrictions and post-action diff collection.
- [ ] 3.4 Record task-start Git status and pre-existing diff summaries; implement first-touch content digests, pre-edit rechecks, `workspace_conflict`, and task-owned diff attribution.
- [ ] 3.5 Add a configurable per-action file-count/write-size guard and require small patch-oriented edits by default.
- [ ] 3.6 Test tool authorization, artifact URI constraints, worker-only execution, blocked destructive commands, conflict detection, and separation of existing versus task-created diffs.

## 4. Targeted CodeAct validation and map workflow migration

- [ ] 4.1 Implement a validation selector that runs matching tests/static checks for scripts, safe `PackedScene` load/instantiation for scenes, `ResourceLoader.load` plus type checks for resources, and reports typed unavailable results when no verifier can run.
- [ ] 4.2 Add worker-only temporary GDScript execution in the task directory, including script hashing, command/artifact capture, post-run diff collection, validation, and cleanup.
- [ ] 4.3 Adapt `app/orchestrator/map_validation.py` and `map_request_scope.py` to run after every map-affecting file, shell, or temporary-script action.
- [ ] 4.4 Persist map CodeAct validation reports, repair context, retry budget, execution id, diff artifact reference, and `failed_validation` state in the map workflow reducer.
- [ ] 4.5 Feed bounded map repair reports back to the same map owner, and on exhaustion/cancel/unrecoverable failure retain the current visible diff without auto-rollback or successful completion publication.
- [ ] 4.6 Retire/disable map Editor transaction and automatic rollback paths that conflict with worker file CodeAct behavior, with explicit migration coverage for existing state.
- [ ] 4.7 Test validation selection, repair retries, state recovery after compaction/restart, retained-diff failure semantics, and no rollback on terminal map validation failure.

## 5. Roles, coordinator, and legacy tool migration

- [ ] 5.1 Bind programming, map, scene, advisor, and coordinator roles to the unified protocol and enforce advisor/coordinator read-only defaults.
- [ ] 5.2 Update coordinator scheduling so only one write-capable agent runs per project while compatible read-only subtasks may run concurrently.
- [ ] 5.3 Migrate `front_tools` file-writing operations to `project.edit` or worker temporary-script/headless paths, reusing domain algorithms where applicable.
- [ ] 5.4 Remove or reject LLM access to arbitrary host Shell, Editor map/scene/resource write APIs, and arbitrary host Editor GDScript execution; add compatibility errors and migration notices for callers.
- [ ] 5.5 Test role capability matrices, serialized writers, permitted read-only parallelism, and rejection of legacy write/execution tools.

## 6. Editor observation RPC

- [ ] 6.1 Implement EditorPlugin registration and revocation with project id, instance id, short-lived backend token, liveness tracking, and a locally restricted IPC transport validated by a technical spike.
- [ ] 6.2 Implement Gateway-to-Plugin request routing with task/call ids, allowlisted method names, timeouts, cancellation, project matching, latest-instance selection, and late-result audit handling.
- [ ] 6.3 Implement `godot.editor.status`, conflict-safe approved `reload_for_validation`, viewport capture, runtime-state, debugger-error, and profiler-snapshot methods with artifact references.
- [ ] 6.4 Add Editor-open prechecks for `.tscn`, `.tres`, and other configured Editor-managed files so `project.edit` returns `editor_open_conflict` for clean and dirty open targets.
- [ ] 6.5 Update frontend surfaces to display Gateway logs, diff, artifacts, validation evidence, and reload approvals without executing or forwarding tool results.
- [ ] 6.6 Test token expiry/revocation, project mismatch, dirty/open-file conflicts, approval gating, busy/cancel/timeout outcomes, and the absence of frontend tool-result reinjection.

## 7. Audit, rollout, and acceptance verification

- [ ] 7.1 Implement the bounded CodeAct audit timeline keyed by task execution id, covering edit digests, temporary scripts, commands, worker results, Editor RPCs, approvals, validation, retries, artifacts, and final outcome.
- [ ] 7.2 Apply artifact size limits and sensitive-content filtering before persisting complete command output, scripts, and screenshots.
- [ ] 7.3 Add configuration defaults and operational diagnostics for map retry budget, action file limits, artifact retention, redaction, worker policy, and Editor RPC availability.
- [ ] 7.4 Add end-to-end acceptance tests for programming, scene, and map read-edit-verify loops; map terminal `failed_validation`; worker cleanup; cache separation; Editor opt-in observation; and task-owned Git diff.
- [ ] 7.5 Roll out behind feature flags, migrate eligible tool callers incrementally, monitor audit/validation outcomes, and document recovery for retained failed diffs.
