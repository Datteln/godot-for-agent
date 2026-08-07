"""提供由 TurnDriver 驱动的 Map 领域 pipeline 入口。"""

from __future__ import annotations

from collections.abc import Callable

from app.agents.types import EffortLevel
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import LLMProvider
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_contracts import MapResponseMode
from app.orchestrator.map_turn.budgets import _map_turn_exhausted
from app.orchestrator.map_turn.contracts import AgentPromptFactory
from app.orchestrator.map_turn.execution import MapTransitionEngine
from app.orchestrator.map_turn.runtime import (
    MapTurnContext,
    MapTurnOptions,
    MapTurnRuntime,
    MapTurnServices,
)
from app.orchestrator.turn.contracts import TurnOutcome
from app.orchestrator.turn.driver import TurnDriver
from app.permissions.engine import SessionAllowGrant
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext


class MapTurnPolicy:
    """把 Map 领域依赖组合成一个由 TurnDriver 控制的有限状态机。"""

    @staticmethod
    async def run(
        session: Session,
        llm: LLMProvider,
        security: SecuritySettings,
        tool_ctx: ToolContext,
        max_turns: int,
        session_allow: set[SessionAllowGrant] | None = None,
        agent_prompt_factory: AgentPromptFactory | None = None,
        model_selector: Callable[[EffortLevel], str | None] | None = None,
        model_override: str | None = None,
        thinking_budget_selector: Callable[[EffortLevel], int | None] | None = None,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
        cache_engine: CacheDecisionEngine | None = None,
        cache_metrics: CacheMetricsCollector | None = None,
        context_token_limit: int | None = None,
        map_worker_structured_output_enabled: bool = True,
        map_worker_response_contract_mode: MapResponseMode = "prompt_only",
        map_worker_structured_correction_limit: int = 2,
        map_worker_structured_thinking_budget: int = 0,
    ) -> TurnOutcome:
        """创建一次运行上下文并把唯一循环所有权交给 TurnDriver。"""
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        services = MapTurnServices(
            llm=llm,
            security=security,
            tool_context=tool_ctx,
            session_allow=session_allow,
            prompt_factory=agent_prompt_factory,
            model_selector=model_selector,
            model_override=model_override,
            thinking_budget_selector=thinking_budget_selector,
            event_callback=event_callback,
            cache_engine=cache_engine,
            cache_metrics=cache_metrics,
        )
        options = MapTurnOptions(
            max_turns=max_turns,
            context_token_limit=context_token_limit,
            structured_output_enabled=map_worker_structured_output_enabled,
            response_contract_mode=map_worker_response_contract_mode,
            structured_correction_limit=map_worker_structured_correction_limit,
            structured_thinking_budget=map_worker_structured_thinking_budget,
        )
        runtime = MapTurnRuntime(
            session=session,
            delegate_artifact_store=DelegateArtifactStore(
                tool_ctx.security.project_root,
                session.session_id,
                session.session_epoch,
            ),
        )
        engine = MapTransitionEngine(MapTurnContext(services, options, runtime))
        driver_turn_limit = max_turns + max(0, map_worker_structured_correction_limit) + 1
        return await TurnDriver.drive(
            maximum=driver_turn_limit,
            transition=engine.transition,
            exhausted=lambda: _map_turn_exhausted(session, max_turns),
        )
