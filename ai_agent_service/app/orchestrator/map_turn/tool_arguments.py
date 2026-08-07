"""解析并规范化 Map 工具参数。"""

from __future__ import annotations

import json
from typing import Any

from app.orchestrator.map_turn.contracts import (
    _INTEGER_TEXT,
    _NUMBER_TEXT,
    _tool_message,
    logger,
)
from app.tools.registry import REGISTRY


def _coerce_schema_value(value: Any, schema: dict[str, Any]) -> tuple[Any, bool]:
    """按工具 schema 安全转换模型字符串化的 JSON 值。"""
    expected_type = schema.get("type")
    normalized = value
    changed = False
    if isinstance(value, str):
        stripped = value.strip()
        if expected_type == "integer" and _INTEGER_TEXT.fullmatch(stripped):
            normalized = int(stripped)
            changed = True
        elif expected_type == "number" and _NUMBER_TEXT.fullmatch(stripped):
            normalized = float(stripped)
            changed = True
        elif expected_type == "boolean" and stripped.lower() in {"true", "false"}:
            normalized = stripped.lower() == "true"
            changed = True
        elif expected_type in {"array", "object"}:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if (expected_type == "array" and isinstance(parsed, list)) or (
                expected_type == "object" and isinstance(parsed, dict)
            ):
                normalized = parsed
                changed = True

    if expected_type == "object" and isinstance(normalized, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            result = dict(normalized)
            for key, child_schema in properties.items():
                if key not in result or not isinstance(child_schema, dict):
                    continue
                child_value, child_changed = _coerce_schema_value(result[key], child_schema)
                if child_changed:
                    result[key] = child_value
                    changed = True
            normalized = result
    elif expected_type == "array" and isinstance(normalized, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            result_items: list[Any] = []
            for item in normalized:
                child_value, child_changed = _coerce_schema_value(item, item_schema)
                result_items.append(child_value)
                changed = changed or child_changed
            normalized = result_items
    return normalized, changed


def _normalize_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """使用已注册工具 schema 规范化模型生成的入参。"""
    tool = REGISTRY.get(tool_name)
    if tool is None:
        return args
    parameters = tool.schema.get("parameters")
    if not isinstance(parameters, dict):
        return args
    normalized, changed = _coerce_schema_value(args, parameters)
    if changed and isinstance(normalized, dict):
        logger.info("Normalized tool arguments from schema tool=%s", tool_name)
        return normalized
    return args


def _load_tool_args(
    call_id: str, arguments: str, tool_name: str = ""
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解析工具入参 JSON，返回 `(args, error_message)` 二元组。"""
    try:
        loaded = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("Tool arguments JSON parse failed call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参不是合法 JSON", is_error=True)
    if not isinstance(loaded, dict):
        logger.warning("Tool arguments are not an object call_id=%s", call_id)
        return None, _tool_message(call_id, "工具入参必须是 JSON object", is_error=True)
    return _normalize_tool_args(tool_name, loaded), None
