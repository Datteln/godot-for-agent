"""轻量事件日志（§13 事件流）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """一条内部事件。"""

    seq: int
    session_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


_MAX_EVENTS_PER_SESSION = 500

# 这两种事件是逐字/逐 token 的流式增量，同一段（相同 type + frame_id + loop）
# 往往会产生几十条事件，但每条都携带"截至当前的完整累积文本"——只有同一段里
# 最后一条对历史回放/SSE 补发有意义，中间那些只是同一份内容的早期截断版本。
# 不去重的话，_MAX_EVENTS_PER_SESSION 的额度会被这些中间态迅速消耗掉，导致
# 较早几轮的 Thought/正文流被挤出缓冲区，历史回放时只剩最近一两段。
_COALESCED_EVENT_TYPES = {"agent_text_delta", "agent_reasoning_delta"}


def _coalesce_stream_key(event_type: str, payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """非流式增量事件返回 None；流式增量返回其 (type, frame_id, loop) 去重键。"""
    if event_type not in _COALESCED_EVENT_TYPES:
        return None
    return (event_type, str(payload.get("frame_id", "")), str(payload.get("loop", "")))


class EventStore:
    """进程内事件存储；每个会话最多保留最近
    `_MAX_EVENTS_PER_SESSION` 条，超出后丢弃最早的事件，避免长会话无界增长。
    同一段流式增量（见 `_coalesce_stream_key`）原地覆盖而不追加，使额度按
    "动作/分段数"而不是"token tick 数"消耗。M2 可替换为持久化/SSE。
    """

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}

    def append(self, session_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        """追加事件并返回带 seq 的记录；超出上限时丢弃该会话最早的事件。"""
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = Event(seq=seq, session_id=session_id, type=event_type, payload=payload)
        events = self._events.setdefault(session_id, [])

        stream_key = _coalesce_stream_key(event_type, payload)
        if stream_key is not None and events:
            last = events[-1]
            if (last.type, str(last.payload.get("frame_id", "")), str(last.payload.get("loop", ""))) == stream_key:
                events[-1] = event
                logger.debug(
                    "Event coalesced session=%s seq=%d type=%s replaces_seq=%d",
                    session_id,
                    seq,
                    event_type,
                    last.seq,
                )
                return event

        events.append(event)
        if len(events) > _MAX_EVENTS_PER_SESSION:
            del events[: len(events) - _MAX_EVENTS_PER_SESSION]
            logger.debug(
                "Event store pruned session=%s max_events=%d",
                session_id,
                _MAX_EVENTS_PER_SESSION,
            )
        logger.debug("Event appended session=%s seq=%d type=%s", session_id, seq, event_type)
        return event

    def list_after(self, session_id: str, after: int = 0) -> list[Event]:
        """返回指定 seq 之后的事件。"""
        events = [event for event in self._events.get(session_id, []) if event.seq > after]
        logger.debug("Events listed session=%s after=%d count=%d", session_id, after, len(events))
        return events

    def last_seq(self, session_id: str) -> int:
        """返回某会话最后事件序号。"""
        return self._seq.get(session_id, 0)
