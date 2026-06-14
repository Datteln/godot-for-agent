"""`list_files`：M0 唯一的最小工具（§19 M0 骨架 / §10 代码检索）。

只读、限定工程根、server 端执行：按 glob 模式列出文件路径，每个结果都
经 `path_ok` 校验，自动遵守 `deny_read_paths`/`allow_paths`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

# 单次返回的最大文件数，避免把超大工程一次性灌入模型上下文。
MAX_RESULTS = 200

logger = logging.getLogger(__name__)

LIST_FILES_SCHEMA: dict[str, Any] = {
    "name": "list_files",
    "description": (
        "在当前 Godot 工程根目录内按 glob 模式列出文件路径（只读）。"
        "结果自动遵守安全边界中的禁止/允许路径规则，超过上限会被截断。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "相对工程根目录的 glob 模式，例如 '**/*.gd' 或 'scenes/**/*.tscn'。",
            }
        },
        "required": ["pattern"],
    },
}


async def list_files_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """按 glob 模式列出工程根目录下的文件路径。

    Args:
        args: 工具入参，必须包含字符串字段 `pattern`（相对工程根的 glob）。
        ctx: 工具执行上下文，提供安全边界配置。

    Returns:
        包含 `pattern`、匹配到的相对路径列表 `files` 与是否被截断的
        `truncated` 字段的字典。

    Raises:
        ValueError: `pattern` 缺失、为绝对路径或包含 `..` 时抛出，由编排层
            转换为 `is_error` 的工具结果。
    """
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern 不能为空")
    if pattern.startswith(("/", "\\")) or ".." in Path(pattern).parts:
        raise ValueError("pattern 不允许使用绝对路径或 '..'")

    root = ctx.security.project_root
    matches: list[str] = []
    truncated = False
    logger.info("list_files start session=%s pattern=%s", ctx.session_id, pattern)
    for candidate in sorted(root.glob(pattern)):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if not path_ok(rel, ctx.security, write=False):
            continue
        if len(matches) >= MAX_RESULTS:
            truncated = True
            break
        matches.append(rel)

    logger.info(
        "list_files success session=%s pattern=%s count=%d truncated=%s",
        ctx.session_id,
        pattern,
        len(matches),
        truncated,
    )
    return {"pattern": pattern, "files": matches, "truncated": truncated}


def register_list_files_tool() -> None:
    """把 `list_files` 注册进全局工具表。"""
    register(
        ToolDef(
            name="list_files",
            domain="project",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="按 glob 模式列出工程内文件路径",
            schema=LIST_FILES_SCHEMA,
            handler=list_files_handler,
        )
    )
