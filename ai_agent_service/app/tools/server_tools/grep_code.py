"""`grep_code`：安全的工程内正则检索（任务 5.1）。

检索语料只包含源码与配置：运行日志、缓存、服务状态等运行时目录与日志/
临时文件在扫描前由路径策略排除。每个命中在工具边界即被规范化为
``{path, line, excerpt, line_truncated}``——受限摘录而非整行原文——模型
上下文、权威展示稿、历史快照与事件流接收的都只是该规范化表示。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import regex

from app.security.paths import path_ok
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

MAX_RESULTS = 200
MAX_FILE_BYTES = 256 * 1024
## 单个匹配摘录的最大字符数（任务 5.1）：命中行超过该长度时只保留前缀
## 并携带 `line_truncated` 标记，绝不把未受限整行交给模型或 UI。
MATCH_EXCERPT_MAX_CHARS = 240
# 单次行匹配的墙钟上限。`regex` 模块支持 `timeout=`，对 `(a+)+$` 这类灾难性
# 回溯模式能在到点后抛 `TimeoutError`，而标准库 `re` 没有这个能力，一旦命中
# 就会把执行它的线程长时间占满 CPU。整个扫描放进 `asyncio.to_thread`，因此即使
# 某一行触发超时也只占用线程池工作线程，不阻塞事件循环。
PER_LINE_MATCH_TIMEOUT_S = 0.5

## 运行时目录排除（任务 5.1）：日志、缓存、服务状态与依赖目录是可观测/
## 生成数据，不是源码检索语料，扫描前按路径段整体排除。
EXCLUDED_DIR_PARTS = frozenset(
    {
        ".godot",
        ".git",
        ".import",
        "logs",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "node_modules",
        ".ai_agent_service",
        ".ai_agent",
    }
)
## 按扩展名排除的运行时/临时文件（任务 5.1）。
EXCLUDED_SUFFIXES = frozenset({".log", ".tmp"})

logger = logging.getLogger(__name__)

GREP_CODE_SCHEMA: dict[str, Any] = {
    "name": "grep_code",
    "description": (
        "在当前 Godot 工程内按正则检索源码/配置文件（只读，自动遵守安全边界）。"
        "运行日志、缓存与服务状态目录不在检索语料内；每个命中只返回受限摘录。"
        "include 请使用源码/配置 glob（如 '**/*.gd'、'**/*.tscn'），不要使用 '**/*'。"
    ),
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


def search_eligible(rel: str) -> bool:
    """判断相对路径是否属于可检索的源码/配置语料（任务 5.1）。

    命中运行时目录段（日志/缓存/服务状态等）或日志/临时扩展名的文件
    在扫描前排除，不进入模型上下文、展示稿或事件流。

    Args:
        rel: 相对工程根目录的 POSIX 路径。

    Returns:
        属于可检索语料时返回 True。
    """
    parts = rel.split("/")
    for part in parts[:-1]:
        if part in EXCLUDED_DIR_PARTS:
            return False
    if Path(parts[-1]).suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return True


def _bounded_excerpt(line: str) -> tuple[str, bool]:
    """把命中行规范化为受限摘录；超长行截断并标记。"""
    if len(line) <= MATCH_EXCERPT_MAX_CHARS:
        return line, False
    return line[:MATCH_EXCERPT_MAX_CHARS], True


def _scan(
    compiled: regex.Pattern[str],
    root: Path,
    include: str,
    max_results: int,
    security: Any,
) -> dict[str, Any]:
    """同步扫描工程文件并收集规范化匹配；在 `asyncio.to_thread` 里执行。

    返回结果含 `regex_timeout` 标记：某一行的匹配触发 `regex` 超时时，立即停止
    整个扫描并把已收集到的部分结果连同该标记一并返回，避免在灾难性回溯模式上
    无意义地继续耗费 CPU。
    """
    matches: list[dict[str, Any]] = []
    truncated = False
    regex_timeout = False
    scanned_files = 0
    skipped_runtime_paths = 0

    for candidate in sorted(root.glob(include)):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(root).as_posix()
        if not path_ok(rel, security, write=False):
            continue
        if not search_eligible(rel):
            # 运行时目录/日志文件不属于检索语料：不读取、不计数为扫描文件。
            skipped_runtime_paths += 1
            continue
        scanned_files += 1
        data = candidate.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
        text = data.decode("utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            try:
                hit = compiled.search(line, timeout=PER_LINE_MATCH_TIMEOUT_S) is not None
            except TimeoutError:
                regex_timeout = True
                hit = False
            if hit:
                excerpt, line_truncated = _bounded_excerpt(line)
                matches.append(
                    {
                        "path": rel,
                        "line": line_no,
                        "excerpt": excerpt,
                        "line_truncated": line_truncated,
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
            if truncated or regex_timeout:
                break
        if truncated or regex_timeout:
            break

    return {
        "matches": matches,
        "truncated": truncated,
        "regex_timeout": regex_timeout,
        "scanned_files": scanned_files,
        "skipped_runtime_paths": skipped_runtime_paths,
    }


async def grep_code_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """按正则检索工程内源码/配置文件，返回规范化后的受限匹配。"""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern 不能为空")
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
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
    logger.info(
        "grep_code start session=%s include=%s max_results=%d pattern_length=%d",
        ctx.session_id,
        include,
        max_results,
        len(pattern),
    )

    # 整个扫描（文件 IO + 逐行正则匹配）放进线程池：既不阻塞事件循环，配合
    # `regex` 的逐行超时，灾难性回溯也最多占满一个工作线程一小段时间即被打断。
    scan = await asyncio.to_thread(_scan, compiled, root, include, max_results, ctx.security)

    logger.info(
        "grep_code success session=%s include=%s scanned_files=%d matches=%d "
        "skipped_runtime_paths=%d truncated=%s regex_timeout=%s",
        ctx.session_id,
        include,
        scan["scanned_files"],
        len(scan["matches"]),
        scan["skipped_runtime_paths"],
        scan["truncated"],
        scan["regex_timeout"],
    )
    return {
        "pattern": pattern,
        "include": include,
        "matches": scan["matches"],
        "match_count": len(scan["matches"]),
        "truncated": scan["truncated"],
        "regex_timeout": scan["regex_timeout"],
        "scanned_files": scan["scanned_files"],
        "skipped_runtime_paths": scan["skipped_runtime_paths"],
    }


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
