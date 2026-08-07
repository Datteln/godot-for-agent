"""Atomic front-tool result validation, enrichment, and Map projection."""

from __future__ import annotations

import logging
from typing import Any

from app.api.schemas import ChatErrorResponse, ToolResult
from app.application.map_result_projection import (
    _arm_map_reader_text_completion,
    _is_dynamic_map_writer,
    _map_reader_has_detailed_region,
    _remember_map_batch_result,
    _remember_map_transaction_validation,
    _writer_platform_validation_failure_text,
)
from app.application.publication import SubmissionPublisher, SubmissionScope
from app.application.response_policy import _planner_completion_text
from app.application.submission.tool_artifacts import ToolArtifactService
from app.application.submission.tool_evidence import (
    append_cell_count_recovery_hint,
    record_screenshot_evidence,
)
from app.config import AppSettings
from app.orchestrator.map_artifacts import MapArtifactLocator
from app.orchestrator.map_progress import (
    map_platform_plan_attempt_count,
    parse_map_plan_outcome,
    remember_map_plan_progress,
    remember_map_tool_failure,
    remember_planning_snapshot_evidence,
    remember_validation_cache,
    remember_validation_progress,
    validation_mode,
)
from app.orchestrator.map_request_scope import mark_completion_candidate
from app.orchestrator.map_turn.contracts import AgentPromptFactory, _tool_message
from app.orchestrator.map_workers import (
    MAP_REVISION_GUARDED_TOOL_NAMES,
    MAP_VALIDATION_TOOL_NAMES,
    PLATFORM_PLAN_TOOL_NAMES,
)
from app.orchestrator.map_workflow import increment_map_counter, replace_map_state_field
from app.permissions.engine import make_session_allow_grant
from app.query.helpers import (
    _MAP_VALIDATION_REPEAT_LIMIT,
    _abort_pending_map_region_read_on_size_error,
    _append_platform_planning_failure_hint,
    _clear_validation_blockers,
    _has_review_blocker,
    _history_payload_for_front_tool,
    _map_completion_blocker,
    _map_region_from_write_args,
    _map_validation_is_successful,
    _remember_latest_map_region_read,
    _remember_latest_map_revision,
    _remember_map_validation,
    _review_required_blocker,
    _schedule_revision_conflict_reader,
    _update_map_context_state,
)
from app.query.tool_result_submission import (
    ToolResultBatchValidationError,
    ValidatedToolResultBatch,
    validate_tool_result_batch,
)
from app.security.settings import SecuritySettings
from app.sessions.store import Session, SessionStore
from app.tools.registry import REGISTRY

logger = logging.getLogger(__name__)


class ToolResultProcessor:
    """Owns one validated front-tool result batch."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        publisher: SubmissionPublisher,
        artifacts: ToolArtifactService,
    ) -> None:
        self._settings = settings
        self._store = store
        self._publisher = publisher
        self._artifacts = artifacts
    async def append(
        self,
        session: Session,
        results: list[ToolResult],
        security: SecuritySettings,
        prompt_factory: AgentPromptFactory | None = None,
        validated_batch: ValidatedToolResultBatch | None = None,
        publication_scope: SubmissionScope | None = None,
    ) -> tuple[ChatErrorResponse | None, list[dict[str, Any]]]:
        """Apply one preflighted batch through the projection pipeline."""
        return await self._append_validated(
            session,
            results,
            security,
            prompt_factory,
            validated_batch,
            publication_scope,
        )

    async def _append_validated(
        self,
        session: Session,
        results: list[ToolResult],
        security: SecuritySettings,
        prompt_factory: AgentPromptFactory | None = None,
        validated_batch: ValidatedToolResultBatch | None = None,
        publication_scope: SubmissionScope | None = None,
    ) -> tuple[ChatErrorResponse | None, list[dict[str, Any]]]:
        """校验前端工具结果并返回拒绝原因或 Verify 候选。"""
        try:
            batch = validated_batch or validate_tool_result_batch(
                session,
                results,
                REGISTRY,
            )
        except ToolResultBatchValidationError as exc:
            logger.warning(
                "Tool result preflight rejected session=%s code=%s reason=%s",
                session.session_id,
                exc.code,
                exc.message,
            )
            return (
                ChatErrorResponse(
                    text=exc.message,
                    error_code="tool_result_preflight_failed",
                ),
                [],
            )

        results = [item.result for item in batch.items]
        frames = {frame.id: frame for frame in session.agent_stack}

        verify_candidates: list[dict[str, Any]] = []
        for result in results:
            frame = frames[result.frame_id]
            is_error = result.status in {"rejected", "error"}
            metadata = session.pending_tool_calls.get(result.tool_use_id, {})
            tool_name = str(metadata.get("name", ""))
            tool_args = metadata.get("input", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool = REGISTRY.get(tool_name)
            payload: Any
            map_artifact_locator: MapArtifactLocator | None = None
            if result.status == "applied":
                applied_result = result.result
                if (
                    tool is not None
                    and tool.enrich is not None
                    and isinstance(applied_result, dict)
                ):
                    applied_result = tool.enrich(tool_args, applied_result)
                if isinstance(applied_result, dict):
                    applied_result = await self._artifacts.enrich_front_image(
                        tool_name,
                        applied_result,
                        security,
                        tool_args,
                    )
                    map_artifact_locator = self._artifacts.store_map_artifact(
                        session.session_id,
                        batch.turn_id,
                        result.tool_use_id,
                        tool_name,
                        tool_args,
                        applied_result,
                        publication_scope,
                    )
                    _update_map_context_state(
                        session,
                        tool_name,
                        tool_args,
                        applied_result,
                        (
                            map_artifact_locator.artifact_ref
                            if map_artifact_locator is not None
                            else None
                        ),
                        (
                            map_artifact_locator.as_dict()
                            if map_artifact_locator is not None
                            else None
                        ),
                    )
                if result.grant_session_allow and tool is not None:
                    session.session_allow.add(make_session_allow_grant(tool, tool_args))
                    logger.info(
                        "Session allow grant added session=%s tool=%s frame=%s",
                        session.session_id,
                        tool.name,
                        frame.id,
                    )
                artifact_refs = list(result.artifact_refs)
                if map_artifact_locator is not None:
                    artifact_refs.append(map_artifact_locator.artifact_ref)
                payload = {
                    "status": result.status,
                    "result": applied_result,
                    "artifact_refs": artifact_refs,
                    "grant_session_allow": result.grant_session_allow,
                }
                if (
                    self._settings.verify_after_edit
                    and tool_name in self._settings.verify_trigger_tools
                ):
                    path = tool_args.get("path") or tool_args.get("target_path")
                    if isinstance(path, str) and path:
                        verify_candidates.append(
                            {
                                "tool_use_id": result.tool_use_id,
                                "frame_id": frame.id,
                                "tool_name": tool_name,
                                "path": path,
                                "input": tool_args,
                            }
                        )
            else:
                payload = {
                    "status": result.status,
                    "error_code": result.error_code,
                    "result": result.result,
                }
            result_for_gate = payload.get("result") if isinstance(payload, dict) else None
            # ---- structure_revision 推进 ----
            # ensure_standard_map_layers 实际创建了/删除了图层时推进结构版本号，
            # 使下游依赖结构信息的派生状态（缓存、校验结果等）自动失效；
            # 未发生变更时仍写入当前版本号，保持下游可追溯。
            if (
                tool_name == "ensure_standard_map_layers"
                and result.status == "applied"
                and isinstance(result_for_gate, dict)
            ):
                if result_for_gate.get("changed") is True:
                    result_for_gate["structure_revision"] = (
                        session.map_task_state.record_structure_change()
                    )
                else:
                    result_for_gate["structure_revision"] = (
                        session.map_task_state.structure_revision
                    )
            record_screenshot_evidence(
                settings=self._settings,
                session=session,
                frame=frame,
                result=result,
                tool_name=tool_name,
                tool_args=tool_args,
                result_payload=result_for_gate,
                response_payload=payload,
            )
            if result.status == "error" and tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                result_error = result.result if isinstance(result.result, dict) else {}
                error_code = str(
                    result.error_code or result_error.get("error_code") or "map_tool_error"
                )
                error_message = str(
                    result_error.get("message") or payload.get("message", "") or error_code
                )
                remember_map_tool_failure(
                    session,
                    tool_name,
                    tool_args,
                    error_code,
                    error_message,
                )
            _remember_map_batch_result(
                session,
                tool_name,
                result.status,
                tool_args,
                result_for_gate,
            )
            if tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                transaction_status = (
                    str(result_for_gate.get("map_transaction_status", ""))
                    if isinstance(result_for_gate, dict)
                    else ""
                )
                if transaction_status == "committed":
                    self._publisher.emit(
                        session.session_id,
                        "write_committed",
                        {
                            "tool": tool_name,
                            "target_path": tool_args.get("target_path"),
                            "map_revision": (
                                result_for_gate.get("map_revision")
                                if isinstance(result_for_gate, dict)
                                else None
                            ),
                            "approval_id": tool_args.get("approval_id"),
                            "snapshot_id": tool_args.get("approval_snapshot_id"),
                        },
                        publication_scope,
                    )
                elif result.status in {"error", "rejected"} or transaction_status in {
                    "failed",
                    "rolled_back",
                }:
                    self._publisher.emit(
                        session.session_id,
                        "map_edit_incomplete",
                        {
                            "tool": tool_name,
                            "target_path": tool_args.get("target_path"),
                            "error_code": result.error_code,
                            "map_transaction_status": transaction_status,
                        },
                        publication_scope,
                    )
            if tool_name in MAP_REVISION_GUARDED_TOOL_NAMES and "plan_version" in tool_args:
                batch_entry = (
                    session.map_task_state.executed_batches[-1]
                    if session.map_task_state.executed_batches
                    else {}
                )
                self._publisher.emit(
                    session.session_id,
                    "map_batch_result",
                    {
                        "plan_version": tool_args.get("plan_version"),
                        "batch_index": tool_args.get("batch_index"),
                        "write_batch_id": tool_args.get("write_batch_id"),
                        "map_transaction_id": tool_args.get("map_transaction_id"),
                        "map_transaction_status": (
                            result_for_gate.get("map_transaction_status")
                            if isinstance(result_for_gate, dict)
                            else None
                        ),
                        "postconditions_passed": batch_entry.get("postconditions_passed", False),
                        "remaining_batches": len(session.map_task_state.pending_batches),
                    },
                    publication_scope,
                )
            if isinstance(result_for_gate, dict) and "workflow_constraints" in tool_args:
                result_for_gate.setdefault(
                    "workflow_constraints", tool_args["workflow_constraints"]
                )
            result_target = (
                str(result_for_gate.get("target_path", ""))
                if isinstance(result_for_gate, dict)
                else ""
            )
            trusted_error_revision = (
                result.status == "error"
                and str(result.error_code) == "map_revision_conflict"
                and bool(str(tool_args.get("target_path", "")).strip())
                and result_target == str(tool_args.get("target_path", "")).strip()
            )
            if result.status == "applied" or trusted_error_revision:
                _remember_latest_map_revision(session, tool_name, tool_args, result_for_gate)
            if result.status == "applied" and tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                session.map_request_scope = mark_completion_candidate(
                    session.map_request_scope,
                    lineage_id=str(metadata.get("request_lineage_id", "")),
                    map_task_id=str(metadata.get("map_task_id", "")),
                )
                if session.map_request_scope.completion_candidate:
                    session.map_task_lineage = {
                        **session.map_task_lineage,
                        "task_id": session.map_request_scope.map_task_id,
                        "lineage_id": session.map_request_scope.lineage_id,
                        "origin_request_id": (
                            session.map_task_lineage.get("origin_request_id")
                            or session.map_request_scope.request_id
                        ),
                        "completion_candidate": True,
                    }
            if isinstance(result_for_gate, dict):
                if result.status == "applied":
                    remember_planning_snapshot_evidence(
                        session,
                        tool_name,
                        tool_args,
                        result_for_gate,
                        self._settings.project_root,
                        (
                            map_artifact_locator.as_dict()
                            if map_artifact_locator is not None
                            else None
                        ),
                    )
                plan_progress = remember_map_plan_progress(
                    session,
                    tool_name,
                    tool_args,
                    result_for_gate,
                    self._settings.project_root,
                )
                if frame.agent.map_stage == "planner" and tool_name in PLATFORM_PLAN_TOOL_NAMES:
                    plan_outcome = parse_map_plan_outcome(tool_name, result_for_gate)
                    attempt_count = map_platform_plan_attempt_count(
                        session,
                        tool_args,
                        tool_name,
                    )
                    retry_exhausted = (
                        bool(plan_progress.get("exhausted"))
                        if isinstance(plan_progress, dict)
                        else False
                    )
                    if plan_outcome.executable or retry_exhausted:
                        frame.forced_completion_text = _planner_completion_text(
                            frame,
                            tool_name,
                            tool_args,
                            result_for_gate,
                        )
                        logger.info(
                            "Scheduled deterministic planner completion session=%s frame=%s "
                            "tool=%s executable=%s attempts=%d retry_exhausted=%s",
                            session.session_id,
                            frame.id,
                            tool_name,
                            plan_outcome.executable,
                            attempt_count,
                            retry_exhausted,
                        )
                        publication = result_for_gate.get("_planning_publication", {})
                        if isinstance(publication, dict):
                            event_name = (
                                "execution_approved"
                                if publication.get("execution_status") == "approved"
                                else "execution_blocked"
                            )
                            self._publisher.emit(
                                session.session_id,
                                "planning_delivered",
                                publication,
                                publication_scope,
                            )
                            self._publisher.emit(
                                session.session_id,
                                event_name,
                                publication,
                                publication_scope,
                            )
                elif (
                    _is_dynamic_map_writer(frame)
                    and tool_name in PLATFORM_PLAN_TOOL_NAMES
                    and not parse_map_plan_outcome(tool_name, result_for_gate).executable
                ):
                    frame.forced_completion_text = _writer_platform_validation_failure_text(
                        frame,
                        tool_name,
                        tool_args,
                        result_for_gate,
                    )
                    logger.info(
                        "Scheduled deterministic writer stop after platform validation "
                        "failure session=%s frame=%s tool=%s",
                        session.session_id,
                        frame.id,
                        tool_name,
                    )
                remember_validation_cache(
                    session,
                    tool_name,
                    tool_args,
                    result_for_gate,
                )
                if tool_name == "describe_map_region" and result.status == "applied":
                    increment_map_counter(session.map_task_state, "reads")
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    streaks = dict(session.map_task_state.no_progress_streaks)
                    streaks[target] = 0
                    replace_map_state_field(
                        session.map_task_state,
                        "no_progress_streaks",
                        streaks,
                        target=target,
                    )
                if (
                    tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                    and result.status == "applied"
                    and "plan_version" not in tool_args
                ):
                    increment_map_counter(session.map_task_state, "writes")
                    session.map_task_state.transition_stage("validate")
                if session.latest_context_used_tokens >= 32_000 and tool_name in {
                    "plan_map_layout",
                    "plan_map_algorithms",
                    "validate_platform_level_plan",
                    "plan_reachable_map_growth",
                    "validate_map_region",
                    *MAP_REVISION_GUARDED_TOOL_NAMES,
                }:
                    session.force_compact_next_turn = True
            if tool_name == "describe_map_region":
                _remember_latest_map_region_read(session, tool_args, result_for_gate)
                _abort_pending_map_region_read_on_size_error(
                    session,
                    tool_args,
                    result.error_code,
                    result_for_gate,
                )
                if (
                    frame.agent.map_stage == "reader"
                    and result.status == "applied"
                    and isinstance(result_for_gate, dict)
                    and _map_reader_has_detailed_region(result_for_gate)
                ):
                    frame.map_reader_detailed_region_ready = True
                    logger.info(
                        "Map reader detailed region ready session=%s frame=%s artifact=%s",
                        session.session_id,
                        frame.id,
                        map_artifact_locator is not None,
                    )
            blocker = _map_completion_blocker(
                tool_name, result.status, result_for_gate, result.error_code
            )
            if blocker is not None:
                map_layer = tool_args.get("map_layer")
                if isinstance(map_layer, int) and not isinstance(map_layer, bool):
                    blocker["map_layer"] = map_layer
            if tool_name in MAP_VALIDATION_TOOL_NAMES and isinstance(result_for_gate, dict):
                validation_state = _remember_map_validation(
                    session, tool_name, result_for_gate, tool_args
                )
                validation_success = _map_validation_is_successful(validation_state)
                mode = validation_mode(tool_args)
                remember_validation_progress(
                    session,
                    tool_name,
                    tool_args,
                    validation_state,
                    validation_success,
                )
                _remember_map_transaction_validation(
                    session,
                    tool_args,
                    validation_state,
                    validation_success,
                )
                if tool_name == "validate_map_region" and mode == "diagnostic":
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        [
                            {
                                "tool": tool_name,
                                "reason": "map_diagnostic_complete",
                                "issues": validation_state.get("issues", [])
                                or ["diagnostic finished; planner must produce a changed map plan"],
                                "target": validation_state["target"],
                                "required_revision": validation_state["map_revision"],
                                "next_stage": "planner",
                            }
                        ],
                        target=str(validation_state["target"]),
                        revision=validation_state["map_revision"],
                    )
                elif validation_success:
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    revision = result_for_gate.get("map_revision")
                    revision_value = (
                        revision
                        if isinstance(revision, int) and not isinstance(revision, bool)
                        else None
                    )
                    blockers = _clear_validation_blockers(
                        session.map_task_state.completion_blockers,
                        target,
                        revision_value,
                        tool_name,
                        tool_args,
                    )
                    if not _has_review_blocker(
                        blockers,
                        target,
                        revision_value,
                    ):
                        blockers.append(
                            _review_required_blocker(
                                tool_name,
                                target,
                                revision_value,
                                _map_region_from_write_args(tool_args, result_for_gate),
                            )
                        )
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        blockers,
                        target=target,
                        revision=revision_value,
                    )
                else:
                    validation_blocker = blocker or {
                        "tool": tool_name,
                        "reason": "validator_failed",
                        "issues": ["map validation did not pass"],
                        "target": validation_state["target"],
                        "required_revision": validation_state["map_revision"],
                    }
                    validation_blocker["validation_fingerprint"] = validation_state["fingerprint"]
                    validation_blocker["repeat_count"] = validation_state["repeat_count"]
                    validation_blocker["next_stage"] = "diagnostic"
                    if validation_state["repeat_count"] >= _MAP_VALIDATION_REPEAT_LIMIT:
                        validation_blocker["reason"] = "map_validation_repeat_limit"
                        validation_blocker["next_stage"] = "planner"
                        validation_blocker["issues"] = [
                            *validation_blocker.get("issues", []),
                            "same validation failure repeated without a new map revision; automatic retry stopped",
                        ]
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        [validation_blocker],
                        target=str(validation_state["target"]),
                        revision=validation_state["map_revision"],
                    )
            elif blocker is not None:
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    [blocker],
                    target=str(blocker.get("target", "")) or None,
                    revision=blocker.get("required_revision"),
                )
            history_payload = (
                _history_payload_for_front_tool(
                    tool_name,
                    payload,
                    (
                        map_artifact_locator.artifact_ref
                        if map_artifact_locator is not None
                        else None
                    ),
                    (map_artifact_locator.as_dict() if map_artifact_locator is not None else None),
                    frozenset(frame.agent.effective_tools),
                )
                if isinstance(payload, dict)
                else payload
            )
            frame.messages.append(
                _tool_message(result.tool_use_id, history_payload, is_error=is_error)
            )
            tool_message_index = len(frame.messages) - 1
            self._publisher.emit(
                session.session_id,
                "front_tool_result",
                {
                    "frame_id": frame.id,
                    "message_index": tool_message_index,
                    "message_id": f"{frame.id}:{tool_message_index}",
                    "tool_use_id": result.tool_use_id,
                    "call": {
                        "id": result.tool_use_id,
                        "name": tool_name,
                        "input": tool_args,
                        "agent": frame.agent.name,
                    },
                    "result": payload,
                    "status": result.status,
                },
                publication_scope,
            )
            if (
                frame.agent.map_stage == "reader"
                and tool_name == "describe_map_region"
                and frame.map_reader_detailed_region_ready
                and map_artifact_locator is None
            ):
                _arm_map_reader_text_completion(
                    frame,
                    mode=self._settings.map_worker_response_contract_mode,
                    correction_limit=(
                        self._settings.map_worker_structured_correction_limit
                        if self._settings.map_worker_structured_output_enabled
                        else 0
                    ),
                )
            if isinstance(result_for_gate, dict):
                _append_platform_planning_failure_hint(session, tool_name, result_for_gate)
            if (
                tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                and str(result.error_code) == "map_revision_conflict"
                and trusted_error_revision
            ):
                await _schedule_revision_conflict_reader(
                    session,
                    frame,
                    tool_name,
                    tool_args,
                    result_for_gate,
                    prompt_factory,
                )
            append_cell_count_recovery_hint(
                frame,
                result.error_code,
                result_for_gate,
            )
            logger.info(
                "Tool result appended session=%s turn_id=%s tool=%s status=%s frame=%s",
                session.session_id,
                result.turn_id,
                tool_name,
                result.status,
                frame.id,
            )

        session.clear_pending()
        logger.info("Tool results completed session=%s count=%d", session.session_id, len(results))
        return None, verify_candidates
