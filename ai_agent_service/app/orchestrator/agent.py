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
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from app.agents.bundled import get_agent
from app.agents.types import EFFORT_LEVELS, AgentDefinition, EffortLevel, Frame
from app.llm.cache_decision_engine import CacheDecision, CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector, CacheMetricsSnapshot
from app.llm.provider import AssistantTurn, LLMError, LLMProvider
from app.permissions.engine import PermissionContext, SessionAllowGrant, check
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, tools_for
from app.orchestrator.map_workers import (
    MAP_WRITE_TOOL_NAMES,
    build_dynamic_map_worker,
    is_map_worker_write_mode,
    is_map_write_tool,
    validate_map_write_args,
)
from app.orchestrator.map_progress import (
    cached_validation_result,
    map_write_stage_error,
    validation_call_error,
)

MAX_AGENT_DEPTH = 4
EVENT_TEXT_PREVIEW_CHARS = 24_000
EVENT_MATCH_PREVIEW_ITEMS = 20
NOOP_SEARCH_TOOLS_HINT_THRESHOLD = 2
_INTEGER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NUMBER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")

logger = logging.getLogger(__name__)

AgentPromptFactory = Callable[[AgentDefinition, str], Awaitable[str]]


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
        return str(exc), True


_TOOL_HISTORY_MAX_JSON_CHARS = 80_000
_TOOL_HISTORY_MAX_STRING_CHARS = 16_000
_TOOL_HISTORY_MAX_LIST_ITEMS = 80
_TOOL_HISTORY_MAX_DICT_ITEMS = 120
_TOOL_HISTORY_DROP_KEYS = frozenset(
    {"data_url", "base64", "image_base64", "screenshot_base64", "binary", "bytes"}
)


def _json_char_size(value: Any) -> int:
    """粗略计算 JSON 值序列化后的字符长度。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _summarize_history_text(text: str, max_chars: int = _TOOL_HISTORY_MAX_STRING_CHARS) -> str:
    """保留长文本的开头和结尾，中间省略以控制 history 体积。"""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return text[:head] + f"\n... ({omitted} chars omitted for history) ...\n" + text[-tail:]


def _bounded_history_value(
    value: Any,
    *,
    max_string_chars: int = _TOOL_HISTORY_MAX_STRING_CHARS,
    max_list_items: int = _TOOL_HISTORY_MAX_LIST_ITEMS,
    max_dict_items: int = _TOOL_HISTORY_MAX_DICT_ITEMS,
) -> Any:
    """递归压缩任意 server/delegate 工具结果，作为写入 history 的最后防线。"""
    if isinstance(value, str):
        return _summarize_history_text(value, max_string_chars)
    if isinstance(value, list):
        bounded = [
            _bounded_history_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
            for item in value[:max_list_items]
        ]
        omitted = len(value) - max_list_items
        if omitted > 0:
            bounded.append({"history_omitted_items": omitted})
        return bounded
    if not isinstance(value, dict):
        return value
    bounded_dict: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        key_str = str(key)
        if key_str in _TOOL_HISTORY_DROP_KEYS:
            bounded_dict[f"{key_str}_omitted_for_history"] = True
            continue
        if index >= max_dict_items:
            bounded_dict["history_omitted_keys"] = len(value) - max_dict_items
            break
        bounded_dict[key_str] = _bounded_history_value(
            item,
            max_string_chars=max_string_chars,
            max_list_items=max_list_items,
            max_dict_items=max_dict_items,
        )
    return bounded_dict


def _bounded_tool_message_body(body: Any) -> Any:
    """限制单条 tool message 的最大体积，避免工具结果撑爆上下文。"""
    if isinstance(body, str):
        return _summarize_history_text(body, _TOOL_HISTORY_MAX_JSON_CHARS)
    if _json_char_size(body) <= _TOOL_HISTORY_MAX_JSON_CHARS:
        return body
    bounded = _bounded_history_value(body)
    if _json_char_size(bounded) <= _TOOL_HISTORY_MAX_JSON_CHARS:
        return bounded
    return {
        "history_truncated": True,
        "summary": _summarize_history_text(
            json.dumps(bounded, ensure_ascii=False, default=str),
            _TOOL_HISTORY_MAX_JSON_CHARS,
        ),
    }


def _tool_message(tool_call_id: str, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    """构造一条 OpenAI `role=tool` 消息。

    Args:
        tool_call_id: 对应的工具调用 id。
        result: 工具结果；非字符串值会被 `json.dumps`。
        is_error: 是否作为错误结果回传（`{"error": ...}`），供模型据此改方案。

    Returns:
        可直接 `append` 进 `frame.messages` 的消息字典。
    """
    body: Any = {"error": result} if is_error else result
    body = _bounded_tool_message_body(body)
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _find_frame(session: Session, frame_id: str) -> Frame | None:
    """按 frame id 查找当前会话里的帧。"""
    for frame in session.agent_stack:
        if frame.id == frame_id:
            return frame
    return None


async def _delegate_child_frame(
    *,
    session: Session,
    parent_id: str,
    call_id: str | None,
    group_id: str | None,
    args: dict[str, Any],
    depth: int,
    prompt_factory: AgentPromptFactory | None,
) -> Frame | None:
    """根据委派参数创建一个子 agent 帧。"""
    agent_name = args.get("agent")
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    parent = _find_frame(session, parent_id)
    worker_spec = args.get("worker_spec")
    if isinstance(worker_spec, dict):
        if parent is None or parent.agent.name != "map-agent":
            return None
        worker_spec = dict(worker_spec)
        worker_spec.setdefault(
            "stage_id",
            f"{group_id or call_id or parent_id}:{worker_spec.get('mode', 'stage')}",
        )
        worker_spec.setdefault("lifecycle_scope", "delegate_frame")
        child_or_error = build_dynamic_map_worker(parent.agent, worker_spec)
        if isinstance(child_or_error, str):
            return None
        child_agent = child_or_error
        if is_map_worker_write_mode(worker_spec.get("mode")):
            # 写 worker 必须先切换阶段，否则阶段裁剪会只留下读取工具。
            session.map_task_state.stage = "write"
    else:
        if not isinstance(agent_name, str) or not agent_name:
            return None
        try:
            child_agent = get_agent(agent_name, set(REGISTRY))
        except KeyError:
            return None
    task_text = task.strip()
    prompt = (
        await prompt_factory(child_agent, task_text)
        if prompt_factory is not None
        else child_agent.prompt
    )
    child_agent = replace(child_agent, prompt=prompt)
    history_anchor_frame_id = parent_id
    history_anchor_message_index = len(parent.messages) if parent is not None else None
    if parent is not None and parent.history_anchor_frame_id is not None:
        history_anchor_frame_id = parent.history_anchor_frame_id
        history_anchor_message_index = parent.history_anchor_message_index
    return Frame(
        id=session.new_frame_id(),
        agent=child_agent,
        messages=[
            {"role": "system", "content": child_agent.prompt},
            {"role": "user", "content": task_text},
        ],
        parent_id=parent_id,
        pending_delegate_call_id=call_id,
        pending_delegate_group_id=group_id,
        depth=depth,
        history_anchor_frame_id=history_anchor_frame_id,
        history_anchor_message_index=history_anchor_message_index,
    )


def _plan_step_started(
    session: Session,
    child: Frame,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
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
    steps = plan.get("steps", [])
    idx = int(plan.get("next_step_index", 0))
    if idx >= len(steps):
        return
    plan.setdefault("frame_steps", {})[child.id] = idx
    plan["next_step_index"] = idx + 1
    step = steps[idx]
    _emit_orchestration_event(
        event_callback,
        "plan_step_started",
        {
            "frame_id": child.id,
            "message_index": len(child.messages),
            **_history_timeline_payload(child),
            "step_index": idx + 1,
            "total_steps": len(steps),
            "agent": step.get("agent", ""),
            "title": step.get("title", ""),
        },
    )


def _plan_step_completed(
    session: Session,
    done: Frame,
    text: str,
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
    frame_steps: dict[str, int] = plan.get("frame_steps", {})
    idx = frame_steps.pop(done.id, None)
    if idx is None:
        return
    full_summary = text.strip()
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
            "step_index": idx + 1,
            "total_steps": len(plan.get("steps", [])),
            "summary": summary,
            "full_summary": full_summary,
        },
    )


def _map_delegate_result_payload(done: Frame, text: str) -> dict[str, Any]:
    """把地图子 worker 结果压缩为结构化载荷，避免向父帧透传完整自然语言历史。"""
    output_schema = _map_output_schema_for_frame(done)
    payload = _json_object_from_text(text)
    if payload is not None and output_schema == _MAP_OUTPUT_SCHEMA_V1:
        slim_payload = _slim_map_delegate_value(payload)
        return {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": _summarize_history_text(str(payload.get("summary", "")), 4000),
            "result": slim_payload if isinstance(slim_payload, dict) else payload,
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
) -> None:
    """记录一个 `delegate_many` 子任务结果，并按需启动下一个子任务。"""
    assert done.pending_delegate_group_id is not None
    _plan_step_completed(session, done, text, event_callback)
    group = session.delegate_groups.get(done.pending_delegate_group_id)
    if group is None:
        logger.warning(
            "Delegate group missing session=%s group_id=%s frame=%s",
            session.session_id,
            done.pending_delegate_group_id,
            done.id,
        )
        return

    group.setdefault("results", []).append(_map_delegate_result_payload(done, text))
    remaining = group.setdefault("remaining", [])
    while isinstance(remaining, list) and remaining:
        next_task = remaining.pop(0)
        if not isinstance(next_task, dict):
            group["results"].append(
                {
                    "agent": "",
                    "summary": "子任务参数不合法，已跳过",
                    "error": True,
                }
            )
            continue
        child = await _delegate_child_frame(
            session=session,
            parent_id=str(group["parent_frame_id"]),
            call_id=None,
            group_id=done.pending_delegate_group_id,
            args=next_task,
            depth=int(group["depth"]),
            prompt_factory=prompt_factory,
        )
        if child is not None:
            session.agent_stack.append(child)
            _plan_step_started(session, child, event_callback)
            logger.info(
                "Delegate group continued session=%s group_id=%s child_frame=%s agent=%s remaining=%d",
                session.session_id,
                done.pending_delegate_group_id,
                child.id,
                child.agent.name,
                len(remaining),
            )
            return
        group["results"].append(
            {
                "agent": str(next_task.get("agent", "")),
                "summary": "子任务参数不合法或 agent 不存在，已跳过",
                "error": True,
            }
        )

    parent = _find_frame(session, str(group["parent_frame_id"]))
    if parent is not None:
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


_MAP_STRUCTURED_OUTPUT_AGENTS = frozenset(
    {
        "map-reader-agent",
        "map-planner-agent",
        "map-validator-agent",
        "map-reviewer-agent",
    }
)
_MAP_WORKER_RESULT_FIELDS = frozenset(
    {
        "stage",
        "worker",
        "mode",
        "objective",
        "target_path",
        "map_revision",
        "region",
        "summary",
        "facts",
        "proposed_batches",
        "write_results",
        "validation",
        "missing_inputs",
        "risks",
        "next_stage",
    }
)
_MAP_WORKER_STAGES = frozenset({"reader", "planner", "writer", "validator", "repairer", "reviewer"})
_MAP_OUTPUT_SCHEMA_V1 = "map_worker_result_v1"
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


def _map_output_schema_for_frame(frame: Frame) -> str | None:
    """解析当前地图 frame 需要执行的结构化输出 schema。"""
    if frame.agent.name in _MAP_STRUCTURED_OUTPUT_AGENTS:
        return _MAP_OUTPUT_SCHEMA_V1
    if frame.agent.source == "project" and _MAP_OUTPUT_SCHEMA_V1 in frame.agent.prompt:
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


def _slim_map_delegate_value(value: Any) -> Any:
    """递归瘦身地图子任务结果，避免父 agent 继承大数组。"""
    if isinstance(value, str):
        return (
            value
            if len(value) <= _MAP_DELEGATE_TEXT_LIMIT
            else value[:_MAP_DELEGATE_TEXT_LIMIT] + "..."
        )
    if isinstance(value, list):
        return [_slim_map_delegate_value(item) for item in value[:_MAP_DELEGATE_LIST_LIMIT]]
    if not isinstance(value, dict):
        return value
    slim: dict[str, Any] = {}
    for key, item in value.items():
        key_str = str(key)
        if key_str in _MAP_DELEGATE_DROP_KEYS:
            slim[f"{key_str}_omitted"] = True
            continue
        slim[key_str] = _slim_map_delegate_value(item)
    return slim


def _map_structured_output_error(frame: Frame, text: str) -> str | None:
    """校验地图阶段 agent 的 map_worker_result_v1 输出。"""
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
    if payload.get("stage") not in _MAP_WORKER_STAGES:
        return "stage 必须是 reader/planner/writer/validator/repairer/reviewer 之一。"
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        return "validation 必须是 object。"
    validation_missing = [
        key
        for key in ("passed", "completion_allowed", "issues", "structured_issues")
        if key not in validation
    ]
    if validation_missing:
        return "validation 缺少字段：" + ", ".join(validation_missing)
    for list_key in ("facts", "proposed_batches", "write_results", "missing_inputs", "risks"):
        if not isinstance(payload.get(list_key), list):
            return f"{list_key} 必须是 array。"
    return None


def _repair_map_structured_output(frame: Frame, text: str, error: str) -> str:
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
    issues.append(f"structured_output_repaired: {error}")
    raw_structured_issues = validation.get("structured_issues")
    structured_issues = (
        list(raw_structured_issues) if isinstance(raw_structured_issues, list) else []
    )
    structured_issues.append(
        {
            "code": "structured_output_repaired",
            "message": error,
            "agent": frame.agent.name,
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

    map_revision = source.get("map_revision")
    if isinstance(map_revision, bool) or not isinstance(map_revision, int):
        map_revision = None
    repaired = {
        "stage": stage,
        "worker": str(source.get("worker") or frame.agent.name),
        "mode": str(source.get("mode") or "partial"),
        "objective": str(source.get("objective") or _frame_objective(frame)),
        "target_path": str(source.get("target_path") or ""),
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
        "next_stage": "validator" if stage == "writer" else "planner",
    }
    return json.dumps(repaired, ensure_ascii=False)


def _map_stage_for_frame(frame: Frame) -> str:
    """根据地图 agent/frame 名称推断结构化收尾阶段。"""
    name = frame.agent.name
    if name == "map-planner-agent":
        return "planner"
    if name == "map-validator-agent":
        return "validator"
    if name == "map-reviewer-agent":
        return "reviewer"
    if "write_one_batch" in frame.agent.prompt:
        return "writer"
    if "repair" in frame.agent.prompt or "repair" in name:
        return "repairer"
    return "reader"


def _frame_objective(frame: Frame) -> str:
    """取子帧第一条用户消息作为 objective。"""
    for message in frame.messages:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return frame.agent.description or frame.agent.name


def _map_frame_exhausted_payload(frame: Frame, limit_label: str, limit: int) -> str:
    """为地图子帧预算耗尽生成合法的部分结果 JSON。"""
    issue = f"子 agent 达到自身{limit_label}上限（{limit}），已返回部分读取/执行结果。"
    payload = {
        "stage": _map_stage_for_frame(frame),
        "worker": frame.agent.name,
        "mode": "partial",
        "objective": _frame_objective(frame),
        "target_path": "",
        "map_layer": None,
        "map_revision": None,
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
        "next_stage": "replan",
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


def _apply_map_structured_completion_result(session: Session, frame: Frame, text: str) -> None:
    """把 validator/reviewer 的结构化 JSON 结果合并进地图完成门。"""
    payload = _json_object_from_text(text)
    if payload is None:
        return
    stage = str(payload.get("stage", ""))
    if stage not in {"validator", "reviewer"}:
        return
    target = str(payload.get("target_path", ""))
    revision = _payload_revision(payload)
    validation = payload.get("validation")
    validation_dict = validation if isinstance(validation, dict) else {}
    completion_allowed = validation_dict.get("completion_allowed") is True
    issues = validation_dict.get("issues")
    issue_list = [str(issue) for issue in issues] if isinstance(issues, list) else []

    if stage == "validator":
        canonical = session.latest_map_validations.get(target)
        canonical_matches = (
            isinstance(canonical, dict)
            and (not target or canonical.get("target") in ("", target))
            and (revision is None or canonical.get("map_revision") in (None, revision))
        )
        canonical_success = (
            canonical_matches
            and canonical is not None
            and all(
                canonical.get(key) is expected
                for key, expected in (
                    ("passed", True),
                    ("completion_allowed", True),
                    ("blocking_completion", False),
                )
            )
        )
        if completion_allowed and canonical_success:
            blockers = _clear_map_blockers(
                session.map_completion_blockers,
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
            session.map_completion_blockers = _append_map_blocker_once(
                blockers,
                {
                    "tool": frame.agent.name,
                    "reason": "map_review_required",
                    "issues": [
                        "same-revision validation passed; reviewer visual check is still required"
                    ],
                    "target": target,
                    "required_revision": revision,
                },
            )
        else:
            existing = next(
                (
                    blocker
                    for blocker in session.map_completion_blockers
                    if blocker.get("target") in ("", target)
                    and (revision is None or blocker.get("required_revision") in (None, revision))
                ),
                None,
            )
            if existing is not None:
                existing.setdefault("next_stage", "planner")
            else:
                session.map_completion_blockers = [
                    {
                        "tool": frame.agent.name,
                        "reason": "validator_failed",
                        "issues": issue_list
                        or ["validator failed or no canonical tool validation was recorded"],
                        "target": target,
                        "required_revision": revision,
                        "next_stage": "planner",
                    }
                ]
        return

    if completion_allowed:
        blockers = _clear_map_blockers(
            session.map_completion_blockers,
            target,
            revision,
            "map_review_required",
        )
        session.map_completion_blockers = _clear_map_blockers(
            blockers,
            target,
            revision,
            "reviewer_failed",
        )
    else:
        session.map_completion_blockers = [
            {
                "tool": frame.agent.name,
                "reason": "reviewer_failed",
                "issues": issue_list or ["reviewer reported completion_allowed=false"],
                "target": target,
                "required_revision": revision,
            }
        ]


async def _finish_frame(
    session: Session,
    text: str,
    prompt_factory: AgentPromptFactory | None = None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
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
        structured_error = _map_structured_output_error(frame, text)
        if structured_error is not None:
            logger.warning(
                "Map structured output rejected session=%s frame=%s agent=%s error=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
                structured_error,
            )
            text = _repair_map_structured_output(frame, text, structured_error)
            logger.warning(
                "Map structured output repaired session=%s frame=%s agent=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
            )

        _apply_map_structured_completion_result(session, frame, text)

    if len(session.agent_stack) <= 1:
        logger.info("Root frame finished session=%s text_length=%d", session.session_id, len(text))
        if session.pending_plan is not None:
            session.pending_plan = None
        return FinalResult(text=text)
    done = session.agent_stack.pop()
    logger.info(
        "Child frame finished session=%s frame=%s agent=%s text_length=%d",
        session.session_id,
        done.id,
        done.agent.name,
        len(text),
    )
    if done.pending_delegate_group_id is not None:
        await _continue_delegate_group(session, done, text, prompt_factory, event_callback)
        return None
    parent = session.top_frame()
    assert parent is not None
    if done.pending_delegate_call_id is not None:
        _plan_step_completed(session, done, text, event_callback)
        parent.messages.append(
            _tool_message(done.pending_delegate_call_id, _map_delegate_result_payload(done, text))
        )
    elif done.parent_id is not None:
        parent.messages.append(
            {
                "role": "user",
                "content": (
                    "自动子阶段结果："
                    + json.dumps(_map_delegate_result_payload(done, text), ensure_ascii=False)
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
        return ErrorResult(text="已达到本轮最大循环次数，请精简任务或拆分请求后重试")
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


def _append_single_tool_call_protocol_errors(frame: Frame, calls: list[Any]) -> None:
    """Reject multi-tool assistant turns so the UI can render atomic workflow steps."""
    logger.warning(
        "Single-tool protocol violation frame=%s agent=%s tool_calls=%d",
        frame.id,
        frame.agent.name,
        len(calls),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                "每轮 assistant 只能调用一个工具；本轮所有工具均未执行，请只选择一个工具后重试",
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


def _map_agent_targets_from_delegate_call(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return map-agent task texts from delegate/delegate_many args."""
    if tool_name == "delegate":
        if args.get("agent") == "map-agent" and isinstance(args.get("task"), str):
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
        if item.get("agent") == "map-agent" and isinstance(item.get("task"), str):
            tasks.append(str(item["task"]))
    return tasks


def _requires_create_plan_before_map_delegate(
    session: Session,
    frame: Frame,
    tool_name: str,
    args: dict[str, Any],
) -> bool:
    """Require coordinator to create a visible plan before complex map delegation."""
    if frame.agent.name != "coordinator" or session.pending_plan is not None:
        return False
    return any(
        _is_complex_map_delegation_task(task)
        for task in _map_agent_targets_from_delegate_call(tool_name, args)
    )


def _append_map_write_protocol_errors(frame: Frame, calls: list[Any]) -> bool:
    """校验地图写工具单轮协议，失败时补工具错误并要求模型重试。"""
    write_calls = [call for call in calls if is_map_write_tool(call.name)]
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


_MAP_VALIDATION_TOOL_NAMES = frozenset(
    {"validate_map_region", "validate_layer_coverage", "validate_object_placements"}
)
_MAP_FOLLOWUP_AGENT_NAMES = frozenset({"map-validator-agent", "map-reviewer-agent"})


def _has_pending_map_write_validation(session: Session) -> bool:
    """判断当前会话是否有写后必须验证的地图阻断。"""
    for blocker in session.map_completion_blockers:
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
    for blocker in session.map_completion_blockers:
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
    return frame.agent.name.startswith("map-") or bool(frame.agent.workflow_operations)


_MAP_STAGE_TOOLS: dict[str, frozenset[str]] = {
    "read": frozenset(
        {
            "delegate",
            "delegate_many",
            "describe_map_context",
            "describe_map_region",
            "describe_tilemap_selection",
            "read_scene_tree",
            "read_file",
            "read_class_docs",
            "read_image_metadata",
            "capture_viewport_screenshot",
            "query_spatial_index",
            "convert_map_coords",
            "load_skill",
            "search_tools",
        }
    ),
    "plan": frozenset(
        {
            "delegate",
            "delegate_many",
            "plan_map_layout",
            "plan_map_algorithms",
            "plan_platform_level",
            "plan_reachable_map_growth",
            "compute_reachable_frontier",
            "sample_poisson_points",
            "sample_noise_grid",
            "compose_map_blueprint_grammar",
            "describe_map_region",
            "query_spatial_index",
            "find_placement_anchors",
            "read_file",
            "read_class_docs",
            "capture_viewport_screenshot",
            "load_skill",
            "search_tools",
        }
    ),
    "write": MAP_WRITE_TOOL_NAMES
    | frozenset(
        {
            "describe_map_region",
            "query_spatial_index",
            "find_placement_anchors",
            "read_file",
            "load_skill",
            "search_tools",
        }
    ),
    "validate": frozenset(
        {
            "delegate",
            "delegate_many",
            "validate_map_region",
            "validate_layer_coverage",
            "validate_object_placements",
            "describe_map_region",
            "query_spatial_index",
            "read_file",
            "load_skill",
            "search_tools",
        }
    ),
    "diagnostic": frozenset(
        {
            "validate_map_region",
            "describe_map_region",
            "query_spatial_index",
            "read_file",
            "load_skill",
        }
    ),
    "review": frozenset(
        {
            "delegate",
            "delegate_many",
            "capture_viewport_screenshot",
            "describe_map_region",
            "validate_map_region",
            "validate_layer_coverage",
            "validate_object_placements",
            "read_scene_tree",
            "read_image_metadata",
            "save_scene",
            "load_skill",
        }
    ),
}


def _stage_effective_tools(session: Session, frame: Frame) -> list[str]:
    """按地图任务阶段裁剪工具，非地图帧保持原白名单。"""
    if not _uses_persistent_map_budget(frame):
        return list(frame.agent.effective_tools)
    stage = session.map_task_state.stage
    allowed = _MAP_STAGE_TOOLS.get(stage)
    if allowed is None:
        return list(frame.agent.effective_tools)
    return [name for name in frame.agent.effective_tools if name in allowed]


def _latest_map_progress_revision(session: Session) -> int | None:
    """返回会话当前已知的最高地图 revision。"""
    return max(session.latest_map_revisions.values(), default=None)


def _sync_map_progress_budget(session: Session, frame: Frame) -> None:
    """地图 revision 前进时开启新的生产性迭代预算。"""
    revision = _latest_map_progress_revision(session)
    if revision == frame.map_progress_revision:
        return
    frame.persistent_turn_count = 0
    frame.persistent_edit_map_turn_count = 0
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
    current_revision = session.latest_map_revisions.get(target)
    targets = session.map_context_state.get("targets", {})
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
            session.map_task_state.counters.read_cache_hits += 1
            return {**entry, "cache_hit": True, "cache_reason": "same_revision_region_covered"}
    return None


def _resumed_full_map_read_error(session: Session, args: dict[str, Any]) -> str | None:
    """恢复任务时拒绝重新读取已知整图范围。"""
    if not session.map_task_state.resumed_from_checkpoint:
        return None
    target = args.get("target_path")
    layer = args.get("map_layer")
    if not isinstance(target, str) or not isinstance(layer, int):
        return None
    targets = session.map_context_state.get("targets", {})
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
            if worker_spec.get("mode") != "review_only":
                return False
            allowed_tools = worker_spec.get("allowed_tools")
            if isinstance(allowed_tools, list) and any(
                tool_name in _MAP_VALIDATION_TOOL_NAMES for tool_name in allowed_tools
            ):
                continue
            return False
        if item.get("agent") not in _MAP_FOLLOWUP_AGENT_NAMES:
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
        if call.name in _MAP_VALIDATION_TOOL_NAMES:
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
        allowed_tools = sorted(set(frame.agent.effective_tools) & set(_MAP_VALIDATION_TOOL_NAMES))
        blocker = session.map_completion_blockers[0] if session.map_completion_blockers else {}
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
    """给地图写工具入参补充服务端掌握的批次来源字段。"""
    if not is_map_write_tool(tool_name):
        return args
    enriched = dict(args)
    target_path = str(enriched.get("target_path", ""))
    latest_revision = session.latest_map_revisions.get(target_path)
    supplied_revision = enriched.get("expected_revision")
    supplied_revision_is_int = isinstance(supplied_revision, int) and not isinstance(
        supplied_revision, bool
    )
    if latest_revision is not None and (
        not supplied_revision_is_int or latest_revision > supplied_revision
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
    latest_layer = session.latest_map_layers.get(target_path)
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
    for raw in raw_steps:
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
        depends_on = raw.get("depends_on")
        if depends_on is not None and not (
            isinstance(depends_on, list) and all(isinstance(value, int) for value in depends_on)
        ):
            return "create_plan.steps[].depends_on 必须是整数数组"
        complexity = raw.get("estimated_complexity")
        if complexity is not None and complexity not in _PLAN_COMPLEXITY_LEVELS:
            return "create_plan.steps[].estimated_complexity 取值必须是 low/medium/high"
        normalized.append(
            {
                "title": title.strip(),
                "agent": agent_name.strip(),
                "task": task.strip(),
                "depends_on": depends_on or [],
                "estimated_complexity": complexity,
            }
        )
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

    session.pending_plan = {
        "summary": summary.strip(),
        "steps": steps,
        "next_step_index": 0,
        "frame_steps": {},
    }
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
                    "index": index + 1,
                    "title": step["title"],
                    "agent": step["agent"],
                    "task": step["task"],
                    "depends_on": step["depends_on"],
                    "estimated_complexity": step["estimated_complexity"],
                }
                for index, step in enumerate(steps)
            ],
        },
    )
    tasks = [{"agent": step["agent"], "task": step["task"]} for step in steps]
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
    if has_worker_spec and frame.agent.name != "map-agent":
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
    if child is None:
        logger.warning(
            "Delegate rejected: unknown child agent session=%s agent=%s",
            session.session_id,
            agent_name,
        )
        if has_worker_spec:
            frame.messages.append(_tool_message(call_id, "动态 worker spec 不合法", is_error=True))
        else:
            frame.messages.append(
                _tool_message(call_id, f"未知子 agent：{agent_name}", is_error=True)
            )
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

    tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not tasks:
        logger.warning(
            "Delegate_many rejected: invalid tasks session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(call_id, "delegate_many.tasks 格式不合法", is_error=True)
        )
        return False
    first = tasks.pop(0)
    if any(isinstance(task.get("worker_spec"), dict) for task in [first, *tasks]):
        if frame.agent.name != "map-agent":
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
            for task in [first, *tasks]
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
        "remaining": tasks,
        "results": [],
        "depth": frame.depth + 1,
    }
    child = await _delegate_child_frame(
        session=session,
        parent_id=frame.id,
        call_id=None,
        group_id=group_id,
        args=first,
        depth=frame.depth + 1,
        prompt_factory=prompt_factory,
    )
    if child is None:
        session.delegate_groups.pop(group_id, None)
        logger.warning(
            "Delegate_many rejected: invalid first task session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(call_id, "delegate_many 首个子任务不合法", is_error=True)
        )
        return False
    session.agent_stack.append(child)
    _plan_step_started(session, child, event_callback)
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

    # 上游 provider 可能把同一条 assistant 消息的 content 与
    # reasoning_content 交错发送。message_index 已经是一次 LLM 调用的稳定
    # 身份，不能再以通道切换作为正文分段边界，否则会截断正文并导致 final
    # 无法收敛替换流式消息。
    def _on_delta(kind: str, text: str, token_count: int | None) -> None:
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

    Returns:
        `ToolCallsResult`（需前端执行/确认）、`FinalResult`（已得到最终回复）
        或 `ErrorResult`（LLM 调用失败/达到轮数上限）。
    """
    logger.info("Agent run_turn start session=%s max_turns=%d", session.session_id, max_turns)
    frame_turns: dict[str, int] = {}  # 非地图帧仍只统计本次 run_turn 的轮数
    # frame_id -> 其中单独计入 edit_map_max_turns 预算的轮数（tool_calls 仅含 edit_map 时）
    frame_edit_map_turns: dict[str, int] = {}
    for loop_index in range(max_turns):
        frame = session.top_frame()
        if frame is None:
            logger.error("Agent run_turn failed: empty frame stack session=%s", session.session_id)
            return ErrorResult(text="会话没有活跃的 agent 帧")

        persistent_map_budget = _uses_persistent_map_budget(frame)
        if persistent_map_budget:
            if session.map_task_state.status == "paused":
                checkpoint = json.dumps(
                    session.map_task_state.checkpoint or {}, ensure_ascii=False, default=str
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_task_paused",
                    {
                        "frame_id": frame.id,
                        "reason": session.map_task_state.pause_reason,
                        "checkpoint": session.map_task_state.checkpoint or {},
                        "counters": session.map_task_state.counters.__dict__,
                    },
                )
                return ErrorResult(text=f"地图任务因连续无进展已暂停。恢复检查点：{checkpoint}")
            _sync_map_progress_budget(session, frame)
            session.map_task_state.counters.llm_turns += 1
        used = (
            frame.persistent_turn_count if persistent_map_budget else frame_turns.get(frame.id, 0)
        )
        # 这里只做一个宽松的总量护栏（max_turns + edit_map_max_turns），防止帧无限循环；
        # 哪个预算先耗尽由下面 tool_calls 揭晓后的精确分类检查负责。
        total_budget = frame.agent.max_turns + (frame.agent.edit_map_max_turns or 0)
        if used >= total_budget:
            result = await _handle_frame_turns_exhausted(
                session, frame, "总轮数", total_budget, agent_prompt_factory, event_callback
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
                temperature=_resolve_temperature(effort),
                thinking_budget=resolve_thinking_budget(effort, thinking_budget_selector),
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
            )
        except LLMError as exc:
            logger.warning(
                "Agent LLM step failed session=%s frame=%s error=%s",
                session.session_id,
                frame.id,
                exc,
            )
            return ErrorResult(text=str(exc))

        frame.messages.append(turn.raw_message)
        _record_cache_metrics(cache_metrics, cache_decision, turn)
        _emit_context_usage_event(event_callback, frame, loop_index + 1, turn, context_token_limit)
        _emit_cache_hit_event(event_callback, frame, loop_index + 1, turn)

        if not turn.tool_calls:
            finish_result = await _finish_frame(
                session, turn.content or "", agent_prompt_factory, event_callback
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
            if tool.name in _MAP_VALIDATION_TOOL_NAMES:
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

        if front_calls:
            if len(front_calls) > 1 and all(
                call.name in MAP_WRITE_TOOL_NAMES for call in front_calls
            ):
                state = session.map_task_state
                state.plan_version = max(1, state.plan_version)
                state.pending_batches.clear()
                for batch_index, call in enumerate(front_calls):
                    call.input.setdefault("plan_version", state.plan_version)
                    call.input.setdefault("batch_index", batch_index)
                state.pending_batches.extend(_queued_front_call(call) for call in front_calls[1:])
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
    return ErrorResult(text="已达到本轮最大循环次数，请精简任务或拆分请求后重试")
