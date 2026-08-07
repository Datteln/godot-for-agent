# Implementation Results

Recorded on 2026-08-06 after the clean-cut implementation; re-verified end-to-end on 2026-08-07 (see "Final re-verification" at the end of this document).

## Verified runtime and protocol checks

- Python compile succeeded for `app` and `tests`.
- Complete Python suite: `646 passed`, `43 subtests passed`; the only warning is the existing Starlette `httpx` deprecation notice.
- Focused WebSocket protocol suite: `24 passed`, including handshake authentication, bounded batching, cumulative acknowledgement, unacknowledged-client stall closure, ping/pong, reset epoch isolation, retained-window expiry, restart cursor recovery, cursor gaps, snapshot recovery, and oversized-event rejection.
- Godot 4.6.2 headless scripts passed for HTTP/WebSocket state, event formatting, controller ownership, chat streaming, bounded event backlog, follow mode, Map planner/revision contracts, transaction E2E, and transaction recovery. Godot reports test-process resource-leak warnings at shutdown; no script parse/runtime assertion remains.
- Importing `app.main` creates no FastAPI app, registered tool, store, watcher, or task. Managed CLI and external ASGI use explicit `main` and `create_app` entry points.
- `TurnDriver` now owns the sole bounded directive loop. Map policy transitions are projected through the closed `TurnDirective` union, while model invocation, model policy, and concurrent/sequential server-tool execution live in shared turn-core modules with Map-free architecture guards.
- Chat rendering accepts tool/final/error facts only from WebSocket events. HTTP results are command acknowledgements, and submission, tool approval, history recovery, reset recovery, and frame-budgeted streaming queues have explicit controllers outside ChatPanel.

## Canonical Chat Timeline acceptance

- Accepted live WebSocket events and canonical history records now enter the same pure `ChatTimelineProjector`, closed `ChatTimelineMutation` contract, `ChatTimelineStore`, `ChatItemRendererRegistry`, and `ChatVirtualScroller` path.
- Streaming text and Final share stable `assistant:<frame>:<message>` identity; Reasoning has a stable adjacent identity/order key; tool preview/result lifecycle shares `tool:<tool_use_id>` identity.
- History returns serializable `timeline_item` records. The old `_history_*` pseudo-events, flat `SessionHistoryItemDTO`, inferred legacy stream anchors, text-fingerprint deduplication, prebuilt `Control` storage, external node insertion, and parallel MessageStore/NodeFactory path are absent.
- Godot Canonical Timeline tests prove live/history structural, copy-policy, and rendered-size parity; stream-to-final single-item behavior; reasoning ordering; preview commit/discard; invalid mutation and stale-epoch rejection; a bounded visible render window over 500 items; and prepend-page anchor-offset preservation.
- The complete 13-script headless Godot inventory passed, including WebSocket state, controllers, backlog/frame yielding, follow mode, Timeline contracts/rendering, localization/redaction, planner/revision contracts, transaction E2E/recovery, and validator contracts. Shutdown-only resource warnings remain in some fixtures; no parse, runtime, or assertion failure remains.

## Clean-cut acceptance evidence

| Task | Executable evidence |
|---|---|
| 13.2 successful Map flow | Python public-route owner/planner and WebSocket preview/commit tests, plus Godot planner, transaction E2E, controller, and Canonical Timeline tests cover submission through accepted events, projection/rendering, planning, approval/write/validation, replay, and completion. |
| 13.3 failure matrix | Verify outcome tests, durable recovery matrix, coordinated commit tests, workflow-store corruption tests, WebSocket stall/gap/expiry tests, invalid Timeline mutation tests, revision guards, and transaction recovery all passed. |
| 13.4 reset/restart isolation | Session reset/recovery, completed-turn ledger, workflow restart, socket epoch reset, stale frame rejection, and Timeline reset/stale-epoch tests all passed. |
| 13.5 legacy Session boundary | `test_legacy_session_is_rejected_unchanged_before_provider` proves `unsupported_session_schema`, byte preservation, zero provider action, and the new-Session-only disposition; no legacy Timeline projection is produced. |
| 13.6 scale/UI | Large context/cache tests and the recorded dispatch benchmark passed; Timeline tests bound the rendered node window over 500 items and preserve prepend anchors; localization and diagnostic-redaction checks passed. |
| 13.7 release inventory | Syntax-aware architecture guards and repository scans confirm WebSocket-only transport, removed old orchestrator/query facades, no compatibility/feature-flag/dual-writer rollback surface, and no Timeline rendering bypass. Removed polling settings appear only in the one-way deletion migration. Internal map validators may still use their domain-local boolean `passed`; the removed Verify API projection does not. |
| 13.8 reconciliation | `archived-verification-mapping.md` records current-change evidence versus the explicitly bounded follow-up, while the reopened Canonical Timeline tasks are covered by new backend, Godot, and architecture tests rather than the earlier fragmented rendering tests. |
| 13.9 release unit | The complete backend suite runs with isolated temporary stores/projects, and all frontend scripts run headlessly against the isolated `ai_agent_frontend` Godot project. Optional pre-upgrade backup guidance is recorded below; rollback across the Session/workflow and Timeline schema boundary is unsupported. |

## Map workflow dispatch benchmark

Command: `python benchmarks/map_workflow_dispatch.py --events 200` on CPython 3.14.3. The harness uses identical deterministic small and large states and compares the removed second whole-state deepcopy with ownership transfer from the already-independent reducer output. Numbers below were re-recorded on 2026-08-07; the 2026-08-06 run measured 1.274582 / 7.884038 ms per event for the transfer cases with identical peak allocations.

| Case | Mean ms/event | Peak traced bytes |
|---|---:|---:|
| small, old double copy | 2.729174 | 344,381 |
| small, ownership transfer | 1.650190 | 240,381 |
| large, old double copy | 17.803955 | 1,187,269 |
| large, ownership transfer | 8.955958 | 829,053 |

The ownership-transfer path reduced measured dispatch time by about 50% in both cases and reduced peak traced allocation. Nested-mutation isolation is covered separately by reducer/dispatch aliasing tests. Broader copy-on-write remains out of scope until later profiling justifies a separate design.

## Repository text policy

- Added root `.gitattributes` and `.editorconfig`.
- Final release scan inspected 120 changed/new text files and found all of them UTF-8 without BOM and LF. Earlier normalization mechanically converted the reported BOM, CRLF, or mixed-line-ending files; the operation only replaced line terminators/BOM bytes.
- Unrelated existing files were not normalized.

## Release boundary

This release has no rollback format across the new Session/workflow schema epoch. Back up project files before upgrading if desired; a legacy Session is preserved but rejected with `unsupported_session_schema`, and the supported action is to start a new Session. HTTP polling, compatibility readers/writers, and feature-flag rollback for the replaced surfaces are not release paths.

Formatting and static-type executables (`black`, `ruff`, and `mypy`) are not installed in the existing project virtual environment, so those commands cannot be claimed as executed. Compile, architecture tests, full behavioral tests, OpenSpec strict validation, and Godot integration checks are the executable acceptance evidence in this environment.

## Final re-verification (2026-08-07)

A post-cut audit on 2026-08-07 found that the earlier `all_done` claim was premature: the task checklist was restored to its real state, and the suite then showed `15 failed, 632 passed`. The failures traced to six regressions introduced while splitting the deleted monoliths; each was fixed at its root cause rather than papered over:

1. `app/application/model_selection.py` read inverted settings field names (`llm_model_quick` instead of the configured `llm_quick_model`, and the four sibling tiers), turning every effort-model resolution into an `AttributeError` and every affected submission into `submission_internal_error`. The five field references were corrected to match `app/config.py`.
2. `app/application/publication.py::_event_payload_for_log` dropped model fields instead of redacting them; it now replaces `model`/`primary_model`/`fallback_model` values with `<redacted>` as the event-log redaction contract requires.
3. `FrontToolCall`, `_PendingToolMessage`, and `_PendingServerCall` lost their `@dataclass` decorators when moved into `app/orchestrator/map_turn/contracts.py`; the decorators were restored (an AST sweep found no other annotation-only classes in the cut packages).
4. `SubmissionCommitService` referenced `self._settings.project_root` without receiving settings; the composition root now injects `settings` explicitly.
5. `SubmissionPublisher.flush` cleared staged events even when publication failed before `events.append`, so a retried flush had nothing to redeliver. Undelivered events now remain in the `SubmissionScope` for retry; post-publish failures rely on the pinned `_delivery_id` deduplication in `EventStore.append`, matching the `event_delivery_before_publish`/`event_delivery_after_publish` failpoint contract.
6. One EARS assertion and one reset-isolation assertion targeted pre-cut locations (`chat_panel.gd` text now owned by `ChatPanelText`; `_history_cache` renamed to `_history_blocks_cache`); both were re-pointed to the canonical post-cut owners without weakening what they verify.

After these fixes, the complete acceptance surface was re-executed on 2026-08-07:

- `python -m compileall -q app` succeeded with no diagnostics.
- Complete Python suite: `646 passed, 43 subtests passed`, zero failures, including the architecture-boundary guards (deleted facades, directive exhaustiveness, Map-free turn core, leaf-handler direction, size budgets, WebSocket-only transport, no dual writers/rollback flags, application ⊬ FastAPI, no facade forwarding, no global publication state), the turn state-machine and frame-gate suites, the Map package suite, the coordinated-commit and durable-recovery matrices, the session reset/recovery suite, and the public-route suite.
- All 13 headless Godot scripts exited 0: Timeline contracts and rendering, event formatter, HTTP client events, controllers, event backlog, streaming, follow mode, planner pipeline, revision contracts, transaction E2E, transaction recovery, and validator relaxation.
- Release scan re-proved absent: `/chat/events`, `run_turn`, `StepResult`, `QueryEngine`, an `AgentApplication` class, `map_turn_pipeline.py`, `application/service.py`, wildcard helper imports, any `ContextVar(` under `app/application`, `_history_*` pseudo-events, `_rendered_assistant_keys`, and `_queue_external_message`.
- The dispatch benchmark was re-recorded (table above) and still shows roughly a 40–50% per-event reduction with identical peak allocations.
- `openspec validate stabilize-agent-workflow-reliability --strict` passes.
