"""把 Map 执行事实投影为安全的编排事件。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.agents.types import Frame
from app.llm.cache_decision_engine import CacheDecision
from app.llm.cache_observability import CacheMetricsCollector, CacheMetricsSnapshot
from app.llm.provider import (
    AssistantTurn,
)
from app.orchestrator.turn.event_projection import result_summary_for_event


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
    return result_summary_for_event(tool_name, result, is_error)


def _emit_orchestration_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if event_callback is None:
        return
    event_callback(event_type, payload)


def _history_timeline_payload(frame: Frame) -> dict[str, Any]:
    """Return the persisted timeline anchor for root and delegated frames."""
    return {
        "timeline_frame_id": frame.history_anchor_frame_id or frame.id,
        "timeline_message_index": (
            frame.history_anchor_message_index
            if frame.history_anchor_message_index is not None
            else len(frame.messages)
        ),
    }


def _estimate_stream_token_count(text: str) -> int:
    """Estimate tokens for an accumulated stream without model-specific dependencies."""
    if not text:
        return 0
    cjk_chars = 0
    other_bytes = 0
    for char in text:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            cjk_chars += 1
        else:
            other_bytes += len(char.encode("utf-8"))
    return max(cjk_chars + (other_bytes + 3) // 4, 1)


def _delta_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
    message_index: int,
    timeline_frame_id: str,
    timeline_message_index: int,
) -> Callable[[str, str, int | None], None] | None:
    """构造传给 `LLMProvider.chat` 的流式增量回调，转发为编排事件。

    Args:
        event_callback: 编排事件回调；为 None 时不产生增量事件。
        frame_id: 本轮所属的 agent 帧 id，供前端关联增量与对应消息。
        loop: 本轮在 `TurnDriver.run` 中的循环序号（从 1 开始）。
        message_index: 本次 LLM 响应即将写入 `frame.messages` 的位置，供历史交织。

    Returns:
        转发增量为 `agent_text_delta`/`agent_reasoning_delta` 事件的回调；
        `event_callback` 为 None 时返回 None。
    """
    if event_callback is None:
        return None

    reasoning_started_at = time.monotonic()
    accumulated_text: dict[str, str] = {"content": "", "reasoning": ""}
    chunk_index = 0

    # 上游 provider 可能把同一条 assistant 消息的 content 与
    # reasoning_content 交错发送。message_index 已经是一次 LLM 调用的稳定
    # 身份，不能再以通道切换作为正文分段边界，否则会截断正文并导致 final
    # 无法收敛替换流式消息。
    def _on_delta(kind: str, text: str, token_count: int | None) -> None:
        nonlocal chunk_index
        chunk_index += 1
        # 同一次 LLM 调用内的 reasoning/content 均使用同一个 segment。
        # 前端据此把 reasoning 合并进同一 Thought，并持续累积同一正文块。
        event_type = "agent_reasoning_delta" if kind == "reasoning" else "agent_text_delta"
        accumulated_text[kind] = accumulated_text.get(kind, "") + text
        payload: dict[str, Any] = {
            "frame_id": frame_id,
            "loop": loop,
            "message_index": message_index,
            "timeline_frame_id": timeline_frame_id,
            "timeline_message_index": timeline_message_index,
            "stream_segment": 0,
            "text": text,
            "append_delta": True,
            "provider_chunk_index": chunk_index,
            "provider_first_chunk": chunk_index == 1,
        }
        if kind == "reasoning":
            payload["elapsed_ms"] = max(int((time.monotonic() - reasoning_started_at) * 1000), 1)
            payload["token_count"] = (
                token_count
                if token_count is not None
                else _estimate_stream_token_count(accumulated_text[kind])
            )
        event_callback(event_type, payload)

    return _on_delta


def _record_cache_metrics(
    cache_metrics: CacheMetricsCollector | None,
    decision: CacheDecision | None,
    turn: AssistantTurn,
) -> None:
    """把本轮缓存决策与实际命中结果写入观测层（§16.1 非功能需求：仅日志/监控）。

    Args:
        cache_metrics: 进程内缓存指标聚合器；为 None 时不记录。
        decision: 本轮的 `CacheDecisionEngine.decide()` 结果；为 None 表示
            本次请求未启用缓存决策（如 provider 不支持显式缓存）。
        turn: 本轮 `LLMProvider.chat()` 的返回。
    """
    if cache_metrics is None or decision is None:
        return
    total = turn.total_input_tokens or 0
    cached = turn.cached_tokens or 0
    hit_ratio = cached / total if total > 0 else 0.0
    cache_metrics.record(
        CacheMetricsSnapshot(
            cache_key=decision.cache_key,
            repo_fingerprint=decision.repo_fingerprint,
            tool_schema_version=decision.tool_schema_version,
            cached_tokens=cached,
            total_tokens=total,
            hit_ratio=hit_ratio,
            prefix_segments_used=decision.segments_used,
            cache_enabled=decision.enabled,
        )
    )


def _emit_cache_hit_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame: Frame,
    loop: int,
    turn: AssistantTurn,
) -> None:
    """命中上下文缓存时发出 `cache_hit` 事件（§16.1）。

    仅在 usage 报告了命中缓存 token（`cached_tokens > 0`）且总输入 token 可用时
    发出；未命中则静默，避免在消息列表里堆噪音。不附带"节省比例"——百炼的
    实际折扣因命中类型（隐式/显式）与路由到的具体模型而异，usage 字段无法
    反推具体属于哪种，硬编码一个比例只会是误导性的假精度。

    Args:
        event_callback: 编排事件回调；为 None 时不产生事件。
        frame: 本轮所属的 agent 帧。
        loop: 本轮在 `TurnDriver.run` 中的循环序号（从 1 开始）。
        turn: 本轮 `LLMProvider.chat()` 的返回，携带 `cached_tokens`/
            `total_input_tokens`/`cache_creation_tokens`。
    """
    cached = turn.cached_tokens
    total = turn.total_input_tokens
    if event_callback is None or not cached or cached <= 0 or not total or total <= 0:
        return
    event_callback(
        "cache_hit",
        {
            "frame_id": frame.id,
            "loop": loop,
            "cached_tokens": cached,
            "total_input_tokens": total,
            "cache_creation_tokens": turn.cache_creation_tokens or 0,
        },
    )


def _emit_context_usage_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame: Frame,
    loop: int,
    turn: AssistantTurn,
    token_limit: int | None,
) -> None:
    """Emit current prompt usage against the configured context limit."""
    used = turn.total_input_tokens
    if (
        event_callback is None
        or used is None
        or used < 0
        or token_limit is None
        or token_limit <= 0
    ):
        return
    event_callback(
        "context_usage",
        {
            "frame_id": frame.id,
            "loop": loop,
            "used_tokens": used,
            "token_limit": token_limit,
        },
    )


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
        loop: 本轮在 `TurnDriver.run` 中的循环序号（从 1 开始）。

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
