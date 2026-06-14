"""文件系统范围硬闸：`path_ok`（详设 B §3.3）。

任何 server 工具访问工程内路径前都必须先过 `path_ok`/`all_paths_ok`：
拒绝越界（`..`、绝对路径、跨盘符）、按路径段/glob 匹配 `deny_*`、并支持
`allow_paths` 收紧。这是权限闸 §3.2 的第 1 级安全硬闸，deny 优先且不可被
任何权限模式/规则绕过。
"""

from __future__ import annotations

import logging
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.security.settings import SecuritySettings

logger = logging.getLogger(__name__)


def _resolved(path: Path) -> Path:
    """返回路径的规范化绝对形式：解析符号链接并在 Windows 下统一大小写/分隔符。

    Args:
        path: 待规范化的路径。

    Returns:
        解析符号链接后的绝对路径；Windows 下额外做大小写归一，便于跨盘符/
        大小写不一致场景下的安全比较。
    """
    return Path(os.path.normcase(str(path.resolve())))


def _matches_deny(rel: str, deny_patterns: list[str]) -> bool:
    """判断相对路径是否命中某条 deny 路径段或 glob 模式。

    Args:
        rel: 相对于 `project_root` 的 POSIX 风格相对路径。
        deny_patterns: deny 列表，元素可以是路径前缀（如 `.git/`）或 glob。

    Returns:
        命中任意一条 deny 规则则返回 True。
    """
    for raw in deny_patterns:
        pattern = raw.rstrip("/")
        if rel == pattern or rel.startswith(pattern + "/") or fnmatch(rel, pattern):
            return True
    return False


def path_ok(target: str, security: SecuritySettings, write: bool = False) -> bool:
    """校验目标路径是否落在工程根内且未被安全规则拒绝。

    校验顺序：先解析为绝对路径并确认未越出 `project_root`（拒绝 `..`、
    绝对路径越界、跨盘符与符号链接逃逸），再按读写场景匹配
    `deny_read_paths`/`deny_write_paths`，最后若配置了 `allow_paths`
    则要求路径落在其子路径之下。

    Args:
        target: 相对 `project_root` 的目标路径（可包含 `..`，会被规范化校验）。
        security: 当前会话的安全边界配置。
        write: 是否为写操作；决定使用 `deny_write_paths` 还是 `deny_read_paths`。

    Returns:
        路径合法且未被拒绝时返回 True，否则返回 False。
    """
    target_path = Path(target)
    if target_path.is_absolute():
        logger.debug("Path rejected reason=absolute target=%s write=%s", target, write)
        return False

    root = _resolved(security.project_root)
    candidate = _resolved(security.project_root / target_path)
    try:
        rel = candidate.relative_to(root).as_posix()
    except ValueError:
        logger.debug("Path rejected reason=outside_root target=%s write=%s", target, write)
        return False  # 越界、绝对路径逃逸或跨盘符

    deny = security.deny_write_paths if write else security.deny_read_paths
    if _matches_deny(rel, deny):
        logger.debug("Path rejected reason=deny_pattern rel=%s write=%s", rel, write)
        return False

    if security.allow_paths:
        allowed = any(
            rel == a.rstrip("/") or rel.startswith(a.rstrip("/") + "/")
            for a in security.allow_paths
        )
        if not allowed:
            logger.debug("Path rejected reason=not_in_allow_paths rel=%s write=%s", rel, write)
            return False

    return True


def all_paths_ok(
    args: dict[str, Any], path_args: list[str], security: SecuritySettings, write: bool = False
) -> bool:
    """批量校验某次工具调用涉及的所有路径参数。

    Args:
        args: 工具调用的入参字典。
        path_args: `ToolDef.path_args` 声明的、值为路径的参数名列表。
        security: 当前会话的安全边界配置。
        write: 是否按写操作的 deny 列表校验。

    Returns:
        所有出现在 `args` 中的路径参数均通过 `path_ok` 时返回 True；
        `path_args` 为空（工具不涉及路径参数）时同样返回 True。
    """
    return all(path_ok(args[name], security, write) for name in path_args if name in args)
