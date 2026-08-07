"""协调 Map turn 的一次显式阶段转换。"""

from __future__ import annotations

from dataclasses import dataclass

from app.orchestrator.map_turn.model_cycle import run_model_cycle
from app.orchestrator.map_turn.response_routing import route_model_response
from app.orchestrator.map_turn.runtime import MapModelStep, MapToolStep, MapTurnContext
from app.orchestrator.map_turn.tool_cycle import execute_tool_cycle
from app.orchestrator.turn.contracts import ContinueModel, TurnDirective, directive_from_outcome


@dataclass(frozen=True, slots=True)
class MapTransitionEngine:
    """通过明确的阶段结果推进一个 Map turn transition。"""

    context: MapTurnContext

    async def transition(self, loop_index: int) -> TurnDirective:
        """执行一次模型、路由与可选工具阶段并返回封闭 directive。"""
        result = await run_model_cycle(self.context, loop_index)
        if isinstance(result, MapModelStep):
            result = await route_model_response(self.context, result)
        if isinstance(result, MapToolStep):
            result = await execute_tool_cycle(self.context, result)
        if isinstance(result, ContinueModel):
            return result
        return directive_from_outcome(result)
