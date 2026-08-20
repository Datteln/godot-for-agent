"""`search_tools`：按需检索并激活 deferred 工具 schema。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, register

MAX_RESULTS = 12

# 连续空匹配的护栏阈值：达到该次数后在结果中返回 search_stop 硬指令，
# 防止 agent 因规则性提示词（如“必须先 create_plan”）对不存在的工具无限换词重试。
EMPTY_STREAK_LIMIT = 3

logger = logging.getLogger(__name__)

# 进程内护栏状态：key=(session_id, agent_role)，只在服务进程生命周期有效。
_EMPTY_MATCH_STREAK: dict[tuple[str, str], int] = {}


def _guardrail_key(ctx: ToolContext) -> tuple[str, str]:
    return (ctx.session_id, ctx.agent_role or "unknown")


def _empty_match_advisory(search_stop: bool, visible_tools: list[str]) -> str:
    if search_stop:
        return (
            "search_stop: 你已连续多次搜索不到任何工具，禁止继续换词搜索。"
            "当前可见工具只有：" + "、".join(visible_tools) + "。"
            "请直接使用这些工具完成可以完成的部分，并向用户说明缺失的工具/能力。"
        )
    return (
        "未找到任何工具（可见工具与全局注册表均无匹配），继续搜索不会得到新结果。"
        "当前可见工具：" + "、".join(visible_tools) + "。请改用现有工具或向用户说明。"
    )

SEARCH_TOOLS_SCHEMA: dict[str, Any] = {
    "name": "search_tools",
    "description": (
        "Search available tool schemas by name/domain/hint. "
        "Deferred tools returned by this command become callable on the next assistant turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": "Maximum tools to return; clamped by the service.",
            },
        },
        "required": ["query"],
    },
}


def _description(tool: ToolDef) -> str:
    """提取工具 schema 中的简短描述。"""
    value = tool.schema.get("description", "")
    return value if isinstance(value, str) else ""


def _score(tool: ToolDef, query: str) -> int:
    """按名称、域、hint 与 schema 描述给工具做简单词法打分。"""
    tokens = [token for token in query.lower().split() if token]
    haystack = " ".join(
        [
            tool.name,
            tool.domain,
            tool.search_hint or "",
            _description(tool),
            json.dumps(tool.schema, ensure_ascii=False, sort_keys=True),
        ]
    ).lower()
    score = 0
    for token in tokens:
        if token == tool.name.lower():
            score += 20
        if token in tool.name.lower():
            score += 10
        if token in haystack:
            score += 2
    return score


async def search_tools_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """检索当前 agent 可见的工具，并返回可注入下一轮的 schema。"""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空")
    max_results_raw = args.get("max_results", MAX_RESULTS)
    if not isinstance(max_results_raw, int) or max_results_raw <= 0:
        raise ValueError("max_results 必须是正整数")
    max_results = min(max_results_raw, MAX_RESULTS)

    visible = set(ctx.effective_tools) if ctx.effective_tools else set(REGISTRY)
    logger.info(
        "search_tools start session=%s visible=%d max_results=%d query_length=%d",
        ctx.session_id,
        len(visible),
        max_results,
        len(query),
    )
    ranked: list[tuple[int, ToolDef]] = []
    for name in visible:
        tool = REGISTRY.get(name)
        if tool is None:
            continue
        score = _score(tool, query)
        if score > 0:
            ranked.append((score, tool))
    ranked.sort(key=lambda item: (-item[0], item[1].name))

    unavailable: list[dict[str, Any]] = []
    if ctx.effective_tools:
        hidden = [
            (score, tool)
            for tool in REGISTRY.values()
            if tool.name not in visible and (score := _score(tool, query)) > 0
        ]
        hidden.sort(key=lambda item: (-item[0], item[1].name))
        unavailable = []
        for _, tool in hidden[:max_results]:
            excluded_by_stage = (
                ctx.workflow_stage is not None and tool.name in ctx.agent_effective_tools
            )
            unavailable.append(
                {
                    "name": tool.name,
                    "status": (
                        "unavailable_in_current_stage"
                        if excluded_by_stage
                        else "unavailable_in_agent_scope"
                    ),
                    "reason": (
                        "excluded_by_workflow_stage"
                        if excluded_by_stage
                        else "excluded_by_agent_scope"
                    ),
                    "current_stage": ctx.workflow_stage,
                    "requires_user_approval": False,
                }
            )

    matches = []
    activated: list[str] = []
    for _, tool in ranked[:max_results]:
        if tool.deferred:
            activated.append(tool.name)
        matches.append(
            {
                "name": tool.name,
                "domain": tool.domain,
                "side": tool.side,
                "deferred": tool.deferred,
                "description": _description(tool),
                "search_hint": tool.search_hint,
                "schema": tool.schema,
            }
        )

    visible_tools = sorted(visible)
    all_empty = len(ranked) == 0 and not unavailable
    key = _guardrail_key(ctx)
    search_stop = False
    if all_empty:
        streak = _EMPTY_MATCH_STREAK.get(key, 0) + 1
        _EMPTY_MATCH_STREAK[key] = streak
        search_stop = streak >= EMPTY_STREAK_LIMIT
    else:
        _EMPTY_MATCH_STREAK.pop(key, None)

    logger.info(
        "search_tools success session=%s matches=%d activated=%d visible=%d "
        "empty_streak=%s search_stop=%s",
        ctx.session_id,
        len(matches),
        len(activated),
        len(visible_tools),
        _EMPTY_MATCH_STREAK.get(key, 0) if all_empty else 0,
        search_stop,
    )
    return {
        "query": query,
        "tools": matches,
        "activated_tools": activated,
        "unavailable_tools": unavailable,
        "visible_tools": visible_tools,
        "advisory": (
            _empty_match_advisory(search_stop, visible_tools) if all_empty else None
        ),
        "search_stop": search_stop,
        "note": (
            "activated_tools 会在下一轮对话中加入当前 agent 的工具 schema。"
            "unavailable_tools 是当前阶段或 agent 范围裁剪结果，不是用户待批准权限。"
        ),
    }


def register_search_tools_tool() -> None:
    """把 `search_tools` 注册进全局工具表。"""
    register(
        ToolDef(
            name="search_tools",
            domain="core",
            side="server",
            is_read_only=True,
            is_concurrency_safe=True,
            schema=SEARCH_TOOLS_SCHEMA,
            handler=search_tools_handler,
        )
    )
