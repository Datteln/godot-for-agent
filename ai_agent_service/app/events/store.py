"""可恢复 WebSocket 传输使用的进程内事件日志。"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MAX_EVENTS_PER_SESSION = 500
_STREAM_EVENT_TYPES = frozenset({"agent_text_delta", "agent_reasoning_delta", "transcript_patch"})
_STREAM_PUBLICATION_INTERVAL_S = 0.05


@dataclass(frozen=True)
class Event:
    """表示一条具备稳定身份的会话事件。"""

    seq: int
    session_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    turn_id: str | None = None

    @property
    def event_id(self) -> str:
        """返回由会话和序号确定的不可变事件标识。"""
        return f"{self.session_id}:{self.seq}"

    def to_wire(self) -> dict[str, Any]:
        """返回 WebSocket 协议使用的规范事件包络。"""
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "payload": copy.deepcopy(self.payload),
        }


@dataclass(frozen=True)
class ResyncRequired:
    """表示订阅者因队列背压必须重同步。"""

    session_id: str
    after_seq: int
    reason: str = "outbound_queue_overflow"


@dataclass(eq=False)
class EventSubscription:
    """表示单个会话的有界实时事件订阅。"""

    session_id: str
    queue: asyncio.Queue[Event | ResyncRequired]
    acknowledged_seq: int = 0
    closed: bool = False

    def acknowledge(self, seq: int) -> None:
        """记录客户端已连续接受的最大序号。"""
        self.acknowledged_seq = max(self.acknowledged_seq, seq)

    def close(self) -> None:
        """标记订阅为已关闭，禁止后续投递。"""
        self.closed = True


class EventStore:
    """维护有界、不可变且可供实时订阅的会话事件序列。"""

    def __init__(
        self, *, max_events_per_session: int = _MAX_EVENTS_PER_SESSION, outbound_queue_size: int = 128
    ) -> None:
        """初始化事件保留区、订阅注册表和流式发布限速器。"""
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._subscriptions: dict[str, set[EventSubscription]] = {}
        self._max_events_per_session = max_events_per_session
        self._outbound_queue_size = outbound_queue_size
        self._last_stream_publication: dict[tuple[str, str, str, str], float] = {}
        self._pending_streams: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}

    def append(self, session_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        """追加事件；高频流式快照会在分配序号前限速并于边界处刷新。"""
        stream_key = self._stream_key(session_id, event_type, payload)
        if stream_key is not None and not self._should_publish_stream(stream_key):
            self._pending_streams[stream_key] = (event_type, copy.deepcopy(payload))
            return self._latest_event(session_id)
        if stream_key is None:
            self._flush_pending_streams(session_id)
        return self._publish(session_id, event_type, payload, stream_key)

    def seed(self, session_id: str, seq_floor: int) -> None:
        """把会话序号下限抬升到持久化游标，保证重启后序号不回退。

        仅抬不降：进程存活期间内存序号可能已高于持久化值。

        Args:
            session_id: 会话 id。
            seq_floor: 持久化的最大已发布序号（`Session.event_seq`）。
        """
        if seq_floor > self._seq.get(session_id, 0):
            self._seq[session_id] = seq_floor

    def flush_pending_streams(self, session_id: str) -> None:
        """立即发布该会话暂存的流式快照，用于轮次收尾等边界。"""
        self._flush_pending_streams(session_id)

    def list_after(self, session_id: str, after: int = 0) -> list[Event]:
        """返回指定游标之后保留的事件，顺序始终递增。"""
        return [event for event in self._events.get(session_id, []) if event.seq > after]

    def retained_range(self, session_id: str) -> tuple[int | None, int]:
        """返回会话当前保留范围的最早与最新序号。"""
        events = self._events.get(session_id, [])
        return (events[0].seq if events else None, self.last_seq(session_id))

    def last_seq(self, session_id: str) -> int:
        """返回会话已经发布的最大事件序号。"""
        return self._seq.get(session_id, 0)

    def subscribe(self, session_id: str, after_seq: int) -> tuple[list[Event], EventSubscription]:
        """原子地取得重放事件并注册后续实时投递。"""
        replay = self.list_after(session_id, after_seq)
        subscription = EventSubscription(
            session_id=session_id,
            queue=asyncio.Queue(maxsize=self._outbound_queue_size),
            acknowledged_seq=after_seq,
        )
        self._subscriptions.setdefault(session_id, set()).add(subscription)
        return replay, subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """移除连接已关闭的订阅。"""
        subscription.close()
        subscribers = self._subscriptions.get(subscription.session_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._subscriptions.pop(subscription.session_id, None)

    def _publish(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        stream_key: tuple[str, str, str, str] | None,
    ) -> Event:
        """为一条已通过限速的事件分配序号并扇出。"""
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        immutable_payload = copy.deepcopy(payload)
        event = Event(
            seq=seq,
            session_id=session_id,
            type=event_type,
            payload=immutable_payload,
            task_id=self._optional_payload_id(immutable_payload, "task_id"),
            turn_id=self._optional_payload_id(immutable_payload, "turn_id"),
        )
        events = self._events.setdefault(session_id, [])
        events.append(event)
        if len(events) > self._max_events_per_session:
            del events[: len(events) - self._max_events_per_session]
        if stream_key is not None:
            self._last_stream_publication[stream_key] = time.monotonic()
        self._fan_out(event)
        logger.debug("Event published session=%s seq=%d type=%s", session_id, seq, event_type)
        return event

    def _flush_pending_streams(self, session_id: str) -> None:
        """在非流式边界前发布该会话待发送的最新流式快照。"""
        pending_keys = [key for key in self._pending_streams if key[0] == session_id]
        for key in pending_keys:
            event_type, payload = self._pending_streams.pop(key)
            self._publish(session_id, event_type, payload, key)

    def _latest_event(self, session_id: str) -> Event:
        """返回最新已发布事件，供不改变游标的限速调用使用。"""
        events = self._events.get(session_id, [])
        if events:
            return events[-1]
        return self._publish(session_id, "stream_publication_started", {}, None)

    def _should_publish_stream(self, stream_key: tuple[str, str, str, str]) -> bool:
        """判断当前流式快照是否已达到下一次可发布时间。"""
        previous = self._last_stream_publication.get(stream_key)
        return previous is None or time.monotonic() - previous >= _STREAM_PUBLICATION_INTERVAL_S

    def _stream_key(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> tuple[str, str, str, str] | None:
        """为需要限速的流式事件构造稳定分段键。

        `transcript_patch` 用载荷里的 `stream_key`（= 条目 id）分段，
        其余流式事件沿用 `(frame_id, loop)`。
        """
        if event_type not in _STREAM_EVENT_TYPES:
            return None
        segment = str(payload.get("stream_key", "")) or str(payload.get("frame_id", ""))
        return (session_id, event_type, segment, str(payload.get("loop", "")))

    def _fan_out(self, event: Event) -> None:
        """无阻塞地向当前订阅者投递事件，并隔离慢客户端。"""
        for subscription in tuple(self._subscriptions.get(event.session_id, ())):
            if subscription.closed:
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.closed = True
                while not subscription.queue.empty():
                    subscription.queue.get_nowait()
                subscription.queue.put_nowait(
                    ResyncRequired(session_id=event.session_id, after_seq=subscription.acknowledged_seq)
                )

    @staticmethod
    def _optional_payload_id(payload: dict[str, Any], key: str) -> str | None:
        """读取有效的任务或轮次标识，缺失时保留空值。"""
        value = payload.get(key)
        return str(value) if value is not None and str(value) else None
