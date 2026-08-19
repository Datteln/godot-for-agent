"""管理经过身份绑定的本地 EditorPlugin 观察注册。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.codeact.contracts import CodeActErrorCode

EditorTransport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
LateResultHandler = Callable[[dict[str, Any]], None]
EDITOR_METHODS = frozenset(
    {
        "godot.editor.status",
        "godot.editor.reload_for_validation",
        "godot.editor.viewport_capture",
        "godot.editor.runtime_state",
        "godot.editor.debugger_errors",
        "godot.editor.profiler_snapshot",
    }
)


class EditorLoopbackConnection:
    """承载已验证本机 EditorPlugin 的 WebSocket 请求与响应。"""

    def __init__(self, late_result_handler: LateResultHandler | None = None) -> None:
        self._requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._late_result_handler = late_result_handler

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """转发 Gateway 请求并等待相同 call id 的 Plugin 响应。"""
        call_id = str(payload["call_id"])
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[call_id] = waiter
        await self._requests.put(payload)
        try:
            return await waiter
        finally:
            self._waiters.pop(call_id, None)

    async def next_request(self) -> dict[str, Any]:
        """返回下一条已认证的本机观察请求。"""
        return await self._requests.get()

    def resolve(self, payload: dict[str, Any]) -> bool:
        """完成未过期请求；迟到结果只返回 False 供审计处理。"""
        waiter = self._waiters.get(str(payload.get("call_id", "")))
        if waiter is None or waiter.done():
            if self._late_result_handler is not None:
                self._late_result_handler(payload)
            return False
        waiter.set_result(payload)
        return True

    def close(self) -> None:
        """在连接关闭时取消尚未完成的等待请求。"""
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.cancel()


@dataclass(slots=True)
class EditorRegistration:
    """保存一个本机 Plugin 实例的短期注册信息。"""

    project_id: str
    instance_id: str
    token: str
    expires_at: datetime
    transport: EditorTransport
    opened_files: dict[str, bool]
    last_seen_at: datetime


class EditorRegistry:
    """选择每个项目最新存活的本地 EditorPlugin，绝不持有写接口。"""

    def __init__(self, late_result_handler: LateResultHandler | None = None) -> None:
        self._registrations: dict[str, EditorRegistration] = {}
        self._lock = asyncio.Lock()
        self._late_result_handler = late_result_handler

    def set_late_result_handler(self, handler: LateResultHandler) -> None:
        """配置迟到 Editor 结果的只审计回调。"""
        self._late_result_handler = handler

    async def register(
        self, project_id: str, instance_id: str, transport: EditorTransport | None = None, *, ttl_seconds: int = 300
    ) -> str:
        """注册或替换项目的最新实例，并返回其短期令牌。"""
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with self._lock:
            self._registrations[project_id] = EditorRegistration(
                project_id, instance_id, token, now + timedelta(seconds=ttl_seconds), transport or _unavailable_transport, {}, now
            )
        return token

    async def connect(self, project_id: str, instance_id: str, token: str) -> EditorLoopbackConnection | None:
        """验证短期 token 后把最新实例绑定为本机 WebSocket transport。"""
        async with self._lock:
            registration = self._registrations.get(project_id)
            if registration is None or registration.instance_id != instance_id or not secrets.compare_digest(registration.token, token) or registration.expires_at <= datetime.now(UTC):
                return None
            connection = EditorLoopbackConnection(self._late_result_handler)
            registration.transport = connection.request
            registration.last_seen_at = datetime.now(UTC)
            return connection

    async def revoke(self, project_id: str, instance_id: str) -> None:
        """仅在实例身份匹配时撤销注册。"""
        async with self._lock:
            current = self._registrations.get(project_id)
            if current is not None and current.instance_id == instance_id:
                self._registrations.pop(project_id, None)

    async def opened_state(self, project_id: str, path: str) -> bool | None:
        """查询 Editor 是否正打开某一文件；无实例时返回 None。"""
        registration = await self._live_registration(project_id)
        if registration is None:
            return None
        return registration.opened_files.get(path)

    async def invoke(self, project_id: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        """将 allowlisted 观察请求发给匹配实例，并返回类型化失败。"""
        if payload.get("method") not in EDITOR_METHODS:
            return {"error_code": CodeActErrorCode.AUTHORIZATION_DENIED.value}
        registration = await self._live_registration(project_id)
        if registration is None:
            return {"error_code": CodeActErrorCode.EDITOR_UNAVAILABLE.value}
        try:
            result = await asyncio.wait_for(registration.transport(payload), timeout_seconds)
        except TimeoutError:
            return {"error_code": CodeActErrorCode.TIMEOUT.value}
        except asyncio.CancelledError:
            return {"error_code": CodeActErrorCode.EDITOR_CANCELLED.value}
        if result.get("project_id") not in {None, project_id}:
            return {"error_code": CodeActErrorCode.PROJECT_MISMATCH.value}
        if result.get("method") not in {None, payload["method"]}:
            return {"error_code": CodeActErrorCode.EDITOR_UNAVAILABLE.value}
        opened_files = result.get("opened_files")
        if isinstance(opened_files, dict):
            registration.opened_files = {
                str(path): bool(dirty) for path, dirty in opened_files.items()
            }
        registration.last_seen_at = datetime.now(UTC)
        return result

    async def _live_registration(self, project_id: str) -> EditorRegistration | None:
        """获取未过期实例，过期时立即撤销。"""
        async with self._lock:
            registration = self._registrations.get(project_id)
            if registration is None:
                return None
            if registration.expires_at <= datetime.now(UTC):
                self._registrations.pop(project_id, None)
                return None
            return registration


async def _unavailable_transport(_payload: dict[str, Any]) -> dict[str, Any]:
    """在已注册实例尚未完成本机连接时返回不可用。"""
    return {"error_code": CodeActErrorCode.EDITOR_UNAVAILABLE.value}
