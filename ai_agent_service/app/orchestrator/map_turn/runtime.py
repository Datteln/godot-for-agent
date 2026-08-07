"""定义一次 Map turn 所需的显式依赖、选项与阶段结果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.agents.types import EffortLevel, Frame
from app.llm.cache_decision_engine import CacheDecision, CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import AssistantTurn, LLMProvider
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_contracts import MapResponseMode
from app.orchestrator.map_turn.contracts import AgentPromptFactory
from app.permissions.engine import PermissionContext, SessionAllowGrant
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext

EventCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class MapTurnServices:
    """保存一个 Map turn 的外部服务依赖。"""

    llm: LLMProvider
    security: SecuritySettings
    tool_context: ToolContext
    session_allow: set[SessionAllowGrant] | None
    prompt_factory: AgentPromptFactory | None
    model_selector: Callable[[EffortLevel], str | None] | None
    model_override: str | None
    thinking_budget_selector: Callable[[EffortLevel], int | None] | None
    event_callback: Callable[[str, dict[str, object]], None] | None
    cache_engine: CacheDecisionEngine | None
    cache_metrics: CacheMetricsCollector | None


@dataclass(frozen=True, slots=True)
class MapTurnOptions:
    """保存一个 Map turn 的经过验证的运行策略。"""

    max_turns: int
    context_token_limit: int | None
    structured_output_enabled: bool
    response_contract_mode: MapResponseMode
    structured_correction_limit: int
    structured_thinking_budget: int


@dataclass(slots=True)
class MapTurnRuntime:
    """保存一次 Map turn 的领域聚合引用与临时计数。"""

    session: Session
    delegate_artifact_store: DelegateArtifactStore
    frame_turns: dict[str, int] = field(default_factory=dict)
    frame_edit_map_turns: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MapTurnContext:
    """组合单步转换需要的类型化依赖，不存储阶段临时值。"""

    services: MapTurnServices
    options: MapTurnOptions
    runtime: MapTurnRuntime


@dataclass(frozen=True, slots=True)
class MapModelStep:
    """表示一次已接受的模型响应及其 Map 执行上下文。"""

    loop_number: int
    frame: Frame
    turn: AssistantTurn
    visible_effective_tools: list[str]
    persistent_map_budget: bool
    final_structured_turn: bool
    resolved_model: str | None
    effective_thinking_budget: int | None
    cache_decision: CacheDecision | None


@dataclass(frozen=True, slots=True)
class MapToolStep:
    """表示一个完成响应路由、等待执行工具的阶段结果。"""

    frame: Frame
    turn: AssistantTurn
    visible_effective_tools: list[str]
    persistent_map_budget: bool
    permission_context: PermissionContext
