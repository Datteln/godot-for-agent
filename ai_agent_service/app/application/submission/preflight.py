"""Submission schema, identity, and idempotency preflight."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.api.schemas import ChatErrorResponse, ChatRequest, ChatResponse, ToolResult
from app.application.completed_turns import (
    CompletedTurnConflictError,
    CompletedTurnIntegrityError,
    CompletedTurnLedger,
)
from app.application.response_mapping import chat_response_from_payload
from app.config import AppSettings
from app.events.store import EventStore
from app.orchestrator.map_artifacts import MapArtifactStore
from app.query.tool_result_submission import (
    ToolResultBatchValidationError,
    ValidatedToolResultBatch,
    validate_tool_result_batch,
)
from app.recovery.supervisor import RecoverySupervisor, RecoveryTokenError
from app.sessions.schema import UnsupportedSessionSchemaError
from app.sessions.store import Session, SessionStore, session_to_dict
from app.tools.registry import REGISTRY
from app.workflow.contracts import WorkflowIntegrityError

logger = logging.getLogger(__name__)


def _tool_result_batch_identity(
    results: list[ToolResult] | None,
) -> tuple[str, str] | None:
    """Build the canonical identity for one front-tool result batch."""
    if not results:
        return None
    turn_ids = {result.turn_id for result in results}
    if len(turn_ids) != 1:
        return None
    canonical_results = sorted(
        (result.model_dump(mode="json") for result in results),
        key=lambda item: str(item.get("tool_use_id", "")),
    )
    encoded = json.dumps(
        canonical_results,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return next(iter(turn_ids)), hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmissionPreflightAccepted:
    session: Session
    validated_tool_batch: ValidatedToolResultBatch | None
    tool_batch_identity: tuple[str, str] | None
    progress_turn_id: str | None


class SubmissionPreflightService:
    """Rejects stale or conflicting requests before creating a working copy."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        events: EventStore | None,
        completed_turns: CompletedTurnLedger,
        recovery_supervisor: RecoverySupervisor,
        available_tools: Callable[[], set[str]],
    ) -> None:
        self._settings = settings
        self._store = store
        self._events = events
        self._completed_turns = completed_turns
        self._recovery_supervisor = recovery_supervisor
        self._available_tools = available_tools

    @property
    def available_tools(self) -> set[str]:
        return self._available_tools()

    def prepare(
        self, request: ChatRequest
    ) -> SubmissionPreflightAccepted | ChatResponse:
        try:
            session = self._store.get_or_create(
                request.session_id,
                self.available_tools,
            )
        except UnsupportedSessionSchemaError as exc:
            return ChatErrorResponse(
                text=str(exc),
                error_code="unsupported_session_schema",
                disposition="pause_for_user",
                retryable=False,
                side_effect_state="none",
                next_action={"action": "create_new_session"},
            )
        except WorkflowIntegrityError as exc:
            return ChatErrorResponse(
                text=f"当前 Session 工作流完整性校验失败：{exc}",
                error_code="workflow_integrity_error",
                disposition="pause_for_user",
                retryable=False,
                side_effect_state="none",
                next_action={"action": "preserve_and_inspect_diagnostics"},
            )
        resumed_run = self._recovery_supervisor.resume_after_restart(session)
        if resumed_run is not None:
            self._store.save_task_run(session)
        if (
            request.session_epoch is not None
            and request.session_epoch != session.session_epoch
        ):
            return ChatErrorResponse(
                text="请求属于已重置的旧会话生命周期，请刷新会话状态后重试",
                error_code="stale_session_epoch",
                disposition="wait_frontend",
                retryable=True,
                side_effect_state="none",
                next_action={
                    "action": "adopt_session_epoch",
                    "session_epoch": session.session_epoch,
                },
            )
        try:
            MapArtifactStore(
                self._settings.project_root,
                session.session_id,
                None,
                session.session_epoch,
            ).reconcile_with_session(session_to_dict(session))
        except (OSError, TypeError, ValueError):
            logger.exception(
                "Map artifact startup reconciliation failed session=%s",
                session.session_id,
            )
        # 确保事件存储的序列号与会话历史计数器对齐，
        # 防止因崩溃恢复或跨进程导致的序列偏移
        if self._events is not None:
            self._events.ensure_sequence(
                session.session_id,
                session.history_event_counter,
                session_epoch=session.session_epoch,
            )
        logger.info(
            "Chat request accepted session=%s request_id=%s has_user=%s tool_results=%d",
            request.session_id,
            request.request_id,
            request.user_message is not None,
            len(request.tool_results or []),
        )
        
        if (
            request.request_id is not None
            and request.request_id in session.request_id_cache
        ):
            logger.info(
                "Chat idempotency hit session=%s request_id=%s",
                request.session_id,
                request.request_id,
            )
            return chat_response_from_payload(session.request_id_cache[request.request_id])
        
        # ---- 工具结果批次持久身份 ----
        # 前端可能因超时/断连重发同一批 tool_results（request_id 可能不同），
        # 此处基于 turn_id + 内容指纹做二次幂等保护：
        # 1) 指纹匹配 → 直接返回缓存的响应，避免重复执行
        # 2) 指纹不匹配 → 同一 turn_id 内容不同属于协议违规，拒绝请求
        tool_batch_identity = _tool_result_batch_identity(request.tool_results)
        if tool_batch_identity is not None:
            try:
                completed_turn = self._completed_turns.resolve(
                    session,
                    turn_id=tool_batch_identity[0],
                    fingerprint=tool_batch_identity[1],
                )
            except CompletedTurnConflictError:
                self._recovery_supervisor.begin_attempt(session, request)
                problem = self._recovery_supervisor.problem(
                    session,
                    error_code="tool_result_batch_mismatch",
                    text="同一 turn_id 已处理，但重试的 tool_results 内容不同",
                )
                self._store.save_task_run(session)
                return ChatErrorResponse(**problem)
            except CompletedTurnIntegrityError as exc:
                self._recovery_supervisor.begin_attempt(session, request)
                problem = self._recovery_supervisor.problem(
                    session,
                    error_code="completed_turn_integrity_error",
                    text=f"已提交工具结果的持久响应无法安全恢复：{exc}",
                    side_effect_state="committed",
                    next_action={"action": "pause_for_recovery"},
                )
                self._store.save_task_run(session)
                return ChatErrorResponse(**problem)
            if completed_turn is not None:
                logger.info(
                    "Tool result batch idempotency hit session=%s turn_id=%s source=%s",
                    request.session_id,
                    tool_batch_identity[0],
                    completed_turn.source,
                )
                return chat_response_from_payload(completed_turn.response)
        
        try:
            self._recovery_supervisor.begin_attempt(session, request)
            self._store.save_task_run(session)
        except RecoveryTokenError as exc:
            return ChatErrorResponse(
                text=str(exc),
                error_code="invalid_recovery_token",
                disposition="pause_for_user",
                retryable=False,
                side_effect_state="none",
            )
        
        validated_tool_batch: ValidatedToolResultBatch | None = None
        if request.tool_results is not None:
            try:
                validated_tool_batch = validate_tool_result_batch(
                    session,
                    request.tool_results,
                    REGISTRY,
                )
            except ToolResultBatchValidationError as exc:
                logger.warning(
                    "Tool result preflight rejected session=%s code=%s reason=%s",
                    request.session_id,
                    exc.code,
                    exc.message,
                )
                problem = self._recovery_supervisor.problem(
                    session,
                    error_code="tool_result_preflight_failed",
                    text=exc.message,
                )
                self._store.save_task_run(session)
                return ChatErrorResponse(**problem)
        progress_turn_id = (
            validated_tool_batch.turn_id if validated_tool_batch is not None else None
        )
        return SubmissionPreflightAccepted(
            session=session,
            validated_tool_batch=validated_tool_batch,
            tool_batch_identity=tool_batch_identity,
            progress_turn_id=progress_turn_id,
        )
