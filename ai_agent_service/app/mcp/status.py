"""MCP 入口能力状态。"""

from __future__ import annotations

from typing import Any


def mcp_status() -> dict[str, Any]:
    """返回当前 MCP 入口状态。"""
    return {
        "enabled": True,
        "mode": "stdio",
        "entrypoint": "python -m app --mcp-stdio",
        "permission_mode_when_enabled": "read_only",
        "front_confirmation_channel": False,
        "server_tools_only": True,
    }
