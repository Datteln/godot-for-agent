"""read_class_docs 工具事实的短暂化与脱敏（fix-transcript-sync-recovery 任务 4.3）。

`read_class_docs` 的有界查询结果只作为"当前模型步骤"的工具事实存在：该步骤
消费完毕后，查询结果不得继续留在会话帧里（后续请求、会话持久化、压缩摘要
都只能看到受限占位符），完整 ClassDB/API 文本绝不进入持久化帧、权威转录、
历史快照或 WebSocket。
"""

from __future__ import annotations

import json
from typing import Any

CLASS_DOCS_TOOL = "read_class_docs"
"""ClassDB 按需查询工具名；其结果是唯一需要短暂化的工具事实。"""

EPHEMERAL_MARK = "ephemeral_class_docs"
"""占位符标记键：既用于生成占位符，也用于识别已短暂化的消息，避免重复处理。"""


def class_docs_placeholder(content: str) -> str:
    """为 read_class_docs 工具结果生成受限的短暂占位符。

    占位符只保留类名、查询模式与成功状态三类标识信息，不含任何成员/常量
    签名等 API 正文，并明确提示后续步骤需重新发起受限查询。

    Args:
        content: read_class_docs 工具消息的原始正文（JSON 字符串）。

    Returns:
        受限占位符的 JSON 字符串。
    """
    class_name = ""
    mode = "overview"
    ok = True
    payload: Any = None
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        body = payload.get("result")
        if not isinstance(body, dict):
            body = payload.get("error") if isinstance(payload.get("error"), dict) else None
        if isinstance(body, dict):
            class_name = str(body.get("class_name", ""))
            mode = str(body.get("mode", mode))
            ok = bool(body.get("ok", True))
    return json.dumps(
        {
            EPHEMERAL_MARK: True,
            "class_name": class_name,
            "mode": mode,
            "ok": ok,
            "note": (
                "read_class_docs facts are ephemeral to the model step that consumed them "
                "and have expired. If the API is still needed, issue a new bounded query "
                "(overview/search/members/constants)."
            ),
        },
        ensure_ascii=False,
    )


def sanitize_class_docs_messages(messages: list[dict[str, Any]]) -> int:
    """把已被模型消费的 read_class_docs 结果原地替换为短暂占位符。

    工具事实只在消费它的那一个模型步骤内有效：步骤结束后，完整查询结果不得
    留在会话帧中（后续请求、会话持久化、压缩摘要都只应看到占位符）。消息按
    顺序扫描，assistant 消息的 `tool_calls` 建立 tool_call_id → 工具名映射，
    其后命中的 `role=tool` 消息才会被替换；已带占位符标记的消息跳过。

    Args:
        messages: 帧消息列表（原地修改）。

    Returns:
        被替换的消息数量。
    """
    tool_names: dict[str, str] = {}
    sanitized = 0
    for message in messages:
        if message.get("role") == "assistant":
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id", ""))
                    function = call.get("function")
                    if call_id and isinstance(function, dict):
                        tool_names[call_id] = str(function.get("name", ""))
            continue
        if message.get("role") != "tool":
            continue
        if tool_names.get(str(message.get("tool_call_id", ""))) != CLASS_DOCS_TOOL:
            continue
        content = message.get("content")
        if not isinstance(content, str) or content == "" or EPHEMERAL_MARK in content:
            continue
        message["content"] = class_docs_placeholder(content)
        sanitized += 1
    return sanitized
