"""Generic tool-call classification and protocol parsing.

Domain policies use these helpers to parse, validate, and classify tool
calls from model responses.  All functions are domain-free: they know
nothing about Map tools, stages, or budgets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.tools.registry import REGISTRY, ToolDef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingToolMessage:
    """A pre-resolved tool message (unknown tool / arg error / permission deny)."""

    message: dict[str, Any]


@dataclass(frozen=True)
class PendingServerCall:
    """A validated server-side call awaiting execution."""

    call_id: str
    tool: ToolDef
    args: dict[str, Any]


PendingItem = PendingToolMessage | PendingServerCall


def tool_message(
    tool_call_id: str,
    result: Any,
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    """Construct an OpenAI ``role=tool`` message for ``frame.messages``."""
    if is_error:
        content = result if isinstance(result, str) else json.dumps(
            {"error": str(result)}, ensure_ascii=False
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }
    content = result if isinstance(result, str) else json.dumps(
        result, ensure_ascii=False
    )
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


def load_tool_args(
    call_id: str,
    arguments: str,
    tool_name: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse tool-argument JSON; return ``(args, error_message)``.

    On parse failure, ``args`` is ``None`` and ``error_message`` is a
    ready-to-append tool message.  On success, ``error_message`` is ``None``
    and ``args`` is the normalized argument dict.
    """
    try:
        loaded = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("Tool arguments JSON parse failed call_id=%s", call_id)
        return None, tool_message(call_id, "工具入参不是合法 JSON", is_error=True)
    if not isinstance(loaded, dict):
        logger.warning("Tool arguments are not an object call_id=%s", call_id)
        return None, tool_message(call_id, "工具入参必须是 JSON object", is_error=True)
    return _normalize_tool_args(tool_name, loaded), None


def _normalize_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Normalize model-generated arguments using the registered tool schema."""
    tool = REGISTRY.get(tool_name)
    if tool is None:
        return args
    normalized: dict[str, Any] = {}
    for field_def in tool.parameters:
        key = field_def["name"]
        if key in args:
            normalized[key] = args[key]
        elif field_def.get("default") is not None:
            normalized[key] = field_def["default"]
    # Pass through any extra keys the model provides
    for key, value in args.items():
        if key not in normalized:
            normalized[key] = value
    return normalized
