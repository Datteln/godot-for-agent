"""`read_file`：安全读取工程内文本文件。"""

from __future__ import annotations

import logging
from typing import Any

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

MAX_BYTES = 128 * 1024

logger = logging.getLogger(__name__)

READ_FILE_SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": "读取当前 Godot 工程根目录内的一个文本文件（只读，自动遵守安全边界）。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工程根目录的文件路径。"},
            "max_bytes": {
                "type": "integer",
                "description": "最多读取的字节数，默认 131072，不能超过服务端上限。",
            },
        },
        "required": ["path"],
    },
}


async def read_file_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """读取工程内文本文件，超出上限时截断。"""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path 不能为空")
    if not path_ok(path, ctx.security, write=False):
        raise ValueError("path 不在允许读取范围内")

    max_bytes_raw = args.get("max_bytes", MAX_BYTES)
    if not isinstance(max_bytes_raw, int) or max_bytes_raw <= 0:
        raise ValueError("max_bytes 必须是正整数")
    max_bytes = min(max_bytes_raw, MAX_BYTES)

    full_path = ctx.security.project_root / path
    logger.info("read_file start session=%s path=%s max_bytes=%d", ctx.session_id, path, max_bytes)
    # 只读取上限 + 1 字节，避免把数 GB 的大文件整体读进内存导致瞬时内存暴涨/OOM；
    # 多读 1 字节用于判断文件是否被截断，无需先 stat 再 read。
    with full_path.open("rb") as stream:
        chunk = stream.read(max_bytes + 1)
    truncated = len(chunk) > max_bytes
    chunk = chunk[:max_bytes]
    text = chunk.decode("utf-8", errors="replace")
    logger.info(
        "read_file success session=%s path=%s bytes_read=%d truncated=%s",
        ctx.session_id,
        path,
        len(chunk),
        truncated,
    )
    return {
        "path": path,
        "content": text,
        "encoding": "utf-8",
        "bytes_read": len(chunk),
        "truncated": truncated,
    }


def register_read_file_tool() -> None:
    """把 `read_file` 注册进全局工具表。"""
    register(
        ToolDef(
            name="read_file",
            domain="project",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="读取工程内文本文件内容",
            schema=READ_FILE_SCHEMA,
            handler=read_file_handler,
            path_args=["path"],
        )
    )
