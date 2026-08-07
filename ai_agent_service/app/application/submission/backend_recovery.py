"""Bounded backend retry after a proven submission rollback."""

from __future__ import annotations

import asyncio
import copy
import logging

from app.api.schemas import ChatErrorResponse, ChatRequest, ChatResponse
from app.application.publication import SubmissionPublisher, SubmissionScope
from app.application.submission.turn_service import TurnExecutionService
from app.query.tool_result_submission import ValidatedToolResultBatch
from app.recovery.supervisor import RecoverySupervisor
from app.sessions.store import Session, SessionStore

logger = logging.getLogger(__name__)


class BackendRecoveryService:
    """Owns clean-checkpoint retries without publishing discarded facts."""

    def __init__(
        self,
        *,
        store: SessionStore,
        recovery_supervisor: RecoverySupervisor,
        publisher: SubmissionPublisher,
        turn_service: TurnExecutionService,
    ) -> None:
        self._store = store
        self._recovery_supervisor = recovery_supervisor
        self._publisher = publisher
        self._turn_service = turn_service

    async def execute(
        self,
        session: Session,
        request: ChatRequest,
        validated_tool_batch: ValidatedToolResultBatch | None,
        *,
        snapshot: Session,
        publication_buffer: SubmissionScope,
    ) -> tuple[ChatResponse, Session]:
        """在已证明回滚的边界内由后端执行有界的新 Attempt 重试。"""
        active = session
        while True:
            try:
                response = await self._turn_service.execute(
                    active,
                    request,
                    validated_tool_batch,
                    publication_buffer,
                )
                return response, active
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Chat submission attempt failed before backend recovery "
                    "session=%s request_id=%s",
                    request.session_id,
                    request.request_id,
                )
                retry_state = copy.deepcopy(snapshot)
                retry_state.task_run = copy.deepcopy(active.task_run)
                problem = self._recovery_supervisor.problem(
                    retry_state,
                    error_code="submission_internal_error",
                    text=(
                        "处理请求时发生内部错误；副作用已回滚，"
                        "后端正在从持久检查点启动新的 attempt"
                    ),
                    side_effect_state="rolled_back",
                )
                has_visible_or_staged_publication = publication_buffer is not None and bool(
                    publication_buffer.preview.items
                    or publication_buffer.events
                    or publication_buffer.map_artifact_turn.entries
                )
                if has_visible_or_staged_publication:
                    assert publication_buffer is not None
                    self._publisher.resolve_previews(
                        publication_buffer,
                        committed=False,
                        reason="submission_failed",
                    )
                    publication_buffer.events.clear()
                    publication_buffer.preview.items.clear()
                    publication_buffer.map_artifact_turn.entries.clear()
                    problem = self._recovery_supervisor.force_pause(
                        retry_state,
                        problem,
                        action="resume_from_clean_submission_checkpoint",
                        reason="provisional_or_transactional_publication_was_discarded",
                    )
                self._store.replace_in_memory(request.session_id, retry_state)
                self._store.save_task_run(retry_state)
                next_action = problem.get("next_action")
                owner = str(next_action.get("owner", "")) if isinstance(next_action, dict) else ""
                token = problem.get("retry_token")
                if (
                    has_visible_or_staged_publication
                    or problem.get("disposition") != "retry_new_attempt"
                    or owner != "backend"
                    or not isinstance(token, str)
                    or not token
                ):
                    logger.exception(
                        "Chat request recovery paused session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return ChatErrorResponse(**problem), retry_state
                if publication_buffer is not None:
                    self._publisher.resolve_previews(
                        publication_buffer,
                        committed=False,
                        reason="backend_retry_new_attempt",
                    )
                    publication_buffer.events.clear()
                    publication_buffer.preview.items.clear()
                    publication_buffer.map_artifact_turn.entries.clear()
                    publication_buffer.session = retry_state
                recovery_request = request.model_copy(update={"recovery_token": token})
                self._recovery_supervisor.begin_attempt(
                    retry_state,
                    recovery_request,
                )
                self._store.save_task_run(retry_state)
                delay_ms = (
                    int(next_action.get("backoff_ms", 0) or 0)
                    if isinstance(next_action, dict)
                    else 0
                )
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
                active = retry_state
