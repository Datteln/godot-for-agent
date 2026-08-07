"""Generic bounded server-tool execution shared by domain policies."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import ToolDef

logger = logging.getLogger(__name__)
ToolResult = tuple[Any, bool]


@dataclass(frozen=True, slots=True)
class ServerToolCall:
    """A validated server-side invocation with stable response identity."""

    call_id: str
    tool: ToolDef
    arguments: dict[str, Any]


async def invoke_server_tool(
    tool: ToolDef,
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Execute one handler and convert runtime failures into typed tool results."""
    assert tool.handler is not None
    started = time.perf_counter()
    logger.info(
        "Server tool start session=%s tool=%s domain=%s path_args=%s",
        context.session_id,
        tool.name,
        tool.domain,
        [name for name in tool.all_path_args if name in arguments],
    )
    try:
        result = await tool.handler(arguments, context)
        logger.info(
            "Server tool success session=%s tool=%s elapsed_ms=%d",
            context.session_id,
            tool.name,
            int((time.perf_counter() - started) * 1000),
        )
        return result, False
    except Exception as exc:
        logger.exception(
            "Server tool failed session=%s tool=%s elapsed_ms=%d",
            context.session_id,
            tool.name,
            int((time.perf_counter() - started) * 1000),
        )
        return {
            "error": str(exc),
            "error_code": "server_tool_exception",
            "disposition": "continue_agent",
            "retryable": True,
            "side_effect_state": "none",
            "next_action": {"action": "agent_correct_or_replace_tool_call"},
        }, True


async def execute_server_tools(
    calls: list[ServerToolCall],
    context: ToolContext,
    *,
    on_start: Callable[[ServerToolCall, bool], None] | None = None,
    on_result: Callable[[ServerToolCall, ToolResult], None] | None = None,
) -> dict[str, ToolResult]:
    """Execute safe calls concurrently and unsafe calls sequentially, preserving identity."""
    concurrent = [call for call in calls if call.tool.is_concurrency_safe]
    sequential = [call for call in calls if not call.tool.is_concurrency_safe]
    results: dict[str, ToolResult] = {}
    if concurrent:
        for call in concurrent:
            if on_start is not None:
                on_start(call, True)
        outcomes = await asyncio.gather(
            *(invoke_server_tool(call.tool, call.arguments, context) for call in concurrent)
        )
        for call, outcome in zip(concurrent, outcomes):
            results[call.call_id] = outcome
            if on_result is not None:
                on_result(call, outcome)
    for call in sequential:
        if on_start is not None:
            on_start(call, False)
        outcome = await invoke_server_tool(call.tool, call.arguments, context)
        results[call.call_id] = outcome
        if on_result is not None:
            on_result(call, outcome)
    return results
