"""会话历史块装配：消息/事件/工具历史到前端块。"""

from __future__ import annotations

import hashlib
import json
from ._text_utils import _COMPACT_SUMMARY_MAX_CHARS
from .map_deferral import _unknown_tool_result_summary
from .message_utils import _brief_message, _display_user_content
from .tool_summary import (
    _compact_tool_summary,
    _format_create_plan_history_summary,
    _format_delegate_group_history_summary,
    _format_delegate_history_summary,
    _front_tool_summary,
    _looks_like_create_plan_result,
    _looks_like_delegate_group_result,
    _looks_like_delegate_result,
)
from app.agents.types import CompactSnapshot, Frame
from app.api.schemas import (
    DelegateResultDTO,
    DelegateResultHistoryBlock,
    DelegateResultsHistoryBlock,
    ErrorHistoryBlock,
    EventHistoryBlock,
    GrepMatchDTO,
    LogEditHistoryBlock,
    LogGrepHistoryBlock,
    LogReadHistoryBlock,
    LogTextHistoryBlock,
    NodeTreeHistoryBlock,
    PlanCreatedHistoryBlock,
    PlanStepDTO,
    SessionHistoryBlock,
    StepCompletedHistoryBlock,
    StepStartedHistoryBlock,
    SystemTextHistoryBlock,
    ThoughtHistoryBlock,
    UserHistoryBlock,
    VerifyOutcomeHistoryBlock,
    VerifyStartedHistoryBlock,
)
from app.events.store import Event
from app.llm.message_transformer import estimate_message_tokens, flatten_message_text
from app.sessions.store import Session
from dataclasses import replace
from typing import Any
# 与 chat_panel.gd 中 `_TOOL_DISPLAY_NAMES`/`_format_log_tool_result` 的分组保持一致，
# 使会话历史里的工具结果摘要能复用前端既有的 "Read"/"Edit"/"Grep" 工作流分组渲染。
_HISTORY_READ_TOOLS = frozenset({"read_file", "read_script"})


_HISTORY_EDIT_TOOLS = frozenset(
    {
        "write_file",
        "propose_script_edit",
        "apply_text_edit",
        "propose_tests",
        "propose_content_file",
    }
)


_HISTORY_GREP_TOOLS = frozenset({"grep_code", "search_codebase", "list_files"})


# 前端工具（在 Godot 编辑器侧执行）不会返回纯文本内容，用短描述摘要替代 JSON 转储
_HISTORY_FRONT_READ_TOOLS = frozenset(
    {
        "read_scene_tree",
        "read_runtime_state",
        "read_profiler_snapshot",
        "read_debugger_errors",
        "capture_viewport_screenshot",
        "read_image_metadata",
        "read_class_docs",
        "describe_tilemap_selection",
        "describe_map_context",
        "plan_map_layout",
        "plan_map_algorithms",
        "validate_platform_level_plan",
        "plan_reachable_map_growth",
        "compute_reachable_frontier",
        "describe_map_region",
        "query_spatial_index",
        "find_placement_anchors",
        "validate_object_placements",
        "validate_layer_coverage",
        "validate_map_region",
        "sample_noise_grid",
        "sample_poisson_points",
        "compose_map_blueprint_grammar",
        "validate_scene_state",
    }
)


_HISTORY_FRONT_SCENE_EDIT_TOOLS = frozenset(
    {
        "add_node",
        "set_node_property",
        "delete_node",
        "reparent_node",
        "rename_node",
        "open_scene",
        "instance_scene",
        "duplicate_node",
        "connect_signal",
        "disconnect_signal",
        "add_to_group",
        "remove_from_group",
        "save_scene",
        "bake_navigation_mesh",
        "create_animation_track",
        "edit_map",
        "paint_terrain_connect",
        "place_map_objects",
        "repair_placements",
        "repair_layer_coverage",
        "repair_map_region",
        "compact_spatial_index",
        "write_resource_registry",
        "save_map_blueprint",
        "apply_map_blueprint",
        "ensure_standard_map_layers",
    }
)


_HISTORY_FRONT_RUN_TOOLS = frozenset(
    {
        "run_tests",
        "run_headless_self_test",
        "run_system_command",
        "execute_gd_script",
        "export_project",
    }
)


_HISTORY_FRONT_TOOLS = (
    _HISTORY_FRONT_READ_TOOLS | _HISTORY_FRONT_SCENE_EDIT_TOOLS | _HISTORY_FRONT_RUN_TOOLS
)


_PERSISTED_HISTORY_EVENT_TYPES = frozenset(
    {
        "agent_reasoning_delta",
        "agent_text_delta",
        "agent_model_fallback",
        "cache_hit",
        "compact_boundary",
        "compact_started",
        "config_changed",
        "plan_created",
        "plan_step_started",
        "plan_step_completed",
        "verify_started",
        "verify_completed",
        "delegate_start",
        "error",
        "pending_discarded",
        "reset",
        "server_tool_start",
        "server_tool_result",
        "context_usage",
        "turn_interrupted",
        "user_submitted",
    }
)


_GENERIC_HISTORY_EVENT_TYPES = frozenset(
    {
        "agent_model_fallback",
        "cache_hit",
        "compact_boundary",
        "compact_started",
        "config_changed",
        "error",
        "pending_discarded",
        "reset",
        "server_tool_start",
        "turn_interrupted",
        "user_submitted",
    }
)


def _count_lines(text: str) -> int:
    # 统计文本行数；空字符串视为 0 行。
    if text == "":
        return 0
    return len(text.splitlines())


def _parse_tool_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    # 解析 `tool_calls[].function.arguments`（JSON 字符串或已是字典）为入参字典。
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return {}
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_tool_result_summary(name: str, input_args: dict[str, Any], content: str) -> str:
    """把一次工具结果压缩为与实时模式一致的简短摘要行。

    与 `chat_panel.gd` 的 `_format_log_tool_result` 保持同构：读取类工具展示
    `Read <path> (lines 1-N)`，写入/编辑类工具展示 `Edit <path>\\n+N -M lines`，
    检索类工具展示 `Grep "<pattern>" (in project)`，使前端能复用既有的工作流
    分组渲染，而不是把完整结果 JSON（如整份文件内容）堆进会话历史。

    Args:
        name: 工具名（如 `read_file`）；找不到对应 tool_call 时为空字符串。
        input_args: 对应工具调用的入参字典；找不到时为空字典。
        content: 工具结果消息的原始 `content`（通常是 JSON 字符串）。

    Returns:
        适合直接展示在会话历史中的摘要文本。
    """
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    inner: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

    error_message = inner.get("error")
    if isinstance(error_message, str):
        return f"{name}: {error_message}"

    if name == "create_plan" or _looks_like_create_plan_result(inner):
        return _format_create_plan_history_summary(input_args, inner)

    if name == "delegate_many" or _looks_like_delegate_group_result(inner):
        return _format_delegate_group_history_summary(inner)

    if name == "delegate" or _looks_like_delegate_result(inner):
        return _format_delegate_history_summary(inner)

    if name in _HISTORY_READ_TOOLS:
        path = str(inner.get("path", input_args.get("path", "<unknown>")))
        offset = int(inner.get("offset", 1) or 1)
        line_count = _count_lines(str(inner.get("content", "")))
        line_end = offset + line_count - 1
        return f"Read {path} (lines {offset}-{max(line_end, offset)})"

    if name == "apply_text_edit":
        path = str(inner.get("path", input_args.get("path", "<unknown>")))
        old_string = str(input_args.get("old_string", ""))
        new_string = str(input_args.get("new_string", ""))
        replaced = int(inner.get("replaced_count", 1) or 1)
        added = _count_lines(new_string) * replaced
        removed = _count_lines(old_string) * replaced
        return f"Edit {path}\n+{added} -{removed} lines"

    if name in _HISTORY_EDIT_TOOLS:
        path = str(
            inner.get("path", input_args.get("path", input_args.get("target_path", "<unknown>")))
        )
        after_text = str(input_args.get("content", input_args.get("after_text", "")))
        before_text = str(input_args.get("before_text", input_args.get("before", "")))
        added = max(_count_lines(after_text) - _count_lines(before_text), 0)
        removed = max(_count_lines(before_text) - _count_lines(after_text), 0)
        return f"Edit {path}\n+{added} -{removed} lines"

    if name in _HISTORY_GREP_TOOLS:
        pattern = str(
            input_args.get("pattern", input_args.get("query", input_args.get("include", "")))
        )
        escaped_pattern = pattern.replace('"', '\\"')
        return f'Grep "{escaped_pattern}" (in project)'

    return _compact_tool_summary(name, inner, input_args)


def _is_internal_history_message(message: dict[str, Any]) -> bool:
    """识别不应回放为聊天消息的服务内部恢复指令。"""
    if bool(message.get("internal", False)):
        return True
    if str(message.get("role", "")) != "user":
        return False
    content = message.get("content", "")
    text = flatten_message_text(content) if isinstance(content, list) else str(content)
    return text.startswith(
        (
            "MAP_COMPLETION_GATE_BLOCKED",
            "出错：自动读取没有拿到需要的 state",
            "出错：自动 describe_map_region 请求超过单轴读取上限",
        )
    )


def _merged_stream_event(events: list[Event]) -> Event | None:
    """把同一段流式事件合并为一个可回放的完整文本事件。"""
    if not events:
        return None
    text_parts: list[str] = []
    selected = events[0]
    for event in sorted(events, key=lambda item: item.seq):
        selected = event
        text = str(event.payload.get("text", ""))
        if bool(event.payload.get("append_delta", False)):
            text_parts.append(text)
        else:
            text_parts = [text]
    return replace(selected, payload={**selected.payload, "text": "".join(text_parts)})


def _json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON object, returning an empty object for non-object content."""
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _history_origin(frame: Frame) -> dict[str, str]:
    return {"frame_id": frame.id, "agent": frame.agent.name}


def _assistant_history_blocks(
    frame: Frame,
    text: str,
    *,
    has_tool_calls: bool,
    include_thought_summary: bool = True,
) -> list[SessionHistoryBlock]:
    """Split a stored assistant message without treating its final body as reasoning."""
    stripped = text.strip()
    if not stripped:
        return []
    origin = _history_origin(frame)
    if has_tool_calls and not stripped.startswith("Thought:"):
        return []
    if not stripped.startswith("Thought:"):
        return [
            LogTextHistoryBlock(
                text=stripped,
                marker=has_tool_calls,
                indent=not has_tool_calls,
                **origin,
            )
        ]

    first_line, separator, remainder = stripped.partition("\n")
    summary = first_line.removeprefix("Thought:").strip()
    blocks: list[SessionHistoryBlock] = (
        [ThoughtHistoryBlock(**origin)] if include_thought_summary else []
    )
    if summary and include_thought_summary:
        blocks.append(LogTextHistoryBlock(text=summary, marker=True, **origin))
    final_text = remainder.strip() if separator else ""
    if final_text:
        blocks.append(LogTextHistoryBlock(text=final_text, indent=True, **origin))
    return blocks


def _grep_matches(inner: dict[str, Any]) -> list[GrepMatchDTO]:
    raw_results: Any = inner.get("results", inner.get("matches", inner.get("items", [])))
    if not isinstance(raw_results, list):
        return []
    matches: list[GrepMatchDTO] = []
    for raw in raw_results:
        if isinstance(raw, dict):
            raw_line = raw.get("line", raw.get("line_no"))
            line: int | None
            try:
                line = int(raw_line) if raw_line not in (None, "") else None
            except (TypeError, ValueError):
                line = None
            matches.append(
                GrepMatchDTO(
                    path=str(raw.get("path", raw.get("file", ""))),
                    line=line,
                    text=str(raw.get("text", raw.get("preview", ""))),
                )
            )
        else:
            matches.append(GrepMatchDTO(path=str(raw)))
    return matches


def _tool_history_blocks_without_replay(
    frame: Frame,
    name: str,
    input_args: dict[str, Any],
    content: str,
) -> list[SessionHistoryBlock]:
    payload = _json_object(content)
    nested_result = payload.get("result")
    inner = nested_result if isinstance(nested_result, dict) else payload
    origin = _history_origin(frame)
    if name == "":
        summary = _unknown_tool_result_summary(payload, inner).strip()
        if str(payload.get("status", "")) in {"error", "rejected"}:
            return [ErrorHistoryBlock(text=summary, **origin)] if summary else []
        return [LogTextHistoryBlock(text=summary, marker=True, **origin)] if summary else []
    if name in _HISTORY_FRONT_TOOLS and (
        payload.get("status") in {"rejected", "error"} or inner.get("ok") is False
    ):
        summary = _front_tool_summary(name, input_args, inner).strip()
        return [ErrorHistoryBlock(text=summary, **origin)] if summary else []
    if payload.get("status") == "rejected":
        return [ErrorHistoryBlock(text=f"{name}: rejected", **origin)]
    error_message = inner.get(
        "error", inner.get("message") if payload.get("status") == "error" else None
    )
    if isinstance(error_message, str):
        return [ErrorHistoryBlock(text=f"{name}: {error_message}", **origin)]

    if name == "create_plan" or _looks_like_create_plan_result(inner):
        raw_steps = input_args.get("steps", inner.get("tasks", []))
        steps = raw_steps if isinstance(raw_steps, list) else []
        return [
            PlanCreatedHistoryBlock(
                summary=str(input_args.get("summary", "")).strip(),
                steps=[
                    PlanStepDTO(
                        index=index,
                        title=str(step.get("title", "")),
                        agent=str(step.get("agent", "")),
                        task=str(step.get("task", "")),
                    )
                    for index, step in enumerate(steps, start=1)
                    if isinstance(step, dict)
                ],
                **origin,
            )
        ]

    if name == "delegate_many" or _looks_like_delegate_group_result(inner):
        raw_results = inner.get("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        return [
            DelegateResultsHistoryBlock(
                results=[
                    DelegateResultDTO(
                        agent=str(result.get("agent", "")),
                        summary=str(result.get("summary", "")),
                    )
                    for result in results
                    if isinstance(result, dict)
                ],
                **origin,
            )
        ]

    if name == "delegate" or _looks_like_delegate_result(inner):
        return [
            DelegateResultHistoryBlock(
                agent=str(inner.get("agent", "")),
                summary=str(inner.get("summary", "")),
                frame_id=frame.id,
            )
        ]

    if name == "search_tools":
        return []

    if name in _HISTORY_READ_TOOLS:
        path = str(inner.get("path", input_args.get("path", "<unknown>")))
        offset = int(inner.get("offset", 1) or 1)
        line_count = max(_count_lines(str(inner.get("content", ""))), 1)
        return [
            LogReadHistoryBlock(
                path=path, line_start=offset, line_end=offset + line_count - 1, **origin
            )
        ]

    if name == "apply_text_edit":
        path = str(inner.get("path", input_args.get("path", "<unknown>")))
        old_string = str(input_args.get("old_string", ""))
        new_string = str(input_args.get("new_string", ""))
        replaced = int(inner.get("replaced_count", 1) or 1)
        return [
            LogEditHistoryBlock(
                path=path,
                added=_count_lines(new_string) * replaced,
                removed=_count_lines(old_string) * replaced,
                after_text=new_string,
                **origin,
            )
        ]

    if name in _HISTORY_EDIT_TOOLS:
        path = str(
            inner.get("path", input_args.get("path", input_args.get("target_path", "<unknown>")))
        )
        after_text = str(input_args.get("content", input_args.get("after_text", "")))
        before_text = str(input_args.get("before_text", input_args.get("before", "")))
        return [
            LogEditHistoryBlock(
                path=path,
                added=max(_count_lines(after_text) - _count_lines(before_text), 0),
                removed=max(_count_lines(before_text) - _count_lines(after_text), 0),
                after_text=after_text,
                **origin,
            )
        ]

    if name in _HISTORY_GREP_TOOLS:
        matches = _grep_matches(inner)
        pattern = str(
            input_args.get("pattern", input_args.get("query", input_args.get("include", "")))
        )
        include = str(input_args.get("include", input_args.get("path", "project"))) or "project"
        raw_count = inner.get("match_count", inner.get("count", len(matches)))
        try:
            match_count = int(raw_count)
        except (TypeError, ValueError):
            match_count = len(matches)
        return [
            LogGrepHistoryBlock(
                pattern=pattern,
                include=include,
                match_count=match_count,
                results=matches,
                truncated=bool(inner.get("truncated", False)),
                **origin,
            )
        ]

    if name == "read_scene_tree":
        return [NodeTreeHistoryBlock(title="Scene tree", tree=inner, **origin)]

    if name == "read_runtime_state":
        tree = inner.get("edited_scene")
        if isinstance(tree, dict):
            return [NodeTreeHistoryBlock(title="Runtime state", tree=tree, **origin)]

    if name in _HISTORY_FRONT_READ_TOOLS:
        summary = _front_tool_summary(name, input_args, inner)
        return [LogTextHistoryBlock(text=summary, marker=True, **origin)]

    if name in _HISTORY_FRONT_SCENE_EDIT_TOOLS or name in _HISTORY_FRONT_RUN_TOOLS:
        summary = _front_tool_summary(name, input_args, inner)
        return [LogTextHistoryBlock(text=summary, marker=True, **origin)]

    summary = _format_tool_result_summary(name, input_args, content).strip()
    return [LogTextHistoryBlock(text=summary, marker=True, **origin)] if summary else []


def _tool_history_blocks(
    frame: Frame,
    name: str,
    input_args: dict[str, Any],
    content: str,
) -> list[SessionHistoryBlock]:
    """重建工具历史块，且不生成前端私有回放事件。"""
    blocks = _tool_history_blocks_without_replay(frame, name, input_args, content)
    if name not in _HISTORY_FRONT_TOOLS:
        return blocks
    result = _json_object(content)
    status = str(result.get("status", ""))
    if status == "":
        status = "error" if result.get("error") or result.get("ok") is False else "applied"
        result = {"status": status, "result": result}
    render_descriptor = {
        "type": "tool_result",
        "call": {"name": name, "input": input_args, "agent": frame.agent.name},
        "result": result,
    }
    return [
        block.model_copy(
            update={
                "render_descriptor": render_descriptor,
            }
        )
        for block in blocks
    ]


def _system_history_blocks(frame: Frame, text: str) -> list[SessionHistoryBlock]:
    inner = _json_object(text)
    verify = inner.get("verify_outcome")
    if not isinstance(verify, dict):
        return []
    origin = _history_origin(frame)
    return [
        VerifyOutcomeHistoryBlock(
            file_path=str(inner.get("verify_target", "")),
            outcome=dict(verify),
            **origin,
        )
    ]


def _message_history_blocks(
    frame: Frame,
    message: dict[str, Any],
    tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]],
    *,
    is_initial_system: bool,
    message_index: int,
    include_thought_summary: bool = True,
) -> list[SessionHistoryBlock]:
    role = str(message.get("role", "system"))
    raw_content = message.get("content", "")
    text = "" if raw_content is None else str(raw_content)
    origin = _history_origin(frame)
    if _is_internal_history_message(message):
        return []
    if str(message.get("history_role", "")) == "error":
        displayed = text.strip()
        return (
            [ErrorHistoryBlock(text=displayed, message_index=message_index, **origin)]
            if displayed
            else []
        )
    if role == "user":
        if frame.parent_id is not None and message_index == 1:
            return []
        displayed = _display_user_content(text).strip()
        return (
            [UserHistoryBlock(text=displayed, message_index=message_index, **origin)]
            if displayed
            else []
        )
    if role == "assistant":
        calls = message.get("tool_calls", [])
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function", {})
                if not isinstance(function, dict):
                    continue
                call_id = str(call.get("id", ""))
                if call_id:
                    tool_calls_by_id[call_id] = (
                        str(function.get("name", "unknown")),
                        _parse_tool_call_arguments(function.get("arguments")),
                    )
        return [
            block.model_copy(update={"message_index": message_index})
            for block in _assistant_history_blocks(
                frame,
                text,
                has_tool_calls=bool(calls),
                include_thought_summary=include_thought_summary,
            )
        ]
    if role == "tool":
        call_id = str(message.get("tool_call_id", ""))
        name, input_args = tool_calls_by_id.get(call_id, ("", {}))
        return [
            block.model_copy(
                update={"message_index": message_index, "tool_use_id": call_id or None}
            )
            for block in _tool_history_blocks(frame, name, input_args, text)
        ]
    if role == "system":
        if is_initial_system or not text.strip():
            return []
        return [
            block.model_copy(update={"message_index": message_index})
            for block in _system_history_blocks(frame, text)
        ]
    return (
        [SystemTextHistoryBlock(text=text, message_index=message_index, **origin)]
        if text.strip()
        else []
    )


def _event_history_blocks(event: Event) -> list[SessionHistoryBlock]:
    payload = event.payload
    frame_id = str(payload.get("frame_id", "")) or None
    agent = str(payload.get("agent", "")) or None
    raw_message_index = payload.get("message_index", payload.get("timeline_message_index"))
    message_index = (
        int(raw_message_index)
        if isinstance(raw_message_index, int) and not isinstance(raw_message_index, bool)
        else None
    )
    origin = {"frame_id": frame_id, "agent": agent, "message_index": message_index}
    if event.type in _GENERIC_HISTORY_EVENT_TYPES:
        return [EventHistoryBlock(event_type=event.type, payload=payload, **origin)]
    if event.type == "agent_reasoning_delta":
        detail = str(payload.get("text", "")).strip()
        if not detail:
            return []
        elapsed_ms = payload.get("elapsed_ms")
        header = "Thought"
        if isinstance(elapsed_ms, int | float) and elapsed_ms > 0:
            header = f"Thought for {elapsed_ms / 1000:.2f}s"
        token_count = payload.get("token_count")
        if isinstance(token_count, int) and token_count > 0:
            header += f" · {token_count:,} tokens"
        return [ThoughtHistoryBlock(header=header, detail=detail, **origin)]
    if event.type == "agent_text_delta":
        text = str(payload.get("text", "")).strip()
        if text.startswith("Thought:"):
            _, _, remainder = text.partition("\n")
            text = remainder.strip()
            return [LogTextHistoryBlock(text=text, indent=True, **origin)] if text else []
        return [LogTextHistoryBlock(text=text, marker=True, **origin)] if text else []
    if event.type == "plan_created":
        raw_steps = payload.get("steps", [])
        steps = raw_steps if isinstance(raw_steps, list) else []
        return [
            PlanCreatedHistoryBlock(
                summary=str(payload.get("summary", "")),
                steps=[
                    PlanStepDTO(
                        index=int(step.get("index", index)),
                        title=str(step.get("title", "")),
                        agent=str(step.get("agent", "")),
                        task=str(step.get("task", "")),
                    )
                    for index, step in enumerate(steps, start=1)
                    if isinstance(step, dict)
                ],
                **origin,
            )
        ]
    if event.type == "plan_step_started":
        return [
            StepStartedHistoryBlock(
                index=int(payload.get("step_index", 0)),
                total=int(payload.get("total_steps", 0)),
                title=str(payload.get("title", "")),
                **origin,
            )
        ]
    if event.type == "plan_step_completed":
        return [
            StepCompletedHistoryBlock(
                index=int(payload.get("step_index", 0)),
                total=int(payload.get("total_steps", 0)),
                summary=str(payload.get("summary", "")),
                **origin,
            )
        ]
    if event.type == "verify_started":
        return [
            VerifyStartedHistoryBlock(
                file_path=str(payload.get("file_path", "")),
                phase=str(payload.get("phase", "")),
                **origin,
            )
        ]
    if event.type == "verify_completed":
        raw_outcome = payload.get("outcome", {})
        outcome = raw_outcome if isinstance(raw_outcome, dict) else {}
        return [
            VerifyOutcomeHistoryBlock(
                file_path=str(payload.get("file_path", "")),
                outcome=dict(outcome),
                **origin,
            )
        ]
    if event.type == "delegate_start":
        args = payload.get("args", {})
        task = str(args.get("task", "")) if isinstance(args, dict) else ""
        delegated_agent = str(args.get("agent", "")) if isinstance(args, dict) else ""
        label = f"Task({delegated_agent})" if delegated_agent else "Task"
        if task:
            label += f"\n{task}"
        return [LogTextHistoryBlock(text=label, marker=True, **origin)]
    if event.type == "server_tool_result":
        summary = payload.get("result_summary")
        if not isinstance(summary, dict):
            return [EventHistoryBlock(event_type=event.type, payload=payload, **origin)]
        kind = str(summary.get("kind", ""))
        if kind == "read":
            return [
                LogReadHistoryBlock(
                    path=str(summary.get("path", "")),
                    line_start=int(summary.get("line_start", 1)),
                    line_end=int(summary.get("line_end", 1)),
                    **origin,
                )
            ]
        if kind == "grep":
            raw_matches = summary.get("matches", [])
            matches = raw_matches if isinstance(raw_matches, list) else []
            return [
                LogGrepHistoryBlock(
                    pattern=str(summary.get("pattern", "")),
                    include=str(summary.get("include", "project")),
                    match_count=int(summary.get("match_count", len(matches))),
                    results=[
                        GrepMatchDTO(
                            path=str(match.get("path", "")),
                            line=(
                                int(match["line"]) if match.get("line") not in (None, "") else None
                            ),
                            text=str(match.get("text", "")),
                        )
                        for match in matches
                        if isinstance(match, dict)
                    ],
                    truncated=bool(summary.get("truncated", False)),
                    **origin,
                )
            ]
        return [EventHistoryBlock(event_type=event.type, payload=payload, **origin)]
    return []


def _structured_history_for_frame(frame: Frame, events: list[Event]) -> list[SessionHistoryBlock]:
    """Interleave frame messages with events anchored to their upcoming message index."""
    anchored: dict[int, list[SessionHistoryBlock]] = {}
    trailing: list[SessionHistoryBlock] = []
    stream_groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    normalized_events: list[tuple[Event, int, int]] = []
    for event in events:
        if event.type not in {"agent_reasoning_delta", "agent_text_delta"}:
            normalized_events.append((event, event.seq, 2))
            continue
        group_key = (
            str(event.payload.get("frame_id", "")),
            str(event.payload.get("loop", "")),
            str(event.payload.get("timeline_frame_id", "")),
            str(
                event.payload.get(
                    "timeline_message_index",
                    event.payload.get("message_index", ""),
                )
            ),
            str(event.payload.get("stream_segment", "")),
        )
        group = stream_groups.setdefault(
            group_key,
            {"reasoning": [], "text": []},
        )
        group_events = group["reasoning" if event.type == "agent_reasoning_delta" else "text"]
        assert isinstance(group_events, list)
        group_events.append(event)

    for group in stream_groups.values():
        reasoning_events = group["reasoning"]
        text_events = group["text"]
        assert isinstance(reasoning_events, list)
        assert isinstance(text_events, list)
        text = _merged_stream_event(text_events)
        if text is not None:
            reasoning_before_text = [event for event in reasoning_events if event.seq < text.seq]
            reasoning = _merged_stream_event(reasoning_before_text)
            usage_events = [
                event
                for event in reasoning_events
                if isinstance(event.payload.get("token_count"), int)
            ]
            if reasoning is not None and usage_events:
                usage_event = max(usage_events, key=lambda event: event.seq)
                reasoning = replace(
                    reasoning,
                    payload={
                        **reasoning.payload,
                        "token_count": usage_event.payload["token_count"],
                    },
                )
        else:
            reasoning = _merged_stream_event(reasoning_events)
        selected = [event for event in (reasoning, text) if event is not None]
        first_seq = min((event.seq for event in selected), default=2**31 - 1)
        if reasoning is not None:
            normalized_events.append((reasoning, first_seq, 0))
        if text is not None:
            normalized_events.append((text, first_seq, 1))

    def event_message_index(event: Event) -> int:
        raw_index = event.payload.get(
            "timeline_message_index",
            event.payload.get("message_index"),
        )
        try:
            return int(raw_index)
        except (TypeError, ValueError):
            return 2**31 - 1

    ordered_events = sorted(
        normalized_events,
        key=lambda item: (
            event_message_index(item[0]),
            item[1],
            item[2],
        ),
    )
    for event, _, _ in ordered_events:
        blocks = _event_history_blocks(event)
        if not blocks:
            continue
        raw_index = event.payload.get(
            "timeline_message_index",
            event.payload.get("message_index"),
        )
        message_index: int | None = None
        if isinstance(raw_index, int):
            message_index = raw_index
        # agent_text_delta events are streaming snapshots; when anchored to an
        # assistant message that already contains the final text, skip them to
        # avoid rendering the same content twice with mismatched indentation.
        if (
            event.type == "agent_text_delta"
            and message_index is not None
            and message_index < len(frame.messages)
            and str(frame.messages[message_index].get("role", "")) == "assistant"
            and str(frame.messages[message_index].get("content", "")).strip()
        ):
            continue
        if message_index is None:
            trailing.extend(blocks)
        else:
            anchored.setdefault(message_index, []).extend(blocks)

    result: list[SessionHistoryBlock] = []

    tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, message in enumerate(frame.messages):
        event_blocks = anchored.get(index, [])
        has_reasoning_event = any(isinstance(block, ThoughtHistoryBlock) for block in event_blocks)
        for block in event_blocks:
            result.append(block)
        message_blocks = _message_history_blocks(
            frame,
            message,
            tool_calls_by_id,
            is_initial_system=index == 0,
            message_index=index,
            include_thought_summary=not has_reasoning_event,
        )
        for block in message_blocks:
            if has_reasoning_event and isinstance(block, ThoughtHistoryBlock) and not block.detail:
                continue
            result.append(block)
    for message_index in sorted(index for index in anchored if index >= len(frame.messages)):
        for block in anchored[message_index]:
            result.append(block)
    for block in trailing:
        result.append(block)
    return result


def _structured_session_history(
    session_frames: list[Frame], events: list[Event]
) -> list[SessionHistoryBlock]:
    frame_aliases: dict[str, tuple[str, int | None]] = {}
    for event in events:
        frame_id = str(event.payload.get("frame_id", ""))
        timeline_frame_id = str(event.payload.get("timeline_frame_id", ""))
        if not frame_id or not timeline_frame_id or frame_id == timeline_frame_id:
            continue
        raw_index = event.payload.get("timeline_message_index")
        try:
            message_index = int(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            message_index = None
        frame_aliases.setdefault(frame_id, (timeline_frame_id, message_index))

    normalized_events: list[Event] = []
    for event in events:
        timeline_frame_id = str(
            event.payload.get("timeline_frame_id", event.payload.get("frame_id", ""))
        )
        message_index = event.payload.get("timeline_message_index")
        seen_aliases: set[str] = set()
        while timeline_frame_id in frame_aliases and timeline_frame_id not in seen_aliases:
            seen_aliases.add(timeline_frame_id)
            timeline_frame_id, alias_index = frame_aliases[timeline_frame_id]
            if alias_index is not None:
                message_index = alias_index
        if timeline_frame_id != str(event.payload.get("timeline_frame_id", "")):
            normalized_events.append(
                replace(
                    event,
                    payload={
                        **event.payload,
                        "timeline_frame_id": timeline_frame_id,
                        "timeline_message_index": message_index,
                    },
                )
            )
        else:
            normalized_events.append(event)

    # 按 timeline_frame_id 建一次索引，避免对每个 frame 都重新扫描全部
    # events（原实现是 O(frames * events)，长会话/大量 delegate_many 子
    # agent frame 叠加大事件日志时会让 session_history 卡到几十秒）。
    events_by_frame: dict[str, list[Event]] = {}
    for event in normalized_events:
        key = str(event.payload.get("timeline_frame_id", event.payload.get("frame_id", "")))
        events_by_frame.setdefault(key, []).append(event)

    blocks: list[SessionHistoryBlock] = []
    claimed_event_ids: set[int] = set()
    for frame in session_frames:
        frame_events = events_by_frame.get(frame.id, [])
        claimed_event_ids.update(id(event) for event in frame_events)
        blocks.extend(_structured_history_for_frame(frame, frame_events))
    for event in normalized_events:
        if id(event) in claimed_event_ids:
            continue
        blocks.extend(_event_history_blocks(event))
    return blocks


def _persisted_history_events(session: Session) -> list[Event]:
    """Convert the session-owned replay timeline back to typed internal events."""
    events: list[Event] = []
    for record in session.history_events:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        try:
            seq = int(record.get("seq", 0))
        except (TypeError, ValueError):
            continue
        event_type = str(record.get("type", ""))
        if seq <= 0 or not event_type:
            continue
        events.append(
            Event(
                seq=seq,
                session_id=session.session_id,
                type=event_type,
                payload=payload,
            )
        )
    return events


def _history_context_used_tokens(session: Session, events: list[Event]) -> int:
    """Return the latest exact provider usage, falling back to a local estimate."""
    for event in reversed(events):
        if event.type != "context_usage":
            continue
        try:
            used = int(event.payload.get("used_tokens", -1))
        except (TypeError, ValueError):
            continue
        if used >= 0:
            return used
    local_estimate = max(
        (estimate_message_tokens(frame.messages) for frame in session.agent_stack), default=0
    )
    return max(session.latest_context_used_tokens, local_estimate)


def _pending_anchor_index(frame: Frame, pending_ids: set[str]) -> int | None:
    """找到包含 pending tool_call 的 assistant 消息位置。"""
    if not pending_ids:
        return None
    for index, message in enumerate(frame.messages):
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict) and str(call.get("id", "")) in pending_ids:
                return index
    return None


_COMPACT_SUMMARY_HEADER = "[compact_summary]"


_COMPACT_SUMMARY_GUIDANCE = "以下是较早上下文的本地摘要；写文件或执行高风险操作前仍需重新读取事实。"


def _previous_summary_body(previous: CompactSnapshot | None) -> str:
    """取出旧快照摘要正文，剥掉 [compact_summary] 标记头与引导语，供合并时复用。"""
    if previous is None or not previous.summary.strip():
        return ""
    body = previous.summary.strip()
    if body.startswith(_COMPACT_SUMMARY_HEADER):
        body = body[len(_COMPACT_SUMMARY_HEADER) :].lstrip()
    if body.startswith(_COMPACT_SUMMARY_GUIDANCE):
        body = body[len(_COMPACT_SUMMARY_GUIDANCE) :].lstrip()
    return body


def _mechanical_summary_body(
    previous: CompactSnapshot | None, messages: list[dict[str, Any]]
) -> str:
    """机械拼接摘要正文：旧摘要正文 + 本次移除消息的逐条预览。

    作为 LLM 语义压缩的确定性回退（LLM 未启用、失败或返回空时使用），也用作
    喂给 LLM 的结构化源文本。
    """
    lines: list[str] = []
    previous_body = _previous_summary_body(previous)
    if previous_body:
        lines.extend(["较早压缩快照：", previous_body])
    if messages:
        if lines:
            lines.append("")
        lines.append("本次收拢的消息：")
        lines.extend(f"- {_brief_message(message)}" for message in messages)
    return "\n".join(lines)


def _wrap_compact_summary(body: str) -> str:
    """给摘要正文套上 [compact_summary] 标记头与引导语，并按上限截断为最终持久化文本。

    标记头是 system content-block 识别压缩层、预留缓存断点的依据（见
    `message_transformer.build_stable_prefix`），无论摘要来自 LLM 还是机械拼接都必须存在。
    """
    summary = "\n".join([_COMPACT_SUMMARY_HEADER, _COMPACT_SUMMARY_GUIDANCE, "", body.strip()])
    if len(summary) <= _COMPACT_SUMMARY_MAX_CHARS:
        return summary
    return summary[:_COMPACT_SUMMARY_MAX_CHARS] + "\n... (compact summary truncated)"


def _compact_summary_text(previous: CompactSnapshot | None, messages: list[dict[str, Any]]) -> str:
    """确定性的机械压缩摘要（零额外 LLM 调用）；LLM 语义压缩失败时的回退路径。"""
    return _wrap_compact_summary(_mechanical_summary_body(previous, messages))


def _compact_digest(summary: str) -> str:
    """计算压缩摘要规范化文本的 SHA-256 指纹。"""
    normalized = "\n".join(line.rstrip() for line in summary.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _inject_compact_snapshot(frame: Frame, *, has_rag_context: bool) -> None:
    """把持久化压缩快照写入首条 system 消息的独立 content block。"""
    if frame.compact_snapshot is None or not frame.messages:
        return
    system_message = frame.messages[0]
    if system_message.get("role") != "system":
        return
    content = system_message.get("content", "")
    blocks = (
        [dict(block) if isinstance(block, dict) else block for block in content]
        if isinstance(content, list)
        else [{"type": "text", "text": str(content)}]
    )
    blocks = [
        block
        for block in blocks
        if not (
            isinstance(block, dict) and str(block.get("text", "")).startswith("[compact_summary]")
        )
    ]
    compact_block = {"type": "text", "text": frame.compact_snapshot.summary}
    insert_at = len(blocks) - 1 if has_rag_context and blocks else len(blocks)
    blocks.insert(insert_at, compact_block)
    system_message["content"] = blocks


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
