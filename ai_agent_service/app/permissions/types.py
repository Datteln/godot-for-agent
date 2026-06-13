"""权限规则数据模型（§8.3 / 详设 B §3.4）。

`PermRule` 单独成模块，使其可被 `app.config`/`app.security.settings` 直接
引用而不触发 `app.tools.registry` -> `app.tools.context` ->
`app.security.settings` 的循环依赖；匹配逻辑见 `app.permissions.rules`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PermRule(BaseModel):
    """单条权限规则：按工具名/域/路径 glob 匹配，命中后产出固定 effect。

    Attributes:
        match: 匹配条件，支持的键为 `tool`（工具名）、`domain`（工具域）、
            `path_glob`（与工具 `path_args` 中任一路径参数值做 glob 匹配）。
            未出现的键视为「不限制」。
        effect: 命中后产出的决策。
    """

    match: dict[str, str] = Field(default_factory=dict)
    effect: Literal["allow", "ask", "deny"]
