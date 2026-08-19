"""Generic event-projection helpers shared by domain policies.

These functions project tool arguments and results into bounded, UI-safe
event payloads without any domain-specific knowledge.  They are used by
both the streaming and tool-dispatch paths.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EVENT_TEXT_PREVIEW_CHARS = 24_000
EVENT_MATCH_PREVIEW_ITEMS = 20


def emit_event(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Null-safe event dispatch — the canonical adapter for EventPort.publish."""
    if event_callback is None:
        return
    event_callback(event_type, payload)


def tool_args_for_event(args: dict[str, Any]) -> dict[str, Any]:
    """Return a small, UI-safe summary of tool arguments."""
    result: dict[str, Any] = {}
    for key in (
        "path",
        "target_path",
        "file_path",
        "script_path",
        "resource_path",
        "scene_path",
        "command",
        "kind",
        "agent",
        "task",
        "query",
    ):
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, str) and len(value) > 180:
            value = value[:180] + "..."
        result[key] = value
    return result


def result_count_for_event(result: Any, is_error: bool) -> int | None:
    """Best-effort extract the item count from a server tool result."""
    if is_error or not isinstance(result, dict):
        return None
    for key in ("matches", "files", "results"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def result_summary_for_event(
    tool_name: str,
    result: Any,
    is_error: bool,
) -> dict[str, Any] | None:
    """Return a bounded, UI-safe summary for workflow event rendering."""
    if is_error or not isinstance(result, dict):
        return None
    if tool_name in {"read_file", "read_script"}:
        content = result.get("content")
        if not isinstance(content, str):
            return None
        preview = content[:EVENT_TEXT_PREVIEW_CHARS]
        offset = result.get("offset", 1)
        line_start = offset if isinstance(offset, int) and offset > 0 else 1
        return {
            "kind": "read",
            "path": str(result.get("path", "")),
            "line_start": line_start,
            "line_end": max(line_start, line_start + len(content.splitlines()) - 1),
            "content": preview,
            "truncated": bool(result.get("truncated", False)) or len(content) > len(preview),
        }
    if tool_name in {"grep_code", "search_codebase", "list_files"}:
        matches = match_items_for_event(result)
        return {
            "kind": "grep",
            "pattern": str(result.get("pattern", result.get("query", ""))),
            "include": str(result.get("include", result.get("path", "project"))),
            "match_count": len(matches),
            "matches": matches[:EVENT_MATCH_PREVIEW_ITEMS],
            "truncated": bool(result.get("truncated", False))
            or len(matches) > EVENT_MATCH_PREVIEW_ITEMS,
        }
    if tool_name.startswith(("project.", "shell.", "godot.", "git.", "skill.", "tool.")):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        diff = str(data.get("diff", ""))
        return {
            "kind": "codeact",
            "task_execution_id": str(result.get("task_execution_id", "")),
            "audit_ref": (
                f"/codeact/audit/{result['task_execution_id']}"
                if result.get("task_execution_id")
                else ""
            ),
            "tool": tool_name,
            "status": str(result.get("status", "unknown")),
            "message": str(result.get("message", "")),
            "diff": diff[:EVENT_TEXT_PREVIEW_CHARS],
            "diff_truncated": len(diff) > EVENT_TEXT_PREVIEW_CHARS,
            "validation": data.get("validation", {}),
            "artifacts": list(result.get("artifacts", [])),
        }
    return None


def match_items_for_event(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize search-like result rows for the frontend workflow list."""
    raw_items = result.get("matches", result.get("results", result.get("files", [])))
    if not isinstance(raw_items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, dict):
            normalized.append(
                {
                    "path": str(item.get("path", item.get("file", ""))),
                    "line": item.get("line", item.get("line_no", "")),
                    "text": str(item.get("text", item.get("preview", ""))),
                }
            )
        else:
            normalized.append({"path": str(item), "line": "", "text": ""})
    return normalized
