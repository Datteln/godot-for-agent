"""`grep_code`：安全的工程内正则检索。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

MAX_RESULTS = 200
MAX_FILE_BYTES = 256 * 1024

logger = logging.getLogger(__name__)

GREP_CODE_SCHEMA: dict[str, Any] = {
    "name": "grep_code",
    "description": "在当前 Godot 工程内按正则检索文本文件（只读，自动遵守安全边界）。",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式。"},
            "include": {
                "type": "string",
                "description": "相对工程根目录的 glob，默认 '**/*'，例如 '**/*.gd'。",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回匹配行数，默认 200，不能超过服务端上限。",
            },
        },
        "required": ["pattern"],
    },
}


def _validate_glob(include: str) -> None:
    """拒绝绝对路径与路径穿越 glob。"""
    path = Path(include)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("include 不允许使用绝对路径或 '..'")


async def grep_code_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """按正则检索工程内文本文件。"""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern 不能为空")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"pattern 不是合法正则：{exc}") from exc

    include = args.get("include", "**/*")
    if not isinstance(include, str) or not include:
        raise ValueError("include 必须是非空字符串")
    _validate_glob(include)

    max_results_raw = args.get("max_results", MAX_RESULTS)
    if not isinstance(max_results_raw, int) or max_results_raw <= 0:
        raise ValueError("max_results 必须是正整数")
    max_results = min(max_results_raw, MAX_RESULTS)

    root = ctx.security.project_root
    matches: list[dict[str, Any]] = []
    truncated = False
    scanned_files = 0
    logger.info(
        "grep_code start session=%s include=%s max_results=%d pattern_length=%d",
        ctx.session_id,
        include,
        max_results,
        len(pattern),
    )

    for candidate in sorted(root.glob(include)):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if not path_ok(rel, ctx.security, write=False):
            continue
        scanned_files += 1
        data = candidate.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
        text = data.decode("utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append({"path": rel, "line": line_no, "text": line})
                if len(matches) >= max_results:
                    truncated = True
                    logger.info(
                        "grep_code success session=%s include=%s scanned_files=%d matches=%d truncated=%s",
                        ctx.session_id,
                        include,
                        scanned_files,
                        len(matches),
                        truncated,
                    )
                    return {
                        "pattern": pattern,
                        "include": include,
                        "matches": matches,
                        "truncated": truncated,
                    }

    logger.info(
        "grep_code success session=%s include=%s scanned_files=%d matches=%d truncated=%s",
        ctx.session_id,
        include,
        scanned_files,
        len(matches),
        truncated,
    )
    return {"pattern": pattern, "include": include, "matches": matches, "truncated": truncated}


def register_grep_code_tool() -> None:
    """把 `grep_code` 注册进全局工具表。"""
    register(
        ToolDef(
            name="grep_code",
            domain="project",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="在工程内按正则检索代码和文本",
            schema=GREP_CODE_SCHEMA,
            handler=grep_code_handler,
        )
    )
