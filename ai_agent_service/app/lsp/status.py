"""LSP 与诊断能力状态。"""

from __future__ import annotations

from typing import Any


def lsp_status() -> dict[str, Any]:
    """返回当前 LSP/诊断子系统状态。"""
    return {
        "enabled": True,
        "mode": "front_context_diagnostics",
        "lsp_server": "front_forwarded",
        "diagnostics_sources": ["Godot editor logs", "open script state", "request context"],
        "fallbacks": ["ClassDB", "grep_code", "read_debugger_errors"],
    }
