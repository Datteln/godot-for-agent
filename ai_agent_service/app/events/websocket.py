"""鉴权、可恢复且带背压的 WebSocket 事件传输。"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.config import AppSettings
from app.events.store import Event, EventStore
from app.events.websocket_protocol import (
    AckMessage,
    CloseMessage,
    EpochChangedMessage,
    EventBatchMessage,
    HelloMessage,
    PingMessage,
    PongMessage,
    ResumeMessage,
    SnapshotRequiredMessage,
    SocketEvent,
    parse_client_message,
)
from app.sessions.store import SessionStore


@dataclass(frozen=True)
class _InflightEvent:
    """一个已发送但尚未累计确认的事件及编码体积。"""

    seq: int
    encoded_bytes: int


def _authorized(websocket: WebSocket, expected_token: str | None) -> bool:
    """验证握手 Authorization header，token 永不进入 URL。"""
    if not expected_token:
        return False
    scheme, _, supplied = websocket.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(supplied, expected_token)


def _socket_event(event: Event) -> SocketEvent:
    """把内部事件投影为当前 WebSocket 协议事件。"""
    payload = event.payload
    return SocketEvent(
        seq=event.seq,
        session_id=event.session_id,
        session_epoch=event.session_epoch,
        type=event.type,
        payload=payload,
        delivery=payload.get("delivery"),
        provisional=bool(payload.get("provisional", False)),
        preview_id=payload.get("preview_id"),
        request_id=payload.get("request_id"),
        turn_id=payload.get("turn_id"),
        frame_id=payload.get("frame_id"),
        message_id=payload.get("message_id"),
    )


async def _send_model(websocket: WebSocket, message: Any) -> int:
    """发送协议模型并返回紧凑 JSON 的 UTF-8 字节数。"""
    payload = message.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await websocket.send_text(encoded.decode("utf-8"))
    return len(encoded)


async def _typed_close(
    websocket: WebSocket,
    *,
    code: str,
    retryable: bool,
    resume_after_seq: int,
    websocket_code: int,
) -> None:
    """尽力发送结构化关闭原因，然后关闭 socket。"""
    await _send_model(
        websocket,
        CloseMessage(
            code=code,
            retryable=retryable,
            resume_after_seq=resume_after_seq,
        ),
    )
    await websocket.close(code=websocket_code)


def _bounded_batch(
    events: list[Event],
    *,
    event_limit: int,
    byte_limit: int,
) -> tuple[list[SocketEvent], list[int]]:
    """按事件数与编码字节双重上限构造批次，绝不发送超限单事件。"""
    accepted: list[SocketEvent] = []
    sizes: list[int] = []
    for event in events[:event_limit]:
        projected = _socket_event(event)
        size = len(
            json.dumps(
                projected.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if sum(sizes) + size > byte_limit:
            break
        accepted.append(projected)
        sizes.append(size)
    return accepted, sizes


async def serve_event_websocket(
    websocket: WebSocket,
    *,
    expected_token: str | None,
    settings: AppSettings,
    event_store: EventStore,
    session_store: SessionStore,
) -> None:
    """运行一个绑定单 Session epoch 的 WebSocket 事件会话。"""
    if not _authorized(websocket, expected_token):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    try:
        raw_resume = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        resume = parse_client_message(raw_resume)
    except (TimeoutError, ValidationError, ValueError, WebSocketDisconnect):
        await _typed_close(
            websocket,
            code="invalid_message",
            retryable=False,
            resume_after_seq=0,
            websocket_code=4400,
        )
        return
    if not isinstance(resume, ResumeMessage):
        await _typed_close(
            websocket,
            code="invalid_message",
            retryable=False,
            resume_after_seq=0,
            websocket_code=4400,
        )
        return

    current_epoch = session_store.current_epoch(resume.session_id, create=False)
    if not current_epoch or resume.session_epoch != current_epoch:
        await _send_model(
            websocket,
            SnapshotRequiredMessage(
                reason="stale_epoch" if current_epoch else "invalid_epoch",
                session_epoch=current_epoch or "missing",
                high_water_seq=event_store.last_seq(resume.session_id),
            ),
        )
        await websocket.close(code=4409)
        return
    persisted_cursor = session_store.persisted_event_cursor(
        resume.session_id,
        current_epoch,
    )
    event_store.ensure_sequence(
        resume.session_id,
        persisted_cursor,
        session_epoch=current_epoch,
    )
    oldest = event_store.oldest_seq(resume.session_id)
    high_water = event_store.last_seq(resume.session_id)
    if resume.after_seq > high_water:
        await _send_model(
            websocket,
            SnapshotRequiredMessage(
                reason="sequence_gap",
                session_epoch=current_epoch,
                high_water_seq=high_water,
            ),
        )
        await websocket.close(code=4409)
        return
    cursor_expired = (
        oldest is not None and resume.after_seq < oldest - 1
    ) or (
        oldest is None and resume.after_seq < high_water
    )
    if cursor_expired:
        await _send_model(
            websocket,
            SnapshotRequiredMessage(
                reason="cursor_expired",
                session_epoch=current_epoch,
                high_water_seq=high_water,
            ),
        )
        await websocket.close(code=4409)
        return

    accepted_seq = resume.after_seq
    sent_seq = resume.after_seq
    await _send_model(
        websocket,
        HelloMessage(
            session_epoch=current_epoch,
            high_water_seq=high_water,
            accepted_seq=accepted_seq,
            resume_disposition=(
                "current"
                if accepted_seq == event_store.last_seq(resume.session_id)
                else "resumed"
            ),
            batch_event_limit=settings.websocket_batch_event_limit,
            batch_byte_limit=settings.websocket_batch_byte_limit,
            unacked_event_limit=settings.websocket_unacked_event_limit,
            unacked_byte_limit=settings.websocket_unacked_byte_limit,
            heartbeat_interval_s=settings.websocket_heartbeat_interval_s,
        ),
    )
    inflight: deque[_InflightEvent] = deque()
    unacked_bytes = 0
    last_ack_at = time.monotonic()
    last_ping_at = time.monotonic()
    pending_nonce = ""

    try:
        while True:
            observed_epoch = event_store.current_epoch(resume.session_id) or current_epoch
            if observed_epoch != current_epoch:
                await _send_model(
                    websocket,
                    EpochChangedMessage(
                        previous_epoch=current_epoch,
                        new_epoch=observed_epoch,
                        last_event_seq=event_store.last_seq(resume.session_id),
                    ),
                )
                await websocket.close(code=4410)
                return

            unacked_events = sent_seq - accepted_seq
            can_send = (
                unacked_events < settings.websocket_unacked_event_limit
                and unacked_bytes < settings.websocket_unacked_byte_limit
            )
            candidates = event_store.list_after(
                resume.session_id,
                sent_seq,
                session_epoch=current_epoch,
            )
            if can_send and candidates:
                remaining_events = settings.websocket_unacked_event_limit - unacked_events
                remaining_bytes = settings.websocket_unacked_byte_limit - unacked_bytes
                batch, sizes = _bounded_batch(
                    candidates,
                    event_limit=min(settings.websocket_batch_event_limit, remaining_events),
                    byte_limit=min(settings.websocket_batch_byte_limit, remaining_bytes),
                )
                if batch:
                    encoded_bytes = sum(sizes)
                    await _send_model(
                        websocket,
                        EventBatchMessage(
                            session_epoch=current_epoch,
                            first_seq=batch[0].seq,
                            last_seq=batch[-1].seq,
                            events=batch,
                            encoded_bytes=encoded_bytes,
                        ),
                    )
                    for item, size in zip(batch, sizes, strict=True):
                        inflight.append(_InflightEvent(item.seq, size))
                    unacked_bytes += encoded_bytes
                    sent_seq = batch[-1].seq
                    continue
                await _typed_close(
                    websocket,
                    code="event_too_large",
                    retryable=False,
                    resume_after_seq=accepted_seq,
                    websocket_code=4409,
                )
                return

            now = time.monotonic()
            if inflight and now - last_ack_at >= settings.websocket_stall_timeout_s:
                await _typed_close(
                    websocket,
                    code="client_stalled",
                    retryable=True,
                    resume_after_seq=accepted_seq,
                    websocket_code=4408,
                )
                return
            if now - last_ping_at >= settings.websocket_heartbeat_interval_s:
                pending_nonce = secrets.token_urlsafe(12)
                await _send_model(websocket, PingMessage(nonce=pending_nonce))
                last_ping_at = now

            receive_task = asyncio.create_task(websocket.receive_json())
            event_task = asyncio.create_task(
                event_store.wait_for_change(
                    resume.session_id,
                    sent_seq,
                    timeout_s=settings.websocket_heartbeat_interval_s,
                )
            )
            done, pending = await asyncio.wait(
                {receive_task, event_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if receive_task not in done:
                continue
            try:
                client_message = parse_client_message(receive_task.result())
            except (ValidationError, ValueError):
                await _typed_close(
                    websocket,
                    code="invalid_message",
                    retryable=False,
                    resume_after_seq=accepted_seq,
                    websocket_code=4400,
                )
                return
            if isinstance(client_message, AckMessage):
                if (
                    client_message.session_epoch != current_epoch
                    or client_message.accepted_seq < accepted_seq
                    or client_message.accepted_seq > sent_seq
                ):
                    await _typed_close(
                        websocket,
                        code="ack_out_of_range",
                        retryable=False,
                        resume_after_seq=accepted_seq,
                        websocket_code=4400,
                    )
                    return
                accepted_seq = client_message.accepted_seq
                while inflight and inflight[0].seq <= accepted_seq:
                    unacked_bytes -= inflight.popleft().encoded_bytes
                last_ack_at = time.monotonic()
            elif isinstance(client_message, PongMessage):
                if client_message.nonce == pending_nonce:
                    pending_nonce = ""
            else:
                await _typed_close(
                    websocket,
                    code="invalid_message",
                    retryable=False,
                    resume_after_seq=accepted_seq,
                    websocket_code=4400,
                )
                return
    except WebSocketDisconnect:
        return
