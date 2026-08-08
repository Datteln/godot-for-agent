"""跨簇共享的文本截断与工具函数。"""

from __future__ import annotations

import logging
from app.llm.message_transformer import estimate_message_tokens, flatten_message_text
from typing import Any
logger = logging.getLogger(__name__)


# 单条消息允许的最大预估 token 数（见 `compact()` 里的"超大单条消息"截断）：
# 超过此值即视为异常（粘贴了整份大文件、工具结果未经摘要直接落地等），即使
# 帧总消息数还没到 `keep_recent` 门槛也会被截断。否则当消息数 <= keep_recent+2
# 时 `compact()` 对该帧完全是空操作——auto-compact 会在后续每个请求里反复
# 触发却什么都没压缩（§16.1 策略 A 的已知缺陷，已修复）。截断目标长度选得
# 足够小，保证截断后的消息再次估算时必然低于阈值（幂等，不会被重复截断）。
_OVERSIZED_MESSAGE_TOKEN_THRESHOLD = 4000


_OVERSIZED_MESSAGE_TRUNCATE_CHARS = 3000


_COMPACT_SUMMARY_MAX_CHARS = 12_000


def _truncate_oversized_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """单条消息预估 token 数超过 `_OVERSIZED_MESSAGE_TOKEN_THRESHOLD` 时返回截断副本。

    与 `compact()` 现有的"按消息数收拢成摘要"逻辑互补：那段逻辑只在帧总长度
    超过 `keep_recent` 门槛时才生效，对"消息数很少但单条内容巨大"的帧完全
    不起作用。这里独立判断单条消息大小，不依赖帧总长度。

    Args:
        message: 待检查的消息字典（OpenAI message dict）。

    Returns:
        预估 token 数未超阈值，或没有可截断的文本内容时返回 None（不修改）；
        否则返回浅拷贝并替换 `content` 为截断文本 + 提示的新消息字典。
    """
    flattened = flatten_message_text(message.get("content"))
    if not flattened or estimate_message_tokens([message]) <= _OVERSIZED_MESSAGE_TOKEN_THRESHOLD:
        return None
    truncated = _truncate_text(flattened, _OVERSIZED_MESSAGE_TRUNCATE_CHARS)
    note = f"\n…（原始内容过大已自动截断；原始约 {len(flattened)} 字符）"
    new_message = dict(message)
    new_message["content"] = truncated + note
    return new_message


def _truncate_text(text: str, max_chars: int) -> str:
    # 按字符数截断超长文本，避免会话历史里堆入过长内容。
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
