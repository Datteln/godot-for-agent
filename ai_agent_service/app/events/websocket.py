"""聊天事件 WebSocket 协议和 FastAPI 路由。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

from app.events.store import Event, EventStore, EventSubscription, ResyncRequired

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_SUBSCRIBE_TIMEOUT_S = 15.0
_SLOW_SEND_THRESHOLD_S = 0.5


def install_event_websocket_route(
    app: FastAPI,
    *,
    event_store: EventStore,
    expected_token: str | None,
    heartbeat_interval_s: float,
) -> None:
    """向应用注册认证且支持断线续传的聊天事件路由。"""

    @app.websocket("/chat/events/ws")
    async def chat_events_websocket(websocket: WebSocket) -> None:
        """处理单个会话的 WebSocket 事件订阅及生命周期。"""
        if not _is_authorized(websocket, expected_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        subscription: EventSubscription | None = None
        receiver: asyncio.Task[None] | None = None
        try:
            subscribe = await asyncio.wait_for(websocket.receive_json(), timeout=_SUBSCRIBE_TIMEOUT_S)
            parsed = _validate_subscribe(subscribe)
            if parsed is None:
                await _send_protocol_error(websocket, "invalid_subscribe", "首条消息必须是有效的 subscribe 请求")
                return
            session_id, after_seq = parsed
            earliest_seq, last_seq = event_store.retained_range(session_id)
            if after_seq > last_seq or (
                after_seq > 0 and earliest_seq is not None and after_seq < earliest_seq - 1
            ):
                await websocket.send_json(
                    {
                        "version": PROTOCOL_VERSION,
                        "type": "history_gap",
                        "session_id": session_id,
                        "after_seq": after_seq,
                        "earliest_seq": earliest_seq,
                        "last_seq": last_seq,
                    }
                )
                return

            replay, subscription = event_store.subscribe(session_id, after_seq)
            for event in replay:
                await _send_event(websocket, event)
            await websocket.send_json(
                {
                    "version": PROTOCOL_VERSION,
                    "type": "subscribed",
                    "session_id": session_id,
                    "last_seq": last_seq,
                }
            )
            incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            receiver = asyncio.create_task(_receive_messages(websocket, incoming), name="chat-event-websocket-receiver")
            await _run_subscription(websocket, subscription, incoming, heartbeat_interval_s)
        except asyncio.TimeoutError:
            await _send_protocol_error(websocket, "subscribe_timeout", "未在规定时间内收到 subscribe 请求")
        except WebSocketDisconnect:
            logger.debug("Chat event websocket disconnected")
        finally:
            if receiver is not None:
                receiver.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await receiver
            if subscription is not None:
                event_store.unsubscribe(subscription)


async def _run_subscription(
    websocket: WebSocket,
    subscription: EventSubscription,
    incoming: asyncio.Queue[dict[str, Any] | None],
    heartbeat_interval_s: float,
) -> None:
    """在单一发送循环中处理事件、心跳和客户端确认。"""
    while True:
        outbound_task = asyncio.create_task(subscription.queue.get())
        incoming_task = asyncio.create_task(incoming.get())
        done, pending = await asyncio.wait(
            {outbound_task, incoming_task}, timeout=heartbeat_interval_s, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        if not done:
            await websocket.send_json({"version": PROTOCOL_VERSION, "type": "heartbeat"})
            continue
        if incoming_task in done:
            message = incoming_task.result()
            if message is None:
                return
            if not await _handle_client_message(websocket, subscription, message):
                return
        if outbound_task in done:
            item = outbound_task.result()
            if isinstance(item, ResyncRequired):
                # 背压重同步：只携带脱敏诊断，不阻塞其他订阅，也不取消活跃轮次。
                await websocket.send_json(
                    {
                        "version": PROTOCOL_VERSION,
                        "type": "resync_required",
                        "session_id": item.session_id,
                        "after_seq": item.after_seq,
                        "reason": item.reason,
                        "diagnostics": subscription.diagnostics(),
                    }
                )
                logger.info(
                    "Subscriber resync issued session=%s reason=%s %s",
                    item.session_id,
                    item.reason,
                    subscription.diagnostics(),
                )
                return
            await _send_event(websocket, item)


async def _receive_messages(websocket: WebSocket, incoming: asyncio.Queue[dict[str, Any] | None]) -> None:
    """持续接收客户端协议消息，确保发送循环始终只有一个。"""
    try:
        while True:
            raw = await websocket.receive_json()
            await incoming.put(raw if isinstance(raw, dict) else {})
    except WebSocketDisconnect:
        await incoming.put(None)
    except json.JSONDecodeError:
        await incoming.put({})


async def _handle_client_message(
    websocket: WebSocket, subscription: EventSubscription, message: dict[str, Any]
) -> bool:
    """验证确认和心跳消息，拒绝任何命令型消息。"""
    if message.get("version") != PROTOCOL_VERSION:
        await _send_protocol_error(websocket, "unsupported_version", "协议版本不受支持")
        return False
    message_type = message.get("type")
    if message_type == "ack":
        seq = message.get("seq")
        if not isinstance(seq, int) or seq < subscription.acknowledged_seq:
            await _send_protocol_error(websocket, "invalid_ack", "ack.seq 必须是递增整数")
            return False
        subscription.acknowledge(seq)
        return True
    if message_type == "heartbeat":
        await websocket.send_json({"version": PROTOCOL_VERSION, "type": "heartbeat"})
        return True
    await _send_protocol_error(websocket, "unsupported_message", "事件通道不接受命令消息")
    return False


def _validate_subscribe(raw: Any) -> tuple[str, int] | None:
    """验证首条 subscribe 消息并提取会话游标。"""
    if not isinstance(raw, dict) or raw.get("version") != PROTOCOL_VERSION or raw.get("type") != "subscribe":
        return None
    session_id = raw.get("session_id")
    after_seq = raw.get("after_seq")
    if not isinstance(session_id, str) or not session_id.strip() or not isinstance(after_seq, int) or after_seq < 0:
        return None
    return session_id, after_seq


def _is_authorized(websocket: WebSocket, expected_token: str | None) -> bool:
    """按 HTTP Bearer 策略验证 WebSocket 握手。"""
    if not expected_token:
        return False
    scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(token, expected_token)


async def _send_event(websocket: WebSocket, event: Event) -> None:
    """发送带版本号的规范事件消息，并记录脱敏发送耗时。"""
    started = time.monotonic()
    await websocket.send_json({"version": PROTOCOL_VERSION, "type": "event", "event": event.to_wire()})
    elapsed = time.monotonic() - started
    if elapsed >= _SLOW_SEND_THRESHOLD_S:
        # 仅记录序号/类型/耗时等脱敏字段，绝不写入正文。
        logger.warning(
            "Slow socket send session=%s seq=%d type=%s elapsed_s=%.3f",
            event.session_id,
            event.seq,
            event.type,
            elapsed,
        )


async def _send_protocol_error(websocket: WebSocket, code: str, message: str) -> None:
    """发送结构化协议错误，并在发送失败时安全退出。"""
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_json(
            {"version": PROTOCOL_VERSION, "type": "protocol_error", "code": code, "message": message}
        )
