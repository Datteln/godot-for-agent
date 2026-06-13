"""`search_codebase`：工程内受限的词法 RAG 检索。"""

from __future__ import annotations

from typing import Any

from app.rag.index import CodebaseIndex, validate_glob
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

MAX_RESULTS = 16

SEARCH_CODEBASE_SCHEMA: dict[str, Any] = {
    "name": "search_codebase",
    "description": (
        "Search project code/resources for semantically related snippets using a local lexical RAG fallback. "
        "Use when grep is too exact or when looking for concepts across files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language or keyword query."},
            "include": {
                "type": "string",
                "description": "Relative glob, default '**/*'. Example: '**/*.gd'.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum snippets to return; clamped by the service.",
            },
        },
        "required": ["query"],
    },
}


async def search_codebase_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """在工程内做本地词法检索并返回相关代码片段。"""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空")

    include = args.get("include", "**/*")
    if not isinstance(include, str) or not include:
        raise ValueError("include 必须是非空字符串")
    validate_glob(include)

    max_results_raw = args.get("max_results", MAX_RESULTS)
    if not isinstance(max_results_raw, int) or max_results_raw <= 0:
        raise ValueError("max_results 必须是正整数")
    max_results = min(max_results_raw, MAX_RESULTS)

    return CodebaseIndex(ctx.security, ctx.rag_index_path).search(query, include, max_results)


def register_search_codebase_tool() -> None:
    """把 `search_codebase` 注册进全局工具表。"""
    register(
        ToolDef(
            name="search_codebase",
            domain="project",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            deferred=True,
            search_hint="按自然语言或关键词检索工程代码片段，RAG 词法 fallback",
            schema=SEARCH_CODEBASE_SCHEMA,
            handler=search_codebase_handler,
        )
    )
