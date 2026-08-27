"""OpenAI 消息的协议安全分组与校验（任务 2.1/2.2）。

把扁平的 `frame.messages` 解析成"协议组"：

- 一条带 `tool_calls` 的 assistant 消息 + 所有与之配对的
  `role=tool` 结果，构成一个 **工具协议组**；组在全部结果到齐前是
  pending/不完整的，绝不允许被压缩、删除或重排；
- user 消息是用户轮边界；不带工具调用的 assistant 消息是普通助手消息。

取消、客户端拒绝、超时、重置等终结性结果必须先补齐配对的
Markdown `role=tool` 终结结果（`terminalize_pending_groups`），
使组重新满足 OpenAI 协议，然后才走正常保留/合并规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MessageGroupKind = Literal["system", "user", "assistant", "tool_group", "orphan_tool"]

TERMINAL_REASONS: tuple[str, ...] = ("cancelled", "rejected", "timeout", "reset", "denied")
"""允许作为终结性工具结果原因的集合。"""

_TERMINAL_MARKER_PREFIX = "<!--tool-terminal:"
_TERMINAL_MARKER_SUFFIX = "-->"


def terminal_marker(reason: str) -> str:
    """生成终结性结果的 Markdown 隐藏标记。

    Args:
        reason: 终结原因（应为 `TERMINAL_REASONS` 之一）。

    Returns:
        形如 `<!--tool-terminal:timeout-->` 的标记，嵌在结果首行。
    """
    return f"{_TERMINAL_MARKER_PREFIX}{reason}{_TERMINAL_MARKER_SUFFIX}"


def is_terminal_content(content: Any) -> bool:
    """判断一条工具结果内容是否带终结性标记。"""
    return isinstance(content, str) and _TERMINAL_MARKER_PREFIX in content


def terminal_reason_of(content: Any) -> str | None:
    """提取终结性标记里的原因；非终结内容返回 None。"""
    if not isinstance(content, str):
        return None
    start = content.find(_TERMINAL_MARKER_PREFIX)
    if start < 0:
        return None
    start += len(_TERMINAL_MARKER_PREFIX)
    end = content.find(_TERMINAL_MARKER_SUFFIX, start)
    if end < 0:
        return None
    return content[start:end]


def extract_tool_call_ids(message: dict[str, Any]) -> list[str]:
    """提取 assistant 消息里全部 tool_call id（保持声明顺序）。"""
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    ids: list[str] = []
    for call in raw_calls:
        if isinstance(call, dict):
            call_id = str(call.get("id", "") or "")
            if call_id:
                ids.append(call_id)
    return ids


def tool_call_names(message: dict[str, Any]) -> dict[str, str]:
    """提取 assistant 消息里 tool_call id → 工具名 的映射。"""
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return {}
    names: dict[str, str] = {}
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id", "") or "")
        function = call.get("function")
        if call_id and isinstance(function, dict):
            names[call_id] = str(function.get("name", "") or "")
    return names


@dataclass
class MessageGroup:
    """消息列表中的一个协议组。

    Attributes:
        kind: 组类别；`orphan_tool` 表示未匹配到任何工具调用的孤立结果。
        start: 组首条消息下标（含）。
        end: 组末条消息下标（不含）。
        call_ids: 工具协议组声明的全部 tool_call id（按声明顺序）。
        matched_ids: 已收到结果的 tool_call id 集合。
        complete: 是否全部结果到齐（非工具组恒为 True）。
        terminal: 是否为终结性组（全部结果都带终结标记）。
        assistant_text: assistant 消息里除工具调用外的用户可见文本。
    """

    kind: MessageGroupKind
    start: int
    end: int
    call_ids: list[str] = field(default_factory=list)
    matched_ids: set[str] = field(default_factory=set)
    complete: bool = True
    terminal: bool = False
    assistant_text: str = ""

    @property
    def missing_ids(self) -> list[str]:
        """尚未收到结果的 tool_call id（按声明顺序）。"""
        return [call_id for call_id in self.call_ids if call_id not in self.matched_ids]

    @property
    def is_pending(self) -> bool:
        """是否为未完成/挂起的工具协议组。"""
        return self.kind == "tool_group" and not self.complete


def _flatten_text(content: Any) -> str:
    """把消息 content 拍平为文本（字符串或 content-block 数组）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def group_messages(messages: list[dict[str, Any]]) -> list[MessageGroup]:
    """把 OpenAI 消息列表解析为协议组序列（纯函数，不修改输入）。

    解析规则：
    - 连续的前导 `system` 消息各自成组；
    - 带 `tool_calls` 的 assistant 消息开启工具协议组，其后紧邻的
      `role=tool` 消息按 `tool_call_id` 归入该组；遇到非配对消息即
      结束该组（缺失结果 => 组不完整）；
    - 未匹配任何已声明调用的 `role=tool` 消息单独成为 `orphan_tool` 组，
      由 `validate_projection` 报错；
    - user / 无工具调用的 assistant 消息各自成组。

    Args:
        messages: 待解析的消息列表。

    Returns:
        覆盖全部消息的协议组列表（相邻组下标连续）。
    """
    groups: list[MessageGroup] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        role = message.get("role")
        if role == "system":
            groups.append(MessageGroup(kind="system", start=index, end=index + 1))
            index += 1
            continue
        if role == "user":
            groups.append(MessageGroup(kind="user", start=index, end=index + 1))
            index += 1
            continue
        if role == "assistant":
            call_ids = extract_tool_call_ids(message)
            if not call_ids:
                groups.append(
                    MessageGroup(
                        kind="assistant",
                        start=index,
                        end=index + 1,
                        assistant_text=_flatten_text(message.get("content")),
                    )
                )
                index += 1
                continue
            matched: set[str] = set()
            cursor = index + 1
            result_terminal: list[bool] = []
            while cursor < total:
                candidate = messages[cursor]
                if candidate.get("role") != "tool":
                    break
                tool_call_id = str(candidate.get("tool_call_id", "") or "")
                if tool_call_id not in call_ids or tool_call_id in matched:
                    break
                matched.add(tool_call_id)
                result_terminal.append(is_terminal_content(candidate.get("content")))
                cursor += 1
            complete = matched == set(call_ids)
            terminal = complete and bool(result_terminal) and all(result_terminal)
            groups.append(
                MessageGroup(
                    kind="tool_group",
                    start=index,
                    end=cursor,
                    call_ids=list(call_ids),
                    matched_ids=matched,
                    complete=complete,
                    terminal=terminal,
                    assistant_text=_flatten_text(message.get("content")),
                )
            )
            index = cursor
            continue
        if role == "tool":
            groups.append(
                MessageGroup(kind="orphan_tool", start=index, end=index + 1, complete=False)
            )
            index += 1
            continue
        # 未知 role：单独成组，保留原样。
        groups.append(MessageGroup(kind="assistant", start=index, end=index + 1))
        index += 1
    return groups


def validate_projection(messages: list[dict[str, Any]]) -> list[str]:
    """校验一个即将发给 LLM 的消息投影不破坏 OpenAI 工具协议（任务 2.2）。

    规则：
    - 不允许孤立的 `role=tool` 结果（找不到声明它的 assistant 调用）；
    - 不允许 assistant 声明了调用却缺失配对结果——**唯一例外**是消息列表
      末尾的单个挂起组（前端工具尚未回传）；
    - 挂起组只能出现在序列末尾，不能夹在后续消息中间。

    Args:
        messages: 投影后的消息列表。

    Returns:
        违规描述列表；为空表示协议有效。
    """
    violations: list[str] = []
    groups = group_messages(messages)
    for position, group in enumerate(groups):
        if group.kind == "orphan_tool":
            violations.append(f"orphan_tool_result_at_{group.start}")
            continue
        if group.kind != "tool_group" or group.complete:
            continue
        is_tail = position == len(groups) - 1
        if not is_tail:
            violations.append(f"pending_group_not_at_tail_{group.start}")
        missing = ",".join(group.missing_ids)
        violations.append(f"missing_tool_results_at_{group.start}:{missing}")
    return violations


def terminalize_pending_groups(
    messages: list[dict[str, Any]],
    reason: str,
    *,
    pending_call_ids: set[str] | None = None,
) -> int:
    """为缺失结果的工具调用补齐 Markdown 终结结果，使协议组闭合。

    仅处理消息列表末尾的挂起组（协议上唯一合法的挂起位置）；若提供了
    `pending_call_ids`，则只终结其中声明的调用（其余仍视为等待中）。
    取消、拒绝、超时、重置等路径必须先调用本函数，再执行任何保留/压缩。

    Args:
        messages: 帧消息列表（原地追加）。
        reason: 终结原因（`TERMINAL_REASONS` 之一）。
        pending_call_ids: 可选的允许终结的调用 id 集合。

    Returns:
        追加的终结结果消息数量。
    """
    normalized_reason = reason if reason in TERMINAL_REASONS else "reset"
    groups = group_messages(messages)
    appended = 0
    for group in reversed(groups):
        if group.kind in ("system", "user", "assistant", "orphan_tool"):
            continue
        if group.complete:
            break
        assistant_message = messages[group.start]
        names = tool_call_names(assistant_message)
        for call_id in group.missing_ids:
            if pending_call_ids is not None and call_id not in pending_call_ids:
                continue
            tool_name = names.get(call_id, "") or "unknown"
            content = (
                f"{terminal_marker(normalized_reason)}\n"
                f"### 工具结果：{tool_name}（终结）\n"
                f"- 状态：{normalized_reason}\n"
                "- 来源：system\n"
                "- 新鲜度：observed；验证：待校验\n"
                f"- 说明：该工具调用未返回正常结果（{normalized_reason}），"
                "此条为协议闭合占位；如需该信息，请重新发起调用。"
            )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
            appended += 1
        break
    return appended


def turn_retention_boundary(
    messages: list[dict[str, Any]],
    *,
    retained_turns: int,
    protected_from: int | None = None,
) -> int:
    """按完整用户轮计算保留边界（任务 2.3）。

    从消息尾部向前数 `retained_turns` 个完整用户轮，返回可安全移除区
    与保留区的分界下标：边界永远落在 user 组起点（或系统层之后），绝不
    切开用户轮或工具协议组；`protected_from` 之后的消息（当前请求 /
    挂起组）无条件受保护。

    Args:
        messages: 帧消息列表。
        retained_turns: 需要保留的完整用户轮数量。
        protected_from: 受保护区起点下标；该下标之后的消息不参与移除。

    Returns:
        保留区起点下标；[1, 返回值) 之间的消息可被整体收拢。
    """
    groups = group_messages(messages)
    system_end = 0
    for group in groups:
        if group.kind == "system":
            system_end = group.end
        else:
            break
    if retained_turns <= 0:
        return system_end

    user_starts: list[int] = []
    for group in groups:
        if group.kind == "user":
            if protected_from is not None and group.start >= protected_from:
                continue
            user_starts.append(group.start)
    if len(user_starts) <= retained_turns:
        return system_end
    boundary = user_starts[-retained_turns]
    if protected_from is not None and boundary > protected_from:
        boundary = protected_from
    return max(boundary, system_end)
