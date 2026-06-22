"""稳定前缀缓存断点规划与注入（§16.1）。

把"消息列表里哪些位置值得标记 `cache_control`"（`build_stable_prefix`）与
"如何把标记写进请求体"（`inject_cache_breakpoints`）分开，使两者可以独立
测试；`cache_manager.py` 负责指纹计算，`cache_decision_engine.py` 负责组合
决策，`provider.py` 只管把结果发出去。

百炼的 `cache_control` 落在消息的 *content 块* 上：单条消息的 `content` 可以是
一个 content-block 数组，每个块可独立带 `cache_control`。因此断点用
`CacheBreakpoint(message_index, block_index)` 表达——`block_index=None` 表示
整条消息（字符串 content 会被包成单元素数组），非 None 表示数组里的具体块。
分层 system prompt（L0 核心 / L2 项目上下文 / L3 RAG 等）正是借由"单条 system
消息、多个 content 块、每块一个断点"实现多断点缓存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 百炼显式缓存单次请求最多生效 4 个 `cache_control` 标记（超出时仅最后 4 个
# 生效）；这里在源头就裁剪到这个上限，不依赖端点"丢弃多余标记"的兜底行为。
MAX_CACHE_BREAKPOINTS = 4

# 历史分层的粒度（消息数）：老 tier 断点向下取整到该粒度的整数倍，使它只在
# 每累积这么多条新消息时才整体前移一次，而不是像 recent tier 那样每轮都跟着
# 消息总数右移。长对话因此被分成"几乎永久稳定的老段"（命中率高、创建成本只
# 摊销一次）+ "尺寸恒定的活跃窗口"（每轮变化，但范围不随对话总长度增长），
# 而不是单个随对话无限变长的前缀（§16.1 / 文档 3.10 衍生：tiered caching）。
HISTORY_TIER_GRANULARITY = 20


@dataclass(frozen=True)
class CacheBreakpoint:
    """一个缓存断点：标记 `cache_control` 的位置。

    Attributes:
        message_index: 消息在列表中的下标。
        block_index: 该消息 content 数组内的块下标；为 None 时表示整条消息
            （字符串 content 包成单块，数组 content 取最后一块）。
        segment: 语义分段名，仅用于日志/观测。
    """

    message_index: int
    block_index: int | None
    segment: str


@dataclass(frozen=True)
class StablePrefixPlan:
    """`build_stable_prefix` 的输出：建议的缓存断点位置。

    Attributes:
        breakpoints: 建议标记 `cache_control` 的断点，按出现顺序排列，长度不超过
            `MAX_CACHE_BREAKPOINTS`。
        stable_prefix_end_index: 被断点覆盖到的最末消息下标（含），即"稳定前缀
            结束位置"；没有断点时为 0。
        segments: 与 `breakpoints` 等长、一一对应的语义分段名。
    """

    breakpoints: list[CacheBreakpoint] = field(default_factory=list)
    stable_prefix_end_index: int = 0
    segments: list[str] = field(default_factory=list)


def flatten_message_text(content: Any) -> str:
    """把消息 `content`（字符串或 content-block 数组）拍平为纯文本。

    用于 token 估算、指纹计算、日志摘要等只关心文本的场景；非文本块（图片等）
    与非法结构被忽略。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估算消息列表的输入 token 数，用于判断是否值得启用显式缓存。

    CJK 字符约 1 token/字，其余字符按 UTF-8 字节数 / 4 估算；与端点真实计费
    有偏差，但足以做"消息前缀是否达到缓存阈值"的判断，且不引入 tokenizer 依赖。
    """
    cjk_chars = 0
    other_bytes = 0
    for message in messages:
        for char in flatten_message_text(message.get("content")):
            codepoint = ord(char)
            if (
                0x3400 <= codepoint <= 0x4DBF
                or 0x4E00 <= codepoint <= 0x9FFF
                or 0xF900 <= codepoint <= 0xFAFF
            ):
                cjk_chars += 1
            else:
                other_bytes += len(char.encode("utf-8"))
    return cjk_chars + (other_bytes + 3) // 4


def _cap_breakpoints(breakpoints: list[CacheBreakpoint]) -> list[CacheBreakpoint]:
    """裁剪到端点上限：保留最靠前的若干（更稳定、更易复用的前缀层）。

    分层 system 断点排在最前（每轮几乎不变，缓存收益最高），稳定历史断点排在
    最后（覆盖范围最大但每轮都在变长）；超出上限时优先丢弃靠后的，确保最稳定的
    层一定被缓存。
    """
    if len(breakpoints) <= MAX_CACHE_BREAKPOINTS:
        return breakpoints
    compact = next((bp for bp in breakpoints if bp.segment == "compact_snapshot"), None)
    capped = breakpoints[:MAX_CACHE_BREAKPOINTS]
    if compact is not None and compact not in capped:
        capped[-1] = compact
        capped.sort(
            key=lambda bp: (
                bp.message_index,
                bp.block_index if bp.block_index is not None else -1,
            )
        )
    return capped


def _history_tier_breakpoints(system_end: int, stable_history_end: int) -> list[CacheBreakpoint]:
    """规划历史部分的断点：短历史只给一个 recent 断点，长历史额外加一个老 tier。

    老 tier 断点位置向下取整到 `HISTORY_TIER_GRANULARITY` 的整数倍——只要历史
    还没跨过下一个粒度边界，这个位置在多轮对话里保持不变，对应的缓存段因此能
    被持续命中而不是每轮都当作"新前缀"重新创建（§16.1 tiered caching）。

    Args:
        system_end: 前导 system 消息块的结束下标（见 `build_stable_prefix`）。
        stable_history_end: recent tier 断点位置（`len(messages) - 2`）。

    Returns:
        历史部分的断点列表；按"老 tier 在前、recent 在后"排列，老 tier 不满足
        条件（历史还不够长，或与 recent 重合）时只返回 recent 一个断点。
    """
    if stable_history_end <= system_end:
        return []
    breakpoints: list[CacheBreakpoint] = []
    old_tier_end = (stable_history_end // HISTORY_TIER_GRANULARITY) * HISTORY_TIER_GRANULARITY
    if system_end < old_tier_end < stable_history_end:
        breakpoints.append(CacheBreakpoint(old_tier_end, None, "history_old_tier"))
    breakpoints.append(CacheBreakpoint(stable_history_end, None, "stable_history"))
    return breakpoints


def build_stable_prefix(messages: list[dict[str, Any]]) -> StablePrefixPlan:
    """规划消息列表里值得标记缓存断点的位置（§16.1 多断点）。

    断点来源有四类：
    - **分层 system 层**：首条 system 消息若是 content-block 数组（L0 核心 /
      L2 项目上下文 / L3 RAG 等分层 prompt），为每个块末尾各放一个断点，使
      "L0"、"L0+L2"、"L0+L2+L3" 这些逐层加长的稳定前缀都能独立命中缓存；
    - **system 尾部**：若首条之后还有连续 system 消息（如压缩摘要），在最后一条
      前导 system 消息上补一个整体断点；
    - **历史老 tier**：历史长度跨过 `HISTORY_TIER_GRANULARITY` 个粒度后出现，
      位置只在跨粒度边界时才前移，多数轮次保持不变（见 `_history_tier_breakpoints`）；
    - **稳定历史（recent tier）**：除最新一条消息外的历史前缀末尾——每轮都在
      末尾追加新消息，这个断点因此每轮都跟着右移，覆盖范围则始终是"老 tier
      之后的活跃窗口"，不随对话总长度增长。

    所有断点合并后按上限裁剪（见 `_cap_breakpoints`），最多 `MAX_CACHE_BREAKPOINTS`
    个；裁剪时系统层优先于历史层，历史老 tier 优先于 recent tier（最该保留的
    排在最前）。

    Args:
        messages: 当前帧即将发给 `LLMProvider.chat()` 的完整消息列表。

    Returns:
        建议的断点位置与稳定前缀结束下标；`messages` 为空时返回空计划。
    """
    if not messages:
        return StablePrefixPlan()

    system_end = 0
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            system_end = index
        else:
            break

    breakpoints: list[CacheBreakpoint] = []
    first_content = messages[0].get("content")
    if isinstance(first_content, list) and first_content:
        for block_index, block in enumerate(first_content):
            text = block.get("text", "") if isinstance(block, dict) else ""
            segment = (
                "compact_snapshot"
                if str(text).startswith("[compact_summary]")
                else f"system_layer_{block_index}"
            )
            breakpoints.append(CacheBreakpoint(0, block_index, segment))
    else:
        breakpoints.append(CacheBreakpoint(0, None, "system_core"))

    if system_end > 0:
        breakpoints.append(CacheBreakpoint(system_end, None, "system_tail"))

    if len(messages) >= 2:
        breakpoints.extend(_history_tier_breakpoints(system_end, len(messages) - 2))

    breakpoints = _cap_breakpoints(breakpoints)
    stable_end = max((bp.message_index for bp in breakpoints), default=0)
    return StablePrefixPlan(
        breakpoints=breakpoints,
        stable_prefix_end_index=stable_end,
        segments=[bp.segment for bp in breakpoints],
    )


def inject_cache_breakpoints(
    messages: list[dict[str, Any]],
    breakpoints: list[CacheBreakpoint],
) -> list[dict[str, Any]]:
    """返回在指定断点处标注 `cache_control` 的消息列表副本（§16.1）。

    百炼要求 `cache_control` 落在 content 块上。字符串 `content` 会被包成单元素
    content-block 数组；数组 `content` 按 `block_index` 在对应块上追加标记
    （`block_index=None` 取最后一块）；`content` 既非字符串也非数组（如纯
    tool_calls 的 assistant 消息，`content` 为 None）时该断点静默跳过，不强行
    造出空文本块。只浅拷贝被标注的消息与其块，不改动调用方持有的原始
    `frame.messages`——缓存标记仅用于本次请求，不写入会话历史。

    Args:
        messages: 当前帧的完整消息列表。
        breakpoints: 待标记的断点；超出消息范围的会被忽略，实际生效数量裁剪到
            `MAX_CACHE_BREAKPOINTS`。

    Returns:
        标记好缓存断点的消息列表副本；没有任何断点可标记时原样返回 `messages`。
    """
    valid = [bp for bp in breakpoints if 0 <= bp.message_index < len(messages)][
        :MAX_CACHE_BREAKPOINTS
    ]
    if not valid:
        return messages

    by_message: dict[int, list[int | None]] = {}
    for bp in valid:
        by_message.setdefault(bp.message_index, []).append(bp.block_index)

    marked = list(messages)
    for message_index, block_indices in by_message.items():
        message = dict(marked[message_index])
        content = message.get("content")
        if isinstance(content, list):
            if not content:
                continue
            blocks = [dict(block) if isinstance(block, dict) else block for block in content]
            for block_index in block_indices:
                target = block_index if block_index is not None else len(blocks) - 1
                if 0 <= target < len(blocks) and isinstance(blocks[target], dict):
                    blocks[target] = {**blocks[target], "cache_control": {"type": "ephemeral"}}
            message["content"] = blocks
        elif isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            continue
        marked[message_index] = message
    return marked
