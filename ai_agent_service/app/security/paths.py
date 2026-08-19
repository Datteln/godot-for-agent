"""文件系统范围硬闸：`path_ok`（详设 B §3.3）。

任何 server 工具访问工程内路径前都必须先过 `path_ok`/`all_paths_ok`：
拒绝越界（`..`、绝对路径、跨盘符）、按路径段/glob 匹配 `deny_*`、并支持
`allow_paths` 收紧。这是权限闸 §3.2 的第 1 级安全硬闸，deny 优先且不可被
任何权限模式/规则绕过。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.security.settings import SecuritySettings

logger = logging.getLogger(__name__)


class ProjectRootResolutionError(ValueError):
    """表示逻辑项目根无法安全解析为 worker 写入边界。"""


@dataclass(frozen=True, slots=True)
class ProjectRoots:
    """保存逻辑工程根、实际挂载根与只读 Git 根。"""

    logical_project_root: Path
    resolved_project_root: Path
    repository_root: Path | None


def _resolved(path: Path) -> Path:
    """返回路径的规范化绝对形式：解析符号链接并在 Windows 下统一大小写/分隔符。

    Args:
        path: 待规范化的路径。

    Returns:
        解析符号链接后的绝对路径；Windows 下额外做大小写归一，便于跨盘符/
        大小写不一致场景下的安全比较。
    """
    return Path(os.path.normcase(str(path.resolve())))


def resolve_project_roots(security: SecuritySettings) -> ProjectRoots:
    """安全解析逻辑工程根、允许的链接目标和独立 Git 根。"""
    logical = Path(os.path.normcase(str(security.project_root.absolute())))
    resolved = _resolved(security.project_root)
    allowed = [_resolved(root) for root in security.allowed_symlink_targets]
    if resolved != _resolved(logical) and not any(
        resolved == root or root in resolved.parents for root in allowed
    ):
        raise ProjectRootResolutionError("resolved project root is outside allowed symlink targets")
    return ProjectRoots(
        logical_project_root=logical,
        resolved_project_root=resolved,
        repository_root=_find_repository_root(logical),
    )


def _find_repository_root(start: Path) -> Path | None:
    """向上寻找 Git 元数据目录并返回解析后的只读仓库根。"""
    for candidate in (start, *start.parents):
        if candidate.joinpath(".git").exists():
            return _resolved(candidate)
    return None


def resolved_path_for(target: str, security: SecuritySettings, *, write: bool = False) -> Path:
    """返回经过现有边界检查的已解析工程内路径。"""
    normalized = normalized_project_path(target)
    if normalized is None or not path_ok(normalized, security, write=write):
        raise ProjectRootResolutionError("path is outside the allowed project boundary")
    return _resolved(security.project_root / normalized)


def normalized_project_path(target: object) -> str | None:
    """把相对路径或 `res://` 路径规范化为安全的项目相对路径。"""
    if not isinstance(target, str):
        return None
    cleaned = target.strip().replace("\\", "/")
    if not cleaned or _has_malformed_godot_scheme(cleaned):
        return None
    if cleaned.startswith("res://"):
        cleaned = cleaned.removeprefix("res://").lstrip("/")
    elif "://" in cleaned:
        return None
    if not cleaned or any(":" in part for part in cleaned.split("/")):
        return None
    return cleaned


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


def path_ok(target: object, security: SecuritySettings, write: bool = False) -> bool:
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
    normalized = normalized_project_path(target)
    if normalized is None:
        logger.debug(
            "Path rejected reason=invalid_project_path target_type=%s write=%s",
            type(target).__name__,
            write,
        )
        return False
    target_path = Path(normalized)
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
    return all(
        isinstance(args[name], str) and path_ok(args[name], security, write)
        for name in path_args
        if name in args
    )


def _has_malformed_godot_scheme(target: str) -> bool:
    """判断路径是否使用了缺少双斜杠的 Godot 伪 scheme。"""
    lowered = target.casefold()
    return any(
        lowered.startswith(prefix)
        for prefix in ("user:", "res:")
    ) and not any(
        lowered.startswith(prefix)
        for prefix in ("user://", "res://")
    )


def capture_path_ok(target: object, security: SecuritySettings, write: bool = False) -> bool:
    """校验截图/图片路径，同时支持工程路径与受限的 Godot `user://` 空间。

    `user://` 仅作为 Godot 管理的临时截图空间，不映射到项目根规则；该分支仍
    严格拒绝空路径、目录穿越、反斜杠穿越与其他 URI scheme。`res://` 会先
    转成项目相对路径，再复用项目路径的完整 allow/deny 边界。
    """
    if not isinstance(target, str):
        logger.debug(
            "Capture path rejected reason=non_string target_type=%s write=%s",
            type(target).__name__,
            write,
        )
        return False
    cleaned = target.strip().replace("\\", "/")
    if not cleaned:
        return False
    if _has_malformed_godot_scheme(cleaned):
        logger.debug(
            "Capture path rejected reason=malformed_scheme target=%s write=%s",
            target,
            write,
        )
        return False
    if cleaned.startswith("user://"):
        relative = cleaned.removeprefix("user://").lstrip("/")
        parts = [part for part in relative.split("/") if part]
        accepted = bool(parts) and ".." not in parts and all(":" not in part for part in parts)
        if not accepted:
            logger.debug("Capture path rejected reason=user_boundary target=%s write=%s", target, write)
        return accepted
    if "://" in cleaned and not cleaned.startswith("res://"):
        logger.debug("Capture path rejected reason=unknown_scheme target=%s write=%s", target, write)
        return False
    project_target = normalized_project_path(cleaned)
    return project_target is not None and path_ok(project_target, security, write)


def all_capture_paths_ok(
    args: dict[str, Any],
    path_args: list[str],
    security: SecuritySettings,
    write: bool = False,
) -> bool:
    """批量校验截图/图片专用路径参数，非字符串参数按非法处理。"""
    return all(
        isinstance(args[name], str) and capture_path_ok(args[name], security, write)
        for name in path_args
        if name in args
    )
