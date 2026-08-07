"""显式的单次提交发布作用域。

Replaces the global ``_PUBLICATION_BUFFER: ContextVar`` with a typed
explicit scope that is passed through the submission boundary.  Only the
owning use case may commit or resolve the scope.

The scope carries:
- The working Session reference
- Request and turn identities
- Staged map artifact turn
- Preview lifecycle state
- A buffered event publisher

Subordinate services receive the scope explicitly; they never discover it
through a module global.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.api.schemas import ChatFinalResponse, ChatResponse, ChatToolCallsResponse
from app.config import AppSettings
from app.events.store import EventStore
from app.query.helpers import _PERSISTED_HISTORY_EVENT_TYPES
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import RecoverySupervisor
from app.sessions.store import Session, SessionStore

logger = logging.getLogger(__name__)

_MAP_AUTO_COMPACT_CONTEXT_TOKENS = 64_000
_MODEL_LOG_FIELDS = frozenset({"model", "primary_model", "fallback_model"})
_PREVIEW_EVENT_TYPES = frozenset({"agent_text_delta", "agent_reasoning_delta"})

# Re-export the buffer event type for type-safe usage.
BufferedEvent = tuple[str, str, dict[str, Any]]


@dataclass(slots=True)
class PreviewLifecycle:
    """保存一次提交已经对外可见的 provisional preview。"""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: bool = False
    event_count: int = 0
    first_event_seq: int = 0

    def add(self, key: str, payload: dict[str, Any]) -> None:
        self.items.setdefault(key, payload)

    def mark_resolved(self) -> None:
        self.resolved = True


@dataclass(slots=True)
class SubmissionScope:
    """One typed per-submission scope carrying all transaction state.

    Passed explicitly to subordinate services.  Only the owning use case
    may commit or discard this scope.
    """

    session: Any  # Session — kept as Any to avoid circular import
    request_id: str | None
    turn_id: str
    map_artifact_turn: Any  # StagedMapArtifactTurn
    events: list[BufferedEvent] = field(default_factory=list)
    preview: PreviewLifecycle = field(default_factory=PreviewLifecycle)
    resolved: bool = False

    def buffer_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """暂存一个事务事件；该操作不会发布事件。"""
        if self.resolved:
            raise RuntimeError("submission scope is already resolved")
        self.events.append((session_id, event_type, payload))

    def buffered_events(self) -> list[BufferedEvent]:
        """返回暂存事件的浅副本。"""
        return list(self.events)

    def discard(self) -> None:
        """丢弃尚未发布的事务事件。"""
        if self.resolved:
            return
        self.events.clear()
        self.resolved = True


def _submission_event_delivery(event_type: str) -> str:
    """返回提交事件的发布时机。"""
    if event_type == "turn_progress":
        return "out_of_band_liveness"
    if event_type in _PREVIEW_EVENT_TYPES:
        return "provisional_preview"
    return "transactional"


def _event_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """把日志里的模型标识替换为统一脱敏标记，不改变实际事件。

    Args:
        payload: 原始事件负载。

    Returns:
        仅用于日志的副本，其中模型字段值被替换为 ``<redacted>``。
    """
    return {
        key: ("<redacted>" if key in _MODEL_LOG_FIELDS else value)
        for key, value in payload.items()
    }


@dataclass(frozen=True, slots=True)
class SubmissionPublisher:
    """发布提交事件并维护 provisional preview 与恢复指针。"""

    settings: AppSettings
    store: SessionStore
    events: EventStore | None
    recovery: RecoveryPointerStore | None
    recovery_supervisor: RecoverySupervisor
    available_tools: Callable[[], set[str]]

    def emit(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        scope: SubmissionScope | None = None,
    ) -> int:
        """发布普通事件，或在显式提交作用域内暂存事务事件。"""
        if scope is not None:
            return self._emit_scoped(session_id, event_type, payload, scope)
        logger.debug(
            "Event emitted session=%s type=%s payload=%s",
            session_id,
            event_type,
            json.dumps(_event_payload_for_log(payload), ensure_ascii=False, default=str),
        )
        session: Session | None = None
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            session = self.store.get_or_create(session_id, self.available_tools())
            self.record_history_event(session, event_type, payload)
        if self.events is None:
            return 0
        epoch = (
            session.session_epoch
            if session is not None
            else self.store.current_epoch(session_id, create=False)
        )
        event = self.events.append(
            session_id,
            event_type,
            payload,
            session_epoch=epoch,
        )
        return event.seq

    def _emit_scoped(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        scope: SubmissionScope,
    ) -> int:
        staged_payload = dict(payload)
        staged_payload.setdefault("_submission_request_id", scope.request_id)
        staged_payload.setdefault("_submission_turn_id", scope.turn_id)
        staged_payload.setdefault("request_id", scope.request_id)
        staged_payload.setdefault("turn_id", scope.turn_id)
        delivery = _submission_event_delivery(event_type)
        if delivery == "out_of_band_liveness":
            if self.events is None:
                return 0
            return self.events.append(
                session_id,
                event_type,
                staged_payload,
                session_epoch=scope.session.session_epoch,
            ).seq
        if delivery == "transactional":
            staged_payload.setdefault("delivery", delivery)
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            self.record_history_event(scope.session, event_type, staged_payload)
        if delivery != "provisional_preview":
            scope.buffer_event(session_id, event_type, staged_payload)
            return 0
        return self._publish_preview(session_id, event_type, staged_payload, scope)

    def _publish_preview(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        scope: SubmissionScope,
    ) -> int:
        frame_id = str(payload.get("frame_id") or "")
        message_index = str(payload.get("message_index") or "")
        message_id = str(payload.get("message_id") or f"{frame_id}:{message_index}")
        preview_id = str(
            payload.get("preview_id")
            or (
                f"{scope.request_id or scope.turn_id}:"
                f"{scope.turn_id}:{event_type}:{message_id}"
            )
        )
        payload.update(
            {
                "delivery": "provisional_preview",
                "provisional": True,
                "preview_id": preview_id,
                "message_id": message_id,
            }
        )
        scope.preview.add(
            preview_id,
            {
                "preview_id": preview_id,
                "event_type": event_type,
                "frame_id": frame_id,
                "message_id": message_id,
            },
        )
        if self.events is None:
            return 0
        event = self.events.append(
            session_id,
            event_type,
            payload,
            session_epoch=scope.session.session_epoch,
        )
        scope.preview.event_count += 1
        if scope.preview.first_event_seq == 0:
            scope.preview.first_event_seq = event.seq
            logger.info(
                "First submission preview published session=%s request_id=%s "
                "turn_id=%s preview_id=%s seq=%d provider_first_chunk=%s",
                session_id,
                scope.request_id,
                scope.turn_id,
                preview_id,
                event.seq,
                bool(payload.get("provider_first_chunk", False)),
            )
        return event.seq

    def resolve_previews(
        self,
        scope: SubmissionScope,
        *,
        committed: bool,
        reason: str | None = None,
    ) -> None:
        """恰好一次地提交或丢弃已经可见的 preview。"""
        if scope.preview.resolved or not scope.preview.items:
            return
        scope.preview.mark_resolved()
        event_type = "submission_preview_committed" if committed else "submission_preview_discarded"
        payload: dict[str, Any] = {
            "delivery": "provisional_preview",
            "provisional": False,
            "request_id": scope.request_id,
            "turn_id": scope.turn_id,
            "preview_ids": list(scope.preview.items),
            "previews": list(scope.preview.items.values()),
        }
        if reason is not None:
            payload["reason"] = reason
        seq = 0
        if self.events is not None:
            seq = self.events.append(
                scope.session.session_id,
                event_type,
                payload,
                session_epoch=scope.session.session_epoch,
            ).seq
        logger.info(
            "Submission previews resolved session=%s request_id=%s turn_id=%s "
            "resolution=%s preview_streams=%d preview_events=%d "
            "first_preview_seq=%d boundary_seq=%d reason=%s",
            scope.session.session_id,
            scope.request_id,
            scope.turn_id,
            "committed" if committed else "discarded",
            len(scope.preview.items),
            scope.preview.event_count,
            scope.preview.first_event_seq,
            seq,
            reason,
        )

    def record_history_event(
        self,
        session: Session,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """把需恢复的事件写入 Session，不触碰 EventStore。"""
        if event_type == "context_usage":
            try:
                used_tokens = int(payload.get("used_tokens", 0))
            except (TypeError, ValueError):
                used_tokens = 0
            if used_tokens > 0:
                session.latest_context_used_tokens = used_tokens
                frame = session.top_frame()
                is_map_frame = frame is not None and (
                    frame.agent.pipeline_kind == "map"
                    or bool(frame.agent.workflow_operations)
                )
                threshold = (
                    min(
                        self.settings.auto_compact_token_threshold,
                        _MAP_AUTO_COMPACT_CONTEXT_TOKENS,
                    )
                    if is_map_frame
                    else self.settings.auto_compact_token_threshold
                )
                if used_tokens >= threshold:
                    session.force_compact_next_turn = True
        session.record_history_event(event_type, payload)

    def flush(self, scope: SubmissionScope) -> None:
        """Session 提交后按原顺序发布暂存的事务事件。

        发布前失败的事件会保留在作用域内供后续重试；发布后失败的事件
        依靠 ``_delivery_id`` 在事件存储中幂等去重，不会重复提交。

        Args:
            scope: 当前提交作用域，携带暂存事务事件。
        """
        if self.events is None:
            scope.resolved = True
            return
        undelivered: list[BufferedEvent] = []
        for event_index, (session_id, event_type, payload) in enumerate(scope.events):
            payload.setdefault(
                "_delivery_id",
                hashlib.sha256(
                    (
                        f"{session_id}\0{scope.session.session_epoch}\0"
                        f"{scope.request_id or ''}\0{scope.turn_id}\0"
                        f"{event_index}\0{event_type}"
                    ).encode()
                ).hexdigest(),
            )
            published = False
            try:
                self.recovery_supervisor.hit_failpoint("event_delivery_before_publish")
                event = self.events.append(
                    session_id,
                    event_type,
                    payload,
                    session_epoch=scope.session.session_epoch,
                )
                published = True
                self.recovery_supervisor.hit_failpoint("event_delivery_after_publish")
            except (OSError, ValueError) as exc:
                self.recovery_supervisor.record_transport_loss(scope.session, transport="event")
                try:
                    self.store.save_task_run(scope.session)
                except (OSError, TypeError, ValueError):
                    logger.exception(
                        "Failed to persist event delivery transport state session=%s",
                        session_id,
                    )
                logger.error(
                    "Committed session event publication failed session=%s type=%s error=%s",
                    session_id,
                    event_type,
                    exc,
                )
                if not published:
                    undelivered.append((session_id, event_type, payload))
                continue
            logger.debug(
                "Deferred event persisted session=%s seq=%d type=%s",
                session_id,
                event.seq,
                event_type,
            )
        scope.events[:] = undelivered
        scope.resolved = not undelivered

    def record_recovery(self, session: Session, response: ChatResponse) -> None:
        """根据最新响应写入或清理最小恢复指针。"""
        if self.recovery is None:
            return
        last_seq = self.events.last_seq(session.session_id) if self.events is not None else 0
        if session.map_task_state.status == "paused":
            self.recovery.write(
                session.session_id,
                session.pending_turn_id,
                last_seq,
                session.map_task_state.checkpoint,
                session_epoch=session.session_epoch,
            )
        elif isinstance(response, ChatToolCallsResponse):
            self.recovery.write(
                session_id=session.session_id,
                pending_turn_id=response.turn_id,
                last_event_seq=last_seq,
                session_epoch=session.session_epoch,
            )
        elif isinstance(response, ChatFinalResponse):
            self.recovery.clear(session.session_id)
