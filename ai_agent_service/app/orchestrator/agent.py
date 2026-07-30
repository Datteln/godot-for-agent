"""Agent 编排循环：`query_loop` 内核（§13）。

`run_turn()` 驱动当前活跃帧反复调用 `LLMProvider.chat()`：
- `delegate`/`delegate_many` 创建并压入子 agent 帧；子帧结束后把摘要
  回填父帧对应的 tool 调用结果，继续驱动父帧（M2+）；
- 其余 `tool_calls` 中，server 工具按 `is_concurrency_safe` 分组执行：
  并发安全的一组用 `asyncio.gather` 并发执行，其余按原始顺序串行执行；
  执行结果再统一按 `tool_calls` 原始顺序 append 回 `frame.messages`；
- front 工具收集为待前端执行/确认的 `FrontToolCall`，整帧挂起并返回；
- `search_tools` 命中的 deferred 工具记入 `frame.active_deferred_tools`，
  仅在本帧内生效，不跨帧继承；
- 无 `tool_calls` 时结束当前帧；根帧结束即整轮结束，子帧结束则把摘要
  回填父帧并继续驱动父帧；
- 每轮 `llm.chat()` 的 `temperature` 由 `_resolve_effort`/
  `_resolve_temperature` 按 `Session.effort`/`AgentDefinition.effort`
  解析得到（§6.5）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from app.agents.bundled import get_agent
from app.agents.types import EFFORT_LEVELS, AgentDefinition, EffortLevel, Frame
from app.llm.cache_decision_engine import CacheDecision, CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector, CacheMetricsSnapshot
from app.llm.provider import (
    AssistantTurn,
    LLMError,
    LLMProvider,
    ResponseContract,
    ToolCallRequest,
)

# ── 本轮整改：将 history 体积控制工具下沉到独立模块，避免 agent.py 膨胀 ──
from app.history_bounds import (
    bounded_history_value as _bounded_history_value,
    bounded_tool_message_body as _bounded_tool_message_body,
    json_char_size as _json_char_size,
    summarize_history_text as _summarize_history_text,
)
from app.permissions.engine import PermissionContext, SessionAllowGrant, check
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, tools_for

# ── 本轮整改：新增模块导入，支持 capability contract、artifact 存储、stage contract ──
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_capabilities import map_tools_for_stage
from app.orchestrator.map_contracts import (
    MAP_WORKER_NEXT_STAGES,
    MAP_WORKER_RESULT_SCHEMA,
    MAP_WORKER_STAGES,
    MapResponseMode,
    arm_map_worker_structured_completion,
    map_worker_required_fields,
    render_map_worker_response_guidance,
    specialized_map_worker_schema,
    validate_map_worker_schema,
)
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    MAP_VALIDATION_TOOL_NAMES,
    MAP_WRITE_TOOL_NAMES,
    MAP_WORKER_MODE_STAGES,
    build_dynamic_map_worker,
    is_map_worker_write_mode,
    is_map_write_tool,
    validate_map_write_args,
)
from app.orchestrator.map_progress import (
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    latest_map_revision,
    cached_validation_result,
    map_pause_message,
    map_platform_plan_call_error,
    map_write_stage_error,
    platform_write_requires_validation,
    remember_map_tool_failure,
    repeated_map_tool_failure_error,
    validation_call_error,
)
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    STRUCTURED_REPAIR_MAX_ATTEMPTS,
    record_semantic_retry,
    record_plan_attempt,
    retry_pause_report,
    safe_structured_diagnostic,
    structured_error_category,
    structured_repair_actions,
)

# 本轮整改：子帧创建统一走 frame_factory，确保 history_anchor / stage_contract 等字段一致
from app.orchestrator.frame_factory import create_child_frame
from app.orchestrator.frame_contracts import validate_frame_result
from app.orchestrator.evidence import scoped_evidence
from app.orchestrator.map_resources import normalize_edit_map_resources
from app.orchestrator.plan_scheduler import PlanGraph, PlanGraphError
from app.orchestrator.runtime_contracts import PlanStepResult, UnapprovedWriteRejection
from app.orchestrator.map_workflow import increment_map_counter, replace_map_state_field

MAX_AGENT_DEPTH = 4
EVENT_TEXT_PREVIEW_CHARS = 24_000
EVENT_MATCH_PREVIEW_ITEMS = 20
NOOP_SEARCH_TOOLS_HINT_THRESHOLD = 2
_INTEGER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NUMBER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")

logger = logging.getLogger(__name__)

AgentPromptFactory = Callable[[AgentDefinition, str], Awaitable[str]]


def _map_stage_contract(
    agent: AgentDefinition,
    task_text: str,
    worker_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据可信委派元数据构造地图子 Frame 合同。

    本轮整改新增：合同在子帧创建时一次性绑定 stage / target_path /
    map_revision / region，后续 _map_structured_output_error 据此做
    一致性校验，防止 worker 越权写入非预期的图层或 revision。
    """
    # 动态 worker 以 worker_spec.mode 为准（mode→stage 映射由 map_contracts 集中维护），
    # 静态 agent 则直接读取 agent.map_stage 元数据
    stage = agent.map_stage
    if isinstance(worker_spec, dict):
        stage = MAP_WORKER_MODE_STAGES.get(str(worker_spec.get("mode", "")))
    if stage is None:
        return {}
    # 尝试从 task_text 解析 JSON 载荷，提取 target/revision/region 等合同字段
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(task_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
    approved_batch = (
        worker_spec.get("approved_batch")
        if isinstance(worker_spec, dict) and isinstance(worker_spec.get("approved_batch"), dict)
        else {}
    )
    contract: dict[str, Any] = {"stage": stage}
    target = payload.get("target_path", approved_batch.get("target_path"))
    if isinstance(target, str) and target.strip():
        contract["target_path"] = target.strip()
    # 兼容 map_revision 与旧字段 required_revision
    revision = payload.get(
        "map_revision",
        payload.get("required_revision", approved_batch.get("map_revision")),
    )
    if isinstance(revision, int) and not isinstance(revision, bool):
        contract["map_revision"] = revision
    region = payload.get("region")
    if isinstance(region, dict):
        # 只保留整数坐标，过滤掉非数值脏数据
        contract["region"] = {
            str(key): value
            for key, value in region.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    if approved_batch:
        contract["approved_batch_ref"] = approved_batch.get("artifact_ref")
        contract["approved_batch_id"] = approved_batch.get("batch_id")
    return contract


@dataclass(frozen=True)
class FrontToolCall:
    """一次需要前端执行/确认的工具调用（响应 `calls` 数组的一项，§14）。

    Attributes:
        id: 工具调用 id，前端回传 `tool_results` 时需带回。
        name: 工具名。
        input: 工具入参（已 `json.loads`）。
        needs_confirm: 是否需要前端预览确认（权限决策为 `ask`）。
        frame_id: 来源帧 id，前端回传结果时用于路由。
        agent: 来源帧绑定的 agent 名。
        render_kind: 前端预览渲染类型（`diff`/`list`/`run`/`log`/`map` 等）。
    """

    id: str
    name: str
    input: dict[str, Any]
    needs_confirm: bool
    frame_id: str
    agent: str
    render_kind: str | None


def _queued_front_call(call: FrontToolCall) -> dict[str, Any]:
    """把前端调用转换为可持久化批次项。"""
    return {
        "id": call.id,
        "name": call.name,
        "input": call.input,
        "needs_confirm": call.needs_confirm,
        "frame_id": call.frame_id,
        "agent": call.agent,
        "render_kind": call.render_kind,
    }


@dataclass(frozen=True)
class ToolCallsResult:
    """`run_turn` 因产出 front 工具调用而挂起当前轮次。"""

    turn_id: str
    text: str | None
    calls: list[FrontToolCall] = field(default_factory=list)
    type: Literal["tool_calls"] = "tool_calls"


@dataclass(frozen=True)
class FinalResult:
    """`run_turn` 正常结束并产出最终文本。"""

    text: str
    type: Literal["final"] = "final"


@dataclass(frozen=True)
class ErrorResult:
    """`run_turn` 因 LLM 调用失败或达到轮数上限而终止。"""

    text: str
    error_code: str = "internal_error"
    type: Literal["error"] = "error"


StepResult = ToolCallsResult | FinalResult | ErrorResult


def _resolve_model(agent: AgentDefinition) -> str | None:
    """把 `AgentDefinition.model` 解析为传给 `LLMProvider.chat()` 的模型名。

    Args:
        agent: 当前活跃帧绑定的 agent 定义。

    Returns:
        `agent.model` 为 `None` 或 `"inherit"` 时返回 None（使用 provider
        默认模型）；否则原样返回该模型名。
    """
    if agent.model is None or agent.model == "inherit":
        return None
    return agent.model


def _resolve_model_for_effort(
    agent: AgentDefinition,
    effort: EffortLevel,
    model_selector: Callable[[EffortLevel], str | None] | None,
) -> str | None:
    """Resolve the model for the current frame.

    Agent definitions with an explicit model keep priority. Inherited models can be
    selected by effort so quick/verify can use cheaper models while deep can use a
    stronger one.
    """
    agent_model = _resolve_model(agent)
    if agent_model is not None:
        return agent_model
    if model_selector is None:
        return None
    return model_selector(effort)


def _resolve_request_model(
    agent: AgentDefinition,
    effort: EffortLevel,
    model_selector: Callable[[EffortLevel], str | None] | None,
    model_override: str | None,
) -> str | None:
    """以请求级覆盖为最高优先级解析本次调用的模型。"""
    return model_override or _resolve_model_for_effort(agent, effort, model_selector)


# effort 档位 -> 采样温度（§6.5）；`verify` 取 0 以追求确定性复核结果。
EFFORT_TEMPERATURE: dict[EffortLevel, float] = {
    "quick": 0.2,
    "standard": 0.7,
    "deep": 0.7,
    "verify": 0.0,
    "advisor": 0.3,
}

# effort 档位 -> thinking token 预算；verify 设为 0 关闭 thinking 以保证确定性；
# -1 表示"不限预算"（沿用 enable_thinking:true 无 budget 的原有行为）。
EFFORT_THINKING_BUDGET: dict[EffortLevel, int] = {
    "quick": 1024,
    "standard": 4096,
    "deep": 16384,
    "verify": 0,
    "advisor": 2048,
}


def _resolve_effort(session: Session, frame: Frame) -> EffortLevel:
    """解析当前帧应使用的 effort 档位（§6.5）。

    根帧采用 `session.effort`（用户可调整的全局档位）；委派子帧始终使用
    各自 `AgentDefinition.effort` 的声明值，避免会话级档位覆盖子 agent
    已校准的默认档位（如 advisor 应始终保持低温）。

    Args:
        session: 当前会话。
        frame: 当前活跃帧。

    Returns:
        合法的 `EffortLevel`。
    """
    if frame.parent_id is None and session.effort in EFFORT_LEVELS:
        return cast(EffortLevel, session.effort)
    return frame.agent.effort


def _resolve_temperature(effort: EffortLevel) -> float:
    """把 effort 档位映射为 `LLMProvider.chat()` 的 `temperature` 参数。

    Args:
        effort: 已解析的 effort 档位。

    Returns:
        `EFFORT_TEMPERATURE` 中对应的采样温度。
    """
    return EFFORT_TEMPERATURE[effort]


def resolve_thinking_budget(
    effort: EffortLevel,
    selector: Callable[[EffortLevel], int | None] | None = None,
) -> int:
    """把 effort 档位映射为 `LLMProvider.chat()` 的 `thinking_budget` 参数。

    Args:
        effort: 已解析的 effort 档位。
        selector: 可选的外部覆盖函数（来自配置），返回 None 时 fallback 到内置默认值。

    Returns:
        thinking token 预算（>0 启用并限制，0 关闭，-1 不限预算）。
    """
    if selector is not None:
        override = selector(effort)
        if override is not None:
            return override
    return EFFORT_THINKING_BUDGET[effort]


@dataclass(frozen=True)
class _PendingToolMessage:
    """第一遍扫描中已确定结果的工具消息（未知工具/参数错误/权限拒绝）。"""

    message: dict[str, Any]


@dataclass(frozen=True)
class _PendingServerCall:
    """第一遍扫描中通过校验、待第二遍执行的 server 工具调用。"""

    call_id: str
    tool: ToolDef
    args: dict[str, Any]


_PendingItem = _PendingToolMessage | _PendingServerCall


async def _invoke_server_tool(
    tool: ToolDef, args: dict[str, Any], call_ctx: ToolContext
) -> tuple[Any, bool]:
    """执行单个 server 工具的 handler，捕获运行期异常。

    Args:
        tool: 待执行的 server 工具定义（`tool.handler` 非 None）。
        args: 已解析的工具入参。
        call_ctx: 本次调用的执行上下文。

    Returns:
        `(result, is_error)` 二元组；handler 抛出异常时 `is_error=True`，
        `result` 为异常信息字符串，供 `_tool_message(..., is_error=True)` 包装。
    """
    assert tool.handler is not None
    started = time.perf_counter()
    logger.info(
        "Server tool start session=%s tool=%s domain=%s path_args=%s",
        call_ctx.session_id,
        tool.name,
        tool.domain,
        [name for name in tool.all_path_args if name in args],
    )
    try:
        result = await tool.handler(args, call_ctx)
        logger.info(
            "Server tool success session=%s tool=%s elapsed_ms=%d",
            call_ctx.session_id,
            tool.name,
            int((time.perf_counter() - started) * 1000),
        )
        return result, False
    except Exception as exc:  # 工具实现的非法参数/运行期错误统一回传给模型修正
        logger.exception(
            "Server tool failed session=%s tool=%s elapsed_ms=%d",
            call_ctx.session_id,
            tool.name,
            int((time.perf_counter() - started) * 1000),
        )
        return {
            "error": str(exc),
            "error_code": "server_tool_exception",
            "disposition": "continue_agent",
            "retryable": True,
            "side_effect_state": "none",
            "next_action": {"action": "agent_correct_or_replace_tool_call"},
        }, True


def _tool_message(tool_call_id: str, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    """构造一条 OpenAI `role=tool` 消息。

    Args:
        tool_call_id: 对应的工具调用 id。
        result: 工具结果；非字符串值会被 `json.dumps`。
        is_error: 是否作为错误结果回传（`{"error": ...}`），供模型据此改方案。

    Returns:
        可直接 `append` 进 `frame.messages` 的消息字典。
    """
    if is_error and isinstance(result, dict) and isinstance(result.get("error_code"), str):
        body: Any = dict(result)
    elif is_error:
        body = {
            "error": result,
            "error_code": "server_tool_protocol_error",
            "disposition": "continue_agent",
            "retryable": True,
            "side_effect_state": "none",
            "next_action": {"action": "agent_correct_tool_request"},
        }
    else:
        body = result
    body = _bounded_tool_message_body(body)
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _find_frame(session: Session, frame_id: str) -> Frame | None:
    """按 frame id 查找当前会话里的帧。"""
    for frame in session.agent_stack:
        if frame.id == frame_id:
            return frame
    return None


def _frame_in_active_map_edit(session: Session, frame: Frame | None) -> bool:
    """Return whether a Frame belongs to the current authorized map-edit request."""
    scope = session.map_request_scope
    return (
        frame is not None
        and scope.activates_map_gate
        and scope.map_task_id == session.map_task_state.task_id
        and frame.map_request_lineage_id == scope.lineage_id
        and frame.map_task_id == scope.map_task_id
    )


async def _delegate_child_frame(
    *,
    session: Session,
    parent_id: str,
    call_id: str | None,
    group_id: str | None,
    args: dict[str, Any],
    depth: int,
    prompt_factory: AgentPromptFactory | None,
) -> Frame | str | None:
    """根据委派参数创建子 agent 帧，并保留动态 worker 的具体错误。

    本轮整改要点：
    - 改用 ``agent.role`` / ``agent.map_stage`` 做权限判断，不再硬编码 agent name；
    - 写阶段切换延迟到 prompt/skill 绑定成功后（``transition_stage``），
      避免 worker 创建失败时污染 ``map_task_state.stage``；
    - 子帧创建统一委托给 ``frame_factory.create_child_frame``，
      由其负责 history_anchor 继承与 stage_contract 注入。
    """
    agent_name = args.get("agent")
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    parent = _find_frame(session, parent_id)
    worker_spec = args.get("worker_spec")
    # 本轮整改：记录 worker_mode 供后续延迟阶段切换使用
    worker_mode: str | None = None
    if isinstance(worker_spec, dict):
        if agent_name not in {"map-worker", "map-agent"}:
            return (
                "动态地图 worker 必须使用 agent=map-worker；"
                "不得把 worker_spec 与永久 Agent 名组合"
            )
        # 本轮整改：用 map_stage 代替硬编码 name=="map-agent"，
        # 使 capability contract 派生的动态 agent 也能成为合法父帧
        if parent is None or parent.agent.map_stage != "orchestrator":
            return "只有 map-agent 可以创建动态地图 worker"
        worker_spec = dict(worker_spec)
        worker_spec.setdefault(
            "stage_id",
            f"{group_id or call_id or parent_id}:{worker_spec.get('mode', 'stage')}",
        )
        worker_spec.setdefault("lifecycle_scope", "delegate_frame")
        requested_worker_name = str(worker_spec.get("name", "")).strip()
        if requested_worker_name:
            try:
                get_agent(requested_worker_name, set(REGISTRY))
            except KeyError:
                pass
            else:
                return "动态 worker 名称与永久 Agent 定义冲突：" f"{requested_worker_name}"
        child_or_error = build_dynamic_map_worker(parent.agent, worker_spec)
        if isinstance(child_or_error, str):
            return child_or_error
        reserved_identity = (
            f"__map_worker__:{session.session_id}:"
            f"{worker_spec['stage_id']}:{requested_worker_name}"
        )
        child_agent = replace(
            child_or_error,
            name=reserved_identity,
            description=(f"{child_or_error.description}; display_name={requested_worker_name}"),
        )
        worker_mode = str(worker_spec.get("mode", ""))
    else:
        if not isinstance(agent_name, str) or not agent_name:
            return None
        try:
            child_agent = get_agent(agent_name, set(REGISTRY))
        except KeyError:
            return None
        # 本轮整改：改用 role=="map_orchestrator" 做递归守卫，
        # 不再依赖 agent name 字符串匹配
        if (
            parent is not None
            and parent.agent.role == "map_orchestrator"
            and child_agent.role == "map_orchestrator"
        ):
            return (
                "地图总控不得递归委派另一个地图总控；"
                "写入请创建带 worker_spec.mode=write_one_batch 的动态地图 worker"
            )
    task_text = task.strip()
    scheduler_inputs = args.get("scheduler_inputs")
    if isinstance(scheduler_inputs, dict) and scheduler_inputs:
        task_text = (
            f"{task_text}\n\n"
            "[SCHEDULER_INPUTS]\n"
            f"{json.dumps(scheduler_inputs, ensure_ascii=False, sort_keys=True, default=str)}"
        )
    try:
        prompt = (
            await prompt_factory(child_agent, task_text)
            if prompt_factory is not None
            else child_agent.prompt
        )
    except ValueError as exc:
        # 本轮整改：prompt_factory 失败时直接返回错误，不再创建残缺子帧
        return str(exc)
    if is_map_worker_write_mode(worker_mode):
        if not _frame_in_active_map_edit(session, parent):
            return "当前用户请求未显式授权地图内容编辑，不能创建写入 worker"
        # Skill 与 prompt 完整绑定成功后再进入写阶段，失败创建不得污染状态。
        # 本轮整改：改用 transition_stage() 受控状态机切换，替代直接赋值 stage
        session.map_task_state.transition_stage("write")
    child_agent = replace(child_agent, prompt=prompt)
    if parent is None:
        return None
    # 本轮整改：子帧创建统一走 frame_factory，自动继承 history_anchor
    # 并注入 _map_stage_contract 用于后续输出一致性校验
    return create_child_frame(
        session=session,
        parent=parent,
        agent=child_agent,
        task_text=task_text,
        depth=depth,
        pending_delegate_call_id=call_id,
        pending_delegate_group_id=group_id,
        map_stage_contract=_map_stage_contract(child_agent, task_text, worker_spec),
    )


def _with_plan_runtime_metadata(
    payload: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Preserve request lineage fields omitted by ``PlanGraph.to_dict``."""
    for key in ("owner_frame_id", "request_lineage_id", "map_task_id"):
        if key in source:
            payload[key] = source.get(key)
    return payload


def _plan_step_started(
    session: Session,
    child: Frame,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    step_id: str | None = None,
) -> None:
    """若当前会话有活跃 `create_plan` 计划，记录新子帧对应的步骤并发出 `plan_step_started`。

    Args:
        session: 当前会话。
        child: 刚创建并压栈的子 agent 帧。
        event_callback: 编排事件回调；为 None 时不产生事件。
    """
    plan = session.pending_plan
    if plan is None:
        return
    try:
        graph = PlanGraph.from_dict(plan)
    except PlanGraphError as exc:
        logger.error("Invalid pending plan session=%s error=%s", session.session_id, exc)
        return
    runnable = graph.runnable_steps()
    selected_id = step_id or (runnable[0].step_id if runnable else None)
    if selected_id is None:
        return
    try:
        graph = graph.start(selected_id, child.id)
        step = graph.step(selected_id)
    except PlanGraphError as exc:
        logger.error(
            "Plan step start rejected session=%s frame=%s step=%s error=%s",
            session.session_id,
            child.id,
            selected_id,
            exc,
        )
        return
    session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
    _emit_orchestration_event(
        event_callback,
        "plan_step_started",
        {
            "frame_id": child.id,
            "message_index": len(child.messages),
            **_history_timeline_payload(child),
            "step_id": step.step_id,
            "step_index": step.order + 1,
            "total_steps": len(graph.steps),
            "agent": step.agent,
            "title": step.title,
        },
    )


def _plan_step_completed(
    session: Session,
    done: Frame,
    result_payload: dict[str, Any],
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """若已完成的子帧对应某个计划步骤，发出 `plan_step_completed`。

    Args:
        session: 当前会话。
        done: 刚结束并弹栈的子 agent 帧。
        text: 该子帧本轮产出的最终文本，用作步骤结果摘要。
        event_callback: 编排事件回调；为 None 时不产生事件。
    """
    plan = session.pending_plan
    if plan is None:
        return
    try:
        graph = PlanGraph.from_dict(plan)
    except PlanGraphError as exc:
        logger.error("Invalid pending plan session=%s error=%s", session.session_id, exc)
        return
    running = next(
        (step for step in graph.steps if step.frame_id == done.id and step.status == "running"),
        None,
    )
    if running is None:
        return
    output = (
        dict(result_payload.get("result", {}))
        if isinstance(result_payload.get("result"), dict)
        else {"summary": str(result_payload.get("summary", ""))}
    )
    artifact_ref = result_payload.get("artifact_ref")
    stage = str(output.get("stage", done.agent.map_stage or ""))
    missing_inputs = output.get("missing_inputs")
    is_reader_recovery = "__reader__attempt_" in running.step_id
    if isinstance(missing_inputs, list) and missing_inputs and stage in {"planner", "validator"}:
        target = str(
            output.get(
                "target_path",
                done.map_stage_contract.get("target_path", ""),
            )
        )
        revision_value = output.get(
            "map_revision",
            done.map_stage_contract.get("map_revision", 0),
        )
        revision = (
            revision_value
            if isinstance(revision_value, int) and not isinstance(revision_value, bool)
            else 0
        )
        retry = record_semantic_retry(
            session.map_task_state,
            category="missing_input",
            error_category="typed_missing_inputs",
            root_cause="planner_or_validator_missing_typed_inputs",
            stage=stage,
            target=target,
            revision=revision,
            operation={
                "step_id": running.step_id,
                "task": running.task,
                "worker_spec": running.worker_spec,
            },
            missing_inputs=missing_inputs,
            threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
        )
        if not bool(retry["exhausted"]):
            try:
                graph = graph.inject_reader_recovery(
                    running.step_id,
                    missing_inputs=missing_inputs,
                    target=target,
                    revision=revision,
                )
            except PlanGraphError as exc:
                logger.error(
                    "Reader recovery injection failed session=%s step=%s error=%s",
                    session.session_id,
                    running.step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
                _emit_orchestration_event(
                    event_callback,
                    "plan_reader_recovery_scheduled",
                    {
                        "step_id": running.step_id,
                        "missing_inputs": missing_inputs,
                        "target": target,
                        "revision": revision,
                        "attempt": retry["attempt"],
                        "retry_key": retry["retry_key"],
                    },
                )
                return
        report = retry_pause_report(
            session.map_task_state,
            stage=stage,
            target=target,
            revision=revision,
            last_attempt=retry,
        )
        output["error"] = "map_retry_exhausted"
        output["retry_result"] = report
        if session.map_task_state.status == "running":
            session.map_task_state.make_checkpoint(
                "map_retry_exhausted",
                report,
                pause_kind="no_progress_exhausted",
            )
        else:
            session.map_task_state.pause_kind = "no_progress_exhausted"
            session.map_task_state.pause_reason = "map_retry_exhausted"
            replace_map_state_field(
                session.map_task_state,
                "pause_report",
                report,
                target=target or None,
                revision=revision,
            )
    failed = result_payload.get("error") is True or "error" in output
    repair = output.get("repair")
    if isinstance(repair, dict) and repair.get("status") == "exhausted":
        failed = True
        output["error"] = "structured_output_repair_exhausted"
    if is_reader_recovery:
        reader_missing = (
            list(missing_inputs) if isinstance(missing_inputs, list) else ["missing_inputs"]
        )
        has_typed_facts = isinstance(artifact_ref, str) or bool(output.get("facts"))
        if reader_missing or not has_typed_facts:
            failed = True
            output["error"] = "reader_recovery_incomplete"
            output["reader_recovery_blocked"] = {
                "missing_inputs": reader_missing,
                "artifact_ref_present": isinstance(artifact_ref, str),
                "facts_present": bool(output.get("facts")),
            }
    error_code = str(output.get("error", "")) or None
    if not failed and isinstance(running.expected_result_schema, dict):
        required = running.expected_result_schema.get("required", [])
        if isinstance(required, list):
            missing = [
                str(field) for field in required if isinstance(field, str) and field not in output
            ]
            if missing:
                failed = True
                error_code = "result_schema_mismatch"
                output["missing_required_fields"] = missing
    result = PlanStepResult(
        status="failed" if failed else "succeeded",
        output=output,
        artifact_refs=(str(artifact_ref),) if isinstance(artifact_ref, str) else (),
        error_code=error_code,
    )
    try:
        graph = graph.finish(running.step_id, result)
    except PlanGraphError as exc:
        logger.error(
            "Plan step finish rejected session=%s frame=%s error=%s",
            session.session_id,
            done.id,
            exc,
        )
        return
    session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
    full_summary = str(result_payload.get("summary", "")).strip()
    summary = " ".join(full_summary.split())
    if len(summary) > 240:
        summary = summary[:240] + "..."
    _emit_orchestration_event(
        event_callback,
        "plan_step_completed",
        {
            "frame_id": done.id,
            "message_index": len(done.messages),
            **_history_timeline_payload(done),
            "step_id": running.step_id,
            "step_index": running.order + 1,
            "total_steps": len(graph.steps),
            "status": result.status,
            "summary": summary,
            "full_summary": full_summary,
        },
    )


def _map_delegate_result_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """提取父 Frame 推进阶段所需的最小地图结果摘要。

    本轮整改新增：当 artifact_store 成功落盘后，父帧只需保留摘要字段
    和列表计数（artifact_list_counts），完整数据通过 artifact_ref 回溯，
    大幅减少父帧 context 体积。
    """
    summary_fields = (
        "stage",
        "worker",
        "mode",
        "objective",
        "target_path",
        "map_layer",
        "map_revision",
        "region",
        "summary",
        "validation",
        "missing_inputs",
        "risks",
        "next_stage",
    )
    summary = {
        key: _slim_map_delegate_value(payload[key]) for key in summary_fields if key in payload
    }
    # 只保留列表长度，父帧据此判断是否需要回查 artifact
    summary["artifact_list_counts"] = {
        key: len(payload[key])
        for key in ("facts", "proposed_batches", "write_results")
        if isinstance(payload.get(key), list)
    }
    return summary


def _map_delegate_result_payload(
    done: Frame,
    text: str,
    artifact_store: DelegateArtifactStore | None = None,
) -> dict[str, Any]:
    """把地图子 worker 结果压缩为结构化载荷，避免向父帧透传完整自然语言历史。

    本轮整改：新增 artifact_store 参数。若落盘成功则只回传最小摘要
    （_map_delegate_result_summary）和 artifact_ref；失败时回退到
    preserve_lists=True 的完整瘦身载荷，保证父帧仍能拿到 proposed_batches
    等关键列表。
    """
    output_schema = _map_output_schema_for_frame(done)
    payload = _json_object_from_text(text)
    if payload is not None and output_schema == _MAP_OUTPUT_SCHEMA_V1:
        # 尝试将完整结果写入 artifact store，换取一个可回溯引用
        artifact_ref: str | None = None
        if artifact_store is not None:
            try:
                artifact_ref = artifact_store.store(
                    frame_id=done.id,
                    agent_name=done.agent.name,
                    result_schema=str(output_schema),
                    result=payload,
                )
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "Delegate artifact store failed frame=%s agent=%s error=%s",
                    done.id,
                    done.agent.name,
                    exc,
                )
        # artifact_ref 成功 → 最小摘要；失败 → 保留列表的完整瘦身
        result_payload = (
            _map_delegate_result_summary(payload)
            if artifact_ref is not None
            else _slim_map_delegate_value(payload, preserve_lists=True)
        )
        return {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": _summarize_history_text(str(payload.get("summary", "")), 4000),
            "result": result_payload,
            "artifact_ref": artifact_ref,
        }
    if output_schema == _MAP_OUTPUT_SCHEMA_V1:
        return {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": "",
            "result": {
                "error": "invalid_map_worker_result",
                "message": "child output was not valid map_worker_result_v1 JSON",
            },
        }
    return {
        "agent": done.agent.name,
        "frame_id": done.id,
        "summary": _summarize_history_text(text),
    }


async def _continue_delegate_group(
    session: Session,
    done: Frame,
    text: str,
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    artifact_store: DelegateArtifactStore | None = None,
) -> None:
    """记录一个 `delegate_many` 子任务结果，并按需启动下一个子任务。"""
    assert done.pending_delegate_group_id is not None
    group = session.delegate_groups.get(done.pending_delegate_group_id)
    if group is None:
        logger.warning(
            "Delegate group missing session=%s group_id=%s frame=%s",
            session.session_id,
            done.pending_delegate_group_id,
            done.id,
        )
        return

    delegate_result = _map_delegate_result_payload(done, text, artifact_store)
    group.setdefault("results", []).append(delegate_result)
    _plan_step_completed(session, done, delegate_result, event_callback)
    child: Any = None

    if group.get("plan_driven") is True and session.pending_plan is not None:
        try:
            graph = PlanGraph.from_dict(session.pending_plan)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "Delegate plan continuation failed session=%s group=%s error=%s",
                session.session_id,
                done.pending_delegate_group_id,
                exc,
            )
            graph = None
        runnable = graph.runnable_steps() if graph is not None else ()
        if runnable:
            assert graph is not None
            next_step = runnable[0]
            try:
                next_task = graph.task_payload(next_step.step_id)
                child = await _delegate_child_frame(
                    session=session,
                    parent_id=str(group["parent_frame_id"]),
                    call_id=None,
                    group_id=done.pending_delegate_group_id,
                    args=next_task,
                    depth=int(group["depth"]),
                    prompt_factory=prompt_factory,
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                child = {
                    "error_code": "plan_dependency_or_stage_blocked",
                    "message": str(exc),
                }
            if isinstance(child, Frame):
                session.agent_stack.append(child)
                _plan_step_started(
                    session,
                    child,
                    event_callback,
                    next_step.step_id,
                )
                logger.info(
                    "Plan delegate group continued session=%s group_id=%s "
                    "step=%s child_frame=%s",
                    session.session_id,
                    done.pending_delegate_group_id,
                    next_step.step_id,
                    child.id,
                )
                return
            failure_text = (
                child
                if isinstance(child, str)
                else (
                    str(child.get("message", ""))
                    if isinstance(child, dict)
                    else "调度器已解锁的子任务参数不合法或 agent 不存在"
                )
            )
            group["results"].append(
                {
                    "agent": next_step.agent,
                    "summary": failure_text,
                    "error": True,
                    "error_code": (
                        child.get("error_code")
                        if isinstance(child, dict)
                        else "child_frame_creation_failed"
                    ),
                }
            )
            try:
                failed_graph = graph.fail_unstarted(
                    next_step.step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                group["results"].append(
                    {
                        "agent": next_step.agent,
                        "summary": str(exc),
                        "error": True,
                        "error_code": "plan_failure_reduction_blocked",
                    }
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
    elif isinstance(group.get("scheduler_plan"), dict):
        try:
            group_graph = PlanGraph.from_dict(group["scheduler_plan"])
            running = next(
                (
                    step
                    for step in group_graph.steps
                    if step.frame_id == done.id and step.status == "running"
                ),
                None,
            )
            if running is None:
                raise PlanGraphError(f"no running step owns frame {done.id}")
            output = (
                dict(delegate_result.get("result", {}))
                if isinstance(delegate_result.get("result"), dict)
                else {"summary": str(delegate_result.get("summary", ""))}
            )
            recovery_injected = False
            missing_inputs = output.get("missing_inputs")
            output_stage = str(output.get("stage", done.agent.map_stage or ""))
            if (
                isinstance(missing_inputs, list)
                and missing_inputs
                and output_stage in {"planner", "validator"}
            ):
                target = str(
                    output.get(
                        "target_path",
                        done.map_stage_contract.get("target_path", ""),
                    )
                )
                revision_value = output.get(
                    "map_revision",
                    done.map_stage_contract.get("map_revision", 0),
                )
                revision = (
                    revision_value
                    if isinstance(revision_value, int) and not isinstance(revision_value, bool)
                    else 0
                )
                retry = record_semantic_retry(
                    session.map_task_state,
                    category="missing_input",
                    error_category="typed_missing_inputs",
                    root_cause="planner_or_validator_missing_typed_inputs",
                    stage=output_stage,
                    target=target,
                    revision=revision,
                    operation={
                        "step_id": running.step_id,
                        "task": running.task,
                        "worker_spec": running.worker_spec,
                    },
                    missing_inputs=missing_inputs,
                    threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
                )
                if not bool(retry["exhausted"]):
                    group_graph = group_graph.inject_reader_recovery(
                        running.step_id,
                        missing_inputs=missing_inputs,
                        target=target,
                        revision=revision,
                    )
                    recovery_injected = True
                else:
                    output["error"] = "map_retry_exhausted"
                    output["retry_result"] = retry_pause_report(
                        session.map_task_state,
                        stage=output_stage,
                        target=target,
                        revision=revision,
                        last_attempt=retry,
                    )
            failed = delegate_result.get("error") is True or "error" in output
            error_code = str(output.get("error", "")) or None
            if "__reader__attempt_" in running.step_id:
                reader_missing = (
                    list(missing_inputs) if isinstance(missing_inputs, list) else ["missing_inputs"]
                )
                artifact_ref_value = delegate_result.get("artifact_ref")
                if reader_missing or (
                    not isinstance(artifact_ref_value, str) and not bool(output.get("facts"))
                ):
                    failed = True
                    error_code = "reader_recovery_incomplete"
                    output["reader_recovery_blocked"] = {
                        "missing_inputs": reader_missing,
                        "artifact_ref_present": isinstance(artifact_ref_value, str),
                        "facts_present": bool(output.get("facts")),
                    }
            if not failed and isinstance(running.expected_result_schema, dict):
                required = running.expected_result_schema.get("required", [])
                if isinstance(required, list):
                    missing = [
                        str(field)
                        for field in required
                        if isinstance(field, str) and field not in output
                    ]
                    if missing:
                        failed = True
                        error_code = "result_schema_mismatch"
                        output["missing_required_fields"] = missing
            artifact_ref = delegate_result.get("artifact_ref")
            if not recovery_injected:
                group_graph = group_graph.finish(
                    running.step_id,
                    PlanStepResult(
                        status="failed" if failed else "succeeded",
                        output=output,
                        artifact_refs=(
                            (str(artifact_ref),) if isinstance(artifact_ref, str) else ()
                        ),
                        error_code=error_code,
                    ),
                )
            group["scheduler_plan"] = group_graph.to_dict()
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "Delegate group scheduler update failed session=%s group=%s error=%s",
                session.session_id,
                done.pending_delegate_group_id,
                exc,
            )
            group_graph = None
        runnable = group_graph.runnable_steps() if group_graph is not None else ()
        if runnable:
            assert group_graph is not None
            next_step = runnable[0]
            try:
                next_task = group_graph.task_payload(next_step.step_id)
                child = await _delegate_child_frame(
                    session=session,
                    parent_id=str(group["parent_frame_id"]),
                    call_id=None,
                    group_id=done.pending_delegate_group_id,
                    args=next_task,
                    depth=int(group["depth"]),
                    prompt_factory=prompt_factory,
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                child = {
                    "error_code": "plan_dependency_or_stage_blocked",
                    "message": str(exc),
                }
            if isinstance(child, Frame):
                try:
                    started_graph = group_graph.start(
                        next_step.step_id,
                        child.id,
                    )
                except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                    child = {
                        "error_code": "plan_stage_transition_blocked",
                        "message": str(exc),
                    }
                else:
                    session.agent_stack.append(child)
                    group["scheduler_plan"] = started_graph.to_dict()
                    logger.info(
                        "Delegate scheduler continued session=%s group_id=%s "
                        "step=%s child_frame=%s",
                        session.session_id,
                        done.pending_delegate_group_id,
                        next_step.step_id,
                        child.id,
                    )
                    return
            failure_text = (
                child
                if isinstance(child, str)
                else (
                    str(child.get("message", ""))
                    if isinstance(child, dict)
                    else "调度器已解锁的子任务参数不合法或 agent 不存在"
                )
            )
            group["results"].append(
                {
                    "agent": next_step.agent,
                    "summary": failure_text,
                    "error": True,
                    "error_code": (
                        child.get("error_code")
                        if isinstance(child, dict)
                        else "child_frame_creation_failed"
                    ),
                }
            )
            try:
                failed_graph = group_graph.fail_unstarted(
                    next_step.step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                group["results"].append(
                    {
                        "agent": next_step.agent,
                        "summary": str(exc),
                        "error": True,
                        "error_code": "plan_failure_reduction_blocked",
                    }
                )
            else:
                group["scheduler_plan"] = failed_graph.to_dict()

    parent = _find_frame(session, str(group["parent_frame_id"]))
    if parent is not None:
        migration_error = str(group.get("migration_error", "")).strip()
        if migration_error:
            group["results"].append(
                {
                    "agent": "",
                    "summary": migration_error,
                    "error": True,
                    "error_code": "legacy_delegate_group_blocked",
                }
            )
        parent.messages.append(
            _tool_message(
                str(group["tool_call_id"]),
                {"results": group.get("results", [])},
            )
        )
        logger.info(
            "Delegate group completed session=%s group_id=%s results=%d",
            session.session_id,
            done.pending_delegate_group_id,
            len(group.get("results", [])),
        )
    session.delegate_groups.pop(done.pending_delegate_group_id, None)


_MAP_WORKER_RESULT_FIELDS = map_worker_required_fields()
_MAP_WORKER_STAGES = MAP_WORKER_STAGES
_MAP_OUTPUT_SCHEMA_V1 = MAP_WORKER_RESULT_SCHEMA
_MAP_DELEGATE_LIST_LIMIT = 12
_MAP_DELEGATE_TEXT_LIMIT = 1200
_MAP_DELEGATE_DROP_KEYS = frozenset(
    {
        "cells",
        "full_cells",
        "raw_cells",
        "atlas_summary",
        "matches",
        "screenshot_base64",
        "image_base64",
        "data_url",
    }
)


def _normalized_map_layers(payload: dict[str, Any]) -> tuple[int, ...]:
    """把结构化地图结果中的单层或多层标识规整为真实图层索引。"""
    value = payload.get("map_layer")
    if isinstance(value, int) and not isinstance(value, bool):
        return (value,)
    if isinstance(value, list):
        layers = tuple(
            layer for layer in value if isinstance(layer, int) and not isinstance(layer, bool)
        )
        if layers and len(layers) == len(value):
            return tuple(dict.fromkeys(layers))
        return ()
    if not isinstance(value, str) or not value.strip().lower().startswith("all"):
        return ()

    facts = payload.get("facts")
    if not isinstance(facts, list):
        return ()
    indexes: list[int] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        raw_layers = fact.get("layers")
        if not isinstance(raw_layers, list):
            continue
        for layer in raw_layers:
            if not isinstance(layer, dict):
                continue
            index = layer.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                indexes.append(index)
    return tuple(dict.fromkeys(indexes))


def _normalized_map_layer_value(payload: dict[str, Any]) -> int | list[int] | None:
    """返回适合写回结构化结果的单层或多层值。"""
    layers = _normalized_map_layers(payload)
    if len(layers) == 1:
        return layers[0]
    if layers:
        return list(layers)
    return None


def _map_output_schema_for_frame(frame: Frame) -> str | None:
    """解析当前地图 frame 需要执行的结构化输出 schema。

    本轮整改：不再维护 _MAP_STRUCTURED_OUTPUT_AGENTS 硬编码名称集合，
    改为两路 capability contract 派生：
    1. frame.map_stage_contract 非空 → 说明子帧已绑定阶段合同；
    2. agent.map_stage 属于已知阶段 → 静态声明式元数据兜底。
    """
    if frame.map_stage_contract:
        return _MAP_OUTPUT_SCHEMA_V1
    if frame.agent.map_stage in _MAP_WORKER_STAGES:
        return _MAP_OUTPUT_SCHEMA_V1
    return None


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    """从模型文本中提取 JSON object。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _json_parse_offset(text: str) -> int | None:
    """在 JSON 解析失败时返回安全字符偏移，不保留原始内容。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        json.loads(stripped)
    except json.JSONDecodeError as exc:
        return exc.pos
    return None


def _slim_map_delegate_value(
    value: Any,
    field_name: str = "",
    preserve_lists: bool = False,
) -> Any:
    """递归瘦身地图子任务结果，避免父 agent 继承大数组。

    本轮整改新增 preserve_lists 参数：当 artifact_store 不可用时，
    父帧仍需完整拿到 proposed_batches / write_results 等关键列表，
    此时 preserve_lists=True 跳过列表截断。对于其他字段名则保持
    原有的 _MAP_DELEGATE_LIST_LIMIT 截断策略。
    """
    if isinstance(value, str):
        return (
            value
            if len(value) <= _MAP_DELEGATE_TEXT_LIMIT
            else value[:_MAP_DELEGATE_TEXT_LIMIT] + "..."
        )
    if isinstance(value, list):
        # proposed_batches 和 write_results 是下游编排的关键数据，不可截断
        preserve_lists = preserve_lists or field_name in {
            "proposed_batches",
            "write_results",
        }
        items = value if preserve_lists else value[:_MAP_DELEGATE_LIST_LIMIT]
        return [_slim_map_delegate_value(item, preserve_lists=preserve_lists) for item in items]
    if not isinstance(value, dict):
        return value
    slim: dict[str, Any] = {}
    for key, item in value.items():
        key_str = str(key)
        if key_str in _MAP_DELEGATE_DROP_KEYS:
            slim[f"{key_str}_omitted"] = True
            continue
        slim[key_str] = _slim_map_delegate_value(item, key_str, preserve_lists)
    return slim


def _map_structured_output_error(
    session: Session,
    frame: Frame,
    text: str,
) -> str | None:
    """校验地图阶段 agent 的 map_worker_result_v1 输出。

    本轮整改大幅增强校验逻辑：
    - stage/target_path/map_revision 必须与 Frame 创建时注入的合同一致；
    - next_stage 必须满足 MAP_WORKER_NEXT_STAGES 定义的合法状态转换；
    - reviewer 阶段必须引用当前 Frame 的截图证据（tool_use_id），
      且截图的 target/revision/region 也要与合同匹配。
    """
    output_schema = _map_output_schema_for_frame(frame)
    if output_schema is None:
        return None
    if output_schema != _MAP_OUTPUT_SCHEMA_V1:
        return f"不支持的地图输出 schema：{output_schema}"
    payload = _json_object_from_text(text)
    if payload is None:
        return "输出必须是一个合法 JSON object，schema=map_worker_result_v1。"
    missing = sorted(_MAP_WORKER_RESULT_FIELDS - set(payload))
    if missing:
        return "map_worker_result_v1 缺少字段：" + ", ".join(missing)
    schema_errors = validate_map_worker_schema(
        payload,
        specialized_map_worker_schema(frame),
    )
    if schema_errors:
        return "map_worker_result_v1 schema 校验失败：" + "; ".join(schema_errors[:8])
    violations = validate_frame_result(frame, payload)
    if violations:
        violation = violations[0]
        return f"{violation.code}: {violation.message}; {violation.to_dict()}"
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        return "validation 必须是 object。"
    # ── 本轮整改：reviewer 必须引用截图证据，且证据与合同字段保持一致 ──
    if payload.get("stage") == "reviewer":
        raw_evidence_refs = validation.get("evidence_refs")
        evidence_refs = (
            {str(item) for item in raw_evidence_refs}
            if isinstance(raw_evidence_refs, list)
            else set()
        )
        target_for_evidence = str(
            frame.map_stage_contract.get("target_path", payload.get("target_path", ""))
        )
        revision_for_evidence = frame.map_stage_contract.get(
            "map_revision",
            payload.get("map_revision"),
        )
        registered_evidence = (
            scoped_evidence(
                session.map_task_state,
                target_for_evidence,
                revision_for_evidence,
                "viewport_screenshot",
            )
            if isinstance(revision_for_evidence, int)
            and not isinstance(revision_for_evidence, bool)
            else []
        )
        matching_evidence = [
            item
            for item in registered_evidence
            if str(item.get("metadata", {}).get("tool_use_id", "")) in evidence_refs
            and item.get("metadata", {}).get("frame_id") == frame.id
        ]
        if not matching_evidence:
            return "reviewer 必须引用当前 Frame 成功截图的 tool_use_id。"
        contracted_target = frame.map_stage_contract.get("target_path")
        contracted_revision = frame.map_stage_contract.get("map_revision")
        if isinstance(contracted_target, str) and contracted_target:
            if any(item.get("target") != contracted_target for item in matching_evidence):
                return "reviewer 截图证据与当前 Frame 的 target_path 不一致。"
        if isinstance(contracted_revision, int) and not isinstance(contracted_revision, bool):
            if any(item.get("revision") != contracted_revision for item in matching_evidence):
                return "reviewer 截图证据与当前 Frame 的 map_revision 不一致。"
        contracted_region = frame.map_stage_contract.get("region")
        if isinstance(contracted_region, dict) and contracted_region:
            if any(
                item.get("metadata", {}).get("region") != contracted_region
                for item in matching_evidence
            ):
                return "reviewer 截图证据与当前 Frame 的 region 不一致。"
    validation_missing = [
        key for key in ("passed", "issues", "structured_issues") if key not in validation
    ]
    if validation_missing:
        return "validation 缺少字段：" + ", ".join(validation_missing)
    for list_key in ("facts", "proposed_batches", "write_results", "missing_inputs", "risks"):
        if not isinstance(payload.get(list_key), list):
            return f"{list_key} 必须是 array。"
    if _normalized_map_layer_value(payload) is None:
        return (
            "map_layer 必须是整数或非空整数数组；读取全部图层时可使用 "
            '"all"，但 facts 必须包含 layers[].index 作为真实索引依据。'
        )
    return None


def _repair_map_structured_output(
    frame: Frame,
    text: str,
    error: str,
    *,
    category: str,
    attempt: int,
    exhausted: bool,
) -> str:
    """把不合规地图输出保守修复为不可完成的合法结果。"""
    source = _json_object_from_text(text) or {}
    stage = source.get("stage")
    if stage not in _MAP_WORKER_STAGES:
        stage = _map_stage_for_frame(frame)
    validation = source.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    raw_issues = validation.get("issues")
    issues = list(raw_issues) if isinstance(raw_issues, list) else []
    issues.append(f"structured_output_repaired: {category}")
    raw_structured_issues = validation.get("structured_issues")
    structured_issues = (
        list(raw_structured_issues) if isinstance(raw_structured_issues, list) else []
    )
    structured_issues.append(
        {
            "code": (
                "structured_output_repair_exhausted" if exhausted else "structured_output_repaired"
            ),
            "message": f"structured output rejected ({category})",
            "agent": frame.agent.name,
            "category": category,
            "attempt": attempt,
        }
    )

    def list_value(key: str) -> list[Any]:
        """把指定字段规整为数组。"""
        value = source.get(key)
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    map_revision = frame.map_stage_contract.get(
        "map_revision",
        source.get("map_revision"),
    )
    if isinstance(map_revision, bool) or not isinstance(map_revision, int):
        map_revision = 0
    map_layer = _normalized_map_layer_value(source)
    if map_layer is None:
        map_layer = 0
    allowed_next = frame.allowed_next_stages or tuple(
        frame.map_stage_contract.get("allowed_next_stages", ())
    )
    preferred_next = "validator" if stage == "writer" else "planner"
    next_stage = (
        preferred_next
        if preferred_next in allowed_next
        else (allowed_next[0] if allowed_next else stage)
    )
    repaired = {
        "contract_id": frame.contract_id or str(source.get("contract_id") or ""),
        "result_schema": frame.result_schema or _MAP_OUTPUT_SCHEMA_V1,
        "stage": stage,
        "worker": frame.worker_instance_id or str(source.get("worker") or frame.agent.name),
        "mode": str(source.get("mode") or "partial"),
        "objective": str(source.get("objective") or _frame_objective(frame)),
        "target_path": str(
            frame.map_stage_contract.get("target_path")
            or source.get("target_path")
            or ""
        ),
        "map_layer": map_layer,
        "map_revision": map_revision,
        "region": source.get("region") if isinstance(source.get("region"), dict) else {},
        "summary": str(source.get("summary") or "地图子阶段输出已由服务端保守修复。"),
        "facts": list_value("facts"),
        "proposed_batches": list_value("proposed_batches"),
        "write_results": list_value("write_results"),
        "validation": {
            "passed": False,
            "completion_allowed": False,
            "issues": issues,
            "structured_issues": structured_issues,
        },
        "missing_inputs": list_value("missing_inputs"),
        "risks": [
            *list_value("risks"),
            "结构化输出曾不合规，本结果不能作为任务完成依据。",
        ],
        "next_stage": next_stage,
        "repair": {
            "status": "exhausted" if exhausted else "repaired",
            "error_category": category,
            "original_issue_categories": sorted(
                {
                    category,
                    *(
                        str(item.get("code"))
                        for item in structured_issues
                        if isinstance(item, dict) and item.get("code")
                    ),
                }
            ),
            "safe_diagnostics": [safe_structured_diagnostic(error)],
            "applied_actions": structured_repair_actions(category),
            "attempt": attempt,
            "threshold": STRUCTURED_REPAIR_MAX_ATTEMPTS,
        },
    }
    return json.dumps(repaired, ensure_ascii=False)


def _map_stage_for_frame(frame: Frame) -> str:
    """根据地图 agent/frame 名称推断结构化收尾阶段。

    本轮整改：优先级从「名称硬编码」改为「合同 → agent 元数据 → reader 兜底」，
    删除了对 agent name / prompt 文本的字符串匹配。
    """
    # 1. 合同优先：子帧创建时由 _map_stage_contract 注入
    contracted_stage = frame.map_stage_contract.get("stage")
    if isinstance(contracted_stage, str) and contracted_stage in _MAP_WORKER_STAGES:
        return contracted_stage
    # 2. 静态 agent 元数据兜底
    if frame.agent.map_stage in _MAP_WORKER_STAGES:
        return str(frame.agent.map_stage)
    return "reader"


def _route_unvalidated_platform_writes_to_validator(
    *,
    session: Session,
    frame: Frame,
    calls: list[ToolCallRequest],
    project_root: Path,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> tuple[bool, ToolCallsResult | None]:
    """拒绝未获 planner 批准的平台写入，不在服务层推断路线。

    本轮整改：旧实现会在服务层自动推断平台几何并下发 validate_platform_level_plan，
    但这绕过了 planner 的路线设计职责，且推断结果常与模型意图不一致。
    新实现只做「守门」：检测到缺少批准批次的平台写入就一律拒绝，
    把控制权还给 planner agent。
    """
    del project_root  # 新实现不再需要 project_root，保留签名兼容
    blocked = False
    for call in calls:
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None or args is None:
            continue
        blocked = blocked or platform_write_requires_validation(session, call.name, args)
    if not blocked:
        return False, None
    rejection = UnapprovedWriteRejection(
        error_code="approved_write_batch_required",
        message=(
            "平台路线写入缺少 planner 生成并由 "
            "validate_platform_level_plan 编译的批准批次。"
            "服务层不会从 edit_map 几何推断路线；请返回 planner。"
        ),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                rejection.to_dict(),
                is_error=True,
            )
        )
    _emit_orchestration_event(
        event_callback,
        "map_platform_write_rejected",
        {"frame_id": frame.id, "agent": frame.agent.name},
    )
    return True, None


def _frame_objective(frame: Frame) -> str:
    """取子帧第一条用户消息作为 objective。"""
    for message in frame.messages:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return frame.agent.description or frame.agent.name


def _frame_semantic_operation(frame: Frame) -> dict[str, Any]:
    """提取不含 Frame/调用 id 的稳定语义操作身份，同时区分并行目标。"""
    objective_text = _frame_objective(frame)
    objective: Any = objective_text
    try:
        parsed = json.loads(objective_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        objective = parsed.get("objective", objective_text)
        inputs = parsed.get("inputs")
        if isinstance(inputs, dict):
            objective = {
                "objective": objective,
                "inputs": {
                    str(key): value
                    for key, value in inputs.items()
                    if str(key)
                    not in {
                        "artifact_ref",
                        "artifact_refs",
                        "call_id",
                        "frame_id",
                        "request_id",
                        "tool_use_id",
                    }
                },
            }
    return {
        "worker_mode": frame.agent.worker_mode,
        "stage": _map_stage_for_frame(frame),
        "objective": objective,
        "output_schema": _map_output_schema_for_frame(frame),
    }


def _map_frame_exhausted_payload(frame: Frame, limit_label: str, limit: int) -> str:
    """为地图子帧预算耗尽生成合法的部分结果 JSON。"""
    issue = f"子 agent 达到自身{limit_label}上限（{limit}），已返回部分读取/执行结果。"
    revision = frame.map_stage_contract.get("map_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = 0
    allowed_next = frame.allowed_next_stages or tuple(
        frame.map_stage_contract.get("allowed_next_stages", ())
    )
    payload = {
        "contract_id": frame.contract_id or "",
        "result_schema": frame.result_schema or _MAP_OUTPUT_SCHEMA_V1,
        "stage": _map_stage_for_frame(frame),
        "worker": frame.worker_instance_id or frame.agent.name,
        "mode": "partial",
        "objective": _frame_objective(frame),
        "target_path": str(frame.map_stage_contract.get("target_path") or ""),
        "map_layer": 0,
        "map_revision": revision,
        "region": {},
        "summary": issue,
        "facts": [],
        "proposed_batches": [],
        "write_results": [],
        "validation": {
            "passed": False,
            "completion_allowed": False,
            "issues": [issue],
            "structured_issues": [
                {
                    "code": "frame_turns_exhausted",
                    "limit_label": limit_label,
                    "limit": limit,
                    "agent": frame.agent.name,
                }
            ],
        },
        "missing_inputs": [
            "需要父 agent 基于已返回的工具结果继续拆分任务，或用更具体的 target_path/map_layer/region 重新委派。"
        ],
        "risks": ["本子阶段未完整收敛，不能作为完成依据。"],
        "next_stage": "replan" if "replan" in allowed_next else (
            allowed_next[0] if allowed_next else _map_stage_for_frame(frame)
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _payload_revision(payload: dict[str, Any]) -> int | None:
    """读取结构化地图结果里的 map_revision。"""
    value = payload.get("map_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _same_payload_target(blocker: dict[str, Any], target: str) -> bool:
    """判断阻断项是否匹配结构化输出的目标地图。"""
    blocker_target = str(blocker.get("target", ""))
    return blocker_target == "" or target == "" or blocker_target == target


def _blocker_required_revision(blocker: dict[str, Any]) -> int | None:
    """读取完成门阻断项要求的 map_revision。"""
    value = blocker.get("required_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clear_map_blockers(
    blockers: list[dict[str, Any]],
    target: str,
    revision: int | None,
    reason: str,
) -> list[dict[str, Any]]:
    """清除同目标、同 revision 已满足的地图完成门阻断项。"""
    remaining: list[dict[str, Any]] = []
    for blocker in blockers:
        if blocker.get("reason") != reason:
            remaining.append(blocker)
            continue
        blocker_revision = _blocker_required_revision(blocker)
        if _same_payload_target(blocker, target) and (
            revision is None or blocker_revision is None or revision >= blocker_revision
        ):
            continue
        remaining.append(blocker)
    return remaining


def _append_map_blocker_once(
    blockers: list[dict[str, Any]],
    blocker: dict[str, Any],
) -> list[dict[str, Any]]:
    """追加完成门阻断项，避免重复添加同目标同 revision 同原因条目。"""
    reason = blocker.get("reason")
    target = str(blocker.get("target", ""))
    revision = _blocker_required_revision(blocker)
    for existing in blockers:
        if existing.get("reason") != reason:
            continue
        if not _same_payload_target(existing, target):
            continue
        existing_revision = _blocker_required_revision(existing)
        if revision is None or existing_revision is None or revision == existing_revision:
            return blockers
    return [*blockers, blocker]


def _apply_reader_structured_completion(
    session: Session,
    payload: dict[str, Any],
) -> None:
    """仅在 reader 提交完整事实合同时把地图工作流推进到规划阶段。"""
    target = payload.get("target_path")
    map_layer = _normalized_map_layer_value(payload)
    revision = _payload_revision(payload)
    facts = payload.get("facts")
    missing_inputs = payload.get("missing_inputs")
    mode = str(payload.get("mode", ""))
    complete = (
        mode != "partial"
        and isinstance(target, str)
        and bool(target.strip())
        and map_layer is not None
        and revision is not None
        and isinstance(facts, list)
        and bool(facts)
        and isinstance(missing_inputs, list)
        and not missing_inputs
    )
    state = session.map_task_state
    if complete:
        payload["map_layer"] = map_layer
        # 本轮整改：改用 transition_stage 受控状态机，替代直接赋值 stage
        state.transition_stage("plan")
        replace_map_state_field(
            state,
            "unresolved_issues",
            [],
            target=target,
            revision=revision,
        )
        context_state = dict(state.context_state)
        context_state["reader_result"] = _slim_map_delegate_value(payload)
        context_state.pop("reader_exhausted", None)
        replace_map_state_field(state, "context_state", context_state)
        logger.info(
            "Map reader completion advanced workflow session=%s target=%s layer=%s revision=%s",
            session.session_id,
            target,
            map_layer,
            revision,
        )
        return

    missing = list(missing_inputs) if isinstance(missing_inputs, list) else []
    invalid_fields: list[str] = []
    if not isinstance(target, str) or not target.strip():
        invalid_fields.append("target_path")
    if map_layer is None:
        invalid_fields.append("map_layer")
    if revision is None:
        invalid_fields.append("map_revision")
    if not isinstance(facts, list) or not facts:
        invalid_fields.append("facts")
    if mode == "partial":
        invalid_fields.append("mode=partial")
    if not isinstance(missing_inputs, list):
        invalid_fields.append("missing_inputs")
    # 本轮整改：改用 transition_stage 受控状态机，替代直接赋值 stage
    state.transition_stage("read")
    replace_map_state_field(
        state,
        "unresolved_issues",
        [
            {
                "kind": "reader_incomplete",
                "missing_inputs": missing or invalid_fields,
            }
        ],
        target=target if isinstance(target, str) else None,
        revision=revision,
    )
    logger.info(
        "Map reader completion kept workflow in read stage "
        "session=%s mode=%s missing=%d invalid_fields=%s",
        session.session_id,
        mode,
        len(missing),
        invalid_fields,
    )


def _apply_map_structured_completion_result(session: Session, frame: Frame, text: str) -> None:
    """把地图阶段 agent 的结构化结果合并进工作流状态和完成门。"""
    payload = _json_object_from_text(text)
    if payload is None:
        return
    stage = str(payload.get("stage", ""))
    if stage == "reader":
        _apply_reader_structured_completion(session, payload)
        return
    if stage not in {"validator", "reviewer"}:
        return
    target = str(payload.get("target_path", ""))
    revision = _payload_revision(payload)
    validation = payload.get("validation")
    validation_dict = validation if isinstance(validation, dict) else {}
    observation_passed = (
        validation_dict.get("passed") is True
        and validation_dict.get("blocking_completion") is not True
    )
    issues = validation_dict.get("issues")
    issue_list = [str(issue) for issue in issues] if isinstance(issues, list) else []

    if stage == "validator":
        # 本轮整改：latest_validations 从 session 顶层迁移到 map_task_state
        canonical = session.map_task_state.latest_validations.get(target)
        canonical_matches = (
            isinstance(canonical, dict)
            and bool(target)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and canonical.get("target") == target
            and canonical.get("map_revision") == revision
        )
        canonical_success = (
            canonical_matches
            and canonical is not None
            and canonical.get("passed") is True
            and canonical.get("blocking_completion") is not True
        )
        if observation_passed and canonical_success:
            # 本轮整改：completion_blockers 从 session 顶层迁移到 map_task_state
            blockers = _clear_map_blockers(
                session.map_task_state.completion_blockers,
                target,
                revision,
                "map_write_requires_validation",
            )
            blockers = _clear_map_blockers(
                blockers,
                target,
                revision,
                "validator_failed",
            )
            replace_map_state_field(
                session.map_task_state,
                "completion_blockers",
                _append_map_blocker_once(
                    blockers,
                    {
                        "tool": frame.agent.name,
                        "reason": "map_review_required",
                        "issues": [
                            "same-revision validation passed; reviewer visual check is still required"
                        ],
                        "target": target,
                        "required_revision": revision,
                        # 本轮整改：blocker 新增 region 字段，供 reviewer 截图校验比对
                        "region": payload.get("region"),
                    },
                ),
                target=target,
                revision=revision,
            )
        else:
            existing = next(
                (
                    blocker
                    for blocker in session.map_task_state.completion_blockers
                    if blocker.get("target") in ("", target)
                    and (revision is None or blocker.get("required_revision") in (None, revision))
                ),
                None,
            )
            if existing is not None:
                blockers = [
                    {
                        **blocker,
                        **(
                            {"next_stage": blocker.get("next_stage", "planner")}
                            if blocker is existing
                            else {}
                        ),
                    }
                    for blocker in session.map_task_state.completion_blockers
                ]
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    blockers,
                    target=target,
                    revision=revision,
                )
            else:
                blockers = _clear_map_blockers(
                    session.map_task_state.completion_blockers,
                    target,
                    revision,
                    "validator_failed",
                )
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    _append_map_blocker_once(
                        blockers,
                        {
                            "tool": frame.agent.name,
                            "reason": "validator_failed",
                            "issues": issue_list
                            or ["validator failed or no canonical tool validation was recorded"],
                            "target": target,
                            "required_revision": revision,
                            "next_stage": "planner",
                        },
                    ),
                    target=target,
                    revision=revision,
                )
        return

    if observation_passed and not issue_list:
        blockers = _clear_map_blockers(
            session.map_task_state.completion_blockers,
            target,
            revision,
            "map_review_required",
        )
        replace_map_state_field(
            session.map_task_state,
            "completion_blockers",
            _clear_map_blockers(
                blockers,
                target,
                revision,
                "reviewer_failed",
            ),
            target=target,
            revision=revision,
        )
    else:
        blockers = _clear_map_blockers(
            session.map_task_state.completion_blockers,
            target,
            revision,
            "reviewer_failed",
        )
        replace_map_state_field(
            session.map_task_state,
            "completion_blockers",
            _append_map_blocker_once(
                blockers,
                {
                    "tool": frame.agent.name,
                    "reason": "reviewer_failed",
                    "issues": issue_list or ["reviewer observation did not pass"],
                    "target": target,
                    "required_revision": revision,
                },
            ),
            target=target,
            revision=revision,
        )


async def _finish_frame(
    session: Session,
    text: str,
    prompt_factory: AgentPromptFactory | None = None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    artifact_store: DelegateArtifactStore | None = None,
) -> FinalResult | None:
    """处理当前帧产出最终文本（无 `tool_calls`）的情况（§13.1）。

    根帧（`agent_stack` 长度为 1）保留在栈中以维持多轮会话历史，直接
    返回 `FinalResult`；由 `delegate` 创建的子帧（M2+）结束时则弹栈，
    把摘要回填父帧那条 `delegate` 的工具结果，交由调用方继续驱动父帧。

    Args:
        session: 当前会话。
        text: 当前帧本轮产出的最终文本。
        prompt_factory: 子 agent 系统提示词构造函数。
        event_callback: 编排事件回调，供 `create_plan` 步骤进度事件使用。

    Returns:
        根帧结束时返回 `FinalResult`；子帧结束时返回 None，调用方应
        继续循环（此时 `session.top_frame()` 已是父帧）。
    """
    frame = session.top_frame()
    if frame is not None:
        structured_error = _map_structured_output_error(session, frame, text)
        if structured_error is not None:
            category = structured_error_category(structured_error)
            raw_digest = hashlib.sha256(
                text.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            safe_diagnostic = {
                **safe_structured_diagnostic(structured_error),
                "schema_version": _map_output_schema_for_frame(frame),
                "response_mode": frame.response_contract_mode or "none",
                "model": frame.structured_response_model or "unknown",
                "temperature": 0.0,
                "thinking_budget": frame.structured_thinking_budget,
                "tools_enabled": False,
                "finish_reason": frame.structured_finish_reason or "unknown",
                "raw_chars": len(text),
                "raw_digest": raw_digest,
                "local_attempt": frame.structured_attempt_count + 1,
            }
            parse_offset = _json_parse_offset(text)
            if parse_offset is not None:
                safe_diagnostic["parse_offset"] = parse_offset
            frame.structured_diagnostics.append(safe_diagnostic)
            if (
                frame.force_text_only
                and frame.structured_attempt_count < frame.structured_correction_limit
            ):
                frame.structured_attempt_count += 1
                immutable_constraints = {
                    key: value
                    for key, value in {
                        "contract_id": frame.contract_id,
                        "result_schema": frame.result_schema,
                        "stage": frame.map_stage_contract.get("stage"),
                        "worker": frame.worker_instance_id,
                        "target_path": frame.map_stage_contract.get("target_path"),
                        "map_revision": frame.map_stage_contract.get("map_revision"),
                        "allowed_next_stages": list(frame.allowed_next_stages),
                    }.items()
                    if value is not None and value != ""
                }
                frame.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Structured result correction required. "
                            f"schema={_MAP_OUTPUT_SCHEMA_V1}; "
                            f"category={category}; "
                            "invalid_fields="
                            + json.dumps(
                                safe_diagnostic.get("fields", []),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "; frozen_constraints="
                            + json.dumps(
                                immutable_constraints,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + ". 只重新输出一个完整 JSON object；不得调用工具，"
                            "不得复述上一条原始输出。"
                        ),
                    }
                )
                logger.warning(
                    "Map structured output correction scheduled session=%s frame=%s "
                    "agent=%s category=%s schema=%s response_mode=%s "
                    "local_attempt=%d/%d raw_chars=%d raw_digest=%s",
                    session.session_id,
                    frame.id,
                    frame.agent.name,
                    category,
                    _MAP_OUTPUT_SCHEMA_V1,
                    frame.response_contract_mode or "none",
                    frame.structured_attempt_count,
                    frame.structured_correction_limit,
                    len(text),
                    raw_digest,
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_structured_correction_scheduled",
                    {
                        "frame_id": frame.id,
                        "schema_version": _MAP_OUTPUT_SCHEMA_V1,
                        "response_mode": frame.response_contract_mode or "none",
                        "category": category,
                        "local_attempt": frame.structured_attempt_count,
                        "raw_chars": len(text),
                        "raw_digest": raw_digest,
                    },
                )
                return None
            source_payload = _json_object_from_text(text) or {}
            target = str(
                source_payload.get(
                    "target_path",
                    frame.map_stage_contract.get("target_path", ""),
                )
            )
            revision_value = source_payload.get(
                "map_revision",
                frame.map_stage_contract.get("map_revision", 0),
            )
            revision = (
                revision_value
                if isinstance(revision_value, int) and not isinstance(revision_value, bool)
                else 0
            )
            retry = record_semantic_retry(
                session.map_task_state,
                category="structured_output",
                error_category=category,
                root_cause=structured_error,
                stage=_map_stage_for_frame(frame),
                target=target,
                revision=revision,
                operation=_frame_semantic_operation(frame),
                threshold=STRUCTURED_REPAIR_MAX_ATTEMPTS,
            )
            # 本轮整改：日志新增 raw_chars + raw_digest，便于跨请求
            # 追踪同一次结构化输出拒绝/修复事件
            logger.warning(
                "Map structured output rejected session=%s frame=%s agent=%s "
                "category=%s raw_chars=%d raw_digest=%s local_attempt=%d",
                session.session_id,
                frame.id,
                frame.agent.name,
                category,
                len(text),
                raw_digest,
                frame.structured_attempt_count,
            )
            text = _repair_map_structured_output(
                frame,
                text,
                structured_error,
                category=category,
                attempt=int(retry["attempt"]),
                exhausted=bool(retry["exhausted"]),
            )
            logger.warning(
                "Map structured output repaired session=%s frame=%s agent=%s "
                "category=%s attempt=%d exhausted=%s actions=%s "
                "repaired_chars=%d repaired_digest=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
                category,
                retry["attempt"],
                retry["exhausted"],
                structured_repair_actions(category),
                len(text),
                hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
            )
            if bool(retry["exhausted"]):
                report = retry_pause_report(
                    session.map_task_state,
                    stage=_map_stage_for_frame(frame),
                    target=target,
                    revision=revision,
                    last_attempt=retry,
                )
                if session.map_task_state.status == "running":
                    session.map_task_state.make_checkpoint(
                        "structured_output_retry_exhausted",
                        report,
                        pause_kind="no_progress_exhausted",
                    )

        _apply_map_structured_completion_result(session, frame, text)
        if structured_error is None and frame.structured_attempt_count:
            logger.info(
                "Map structured output corrected session=%s frame=%s schema=%s "
                "response_mode=%s local_attempts=%d",
                session.session_id,
                frame.id,
                _MAP_OUTPUT_SCHEMA_V1,
                frame.response_contract_mode or "none",
                frame.structured_attempt_count,
            )
            _emit_orchestration_event(
                event_callback,
                "map_structured_correction_succeeded",
                {
                    "frame_id": frame.id,
                    "schema_version": _MAP_OUTPUT_SCHEMA_V1,
                    "response_mode": frame.response_contract_mode or "none",
                    "local_attempts": frame.structured_attempt_count,
                },
            )

    if len(session.agent_stack) <= 1:
        logger.info("Root frame finished session=%s text_length=%d", session.session_id, len(text))
        if session.pending_plan is not None:
            session.pending_plan = None
        return FinalResult(text=text)
    done = session.agent_stack.pop()
    # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
    if done.agent.map_stage == "reader":
        context_state = dict(session.map_task_state.context_state)
        context_state.pop("reader_exhausted", None)
        replace_map_state_field(
            session.map_task_state,
            "context_state",
            context_state,
        )
    logger.info(
        "Child frame finished session=%s frame=%s agent=%s text_length=%d",
        session.session_id,
        done.id,
        done.agent.name,
        len(text),
    )
    if done.pending_delegate_group_id is not None:
        await _continue_delegate_group(
            session,
            done,
            text,
            prompt_factory,
            event_callback,
            artifact_store,
        )
        return None
    parent = session.top_frame()
    assert parent is not None
    if done.pending_delegate_call_id is not None:
        delegate_result = _map_delegate_result_payload(done, text, artifact_store)
        _plan_step_completed(session, done, delegate_result, event_callback)
        parent.messages.append(
            _tool_message(
                done.pending_delegate_call_id,
                delegate_result,
            )
        )
    elif done.parent_id is not None:
        parent.messages.append(
            {
                "role": "user",
                "content": (
                    "自动子阶段结果："
                    + json.dumps(
                        _map_delegate_result_payload(done, text, artifact_store),
                        ensure_ascii=False,
                    )
                ),
            }
        )
    return None


async def _handle_frame_turns_exhausted(
    session: Session,
    frame: Frame,
    limit_label: str,
    limit: int,
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    artifact_store: DelegateArtifactStore | None = None,
) -> ErrorResult | None:
    """某个轮次预算（总轮数/edit_map 轮数/常规轮数）耗尽时的统一收尾。

    根帧耗尽时整轮直接报错终止；子帧耗尽时用 `_finish_frame` 收尾并把控制权
    交还父帧，让父 agent 据此判断是否要重新拆分任务。

    Returns:
        根帧耗尽时返回 `ErrorResult`（调用方应立即 `return`）；子帧耗尽时返回
        `None`（`_finish_frame` 已处理收尾，调用方应 `continue` 外层循环）。
    """
    if len(session.agent_stack) <= 1:
        logger.warning(
            "Agent run_turn reached root frame turns limit session=%s agent=%s limit=%s=%d",
            session.session_id,
            frame.agent.name,
            limit_label,
            limit,
        )
        return ErrorResult(
            text="已达到本轮最大循环次数，请精简任务或拆分请求后重试",
            error_code="agent_turn_budget_exhausted",
        )
    logger.warning(
        "Delegate frame reached its turns limit session=%s frame=%s agent=%s limit=%s=%d",
        session.session_id,
        frame.id,
        frame.agent.name,
        limit_label,
        limit,
    )
    text = (
        _map_frame_exhausted_payload(frame, limit_label, limit)
        if _map_output_schema_for_frame(frame) == _MAP_OUTPUT_SCHEMA_V1
        else (
            f"子 agent「{frame.agent.name}」已达到自身{limit_label}上限（{limit}），"
            "任务未完成，已强制收尾。以上为已执行步骤记录，请据此判断是否需要重新拆分任务或继续委派。"
        )
    )
    await _finish_frame(
        session,
        text,
        prompt_factory,
        event_callback,
        artifact_store,
    )
    # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
    if frame.agent.map_stage == "reader":
        context_state = dict(session.map_task_state.context_state)
        context_state["reader_exhausted"] = True
        replace_map_state_field(
            session.map_task_state,
            "context_state",
            context_state,
        )
    return None


def _coerce_schema_value(value: Any, schema: dict[str, Any]) -> tuple[Any, bool]:
    """按工具 schema 安全转换模型字符串化的 JSON 值。"""
    expected_type = schema.get("type")
    normalized = value
    changed = False
    if isinstance(value, str):
        stripped = value.strip()
        if expected_type == "integer" and _INTEGER_TEXT.fullmatch(stripped):
            normalized = int(stripped)
            changed = True
        elif expected_type == "number" and _NUMBER_TEXT.fullmatch(stripped):
            normalized = float(stripped)
            changed = True
        elif expected_type == "boolean" and stripped.lower() in {"true", "false"}:
            normalized = stripped.lower() == "true"
            changed = True
        elif expected_type in {"array", "object"}:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if (expected_type == "array" and isinstance(parsed, list)) or (
                expected_type == "object" and isinstance(parsed, dict)
            ):
                normalized = parsed
                changed = True

    if expected_type == "object" and isinstance(normalized, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            result = dict(normalized)
            for key, child_schema in properties.items():
                if key not in result or not isinstance(child_schema, dict):
                    continue
                child_value, child_changed = _coerce_schema_value(result[key], child_schema)
                if child_changed:
                    result[key] = child_value
                    changed = True
            normalized = result
    elif expected_type == "array" and isinstance(normalized, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            result_items: list[Any] = []
            for item in normalized:
                child_value, child_changed = _coerce_schema_value(item, item_schema)
                result_items.append(child_value)
                changed = changed or child_changed
            normalized = result_items
    return normalized, changed


def _normalize_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """使用已注册工具 schema 规范化模型生成的入参。"""
    tool = REGISTRY.get(tool_name)
    if tool is None:
        return args
    parameters = tool.schema.get("parameters")
    if not isinstance(parameters, dict):
        return args
    normalized, changed = _coerce_schema_value(args, parameters)
    if changed and isinstance(normalized, dict):
        logger.info("Normalized tool arguments from schema tool=%s", tool_name)
        return normalized
    return args


def _load_tool_args(
    call_id: str, arguments: str, tool_name: str = ""
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解析工具入参 JSON，返回 `(args, error_message)` 二元组。"""
    try:
        loaded = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("Tool arguments JSON parse failed call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参不是合法 JSON", is_error=True)
    if not isinstance(loaded, dict):
        logger.warning("Tool arguments are not an object call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参必须是 JSON object", is_error=True)
    return _normalize_tool_args(tool_name, loaded), None


def _append_delegate_protocol_errors(frame: Frame, calls: list[Any]) -> None:
    """当 `delegate` 与其他 tool call 并列时，给本轮所有调用补错误结果。"""
    logger.warning(
        "Delegate protocol violation frame=%s agent=%s tool_calls=%d",
        frame.id,
        frame.agent.name,
        len(calls),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                "`delegate` 必须是本轮唯一的 tool call；本轮所有工具均未执行，请重试",
                is_error=True,
            )
        )


def _append_create_plan_protocol_errors(frame: Frame, calls: list[Any]) -> None:
    """当 `create_plan` 与其他 tool call 并列时，给本轮所有调用补错误结果。"""
    logger.warning(
        "Create_plan protocol violation frame=%s agent=%s tool_calls=%d",
        frame.id,
        frame.agent.name,
        len(calls),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                "`create_plan` 必须是本轮唯一的 tool call；本轮所有工具均未执行，请重试",
                is_error=True,
            )
        )


_COMPLEX_MAP_DELEGATION_KEYWORDS = (
    "扩展",
    "生成",
    "设计",
    "关卡",
    "路线",
    "通关",
    "平台",
    "阶梯",
    "悬浮",
    "陷阱",
    "金币",
    "树",
    "终点",
    "预览",
    "确认",
    "批量",
    "decorate",
    "decoration",
    "extend",
    "expansion",
    "level",
    "route",
    "platform",
    "coin",
    "preview",
)


def _is_complex_map_delegation_task(task: str) -> bool:
    """Heuristically detect map tasks that need a visible create_plan first."""
    normalized = task.lower()
    hits = sum(1 for keyword in _COMPLEX_MAP_DELEGATION_KEYWORDS if keyword in normalized)
    return hits >= 2


def _agent_name_has_role(agent_name: Any, role: str) -> bool:
    """判断内置 Agent 名是否解析为指定稳定角色。

    本轮整改新增：将 agent 名称解析为 AgentDefinition 后比对 role 字段，
    替代直接硬编码 agent name 字符串，使 capability contract 派生的
    动态 agent 也能被正确识别。
    """
    if not isinstance(agent_name, str) or not agent_name:
        return False
    try:
        return get_agent(agent_name, set(REGISTRY)).role == role
    except KeyError:
        return False


def _map_agent_targets_from_delegate_call(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return map-agent task texts from delegate/delegate_many args."""
    if tool_name == "delegate":
        # 本轮整改：用 role=="map_orchestrator" 代替硬编码 "map-agent"
        if _agent_name_has_role(args.get("agent"), "map_orchestrator") and isinstance(
            args.get("task"), str
        ):
            return [str(args["task"])]
        return []
    if tool_name != "delegate_many":
        return []
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[str] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        if _agent_name_has_role(item.get("agent"), "map_orchestrator") and isinstance(
            item.get("task"), str
        ):
            tasks.append(str(item["task"]))
    return tasks


def _requires_create_plan_before_map_delegate(
    session: Session,
    frame: Frame,
    tool_name: str,
    args: dict[str, Any],
) -> bool:
    """Require coordinator to create a visible plan before complex map delegation."""
    # 本轮整改：用 role 代替 name=="coordinator"，支持 capability contract 派生
    if frame.agent.role != "coordinator" or session.pending_plan is not None:
        return False
    return any(
        _is_complex_map_delegation_task(task)
        for task in _map_agent_targets_from_delegate_call(tool_name, args)
    )


def _append_map_write_protocol_errors(frame: Frame, calls: list[Any]) -> bool:
    """校验地图写工具单轮协议，失败时补工具错误并要求模型重试。"""
    write_calls = [call for call in calls if is_map_write_tool(call.name)]
    # 本轮整改：用 role/map_stage 代替 name in {"coordinator","map-agent"}，
    # 确保 capability contract 派生的 agent 也受此约束
    if write_calls and (
        frame.agent.role == "coordinator" or frame.agent.map_stage == "orchestrator"
    ):
        message = (
            "地图总控不得直接调用写工具或临时拼接地图块；请把已确认的规划批次委派给 "
            "write_one_batch worker。平台路线只能执行 validate_platform_level_plan "
            "校验通过后返回的 edit_map_batches。"
        )
        logger.warning(
            "Blocked coordinator direct map write frame=%s agent=%s tools=%s",
            frame.id,
            frame.agent.name,
            [call.name for call in write_calls],
        )
        for call in calls:
            frame.messages.append(_tool_message(call.id, message, is_error=True))
        return True
    if len(write_calls) > 1 and len(write_calls) != len(calls):
        logger.warning(
            "Map write protocol violation frame=%s agent=%s write_calls=%d",
            frame.id,
            frame.agent.name,
            len(write_calls),
        )
        for call in calls:
            frame.messages.append(
                _tool_message(
                    call.id,
                    "确定性地图批次不能与读取、验证或服务端工具混在同一轮；请只提交有序写批次",
                    is_error=True,
                )
            )
        return True

    for call in write_calls:
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return True
        assert args is not None
        error = validate_map_write_args(call.name, args)
        if error is not None:
            frame.messages.append(_tool_message(call.id, error, is_error=True))
            return True
    return False


def _append_map_plan_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """在前端执行前拒绝重复或超限的平台规划方案。"""
    for call in calls:
        if call.name not in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
            continue
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return True
        assert args is not None
        error = map_platform_plan_call_error(session, call.name, args)
        if error is None:
            continue
        logger.warning(
            "Map platform plan protocol violation session=%s frame=%s tool=%s error=%s",
            session.session_id,
            frame.id,
            call.name,
            error,
        )
        frame.messages.append(_tool_message(call.id, error, is_error=True))
        return True
    return False


def _append_reader_fallback_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """reader 耗尽后阻止总控绕过专职 agent 重复读取同一批事实。"""
    # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
    if frame.agent.map_stage != "orchestrator":
        return False
    if session.map_task_state.context_state.get("reader_exhausted") is not True:
        return False
    blocked_names = {
        "describe_map_context",
        "describe_map_region",
        "read_scene_tree",
        "read_file",
        "query_spatial_index",
    }
    blocked_calls = [call for call in calls if call.name in blocked_names]
    if not blocked_calls:
        return False
    message = (
        "map-reader-agent 已达到轮次上限；总控不得绕过职责直接重复读取。"
        "请根据 reader 的部分结构化结果缩小 missing_inputs 后重新委派一次，"
        "或向用户返回明确缺失事实。"
    )
    for call in calls:
        frame.messages.append(_tool_message(call.id, message, is_error=True))
    logger.warning(
        "Blocked map-agent direct read after reader exhaustion session=%s frame=%s tools=%s",
        session.session_id,
        frame.id,
        [call.name for call in blocked_calls],
    )
    return True


# 本轮整改：改用 role 集合代替硬编码 agent name，使动态派生 agent 也能被识别
_MAP_FOLLOWUP_AGENT_ROLES = frozenset({"map_validator", "map_reviewer"})


def _has_pending_map_write_validation(session: Session) -> bool:
    """判断当前会话是否有写后必须验证的地图阻断。"""
    # 本轮整改：blockers 从 session 顶层迁移到 map_task_state 内部
    for blocker in session.map_task_state.completion_blockers:
        reason = blocker.get("reason")
        if reason in {"map_write_requires_validation", "map_review_required"}:
            return True
        if (
            reason in {"blocking_completion", "completion_not_allowed"}
            and blocker.get("tool") in MAP_WRITE_TOOL_NAMES
        ):
            return True
    return False


def _map_validation_arg_error(session: Session, tool_name: str, args: dict[str, Any]) -> str | None:
    """按写入时声明的通用约束检查验证工具参数。"""
    progress_error = validation_call_error(session, tool_name, args)
    if progress_error is not None:
        return progress_error
    # 本轮整改：blockers 从 session 顶层迁移到 map_task_state 内部
    for blocker in session.map_task_state.completion_blockers:
        raw_constraints = blocker.get("workflow_constraints", [])
        if not isinstance(raw_constraints, list):
            continue
        for constraint in raw_constraints:
            if not isinstance(constraint, dict) or constraint.get("validator") != tool_name:
                continue
            required_args = constraint.get("required_args", {})
            if not isinstance(required_args, dict):
                continue
            for key, value in required_args.items():
                if args.get(key) != value:
                    return f"{tool_name} 必须传 {key}={value!r} 以满足当前地图约束"
    return None


def _uses_persistent_map_budget(frame: Frame) -> bool:
    """判断帧是否属于需要跨 HTTP 累计预算的地图工作流。"""
    # 本轮整改：改用 pipeline_kind=="map" 声明式元数据，
    # 替代 name.startswith("map-") 和 workflow_operations 的隐式判断
    return frame.agent.pipeline_kind == "map"


def _stage_effective_tools(session: Session, frame: Frame) -> list[str]:
    """按地图任务阶段裁剪工具，非地图帧保持原白名单。"""
    if frame.force_text_only:
        return []
    if not _uses_persistent_map_budget(frame):
        return list(frame.agent.effective_tools)
    stage = session.map_task_state.stage
    # 本轮整改：阶段→工具映射从内嵌 _MAP_STAGE_TOOLS 迁移到
    # map_capabilities.map_tools_for_stage()，支持动态扩展
    allowed = map_tools_for_stage(stage)
    if not allowed:
        return list(frame.agent.effective_tools)
    return [name for name in frame.agent.effective_tools if name in allowed]


def _latest_map_progress_revision(session: Session) -> int | None:
    """返回会话当前已知的最高地图 revision。"""
    # 本轮整改：revisions 从 session.latest_map_revisions 迁移到
    # map_task_state.latest_revisions，统一管理地图进度状态
    return max(session.map_task_state.latest_revisions.values(), default=None)


def _sync_map_progress_budget(session: Session, frame: Frame) -> None:
    """Track revision progress without resetting task-level convergence budgets."""
    revision = _latest_map_progress_revision(session)
    if revision == frame.map_progress_revision:
        return
    frame.map_progress_revision = revision


def _region_contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    """判断缓存区域是否完整覆盖请求区域。"""
    try:
        for axis, size in (("x", "width"), ("y", "height"), ("z", "depth")):
            outer_start = int(outer.get(axis, 0))
            inner_start = int(inner.get(axis, 0))
            if outer_start > inner_start:
                return False
            if outer_start + int(outer.get(size, 1)) < inner_start + int(inner.get(size, 1)):
                return False
        return True
    except (TypeError, ValueError):
        return False


def _cached_map_region_summary(
    session: Session,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """返回同 revision 下覆盖请求的最近区域摘要。"""
    target = args.get("target_path")
    layer = args.get("map_layer")
    if not isinstance(target, str) or not target:
        return None
    if not isinstance(layer, int) or isinstance(layer, bool):
        return None
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    current_revision = latest_map_revision(session, target, layer)
    # 本轮整改：context_state 从 session 顶层迁移到 map_task_state
    targets = session.map_task_state.context_state.get("targets", {})
    target_state = targets.get(target, {}) if isinstance(targets, dict) else {}
    layers = target_state.get("layers", {}) if isinstance(target_state, dict) else {}
    layer_state = layers.get(str(layer), {}) if isinstance(layers, dict) else {}
    regions = layer_state.get("recent_regions", []) if isinstance(layer_state, dict) else []
    if not isinstance(regions, list):
        return None
    requested_region = {
        "x": args.get("x", 0),
        "y": args.get("y", 0),
        "z": args.get("z", 0),
        "width": args.get("width", 1),
        "height": args.get("height", 1),
        "depth": args.get("depth", 1),
    }
    format_rank = {"summary_only": 0, "non_empty_only": 1, "full": 2}
    requested_rank = format_rank.get(str(args.get("cells_format", "summary_only")), 0)
    for entry in reversed(regions):
        if not isinstance(entry, dict) or entry.get("map_revision") != current_revision:
            continue
        cached_rank = format_rank.get(str(entry.get("cells_format", "summary_only")), 0)
        if cached_rank < requested_rank:
            continue
        region = entry.get("region", {})
        if isinstance(region, dict) and _region_contains(region, requested_region):
            increment_map_counter(session.map_task_state, "read_cache_hits")
            return {**entry, "cache_hit": True, "cache_reason": "same_revision_region_covered"}
    return None


def _resumed_full_map_read_error(session: Session, args: dict[str, Any]) -> str | None:
    """恢复任务时拒绝重新读取已知整图范围。"""
    if not session.map_request_scope.explicit_continuation:
        return None
    target = args.get("target_path")
    layer = args.get("map_layer")
    if not isinstance(target, str) or not isinstance(layer, int):
        return None
    # 本轮整改：context_state 从 session 顶层迁移到 map_task_state
    targets = session.map_task_state.context_state.get("targets", {})
    target_state = targets.get(target, {}) if isinstance(targets, dict) else {}
    layers = target_state.get("layers", {}) if isinstance(target_state, dict) else {}
    layer_state = layers.get(str(layer), {}) if isinstance(layers, dict) else {}
    used_bounds = layer_state.get("used_bounds") if isinstance(layer_state, dict) else None
    requested = {
        "x": args.get("x", 0),
        "y": args.get("y", 0),
        "z": args.get("z", 0),
        "width": args.get("width", 1),
        "height": args.get("height", 1),
        "depth": args.get("depth", 1),
    }
    if isinstance(used_bounds, dict) and _region_contains(requested, used_bounds):
        return (
            "任务已从结构化检查点恢复；禁止从头读取整个地图。"
            "请复用 checkpoint/region cache，只读取 failure_frontier 或尚未缓存的小区域。"
        )
    return None


def _is_delegate_map_followup(tool_name: str, args: dict[str, Any]) -> bool:
    """判断委派调用是否只进入地图验证或复核阶段。"""
    task_items: list[dict[str, Any]]
    if tool_name == "delegate":
        task_items = [args]
    elif tool_name == "delegate_many":
        raw_tasks = args.get("tasks")
        task_items = (
            [item for item in raw_tasks if isinstance(item, dict)]
            if isinstance(raw_tasks, list)
            else []
        )
    else:
        return False
    if not task_items:
        return False
    for item in task_items:
        worker_spec = item.get("worker_spec")
        if isinstance(worker_spec, dict):
            # 本轮整改：简化 worker_spec 判断——只有 review_only 模式才属于 followup
            if worker_spec.get("mode") == "review_only":
                continue
            return False
        # 本轮整改：用 _agent_name_has_role 解析 role 代替硬编码 agent name
        agent_name = item.get("agent")
        if not any(_agent_name_has_role(agent_name, role) for role in _MAP_FOLLOWUP_AGENT_ROLES):
            return False
    return True


def _append_map_write_followup_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """强制地图写入后的下一阶段必须是验证或复核。"""
    if not _has_pending_map_write_validation(session):
        return False
    for call in calls:
        if call.name in MAP_VALIDATION_TOOL_NAMES:
            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                return True
            assert args is not None
            arg_error = _map_validation_arg_error(session, call.name, args)
            if arg_error is not None:
                frame.messages.append(_tool_message(call.id, arg_error, is_error=True))
                return True
            continue
        if call.name in {"delegate", "delegate_many"}:
            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                return True
            assert args is not None
            if _is_delegate_map_followup(call.name, args):
                continue
        logger.warning(
            "Map write followup violation session=%s frame=%s agent=%s tool=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
            call.name,
        )
        allowed_tools = sorted(set(frame.agent.effective_tools) & set(MAP_VALIDATION_TOOL_NAMES))
        blocker = (
            session.map_task_state.completion_blockers[0]
            if session.map_task_state.completion_blockers
            else {}
        )
        target = str(blocker.get("target", ""))
        revision = blocker.get("required_revision")
        next_action = (
            f"请调用 {allowed_tools[0]}"
            if allowed_tools
            else "请单独 delegate 给 map-validator-agent 或 map-reviewer-agent"
        )
        context_hint = (
            f"；target_path={target}, expected_revision={revision}"
            if target and revision is not None
            else ""
        )
        for pending_call in calls:
            frame.messages.append(
                _tool_message(
                    pending_call.id,
                    (
                        "地图写入后下一阶段必须先执行 validator/reviewer 或验证工具；"
                        f"本轮工具未执行。{next_action}{context_hint}。"
                        f"当前允许的验证工具：{allowed_tools or ['delegate(map-validator-agent)']}"
                    ),
                    is_error=True,
                )
            )
        return True
    return False


def _with_map_write_metadata(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """补充服务端掌握的地图写组与验证事务元数据。"""
    enriched = dict(args)
    if tool_name in MAP_VALIDATION_TOOL_NAMES:
        target_path = str(enriched.get("target_path", "")).strip()
        candidates = [
            item
            for item in session.map_task_state.transaction_journals
            if item.get("status") == "prepared"
            and (not target_path or str(item.get("target", "")).strip() == target_path)
        ]
        if candidates:
            active = candidates[-1]
            enriched.setdefault("map_transaction_id", str(active.get("transaction_id", "")))
            enriched.setdefault("map_transaction_revision", active.get("final_revision"))
            enriched.setdefault("map_transaction_target", str(active.get("target", "")))
        return enriched
    if not is_map_write_tool(tool_name):
        return enriched
    target_path = str(enriched.get("target_path", ""))
    # 本轮整改：revision 查询改为图层感知，传入 map_layer 避免跨图层冲突
    layer = enriched.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    latest_revision = latest_map_revision(session, target_path, map_layer)
    supplied_revision = enriched.get("expected_revision")
    supplied_revision_is_int = isinstance(supplied_revision, int) and not isinstance(
        supplied_revision, bool
    )
    if latest_revision is not None and (
        not supplied_revision_is_int or latest_revision > int(supplied_revision)
    ):
        logger.info(
            "Overriding stale map expected_revision session=%s frame=%s tool=%s target=%s supplied=%s latest=%s",
            session.session_id,
            frame.id,
            tool_name,
            target_path,
            supplied_revision,
            latest_revision,
        )
        enriched["expected_revision"] = latest_revision
    # 本轮整改：latest_layers 从 session 顶层迁移到 map_task_state
    latest_layer = session.map_task_state.latest_layers.get(target_path)
    if latest_layer is not None and "map_layer" not in enriched:
        logger.info(
            "Filling missing map_layer session=%s frame=%s tool=%s target=%s map_layer=%s",
            session.session_id,
            frame.id,
            tool_name,
            target_path,
            latest_layer,
        )
        enriched["map_layer"] = latest_layer
    enriched.setdefault("write_batch_id", f"b-{call_id}")
    if isinstance(enriched.get("plan_version"), int) and not isinstance(
        enriched.get("plan_version"), bool
    ):
        transaction_seed = ":".join(
            (
                session.session_id,
                frame.id,
                str(enriched["plan_version"]),
                target_path,
            )
        )
        enriched.setdefault(
            "map_transaction_id",
            "mtx-" + hashlib.sha256(transaction_seed.encode("utf-8")).hexdigest()[:24],
        )
        enriched.setdefault("map_transaction_mode", "approved_write_group")
        enriched.setdefault("map_transaction_base_revision", enriched.get("expected_revision"))
        enriched.setdefault("map_transaction_validator", "validate_map_region")
    else:
        enriched.setdefault("map_transaction_mode", "single_tool")
    enriched.setdefault("worker", frame.agent.name)
    enriched.setdefault("mode", "write_one_batch")
    enriched.setdefault("frame_id", frame.id)
    if frame.agent.workflow_operations:
        enriched.setdefault("workflow_operations", frame.agent.workflow_operations)
    if frame.agent.workflow_constraints:
        enriched.setdefault("workflow_constraints", frame.agent.workflow_constraints)
    if frame.pending_delegate_group_id is not None:
        enriched.setdefault("delegate_group_id", frame.pending_delegate_group_id)
    if "task_summary" not in enriched:
        enriched["task_summary"] = str(enriched.get("objective", tool_name))
    return enriched


_PLAN_COMPLEXITY_LEVELS = {"low", "medium", "high"}


def _normalize_plan_steps(raw_steps: Any) -> list[dict[str, Any]] | str:
    """校验并规范化 `create_plan.steps` 入参。

    Args:
        raw_steps: `create_plan` 工具调用入参里的 `steps` 原始值。

    Returns:
        校验通过时返回规范化后的步骤字典列表；校验失败时返回中文错误提示字符串。
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        return "create_plan.steps 不能为空"
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            return "create_plan.steps 的每一项必须是 object"
        title = raw.get("title")
        agent_name = raw.get("agent")
        task = raw.get("task")
        if not isinstance(title, str) or not title.strip():
            return "create_plan.steps[].title 不能为空"
        if not isinstance(agent_name, str) or not agent_name.strip():
            return "create_plan.steps[].agent 不能为空"
        if not isinstance(task, str) or not task.strip():
            return "create_plan.steps[].task 不能为空"
        try:
            get_agent(agent_name, set(REGISTRY))
        except KeyError:
            return f"未知子 agent：{agent_name}"
        complexity = raw.get("estimated_complexity")
        if complexity is not None and complexity not in _PLAN_COMPLEXITY_LEVELS:
            return "create_plan.steps[].estimated_complexity 取值必须是 low/medium/high"
        normalized.append(
            {
                "id": str(raw.get("id", raw.get("step_id", f"step-{index + 1}"))).strip(),
                "title": title.strip(),
                "agent": agent_name.strip(),
                "task": task.strip(),
                "depends_on": [
                    str(item).strip()
                    for item in raw.get("depends_on", [])
                    if isinstance(item, str) and item.strip()
                ],
                "input_bindings": [
                    dict(item) for item in raw.get("input_bindings", []) if isinstance(item, dict)
                ],
                "expected_result_schema": (
                    dict(raw["expected_result_schema"])
                    if isinstance(raw.get("expected_result_schema"), dict)
                    else None
                ),
                "worker_spec": (
                    dict(raw["worker_spec"]) if isinstance(raw.get("worker_spec"), dict) else None
                ),
                "estimated_complexity": complexity,
            }
        )
    try:
        graph = PlanGraph.from_dict({"summary": "", "steps": normalized})
    except PlanGraphError as exc:
        return f"create_plan.steps 依赖图不合法：{exc}"
    normalized = [step.to_dict() for step in graph.steps]
    return normalized


def _handle_create_plan(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """处理 `create_plan` 工具调用：校验入参、记录计划、发出通知事件并回填工具结果。

    `create_plan` 不挂起轮次：校验通过后立即把 `steps` 转换为 `delegate_many.tasks`
    形状，作为成功结果回填本次调用，引导 LLM 在下一轮自行调用 `delegate_many`
    开始执行（§2.4.2）。

    Args:
        session: 当前会话。
        frame: 发起 `create_plan` 调用的帧（必须是允许委派的 agent）。
        call_id: 本次 `create_plan` 调用的 tool_call id。
        args: 已解析的入参（`summary`/`steps`）。
        event_callback: 编排事件回调，用于发出 `plan_created`。
    """
    if not frame.agent.can_delegate:
        logger.warning(
            "Create_plan rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent，不能创建计划", is_error=True)
        )
        return

    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        frame.messages.append(_tool_message(call_id, "create_plan.summary 不能为空", is_error=True))
        return

    steps = _normalize_plan_steps(args.get("steps"))
    if isinstance(steps, str):
        frame.messages.append(_tool_message(call_id, steps, is_error=True))
        return

    try:
        graph = PlanGraph.from_dict(
            {
                "summary": summary.strip(),
                "steps": steps,
            }
        )
    except PlanGraphError as exc:
        frame.messages.append(
            _tool_message(call_id, f"create_plan 依赖图不合法：{exc}", is_error=True)
        )
        return
    state = session.map_task_state
    frontier = state.failure_frontier if isinstance(state.failure_frontier, dict) else {}
    target = str(frontier.get("target", "")).strip()
    if not target and state.latest_revisions:
        target = sorted(state.latest_revisions)[0].split("::", 1)[0]
    revision = max(state.latest_revisions.values(), default=0)
    root_error_code = str(
        frontier.get("error_code") or frontier.get("blocked_reason") or "planning"
    )
    attempt: dict[str, Any] | None = None
    if state.status == "running":
        attempt = record_plan_attempt(
            state,
            stage=state.stage,
            target=target or "__workflow__",
            revision=revision,
            operation={"summary": summary.strip(), "steps": steps},
            root_error_code=root_error_code,
        )
        if bool(attempt.get("exhausted", False)):
            report = {
                "type": "map_task_convergence_exhausted",
                "exact_attempt": attempt["exact"],
                "task_convergence": attempt["convergence"],
                "root_error_code": root_error_code,
                "recovery_guidance": (
                    "Inspect the first root cause or explicitly start a distinct "
                    "task epoch; sub-step revision progress does not reset this count."
                ),
            }
            state.make_checkpoint(
                "create_plan convergence circuit breaker exhausted",
                report,
                pause_kind="no_progress_exhausted",
            )
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "map_task_convergence_exhausted",
                        "report": report,
                    },
                    is_error=True,
                )
            )
            return
    exact_key = str(attempt["exact"].get("key", "")) if isinstance(attempt, dict) else ""
    if (
        exact_key
        and isinstance(session.pending_plan, dict)
        and session.pending_plan.get("semantic_attempt_key") == exact_key
    ):
        existing_graph = PlanGraph.from_dict(session.pending_plan)
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": True,
                    "idempotent_replay": True,
                    "tasks": [
                        existing_graph.task_payload(step.step_id) for step in existing_graph.steps
                    ],
                    "note": "相同语义计划已存在；保留当前 running/terminal 步骤结果。",
                },
            )
        )
        return
    session.pending_plan = graph.to_dict()
    if exact_key:
        session.pending_plan["semantic_attempt_key"] = exact_key
    session.pending_plan["owner_frame_id"] = frame.id
    session.pending_plan["request_lineage_id"] = frame.map_request_lineage_id or ""
    session.pending_plan["map_task_id"] = frame.map_task_id or ""
    logger.info(
        "Plan created session=%s frame=%s steps=%d",
        session.session_id,
        frame.id,
        len(steps),
    )
    _emit_orchestration_event(
        event_callback,
        "plan_created",
        {
            "frame_id": frame.id,
            "agent": frame.agent.name,
            "message_index": len(frame.messages),
            **_history_timeline_payload(frame),
            "summary": session.pending_plan["summary"],
            "steps": [
                {
                    "id": step.step_id,
                    "index": step.order + 1,
                    "title": step.title,
                    "agent": step.agent,
                    "task": step.task,
                    "depends_on": list(step.depends_on),
                    "estimated_complexity": step.estimated_complexity,
                }
                for step in graph.steps
            ],
        },
    )
    tasks = [graph.task_payload(step.step_id) for step in graph.steps]
    frame.messages.append(
        _tool_message(
            call_id,
            {
                "ok": True,
                "tasks": tasks,
                "note": "计划已记录并通知用户。请立即调用 delegate_many，把上面的 tasks 原样作为参数传入以开始执行。",
            },
        )
    )


async def _start_delegate_frame(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """创建子 agent 帧并压栈，成功时返回 True。"""
    agent_name = args.get("agent")
    task = args.get("task")
    has_worker_spec = isinstance(args.get("worker_spec"), dict)
    if not has_worker_spec and (not isinstance(agent_name, str) or not agent_name):
        logger.warning(
            "Delegate rejected: missing agent session=%s frame=%s", session.session_id, frame.id
        )
        frame.messages.append(_tool_message(call_id, "delegate.agent 不能为空", is_error=True))
        return False
    if not isinstance(task, str) or not task.strip():
        logger.warning(
            "Delegate rejected: missing task session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            agent_name,
        )
        frame.messages.append(_tool_message(call_id, "delegate.task 不能为空", is_error=True))
        return False
    if not frame.agent.can_delegate:
        logger.warning(
            "Delegate rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True)
        )
        return False
    # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
    if has_worker_spec and frame.agent.map_stage != "orchestrator":
        logger.warning(
            "Delegate rejected: dynamic worker parent is not map-agent session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "只有 map-agent 可以创建动态地图 worker", is_error=True)
        )
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(
            _tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True)
        )
        return False

    child = await _delegate_child_frame(
        session=session,
        parent_id=frame.id,
        call_id=call_id,
        group_id=None,
        args=args,
        depth=frame.depth + 1,
        prompt_factory=prompt_factory,
    )
    if isinstance(child, str):
        logger.warning(
            "Delegate rejected: invalid child session=%s frame=%s agent=%s error=%s",
            session.session_id,
            frame.id,
            agent_name,
            child,
        )
        frame.messages.append(_tool_message(call_id, child, is_error=True))
        return False
    if child is None:
        logger.warning(
            "Delegate rejected: unknown child agent session=%s agent=%s",
            session.session_id,
            agent_name,
        )
        frame.messages.append(_tool_message(call_id, f"未知子 agent：{agent_name}", is_error=True))
        return False
    session.agent_stack.append(child)
    _plan_step_started(session, child, event_callback)
    logger.info(
        "Delegate frame started session=%s parent_frame=%s child_frame=%s parent_agent=%s child_agent=%s depth=%d",
        session.session_id,
        frame.id,
        child.id,
        frame.agent.name,
        child.agent.name,
        child.depth,
    )
    return True


async def _start_delegate_group(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """启动 `delegate_many` 顺序子任务组。"""
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        logger.warning(
            "Delegate_many rejected: missing tasks session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(_tool_message(call_id, "delegate_many.tasks 不能为空", is_error=True))
        return False
    if not frame.agent.can_delegate:
        logger.warning(
            "Delegate_many rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True)
        )
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate_many rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(
            _tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True)
        )
        return False

    submitted_tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not submitted_tasks:
        logger.warning(
            "Delegate_many rejected: invalid tasks session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(call_id, "delegate_many.tasks 格式不合法", is_error=True)
        )
        return False
    plan_driven = (
        session.pending_plan is not None and session.pending_plan.get("owner_frame_id") == frame.id
    )
    plan_step_id: str | None = None
    if plan_driven:
        try:
            graph = PlanGraph.from_dict(session.pending_plan or {})
        except PlanGraphError as exc:
            frame.messages.append(_tool_message(call_id, f"当前计划无法恢复：{exc}", is_error=True))
            return False
        runnable = graph.runnable_steps()
        if not runnable:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "status": "blocked",
                        "error_code": "plan_predecessor_not_succeeded",
                        "message": "当前计划没有可运行步骤；请检查前置步骤终态",
                    },
                    is_error=True,
                )
            )
            return False
        first_step = runnable[0]
        plan_step_id = first_step.step_id
        try:
            first = graph.task_payload(first_step.step_id)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_dependency_binding_failed",
                        "step_id": first_step.step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
        contract_tasks = [
            {
                "agent": step.agent,
                "task": step.task,
                "worker_spec": step.worker_spec,
            }
            for step in graph.steps
            if step.status == "pending"
        ]
    else:
        raw_group_steps = [
            {
                "id": str(
                    task.get(
                        "plan_step_id",
                        task.get("id", f"{call_id}-step-{index + 1}"),
                    )
                ),
                "title": str(task.get("title", task.get("task", "")))[:120],
                "agent": task.get("agent"),
                "task": task.get("task"),
                "depends_on": task.get("depends_on", []),
                "input_bindings": task.get("input_bindings", []),
                "expected_result_schema": task.get("expected_result_schema"),
                "worker_spec": task.get("worker_spec"),
            }
            for index, task in enumerate(submitted_tasks)
        ]
        try:
            group_graph = PlanGraph.from_dict(
                {
                    "summary": f"delegate_many:{call_id}",
                    "steps": raw_group_steps,
                }
            )
        except PlanGraphError as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    f"delegate_many 依赖图不合法：{exc}",
                    is_error=True,
                )
            )
            return False
        runnable = group_graph.runnable_steps()
        if not runnable:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "status": "blocked",
                        "error_code": "plan_predecessor_not_succeeded",
                        "message": "delegate_many 没有可运行根步骤",
                    },
                    is_error=True,
                )
            )
            return False
        first_step = runnable[0]
        plan_step_id = first_step.step_id
        try:
            first = group_graph.task_payload(first_step.step_id)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_dependency_binding_failed",
                        "step_id": first_step.step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
        contract_tasks = [
            {
                "agent": step.agent,
                "task": step.task,
                "worker_spec": step.worker_spec,
            }
            for step in group_graph.steps
        ]
    if any(isinstance(task.get("worker_spec"), dict) for task in contract_tasks):
        # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
        if frame.agent.map_stage != "orchestrator":
            logger.warning(
                "Delegate_many rejected: dynamic worker parent is not map-agent session=%s frame=%s agent=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
            )
            frame.messages.append(
                _tool_message(call_id, "只有 map-agent 可以创建动态地图 worker", is_error=True)
            )
            return False
        write_workers = [
            task
            for task in contract_tasks
            if isinstance(task.get("worker_spec"), dict)
            and is_map_worker_write_mode(task["worker_spec"].get("mode"))
        ]
        if len(write_workers) > 1:
            logger.warning(
                "Delegate_many rejected: multiple map write workers session=%s frame=%s count=%d",
                session.session_id,
                frame.id,
                len(write_workers),
            )
            frame.messages.append(
                _tool_message(
                    call_id,
                    "delegate_many 同一组最多只能包含一个地图写入 worker；请拆成多个阶段串行执行",
                    is_error=True,
                )
            )
            return False
    group_id = call_id
    session.delegate_groups[group_id] = {
        "parent_frame_id": frame.id,
        "tool_call_id": call_id,
        "request_lineage_id": frame.map_request_lineage_id or "",
        "map_task_id": frame.map_task_id or "",
        "results": [],
        "depth": frame.depth + 1,
        "plan_driven": plan_driven,
        "scheduler_plan": (None if plan_driven else group_graph.to_dict()),
    }
    workflow_snapshot = copy.deepcopy(session.map_task_state)
    try:
        child = await _delegate_child_frame(
            session=session,
            parent_id=frame.id,
            call_id=None,
            group_id=group_id,
            args=first,
            depth=frame.depth + 1,
            prompt_factory=prompt_factory,
        )
    except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "error_code": "plan_child_creation_blocked",
                    "step_id": plan_step_id,
                    "message": str(exc),
                },
                is_error=True,
            )
        )
        return False
    if isinstance(child, str):
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        if plan_driven and session.pending_plan is not None and plan_step_id is not None:
            try:
                active_graph = PlanGraph.from_dict(session.pending_plan)
                failed_graph = active_graph.fail_unstarted(
                    plan_step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                logger.error(
                    "Failed to reduce child creation outcome session=%s step=%s error=%s",
                    session.session_id,
                    plan_step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
        logger.warning(
            "Delegate_many rejected: invalid first task session=%s frame=%s error=%s",
            session.session_id,
            frame.id,
            child,
        )
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "status": "blocked",
                    "error_code": "plan_child_contract_blocked",
                    "step_id": plan_step_id,
                    "message": child,
                },
                is_error=True,
            )
        )
        return False
    if child is None:
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        if plan_driven and session.pending_plan is not None and plan_step_id is not None:
            try:
                active_graph = PlanGraph.from_dict(session.pending_plan)
                failed_graph = active_graph.fail_unstarted(
                    plan_step_id,
                    "invalid_delegate_task",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                logger.error(
                    "Failed to reduce invalid child payload session=%s step=%s error=%s",
                    session.session_id,
                    plan_step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
        logger.warning(
            "Delegate_many rejected: invalid first task session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "status": "blocked",
                    "error_code": "plan_child_payload_invalid",
                    "step_id": plan_step_id,
                    "message": "delegate_many 首个子任务不合法",
                },
                is_error=True,
            )
        )
        return False
    session.agent_stack.append(child)
    if plan_driven:
        _plan_step_started(session, child, event_callback, plan_step_id)
    else:
        group = session.delegate_groups[group_id]
        try:
            group_graph = PlanGraph.from_dict(group["scheduler_plan"])
            group["scheduler_plan"] = group_graph.start(
                str(plan_step_id),
                child.id,
            ).to_dict()
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            session.map_task_state = workflow_snapshot
            session.agent_stack.pop()
            session.delegate_groups.pop(group_id, None)
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_stage_transition_blocked",
                        "step_id": plan_step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
    logger.info(
        "Delegate_many group started session=%s group_id=%s parent_frame=%s child_frame=%s total_tasks=%d",
        session.session_id,
        group_id,
        frame.id,
        child.id,
        len(raw_tasks),
    )
    return True


def _event_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a small, UI-safe summary of tool arguments."""
    result: dict[str, Any] = {}
    for key in (
        "path",
        "target_path",
        "file_path",
        "script_path",
        "resource_path",
        "scene_path",
        "command",
        "kind",
        "agent",
        "task",
        "query",
    ):
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, str) and len(value) > 180:
            value = value[:180] + "..."
        result[key] = value
    return result


def _event_result_count(result: Any, is_error: bool) -> int | None:
    """Best-effort 提取 server 工具结果的条目数，供事件展示行数统计。

    `grep_code`/`list_files`/`search_codebase` 等检索类工具的结果分别以
    `matches`/`files`/`results` 列表承载命中项；其它工具或出错时返回 None，
    前端据此回退为不带计数的展示文案。
    """
    if is_error or not isinstance(result, dict):
        return None
    for key in ("matches", "files", "results"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _event_result_summary(tool_name: str, result: Any, is_error: bool) -> dict[str, Any] | None:
    """Return a bounded, UI-safe summary for workflow event rendering."""
    if is_error or not isinstance(result, dict):
        return None
    if tool_name in {"read_file", "read_script"}:
        content = result.get("content")
        if not isinstance(content, str):
            return None
        preview = content[:EVENT_TEXT_PREVIEW_CHARS]
        offset = result.get("offset", 1)
        line_start = offset if isinstance(offset, int) and offset > 0 else 1
        return {
            "kind": "read",
            "path": str(result.get("path", "")),
            "line_start": line_start,
            "line_end": max(line_start, line_start + len(content.splitlines()) - 1),
            "content": preview,
            "truncated": bool(result.get("truncated", False)) or len(content) > len(preview),
        }
    if tool_name in {"grep_code", "search_codebase", "list_files"}:
        matches = _event_match_items(result)
        return {
            "kind": "grep",
            "pattern": str(result.get("pattern", result.get("query", ""))),
            "include": str(result.get("include", result.get("path", "project"))),
            "match_count": len(matches),
            "matches": matches[:EVENT_MATCH_PREVIEW_ITEMS],
            "truncated": bool(result.get("truncated", False))
            or len(matches) > EVENT_MATCH_PREVIEW_ITEMS,
        }
    return None


def _event_match_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize search-like result rows for the frontend workflow list."""
    raw_items = result.get("matches", result.get("results", result.get("files", [])))
    if not isinstance(raw_items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, dict):
            normalized.append(
                {
                    "path": str(item.get("path", item.get("file", ""))),
                    "line": item.get("line", item.get("line_no", "")),
                    "text": str(item.get("text", item.get("preview", ""))),
                }
            )
        else:
            normalized.append({"path": str(item), "line": "", "text": ""})
    return normalized


def _emit_orchestration_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if event_callback is None:
        return
    event_callback(event_type, payload)


def _history_timeline_payload(frame: Frame) -> dict[str, Any]:
    """Return the persisted timeline anchor for root and delegated frames."""
    return {
        "timeline_frame_id": frame.history_anchor_frame_id or frame.id,
        "timeline_message_index": (
            frame.history_anchor_message_index
            if frame.history_anchor_message_index is not None
            else len(frame.messages)
        ),
    }


def _estimate_stream_token_count(text: str) -> int:
    """Estimate tokens for an accumulated stream without model-specific dependencies."""
    if not text:
        return 0
    cjk_chars = 0
    other_bytes = 0
    for char in text:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            cjk_chars += 1
        else:
            other_bytes += len(char.encode("utf-8"))
    return max(cjk_chars + (other_bytes + 3) // 4, 1)


def _delta_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
    message_index: int,
    timeline_frame_id: str,
    timeline_message_index: int,
) -> Callable[[str, str, int | None], None] | None:
    """构造传给 `LLMProvider.chat` 的流式增量回调，转发为编排事件。

    Args:
        event_callback: 编排事件回调；为 None 时不产生增量事件。
        frame_id: 本轮所属的 agent 帧 id，供前端关联增量与对应消息。
        loop: 本轮在 `run_turn` 中的循环序号（从 1 开始）。
        message_index: 本次 LLM 响应即将写入 `frame.messages` 的位置，供历史交织。

    Returns:
        转发增量为 `agent_text_delta`/`agent_reasoning_delta` 事件的回调；
        `event_callback` 为 None 时返回 None。
    """
    if event_callback is None:
        return None

    reasoning_started_at = time.monotonic()
    accumulated_text: dict[str, str] = {"content": "", "reasoning": ""}
    chunk_index = 0

    # 上游 provider 可能把同一条 assistant 消息的 content 与
    # reasoning_content 交错发送。message_index 已经是一次 LLM 调用的稳定
    # 身份，不能再以通道切换作为正文分段边界，否则会截断正文并导致 final
    # 无法收敛替换流式消息。
    def _on_delta(kind: str, text: str, token_count: int | None) -> None:
        nonlocal chunk_index
        chunk_index += 1
        # 同一次 LLM 调用内的 reasoning/content 均使用同一个 segment。
        # 前端据此把 reasoning 合并进同一 Thought，并持续累积同一正文块。
        event_type = "agent_reasoning_delta" if kind == "reasoning" else "agent_text_delta"
        accumulated_text[kind] = accumulated_text.get(kind, "") + text
        payload: dict[str, Any] = {
            "frame_id": frame_id,
            "loop": loop,
            "message_index": message_index,
            "timeline_frame_id": timeline_frame_id,
            "timeline_message_index": timeline_message_index,
            "stream_segment": 0,
            "text": text,
            "append_delta": True,
            "provider_chunk_index": chunk_index,
            "provider_first_chunk": chunk_index == 1,
        }
        if kind == "reasoning":
            payload["elapsed_ms"] = max(int((time.monotonic() - reasoning_started_at) * 1000), 1)
            payload["token_count"] = (
                token_count
                if token_count is not None
                else _estimate_stream_token_count(accumulated_text[kind])
            )
        event_callback(event_type, payload)

    return _on_delta


def _record_cache_metrics(
    cache_metrics: CacheMetricsCollector | None,
    decision: CacheDecision | None,
    turn: AssistantTurn,
) -> None:
    """把本轮缓存决策与实际命中结果写入观测层（§16.1 非功能需求：仅日志/监控）。

    Args:
        cache_metrics: 进程内缓存指标聚合器；为 None 时不记录。
        decision: 本轮的 `CacheDecisionEngine.decide()` 结果；为 None 表示
            本次请求未启用缓存决策（如 provider 不支持显式缓存）。
        turn: 本轮 `LLMProvider.chat()` 的返回。
    """
    if cache_metrics is None or decision is None:
        return
    total = turn.total_input_tokens or 0
    cached = turn.cached_tokens or 0
    hit_ratio = cached / total if total > 0 else 0.0
    cache_metrics.record(
        CacheMetricsSnapshot(
            cache_key=decision.cache_key,
            repo_fingerprint=decision.repo_fingerprint,
            tool_schema_version=decision.tool_schema_version,
            cached_tokens=cached,
            total_tokens=total,
            hit_ratio=hit_ratio,
            prefix_segments_used=decision.segments_used,
            cache_enabled=decision.enabled,
        )
    )


def _emit_cache_hit_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame: Frame,
    loop: int,
    turn: AssistantTurn,
) -> None:
    """命中上下文缓存时发出 `cache_hit` 事件（§16.1）。

    仅在 usage 报告了命中缓存 token（`cached_tokens > 0`）且总输入 token 可用时
    发出；未命中则静默，避免在消息列表里堆噪音。不附带"节省比例"——百炼的
    实际折扣因命中类型（隐式/显式）与路由到的具体模型而异，usage 字段无法
    反推具体属于哪种，硬编码一个比例只会是误导性的假精度。

    Args:
        event_callback: 编排事件回调；为 None 时不产生事件。
        frame: 本轮所属的 agent 帧。
        loop: 本轮在 `run_turn` 中的循环序号（从 1 开始）。
        turn: 本轮 `LLMProvider.chat()` 的返回，携带 `cached_tokens`/
            `total_input_tokens`/`cache_creation_tokens`。
    """
    cached = turn.cached_tokens
    total = turn.total_input_tokens
    if event_callback is None or not cached or cached <= 0 or not total or total <= 0:
        return
    event_callback(
        "cache_hit",
        {
            "frame_id": frame.id,
            "loop": loop,
            "cached_tokens": cached,
            "total_input_tokens": total,
            "cache_creation_tokens": turn.cache_creation_tokens or 0,
        },
    )


def _emit_context_usage_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame: Frame,
    loop: int,
    turn: AssistantTurn,
    token_limit: int | None,
) -> None:
    """Emit current prompt usage against the configured context limit."""
    used = turn.total_input_tokens
    if (
        event_callback is None
        or used is None
        or used < 0
        or token_limit is None
        or token_limit <= 0
    ):
        return
    event_callback(
        "context_usage",
        {
            "frame_id": frame.id,
            "loop": loop,
            "used_tokens": used,
            "token_limit": token_limit,
        },
    )


def _fallback_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
) -> Callable[[str, str], None] | None:
    """构造传给 `LLMProvider.chat` 的降级回调，转发为 `agent_model_fallback` 事件。

    主模型请求失败、provider 即将用 `fallback_model` 重试时触发一次，
    让前端/日志能看到"这轮回复换了模型"，而不是看到推理风格突变却不知道原因。

    Args:
        event_callback: 编排事件回调；为 None 时不产生降级事件。
        frame_id: 本轮所属的 agent 帧 id。
        loop: 本轮在 `run_turn` 中的循环序号（从 1 开始）。

    Returns:
        转发降级信息为 `agent_model_fallback` 事件的回调；`event_callback`
        为 None 时返回 None。
    """
    if event_callback is None:
        return None

    def _on_fallback(primary_model: str, fallback_model: str) -> None:
        event_callback(
            "agent_model_fallback",
            {
                "frame_id": frame_id,
                "loop": loop,
                "primary_model": primary_model,
                "fallback_model": fallback_model,
            },
        )

    return _on_fallback


async def run_turn(
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
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    cache_engine: CacheDecisionEngine | None = None,
    cache_metrics: CacheMetricsCollector | None = None,
    context_token_limit: int | None = None,
    map_worker_structured_output_enabled: bool = True,
    map_worker_response_contract_mode: MapResponseMode = "prompt_only",
    map_worker_structured_correction_limit: int = 1,
    map_worker_structured_thinking_budget: int = 0,
) -> StepResult:
    """驱动当前会话的活跃帧完成一轮（或多轮）编排循环。

    Args:
        session: 当前会话，`agent_stack` 至少含一个根帧。
        llm: 大模型 provider。
        security: 当前会话的安全边界配置，供权限闸使用。
        tool_ctx: server 工具执行上下文。
        max_turns: 本次调用允许驱动的最大 LLM 往返轮数，超出则返回
            `ErrorResult`，避免死循环消耗配额。
        cache_engine: 上下文缓存决策引擎（§16.1）；为 None 或
            `llm.supports_prompt_cache=False` 时不标记任何显式缓存断点。
        cache_metrics: 缓存命中率观测聚合器；为 None 时不记录指标。
        map_worker_structured_output_enabled: 是否启用同 Frame 结构化纠错。
        map_worker_response_contract_mode: 最终地图 worker 回合的显式响应模式。
        map_worker_structured_correction_limit: 单 Frame 本地纠错上限。
        map_worker_structured_thinking_budget: 最终结构化回合的思考预算。

    Returns:
        `ToolCallsResult`（需前端执行/确认）、`FinalResult`（已得到最终回复）
        或 `ErrorResult`（LLM 调用失败/达到轮数上限）。
    """
    logger.info("Agent run_turn start session=%s max_turns=%d", session.session_id, max_turns)
    # 本轮整改：每次 run_turn 创建独立的 artifact store 实例，
    # 地图子 worker 完整结果落盘后父帧只保留摘要 + artifact_ref，
    # 大幅减少上下文膨胀。生命周期与 run_turn 一致，HTTP 请求结束后自动 GC。
    delegate_artifact_store = DelegateArtifactStore(
        tool_ctx.security.project_root,
        session.session_id,
        session.session_epoch,
    )
    frame_turns: dict[str, int] = {}  # 非地图帧仍只统计本次 run_turn 的轮数
    # frame_id -> 其中单独计入 edit_map_max_turns 预算的轮数（tool_calls 仅含 edit_map 时）
    frame_edit_map_turns: dict[str, int] = {}
    driver_turn_limit = (
        max_turns
        + max(0, map_worker_structured_correction_limit)
        + 1
    )
    for loop_index in range(driver_turn_limit):
        frame = session.top_frame()
        if frame is None:
            logger.error("Agent run_turn failed: empty frame stack session=%s", session.session_id)
            return ErrorResult(text="会话没有活跃的 agent 帧", error_code="missing_agent_frame")

        if (
            frame.force_text_only
            and _map_output_schema_for_frame(frame) == _MAP_OUTPUT_SCHEMA_V1
            and frame.response_contract_mode is None
        ):
            arm_map_worker_structured_completion(
                frame,
                mode=map_worker_response_contract_mode,
                correction_limit=(
                    map_worker_structured_correction_limit
                    if map_worker_structured_output_enabled
                    else 0
                ),
            )

        if frame.forced_completion_text is not None:
            forced_text = frame.forced_completion_text
            frame.forced_completion_text = None
            logger.info(
                "Finishing frame from deterministic completion gate session=%s frame=%s agent=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
            )
            forced_result = await _finish_frame(
                session,
                forced_text,
                agent_prompt_factory,
                event_callback,
                delegate_artifact_store,
            )
            if forced_result is not None:
                return forced_result
            continue

        persistent_map_budget = _uses_persistent_map_budget(frame)
        if persistent_map_budget:
            current_scope_owns_task = (
                session.map_request_scope.activates_map_gate
                and session.map_request_scope.map_task_id == session.map_task_state.task_id
            )
            if session.map_task_state.status == "paused" and current_scope_owns_task:
                _emit_orchestration_event(
                    event_callback,
                    "map_task_paused",
                    {
                        "frame_id": frame.id,
                        "pause_kind": session.map_task_state.pause_kind,
                        "reason": session.map_task_state.pause_reason,
                        "pause_report": session.map_task_state.pause_report,
                        "checkpoint": session.map_task_state.checkpoint or {},
                        "counters": session.map_task_state.counters.__dict__,
                    },
                )
                return ErrorResult(
                    text=map_pause_message(session.map_task_state),
                    error_code="agent_turn_budget_exhausted",
                )
            _sync_map_progress_budget(session, frame)
            increment_map_counter(session.map_task_state, "llm_turns")
        used = (
            frame.persistent_turn_count if persistent_map_budget else frame_turns.get(frame.id, 0)
        )
        # 这里只做一个宽松的总量护栏（max_turns + edit_map_max_turns），防止帧无限循环；
        # 哪个预算先耗尽由下面 tool_calls 揭晓后的精确分类检查负责。
        structured_budget = (
            frame.structured_correction_limit + 1
            if frame.force_text_only
            and _map_output_schema_for_frame(frame) == _MAP_OUTPUT_SCHEMA_V1
            else 0
        )
        total_budget = (
            frame.agent.max_turns
            + (frame.agent.edit_map_max_turns or 0)
            + structured_budget
        )
        if used >= total_budget:
            result = await _handle_frame_turns_exhausted(
                session,
                frame,
                "总轮数",
                total_budget,
                agent_prompt_factory,
                event_callback,
                delegate_artifact_store,
            )
            if result is not None:
                return result
            continue

        if persistent_map_budget:
            frame.persistent_turn_count = used + 1
        else:
            frame_turns[frame.id] = used + 1

        try:
            visible_effective_tools = _stage_effective_tools(session, frame)
            visible_tools = tools_for(visible_effective_tools, frame.active_deferred_tools)
            logger.info(
                "Agent frame step session=%s loop=%d frame=%s agent=%s depth=%d messages=%d tools=%d",
                session.session_id,
                loop_index + 1,
                frame.id,
                frame.agent.name,
                frame.depth,
                len(frame.messages),
                len(visible_tools),
            )
            _emit_orchestration_event(
                event_callback,
                "agent_step",
                {
                    "loop": loop_index + 1,
                    "frame_id": frame.id,
                    "agent": frame.agent.name,
                    "depth": frame.depth,
                    "visible_tools": len(visible_tools),
                },
            )
            effort = _resolve_effort(session, frame)
            resolved_model = _resolve_request_model(
                frame.agent,
                effort,
                model_selector,
                model_override,
            )
            final_structured_turn = (
                frame.force_text_only
                and _map_output_schema_for_frame(frame) == _MAP_OUTPUT_SCHEMA_V1
            )
            response_contract = (
                ResponseContract(
                    mode=frame.response_contract_mode or map_worker_response_contract_mode,
                    schema_name=_MAP_OUTPUT_SCHEMA_V1,
                    schema=specialized_map_worker_schema(frame),
                    fallback_guidance=render_map_worker_response_guidance(
                        frame,
                        "prompt_only",
                    ),
                )
                if final_structured_turn
                else None
            )
            if event_callback is not None and resolved_model is not None:
                event_callback(
                    "agent_model_selected",
                    {
                        "frame_id": frame.id,
                        "loop": loop_index + 1,
                        "model": resolved_model,
                    },
                )
            cache_decision: CacheDecision | None = None
            if cache_engine is not None and llm.supports_prompt_cache:
                cache_decision = await cache_engine.decide(
                    session_id=session.session_id,
                    frame_id=frame.id,
                    messages=frame.messages,
                    tools=visible_tools,
                    project_root=tool_ctx.security.project_root,
                    rag_index_path=tool_ctx.rag_index_path,
                    compact_digest=(
                        frame.compact_snapshot.digest if frame.compact_snapshot is not None else ""
                    ),
                )

            turn = await llm.chat(
                frame.messages,
                visible_tools,
                model=resolved_model,
                temperature=0.0 if final_structured_turn else _resolve_temperature(effort),
                thinking_budget=(
                    map_worker_structured_thinking_budget
                    if final_structured_turn
                    else resolve_thinking_budget(effort, thinking_budget_selector)
                ),
                on_delta=_delta_callback(
                    event_callback,
                    frame.id,
                    loop_index + 1,
                    len(frame.messages),
                    frame.history_anchor_frame_id or frame.id,
                    (
                        frame.history_anchor_message_index
                        if frame.history_anchor_message_index is not None
                        else len(frame.messages)
                    ),
                ),
                on_fallback=_fallback_callback(event_callback, frame.id, loop_index + 1),
                cache_breakpoints=(
                    cache_decision.breakpoints
                    if cache_decision is not None and cache_decision.enabled
                    else None
                ),
                response_contract=response_contract,
            )
        except LLMError as exc:
            logger.warning(
                "Agent LLM step failed session=%s frame=%s error=%s",
                session.session_id,
                frame.id,
                exc,
            )
            if (
                session.map_request_scope.activates_map_gate
                and session.map_request_scope.map_task_id == session.map_task_state.task_id
                and session.map_task_state.status == "running"
            ):
                session.map_task_state.make_checkpoint(
                    "provider_exhausted",
                    pause_kind="provider_exhausted",
                )
                return ErrorResult(
                    text=map_pause_message(session.map_task_state),
                    error_code="provider_exhausted",
                )
            return ErrorResult(text=str(exc), error_code="provider_exhausted")

        frame.messages.append(turn.raw_message)
        if final_structured_turn:
            frame.structured_response_model = turn.model or resolved_model
            frame.structured_finish_reason = turn.finish_reason
            frame.structured_thinking_budget = map_worker_structured_thinking_budget
            if turn.response_mode is not None:
                frame.response_contract_mode = turn.response_mode
        _record_cache_metrics(cache_metrics, cache_decision, turn)
        _emit_context_usage_event(event_callback, frame, loop_index + 1, turn, context_token_limit)
        _emit_cache_hit_event(event_callback, frame, loop_index + 1, turn)

        if not turn.tool_calls:
            if (
                _map_output_schema_for_frame(frame) == _MAP_OUTPUT_SCHEMA_V1
                and not frame.force_text_only
            ):
                arm_map_worker_structured_completion(
                    frame,
                    mode=map_worker_response_contract_mode,
                    correction_limit=(
                        map_worker_structured_correction_limit
                        if map_worker_structured_output_enabled
                        else 0
                    ),
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_structured_final_turn_armed",
                    {
                        "frame_id": frame.id,
                        "schema_version": _MAP_OUTPUT_SCHEMA_V1,
                        "response_mode": frame.response_contract_mode,
                    },
                )
                continue
            finish_result = await _finish_frame(
                session,
                turn.content or "",
                agent_prompt_factory,
                event_callback,
                delegate_artifact_store,
            )
            if finish_result is not None:
                logger.info(
                    "Agent run_turn final session=%s loop=%d", session.session_id, loop_index + 1
                )
                return finish_result
            continue  # 子帧已结束，继续驱动父帧

        tool_names = [call.name for call in turn.tool_calls]
        logger.info(
            "Agent requested tools session=%s frame=%s agent=%s names=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
            tool_names,
        )
        _emit_orchestration_event(
            event_callback,
            "agent_tool_calls",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "tools": tool_names,
            },
        )
        if _append_map_write_protocol_errors(frame, turn.tool_calls):
            continue
        if _append_map_plan_protocol_errors(session, frame, turn.tool_calls):
            continue
        if _append_reader_fallback_protocol_errors(session, frame, turn.tool_calls):
            continue
        if _append_map_write_followup_protocol_errors(session, frame, turn.tool_calls):
            continue

        # edit_map 调用按 edit_map_max_turns 单独计算预算，不挤占该 agent 处理其他
        # 工具（read_scene_tree/截图/规划等）的常规 max_turns 配额；反之亦然。
        is_edit_map_turn = bool(tool_names) and all(name == "edit_map" for name in tool_names)
        if is_edit_map_turn and frame.agent.edit_map_max_turns is not None:
            edit_map_used = (
                frame.persistent_edit_map_turn_count
                if persistent_map_budget
                else frame_edit_map_turns.get(frame.id, 0)
            ) + 1
            if persistent_map_budget:
                frame.persistent_edit_map_turn_count = edit_map_used
            else:
                frame_edit_map_turns[frame.id] = edit_map_used
            if edit_map_used > frame.agent.edit_map_max_turns:
                result = await _handle_frame_turns_exhausted(
                    session,
                    frame,
                    "edit_map 调用次数",
                    frame.agent.edit_map_max_turns,
                    agent_prompt_factory,
                    event_callback,
                    delegate_artifact_store,
                )
                if result is not None:
                    return result
                continue
        else:
            total_used = (
                frame.persistent_turn_count
                if persistent_map_budget
                else frame_turns.get(frame.id, 0)
            )
            edit_used = (
                frame.persistent_edit_map_turn_count
                if persistent_map_budget
                else frame_edit_map_turns.get(frame.id, 0)
            )
            general_used = total_used - edit_used
            if general_used > frame.agent.max_turns:
                result = await _handle_frame_turns_exhausted(
                    session,
                    frame,
                    "常规轮数",
                    frame.agent.max_turns,
                    agent_prompt_factory,
                    event_callback,
                    delegate_artifact_store,
                )
                if result is not None:
                    return result
                continue

        permission_ctx = PermissionContext(
            security=security,
            effective_tools=frozenset(visible_effective_tools),
            deny_rules=security.deny_rules,
            allow_rules=security.allow_rules,
            session_allow=session_allow or set(),
        )
        delegate_calls = [
            call for call in turn.tool_calls if call.name in {"delegate", "delegate_many"}
        ]
        if delegate_calls:
            if len(turn.tool_calls) != 1:
                _append_delegate_protocol_errors(frame, turn.tool_calls)
                continue

            call = delegate_calls[0]
            tool = REGISTRY.get(call.name)
            if tool is None:
                logger.warning(
                    "Delegate tool missing from registry session=%s tool=%s",
                    session.session_id,
                    call.name,
                )
                frame.messages.append(
                    _tool_message(call.id, f"{call.name} 工具未注册", is_error=True)
                )
                continue

            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                continue
            assert args is not None

            if _requires_create_plan_before_map_delegate(session, frame, call.name, args):
                logger.warning(
                    "Delegate rejected: complex map task requires create_plan first session=%s frame=%s agent=%s tool=%s",
                    session.session_id,
                    frame.id,
                    frame.agent.name,
                    call.name,
                )
                frame.messages.append(
                    _tool_message(
                        call.id,
                        "复杂地图任务必须先调用 create_plan 生成用户可见计划；"
                        "本轮委派未执行。请下一轮只调用 create_plan，"
                        "计划步骤应包含读取地图上下文、规划可达路线、预览/确认、小批写入、验证和截图复核。",
                        is_error=True,
                    )
                )
                continue

            decision = check(tool, args, permission_ctx)
            if decision == "deny":
                logger.warning(
                    "Delegate denied session=%s frame=%s tool=%s agent=%s",
                    session.session_id,
                    frame.id,
                    tool.name,
                    frame.agent.name,
                )
                frame.messages.append(
                    _tool_message(
                        call.id, "被拒绝：当前 agent/权限模式不允许 delegate", is_error=True
                    )
                )
                continue

            _emit_orchestration_event(
                event_callback,
                "delegate_start",
                {
                    "frame_id": frame.id,
                    "agent": frame.agent.name,
                    "tool": call.name,
                    "args": _event_tool_args(args),
                    **_history_timeline_payload(frame),
                },
            )
            if call.name == "delegate_many":
                await _start_delegate_group(
                    session=session,
                    frame=frame,
                    call_id=call.id,
                    args=args,
                    prompt_factory=agent_prompt_factory,
                    event_callback=event_callback,
                )
            else:
                await _start_delegate_frame(
                    session=session,
                    frame=frame,
                    call_id=call.id,
                    args=args,
                    prompt_factory=agent_prompt_factory,
                    event_callback=event_callback,
                )
            continue

        plan_calls = [call for call in turn.tool_calls if call.name == "create_plan"]
        if plan_calls:
            if len(turn.tool_calls) != 1:
                _append_create_plan_protocol_errors(frame, turn.tool_calls)
                continue

            call = plan_calls[0]
            tool = REGISTRY.get(call.name)
            if tool is None:
                logger.warning(
                    "Create_plan tool missing from registry session=%s", session.session_id
                )
                frame.messages.append(
                    _tool_message(call.id, "create_plan 工具未注册", is_error=True)
                )
                continue

            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                continue
            assert args is not None

            decision = check(tool, args, permission_ctx)
            if decision == "deny":
                logger.warning(
                    "Create_plan denied session=%s frame=%s agent=%s",
                    session.session_id,
                    frame.id,
                    frame.agent.name,
                )
                frame.messages.append(
                    _tool_message(
                        call.id, "被拒绝：当前 agent/权限模式不允许 create_plan", is_error=True
                    )
                )
                continue

            _handle_create_plan(
                session=session,
                frame=frame,
                call_id=call.id,
                args=args,
                event_callback=event_callback,
            )
            continue

        platform_route_handled, platform_validation_response = (
            _route_unvalidated_platform_writes_to_validator(
                session=session,
                frame=frame,
                calls=turn.tool_calls,
                project_root=tool_ctx.security.project_root,
                event_callback=event_callback,
            )
        )
        if platform_validation_response is not None:
            return platform_validation_response
        if platform_route_handled:
            continue

        front_calls: list[FrontToolCall] = []
        pending_items: list[_PendingItem] = []
        turn_id = session.new_turn_id()

        # 第一遍：分类每个 tool call，不执行 server handler（同步、保留顺序）。
        for call in turn.tool_calls:
            tool = REGISTRY.get(call.name)
            if tool is None:
                logger.warning(
                    "Unknown tool requested session=%s frame=%s tool=%s",
                    session.session_id,
                    frame.id,
                    call.name,
                )
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(call.id, f"未知工具：{call.name}", is_error=True)
                    )
                )
                continue

            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                pending_items.append(_PendingToolMessage(parse_error))
                continue
            assert args is not None
            if tool.name in MAP_WRITE_TOOL_NAMES:
                repeated_error = repeated_map_tool_failure_error(
                    session,
                    tool.name,
                    args,
                )
                if repeated_error is not None:
                    logger.warning(
                        "Repeated map tool failure blocked session=%s frame=%s tool=%s",
                        session.session_id,
                        frame.id,
                        tool.name,
                    )
                    pending_items.append(
                        _PendingToolMessage(_tool_message(call.id, repeated_error, is_error=True))
                    )
                    continue
            if tool.name == "edit_map":
                normalization = normalize_edit_map_resources(
                    tool_ctx.security.project_root,
                    args,
                )
                if normalization.error_code is not None:
                    error_message = normalization.error_message or (
                        "地图资源规范化失败，edit_map 未下发。"
                    )
                    remember_map_tool_failure(
                        session,
                        tool.name,
                        args,
                        normalization.error_code,
                        error_message,
                    )
                    logger.warning(
                        "Map resource normalization blocked session=%s frame=%s " "error_code=%s",
                        session.session_id,
                        frame.id,
                        normalization.error_code,
                    )
                    _emit_orchestration_event(
                        event_callback,
                        "map_resource_normalization_blocked",
                        {
                            "frame_id": frame.id,
                            "tool": tool.name,
                            "error_code": normalization.error_code,
                        },
                    )
                    pending_items.append(
                        _PendingToolMessage(_tool_message(call.id, error_message, is_error=True))
                    )
                    continue
                args = normalization.args
                repeated_error = repeated_map_tool_failure_error(
                    session,
                    tool.name,
                    args,
                )
                if repeated_error is not None:
                    logger.warning(
                        "Normalized repeated map tool failure blocked "
                        "session=%s frame=%s tool=%s",
                        session.session_id,
                        frame.id,
                        tool.name,
                    )
                    pending_items.append(
                        _PendingToolMessage(_tool_message(call.id, repeated_error, is_error=True))
                    )
                    continue
                if normalization.rewritten_operations:
                    logger.info(
                        "Map resources normalized session=%s frame=%s operations=%d",
                        session.session_id,
                        frame.id,
                        normalization.rewritten_operations,
                    )
                    _emit_orchestration_event(
                        event_callback,
                        "map_resources_normalized",
                        {
                            "frame_id": frame.id,
                            "tool": tool.name,
                            "rewritten_operations": normalization.rewritten_operations,
                        },
                    )
            args = _with_map_write_metadata(
                session=session,
                frame=frame,
                call_id=call.id,
                tool_name=tool.name,
                args=args,
            )
            if tool.name == "describe_map_region":
                cached_region = _cached_map_region_summary(session, args)
                if cached_region is not None:
                    logger.info(
                        "Map region read served from cache session=%s frame=%s target=%s layer=%s",
                        session.session_id,
                        frame.id,
                        args.get("target_path"),
                        args.get("map_layer"),
                    )
                    _emit_orchestration_event(
                        event_callback,
                        "map_cache_hit",
                        {"kind": "region", "target": args.get("target_path")},
                    )
                    pending_items.append(
                        _PendingToolMessage(
                            _tool_message(
                                call.id,
                                {"status": "applied", "result": cached_region},
                            )
                        )
                    )
                    continue
                resumed_read_error = _resumed_full_map_read_error(session, args)
                if resumed_read_error is not None:
                    pending_items.append(
                        _PendingToolMessage(
                            _tool_message(call.id, resumed_read_error, is_error=True)
                        )
                    )
                    continue
            if tool.name in MAP_VALIDATION_TOOL_NAMES:
                cached_validation = cached_validation_result(session, tool.name, args)
                if cached_validation is not None:
                    logger.info(
                        "Map validation served from cache session=%s frame=%s target=%s",
                        session.session_id,
                        frame.id,
                        args.get("target_path"),
                    )
                    _emit_orchestration_event(
                        event_callback,
                        "map_cache_hit",
                        {"kind": "validation", "target": args.get("target_path")},
                    )
                    pending_items.append(
                        _PendingToolMessage(
                            _tool_message(
                                call.id,
                                {"status": "applied", "result": cached_validation},
                            )
                        )
                    )
                    continue
                validation_error = _map_validation_arg_error(session, tool.name, args)
                if validation_error is not None:
                    logger.warning(
                        "Map validation blocked by progress policy session=%s frame=%s tool=%s error=%s",
                        session.session_id,
                        frame.id,
                        tool.name,
                        validation_error,
                    )
                    pending_items.append(
                        _PendingToolMessage(_tool_message(call.id, validation_error, is_error=True))
                    )
                    continue
            if tool.name in MAP_WRITE_TOOL_NAMES:
                stage_error = (
                    None
                    if _frame_in_active_map_edit(session, frame)
                    else "当前用户请求未显式授权地图内容编辑，地图写工具已拒绝"
                )
                if stage_error is None:
                    stage_error = map_write_stage_error(session, tool.name, args)
                if stage_error is not None:
                    logger.warning(
                        "Map write blocked by progress stage session=%s frame=%s tool=%s error=%s",
                        session.session_id,
                        frame.id,
                        tool.name,
                        stage_error,
                    )
                    pending_items.append(
                        _PendingToolMessage(_tool_message(call.id, stage_error, is_error=True))
                    )
                    continue

            decision = check(tool, args, permission_ctx)
            logger.info(
                "Tool permission decision session=%s frame=%s tool=%s side=%s decision=%s",
                session.session_id,
                frame.id,
                tool.name,
                tool.side,
                decision,
            )
            if decision == "deny":
                denial_message = (
                    f"当前地图工作流处于 {session.map_task_state.stage} 阶段，"
                    f"不允许调用 {tool.name}"
                    if tool.name not in permission_ctx.effective_tools
                    else f"被拒绝：当前权限模式/安全边界不允许调用 {tool.name}"
                )
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(
                            call.id,
                            denial_message,
                            is_error=True,
                        )
                    )
                )
                continue

            if tool.side == "server":
                pending_items.append(_PendingServerCall(call_id=call.id, tool=tool, args=args))
            else:
                front_calls.append(
                    FrontToolCall(
                        id=call.id,
                        name=tool.name,
                        input=args,
                        needs_confirm=decision == "ask",
                        frame_id=frame.id,
                        agent=frame.agent.name,
                        render_kind=tool.render_kind,
                    )
                )

        # 第二遍：执行 server 工具——`is_concurrency_safe` 的一组用
        # `asyncio.gather` 并发执行，其余按原始顺序串行执行。
        call_ctx = replace(
            tool_ctx,
            effective_tools=frozenset(visible_effective_tools),
            agent_effective_tools=frozenset(frame.agent.effective_tools),
            workflow_stage=(
                session.map_task_state.stage if _uses_persistent_map_budget(frame) else None
            ),
            agent_role=frame.agent.role,
            worker_mode=frame.agent.worker_mode,
        )
        server_calls = [item for item in pending_items if isinstance(item, _PendingServerCall)]
        concurrent_calls = [item for item in server_calls if item.tool.is_concurrency_safe]
        sequential_calls = [item for item in server_calls if not item.tool.is_concurrency_safe]

        results: dict[str, tuple[Any, bool]] = {}
        if concurrent_calls:
            logger.info(
                "Running concurrent server tools session=%s count=%d",
                session.session_id,
                len(concurrent_calls),
            )
            for item in concurrent_calls:
                _emit_orchestration_event(
                    event_callback,
                    "server_tool_start",
                    {
                        "frame_id": frame.id,
                        "agent": frame.agent.name,
                        "tool": item.tool.name,
                        "args": _event_tool_args(item.args),
                        "concurrent": True,
                        **_history_timeline_payload(frame),
                    },
                )
            outcomes = await asyncio.gather(
                *(_invoke_server_tool(item.tool, item.args, call_ctx) for item in concurrent_calls)
            )
            for item, outcome in zip(concurrent_calls, outcomes):
                results[item.call_id] = outcome
                _emit_orchestration_event(
                    event_callback,
                    "server_tool_result",
                    {
                        "frame_id": frame.id,
                        "agent": frame.agent.name,
                        "tool": item.tool.name,
                        "args": _event_tool_args(item.args),
                        "is_error": outcome[1],
                        "result_count": _event_result_count(outcome[0], outcome[1]),
                        "result_summary": _event_result_summary(
                            item.tool.name, outcome[0], outcome[1]
                        ),
                        **_history_timeline_payload(frame),
                    },
                )
        for item in sequential_calls:
            logger.info(
                "Running sequential server tool session=%s tool=%s",
                session.session_id,
                item.tool.name,
            )
            _emit_orchestration_event(
                event_callback,
                "server_tool_start",
                {
                    "frame_id": frame.id,
                    "agent": frame.agent.name,
                    "tool": item.tool.name,
                    "args": _event_tool_args(item.args),
                    "concurrent": False,
                    **_history_timeline_payload(frame),
                },
            )
            results[item.call_id] = await _invoke_server_tool(item.tool, item.args, call_ctx)
            _emit_orchestration_event(
                event_callback,
                "server_tool_result",
                {
                    "frame_id": frame.id,
                    "agent": frame.agent.name,
                    "tool": item.tool.name,
                    "args": _event_tool_args(item.args),
                    "is_error": results[item.call_id][1],
                    "result_count": _event_result_count(*results[item.call_id]),
                    "result_summary": _event_result_summary(item.tool.name, *results[item.call_id]),
                    **_history_timeline_payload(frame),
                },
            )

        if server_calls and event_callback is not None:
            # ponytail: sync event stores do not need flushing; this yields for async transports.
            await asyncio.sleep(0)

        # 第三遍：按 `tool_calls` 原始顺序把结果 append 回 `frame.messages`。
        for item in pending_items:
            if isinstance(item, _PendingToolMessage):
                frame.messages.append(item.message)
                continue

            result, is_error = results[item.call_id]
            if not is_error and item.tool.name == "search_tools":
                activated = {
                    str(name)
                    for name in result.get("activated_tools", [])
                    if name in frame.agent.effective_tools
                    and name in REGISTRY
                    and REGISTRY[str(name)].deferred
                }
                frame.active_deferred_tools.update(activated)
                result["activated_tools"] = sorted(activated)
                if activated:
                    frame.search_tools_noop_count = 0
                else:
                    frame.search_tools_noop_count += 1
                    if frame.search_tools_noop_count >= NOOP_SEARCH_TOOLS_HINT_THRESHOLD:
                        result["no_more_tools_hint"] = (
                            "search_tools 连续没有激活新工具；若已有足够事实，请输出结果，"
                            "缺失内容写入 missing_inputs。"
                        )
                logger.info(
                    "Deferred tools activated session=%s frame=%s tools=%s",
                    session.session_id,
                    frame.id,
                    sorted(activated),
                )
            frame.messages.append(_tool_message(item.call_id, result, is_error=is_error))

        if (
            # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
            frame.agent.map_stage == "reader"
            and frame.map_reader_detailed_region_ready
            and not frame.force_text_only
        ):
            artifact_read_completed = any(
                item.tool.name == "read_file"
                and not results[item.call_id][1]
                and ".ai_agent_service/artifacts/"
                in str(item.args.get("path", "")).replace("\\", "/")
                for item in server_calls
            )
            if artifact_read_completed:
                arm_map_worker_structured_completion(
                    frame,
                    mode=map_worker_response_contract_mode,
                    correction_limit=(
                        map_worker_structured_correction_limit
                        if map_worker_structured_output_enabled
                        else 0
                    ),
                )
                logger.info(
                    "Map reader armed for text-only completion session=%s frame=%s",
                    session.session_id,
                    frame.id,
                )

        if front_calls:
            if len(front_calls) > 1 and all(
                call.name in MAP_WRITE_TOOL_NAMES for call in front_calls
            ):
                state = session.map_task_state
                replace_map_state_field(
                    state,
                    "plan_version",
                    max(1, state.plan_version),
                )
                for batch_index, call in enumerate(front_calls):
                    call.input.setdefault("plan_version", state.plan_version)
                    call.input.setdefault("batch_index", batch_index)
                replace_map_state_field(
                    state,
                    "pending_batches",
                    [_queued_front_call(call) for call in front_calls[1:]],
                )
                front_calls = front_calls[:1]
                assistant_message = frame.messages[-1] if frame.messages else {}
                raw_tool_calls = assistant_message.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    assistant_message["tool_calls"] = [
                        item
                        for item in raw_tool_calls
                        if isinstance(item, dict) and item.get("id") == front_calls[0].id
                    ]
                logger.info(
                    "Map batch queue created session=%s plan_version=%d pending=%d",
                    session.session_id,
                    state.plan_version,
                    len(state.pending_batches),
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_batch_queue_created",
                    {
                        "plan_version": state.plan_version,
                        "batch_count": len(state.pending_batches) + 1,
                    },
                )
            session.set_pending(
                turn_id,
                [c.id for c in front_calls],
                {
                    c.id: {
                        "name": c.name,
                        "input": c.input,
                        "frame_id": c.frame_id,
                        "agent": c.agent,
                        # 本轮整改：持久化 needs_confirm 标志，
                        # 前端恢复时可据此区分自动执行与需用户确认的调用
                        "needs_confirm": c.needs_confirm,
                    }
                    for c in front_calls
                },
            )
            logger.info(
                "Front tool calls pending session=%s turn_id=%s count=%d needs_confirm=%d",
                session.session_id,
                turn_id,
                len(front_calls),
                sum(1 for call in front_calls if call.needs_confirm),
            )
            return ToolCallsResult(turn_id=turn_id, text=turn.content, calls=front_calls)

    logger.warning(
        "Agent run_turn reached max turns session=%s max_turns=%d", session.session_id, max_turns
    )
    if (
        session.map_request_scope.activates_map_gate
        and session.map_request_scope.map_task_id == session.map_task_state.task_id
        and session.map_task_state.status == "running"
    ):
        session.map_task_state.make_checkpoint(
            "budget_exhausted",
            pause_kind="budget_exhausted",
        )
        return ErrorResult(
            text=map_pause_message(session.map_task_state),
            error_code="agent_turn_budget_exhausted",
        )
    return ErrorResult(
        text="已达到本轮最大循环次数，请精简任务或拆分请求后重试",
        error_code="agent_turn_budget_exhausted",
    )
