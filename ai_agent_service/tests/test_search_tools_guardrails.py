"""tool.search 失败反馈与空转护栏测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.security.settings import SecuritySettings
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY
from app.tools.server_tools import register_server_tools
from app.tools.server_tools.search_tools import (
    EMPTY_STREAK_LIMIT,
    _EMPTY_MATCH_STREAK,
    search_tools_handler,
)


def _ctx(session_id: str = "guard-test", *, effective: frozenset[str] | None = None) -> ToolContext:
    return ToolContext(
        security=SecuritySettings(project_root=Path("/tmp/guard-test-project")),
        session_id=session_id,
        effective_tools=effective
        or frozenset({"project.read", "project.search", "git.status", "git.diff", "skill.load", "tool.search"}),
        agent_effective_tools=frozenset(),
        agent_role="coordinator",
    )


@pytest.fixture(autouse=True)
def _registry_and_streak():
    previous = REGISTRY.copy()
    _EMPTY_MATCH_STREAK.clear()
    try:
        REGISTRY.clear()
        register_server_tools()
        yield
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)
        _EMPTY_MATCH_STREAK.clear()


async def _search(query: str, ctx: ToolContext) -> dict:
    return await search_tools_handler({"query": query, "max_results": 12}, ctx)


# 一个在任何已注册工具的 name/domain/description/schema 里都不存在的查询词
_NO_TOOL_QUERY = "zzz-nonexistent-tool"


def test_empty_result_exposes_visible_tools_and_advisory() -> None:
    result = asyncio.run(_search(_NO_TOOL_QUERY, _ctx()))

    assert result["tools"] == []
    assert result["visible_tools"] == sorted(_ctx().effective_tools)
    assert isinstance(result["advisory"], str) and "未找到任何工具" in result["advisory"]
    assert result["search_stop"] is False


def test_repeated_empty_results_escalate_to_search_stop() -> None:
    ctx = _ctx()
    for _ in range(EMPTY_STREAK_LIMIT - 1):
        result = asyncio.run(_search(_NO_TOOL_QUERY, ctx))
        assert result["search_stop"] is False

    result = asyncio.run(_search(_NO_TOOL_QUERY, ctx))
    assert result["search_stop"] is True
    assert "search_stop" in result["advisory"]
    assert "禁止继续" in result["advisory"]


def test_successful_match_resets_the_streak() -> None:
    ctx = _ctx()
    for _ in range(EMPTY_STREAK_LIMIT - 1):
        asyncio.run(_search(_NO_TOOL_QUERY, ctx))

    hit = asyncio.run(_search("read", ctx))
    assert hit["tools"] != []
    assert hit["search_stop"] is False

    # 命中后重新搜索空词，计数从 1 开始：前两次仍非 hard stop
    first_after_reset = asyncio.run(_search(_NO_TOOL_QUERY, ctx))
    assert first_after_reset["search_stop"] is False
    second_after_reset = asyncio.run(_search(_NO_TOOL_QUERY, ctx))
    assert second_after_reset["search_stop"] is False
    third_after_reset = asyncio.run(_search(_NO_TOOL_QUERY, ctx))
    assert third_after_reset["search_stop"] is True


def test_streak_is_scoped_per_session_and_agent() -> None:
    other = _ctx(session_id="other-session")
    for _ in range(EMPTY_STREAK_LIMIT):
        asyncio.run(_search(_NO_TOOL_QUERY, other))
    assert other.session_id != _ctx().session_id

    fresh = _ctx()
    result = asyncio.run(_search(_NO_TOOL_QUERY, fresh))
    assert result["search_stop"] is False


def test_hidden_registry_match_is_not_counted_as_empty() -> None:
    """工具存在但不在可见集（hidden 命中）时返回 unavailable，不触发护栏。"""
    ctx = _ctx()
    result = asyncio.run(_search("create_plan", ctx))

    assert result["tools"] == []
    assert result["advisory"] is None
    assert result["search_stop"] is False
    names = {entry["name"] for entry in result["unavailable_tools"]}
    assert "create_plan" in names and "delegate_many" in names