"""无领域依赖的有界 TurnDriver。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.orchestrator.turn.contracts import (
    CompleteTurn,
    ContinueModel,
    ErrorTurnOutcome,
    FailTurn,
    FinalTurnOutcome,
    PausedTurnOutcome,
    PauseWorkflow,
    SuspendForFrontend,
    ToolCallsTurnOutcome,
    TurnDirective,
    TurnOutcome,
)


class TurnPipeline(Protocol):
    """由领域组合实现的 turn pipeline 端口。"""

    async def run(self, **arguments: Any) -> TurnOutcome:
        """执行一个有界 turn 并返回 canonical outcome。"""
        ...


@dataclass(frozen=True, slots=True)
class TurnDriver:
    """拥有唯一有界 transition 循环，不知道 Map、工具名或 Session 字段。"""

    pipeline: TurnPipeline

    async def run(self, **arguments: Any) -> TurnOutcome:
        """启动注入的领域策略；HTTP 与持久化协调位于状态机之外。"""
        return await self.pipeline.run(**arguments)

    @staticmethod
    async def drive(
        *,
        maximum: int,
        transition: Callable[[int], Awaitable[TurnDirective]],
        exhausted: Callable[[], TurnOutcome],
    ) -> TurnOutcome:
        """执行封闭的 TurnDirective 状态机并统一拥有循环上限。"""
        if maximum <= 0:
            raise ValueError("turn driver maximum must be positive")
        for transition_index in range(maximum):
            directive = await transition(transition_index)

            if isinstance(directive, ContinueModel):
                continue
            if isinstance(directive, CompleteTurn):
                return FinalTurnOutcome(
                    text=directive.text,
                    metadata=directive.metadata,
                )
            if isinstance(directive, SuspendForFrontend):
                return ToolCallsTurnOutcome(
                    turn_id=directive.turn_id,
                    calls=directive.calls,
                    text=directive.text,
                )
            if isinstance(directive, PauseWorkflow):
                return PausedTurnOutcome(
                    reason_code=directive.reason_code,
                    checkpoint=directive.checkpoint,
                    text=directive.user_text,
                )
            if isinstance(directive, FailTurn):
                return ErrorTurnOutcome(
                    error_code=directive.error_code,
                    text=directive.text,
                    retryable=directive.retryable,
                    details=directive.details,
                )
            raise RuntimeError(f"unapplied turn directive: {directive.kind}")
        return exhausted()
