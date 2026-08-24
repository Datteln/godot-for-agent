"""实时 `transcript_patch` 的有界载荷表示决策（任务 1.3 / 2.1）。

权威展示稿与历史快照始终保留完整条目；实时传输只为“增长中的
Thought/assistant 正文”选择有界表示：

- ``full``：条目完整最新状态。首次可见、结构性状态与终态恒用该表示。
- ``append_delta``：相对 ``base_revision`` 的纯追加后缀。客户端只有在自己
  已接受的修订号等于 ``base_revision`` 时才能应用；否则走快照重同步。
- ``preview``：正文末尾的有界预览（含总字符数）。预览之后服务端对该条目
  的下一个可组合发布必须回退为 ``full``，因为预览不携带可重建的前缀。

表示决策是纯函数：给定完整补丁与该条目上一次实际发布的表示状态，产出
线上载荷与新的发布状态。限速暂存、订阅合并等传输细节不影响该决策。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.transcript.models import (
    GROWING_STREAM_STATES,
    PATCH_FORMAT_APPEND_DELTA,
    PATCH_FORMAT_FULL,
    PATCH_FORMAT_PREVIEW,
    REALTIME_PATCH_VERSION,
    STREAM_TEXT_FIELDS,
    TERMINAL_ENTRY_STATES,
)

_DEFAULT_PREVIEW_MAX_CHARS = 800


@dataclass(frozen=True)
class LastPublishedStream:
    """某条目上一次实际发布的实时表示状态（仅内存，不持久化）。

    Attributes:
        revision: 上一次发布的条目修订号。
        text: 上一次发布时条目的完整正文（仅组合发布时可信）。
        representation: 上一次发布的表示（``full``/``append_delta``/``preview``）。
        composable: 客户端能否凭已接受状态重建精确正文。``preview`` 之后为
            False，下一次发布必须回退为 ``full``。
    """

    revision: int
    text: str
    representation: str
    composable: bool


def is_growing_stream_entry(entry: dict[str, Any]) -> bool:
    """判断条目是否处于“正文仍在增长”的流式组合状态。"""
    kind = str(entry.get("kind", ""))
    return GROWING_STREAM_STATES.get(kind) == str(entry.get("state", ""))


def is_terminal_entry(entry: dict[str, Any]) -> bool:
    """判断条目是否处于其类型定义的终态。"""
    kind = str(entry.get("kind", ""))
    return str(entry.get("state", "")) in TERMINAL_ENTRY_STATES.get(kind, frozenset())


def _stamped_full(payload: dict[str, Any]) -> dict[str, Any]:
    """为完整补丁打上版本化表示标记。"""
    stamped = copy.deepcopy(payload)
    stamped["patch_format"] = PATCH_FORMAT_FULL
    stamped["patch_version"] = REALTIME_PATCH_VERSION
    return stamped


def build_realtime_patch(
    full_payload: dict[str, Any],
    last: LastPublishedStream | None,
    *,
    preview_max_chars: int = _DEFAULT_PREVIEW_MAX_CHARS,
) -> tuple[dict[str, Any], LastPublishedStream]:
    """为一次 `transcript_patch` 发布选择有界实时表示。

    Args:
        full_payload: 写入端发布的完整补丁，含 ``entry`` 与 ``stream_key``。
        last: 该条目上一次实际发布的表示状态；首次发布为 None。
        preview_max_chars: 受限预览的最大字符数。

    Returns:
        ``(线上载荷, 新的发布状态)``。线上载荷可能是完整补丁、追加增量或
        受限预览；新的发布状态供同一发布通道下一次决策使用。
    """
    entry = full_payload.get("entry")
    if not isinstance(entry, dict):
        return _stamped_full(full_payload), _full_state(full_payload)
    kind = str(entry.get("kind", ""))
    text_field = STREAM_TEXT_FIELDS.get(kind)
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    new_text = str(payload.get(text_field, "")) if text_field is not None else ""
    revision = int(entry.get("revision", 1))

    if text_field is None or not is_growing_stream_entry(entry) or last is None:
        # 首次可见、结构性状态、终态与非流式条目：恒用完整补丁。
        return _stamped_full(full_payload), LastPublishedStream(
            revision=revision, text=new_text, representation=PATCH_FORMAT_FULL, composable=True
        )

    if last.composable and new_text.startswith(last.text):
        delta = {
            "stream_key": str(full_payload.get("stream_key", entry.get("entry_id", ""))),
            "patch_format": PATCH_FORMAT_APPEND_DELTA,
            "patch_version": REALTIME_PATCH_VERSION,
            "entry_id": str(entry.get("entry_id", "")),
            "kind": kind,
            "state": str(entry.get("state", "")),
            "revision": revision,
            "base_revision": last.revision,
            "text_field": text_field,
            "append_text": new_text[len(last.text) :],
        }
        token_count = payload.get("token_count")
        if token_count is not None:
            delta["meta"] = {"token_count": int(token_count)}
        return delta, LastPublishedStream(
            revision=revision,
            text=new_text,
            representation=PATCH_FORMAT_APPEND_DELTA,
            composable=True,
        )

    if not last.composable or len(new_text) <= preview_max_chars:
        # 预览之后必须回退完整补丁（前缀不可重建）；短文本完整补丁已有界。
        return _stamped_full(full_payload), LastPublishedStream(
            revision=revision, text=new_text, representation=PATCH_FORMAT_FULL, composable=True
        )

    preview = {
        "stream_key": str(full_payload.get("stream_key", entry.get("entry_id", ""))),
        "patch_format": PATCH_FORMAT_PREVIEW,
        "patch_version": REALTIME_PATCH_VERSION,
        "entry_id": str(entry.get("entry_id", "")),
        "kind": kind,
        "state": str(entry.get("state", "")),
        "revision": revision,
        "base_revision": last.revision,
        "text_field": text_field,
        "preview_text": new_text[-preview_max_chars:],
        "total_chars": len(new_text),
    }
    return preview, LastPublishedStream(
        revision=revision, text=new_text, representation=PATCH_FORMAT_PREVIEW, composable=False
    )


def _full_state(full_payload: dict[str, Any]) -> LastPublishedStream:
    """从完整补丁提取发布状态（用于无法解析条目时的保守回退）。"""
    entry = full_payload.get("entry")
    entry = entry if isinstance(entry, dict) else {}
    kind = str(entry.get("kind", ""))
    text_field = STREAM_TEXT_FIELDS.get(kind)
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return LastPublishedStream(
        revision=int(entry.get("revision", 1)),
        text=str(payload.get(text_field, "")) if text_field is not None else "",
        representation=PATCH_FORMAT_FULL,
        composable=True,
    )
