"""`search_tools`：按需检索并激活 deferred 工具 schema。"""

from __future__ import annotations

import json
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, register

MAX_RESULTS = 12

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
    ranked: list[tuple[int, ToolDef]] = []
    for name in visible:
        tool = REGISTRY.get(name)
        if tool is None:
            continue
        score = _score(tool, query)
        if score > 0:
            ranked.append((score, tool))
    ranked.sort(key=lambda item: (-item[0], item[1].name))

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

    return {
        "query": query,
        "tools": matches,
        "activated_tools": activated,
        "note": "activated_tools 会在下一轮对话中加入当前 agent 的工具 schema。",
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
