"""安全边界配置 schema（§9 安全边界 / 详设 B §3.8）。

`SecuritySettings` 是权限闸与 server 工具共同依赖的「配置即接口」：
- `project_root` 限定所有 server 文件操作的根目录；
- `deny_read_paths` / `deny_write_paths` 做读写分离（`addons/` 默认可读不可写）；
- `allow_paths` 非空时进一步收紧到指定子路径；
- `enabled_domains` 与 `permission_mode` 是权限闸 §3.2 决策管线的输入之一。

工程内配置只能在此基础上收紧（追加 deny / 缩小 allow_paths），不能放宽，
该约束在加载层（M1 配置 schema/migrations）落地，本模块只定义数据形状。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import AppSettings
from app.permissions.types import PermRule

logger = logging.getLogger(__name__)


class SecuritySettings(BaseModel):
    """权限闸与 server 工具共用的安全边界配置。

    Attributes:
        project_root: 工程根目录绝对路径，所有路径校验以此为基准。
        trusted: 工程是否已被用户标记为受信任；影响 `auto_approve`/allow 规则是否生效。
        permission_mode: 会话当前权限模式。
        enabled_domains: 当前启用的工具域集合，域外工具一律 `deny`。
        deny_read_paths: 禁止读取/检索的路径段或 glob（相对 `project_root`）。
        deny_write_paths: 禁止写入的路径段或 glob；`addons/` 默认可读不可写。
        allow_paths: 非空时，可检索/读取范围收紧到这些子路径之下。
        deny_rules: 显式 deny 规则（§8.3 / `PermissionContext.deny_rules`），
            始终生效，不受 `trusted` 影响。
        allow_rules: 显式 allow 规则（§8.3 / `PermissionContext.allow_rules`），
            仅在 `trusted=True` 时生效。
    """

    project_root: Path
    trusted: bool = False
    permission_mode: Literal["default", "plan", "auto_approve", "read_only"] = "default"
    enabled_domains: list[str] = Field(
        default_factory=lambda: ["core", "program", "map", "scene", "resource", "project"]
    )
    deny_read_paths: list[str] = Field(default_factory=lambda: [".git/", ".godot/"])
    deny_write_paths: list[str] = Field(
        default_factory=lambda: [".git/", ".godot/", "addons/"]
    )
    allow_paths: list[str] = Field(default_factory=list)
    deny_rules: list[PermRule] = Field(default_factory=list)
    allow_rules: list[PermRule] = Field(default_factory=list)


def security_settings_from_app(settings: AppSettings) -> SecuritySettings:
    """根据服务全局配置构造一份默认的安全边界配置。

    Args:
        settings: 服务全局配置（环境变量/`.env` 加载结果）。

    Returns:
        以 `settings.project_root` 为根、采用其权限模式与信任标记的安全边界配置。
    """
    security = SecuritySettings(
        project_root=settings.project_root.resolve(),
        trusted=settings.trusted_project,
        permission_mode=settings.permission_mode,
        deny_rules=settings.deny_rules,
        allow_rules=settings.allow_rules,
    )
    logger.info(
        "Security settings resolved project_root=%s permission_mode=%s trusted=%s deny_rules=%d allow_rules=%d",
        security.project_root,
        security.permission_mode,
        security.trusted,
        len(security.deny_rules),
        len(security.allow_rules),
    )
    return security
