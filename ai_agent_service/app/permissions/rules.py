"""权限规则引擎（§8.3 / 详设 B §3.4）。

`PermRule`（定义见 `app.permissions.types`）按 工具名 / 域 / 路径 glob 匹配
工具调用，产出 `allow`/`ask`/`deny` 三态中的一种。规则本身不决定最终结果——
`permissions/engine.py` 的 `check()` 按既定优先级（deny 优先）消费匹配结果。
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any, Literal

from app.permissions.types import PermRule as PermRule
from app.tools.registry import ToolDef


def _rule_matches(rule: PermRule, tool: ToolDef, args: dict[str, Any]) -> bool:
    """判断单条规则是否命中本次工具调用。

    Args:
        rule: 待校验的权限规则。
        tool: 当前调用的工具定义。
        args: 本次调用的入参字典。

    Returns:
        规则的所有限定条件均满足时返回 True；规则未声明任何条件视为全匹配。
    """
    tool_pattern = rule.match.get("tool")
    if tool_pattern is not None and not fnmatch(tool.name, tool_pattern):
        return False

    domain_pattern = rule.match.get("domain")
    if domain_pattern is not None and not fnmatch(tool.domain, domain_pattern):
        return False

    path_glob = rule.match.get("path_glob")
    if path_glob is not None:
        candidates = [args[name] for name in tool.path_args if name in args]
        if not any(fnmatch(str(value), path_glob) for value in candidates):
            return False

    return True


def match_rules(
    rules: list[PermRule],
    tool: ToolDef,
    args: dict[str, Any],
    effect: Literal["allow", "ask", "deny"],
) -> bool:
    """判断是否存在某条 effect 为目标值且命中本次调用的规则。

    Args:
        rules: 待匹配的规则列表（通常为 deny 规则或 allow 规则）。
        tool: 当前调用的工具定义。
        args: 本次调用的入参字典。
        effect: 只考虑该 effect 的规则。

    Returns:
        存在命中的规则则返回 True。
    """
    return any(rule.effect == effect and _rule_matches(rule, tool, args) for rule in rules)
