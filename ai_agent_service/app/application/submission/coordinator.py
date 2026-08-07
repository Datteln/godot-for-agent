"""原子提交协调器：拥有用户与工具结果的事务边界。

`SubmissionCoordinator` 负责：
- 会话锁与本地持久化；
- 用户消息、前端工具结果与 agent 帧消息的转换；
- `request_id` 幂等缓存；
- 当前请求权限模式覆盖；
- 调用 `TurnDriver.run()` 并转换为 HTTP DTO。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.api.schemas import (
    ChatErrorResponse,
    ChatRequest,
    ChatResponse,
)
from app.application.completed_turns import (
    CompletedTurnLedger,
)
from app.application.progress import TurnActivityRegistry, TurnProgressRegistry
from app.application.publication import SubmissionPublisher, SubmissionScope
from app.application.session_uow import SessionUnitOfWork
from app.application.submission.backend_recovery import BackendRecoveryService
from app.application.submission.commit_service import (
    SubmissionCommitCommand,
    SubmissionCommitService,
)
from app.application.submission.preflight import (
    SubmissionPreflightAccepted,
    SubmissionPreflightService,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.orchestrator.map_artifacts import (
    CoordinatedCommitFailureInjector,
    StagedMapArtifactTurn,
)
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import RecoverySupervisor
from app.sessions.store import SessionStore
from app.tools.registry import REGISTRY

logger = logging.getLogger(__name__)
class SubmissionCoordinator:
    """用户消息与工具结果提交的事务协调器。

    M0 中该对象可作为进程级单例：内部把不同 `session_id` 分发给
    `SessionStore`，并用 per-session lock 串行化同一会话的请求。
    """

    def __init__(
        self,
        settings: AppSettings,
        session_store: SessionStore,
        *,
        event_store: EventStore | None = None,
        recovery_store: RecoveryPointerStore | None = None,
        coordinated_commit_failure_injector: CoordinatedCommitFailureInjector | None = None,
        recovery_supervisor: RecoverySupervisor,
        publisher: SubmissionPublisher,
        activity: TurnActivityRegistry,
        progress: TurnProgressRegistry,
        backend_recovery: BackendRecoveryService,
        completed_turns: CompletedTurnLedger,
        commit_service: SubmissionCommitService,
        preflight: SubmissionPreflightService,
        unit_of_work: SessionUnitOfWork,
    ) -> None:
        """构造提交协调器。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
            cache_engine: 上下文缓存决策引擎（§16.1）；缺省时构造新实例。
            cache_metrics: 缓存命中率观测聚合器；缺省时构造新实例。
            coordinated_commit_failure_injector: 仅测试组合传入的命名故障依赖；
                生产默认关闭，且不从请求或持久化载荷读取。
        """
        self._settings = settings
        self._store = session_store
        self._events = event_store
        self._recovery = recovery_store
        self._completed_turns = completed_turns
        self._coordinated_commit_failure_injector = coordinated_commit_failure_injector
        self._recovery_supervisor = recovery_supervisor
        self._publisher = publisher
        self._backend_recovery = backend_recovery
        self._commit_service = commit_service
        self._preflight = preflight
        self._unit_of_work = unit_of_work
        # session_id -> 该会话当前所有"正在处理 /chat 请求"的任务集合（通常只有
        # 一个，但用户可能在前一个请求仍卡在 per-session 锁等待时就发出下一条
        # 消息/中断，short-lived 地出现多个；用 set 而不是单个槎位，避免新任务
        # 覆盖掉真正持有锁、仍在运行的旧任务引用，导致 interrupt() 取消错对象。
        self._activity = activity
        self._progress = progress

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    def turn_progress(self, session_id: str) -> dict[str, Any] | None:
        """返回活跃请求的事务外存活快照，并推进临时心跳序号。

        该状态只存在于进程内，不写 Session、历史、artifact 或恢复指针。

        Args:
            session_id: 查询的会话标识。

        Returns:
            活跃请求的存活信息；无活跃请求时返回 None。
        """
        return self._progress.heartbeat_snapshot(session_id)

    def _set_turn_progress(
        self,
        session_id: str,
        *,
        owner_id: int,
        request_id: str | None,
        turn_id: str | None,
        phase: str,
    ) -> None:
        """更新活跃请求的临时阶段，不触碰可恢复业务状态。"""
        self._progress.upsert_owned(
            session_id,
            owner_id=owner_id,
            request_id=request_id,
            turn_id=turn_id,
            phase=phase,
        )

    def _clear_turn_progress(self, session_id: str, owner_id: int) -> None:
        """仅清除属于指定请求的临时存活状态。"""
        self._progress.remove_owned(session_id, owner_id)

    async def submit_user_turn(self, request: ChatRequest) -> ChatResponse:
        """处理一次 `/chat` 请求。

        `user_message` 发起新用户轮次；`tool_results` 回填上一轮 front 工具结果。
        两者不可同时出现，且会话有 pending 工具结果时拒绝新用户消息。

        本方法把当前 `asyncio.Task` 登记到 `_active_tasks`，使
        `interrupt()` 能在用户点击"停止"时真正取消仍在运行的 agent 循环
        （而不是仅让前端断开 HTTP 连接、后端继续跑完整个 turn）。
        """
        task = asyncio.current_task()
        progress_owner = id(task) if task is not None else id(request)
        if task is not None:
            self._activity.add(request.session_id, task)
        self._set_turn_progress(
            request.session_id,
            owner_id=progress_owner,
            request_id=request.request_id,
            turn_id=None,
            phase="queued",
        )
        try:
            async with self._unit_of_work.serialize(request.session_id):
                self._set_turn_progress(
                    request.session_id,
                    owner_id=progress_owner,
                    request_id=request.request_id,
                    turn_id=None,
                    phase="accepted",
                )
                preflight = self._preflight.prepare(request)
                if not isinstance(preflight, SubmissionPreflightAccepted):
                    return preflight
                session = preflight.session
                validated_tool_batch = preflight.validated_tool_batch
                tool_batch_identity = preflight.tool_batch_identity
                progress_turn_id = preflight.progress_turn_id
                self._set_turn_progress(
                    request.session_id,
                    owner_id=progress_owner,
                    request_id=request.request_id,
                    turn_id=progress_turn_id,
                    phase=(
                        "tool_result_transaction"
                        if validated_tool_batch is not None
                        else "agent_turn"
                    ),
                )
                working_set = self._unit_of_work.working_set(
                    session,
                    isolate=validated_tool_batch is not None,
                )
                snapshot = working_set.snapshot
                working_session = working_set.session
                current_run = (
                    working_session.task_run
                    if isinstance(working_session.task_run, dict)
                    else {}
                )
                submission_turn_id = (
                    validated_tool_batch.turn_id
                    if validated_tool_batch is not None
                    else str(
                        current_run.get("current_attempt_id")
                        or request.request_id
                        or "unidentified-submission"
                    )
                )
                staged_map_turn = StagedMapArtifactTurn(
                    session_id=working_session.session_id,
                    turn_id=submission_turn_id,
                    request_id=request.request_id,
                    session_epoch=working_session.session_epoch,
                )
                publication_buffer = SubmissionScope(
                    session=working_session,
                    request_id=request.request_id,
                    turn_id=submission_turn_id,
                    map_artifact_turn=staged_map_turn,
                )
                try:
                    response, working_session = await self._backend_recovery.execute(
                        working_session,
                        request,
                        validated_tool_batch,
                        snapshot=snapshot,
                        publication_buffer=publication_buffer,
                    )
                    if publication_buffer is not None:
                        publication_buffer.session = working_session
                except asyncio.CancelledError:
                    if publication_buffer is not None:
                        self._publisher.resolve_previews(
                            publication_buffer,
                            committed=False,
                            reason="cancelled",
                        )
                    if isinstance(snapshot.task_run, dict):
                        try:
                            problem = self._recovery_supervisor.problem(
                                snapshot,
                                error_code="response_transport_lost",
                                text="请求传输已中断；任务检查点和 attempt 身份已保留",
                                side_effect_state="ambiguous",
                                next_action={
                                    "action": "reconnect_and_observe_attempt",
                                },
                            )
                            snapshot.task_run["last_problem"] = problem
                            self._store.save_task_run(snapshot)
                        except (OSError, TypeError, ValueError):
                            logger.exception(
                                "Failed to persist cancelled transport state session=%s",
                                request.session_id,
                            )
                    self._unit_of_work.restore(request.session_id, snapshot)
                    raise
                except Exception:
                    if publication_buffer is not None:
                        self._publisher.resolve_previews(
                            publication_buffer,
                            committed=False,
                            reason="submission_failed",
                        )
                    problem = self._recovery_supervisor.problem(
                        snapshot,
                        error_code="submission_internal_error",
                        text=(
                            "处理工具结果时发生内部错误；会话状态已回滚，"
                            "后端已保留同一 attempt 的安全恢复检查点"
                        ),
                    )
                    self._unit_of_work.restore(request.session_id, snapshot)
                    try:
                        self._store.save_task_run(snapshot)
                    except (OSError, TypeError, ValueError):
                        logger.exception(
                            "Failed to persist recovery problem session=%s",
                            request.session_id,
                        )
                    logger.exception(
                        "Chat request failed; restored session snapshot session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return ChatErrorResponse(**problem)
                return await self._commit_service.commit(
                    SubmissionCommitCommand(
                        request=request,
                        response=response,
                        working_session=working_session,
                        snapshot=snapshot,
                        scope=publication_buffer,
                        tool_batch_identity=tool_batch_identity,
                        progress_owner=progress_owner,
                        progress_turn_id=progress_turn_id,
                    )
                )
        finally:
            self._clear_turn_progress(request.session_id, progress_owner)
            if task is not None:
                self._activity.remove(request.session_id, task)
