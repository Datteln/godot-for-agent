"""出站 LLM 消息投影与脱敏上下文审计（任务 4.1 / 5.2 / 5.3）。

投影把"持久化帧消息 + 帧记忆状态"组合成一次实际发给模型的消息列表：

1. 分层 system 层保持原样（缓存断点行为不变）；
2. 命名 `[conversation_memory]` 记忆块作为额外 system 消息紧随其后——
   永远不插入 assistant tool_calls 与配对结果之间；
3. 历史消息原序保留（保留边界在轮次结束/压缩时已按完整轮处理）；
4. 投影前后都跑协议校验；末尾挂起组（当前请求/待回传工具）受保护。

审计只输出计数与身份类标识（消息数、估算 token、保留轮数、协议组数、
记忆记录数），绝不包含提示词文本、编辑器 JSON、Thought 内容或完整工具
结果载荷。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.context.grouping import group_messages, terminalize_pending_groups, validate_projection
from app.context.memory import render_memory_block
from app.context.models import ContextMemoryState
from app.llm.message_transformer import estimate_message_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextProjectionSettings:
    """投影所需的运行参数（来自 `AppSettings`）。

    Attributes:
        budget_tokens: 整体模型上下文预算（估算 token）。
        retained_turns: 保留的完整用户轮数。
        active_group_window: 活跃轮协议窗口大小（默认 12）。
    """

    budget_tokens: int = 200_000
    retained_turns: int = 8
    active_group_window: int = 12


@dataclass
class ContextProjection:
    """一次投影的产物。

    Attributes:
        messages: 实际发给模型的消息列表（不改动帧的持久化消息）。
        audit: 脱敏审计字典（仅计数与身份标识）。
        violations: 投影校验发现的协议违规（正常应为空）。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


def build_context_audit(
    messages: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    memory_injected: bool,
) -> dict[str, Any]:
    """构造脱敏上下文审计（任务 5.2/5.3）。

    只含计数与身份类标识，结构性地不包含任何提示词文本、编辑器 JSON、
    Thought 内容或工具结果载荷。

    Args:
        messages: 投影后的消息列表。
        state: 帧记忆状态。
        memory_injected: 本次是否注入了记忆块。

    Returns:
        审计字典。
    """
    groups = group_messages(messages)
    tool_groups = [group for group in groups if group.kind == "tool_group"]
    pending_groups = [group for group in tool_groups if not group.complete]
    user_messages = sum(1 for message in messages if message.get("role") == "user")
    return {
        "message_count": len(messages),
        "estimated_tokens": estimate_message_tokens(messages),
        "retained_turns": user_messages,
        "protocol_groups": len(tool_groups),
        "pending_groups": len(pending_groups),
        "memory_injected": memory_injected,
        "durable_tool_records": len(state.tool_records),
        "current_turn_records": len(state.current_turn_records),
        "editor_facts": len(state.editor_facts),
        "memory_revision": state.revision,
    }


def project_frame_messages(
    messages: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    settings: ContextProjectionSettings | None = None,
    pending_call_ids: set[str] | None = None,
    session_id: str = "",
    frame_id: str = "",
) -> ContextProjection:
    """构造一次出站 LLM 请求的消息投影（任务 4.1）。

    记忆块作为独立 system 消息插在前导 system 层之后；若帧状态中存在未
    闭合且已无活跃等待方的尾部协议组，先补齐终结结果再投影。

    Args:
        messages: 帧持久化消息列表（只读引用；投影生成副本）。
        state: 帧记忆状态。
        settings: 投影参数。
        pending_call_ids: 当前仍在等待回传的 tool_call id（受保护）。
        session_id: 仅用于日志。
        frame_id: 仅用于日志。

    Returns:
        `ContextProjection`。
    """
    projected = [dict(message) for message in messages]

    # 尾部无主挂起组（会话已无 pending 等待）→ 先终结化，保证协议闭合；
    # 仍在等待回传的调用保持挂起、受保护（任务 2.2 / 3 场景）。
    live_pending = pending_call_ids if pending_call_ids is not None else set()
    stale_ids: set[str] = set()
    for group in group_messages(projected):
        if group.kind == "tool_group" and not group.complete:
            stale_ids.update(
                call_id for call_id in group.missing_ids if call_id not in live_pending
            )
    if stale_ids:
        terminalize_pending_groups(projected, "reset", pending_call_ids=stale_ids)

    system_end = 0
    for index, message in enumerate(projected):
        if message.get("role") == "system":
            system_end = index + 1
        else:
            break

    memory_block = render_memory_block(state)
    memory_injected = bool(memory_block.strip())
    if memory_injected:
        projected.insert(system_end, {"role": "system", "content": memory_block})

    final_violations = validate_projection(projected)
    audit = build_context_audit(projected, state, memory_injected=memory_injected)
    logger.info(
        "Context audit session=%s frame=%s messages=%d tokens=%d turns=%d groups=%d "
        "pending=%d durable_records=%d current_records=%d memory=%s violations=%d",
        session_id,
        frame_id,
        audit["message_count"],
        audit["estimated_tokens"],
        audit["retained_turns"],
        audit["protocol_groups"],
        audit["pending_groups"],
        audit["durable_tool_records"],
        audit["current_turn_records"],
        memory_injected,
        len(final_violations),
    )
    return ContextProjection(messages=projected, audit=audit, violations=final_violations)
