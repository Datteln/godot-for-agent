"""定义 Map turn 处理器共享的封闭数据合同与常量。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agents.types import AgentDefinition
from app.history_bounds import (
    bounded_tool_message_body as _bounded_tool_message_body,
)
from app.tools.registry import ToolDef

MAX_AGENT_DEPTH = 4


EVENT_TEXT_PREVIEW_CHARS = 24_000


EVENT_MATCH_PREVIEW_ITEMS = 20


NOOP_SEARCH_TOOLS_HINT_THRESHOLD = 2


_INTEGER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")


_NUMBER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


logger = logging.getLogger(__name__)


AgentPromptFactory = Callable[[AgentDefinition, str], Awaitable[str]]


@dataclass
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


@dataclass
class _PendingToolMessage:
    """第一遍扫描中已确定结果的工具消息（未知工具/参数错误/权限拒绝）。"""

    message: dict[str, Any]


@dataclass
class _PendingServerCall:
    """第一遍扫描中通过校验、待第二遍执行的 server 工具调用。"""

    call_id: str
    tool: ToolDef
    args: dict[str, Any]


_PendingItem = _PendingToolMessage | _PendingServerCall


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
