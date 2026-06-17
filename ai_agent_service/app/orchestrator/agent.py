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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace
from typing import Any, Literal, cast

from app.agents.bundled import get_agent
from app.agents.types import EFFORT_LEVELS, AgentDefinition, EffortLevel, Frame
from app.llm.provider import LLMError, LLMProvider
from app.permissions.engine import PermissionContext, check
from app.permissions.engine import SessionAllowGrant
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, tools_for

MAX_AGENT_DEPTH = 4
EVENT_TEXT_PREVIEW_CHARS = 24_000
EVENT_MATCH_PREVIEW_ITEMS = 20

logger = logging.getLogger(__name__)


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


# effort 档位 -> 采样温度（§6.5）；`verify` 取 0 以追求确定性复核结果。
EFFORT_TEMPERATURE: dict[EffortLevel, float] = {
    "quick": 0.2,
    "standard": 0.7,
    "deep": 0.7,
    "verify": 0.0,
    "advisor": 0.3,
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


async def _invoke_server_tool(tool: ToolDef, args: dict[str, Any], call_ctx: ToolContext) -> tuple[Any, bool]:
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
        [name for name in tool.path_args if name in args],
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
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _find_frame(session: Session, frame_id: str) -> Frame | None:
    """按 frame id 查找当前会话里的帧。"""
    for frame in session.agent_stack:
        if frame.id == frame_id:
            return frame
    return None


def _delegate_child_frame(
    *,
    session: Session,
    parent_id: str,
    call_id: str | None,
    group_id: str | None,
    args: dict[str, Any],
    depth: int,
    prompt_factory: Callable[[AgentDefinition], str] | None,
) -> Frame | None:
    """根据委派参数创建一个子 agent 帧。"""
    agent_name = args.get("agent")
    task = args.get("task")
    if not isinstance(agent_name, str) or not agent_name:
        return None
    if not isinstance(task, str) or not task.strip():
        return None
    try:
        child_agent = get_agent(agent_name, set(REGISTRY))
    except KeyError:
        return None
    prompt = prompt_factory(child_agent) if prompt_factory is not None else child_agent.prompt
    child_agent = replace(child_agent, prompt=prompt)
    return Frame(
        id=session.new_frame_id(),
        agent=child_agent,
        messages=[
            {"role": "system", "content": child_agent.prompt},
            {"role": "user", "content": task.strip()},
        ],
        parent_id=parent_id,
        pending_delegate_call_id=call_id,
        pending_delegate_group_id=group_id,
        depth=depth,
    )


def _continue_delegate_group(
    session: Session,
    done: Frame,
    text: str,
    prompt_factory: Callable[[AgentDefinition], str] | None,
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

    group.setdefault("results", []).append(
        {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": text,
        }
    )
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
        child = _delegate_child_frame(
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


def _finish_frame(
    session: Session,
    text: str,
    prompt_factory: Callable[[AgentDefinition], str] | None = None,
) -> FinalResult | None:
    """处理当前帧产出最终文本（无 `tool_calls`）的情况（§13.1）。

    根帧（`agent_stack` 长度为 1）保留在栈中以维持多轮会话历史，直接
    返回 `FinalResult`；由 `delegate` 创建的子帧（M2+）结束时则弹栈，
    把摘要回填父帧那条 `delegate` 的工具结果，交由调用方继续驱动父帧。

    Args:
        session: 当前会话。
        text: 当前帧本轮产出的最终文本。

    Returns:
        根帧结束时返回 `FinalResult`；子帧结束时返回 None，调用方应
        继续循环（此时 `session.top_frame()` 已是父帧）。
    """
    if len(session.agent_stack) <= 1:
        logger.info("Root frame finished session=%s text_length=%d", session.session_id, len(text))
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
        _continue_delegate_group(session, done, text, prompt_factory)
        return None
    parent = session.top_frame()
    assert parent is not None
    if done.pending_delegate_call_id is not None:
        parent.messages.append(_tool_message(done.pending_delegate_call_id, {"summary": text}))
    return None


def _load_tool_args(call_id: str, arguments: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解析工具入参 JSON，返回 `(args, error_message)` 二元组。"""
    try:
        loaded = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("Tool arguments JSON parse failed call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参不是合法 JSON", is_error=True)
    if not isinstance(loaded, dict):
        logger.warning("Tool arguments are not an object call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参必须是 JSON object", is_error=True)
    return loaded, None


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


def _start_delegate_frame(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: Callable[[AgentDefinition], str] | None,
) -> bool:
    """创建子 agent 帧并压栈，成功时返回 True。"""
    agent_name = args.get("agent")
    task = args.get("task")
    if not isinstance(agent_name, str) or not agent_name:
        logger.warning("Delegate rejected: missing agent session=%s frame=%s", session.session_id, frame.id)
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
        frame.messages.append(_tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True))
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(_tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True))
        return False

    child = _delegate_child_frame(
        session=session,
        parent_id=frame.id,
        call_id=call_id,
        group_id=None,
        args=args,
        depth=frame.depth + 1,
        prompt_factory=prompt_factory,
    )
    if child is None:
        logger.warning("Delegate rejected: unknown child agent session=%s agent=%s", session.session_id, agent_name)
        frame.messages.append(_tool_message(call_id, f"未知子 agent：{agent_name}", is_error=True))
        return False
    session.agent_stack.append(child)
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


def _start_delegate_group(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: Callable[[AgentDefinition], str] | None,
) -> bool:
    """启动 `delegate_many` 顺序子任务组。"""
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        logger.warning("Delegate_many rejected: missing tasks session=%s frame=%s", session.session_id, frame.id)
        frame.messages.append(_tool_message(call_id, "delegate_many.tasks 不能为空", is_error=True))
        return False
    if not frame.agent.can_delegate:
        logger.warning(
            "Delegate_many rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(_tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True))
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate_many rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(_tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True))
        return False

    tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not tasks:
        logger.warning("Delegate_many rejected: invalid tasks session=%s frame=%s", session.session_id, frame.id)
        frame.messages.append(_tool_message(call_id, "delegate_many.tasks 格式不合法", is_error=True))
        return False
    first = tasks.pop(0)
    group_id = call_id
    session.delegate_groups[group_id] = {
        "parent_frame_id": frame.id,
        "tool_call_id": call_id,
        "remaining": tasks,
        "results": [],
        "depth": frame.depth + 1,
    }
    child = _delegate_child_frame(
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
        logger.warning("Delegate_many rejected: invalid first task session=%s frame=%s", session.session_id, frame.id)
        frame.messages.append(_tool_message(call_id, "delegate_many 首个子任务不合法", is_error=True))
        return False
    session.agent_stack.append(child)
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
        return {
            "kind": "read",
            "path": str(result.get("path", "")),
            "line_start": 1,
            "line_end": max(1, len(content.splitlines())),
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
            "truncated": bool(result.get("truncated", False)) or len(matches) > EVENT_MATCH_PREVIEW_ITEMS,
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


def _delta_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
) -> Callable[[str, str], None] | None:
    """构造传给 `LLMProvider.chat` 的流式增量回调，转发为编排事件。

    Args:
        event_callback: 编排事件回调；为 None 时不产生增量事件。
        frame_id: 本轮所属的 agent 帧 id，供前端关联增量与对应消息。
        loop: 本轮在 `run_turn` 中的循环序号（从 1 开始）。

    Returns:
        转发增量为 `agent_text_delta`/`agent_reasoning_delta` 事件的回调；
        `event_callback` 为 None 时返回 None。
    """
    if event_callback is None:
        return None

    def _on_delta(kind: str, text: str) -> None:
        event_type = "agent_reasoning_delta" if kind == "reasoning" else "agent_text_delta"
        event_callback(event_type, {"frame_id": frame_id, "loop": loop, "text": text})

    return _on_delta


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
    agent_prompt_factory: Callable[[AgentDefinition], str] | None = None,
    model_selector: Callable[[EffortLevel], str | None] | None = None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> StepResult:
    """驱动当前会话的活跃帧完成一轮（或多轮）编排循环。

    Args:
        session: 当前会话，`agent_stack` 至少含一个根帧。
        llm: 大模型 provider。
        security: 当前会话的安全边界配置，供权限闸使用。
        tool_ctx: server 工具执行上下文。
        max_turns: 本次调用允许驱动的最大 LLM 往返轮数，超出则返回
            `ErrorResult`，避免死循环消耗配额。

    Returns:
        `ToolCallsResult`（需前端执行/确认）、`FinalResult`（已得到最终回复）
        或 `ErrorResult`（LLM 调用失败/达到轮数上限）。
    """
    logger.info("Agent run_turn start session=%s max_turns=%d", session.session_id, max_turns)
    frame_turns: dict[str, int] = {}  # frame_id -> 本次 run_turn 调用内该帧已消耗轮数
    for loop_index in range(max_turns):
        frame = session.top_frame()
        if frame is None:
            logger.error("Agent run_turn failed: empty frame stack session=%s", session.session_id)
            return ErrorResult(text="会话没有活跃的 agent 帧")

        used = frame_turns.get(frame.id, 0)
        if used >= frame.agent.max_turns:
            if len(session.agent_stack) <= 1:
                logger.warning(
                    "Agent run_turn reached root frame max turns session=%s agent=%s max_turns=%d",
                    session.session_id,
                    frame.agent.name,
                    frame.agent.max_turns,
                )
                return ErrorResult(text="已达到本轮最大循环次数，请精简任务或拆分请求后重试")
            logger.warning(
                "Delegate frame reached its max turns session=%s frame=%s agent=%s max_turns=%d",
                session.session_id,
                frame.id,
                frame.agent.name,
                frame.agent.max_turns,
            )
            _finish_frame(
                session,
                f"子 agent「{frame.agent.name}」已达到自身最大循环次数（{frame.agent.max_turns}），"
                "任务未完成，已强制收尾。以上为已执行步骤记录，请据此判断是否需要重新拆分任务或继续委派。",
                agent_prompt_factory,
            )
            continue

        frame_turns[frame.id] = used + 1

        try:
            visible_tools = tools_for(frame.agent.effective_tools, frame.active_deferred_tools)
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
            turn = await llm.chat(
                frame.messages,
                visible_tools,
                model=_resolve_model_for_effort(frame.agent, effort, model_selector),
                temperature=_resolve_temperature(effort),
                on_delta=_delta_callback(event_callback, frame.id, loop_index + 1),
                on_fallback=_fallback_callback(event_callback, frame.id, loop_index + 1),
            )
        except LLMError as exc:
            logger.warning("Agent LLM step failed session=%s frame=%s error=%s", session.session_id, frame.id, exc)
            return ErrorResult(text=str(exc))

        frame.messages.append(turn.raw_message)

        if not turn.tool_calls:
            result = _finish_frame(session, turn.content or "", agent_prompt_factory)
            if result is not None:
                logger.info("Agent run_turn final session=%s loop=%d", session.session_id, loop_index + 1)
                return result
            continue  # 子帧已结束，继续驱动父帧

        if len(turn.tool_calls) > 1:
            _append_single_tool_call_protocol_errors(frame, turn.tool_calls)
            continue

        logger.info(
            "Agent requested tools session=%s frame=%s agent=%s names=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
            [call.name for call in turn.tool_calls],
        )
        _emit_orchestration_event(
            event_callback,
            "agent_tool_calls",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "tools": [call.name for call in turn.tool_calls],
            },
        )

        permission_ctx = PermissionContext(
            security=security,
            effective_tools=frozenset(frame.agent.effective_tools),
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
                logger.warning("Delegate tool missing from registry session=%s tool=%s", session.session_id, call.name)
                frame.messages.append(_tool_message(call.id, f"{call.name} 工具未注册", is_error=True))
                continue

            args, parse_error = _load_tool_args(call.id, call.arguments)
            if parse_error is not None:
                frame.messages.append(parse_error)
                continue
            assert args is not None

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
                    _tool_message(call.id, "被拒绝：当前 agent/权限模式不允许 delegate", is_error=True)
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
                },
            )
            if call.name == "delegate_many":
                _start_delegate_group(
                    session=session,
                    frame=frame,
                    call_id=call.id,
                    args=args,
                    prompt_factory=agent_prompt_factory,
                )
            else:
                _start_delegate_frame(
                    session=session,
                    frame=frame,
                    call_id=call.id,
                    args=args,
                    prompt_factory=agent_prompt_factory,
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
                    _PendingToolMessage(_tool_message(call.id, f"未知工具：{call.name}", is_error=True))
                )
                continue

            args, parse_error = _load_tool_args(call.id, call.arguments)
            if parse_error is not None:
                pending_items.append(_PendingToolMessage(parse_error))
                continue
            assert args is not None

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
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(call.id, f"被拒绝：当前权限模式/安全边界不允许调用 {tool.name}", is_error=True)
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
        call_ctx = replace(tool_ctx, effective_tools=frozenset(frame.agent.effective_tools))
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
                        "result_summary": _event_result_summary(item.tool.name, outcome[0], outcome[1]),
                    },
                )
        for item in sequential_calls:
            logger.info("Running sequential server tool session=%s tool=%s", session.session_id, item.tool.name)
            _emit_orchestration_event(
                event_callback,
                "server_tool_start",
                {
                    "frame_id": frame.id,
                    "agent": frame.agent.name,
                    "tool": item.tool.name,
                    "args": _event_tool_args(item.args),
                    "concurrent": False,
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
                },
            )

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
                logger.info(
                    "Deferred tools activated session=%s frame=%s tools=%s",
                    session.session_id,
                    frame.id,
                    sorted(activated),
                )
            frame.messages.append(_tool_message(item.call_id, result, is_error=is_error))

        if front_calls:
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

    logger.warning("Agent run_turn reached max turns session=%s max_turns=%d", session.session_id, max_turns)
    return ErrorResult(text="已达到本轮最大循环次数，请精简任务或拆分请求后重试")
