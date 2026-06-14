"""MCP stdio 最小入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, TextIO

from app.config import AppSettings
from app.permissions.engine import PermissionContext, check
from app.security.settings import SecuritySettings, security_settings_from_app
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef
from app.tools.server_tools import register_server_tools

logger = logging.getLogger(__name__)


def _mcp_tool(tool: ToolDef) -> dict[str, Any]:
    """把内部 ToolDef 转换为 MCP tools/list 条目。"""
    return {
        "name": tool.name,
        "description": str(tool.schema.get("description", tool.search_hint or "")),
        "inputSchema": tool.schema.get("parameters", {"type": "object", "properties": {}}),
    }


def _text_content(payload: Any) -> list[dict[str, str]]:
    """把任意 JSON 值包装为 MCP 文本 content。"""
    return [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class McpStdioServer:
    """基于 stdin/stdout 的 MCP JSON-RPC server。"""

    def __init__(self, settings: AppSettings, security: SecuritySettings) -> None:
        """初始化 MCP server。"""
        self._settings = settings
        self._security = security.model_copy(update={"permission_mode": "read_only"})

    async def run(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
        """持续读取 JSON-RPC line 并写回响应。"""
        for line in stdin:
            text = line.strip()
            if not text:
                continue
            try:
                request = json.loads(text)
                response = await self.handle(request)
            except json.JSONDecodeError as exc:
                logger.warning("MCP parse error error=%s", exc)
                response = _error(None, -32700, f"Parse error: {exc}")
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        return 0

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 请求或通知。"""
        request_id = request.get("id")
        method = str(request.get("method", ""))
        params = request.get("params", {})
        logger.debug("MCP request method=%s has_id=%s", method, request_id is not None)
        if request_id is None and method.startswith("notifications/"):
            return None
        if method == "initialize":
            logger.info("MCP initialize")
            return _response(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "godot-ai-agent-service", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            tools = self._visible_tools()
            logger.info("MCP tools/list count=%d", len(tools))
            return _response(request_id, {"tools": [_mcp_tool(tool) for tool in tools]})
        if method == "tools/call":
            if not isinstance(params, dict):
                logger.warning("MCP tools/call rejected: params must be object")
                return _error(request_id, -32602, "params must be an object")
            result = await self._call_tool(params)
            return _response(request_id, result)
        if method == "ping":
            return _response(request_id, {})
        logger.warning("MCP unknown method=%s", method)
        return _error(request_id, -32601, f"Unknown method: {method}")

    def _visible_tools(self) -> list[ToolDef]:
        """返回 MCP 可见工具集合。"""
        permission_ctx = PermissionContext(
            security=self._security,
            effective_tools=frozenset(REGISTRY),
        )
        visible: list[ToolDef] = []
        for tool in REGISTRY.values():
            if tool.side != "server" or tool.handler is None:
                continue
            if check(tool, {}, permission_ctx) == "allow":
                visible.append(tool)
        logger.debug("MCP visible tools resolved count=%d", len(visible))
        return sorted(visible, key=lambda item: item.name)

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个 MCP tools/call 请求。"""
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            logger.warning("MCP tools/call rejected: missing tool name")
            return {"isError": True, "content": _text_content({"error": "tool name is required"})}
        if not isinstance(args, dict):
            logger.warning("MCP tools/call rejected: arguments not object tool=%s", name)
            return {"isError": True, "content": _text_content({"error": "arguments must be an object"})}
        logger.info("MCP tools/call tool=%s", name)
        tool = REGISTRY.get(name)
        if tool is None or tool.side != "server" or tool.handler is None:
            logger.warning("MCP tools/call rejected: unknown server tool=%s", name)
            return {"isError": True, "content": _text_content({"error": f"unknown server tool: {name}"})}
        permission_ctx = PermissionContext(
            security=self._security,
            effective_tools=frozenset(REGISTRY),
        )
        if check(tool, args, permission_ctx) != "allow":
            logger.warning("MCP tools/call denied tool=%s", name)
            return {"isError": True, "content": _text_content({"error": f"permission denied: {name}"})}
        try:
            result = await tool.handler(
                args,
                ToolContext(
                    security=self._security,
                    session_id="mcp-stdio",
                    effective_tools=frozenset(REGISTRY),
                    rag_index_path=self._settings.resolved_rag_index_path(),
                ),
            )
        except Exception as exc:
            logger.exception("MCP tools/call failed tool=%s", name)
            return {"isError": True, "content": _text_content({"error": str(exc)})}
        logger.info("MCP tools/call success tool=%s", name)
        return {"content": _text_content(result)}


async def run_mcp_stdio(settings: AppSettings | None = None) -> int:
    """注册 server 工具并启动 MCP stdio server。"""
    resolved_settings = settings or AppSettings()
    register_server_tools()
    security = security_settings_from_app(resolved_settings)
    logger.info(
        "Starting MCP stdio server project_root=%s permission_mode=%s",
        security.project_root,
        security.permission_mode,
    )
    return await McpStdioServer(resolved_settings, security).run()
