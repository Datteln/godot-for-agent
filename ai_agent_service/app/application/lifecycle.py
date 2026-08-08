"""Session ??????????? Map ???????"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatResponse,
    InterruptCause,
    InterruptResponse,
    ResetResponse,
)
from app.application.progress import TurnActivityRegistry, TurnProgressRegistry
from app.application.publication import SubmissionPublisher
from app.config import AppSettings
from app.events.store import EventStore
from app.orchestrator.map_artifacts import clear_session_artifacts
from app.orchestrator.map_state import resume_map_task

from app.orchestrator.map_request_scope import invalidate_completion_candidate
from app.orchestrator.map_turn.contracts import _tool_message
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import RecoverySupervisor
from app.sessions.resource_registry import BACKEND_RESET_STEPS
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)


class SessionLifecycleService:
    """?? Session epoch?interrupt???? Map ???????"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        events: EventStore | None,
        recovery: RecoveryPointerStore | None,
        recovery_supervisor: RecoverySupervisor,
        publisher: SubmissionPublisher,
        activity: TurnActivityRegistry,
        progress: TurnProgressRegistry,
        history_cache: dict[tuple[str, str], tuple[tuple[int, int, int], list[Any]]],
        available_tools: Callable[[], set[str]],
    ) -> None:
        self._settings = settings
        self._store = store
        self._events = events
        self._recovery = recovery
        self._recovery_supervisor = recovery_supervisor
        self._publisher = publisher
        self._activity = activity
        self._progress = progress
        self._history_blocks_cache = history_cache
        self._available_tools = available_tools
        self._resume_incomplete_resets()

    @property
    def available_tools(self) -> set[str]:
        return self._available_tools()

    def _complete_reset_cleanup(self, record: dict[str, Any]) -> int:
        """完成 epoch barrier 之后的幂等物理清理并返回 reset 边界序号。"""
        session_id = str(record["session_id"])
        new_epoch = str(record["new_epoch"])
        persisted_highwater = int(record.get("last_event_seq", 0) or 0)
        last_seq = persisted_highwater
        handlers: dict[str, Callable[[], None]] = {
            "event_content": lambda: self._reset_event_content(
                record,
                session_id,
                new_epoch,
                persisted_highwater,
            ),
            "in_memory_session": lambda: self._store.remove_session_payload(session_id),
            "session_document": lambda: self._store.remove_session_payload(session_id),
            "task_run_journal": lambda: self._store.remove_session_payload(session_id),
            "map_artifacts": lambda: clear_session_artifacts(
                self._settings.project_root,
                session_id,
            ),
            "delegate_artifacts": lambda: clear_session_artifacts(
                self._settings.project_root,
                session_id,
            ),
            "recovery_pointer": lambda: (
                self._recovery.clear(session_id) if self._recovery is not None else None
            ),
            "history_projection_cache": lambda: self._history_blocks_cache.pop(
                (session_id, str(record.get("old_epoch", ""))),
                None,
            ),
            "turn_progress": lambda: self._progress.remove(session_id),
        }
        if set(handlers) != set(BACKEND_RESET_STEPS):
            raise ValueError("reset handler registry does not match resource contracts")
        self._store.checkpoint_reset(record, "cleaning")
        for resource_id in BACKEND_RESET_STEPS:
            if resource_id != "event_content" and self._store.reset_step_completed(
                record, resource_id
            ):
                continue
            self._store.hit_reset_failpoint(f"cleanup_before_{resource_id}")
            handlers[resource_id]()
            self._store.complete_reset_step(record, resource_id)
            self._store.hit_reset_failpoint(f"cleanup_after_{resource_id}")
        if self._events is not None:
            last_seq = max(
                int(record.get("last_event_seq", 0) or 0),
                self._events.last_seq(session_id),
            )
        record["last_event_seq"] = last_seq
        self._store.finish_reset(record)
        return last_seq

    def _reset_event_content(
        self,
        record: dict[str, Any],
        session_id: str,
        new_epoch: str,
        persisted_highwater: int,
    ) -> None:
        """切换 EventStore epoch 并把 reset 边界序号写回 reset 记录。"""
        if self._events is None:
            return
        if self._events.current_epoch(session_id) != new_epoch:
            self._events.ensure_sequence(
                session_id,
                persisted_highwater,
                session_epoch=new_epoch,
            )
            boundary = self._events.reset(session_id, new_epoch)
            record["last_event_seq"] = boundary.seq
        else:
            record["last_event_seq"] = self._events.last_seq(session_id)

    def _resume_incomplete_resets(self) -> None:
        """服务启动时继续 epoch 已切换但尚未完成的 reset 清理。"""
        for record in self._store.pending_reset_records():
            try:
                current_epoch = self._store.current_epoch(
                    str(record["session_id"]),
                    create=False,
                )
                if current_epoch != record.get("new_epoch"):
                    self._store.abandon_reset(
                        record,
                        (
                            "epoch_barrier_not_established"
                            if current_epoch == record.get("old_epoch")
                            else "reset_record_lost_epoch_ownership"
                        ),
                    )
                    continue
                self._complete_reset_cleanup(record)
            except (OSError, TypeError, ValueError):
                logger.exception(
                    "Incomplete reset cleanup remains pending session=%s reset_id=%s",
                    record.get("session_id"),
                    record.get("reset_id"),
                )

    async def resume_pending_recoveries(self) -> int:
        """启动时重建未终态 TaskRun 的监督状态并发布可观察恢复事件。"""
        resumed = 0
        for session_id in self._store.task_run_session_ids():
            async with self._store.lock_for(session_id):
                session = self._store.get_or_create(
                    session_id,
                    self.available_tools,
                )
                run = self._recovery_supervisor.resume_after_restart(session)
                if run is None:
                    continue
                self._store.save_task_run(session)
                self._publisher.emit(
                    session_id,
                    "recovery_resumed",
                    {
                        "task_id": run.get("task_id"),
                        "attempt_id": run.get("current_attempt_id"),
                        "checkpoint_id": run.get("checkpoint_id"),
                        "disposition": run.get("active_disposition"),
                        "side_effect_state": run.get("side_effect_state"),
                        "next_action": run.get("next_action"),
                        "status": run.get("status"),
                    },
                )
                resumed += 1
        return resumed

    async def _cancel_active_tasks(self, session_id: str) -> bool:
        """取消并等待该会话仍在运行的 `/chat` 任务，返回是否取消了任何任务。

        会话生命周期操作（reset/interrupt）必须先把仍在 await LLM/工具的旧
        turn 真正取消并 await 到它退出，否则旧 turn 之后的 `save(session)` 会
        把已被重置/中断的会话重新写回，造成"会话复活"（§14.2）。排除当前
        协程自身，避免自取消。
        """
        return await self._activity.cancel_others(session_id)

    async def reset(self, session_id: str) -> ResetResponse:
        """清空指定会话。

        先取消该会话仍在运行的 `/chat` 任务并等待其退出，再在持锁状态下清空
        会话；否则旧 turn 返回后的 `save()` 会把已重置的会话重新写回磁盘。
        """
        await self._cancel_active_tasks(session_id)
        async with self._store.lock_for(session_id):
            old_session = self._store.get_or_create(session_id, self.available_tools)
            event_highwater = max(
                old_session.history_event_counter,
                self._events.last_seq(session_id) if self._events is not None else 0,
            )
            try:
                record = self._store.begin_reset(
                    session_id,
                    last_event_seq=event_highwater,
                )
            except (OSError, TypeError, ValueError) as exc:
                logger.exception("Session reset epoch barrier failed session=%s", session_id)
                return ResetResponse(
                    ok=False,
                    session_id=session_id,
                    session_epoch=old_session.session_epoch,
                    last_event_seq=event_highwater,
                    error_code="reset_epoch_barrier_failed",
                    text=f"无法建立会话重置隔离屏障：{exc}",
                )
            try:
                last_seq = self._complete_reset_cleanup(record)
            except (OSError, TypeError, ValueError):
                logger.exception(
                    "Session reset cleanup pending session=%s reset_id=%s",
                    session_id,
                    record.get("reset_id"),
                )
                current_epoch = str(record["new_epoch"])
                last_seq = (
                    self._events.last_seq(session_id)
                    if self._events is not None
                    else event_highwater
                )
                return ResetResponse(
                    ok=True,
                    session_id=session_id,
                    session_epoch=current_epoch,
                    last_event_seq=last_seq,
                    cleanup_pending=True,
                    text="会话已隔离；后台清理将在重启或下次重试时继续",
                )
        logger.info(
            "Session reset through AgentApplication session=%s epoch=%s last_seq=%d",
            session_id,
            record["new_epoch"],
            last_seq,
        )
        return ResetResponse(
            ok=True,
            session_id=session_id,
            session_epoch=str(record["new_epoch"]),
            last_event_seq=last_seq,
        )

    async def interrupt(
        self,
        session_id: str,
        *,
        cause: InterruptCause = "user_interrupted",
    ) -> InterruptResponse:
        """真正中断该会话仍在运行的 `/chat` 请求，并丢弃其后续输出。

        前端"停止"按钮此前只是断开自己的 HTTP 连接：后端的 `TurnDriver.run`
        循环（自动执行的静默工具，如 grep/read）会继续跑完整轮，并持续把
        新事件写进 `EventStore`。等用户发出下一条消息时，这些属于已停止
        旧任务的事件会被一起拉取并误渲染成新对话的内容。这里改为取消
        该会话当前登记的 `asyncio.Task`，让 `CancelledError` 在下一个
        await 点（LLM 调用/工具执行）处中断循环，并清理任何尚未回传的
        pending 工具调用占位，使会话立刻能接受新消息。

        `_active_tasks[session_id]` 是一个集合而不是单个任务：如果用户在
        前一个请求仍卡在 per-session 锁等待时就又发了一条消息（或快速点了
        多次"停止"），会话上会短暂同时存在多个 `submit_user_turn` 任务。
        只取消其中一个（尤其是若取了最新、可能只是在排队等锁的那个）会让
        真正持锁运行的旧任务永远不会被取消，导致锁一直被占用，包括这次
        interrupt 自己后面要拿的锁也会卡死。所以这里要把所有未完成的都
        取消掉。
        Args:
            session_id: 需要中断的会话标识。
            cause: 用户主动停止或客户端等待超时。

        Returns:
            取消结果与最新事件游标。
        """
        cancelled = await self._cancel_active_tasks(session_id)

        discarded = 0
        map_checkpoint_created = False
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            if session.map_task_state.status == "running":
                session.map_task_state.make_checkpoint(
                    cause,
                    pause_kind=cause,
                )
                map_checkpoint_created = True
            had_pending_plan = session.pending_plan is not None
            session.pending_plan = None
            if session.pending_turn_id is not None:
                frames = {frame.id: frame for frame in session.agent_stack}
                for tool_use_id in sorted(session.pending_tool_call_ids):
                    metadata = session.pending_tool_calls.get(tool_use_id, {})
                    frame = frames.get(str(metadata.get("frame_id", "")))
                    if frame is None:
                        continue
                    frame.messages.append(
                        _tool_message(
                            tool_use_id,
                            (
                                "客户端等待超时并中断了当前请求，该工具调用结果未回传。"
                                if cause == "client_timeout"
                                else "用户中断了当前请求，该工具调用结果未回传。"
                            ),
                            is_error=True,
                        )
                    )
                    discarded += 1
                session.clear_pending()
                self._store.save(session)
                if self._recovery is not None and not map_checkpoint_created:
                    self._recovery.clear(session_id)
            elif had_pending_plan or map_checkpoint_created:
                self._store.save(session)
            if isinstance(session.task_run, dict):
                try:
                    problem = self._recovery_supervisor.problem(
                        session,
                        error_code=(
                            "response_transport_lost" if cause == "client_timeout" else "user_stop"
                        ),
                        text=(
                            "客户端连接中断；任务检查点已保留"
                            if cause == "client_timeout"
                            else "用户已停止当前执行；任务检查点已保留，可显式恢复"
                        ),
                        side_effect_state="none",
                    )
                    session.task_run["last_problem"] = problem
                    self._store.save_task_run(session)
                except ValueError:
                    logger.exception(
                        "Unable to record interrupted TaskRun session=%s",
                        session_id,
                    )

        self._publisher.emit(
            session_id,
            "turn_interrupted",
            {
                "cancelled": cancelled,
                "pending_discarded": discarded,
                "cause": cause,
            },
        )
        last_seq = self._events.last_seq(session_id) if self._events is not None else 0
        if self._recovery is not None and map_checkpoint_created:
            self._recovery.write(
                session_id,
                None,
                last_seq,
                session.map_task_state.checkpoint,
                session_epoch=session.session_epoch,
            )
        logger.info(
            "Turn interrupted session=%s cause=%s cancelled=%s pending_discarded=%d last_seq=%d",
            session_id,
            cause,
            cancelled,
            discarded,
            last_seq,
        )
        return InterruptResponse(ok=True, cancelled=cancelled, last_event_seq=last_seq)

    async def discard_pending(self, session_id: str) -> ChatResponse:
        """放弃当前会话待回传的前端工具调用，保留其余会话历史。

        为每个待回应的 `tool_use_id` 写入一条"用户放弃"的占位 `tool` 消息，
        然后清空 `pending_turn_id`，使会话恢复到可接受新用户消息的状态。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            if session.pending_turn_id is None:
                return ChatErrorResponse(
                    text="当前会话没有等待回传的工具调用",
                    error_code="pending_tool_results",
                    disposition="wait_frontend",
                    retryable=True,
                )

            frames = {frame.id: frame for frame in session.agent_stack}
            discarded = 0
            for tool_use_id in sorted(session.pending_tool_call_ids):
                metadata = session.pending_tool_calls.get(tool_use_id, {})
                frame = frames.get(str(metadata.get("frame_id", "")))
                if frame is None:
                    continue
                frame.messages.append(
                    _tool_message(tool_use_id, "用户放弃了该工具调用的结果回传。", is_error=True)
                )
                discarded += 1

            session.clear_pending()
            self._store.save(session)
            response = ChatFinalResponse(
                text=f"已放弃 {discarded} 个待回传的工具调用，可以继续发送新消息。"
            )
            self._publisher.record_recovery(session, response)
            self._publisher.emit(session_id, "pending_discarded", {"count": discarded})
            logger.info("Pending tool calls discarded session=%s count=%d", session_id, discarded)
            return response

    async def set_effort(self, session_id: str, effort: str) -> None:
        """Set session effort without starting a model turn.

        持锁修改：否则会与正在 await LLM 的活跃 turn 抢同一个 Session，导致
        配置在一轮中途被改、响应与上下文错配（§会话锁边界）。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.effort = effort
            self._store.save(session)
        self._publisher.emit(session_id, "config_changed", {"effort": effort})
        logger.info("Session effort changed session=%s effort=%s", session_id, effort)

    async def resume_paused_map_task(self, session_id: str) -> dict[str, Any]:
        """显式恢复暂停的地图任务，但不在命令请求内静默执行写批次。

        仅在任务处于 ``paused`` 状态且无 pending 前端工具调用时允许恢复。
        恢复操作本身只修改状态并持久化，实际的批次执行由下一轮 chat 请求
        驱动（通过一次性 resume authorization 路径），避免在此处隐式触发
        长时间运行的写操作。

        Args:
            session_id: 需要恢复的会话标识。

        Returns:
            恢复后的地图任务状态和检查点摘要。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            state = session.map_task_state
            # 前置条件：任务必须处于暂停状态且有可用检查点
            if state.status != "paused" or state.checkpoint is None:
                return {
                    "resumed": False,
                    "status": state.status,
                    "reason": "map_task_not_paused",
                }
            # 前置条件：不能有尚未回传的前端工具调用
            if session.pending_turn_id is not None:
                return {
                    "resumed": False,
                    "status": state.status,
                    "reason": "front_tools_still_pending",
                }
            task_lineage = session.map_task_lineage
            lineage_id = (
                str(task_lineage.get("lineage_id", ""))
                or str(session.map_request_scope.lineage_id)
                or state.task_id
            )
            resume_map_task(state, lineage_id=lineage_id)
            if isinstance(session.task_run, dict):
                session.task_run["status"] = "recovering"
                session.task_run["active_disposition"] = "retry_new_attempt"
                session.task_run["next_action"] = {
                    "action": "resume_from_checkpoint",
                    "owner": "backend",
                }
                session.task_run["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._store.save_task_run(session)
            self._store.save(session)
            result = {
                "resumed": True,
                "status": state.status,
                "task_id": state.task_id,
                "checkpoint": state.checkpoint,
            }
        self._publisher.emit(session_id, "map_task_resumed", {"task_id": result["task_id"]})
        logger.info("Paused map task explicitly resumed session=%s", session_id)
        return result

    async def cancel_map_task(self, session_id: str) -> dict[str, Any]:
        """显式取消地图任务并清除尚未执行的地图批次。

        适用于用户主动中止正在运行或暂停的地图任务。仅当任务处于
        ``running`` 或 ``paused`` 状态时才允许取消，且要求当前无 pending
        前端工具调用，防止取消操作与正在处理的工具结果产生竞态。

        Args:
            session_id: 需要取消地图任务的会话标识。

        Returns:
            是否取消成功及取消后的任务状态。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            state = session.map_task_state
            # 前置条件：只有运行中或暂停中的任务才能被取消
            if state.status not in {"running", "paused"}:
                return {
                    "cancelled": False,
                    "status": state.status,
                    "reason": "map_task_not_active",
                }
            # 前置条件：不能有尚未回传的前端工具调用
            if session.pending_turn_id is not None:
                return {
                    "cancelled": False,
                    "status": state.status,
                    "reason": "front_tools_still_pending",
                }
            task_id = state.task_id
            state.cancel("cancelled_by_user")
            session.map_request_scope = invalidate_completion_candidate(session.map_request_scope)
            session.map_task_lineage = {
                **session.map_task_lineage,
                "completion_candidate": False,
            }
            if isinstance(session.task_run, dict):
                self._recovery_supervisor.mark_terminal(
                    session,
                    outcome="cancelled",
                    authorized_by="explicit_cancel",
                )
                self._store.save_task_run(session)
            self._store.save(session)
            result = {
                "cancelled": True,
                "status": state.status,
                "task_id": task_id,
            }
        self._publisher.emit(session_id, "map_task_cancelled", {"task_id": task_id})
        logger.info("Map task explicitly cancelled session=%s task_id=%s", session_id, task_id)
        return result

    async def set_output_style(self, session_id: str, output_style: str) -> None:
        """Set session output style without starting a model turn."""
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.output_style = output_style
            self._store.save(session)
        self._publisher.emit(session_id, "config_changed", {"output_style": output_style})
        logger.info(
            "Session output style changed session=%s output_style=%s", session_id, output_style
        )
