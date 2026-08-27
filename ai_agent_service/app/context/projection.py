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

from app.context.evidence import reduce_tool_record_detail
from app.context.grouping import group_messages, terminalize_pending_groups, validate_projection
from app.context.memory import enforce_memory_budget, render_memory_block
from app.context.models import ContextMemoryState
from app.llm.message_transformer import estimate_message_tokens, estimate_request_tokens

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
        # 保留原字段作为同一会话内的压缩趋势指标，避免把请求包装开销变化
        # 误判为记忆正文膨胀；硬预算必须读取下面的 conservative_tokens。
        "estimated_tokens": estimate_message_tokens(messages),
        "conservative_tokens": estimate_request_tokens(messages),
        "retained_turns": user_messages,
        "protocol_groups": len(tool_groups),
        "pending_groups": len(pending_groups),
        "memory_injected": memory_injected,
        "durable_tool_records": len(state.tool_records),
        "current_turn_records": len(state.current_turn_records),
        "editor_facts": len(state.editor_facts),
        "evidence_references": len(state.evidence_index),
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

    # 任务 8.6：仍保留在出站协议组里的结果，不通过记忆块二次注入。
    retained_call_ids: set[str] = set()
    for group in group_messages(projected):
        if group.kind == "tool_group":
            retained_call_ids.update(group.call_ids)
    memory_block = render_memory_block(state, exclude_call_ids=retained_call_ids)
    memory_injected = bool(memory_block.strip())
    if memory_injected:
        projected.insert(system_end, {"role": "system", "content": memory_block})

    final_violations = validate_projection(projected)
    audit = build_context_audit(projected, state, memory_injected=memory_injected)
    logger.info(
        "Context audit session=%s frame=%s messages=%d tokens=%d conservative=%d turns=%d groups=%d "
        "pending=%d durable_records=%d current_records=%d memory=%s violations=%d",
        session_id,
        frame_id,
        audit["message_count"],
        audit["estimated_tokens"],
        audit["conservative_tokens"],
        audit["retained_turns"],
        audit["protocol_groups"],
        audit["pending_groups"],
        audit["durable_tool_records"],
        audit["current_turn_records"],
        memory_injected,
        len(final_violations),
    )
    return ContextProjection(messages=projected, audit=audit, violations=final_violations)

@dataclass
class BudgetResult:
    """一次硬性预算门检查的结果。

    Attributes:
        passed: 投影（含工具 schema）是否落在预算内。
        estimated_tokens: 最终保守估算的出站总 token 数。
        budget_tokens: 预算上限。
        actions: 为满足预算采取的收缩动作描述（仅结构性标识）。
    """

    passed: bool
    estimated_tokens: int
    budget_tokens: int
    actions: list[str] = field(default_factory=list)


def estimate_tools_tokens(tools: list[dict[str, Any]]) -> int:
    """保守估算工具 schema 的 token 占用（JSON 文本，任务 7.1）。"""
    if not tools:
        return 0
    return estimate_request_tokens([], tools)


def apply_hard_budget(
    projected: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    budget_tokens: int,
    memory_index: int | None,
    retained_call_ids: set[str] | None = None,
) -> BudgetResult:
    """出站前的硬性预算门（任务 7.1/8.6）。

    计数覆盖 system 层、记忆块、普通消息、受保护协议组与工具 schema。
    超预算时依次：收缩有定位符可恢复的记录细节（保留事实卡）→
    按预算约束记忆记录体积；每次收缩后重渲染记忆块并重新计数。仍超
    预算时返回 `passed=False`，调用方必须安全失败、不得发送请求。

    Args:
        projected: 投影后的消息列表（记忆块所在消息会被原地重渲染）。
        tools: 本次请求可见的工具 schema。
        state: 帧记忆状态。
        budget_tokens: 预算（估算 token）。
        memory_index: 记忆块消息在投影里的下标；None 表示未注入。
        retained_call_ids: 仍保留协议组的 call id（重渲染时继续排除）。

    Returns:
        `BudgetResult`。
    """
    def _rerender() -> None:
        if memory_index is not None and 0 <= memory_index < len(projected):
            block = render_memory_block(state, exclude_call_ids=retained_call_ids or set())
            if block.strip():
                projected[memory_index]["content"] = block

    def _total() -> int:
        return estimate_request_tokens(projected, tools)

    actions: list[str] = []
    tokens = _total()
    if tokens <= budget_tokens:
        return BudgetResult(True, tokens, budget_tokens, actions)

    reduced = reduce_tool_record_detail(state)
    if reduced:
        _rerender()
        actions.append(f"reduced_locator_detail:{reduced}")
        tokens = _total()
        if tokens <= budget_tokens:
            return BudgetResult(True, tokens, budget_tokens, actions)

    memory_baseline = max(_total() - estimate_request_tokens(
        [projected[memory_index]] if memory_index is not None and 0 <= memory_index < len(projected) else []
    ), 0)
    trimmed = enforce_memory_budget(
        state, budget_tokens=budget_tokens, baseline_tokens=memory_baseline
    )
    if trimmed:
        _rerender()
        actions.append(f"trimmed_memory_records:{trimmed}")
        tokens = _total()

    return BudgetResult(tokens <= budget_tokens, tokens, budget_tokens, actions)

def retained_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    """收集消息里仍保留的工具协议组的全部 tool_call id（任务 8.6）。"""
    ids: set[str] = set()
    for group in group_messages(messages):
        if group.kind == "tool_group":
            ids.update(group.call_ids)
    return ids
