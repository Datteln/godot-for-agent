"""LLM history 载荷的大小限制与重键剔除，供编排层（agent）和查询层（query）共用。

集中管理字符串截断、列表/字典限长、二进制重键剔除和 tool 消息体积控制，
避免两侧各自维护一份参数与实现导致行为漂移。
"""

from __future__ import annotations

import json
from typing import Any, Final

HISTORY_MAX_JSON_CHARS: Final = 80_000
HISTORY_MAX_STRING_CHARS: Final = 16_000
HISTORY_MAX_LIST_ITEMS: Final = 80
HISTORY_MAX_DICT_ITEMS: Final = 120
HISTORY_DROP_KEYS: Final = frozenset(
    {"data_url", "base64", "image_base64", "screenshot_base64", "binary", "bytes"}
)


def json_char_size(value: Any) -> int:
    """Return an approximate serialized JSON character count."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def summarize_history_text(
    text: str,
    max_chars: int = HISTORY_MAX_STRING_CHARS,
) -> str:
    """Keep the beginning and end of oversized text."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return text[:head] + f"\n... ({omitted} chars omitted for history) ...\n" + text[-tail:]


def bounded_history_value(
    value: Any,
    *,
    max_string_chars: int = HISTORY_MAX_STRING_CHARS,
    max_list_items: int = HISTORY_MAX_LIST_ITEMS,
    max_dict_items: int = HISTORY_MAX_DICT_ITEMS,
) -> Any:
    """Recursively bound a value before it enters LLM history."""
    if isinstance(value, str):
        return summarize_history_text(value, max_string_chars)
    if isinstance(value, list):
        bounded = [
            bounded_history_value(
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
        if key_str in HISTORY_DROP_KEYS:
            bounded_dict[f"{key_str}_omitted_for_history"] = True
            continue
        if index >= max_dict_items:
            bounded_dict["history_omitted_keys"] = len(value) - max_dict_items
            break
        bounded_dict[key_str] = bounded_history_value(
            item,
            max_string_chars=max_string_chars,
            max_list_items=max_list_items,
            max_dict_items=max_dict_items,
        )
    return bounded_dict


def bounded_tool_message_body(body: Any) -> Any:
    """Bound one tool message so generic tools cannot bypass history limits."""
    if isinstance(body, str):
        return summarize_history_text(body, HISTORY_MAX_JSON_CHARS)
    if json_char_size(body) <= HISTORY_MAX_JSON_CHARS:
        return body
    bounded = bounded_history_value(body)
    if json_char_size(bounded) <= HISTORY_MAX_JSON_CHARS:
        return bounded
    return {
        "history_truncated": True,
        "summary": summarize_history_text(
            json.dumps(bounded, ensure_ascii=False, default=str),
            HISTORY_MAX_JSON_CHARS,
        ),
    }
