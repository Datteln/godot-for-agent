"""提供强制回环地址校验的 EditorPlugin 观察通道。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field

from app.codeact.editor import EditorLoopbackConnection, EditorRegistry


class EditorRegistrationRequest(BaseModel):
    """描述已由主服务认证的 EditorPlugin 本机注册。"""

    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=1024)
    instance_id: str = Field(min_length=1, max_length=256)


class EditorRegistrationResponse(BaseModel):
    """返回只用于紧随其后的本机 WebSocket 的短期令牌。"""

    token: str
    expires_in_seconds: int = 300


def add_editor_loopback_routes(router: APIRouter, registry: EditorRegistry) -> None:
    """附加双重认证且仅接收 loopback 客户端的 Editor 观察路由。"""
    @router.post("/internal/codeact/editor/register", response_model=EditorRegistrationResponse)
    async def register_editor(request: Request, payload: EditorRegistrationRequest) -> EditorRegistrationResponse:
        """向经过主服务认证的回环 Plugin 签发短期连接令牌。"""
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Editor IPC is loopback-only")
        return EditorRegistrationResponse(token=await registry.register(payload.project_id, payload.instance_id))

    @router.websocket("/internal/codeact/editor/socket")
    async def editor_socket(websocket: WebSocket, project_id: str, instance_id: str) -> None:
        """仅为 loopback 客户端建立任务/调用身份绑定的观察通道。"""
        if not _is_loopback(websocket.client.host if websocket.client else None):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            token = str(hello.get("token", "")) if isinstance(hello, dict) else ""
            connection = await registry.connect(project_id, instance_id, token)
            if connection is None:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            sender = asyncio.create_task(_send_requests(websocket, connection))
            try:
                while True:
                    payload = await websocket.receive_json()
                    if isinstance(payload, dict):
                        connection.resolve(payload)
            finally:
                connection.close()
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender
                await registry.revoke(project_id, instance_id)
        except (TimeoutError, WebSocketDisconnect):
            with suppress(RuntimeError):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


async def _send_requests(websocket: WebSocket, connection: EditorLoopbackConnection) -> None:
    """顺序发送 Gateway allowlist 观察请求。"""
    while True:
        await websocket.send_json(await connection.next_request())


def _is_loopback(host: str | None) -> bool:
    """严格判断 IPv4/IPv6 客户端地址是否为本机回环。"""
    if not host:
        return False
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
