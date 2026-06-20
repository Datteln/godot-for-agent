"""`read_file`：安全读取工程内文本文件（按行分页，类似 Claude Code 的 Read 工具）。"""

from __future__ import annotations

import logging
from typing import Any

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

# 单次系统调用最多扫描的字节数：无论文件实际多大，一次 read() 只读这么多，
# 避免把几百 MB/GB 的文件整体读进内存导致瞬时 OOM。超过此上限的部分对本次
# 调用不可见（scan_truncated=True），需要换用 grep_code 之类的工具定位。
MAX_SCAN_BYTES = 4 * 1024 * 1024
# 单次调用最多返回的字节数（即便请求的行数范围内字节数更大也会截断）。
MAX_RETURN_BYTES = 128 * 1024
DEFAULT_LIMIT_LINES = 200
MAX_LIMIT_LINES = 20000

logger = logging.getLogger(__name__)

READ_FILE_SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "读取当前 Godot 工程根目录内的一个文本文件（只读，自动遵守安全边界）。"
        "默认从第 1 行开始最多返回 200 行；返回结果里 has_more=true 表示文件还有更多内容，"
        "此时应带上更大的 offset 再调用一次以继续读取，而不是假定文件已读完。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工程根目录的文件路径。"},
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始），默认 1。",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的行数，默认 200，不能超过服务端上限。",
            },
        },
        "required": ["path"],
    },
}


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(minimum, min(value, maximum))


async def read_file_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """按行分页读取工程内文本文件，超出单次扫描上限时标记 scan_truncated。"""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path 不能为空")
    if not path_ok(path, ctx.security, write=False):
        raise ValueError("path 不在允许读取范围内")

    offset = _clamp_int(args.get("offset", 1), default=1, minimum=1, maximum=2**31 - 1)
    limit = _clamp_int(args.get("limit", DEFAULT_LIMIT_LINES), default=DEFAULT_LIMIT_LINES, minimum=1, maximum=MAX_LIMIT_LINES)

    full_path = ctx.security.project_root / path
    logger.info(
        "read_file start session=%s path=%s offset=%d limit=%d",
        ctx.session_id,
        path,
        offset,
        limit,
    )

    with full_path.open("rb") as stream:
        chunk = stream.read(MAX_SCAN_BYTES + 1)
    scan_truncated = len(chunk) > MAX_SCAN_BYTES
    chunk = chunk[:MAX_SCAN_BYTES]
    text = chunk.decode("utf-8", errors="replace")
    # 统一换行符，避免 CRLF 文件里每一行末尾都带一个看不见的 \r 混进返回内容。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    all_lines = text.split("\n")
    total_lines_scanned = len(all_lines)
    start = offset - 1
    end = min(start + limit, total_lines_scanned)
    page_lines = all_lines[start:end] if start < total_lines_scanned else []
    page_text = "\n".join(page_lines)

    byte_truncated = False
    encoded = page_text.encode("utf-8")
    if len(encoded) > MAX_RETURN_BYTES:
        page_text = encoded[:MAX_RETURN_BYTES].decode("utf-8", errors="ignore")
        byte_truncated = True

    has_more = end < total_lines_scanned or scan_truncated
    logger.info(
        "read_file success session=%s path=%s lines_returned=%d has_more=%s scan_truncated=%s",
        ctx.session_id,
        path,
        len(page_lines),
        has_more,
        scan_truncated,
    )
    return {
        "path": path,
        "content": page_text,
        "encoding": "utf-8",
        "offset": offset,
        "limit": limit,
        "lines_returned": len(page_lines),
        "total_lines_scanned": total_lines_scanned,
        "has_more": has_more,
        "scan_truncated": scan_truncated,
        "truncated": byte_truncated or has_more,
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
