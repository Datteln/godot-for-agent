"""权限闸（§8 权限系统 / 详设 B §3.2）。

`check()` 是每个工具调用在执行/返回前必经的统一校验入口，按固定优先级
（先命中先返回，deny 永远优先）求值：

1. 安全硬闸 `path_ok`（越界路径一律 `deny`，不可绕过）
2. 工具域是否在 `enabled_domains` 内
3. 工具是否在当前 agent 的 `effective_tools` 可见集合内
4. 显式 deny 规则
5. 受信任前提下的显式 allow 规则
6. 会话级"总是允许"授权
7. 权限模式默认值（§8.2/详设 B §3.3）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.permissions.rules import PermRule, match_rules
from app.security.paths import all_paths_ok
from app.security.settings import SecuritySettings
from app.tools.registry import ToolDef

Decision = Literal["allow", "ask", "deny"]

logger = logging.getLogger(__name__)

# 会话级"总是允许"授权的粒度：tool + domain + effect（详设 A §3.6）。
# 不含具体路径——按文件路径精确授权时，用户勾选一次"自动允许相似低风险更改"
# 后，换一个文件名又得再问一遍，体验上等同于这个授权从没生效过；同一个工具
# 名称本身已经是足够安全的授权粒度（不同工具各自独立授权，互不放宽）。
SessionAllowGrant = tuple[str, str, str]


@dataclass
class PermissionContext:
    """单次权限校验所需的上下文。

    Attributes:
        security: 当前会话的安全边界配置（含 `permission_mode`/`trusted`/
            `enabled_domains` 等）。
        effective_tools: 当前活跃 agent 帧裁剪后的可见工具名集合。
        deny_rules: 显式 deny 规则（始终生效，不受信任状态影响）。
        allow_rules: 显式 allow 规则（仅受信任工程生效）。
        session_allow: 本会话内"总是允许"的授权集合，元素为
            `(tool, domain, effect)`。
    """

    security: SecuritySettings
    effective_tools: frozenset[str]
    deny_rules: list[PermRule] = field(default_factory=list)
    allow_rules: list[PermRule] = field(default_factory=list)
    session_allow: set[SessionAllowGrant] = field(default_factory=set)


def _effect_of(tool: ToolDef) -> str:
    """把工具的 effect 元数据折算为会话授权粒度中的单一 effect 标签。

    Args:
        tool: 工具定义。

    Returns:
        `execute_project` / `write_project` / `network` / `read_project`
        中的一个，按风险从高到低优先判定。
    """
    if tool.executes_process:
        return "execute_project"
    if tool.writes_project:
        return "write_project"
    if tool.uses_network:
        return "network"
    return "read_project"


def _session_allow_match(ctx: PermissionContext, tool: ToolDef) -> bool:
    """判断本次调用是否命中会话级"总是允许"授权。

    Args:
        ctx: 当前权限上下文。
        tool: 工具定义。

    Returns:
        命中则返回 True。
    """
    return make_session_allow_grant(tool) in ctx.session_allow


def make_session_allow_grant(tool: ToolDef) -> SessionAllowGrant:
    """构造该工具对应的会话级"总是允许"授权键。

    Args:
        tool: 工具定义。

    Returns:
        `(tool.name, tool.domain, effect)` 三元组；不含具体路径，因此对
        同一工具的任意一次调用授权后，本会话内该工具的后续调用都会命中。
    """
    return (tool.name, tool.domain, _effect_of(tool))


def _default_mode(tool: ToolDef, ctx: PermissionContext) -> Decision:
    """`default` 模式：只读 `allow`，改动型 `ask`。"""
    return "ask" if tool.mutating else "allow"


def _plan_mode(tool: ToolDef, ctx: PermissionContext) -> Decision:
    """`plan` 模式：只读 `allow`，改动型一律 `deny`（只规划不动手）。"""
    return "deny" if tool.mutating else "allow"


def _auto_approve_mode(tool: ToolDef, ctx: PermissionContext) -> Decision:
    """`auto_approve` 模式：受信任工程下改动型也 `allow`，否则降级为 `ask`。"""
    if tool.mutating and not ctx.security.trusted:
        return "ask"
    return "allow"


def _read_only_mode(tool: ToolDef, ctx: PermissionContext) -> Decision:
    """`read_only` 模式：只读 `allow`，改动型一律 `deny`（MCP 入口默认）。"""
    return "deny" if tool.mutating else "allow"


_MODE_HANDLERS: dict[str, Callable[[ToolDef, PermissionContext], Decision]] = {
    "default": _default_mode,
    "plan": _plan_mode,
    "auto_approve": _auto_approve_mode,
    "read_only": _read_only_mode,
}


def check(tool: ToolDef, args: dict[str, Any], ctx: PermissionContext) -> Decision:
    """对一次工具调用做出 `allow`/`ask`/`deny` 决策。

    Args:
        tool: 待调用的工具定义。
        args: 本次调用的入参（已 `json.loads` 的 dict）。
        ctx: 当前会话/帧的权限上下文。

    Returns:
        三态决策之一；`deny` 表示不执行，`ask` 表示 front 改动型工具需前端
        预览确认，`allow` 表示可直接执行/静默返回前端执行。
    """
    legacy_read_path_args = [] if tool.writes_project else tool.path_args
    legacy_write_path_args = tool.path_args if tool.writes_project else []
    read_path_args = [*legacy_read_path_args, *tool.read_path_args]
    write_path_args = [*legacy_write_path_args, *tool.write_path_args]
    if not all_paths_ok(args, read_path_args, ctx.security, write=False):
        logger.debug("Permission deny tool=%s reason=read_path_boundary", tool.name)
        return "deny"
    if not all_paths_ok(args, write_path_args, ctx.security, write=True):
        logger.debug("Permission deny tool=%s reason=path_boundary", tool.name)
        return "deny"
    if tool.domain not in ctx.security.enabled_domains:
        logger.debug("Permission deny tool=%s reason=disabled_domain domain=%s", tool.name, tool.domain)
        return "deny"
    if tool.name not in ctx.effective_tools:
        logger.debug("Permission deny tool=%s reason=not_effective_tool", tool.name)
        return "deny"
    if match_rules(ctx.deny_rules, tool, args, "deny"):
        logger.debug("Permission deny tool=%s reason=deny_rule", tool.name)
        return "deny"
    if ctx.security.trusted and match_rules(ctx.allow_rules, tool, args, "allow"):
        logger.debug("Permission allow tool=%s reason=allow_rule", tool.name)
        return "allow"
    if _session_allow_match(ctx, tool):
        logger.debug("Permission allow tool=%s reason=session_allow", tool.name)
        return "allow"
    decision = _MODE_HANDLERS[ctx.security.permission_mode](tool, ctx)
    logger.debug(
        "Permission decision tool=%s mode=%s mutating=%s trusted=%s decision=%s",
        tool.name,
        ctx.security.permission_mode,
        tool.mutating,
        ctx.security.trusted,
        decision,
    )
    return decision
