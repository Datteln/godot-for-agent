"""可恢复 WebSocket 传输使用的进程内事件日志。

实时正文补丁在发布时被转换为有界表示（完整/追加增量/受限预览，见
`app.transcript.realtime`），权威完整条目仍由展示稿与历史快照保留。

每个订阅维护独立的有界发送缓冲：

- 同时施加条数与序列化字节预算；
- 未发送的增长型流式补丁按条目键做 latest-wins 合并（增量可拼接合并，
  预览可被更新预览/增量后的完整补丁替换）；
- 工具、审批、错误与终态补丁保持顺序、绝不合并；
- 预算耗尽时订阅进入显式 `resync_required`，不静默丢序、不阻塞其他订阅，
  也不影响仍在运行的后端轮次。

所有诊断只记录计数、字节数与时序等脱敏数值，绝不包含正文。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.transcript.models import (
    LEGACY_ONLY_KINDS,
    PATCH_FORMAT_APPEND_DELTA,
    PATCH_FORMAT_FULL,
    PATCH_FORMAT_PREVIEW,
    STREAM_TEXT_FIELDS,
    TERMINAL_ENTRY_STATES,
)
from app.transcript.realtime import LastPublishedStream, build_realtime_patch

logger = logging.getLogger(__name__)

_MAX_EVENTS_PER_SESSION = 500
_STREAM_EVENT_TYPES = frozenset({"agent_text_delta", "agent_reasoning_delta", "transcript_patch"})
_STREAM_PUBLICATION_INTERVAL_S = 0.05
_DEFAULT_OUTBOUND_BYTE_BUDGET = 512 * 1024
_DEFAULT_STREAM_PREVIEW_MAX_CHARS = 800
## 终态非流式展示稿补丁的序列化字节预算（任务 5.2）：超预算的补丁不发送
## 原始载荷，替换为不含正文的安全摘要。流式条目（Thought/assistant）的终态
## 补丁属于流式表示契约，不适用该预算。
_DEFAULT_TERMINAL_PATCH_MAX_BYTES = 32 * 1024

REASON_ITEM_BUDGET = "outbound_item_budget"
REASON_BYTE_BUDGET = "outbound_byte_budget"
REASON_LEGACY_OVERFLOW = "outbound_queue_overflow"


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
    reason: str = REASON_LEGACY_OVERFLOW


@dataclass
class SubscriptionStats:
    """单个订阅的脱敏背压诊断；只含计数/字节/时序，绝不含正文。"""

    enqueued_items: int = 0
    enqueued_bytes: int = 0
    peak_items: int = 0
    peak_bytes: int = 0
    coalesced_patches: int = 0
    resync_count: int = 0
    last_resync_reason: str = ""

    def snapshot(self) -> dict[str, int | str]:
        """返回可写入日志/诊断接口的纯数值快照。"""
        return {
            "enqueued_items": self.enqueued_items,
            "enqueued_bytes": self.enqueued_bytes,
            "peak_items": self.peak_items,
            "peak_bytes": self.peak_bytes,
            "coalesced_patches": self.coalesced_patches,
            "resync_count": self.resync_count,
            "last_resync_reason": self.last_resync_reason,
        }


class OutboundBuffer:
    """单订阅的有界发送缓冲：条数/字节预算与条目键 latest-wins 合并。

    顺序由单调位置号表达；合并只替换同一条目的未发送流式补丁，位置不变，
    其余条目的相对顺序与终态位置完全保持。
    """

    def __init__(self, *, max_items: int, max_bytes: int, coalescing_enabled: bool) -> None:
        """初始化缓冲预算与合并开关。"""
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._coalescing_enabled = coalescing_enabled
        self._by_pos: dict[int, tuple[Event | ResyncRequired, int]] = {}
        self._signals: deque[int] = deque()
        self._wakeup: asyncio.Event = asyncio.Event()
        self._next_pos = 0
        self._stream_slots: dict[str, int] = {}
        self._item_count = 0
        self._byte_depth = 0
        self._resync_pending = False
        self.stats = SubscriptionStats()

    def offer(self, event: Event, size: int, stream_key: str | None, replaceable: bool) -> str | None:
        """投递一条事件；必要时先合并同条目旧补丁，再检查预算。

        Returns:
            越界时的重同步原因；未越界返回 None。
        """
        if self._resync_pending:
            return None
        if (
            self._coalescing_enabled
            and replaceable
            and stream_key is not None
            and self._try_coalesce(event, stream_key)
        ):
            return self._check_budget()
        self._append(event, size)
        if self._coalescing_enabled and replaceable and stream_key is not None:
            self._stream_slots[stream_key] = self._next_pos - 1
        elif stream_key is not None:
            # 同条目出现不可替换补丁（结构/终态）后，旧流式槽位不得再被合并。
            self._stream_slots.pop(stream_key, None)
        return self._check_budget()

    def offer_resync(self, reason: str, after_seq: int, session_id: str) -> None:
        """清空待发内容并转入显式重同步状态。"""
        self._by_pos.clear()
        self._stream_slots.clear()
        self._signals.clear()
        self._item_count = 0
        self._byte_depth = 0
        self._resync_pending = True
        self.stats.resync_count += 1
        self.stats.last_resync_reason = reason
        resync = ResyncRequired(session_id=session_id, after_seq=after_seq, reason=reason)
        self._append(resync, 0)
        logger.warning(
            "Subscriber entered resync session=%s reason=%s %s",
            session_id,
            reason,
            self.stats.snapshot(),
        )

    async def get(self) -> Event | ResyncRequired:
        """等待并取出下一条待发内容；跳过已被合并消耗的位置。"""
        while True:
            while not self._signals:
                self._wakeup.clear()
                await self._wakeup.wait()
            pos = self._signals.popleft()
            item = self._take(pos)
            if item is not None:
                return item

    def get_nowait(self) -> Event | ResyncRequired:
        """非阻塞取出下一条待发内容；空缓冲抛 `asyncio.QueueEmpty`。"""
        while self._signals:
            pos = self._signals.popleft()
            item = self._take(pos)
            if item is not None:
                return item
        raise asyncio.QueueEmpty

    def empty(self) -> bool:
        """是否没有待发内容。"""
        return self._item_count == 0

    def depth(self) -> tuple[int, int]:
        """返回当前待发条数与字节深度。"""
        return self._item_count, self._byte_depth

    def _append(self, item: Event | ResyncRequired, size: int) -> None:
        """把内容登记到新位置并唤醒等待者。"""
        pos = self._next_pos
        self._next_pos += 1
        self._by_pos[pos] = (item, size)
        self._signals.append(pos)
        self._item_count += 1
        self._byte_depth += size
        self.stats.enqueued_items += 1
        self.stats.enqueued_bytes += size
        self.stats.peak_items = max(self.stats.peak_items, self._item_count)
        self.stats.peak_bytes = max(self.stats.peak_bytes, self._byte_depth)
        self._wakeup.set()

    def _take(self, pos: int) -> Event | ResyncRequired | None:
        """取出一个位置的内容并同步簿记；缺失位置返回 None。"""
        entry = self._by_pos.pop(pos, None)
        if entry is None:
            return None
        item, size = entry
        self._item_count -= 1
        self._byte_depth -= size
        slot_key = next((k for k, v in self._stream_slots.items() if v == pos), None)
        if slot_key is not None:
            self._stream_slots.pop(slot_key, None)
        return item

    def _try_coalesce(self, event: Event, stream_key: str) -> bool:
        """尝试把新流式补丁合并进同条目未发送的旧补丁；成功返回 True。"""
        pos = self._stream_slots.get(stream_key)
        if pos is None or pos not in self._by_pos:
            return False
        old_event, old_size = self._by_pos[pos]
        if not isinstance(old_event, Event) or not isinstance(event, Event):
            return False
        merged_payload = _merge_stream_payload(old_event.payload, event.payload)
        if merged_payload is None:
            return False
        merged = Event(
            seq=old_event.seq,
            session_id=old_event.session_id,
            type=old_event.type,
            payload=merged_payload,
            task_id=old_event.task_id,
            turn_id=old_event.turn_id,
        )
        merged_size = len(json.dumps(merged.to_wire(), ensure_ascii=False).encode("utf-8"))
        self._by_pos[pos] = (merged, merged_size)
        self._byte_depth += merged_size - old_size
        self.stats.coalesced_patches += 1
        return True

    def _check_budget(self) -> str | None:
        """超出条数或字节预算时返回重同步原因；未越界返回 None。

        兼容模式（未启用合并）只施加条数预算并使用既有溢出原因，
        与改造前的纯条数队列行为保持一致。
        """
        if self._resync_pending:
            return None
        if self._item_count > self._max_items:
            return REASON_ITEM_BUDGET if self._coalescing_enabled else REASON_LEGACY_OVERFLOW
        if self._coalescing_enabled and self._byte_depth > self._max_bytes:
            return REASON_BYTE_BUDGET
        return None


def _merge_stream_payload(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    """合并两条同条目未发送的流式补丁；不可合并时返回 None。

    合并结果保留旧补丁的事件身份（序号连续不被打破），载荷取新修订：

    - 增量+增量：拼接 `append_text`，`base_revision` 保留旧值；
    - 增量/预览+预览：预览自含最新显示近似，直接替换；
    - 预览+增量：客户端无法凭预览重建精确正文，不可合并。
    """
    old_format = str(old.get("patch_format", ""))
    new_format = str(new.get("patch_format", ""))
    if old_format == PATCH_FORMAT_APPEND_DELTA and new_format == PATCH_FORMAT_APPEND_DELTA:
        if int(new.get("base_revision", -1)) != int(old.get("revision", -2)):
            return None
        merged = copy.deepcopy(new)
        merged["base_revision"] = int(old.get("base_revision", 0))
        merged["append_text"] = str(old.get("append_text", "")) + str(new.get("append_text", ""))
        return merged
    if old_format in {PATCH_FORMAT_APPEND_DELTA, PATCH_FORMAT_PREVIEW} and new_format == PATCH_FORMAT_PREVIEW:
        return copy.deepcopy(new)
    return None


@dataclass(eq=False)
class EventSubscription:
    """表示单个会话的有界实时事件订阅。"""

    session_id: str
    queue: OutboundBuffer
    acknowledged_seq: int = 0
    closed: bool = False

    def acknowledge(self, seq: int) -> None:
        """记录客户端已连续接受的最大序号。"""
        self.acknowledged_seq = max(self.acknowledged_seq, seq)

    def close(self) -> None:
        """标记订阅为已关闭，禁止后续投递。"""
        self.closed = True

    def diagnostics(self) -> dict[str, int | str]:
        """返回该订阅的脱敏背压诊断快照。"""
        items, bytes_depth = self.queue.depth()
        snapshot = self.queue.stats.snapshot()
        snapshot["pending_items"] = items
        snapshot["pending_bytes"] = bytes_depth
        snapshot["acknowledged_seq"] = self.acknowledged_seq
        return snapshot


class EventStore:
    """维护有界、不可变且可供实时订阅的会话事件序列。"""

    def __init__(
        self,
        *,
        max_events_per_session: int = _MAX_EVENTS_PER_SESSION,
        outbound_queue_size: int = 128,
        max_outbound_bytes: int = _DEFAULT_OUTBOUND_BYTE_BUDGET,
        coalescing_enabled: bool = True,
        bounded_stream_payloads: bool = True,
        stream_preview_max_chars: int = _DEFAULT_STREAM_PREVIEW_MAX_CHARS,
        terminal_patch_max_bytes: int = _DEFAULT_TERMINAL_PATCH_MAX_BYTES,
    ) -> None:
        """初始化事件保留区、订阅注册表和流式发布限速器。

        Args:
            max_events_per_session: 每会话保留的事件条数上限。
            outbound_queue_size: 单订阅待发条数预算。
            max_outbound_bytes: 单订阅待发序列化字节预算。
            coalescing_enabled: 是否启用条目键 latest-wins 合并与字节预算。
            bounded_stream_payloads: 是否把增长型正文补丁转换为增量/预览表示。
            stream_preview_max_chars: 受限预览的最大字符数。
            terminal_patch_max_bytes: 终态非流式展示稿补丁的序列化字节预算
                （任务 5.2）；超预算补丁替换为无正文安全摘要后再发布。
        """
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._subscriptions: dict[str, set[EventSubscription]] = {}
        self._max_events_per_session = max_events_per_session
        self._outbound_queue_size = outbound_queue_size
        self._max_outbound_bytes = max_outbound_bytes
        self._coalescing_enabled = coalescing_enabled
        self._bounded_stream_payloads = bounded_stream_payloads
        self._stream_preview_max_chars = stream_preview_max_chars
        self._terminal_patch_max_bytes = terminal_patch_max_bytes
        self._last_stream_publication: dict[tuple[str, str, str, str], float] = {}
        self._pending_streams: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
        self._last_realtime: dict[tuple[str, str, str, str], LastPublishedStream] = {}
        self._wire_sizes: dict[str, int] = {}
        # 可见进度水位（任务 1.2）：只有用户可见条目的展示稿补丁才会推进。
        self._visible_seq: dict[str, int] = {}
        self._visible_updated_at: dict[str, float] = {}
        self._diag_stream_events = 0
        self._diag_stream_bytes = 0
        self._diag_terminal_over_budget = 0

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
        if seq_floor > self._visible_seq.get(session_id, 0):
            # 重启后把可见水位抬到持久化游标：客户端水合后
            # upto_event_seq == session.event_seq，避免误判停滞。
            self._visible_seq[session_id] = seq_floor
            self._visible_updated_at[session_id] = time.time()

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
            queue=OutboundBuffer(
                max_items=self._outbound_queue_size,
                max_bytes=self._max_outbound_bytes,
                coalescing_enabled=self._coalescing_enabled,
            ),
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

    def visible_progress(self, session_id: str) -> tuple[int, float]:
        """返回会话最近一次已发布可见条目的序号与墙钟时间戳。

        进度字段只含标识符/计数/时间戳，绝不含正文；客户端据此在
        活跃请求中检测可见转录停滞（任务 1.2）。

        Args:
            session_id: 会话 id。

        Returns:
            ``(visible_seq, visible_updated_at)``；无可见事件时为 ``(0, 0.0)``。
        """
        return self._visible_seq.get(session_id, 0), self._visible_updated_at.get(session_id, 0.0)

    def diagnostics(self) -> dict[str, int]:
        """返回存储级脱敏发布诊断（只含计数与字节数）。"""
        return {
            "stream_events_published": self._diag_stream_events,
            "stream_payload_bytes": self._diag_stream_bytes,
            "terminal_patches_over_budget": self._diag_terminal_over_budget,
            "active_subscriptions": sum(len(subs) for subs in self._subscriptions.values()),
        }

    def _publish(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        stream_key: tuple[str, str, str, str] | None,
    ) -> Event:
        """为一条已通过限速的事件分配序号并扇出。"""
        wire_payload = payload
        if event_type == "transcript_patch" and self._bounded_stream_payloads:
            wire_payload = self._bounded_realtime_payload(stream_key, payload)
        if event_type == "transcript_patch":
            # 终态非流式补丁的入站字节预算（任务 5.2）：在分配序号与扇出前
            # 衡量序列化字节，超预算替换为无正文安全摘要。
            wire_payload = self._bounded_terminal_patch(wire_payload)
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        immutable_payload = copy.deepcopy(wire_payload)
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
            self._diag_stream_events += 1
            self._diag_stream_bytes += self._wire_size(event)
        if self._is_visible_transcript_event(event_type, wire_payload):
            self._visible_seq[session_id] = seq
            self._visible_updated_at[session_id] = time.time()
        self._fan_out(event)
        logger.debug("Event published session=%s seq=%d type=%s", session_id, seq, event_type)
        return event

    def _bounded_realtime_payload(
        self, stream_key: tuple[str, str, str, str] | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """为展示稿补丁选择有界实时表示并维护该条目的发布状态。"""
        if stream_key is None:
            return payload
        last = self._last_realtime.get(stream_key)
        wire_payload, new_state = build_realtime_patch(
            payload, last, preview_max_chars=self._stream_preview_max_chars
        )
        entry = payload.get("entry")
        terminal = (
            isinstance(entry, dict)
            and str(entry.get("state", "")) in TERMINAL_ENTRY_STATES.get(str(entry.get("kind", "")), frozenset())
        )
        if terminal:
            self._last_realtime.pop(stream_key, None)
        else:
            self._last_realtime[stream_key] = new_state
        return wire_payload

    def _bounded_terminal_patch(self, wire_payload: dict[str, Any]) -> dict[str, Any]:
        """对终态非流式展示稿补丁施加序列化字节预算（任务 5.2）。

        正常路径发布的工具摘要已受工具语义约束；若兼容数据或未知工具仍使
        终态补丁超出预算，绝不发送原始载荷——替换为只含工具名、状态、计数
        与超限标志的无正文安全摘要，客户端随后仍可通过权威快照取得已净化
        的完整条目。流式条目（Thought/assistant）的终态补丁属于流式表示
        契约，不适用本预算。

        Args:
            wire_payload: 已选择实时表示的展示稿补丁载荷。

        Returns:
            预算内的原载荷，或替换后的安全摘要载荷。
        """
        entry = wire_payload.get("entry")
        if not isinstance(entry, dict):
            return wire_payload
        kind = str(entry.get("kind", ""))
        state = str(entry.get("state", ""))
        if state not in TERMINAL_ENTRY_STATES.get(kind, frozenset()):
            return wire_payload
        if kind in STREAM_TEXT_FIELDS:
            # Thought/assistant 的终态完整补丁由流式表示契约约束，不在此裁剪。
            return wire_payload
        if str(wire_payload.get("patch_format", PATCH_FORMAT_FULL)) != PATCH_FORMAT_FULL:
            return wire_payload
        serialized = len(json.dumps(wire_payload, ensure_ascii=False).encode("utf-8"))
        if serialized <= self._terminal_patch_max_bytes:
            return wire_payload

        self._diag_terminal_over_budget += 1
        source_payload = entry.get("payload")
        source_payload = source_payload if isinstance(source_payload, dict) else {}
        safe_entry_payload: dict[str, Any] = {
            "oversized": True,
            "reason": "terminal_patch_byte_budget",
            "original_bytes": serialized,
        }
        for key in ("tool", "agent", "is_error", "outcome_status", "result_count", "render_kind"):
            if key in source_payload:
                safe_entry_payload[key] = source_payload[key]
        safe_entry = {
            "entry_id": entry.get("entry_id"),
            "ordinal": entry.get("ordinal"),
            "kind": kind,
            "state": state,
            "revision": entry.get("revision"),
            "turn_id": entry.get("turn_id"),
            "tool_call_id": entry.get("tool_call_id"),
            "payload": safe_entry_payload,
        }
        logger.warning(
            "Terminal transcript patch exceeded byte budget; emitting safe summary "
            "session_kind=%s state=%s original_bytes=%d budget=%d",
            kind,
            state,
            serialized,
            self._terminal_patch_max_bytes,
        )
        return {
            "entry": safe_entry,
            "stream_key": wire_payload.get("stream_key"),
            "patch_format": PATCH_FORMAT_FULL,
            "patch_version": wire_payload.get("patch_version"),
        }

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
        """无阻塞地向当前订阅者投递事件，并按条目键合并未发送的流式补丁。"""
        stream_key, replaceable = self._coalesce_info(event)
        size = self._wire_size(event)
        for subscription in tuple(self._subscriptions.get(event.session_id, ())):
            if subscription.closed:
                continue
            reason = subscription.queue.offer(event, size, stream_key, replaceable)
            if reason is not None:
                subscription.queue.offer_resync(
                    reason, subscription.acknowledged_seq, event.session_id
                )

    def _coalesce_info(self, event: Event) -> tuple[str | None, bool]:
        """提取事件的条目合并键与是否可被同条目新补丁替换。

        只有增长型流式表示（追加增量/受限预览）可替换；完整补丁、工具、
        审批、错误与终态一律保持顺序。
        """
        if event.type != "transcript_patch":
            return None, False
        payload = event.payload
        key = str(payload.get("stream_key", ""))
        if not key:
            return None, False
        patch_format = str(payload.get("patch_format", ""))
        return key, patch_format in {PATCH_FORMAT_APPEND_DELTA, PATCH_FORMAT_PREVIEW}

    def _wire_size(self, event: Event) -> int:
        """计算并缓存事件包络的序列化字节数（脱敏诊断用）。"""
        cached = self._wire_sizes.get(event.event_id)
        if cached is None:
            cached = len(json.dumps(event.to_wire(), ensure_ascii=False).encode("utf-8"))
            self._wire_sizes[event.event_id] = cached
            if len(self._wire_sizes) > 4096:
                self._wire_sizes.clear()
        return cached

    @staticmethod
    def _is_visible_transcript_event(event_type: str, payload: dict[str, Any]) -> bool:
        """判断事件是否为用户可见条目的展示稿补丁。

        完整补丁从 ``entry`` 读取 kind；``append_delta``/``preview`` 表示在
        载荷顶层携带 kind，缺失时保守视为可见（增长型流只有 thought/assistant）。

        Args:
            event_type: 事件类型。
            payload: 已转换为线上表示的载荷。

        Returns:
            True 表示该事件应推进会话的可见进度水位。
        """
        if event_type != "transcript_patch":
            return False
        entry = payload.get("entry")
        kind = str(entry.get("kind", "")) if isinstance(entry, dict) else str(payload.get("kind", ""))
        return kind == "" or kind not in LEGACY_ONLY_KINDS

    @staticmethod
    def _optional_payload_id(payload: dict[str, Any], key: str) -> str | None:
        """读取有效的任务或轮次标识，缺失时保留空值。"""
        value = payload.get(key)
        return str(value) if value is not None and str(value) else None
