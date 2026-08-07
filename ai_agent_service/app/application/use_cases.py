"""HTTP/MCP 之外的应用用例边界。

每个用例通过显式端口（协议）接收其依赖，而非通过通用的
应用门面。这使得组合根可以零门面地构造用例。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.api.schemas import (
    ChatResponse,
    InterruptCause,
    InterruptResponse,
    ResetResponse,
    SessionHistoryResponse,
)
from app.application.response_mapping import chat_response_from_payload, map_turn_outcome
from app.application.submission.tool_result_submission import ToolResultSubmissionUseCase
from app.application.submission.user_submission import UserSubmissionUseCase
from app.orchestrator.turn.contracts import TurnOutcome

# ── 端口（协议），每个用例声明其自身的最小依赖 ──


class SessionLifecyclePort(Protocol):
    """重置、中断、恢复与会话设置。"""

    async def reset(self, session_id: str) -> ResetResponse: ...
    async def interrupt(self, session_id: str, *, cause: InterruptCause) -> InterruptResponse: ...
    async def set_effort(self, session_id: str, effort: str) -> None: ...
    async def set_output_style(self, session_id: str, output_style: str) -> None: ...


class MapTaskPort(Protocol):
    """暂停/恢复/取消地图任务。"""

    async def resume_paused_map_task(self, session_id: str) -> dict[str, Any]: ...
    async def cancel_map_task(self, session_id: str) -> dict[str, Any]: ...


class HistoryPort(Protocol):
    """读取会话历史。"""

    def session_history(
        self, session_id: str, *, limit: int, before: int
    ) -> SessionHistoryResponse: ...


class CompactionPort(Protocol):
    """压缩会话上下文。"""

    async def compact(
        self, session_id: str, *, keep_recent: int, use_llm: bool | None
    ) -> dict[str, Any]: ...


class RecoveryPort(Protocol):
    """继续持久恢复 journal。"""

    async def resume_pending_recoveries(self) -> int: ...


# ── 用例 ──


@dataclass(frozen=True, slots=True)
class ResumeUseCase:
    """从当前 schema 的持久 checkpoint 恢复 Map workflow。"""

    _service: MapTaskPort

    async def execute(self, session_id: str) -> dict[str, Any]:
        """恢复一个已暂停 workflow，不接受历史格式。"""
        return await self._service.resume_paused_map_task(session_id)


@dataclass(frozen=True, slots=True)
class ResponseMappingUseCase:
    """在 canonical outcome、durable payload 与 API DTO 间映射。"""

    def from_turn(self, outcome: TurnOutcome) -> ChatResponse:
        """映射一个封闭 TurnOutcome。"""
        return map_turn_outcome(outcome)

    def from_payload(self, payload: dict[str, Any]) -> ChatResponse:
        """验证并恢复一个当前 schema 持久响应。"""
        return chat_response_from_payload(payload)


@dataclass(frozen=True, slots=True)
class InterruptionUseCase:
    """中断当前 Session 的活跃请求。"""

    _service: SessionLifecyclePort

    async def execute(self, session_id: str, cause: InterruptCause) -> InterruptResponse:
        """提交中断命令并返回持久边界。"""
        return await self._service.interrupt(session_id, cause=cause)


@dataclass(frozen=True, slots=True)
class ResetUseCase:
    """切换 Session epoch 并清理旧 epoch 资源。"""

    _service: SessionLifecyclePort

    async def execute(self, session_id: str) -> ResetResponse:
        """执行不可回退的 Session reset。"""
        return await self._service.reset(session_id)


@dataclass(frozen=True, slots=True)
class HistoryUseCase:
    """读取有界、可展示的 Session 历史。"""

    _service: HistoryPort

    def execute(self, session_id: str, *, limit: int, before: int) -> SessionHistoryResponse:
        """返回指定历史窗口。"""
        return self._service.session_history(session_id, limit=limit, before=before)


@dataclass(frozen=True, slots=True)
class CompactionUseCase:
    """执行显式 Session 上下文压缩。"""

    _service: CompactionPort

    async def execute(
        self,
        session_id: str,
        *,
        keep_recent: int,
        use_llm: bool | None,
    ) -> dict[str, Any]:
        """压缩上下文并返回边界摘要。"""
        return await self._service.compact(
            session_id,
            keep_recent=keep_recent,
            use_llm=use_llm,
        )


@dataclass(frozen=True, slots=True)
class SessionSettingsUseCase:
    """修改当前 Session 的非秘密运行偏好。"""

    _service: SessionLifecyclePort

    async def set_effort(self, session_id: str, effort: str) -> None:
        """更新 effort。"""
        await self._service.set_effort(session_id, effort)

    async def set_output_style(self, session_id: str, output_style: str) -> None:
        """更新输出风格。"""
        await self._service.set_output_style(session_id, output_style)


@dataclass(frozen=True, slots=True)
class MapTaskControlUseCase:
    """显式取消持久地图任务。"""

    _service: MapTaskPort

    async def cancel(self, session_id: str) -> dict[str, Any]:
        """取消当前地图任务。"""
        return await self._service.cancel_map_task(session_id)


@dataclass(frozen=True, slots=True)
class RecoveryUseCase:
    """启动时继续当前 schema 的恢复 journal。"""

    _service: RecoveryPort

    async def resume_pending(self) -> int:
        """继续所有可恢复 journal 并返回数量。"""
        return await self._service.resume_pending_recoveries()


@dataclass(frozen=True, slots=True)
class ApplicationUseCases:
    """路由层只依赖的显式用例集合。"""

    user_submission: UserSubmissionUseCase
    tool_result_submission: ToolResultSubmissionUseCase
    resume: ResumeUseCase
    interruption: InterruptionUseCase
    reset: ResetUseCase
    history: HistoryUseCase
    compaction: CompactionUseCase
    settings: SessionSettingsUseCase
    map_tasks: MapTaskControlUseCase
    recovery: RecoveryUseCase
    response_mapping: ResponseMappingUseCase
