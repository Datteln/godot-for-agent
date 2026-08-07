"""Atomic Session/artifact/event publication after turn execution."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass

from app.api.schemas import ChatErrorResponse, ChatRequest, ChatResponse, ChatToolCallsResponse
from app.application.completed_turns import CompletedTurnLedger
from app.application.progress import TurnProgressRegistry
from app.application.publication import SubmissionPublisher, SubmissionScope
from app.application.response_mapping import chat_response_from_payload
from app.config import AppSettings
from app.orchestrator.map_artifacts import (
    CoordinatedCommitFailureInjector,
    MapArtifactStore,
    MapArtifactTurnConflictError,
)
from app.recovery.supervisor import RecoverySupervisor, RecoveryTokenError
from app.sessions.store import Session, SessionStore, session_to_dict

logger = logging.getLogger(__name__)


def _rebase_artifact_turn_identity(value: object, old_turn_id: str, new_turn_id: str) -> None:
    """Rebase staged locator identities without touching committed data."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in {"artifact_turn_id", "turn_id", "_submission_turn_id"} and item == old_turn_id:
                value[key] = new_turn_id
            else:
                _rebase_artifact_turn_identity(item, old_turn_id, new_turn_id)
    elif isinstance(value, list):
        for item in value:
            _rebase_artifact_turn_identity(item, old_turn_id, new_turn_id)


@dataclass(frozen=True, slots=True)
class SubmissionCommitCommand:
    request: ChatRequest
    response: ChatResponse
    working_session: Session
    snapshot: Session
    scope: SubmissionScope
    tool_batch_identity: tuple[str, str] | None
    progress_owner: int
    progress_turn_id: str | None


class SubmissionCommitService:
    """Owns the only durable commit of a completed submission scope."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        completed_turns: CompletedTurnLedger,
        recovery_supervisor: RecoverySupervisor,
        publisher: SubmissionPublisher,
        progress: TurnProgressRegistry,
        coordinated_failure: CoordinatedCommitFailureInjector | None,
    ) -> None:
        """初始化提交服务并保存注入的依赖。

        Args:
            settings: 应用配置，提供项目根路径等提交所需环境信息。
            store: Session 持久化存储。
            completed_turns: 已完成轮次的持久身份账本。
            recovery_supervisor: 恢复指针与令牌监督器。
            publisher: 事件与预览发布器。
            progress: 轮次进度注册表。
            coordinated_failure: 协调提交故障注入器（测试用，可为 None）。
        """
        self._settings = settings
        self._store = store
        self._completed_turns = completed_turns
        self._recovery_supervisor = recovery_supervisor
        self._publisher = publisher
        self._progress = progress
        self._coordinated_commit_failure_injector = coordinated_failure

    def _set_turn_progress(
        self,
        session_id: str,
        *,
        owner_id: int,
        request_id: str | None,
        turn_id: str | None,
        phase: str,
    ) -> None:
        self._progress.upsert_owned(
            session_id,
            owner_id=owner_id,
            request_id=request_id,
            turn_id=turn_id,
            phase=phase,
        )

    async def commit(self, command: SubmissionCommitCommand) -> ChatResponse:
        """Commit one resolved turn without exposing commit internals."""
        return await self._commit(command)

    async def _commit(self, command: SubmissionCommitCommand) -> ChatResponse:
        """Commit Session, artifact and buffered publications atomically."""
        request = command.request
        response = command.response
        working_session = command.working_session
        snapshot = command.snapshot
        publication_buffer = command.scope
        tool_batch_identity = command.tool_batch_identity
        progress_owner = command.progress_owner
        progress_turn_id = command.progress_turn_id

        if isinstance(response, ChatErrorResponse) and response.attempt_id is None:
            problem = self._recovery_supervisor.problem(
                working_session,
                error_code=response.error_code or "internal_error",
                text=response.text,
                side_effect_state=(
                    response.side_effect_state
                    if response.side_effect_state != "none"
                    else None
                ),
                next_action=response.next_action,
            )
            response = ChatErrorResponse(**problem)
            if publication_buffer is not None:
                problem_fields = response.model_dump(
                    exclude={"type", "text"},
                    exclude_none=True,
                )
                for _, event_type, event_payload in publication_buffer.events:
                    if event_type == "error":
                        event_payload.update(problem_fields)
        else:
            if not isinstance(response, ChatErrorResponse):
                self._recovery_supervisor.complete_attempt(
                    working_session,
                    waiting_frontend=isinstance(response, ChatToolCallsResponse),
                )
                if working_session.map_task_state.status == "completed":
                    self._recovery_supervisor.mark_terminal(
                        working_session,
                        outcome="completed",
                        authorized_by="completion_gate",
                    )
        self._store.save_task_run(working_session)
        
        if request.request_id is not None:
            working_session.request_id_cache[request.request_id] = response.model_dump()
        # 若本轮已完整消费且 pending_turn_id 已推进（说明不是同一轮的重试），
        # 将响应写入幂等缓存，供后续相同批次的重放请求使用
        if (
            tool_batch_identity is not None
            and working_session.pending_turn_id != tool_batch_identity[0]
        ):
            self._completed_turns.record(
                working_session,
                turn_id=tool_batch_identity[0],
                fingerprint=tool_batch_identity[1],
                response=response.model_dump(),
            )
        artifact_store: MapArtifactStore | None = None
        artifact_prepared = False
        try:
            if (
                publication_buffer is not None
                and publication_buffer.map_artifact_turn.entries
            ):
                artifact_store = MapArtifactStore(
                    self._settings.project_root,
                    working_session.session_id,
                    self._coordinated_commit_failure_injector,
                    working_session.session_epoch,
                )
                artifact_prepared = artifact_store.prepare_turn(
                    publication_buffer.map_artifact_turn
                )
            self._set_turn_progress(
                request.session_id,
                owner_id=progress_owner,
                request_id=request.request_id,
                turn_id=progress_turn_id,
                phase="committing",
            )
            if self._coordinated_commit_failure_injector is not None:
                self._coordinated_commit_failure_injector.hit(
                    "session_publish_before_write"
                )
            self._store.save(working_session)
            if self._coordinated_commit_failure_injector is not None:
                self._coordinated_commit_failure_injector.hit("session_publish_after_write")
        except MapArtifactTurnConflictError as exc:
            try:
                if publication_buffer is None or artifact_store is None:
                    raise ValueError("turn conflict has no recoverable publication")
                old_turn_id = exc.turn_id
                self._recovery_supervisor.hit_failpoint("fresh_turn_before_allocate")
                fresh_turn_id = working_session.new_turn_id()
                self._recovery_supervisor.hit_failpoint("fresh_turn_after_allocate")
                conflict_problem = self._recovery_supervisor.problem(
                    working_session,
                    error_code=exc.error_code,

                    text=(
                        "工具结果 turn_id 与已提交内容冲突；原提交已保留，"
                        "后端正在新的更大 turn_id 下恢复"
                    ),
                    side_effect_state="committed",
                    next_action={
                        "action": "backend_rebase_and_commit",
                        "turn_id": fresh_turn_id,
                    },
                )
                recovery_token = conflict_problem.get("retry_token")
                if not isinstance(recovery_token, str) or not recovery_token:
                    raise ValueError("turn conflict did not issue a recovery token")
                recovery_request = request.model_copy(
                    update={"recovery_token": recovery_token}
                )
                self._recovery_supervisor.begin_attempt(
                    working_session,
                    recovery_request,
                )
        
                recovered_session = copy.deepcopy(working_session)
                _rebase_artifact_turn_identity(
                    recovered_session.__dict__,
                    old_turn_id,
                    fresh_turn_id,
                )
                recovered_session.turn_counter = max(
                    recovered_session.turn_counter,
                    working_session.turn_counter,
                )
                recovered_response_payload = response.model_dump()
                _rebase_artifact_turn_identity(
                    recovered_response_payload,
                    old_turn_id,
                    fresh_turn_id,
                )
                recovered_response = chat_response_from_payload(recovered_response_payload)
                publication_buffer.session = recovered_session
                publication_buffer.turn_id = fresh_turn_id
                publication_buffer.map_artifact_turn.turn_id = fresh_turn_id
                for _, _, event_payload in publication_buffer.events:
                    _rebase_artifact_turn_identity(
                        event_payload,
                        old_turn_id,
                        fresh_turn_id,
                    )
                artifact_prepared = artifact_store.prepare_turn(
                    publication_buffer.map_artifact_turn
                )
                self._recovery_supervisor.complete_attempt(
                    recovered_session,
                    waiting_frontend=isinstance(
                        recovered_response,
                        ChatToolCallsResponse,
                    ),
                )
                self._store.save_task_run(recovered_session)
                self._store.save(recovered_session)
                if artifact_prepared:
                    artifact_store.commit_prepared_turn(
                        publication_buffer.map_artifact_turn
                    )
                self._publisher.flush(publication_buffer)
                self._publisher.resolve_previews(
                    publication_buffer,
                    committed=not isinstance(
                        recovered_response,
                        ChatErrorResponse,
                    ),
                    reason=(
                        recovered_response.error_code
                        or "submission_returned_error"
                        if isinstance(recovered_response, ChatErrorResponse)
                        else None
                    ),
                )
                self._publisher.record_recovery(
                    recovered_session,
                    recovered_response,
                )
                logger.warning(
                    "Map artifact turn conflict recovered session=%s "
                    "old_turn=%s fresh_turn=%s",
                    request.session_id,
                    old_turn_id,
                    fresh_turn_id,
                )
                return recovered_response
            except (OSError, TypeError, ValueError, RecoveryTokenError):

                if (
                    artifact_prepared
                    and artifact_store is not None
                    and publication_buffer is not None
                ):
                    try:
                        artifact_store.discard_prepared_turn(
                            publication_buffer.map_artifact_turn
                        )
                    except (OSError, TypeError, ValueError):
                        logger.exception(
                            "Failed to discard rebased artifact session=%s",
                            request.session_id,
                        )
                if publication_buffer is not None:
                    self._publisher.resolve_previews(
                        publication_buffer,
                        committed=False,
                        reason="turn_identity_recovery_failed",
                    )
                snapshot.turn_counter = max(
                    snapshot.turn_counter,
                    working_session.turn_counter,
                )
                fresh_turn_id = snapshot.new_turn_id()
                if snapshot.pending_turn_id is not None:
                    snapshot.pending_turn_id = fresh_turn_id
                problem = self._recovery_supervisor.problem(
                    snapshot,
                    error_code=exc.error_code,
                    text=(
                        "工具结果 turn_id 与已提交内容冲突；原提交已保留，"
                        "自动恢复失败，已保留新的 turn 检查点"
                    ),
                    side_effect_state="committed",
                    next_action={
                        "action": "resubmit_tool_results",
                        "turn_id": fresh_turn_id,
                    },
                )
                self._store.replace_in_memory(
                    request.session_id,
                    snapshot,
                )
                self._store.save_task_run(snapshot)
                self._store.save(snapshot)
                logger.exception(
                    "Map artifact turn identity recovery failed " "session=%s turn=%s",
                    request.session_id,
                    exc.turn_id,
                )
                return ChatErrorResponse(**problem)
        except (OSError, TypeError, ValueError):
            if (
                artifact_prepared
                and artifact_store is not None
                and publication_buffer is not None
            ):
                try:
                    artifact_store.discard_prepared_turn(
                        publication_buffer.map_artifact_turn
                    )
                except (OSError, TypeError, ValueError):
                    logger.exception(
                        "Failed to discard unreferenced prepared map artifact "
                        "session=%s turn=%s",
                        request.session_id,
                        publication_buffer.turn_id,
                    )
            if publication_buffer is not None:
                self._publisher.resolve_previews(
                    publication_buffer,
                    committed=False,
                    reason="session_persistence_failed",
                )
            problem = self._recovery_supervisor.problem(
                snapshot,
                error_code="session_persistence_failed",
                text="会话持久化失败；工具结果未提交，后端将从原检查点恢复",
                side_effect_state="rolled_back",
            )
            self._store.replace_in_memory(request.session_id, snapshot)
            try:
                self._store.save_task_run(snapshot)
            except (OSError, TypeError, ValueError):
                logger.exception(
                    "Failed to persist persistence-failure recovery state " "session=%s",
                    request.session_id,
                )
            logger.exception(

                "Session commit failed; original session retained session=%s request_id=%s",
                request.session_id,
                request.request_id,
            )
            return ChatErrorResponse(**problem)
        session = working_session
        if publication_buffer is not None:
            if artifact_prepared and artifact_store is not None:
                try:
                    artifact_store.commit_prepared_turn(
                        publication_buffer.map_artifact_turn
                    )
                except (OSError, TypeError, ValueError):
                    logger.exception(
                        "Prepared map artifact finalization failed; "
                        "attempting reconciliation session=%s turn=%s",
                        request.session_id,
                        publication_buffer.turn_id,
                    )
                    try:
                        artifact_store.reconcile_with_session(
                            session_to_dict(working_session)
                        )
                    except (OSError, TypeError, ValueError):
                        logger.exception(
                            "Map artifact reconciliation remains pending "
                            "session=%s turn=%s",
                            request.session_id,
                            publication_buffer.turn_id,
                        )
            self._publisher.flush(publication_buffer)
            self._publisher.resolve_previews(
                publication_buffer,
                committed=not isinstance(response, ChatErrorResponse),
                reason=(
                    response.error_code or "submission_returned_error"
                    if isinstance(response, ChatErrorResponse)
                    else None
                ),
            )
        self._publisher.record_recovery(session, response)
        logger.info(
            "Chat request completed session=%s response_type=%s pending=%s",
            request.session_id,
            response.type,
            session.pending_turn_id is not None,
        )
        logger.debug(
            "Chat response details session=%s type=%s response=%s",
            request.session_id,
            response.type,
            json.dumps(response.model_dump(), ensure_ascii=False, default=str),
        )
        return response
