"""Verify Phase 1：编辑后语法快检（按文件类型跑对应 CLI 命令，纯 bash，不消耗 token）。

`run_syntax_check()` 只负责"这份文件改完后还能不能被对应语言的解析器接受"，
语义/逻辑层面的问题留给 Phase 2（LLM 校验）。可执行文件缺失或命令超时都
视为该阶段不可用，调用方据此跳过 Phase 1 直接进入 Phase 2。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.verify.contracts import VerifyIssue

logger = logging.getLogger(__name__)

_LINE_NUMBER_PATTERN = re.compile(r":(\d+)\b")
_STDERR_PREVIEW_CHARS = 500


@dataclass(frozen=True, slots=True)
class SyntaxCheckResult:
    status: Literal["passed", "failed", "unavailable"]
    reason_code: Literal["verified", "syntax_issue", "validator_missing", "validator_timeout"]
    issues: tuple[VerifyIssue, ...] = ()
    summary: str = ""


def _build_command(path: str, project_root: Path, godot_path: str) -> list[str] | None:
    """根据文件后缀构造语法快检命令。

    Args:
        path: 相对工程根目录的文件路径。
        project_root: 工程根目录绝对路径。
        godot_path: Godot 可执行文件路径（GDScript 检查用）。

    Returns:
        可直接传给 `asyncio.create_subprocess_exec` 的参数列表；文件类型不
        支持语法快检时返回 None。
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".gd":
        return [godot_path, "--headless", "--check-only", "--path", str(project_root), "--script", path]
    if suffix == ".py":
        return [sys.executable, "-m", "py_compile", str(project_root / path)]
    if suffix == ".cs":
        return ["dotnet", "build", "--nologo", "-v", "q"]
    return None


def _parse_stderr_issues(stderr_text: str, path: str) -> list[VerifyIssue]:
    """把语法检查命令的 stderr 输出解析为结构化问题列表（best-effort）。

    Args:
        stderr_text: 命令的 stderr 文本。
        path: 被检查文件的相对路径，写入每条 `VerifyIssue.file_path`。

    Returns:
        解析出的 `VerifyIssue` 列表；无法解析出任何非空行时返回空列表。
    """
    issues: list[VerifyIssue] = []
    for line in stderr_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _LINE_NUMBER_PATTERN.search(stripped)
        line_no = int(match.group(1)) if match else None
        issues.append(
            VerifyIssue(severity="error", file_path=path, line=line_no, message=stripped[:_STDERR_PREVIEW_CHARS])
        )
    return issues


async def run_syntax_check(
    *,
    path: str,
    project_root: Path,
    godot_path: str,
    timeout_s: int,
) -> SyntaxCheckResult:
    """对单个文件运行 Phase 1 语法快检。

    Args:
        path: 相对工程根目录的文件路径（编辑工具调用入参里的 `path`）。
        project_root: 工程根目录绝对路径。
        godot_path: Godot 可执行文件路径，用于 `.gd` 文件检查。
        timeout_s: 命令执行超时秒数。

    Returns:
        `(passed, issues)`：`passed=True` 时 `issues` 为空；`passed=False`
        时 `issues` 至少含一条。文件类型不支持快检、对应可执行文件不存在、
        或命令执行超时时返回 None（调用方应视为跳过本阶段，直接进入 Phase 2）。
    """
    command = _build_command(path, project_root, godot_path)
    if command is None:
        return SyntaxCheckResult(
            status="unavailable",
            reason_code="validator_missing",
            summary="No deterministic syntax validator is configured for this file type.",
        )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Verify syntax check unavailable command=%s error=%s", command[0], exc)
        return SyntaxCheckResult(
            status="unavailable",
            reason_code="validator_missing",
            summary=f"Syntax validator executable is unavailable: {command[0]}",
        )

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("Verify syntax check timed out command=%s path=%s timeout_s=%d", command[0], path, timeout_s)
        return SyntaxCheckResult(
            status="unavailable",
            reason_code="validator_timeout",
            summary=f"Syntax validator timed out after {timeout_s} seconds.",
        )

    if process.returncode == 0:
        return SyntaxCheckResult(
            status="passed",
            reason_code="verified",
            summary="Deterministic syntax validation passed.",
        )

    stderr_text = stderr.decode("utf-8", errors="replace")
    issues = _parse_stderr_issues(stderr_text, path)
    if not issues:
        issues = [
            VerifyIssue(
                severity="error",
                file_path=path,
                line=None,
                message=stderr_text.strip()[:_STDERR_PREVIEW_CHARS] or "语法检查失败，命令未输出详细信息",
            )
        ]
    return SyntaxCheckResult(
        status="failed",
        reason_code="syntax_issue",
        issues=tuple(issues),
        summary=issues[0].message,
    )
