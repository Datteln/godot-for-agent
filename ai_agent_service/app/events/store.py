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


class EventStore:
    """进程内事件存储；每个会话最多保留最近
    `_MAX_EVENTS_PER_SESSION` 条，超出后丢弃最早的事件，避免长会话无界增长。
    M2 可替换为持久化/SSE。
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
