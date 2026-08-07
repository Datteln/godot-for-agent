"""轻量事件日志（§13 事件流）。"""

from __future__ import annotations

import asyncio
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
    session_epoch: str = ""


@dataclass(frozen=True)
class EventPage:
    """A bounded, sequence-ordered event page."""

    events: list[Event]
    cursor: int
    has_more: bool
    session_epoch: str = ""


_MAX_EVENTS_PER_SESSION = 500


class EventStore:
    """进程内事件存储；每个会话最多保留最近
    `_MAX_EVENTS_PER_SESSION` 条，超出后丢弃最早的事件，避免长会话无界增长。
    同一段流式增量（见 `_coalesce_stream_key`）原地覆盖而不追加，使额度按
    "动作/分段数"而不是"token tick 数"消耗。M2 可替换为持久化/SSE。
    """

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._epochs: dict[str, str] = {}
        self._signals: dict[str, asyncio.Event] = {}

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_epoch: str | None = None,
    ) -> Event:
        """追加事件并返回带 seq 的记录；超出上限时丢弃该会话最早的事件。"""
        if session_epoch is not None:
            current_epoch = self._epochs.get(session_id)
            if current_epoch is not None and current_epoch != session_epoch:
                raise ValueError("cannot append an event for a stale session epoch")
            self._epochs[session_id] = session_epoch
        epoch = self._epochs.get(session_id, "")
        delivery_id = payload.get("_delivery_id")
        if isinstance(delivery_id, str) and delivery_id:
            for existing in reversed(self._events.get(session_id, [])):
                if (
                    existing.session_epoch == epoch
                    and existing.payload.get("_delivery_id") == delivery_id
                ):
                    return existing
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = Event(
            seq=seq,
            session_id=session_id,
            session_epoch=epoch,
            type=event_type,
            payload=payload,
        )
        events = self._events.setdefault(session_id, [])

        events.append(event)
        if len(events) > _MAX_EVENTS_PER_SESSION:
            del events[: len(events) - _MAX_EVENTS_PER_SESSION]
            logger.debug(
                "Event store pruned session=%s max_events=%d",
                session_id,
                _MAX_EVENTS_PER_SESSION,
            )
        signal = self._signals.get(session_id)
        if signal is not None:
            signal.set()
        logger.debug("Event appended session=%s seq=%d type=%s", session_id, seq, event_type)
        return event

    def ensure_sequence(
        self,
        session_id: str,
        persisted_seq: int,
        *,
        session_epoch: str | None = None,
    ) -> None:
        """让进程内序号从持久化 cursor 之后继续，避免重启后回退到 1。

        当会话从磁盘加载时，其 history_event_counter 可能远大于进程内的 _seq 计数。
        此方法把进程内序号推进到持久化值，确保新追加的事件序号严格递增、不与
        历史事件序号冲突。
        """
        if persisted_seq > self._seq.get(session_id, 0):
            self._seq[session_id] = persisted_seq
        if session_epoch is not None:
            current_epoch = self._epochs.get(session_id)
            if current_epoch is not None and current_epoch != session_epoch:
                self._events.pop(session_id, None)
            self._epochs[session_id] = session_epoch

    def reset(self, session_id: str, session_epoch: str) -> Event:
        """切换事件 epoch、丢弃旧内容并保留严格递增的 sequence high-water。"""
        self._events.pop(session_id, None)
        self._epochs[session_id] = session_epoch
        return self.append(
            session_id,
            "session_reset",
            {
                "session_epoch": session_epoch,
                "boundary": True,
            },
            session_epoch=session_epoch,
        )

    def list_after(
        self,
        session_id: str,
        after: int = 0,
        *,
        session_epoch: str | None = None,
    ) -> list[Event]:
        """返回指定 seq 之后的事件。"""
        if session_epoch is not None and self._epochs.get(session_id, "") != session_epoch:
            return []
        events = [event for event in self._events.get(session_id, []) if event.seq > after]
        if events:
            logger.debug(
                "Events listed session=%s after=%d count=%d", session_id, after, len(events)
            )
        return events

    def page_after(
        self,
        session_id: str,
        after: int = 0,
        *,
        limit: int = 50,
        session_epoch: str | None = None,
    ) -> EventPage:
        """返回有界事件页；cursor 只覆盖本页实际返回的事件。"""
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        current_epoch = self._epochs.get(session_id, "")
        if session_epoch is not None and current_epoch != session_epoch:
            return EventPage(
                events=[],
                cursor=self.last_seq(session_id),
                has_more=False,
                session_epoch=current_epoch,
            )
        candidates = [event for event in self._events.get(session_id, []) if event.seq > after]
        page_events = candidates[:limit]
        cursor = page_events[-1].seq if page_events else after
        has_more = len(candidates) > len(page_events)
        if page_events or has_more:
            logger.debug(
                "Event page listed session=%s after=%d limit=%d count=%d "
                "cursor=%d has_more=%s backlog=%d",
                session_id,
                after,
                limit,
                len(page_events),
                cursor,
                has_more,
                max(len(candidates) - len(page_events), 0),
            )
        return EventPage(
            events=page_events,
            cursor=cursor,
            has_more=has_more,
            session_epoch=current_epoch,
        )

    def last_seq(self, session_id: str) -> int:
        """返回某会话最后事件序号。"""
        return self._seq.get(session_id, 0)

    def current_epoch(self, session_id: str) -> str:
        """返回进程内事件流当前 epoch。"""
        return self._epochs.get(session_id, "")

    def oldest_seq(self, session_id: str) -> int | None:
        """返回当前保留窗口最早序号；没有事件时返回 ``None``。"""
        events = self._events.get(session_id, [])
        return events[0].seq if events else None

    async def wait_for_change(
        self,
        session_id: str,
        after: int,
        *,
        timeout_s: float,
    ) -> bool:
        """等待 ``after`` 后出现事件，供 WebSocket 推送而非客户端轮询。"""
        if self.last_seq(session_id) > after:
            return True
        signal = self._signals.setdefault(session_id, asyncio.Event())
        signal.clear()
        if self.last_seq(session_id) > after:
            return True
        try:
            await asyncio.wait_for(signal.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return self.last_seq(session_id) > after
