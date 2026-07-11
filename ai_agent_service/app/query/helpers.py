"""QueryEngine helper functions split out of the HTTP/session facade."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from typing import Any

from app.agents.types import CompactSnapshot, Frame
from app.api.schemas import (
    ChatRequest,
    ChatToolCallsResponse,
    DelegateResultDTO,
    DelegateResultHistoryBlock,
    DelegateResultsHistoryBlock,
    ErrorHistoryBlock,
    EventHistoryBlock,
    FrontToolCallDTO,
    GrepMatchDTO,
    LogEditHistoryBlock,
    LogGrepHistoryBlock,
    LogReadHistoryBlock,
    LogTextHistoryBlock,
    NodeTreeHistoryBlock,
    PlanCreatedHistoryBlock,
    PlanStepDTO,
    SessionHistoryBlock,
    SessionHistoryItemDTO,
    StepCompletedHistoryBlock,
    StepStartedHistoryBlock,
    SystemTextHistoryBlock,
    ThoughtHistoryBlock,
    ToolResult,
    UserHistoryBlock,
    VerifyFailedHistoryBlock,
    VerifyPassedHistoryBlock,
    VerifyResultDTO,
    VerifyStartedHistoryBlock,
)
from app.events.store import Event
from app.llm.message_transformer import estimate_message_tokens, flatten_message_text
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES, MAP_WRITE_TOOL_NAMES
from app.sessions.store import Session

logger = logging.getLogger(__name__)
_MAP_CONTEXT_MAX_TARGETS = 8
_MAP_CONTEXT_MAX_REGIONS_PER_LAYER = 24
_MAP_CONTEXT_MAX_SUMMARY_CHARS = 2048
_MAP_CONTEXT_MAX_TOTAL_CHARS = 262_144
_MAP_ATLAS_SUMMARY_LIMIT = 12
_MAP_MATCH_SUMMARY_LIMIT = 12
_HISTORY_TOOL_MAX_JSON_CHARS = 80_000
_HISTORY_TOOL_MAX_STRING_CHARS = 16_000
_HISTORY_TOOL_MAX_LIST_ITEMS = 80
_HISTORY_TOOL_MAX_DICT_ITEMS = 120
_HISTORY_TOOL_DROP_KEYS = frozenset(
    {"data_url", "base64", "image_base64", "screenshot_base64", "binary", "bytes"}
)


def _raw_tool_call(call: FrontToolCallDTO) -> dict[str, Any]:
    """生成可写入 agent 历史的 assistant tool_call。"""
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.input, ensure_ascii=False),
        },
    }


def _replace_last_assistant_tool_calls(
    session: Session,
    text: str | None,
    calls: list[FrontToolCallDTO],
) -> None:
    """把最近一次 assistant tool_calls 替换为服务层改写后的调用。"""
    frame = session.top_frame()
    if frame is None or not frame.messages:
        return
    message = frame.messages[-1]
    if message.get("role") != "assistant":
        return
    message["content"] = text
    message["tool_calls"] = [_raw_tool_call(call) for call in calls]


def _append_assistant_tool_calls(
    session: Session,
    text: str,
    calls: list[FrontToolCallDTO],
) -> None:
    """追加一条服务层恢复出的 assistant tool_calls 消息。"""
    frame = session.top_frame()
    if frame is None:
        return
    frame.messages.append(
        {
            "role": "assistant",
            "content": text,
            "tool_calls": [_raw_tool_call(call) for call in calls],
        }
    )


def _append_map_state_read_error(
    session: Session,
    tool_name: str,
    target: str,
    required_state: str,
) -> None:
    """向 LLM 追加自动读状态失败后的可恢复错误消息。"""
    frame = session.top_frame()
    if frame is None:
        return
    frame.messages.append(
        {
            "role": "system",
            "internal": True,
            "content": (
                "出错：自动读取没有拿到需要的 state，"
                f"无法恢复挂起的 {tool_name} 调用。"
                f"target_path={target}，缺少 {required_state}。"
                "请重新 describe_map_region 或显式指定 map_layer/expected_revision。"
            ),
        }
    )


def _abort_pending_map_region_read_on_size_error(
    session: Session,
    tool_args: dict[str, Any],
    error_code: str | None,
    result: Any,
) -> bool:
    """在自动区域读取超限时取消挂起调用，避免原参数无限重试。"""
    if (
        str(error_code) != "region_too_large"
        or not bool(tool_args.get("__auto_map_state_read"))
        or session.pending_map_tool_after_read is None
    ):
        return False
    session.pending_map_tool_after_read = None
    suggested_regions = result.get("suggested_regions", []) if isinstance(result, dict) else []
    frame = session.top_frame()
    if frame is not None:
        frame.messages.append(
            {
                "role": "system",
                "internal": True,
                "content": (
                    "出错：自动 describe_map_region 请求超过 1600 cells，已取消原请求，"
                    "禁止使用相同参数重试。请逐个使用工具返回的 suggested_regions 读取，"
                    "所有分块成功后再重新调用原地图校验/规划工具。"
                    f" suggested_regions={json.dumps(suggested_regions, ensure_ascii=False)}"
                ),
            }
        )
    logger.warning(
        "Aborted pending map region read after size error session=%s target=%s",
        session.session_id,
        tool_args.get("target_path", ""),
    )
    return True


def _append_platform_planning_failure_hint(
    session: Session,
    tool_name: str,
    result: dict[str, Any],
) -> None:
    """平台规划失败时追加恢复指引，避免继续执行空规划。"""
    if tool_name not in {"plan_platform_level", "plan_reachable_map_growth"}:
        return
    blocked_reason = result.get("blocked_reason")
    edit_batches = result.get("edit_map_batches")
    jump_graph = result.get("jump_graph")
    jump_failed = isinstance(jump_graph, dict) and jump_graph.get("passed") is False
    empty_batches = isinstance(edit_batches, list) and not edit_batches
    if blocked_reason not in {"entry_anchor_not_found", "jump_graph_failed"} and not (
        jump_failed or (blocked_reason and empty_batches)
    ):
        return
    frame = session.top_frame()
    if frame is None:
        return
    frame.messages.append(
        {
            "role": "user",
            "content": (
                "出错：平台扩图规划失败，禁止执行空 edit_map_batches。"
                f"blocked_reason={blocked_reason or 'unknown'}。"
                "请先 describe_map_region 读取正确 target_path/map_layer 的连接边界；"
                "若没有满足 min_landing_width 的连续可站立入口，先用小批地图修复"
                "补入口 landing，validate_map_region(movement_model='leap') 通过后再重新规划。"
            ),
        }
    )


def _tool_message(tool_call_id: str, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    """构造 OpenAI `role=tool` 消息。"""
    body: Any = {"error": result} if is_error else result
    body = _bounded_tool_message_body(body)
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


_VERIFY_SYSTEM_PROMPT = (
    "你是代码改动校验员。这份文件已经通过 Phase 1 语法检查，请不要再判断语法正确性，"
    "只关注语义/逻辑层面：\n"
    "1) 是否有未定义的变量、函数、类引用；\n"
    "2) 编辑意图（tool_name/tool_input_path 所表达的修改目标）是否完整实现；\n"
    "3) 是否引入了明显的逻辑错误；\n"
    "4) 信号连接是否完整（GDScript 场景相关改动）；\n"
    "5) 依赖关系是否正确（import/preload 引用）。\n"
    "只返回 JSON，不要任何额外文字、不要 markdown 代码块标记，格式为："
    '{"passed": bool, "issues": [{"severity": "error"|"warning"|"info", '
    '"file_path": str, "line": int|null, "message": str}], "summary": str}'
)


def _parse_verify_response(text: str) -> VerifyResultDTO:
    """把 Phase 2 LLM 校验返回的文本解析为 `VerifyResultDTO`。

    解析失败（非 JSON/字段不合法）时保守返回 `passed=True`，避免校验自身的
    解析问题阻塞用户的正常工作流；失败原因记录在 `summary` 与日志中。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析得到的 `VerifyResultDTO`，或解析失败时的保守兜底结果。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Verify response is not valid JSON: %s", cleaned[:200])
        return VerifyResultDTO(passed=True, issues=[], summary="校验响应解析失败，已跳过")
    if not isinstance(data, dict):
        return VerifyResultDTO(passed=True, issues=[], summary="校验响应格式不合法，已跳过")
    try:
        return VerifyResultDTO.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 及其他兜底
        logger.warning("Verify response failed validation: %s", exc)
        return VerifyResultDTO(passed=True, issues=[], summary="校验响应字段不合法，已跳过")


def _build_user_content(request: ChatRequest) -> str:
    """把用户消息与前端上下文打包为稳定、可审计的 user message。"""
    assert request.user_message is not None
    context_payload: dict[str, Any] = {}
    if request.context is not None:
        context_payload["context"] = request.context.model_dump(exclude_none=True)
    if request.language_hint is not None:
        context_payload["language_hint"] = request.language_hint
    if request.engine_version is not None:
        context_payload["engine_version"] = request.engine_version
    if request.effort is not None:
        context_payload["effort"] = request.effort
    if request.output_style is not None:
        context_payload["output_style"] = request.output_style

    if not context_payload:
        return request.user_message
    return (
        request.user_message
        + "\n\n[editor_context]\n"
        + json.dumps(context_payload, ensure_ascii=False, sort_keys=True)
    )


def _brief_message(message: dict[str, Any]) -> str:
    """把一条历史 message 压成可读摘要行。"""
    role = str(message.get("role", "unknown"))
    if role == "assistant" and message.get("tool_calls"):
        names: list[str] = []
        for call in message.get("tool_calls", []):
            if isinstance(call, dict):
                function = call.get("function", {})
                if isinstance(function, dict):
                    names.append(str(function.get("name", "unknown")))
        return f"assistant 调用了工具：{', '.join(names) if names else 'unknown'}"
    content = flatten_message_text(message.get("content"))
    compact = " ".join(content.split())
    if len(compact) > 360:
        compact = compact[:360] + "..."
    return f"{role}: {compact}"


def _display_user_content(content: str) -> str:
    """Remove frontend context metadata from a stored user message."""
    marker = "\n\n[editor_context]\n"
    if marker in content:
        return content.split(marker, 1)[0]
    return content


_HISTORY_PREVIEW_LIMIT = 2000

# 单条消息允许的最大预估 token 数（见 `compact()` 里的"超大单条消息"截断）：
# 超过此值即视为异常（粘贴了整份大文件、工具结果未经摘要直接落地等），即使
# 帧总消息数还没到 `keep_recent` 门槛也会被截断。否则当消息数 <= keep_recent+2
# 时 `compact()` 对该帧完全是空操作——auto-compact 会在后续每个请求里反复
# 触发却什么都没压缩（§16.1 策略 A 的已知缺陷，已修复）。截断目标长度选得
# 足够小，保证截断后的消息再次估算时必然低于阈值（幂等，不会被重复截断）。
_OVERSIZED_MESSAGE_TOKEN_THRESHOLD = 4000
_OVERSIZED_MESSAGE_TRUNCATE_CHARS = 3000
_COMPACT_SUMMARY_MAX_CHARS = 12_000

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
        "plan_platform_level",
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
_MAP_VALIDATION_TOOL_NAMES = frozenset(
    {"validate_map_region", "validate_layer_coverage", "validate_object_placements"}
)
_MAP_OBJECT_PLACEMENT_TOOL_NAMES = frozenset(
    {
        "place_map_objects",
        "find_placement_anchors",
        "validate_object_placements",
        "repair_placements",
    }
)
_MAP_COMPLETION_TOOL_NAMES = MAP_REVISION_GUARDED_TOOL_NAMES | _MAP_VALIDATION_TOOL_NAMES
_MAP_REGION_READ_GUARDED_TOOL_NAMES = (
    MAP_WRITE_TOOL_NAMES
    | _MAP_VALIDATION_TOOL_NAMES
    | frozenset(
        {
            "plan_map_layout",
            "plan_map_algorithms",
            "plan_platform_level",
            "plan_reachable_map_growth",
            "compute_reachable_frontier",
            "convert_map_coords",
            "find_placement_anchors",
            "query_spatial_index",
            "sample_poisson_points",
            "sample_noise_grid",
        }
    )
) - frozenset({"write_resource_registry", "ensure_standard_map_layers"})
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


def _looks_like_create_plan_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是 `create_plan` 的内部回填载荷。"""
    tasks = content.get("tasks")
    note = str(content.get("note", ""))
    return bool(content.get("ok", False)) and isinstance(tasks, list) and "delegate_many" in note


def _format_create_plan_history_summary(input_args: dict[str, Any], content: dict[str, Any]) -> str:
    """把 `create_plan` 历史结果压缩成用户可读的计划摘要。"""
    summary = str(input_args.get("summary", "")).strip()
    raw_steps = input_args.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = content.get("tasks", [])
    steps = [step for step in raw_steps if isinstance(step, dict)]

    title = f"Plan created: {summary}" if summary else "Plan created"
    lines = [title]
    for index, step in enumerate(steps[:8], start=1):
        step_title = str(step.get("title", "")).strip()
        task = str(step.get("task", "")).strip()
        agent = str(step.get("agent", "")).strip()
        label = step_title or task or "Untitled step"
        suffix = f" ({agent})" if agent else ""
        lines.append(f"{index}. {label}{suffix}")
    if len(steps) > 8:
        lines.append(f"... {len(steps) - 8} more step(s)")
    return "\n".join(lines)


def _looks_like_delegate_group_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是 `delegate_many` 的子任务汇总载荷。"""
    results = content.get("results")
    if not isinstance(results, list) or not results:
        return False
    return all(isinstance(item, dict) and "summary" in item for item in results)


def _format_delegate_group_history_summary(content: dict[str, Any]) -> str:
    """把 `delegate_many` 子任务结果转换成可渲染 Markdown 的历史块。"""
    results = content.get("results")
    if not isinstance(results, list):
        return "Delegate results:"

    lines = ["Delegate results:"]
    for index, item in enumerate(results[:8], start=1):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "")).strip()
        summary = str(item.get("summary", "")).strip()
        heading = f"**{index}. {agent or 'delegate'}**"
        lines.extend(["", heading, _truncate_text(summary or "No summary", 1600)])
    if len(results) > 8:
        lines.append("")
        lines.append(f"... {len(results) - 8} more result(s)")
    return "\n".join(lines)


def _looks_like_delegate_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是单个 `delegate` 子任务摘要。"""
    return "summary" in content and set(content.keys()).issubset(
        {"summary", "agent", "frame_id", "error"}
    )


def _format_delegate_history_summary(content: dict[str, Any]) -> str:
    """把单个 `delegate` 子任务结果转换成可渲染 Markdown 的历史块。"""
    agent = str(content.get("agent", "")).strip()
    summary = str(content.get("summary", "")).strip()
    title = f"Delegate result: {agent}" if agent else "Delegate result:"
    return f"{title}\n{_truncate_text(summary or 'No summary', 2000)}"


def _truncate_oversized_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """单条消息预估 token 数超过 `_OVERSIZED_MESSAGE_TOKEN_THRESHOLD` 时返回截断副本。

    与 `compact()` 现有的"按消息数收拢成摘要"逻辑互补：那段逻辑只在帧总长度
    超过 `keep_recent` 门槛时才生效，对"消息数很少但单条内容巨大"的帧完全
    不起作用。这里独立判断单条消息大小，不依赖帧总长度。

    Args:
        message: 待检查的消息字典（OpenAI message dict）。

    Returns:
        预估 token 数未超阈值，或没有可截断的文本内容时返回 None（不修改）；
        否则返回浅拷贝并替换 `content` 为截断文本 + 提示的新消息字典。
    """
    flattened = flatten_message_text(message.get("content"))
    if not flattened or estimate_message_tokens([message]) <= _OVERSIZED_MESSAGE_TOKEN_THRESHOLD:
        return None
    truncated = _truncate_text(flattened, _OVERSIZED_MESSAGE_TRUNCATE_CHARS)
    note = f"\n…（原始内容过大已自动截断；原始约 {len(flattened)} 字符）"
    new_message = dict(message)
    new_message["content"] = truncated + note
    return new_message


def _truncate_text(text: str, max_chars: int) -> str:
    # 按字符数截断超长文本，避免会话历史里堆入过长内容。
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


def _front_result_lines(value: Any, *, indent: int = 0, max_items: int = 80) -> list[str]:
    """把前端工具结果转为有界的 Markdown 列表，保留节点层级。"""
    prefix = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                lines.append(f"{prefix}- ... (truncated)")
                break
            if isinstance(item, dict | list):
                lines.append(f"{prefix}- {key}:")
                lines.extend(_front_result_lines(item, indent=indent + 1, max_items=max_items))
            else:
                lines.append(f"{prefix}- {key}: {item}")
        return lines
    if isinstance(value, list):
        lines = []
        for index, item in enumerate(value):
            if index >= max_items:
                lines.append(f"{prefix}- ... (truncated)")
                break
            if isinstance(item, dict | list):
                lines.append(f"{prefix}- item {index + 1}:")
                lines.extend(_front_result_lines(item, indent=indent + 1, max_items=max_items))
            else:
                lines.append(f"{prefix}- {item}")
        return lines
    return [f"{prefix}- {value}"]


def _front_tool_error_message(result: dict[str, Any]) -> str:
    """提取前端工具结果中的错误摘要。"""
    if result.get("ok") is not False:
        return ""
    for key in ("message", "error", "error_code"):
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    return "Unknown error"


def _map_revision_from_result(result: dict[str, Any]) -> int | None:
    """从地图工具结果中提取最新可用的地图版本号。"""
    for key in ("map_revision", "actual_revision", "next_expected_revision"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _preferred_map_layer_from_layers(layers: Any) -> int | None:
    """从 legacy TileMap 图层列表中选一个更像前景/碰撞层的图层。"""
    if not isinstance(layers, list):
        return None
    ranked_keywords = ("mid", "foreground", "front", "ground", "collision")
    fallback: int | None = None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        index = layer.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if fallback is None:
            fallback = index
        name = str(layer.get("name", "")).lower()
        if any(keyword in name for keyword in ranked_keywords):
            return index
    return fallback


def _map_layer_from_result(result: dict[str, Any], *, prefer_layers: bool = False) -> int | None:
    """从地图工具结果中提取最新确认或建议的地图图层。"""
    if prefer_layers:
        preferred = _preferred_map_layer_from_layers(result.get("layers"))
        if preferred is not None:
            return preferred
    for key in ("map_layer", "suggested_map_layer"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _map_target_from_result(tool_args: dict[str, Any], result: dict[str, Any]) -> str:
    """从工具入参与结果中提取地图目标路径。"""
    for key in ("target_path", "target"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    value = tool_args.get("target_path")
    return value if isinstance(value, str) else ""


def _single_known_map_target(session: Session) -> str:
    """返回当前会话唯一已知地图目标；多目标时不猜。"""
    targets: set[str] = set(session.latest_map_revisions) | set(session.latest_map_layers)
    state_targets = session.map_context_state.get("targets")
    if isinstance(state_targets, dict):
        targets.update(str(target) for target in state_targets if str(target))
    return next(iter(targets)) if len(targets) == 1 else ""


def _resolved_map_tool_args(session: Session, tool_args: dict[str, Any]) -> dict[str, Any]:
    """用会话里已确认的地图目标和图层补齐工具参数。"""
    resolved = dict(tool_args)
    target = resolved.get("target_path")
    if not isinstance(target, str) or not target:
        target = _single_known_map_target(session)
        if target:
            resolved["target_path"] = target
    layer = resolved.get("map_layer", resolved.get("ground_map_layer"))
    if isinstance(layer, int) and not isinstance(layer, bool):
        resolved["map_layer"] = layer
        return resolved
    if isinstance(target, str) and target:
        latest_layer = session.latest_map_layers.get(target)
        if latest_layer is not None:
            resolved["map_layer"] = latest_layer
    return resolved


def _map_tool_requires_map_layer(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """判断地图工具是否必须带 2D map_layer。"""
    if tool_name in {
        "plan_platform_level",
        "plan_reachable_map_growth",
        "compute_reachable_frontier",
    }:
        return True
    if "map_layer" in tool_args or "ground_map_layer" in tool_args:
        return True
    target = tool_args.get("target_path")
    return isinstance(target, str) and target in session.latest_map_layers


def _map_tool_missing_required_context(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """返回地图工具执行前仍缺少的关键上下文字段。"""
    if _map_region_from_tool_args(tool_name, tool_args) is None:
        return ""
    if not isinstance(tool_args.get("target_path"), str) or not tool_args.get("target_path"):
        return "target_path"
    if _map_tool_requires_map_layer(session, tool_name, tool_args) and "map_layer" not in tool_args:
        return "map_layer"
    return ""


def _safe_artifact_name(value: str) -> str:
    """把任意字符串转成可作为 artifact 文件名片段的短标识。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    if cleaned:
        return cleaned[:80]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _json_char_size(value: Any) -> int:
    """粗略计算 JSON 值序列化后的字符长度。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _summarize_history_text(text: str, max_chars: int = _HISTORY_TOOL_MAX_STRING_CHARS) -> str:
    """保留长文本的开头和结尾，中间省略以控制 history 体积。"""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return text[:head] + f"\n... ({omitted} chars omitted for history) ...\n" + text[-tail:]


def _bounded_history_value(
    value: Any,
    *,
    max_string_chars: int = _HISTORY_TOOL_MAX_STRING_CHARS,
    max_list_items: int = _HISTORY_TOOL_MAX_LIST_ITEMS,
    max_dict_items: int = _HISTORY_TOOL_MAX_DICT_ITEMS,
) -> Any:
    """递归压缩任意工具结果，作为写入 LLM history 的最后防线。"""
    if isinstance(value, str):
        return _summarize_history_text(value, max_string_chars)
    if isinstance(value, list):
        bounded = [
            _bounded_history_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
            for item in value[:max_list_items]
        ]
        omitted = len(value) - max_list_items
        if omitted > 0:
            bounded.append({"history_omitted_items": omitted})
        return bounded
    if not isinstance(value, dict):
        return value
    bounded_dict: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        key_str = str(key)
        if key_str in _HISTORY_TOOL_DROP_KEYS:
            bounded_dict[f"{key_str}_omitted_for_history"] = True
            continue
        if index >= max_dict_items:
            bounded_dict["history_omitted_keys"] = len(value) - max_dict_items
            break
        bounded_dict[key_str] = _bounded_history_value(
            item,
            max_string_chars=max_string_chars,
            max_list_items=max_list_items,
            max_dict_items=max_dict_items,
        )
    return bounded_dict


def _bounded_tool_message_body(body: Any) -> Any:
    """限制单条 tool message 的最大体积，避免新工具绕过专用摘要。"""
    if isinstance(body, str):
        return _summarize_history_text(body, _HISTORY_TOOL_MAX_JSON_CHARS)
    if _json_char_size(body) <= _HISTORY_TOOL_MAX_JSON_CHARS:
        return body
    bounded = _bounded_history_value(body)
    if _json_char_size(bounded) <= _HISTORY_TOOL_MAX_JSON_CHARS:
        return bounded
    return {
        "history_truncated": True,
        "summary": _summarize_history_text(
            json.dumps(bounded, ensure_ascii=False, default=str),
            _HISTORY_TOOL_MAX_JSON_CHARS,
        ),
    }


def _region_summary_from_value(value: Any) -> dict[str, Any]:
    """从工具结果里抽取标准 region 字段。"""
    if not isinstance(value, dict):
        return {}
    region = value.get("region")
    if isinstance(region, dict):
        return dict(region)
    keys = ("x", "y", "z", "width", "height", "depth")
    return {key: value[key] for key in keys if key in value}


def _top_atlas_summary(value: Any, limit: int = _MAP_ATLAS_SUMMARY_LIMIT) -> Any:
    """截取 atlas_summary 的前 N 项，避免完整瓦片分布进入 history。"""
    if isinstance(value, list):
        return value[:limit]
    if not isinstance(value, dict):
        return value
    items = list(value.items())
    try:
        items.sort(
            key=lambda item: (
                int(item[1].get("count", item[1])) if isinstance(item[1], dict) else int(item[1])
            ),
            reverse=True,
        )
    except (TypeError, ValueError):
        pass
    return {str(key): entry for key, entry in items[:limit]}


def _map_result_summary(
    tool_name: str,
    result: dict[str, Any],
    artifact_ref: str | None,
) -> dict[str, Any]:
    """把大型地图工具结果压缩成可进入 LLM history 的小摘要。"""
    if tool_name == "capture_viewport_screenshot":
        keep_keys = (
            "ok",
            "path",
            "absolute_path",
            "width",
            "height",
            "focus",
            "semantic_description",
            "semantic",
            "message",
            "error_code",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        for key in ("semantic_description", "semantic", "message"):
            if isinstance(summary.get(key), str):
                summary[key] = _truncate_text(str(summary[key]), _MAP_CONTEXT_MAX_SUMMARY_CHARS)
        for key in ("rendered_nodes", "nodes_missing_visual_resource"):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = value[:_MAP_MATCH_SUMMARY_LIMIT]
                summary[f"{key}_omitted"] = max(0, len(value) - _MAP_MATCH_SUMMARY_LIMIT)
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        return summary

    if tool_name == "describe_map_region":
        cells = result.get("cells", [])
        summary: dict[str, Any] = {
            "ok": result.get("ok", True),
            "target": result.get("target", result.get("target_path")),
            "target_path": result.get("target_path", result.get("target")),
            "type": result.get("type"),
            "dimension": result.get("dimension"),
            "map_layer": result.get("map_layer"),
            "map_revision": result.get("map_revision"),
            "region": _region_summary_from_value(result),
            "used_bounds": result.get("used_bounds"),
            "layers": result.get("layers"),
            "cells_format": result.get("cells_format"),
            "cells_total": result.get("cells_total"),
            "cells_returned": result.get("cells_returned"),
            "non_empty_count": (
                result.get("non_empty_count")
                if "non_empty_count" in result
                else (len(cells) if isinstance(cells, list) else result.get("cells"))
            ),
            "cells_omitted": (
                result.get("cells_omitted")
                if "cells_omitted" in result
                else (isinstance(cells, list) and bool(cells))
            ),
            "artifact_ref": artifact_ref,
        }
        if "atlas_summary" in result:
            atlas_summary = _top_atlas_summary(result.get("atlas_summary"))
            summary["atlas_summary"] = atlas_summary
            summary["atlas_summary_top"] = atlas_summary
            summary["atlas_summary_omitted"] = True
        if artifact_ref is not None and (
            result.get("cells_omitted") or result.get("cells_returned") != result.get("cells_total")
        ):
            summary["exact_cells_hint"] = (
                "需要精确 cell 坐标/atlas 时，调用 read_file 读取 artifact_ref；"
                "不要从 cells_total/non_empty_count/atlas_summary 推断具体坐标。"
            )
        for key in (
            "message",
            "warning",
            "warnings",
            "stale_warning",
            "suggested_map_layer",
            "next_expected_revision",
        ):
            if key in result:
                summary[key] = result[key]
        return {key: value for key, value in summary.items() if value is not None}

    if tool_name == "query_spatial_index":
        matches = result.get("matches", [])
        summary = dict(result)
        if isinstance(matches, list):
            summary["matches"] = matches[:_MAP_MATCH_SUMMARY_LIMIT]
            summary["matches_omitted"] = max(0, len(matches) - _MAP_MATCH_SUMMARY_LIMIT)
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        return summary

    if tool_name in _MAP_OBJECT_PLACEMENT_TOOL_NAMES:
        keep_keys = (
            "ok",
            "passed",
            "changed",
            "target",
            "target_path",
            "parent_path",
            "dimension",
            "map_layer",
            "map_revision",
            "region",
            "message",
            "error_code",
            "coords",
            "blocking_cell",
            "support_cells",
            "hint",
            "failed_index",
            "failed_object",
            "batch_atomic",
            "placement_profile",
            "candidate_source",
            "rejected_summary",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        for key, limit in (
            ("objects", 20),
            ("paths", 40),
            ("anchors", 24),
            ("placements", 40),
            ("issues", 40),
            ("repair_plan", 24),
            ("moved", 40),
            ("plans", 24),
        ):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = _bounded_history_value(
                    value[:limit],
                    max_string_chars=4000,
                    max_list_items=limit,
                    max_dict_items=80,
                )
                summary[f"{key}_omitted"] = max(0, len(value) - limit)
        if "instance_summary" in result:
            summary["instance_summary"] = _bounded_history_value(
                result["instance_summary"],
                max_string_chars=4000,
                max_list_items=40,
                max_dict_items=80,
            )
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        return summary

    if tool_name in _MAP_VALIDATION_TOOL_NAMES:
        keep_keys = (
            "ok",
            "passed",
            "completion_allowed",
            "blocking_completion",
            "target",
            "target_path",
            "map_layer",
            "map_revision",
            "region",
            "issues",
            "structured_issues",
            "message",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        return summary

    return result


def _front_tool_result_summary(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """为非地图前端工具生成有界 history 摘要。"""
    if tool_name in {
        "run_system_command",
        "execute_gd_script",
        "run_tests",
        "run_headless_self_test",
        "git_diff",
        "git_status",
        "export_project",
    }:
        keep_keys = (
            "ok",
            "status",
            "exit_code",
            "pid",
            "path",
            "shell",
            "working_directory",
            "timeout_ms",
            "output_truncated",
            "error_code",
            "message",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        output = result.get("output")
        if isinstance(output, str) and output:
            summary["output"] = _summarize_history_text(output, 24_000)
            summary["output_omitted_for_history"] = len(output) > 24_000
        return summary

    if tool_name == "read_class_docs":
        summary = {
            key: result[key]
            for key in ("source", "class_name", "parent", "path", "base", "load_error")
            if key in result
        }
        for key, limit in (("methods", 40), ("properties", 50), ("signals", 30)):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = _bounded_history_value(value[:limit], max_string_chars=2000)
                summary[f"{key}_omitted"] = max(0, len(value) - limit)
        constants = result.get("constants")
        if isinstance(constants, dict):
            items = list(constants.items())
            summary["constants"] = {str(key): value for key, value in items[:80]}
            summary["constants_omitted"] = max(0, len(items) - 80)
        return summary

    if tool_name == "read_image_metadata":
        keep_keys = (
            "ok",
            "path",
            "absolute_path",
            "width",
            "height",
            "format",
            "message",
            "error_code",
            "semantic_description",
            "semantic",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        colors = result.get("dominant_colors")
        if isinstance(colors, list):
            summary["dominant_colors"] = colors[:16]
            summary["dominant_colors_omitted"] = max(0, len(colors) - 16)
        for key in ("semantic_description", "semantic", "message"):
            if isinstance(summary.get(key), str):
                summary[key] = _truncate_text(str(summary[key]), _MAP_CONTEXT_MAX_SUMMARY_CHARS)
        return summary

    if tool_name == "read_resource":
        summary = {
            key: result[key]
            for key in ("ok", "path", "type", "script_path", "message", "error_code")
            if key in result
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            summary["properties"] = _bounded_history_value(
                properties,
                max_string_chars=2000,
                max_list_items=30,
                max_dict_items=80,
            )
        return summary

    if tool_name in {"read_scene_tree", "read_runtime_state"}:
        return _bounded_history_value(
            result,
            max_string_chars=2000,
            max_list_items=60,
            max_dict_items=100,
        )

    if tool_name == "validate_scene_state":
        summary = {
            key: result[key]
            for key in ("ok", "passed", "failed", "message", "error_code")
            if key in result
        }
        results = result.get("results")
        if isinstance(results, list):
            summary["results"] = _bounded_history_value(
                results[:40],
                max_string_chars=2000,
                max_list_items=40,
                max_dict_items=80,
            )
            summary["results_omitted"] = max(0, len(results) - 40)
        return summary

    if tool_name == "read_debugger_errors":
        items = result.get("items")
        summary = {"ok": result.get("ok", True)}
        if isinstance(items, list):
            summary["items"] = _bounded_history_value(items[:30], max_string_chars=4000)
            summary["items_omitted"] = max(0, len(items) - 30)
        return summary

    return _bounded_history_value(result)


def _history_payload_for_front_tool(
    tool_name: str,
    payload: dict[str, Any],
    artifact_ref: str | None,
) -> dict[str, Any]:
    """生成写入 agent tool history 的瘦 payload。"""
    result = payload.get("result")
    if not isinstance(result, dict):
        return _bounded_tool_message_body(payload)
    if tool_name in {
        "capture_viewport_screenshot",
        "describe_map_region",
        "query_spatial_index",
        "validate_map_region",
        "validate_layer_coverage",
        "validate_object_placements",
        "place_map_objects",
        "find_placement_anchors",
        "repair_placements",
    }:
        slim = dict(payload)
        slim["result"] = _map_result_summary(tool_name, result, artifact_ref)
        return _bounded_tool_message_body(slim)
    if (
        tool_name
        in {
            "run_system_command",
            "execute_gd_script",
            "run_tests",
            "run_headless_self_test",
            "git_diff",
            "git_status",
            "export_project",
            "read_scene_tree",
            "read_runtime_state",
            "read_class_docs",
            "read_image_metadata",
            "read_resource",
            "validate_scene_state",
            "read_debugger_errors",
        }
        or _json_char_size(result) > _HISTORY_TOOL_MAX_JSON_CHARS
    ):
        slim = dict(payload)
        slim["result"] = _front_tool_result_summary(tool_name, result)
        return _bounded_tool_message_body(slim)
    slim = _bounded_tool_message_body(payload)
    if isinstance(slim, dict):
        return slim
    return slim


def _trim_text_fields(value: Any, max_chars: int = _MAP_CONTEXT_MAX_SUMMARY_CHARS) -> Any:
    """递归截断 map_context_state 摘要中的长字符串字段。"""
    if isinstance(value, str):
        return _truncate_text(value, max_chars)
    if isinstance(value, dict):
        return {str(key): _trim_text_fields(item, max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim_text_fields(item, max_chars) for item in value[:_MAP_MATCH_SUMMARY_LIMIT]]
    return value


def _update_map_context_state(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
    artifact_ref: str | None,
) -> None:
    """维护每 session 地图小索引；只保存摘要和 artifact_ref。"""
    if tool_name != "describe_map_region" or not isinstance(result, dict):
        return
    target = _map_target_from_result(tool_args, result)
    if not target:
        return
    layer = _map_layer_from_result(result, prefer_layers=True)
    layer_key = str(layer if layer is not None else tool_args.get("map_layer", "default"))
    state = session.map_context_state
    targets = state.setdefault("targets", {})
    if not isinstance(targets, dict):
        targets = {}
        state["targets"] = targets
    if target not in targets and len(targets) >= _MAP_CONTEXT_MAX_TARGETS:
        targets.pop(next(iter(targets)))
    target_state = targets.setdefault(target, {"layers": {}})
    if not isinstance(target_state, dict):
        target_state = {"layers": {}}
        targets[target] = target_state
    revision = _map_revision_from_result(result)
    if revision is not None:
        target_state["latest_revision"] = revision
    layers = target_state.setdefault("layers", {})
    if not isinstance(layers, dict):
        layers = {}
        target_state["layers"] = layers
    layer_state = layers.setdefault(layer_key, {"recent_regions": []})
    if not isinstance(layer_state, dict):
        layer_state = {"recent_regions": []}
        layers[layer_key] = layer_state
    if "used_bounds" in result:
        layer_state["used_bounds"] = result.get("used_bounds")
    entry = _trim_text_fields(
        _map_result_summary("describe_map_region", result, artifact_ref),
        _MAP_CONTEXT_MAX_SUMMARY_CHARS,
    )
    regions = layer_state.setdefault("recent_regions", [])
    if not isinstance(regions, list):
        regions = []
        layer_state["recent_regions"] = regions
    regions.append(entry)
    del regions[: max(0, len(regions) - _MAP_CONTEXT_MAX_REGIONS_PER_LAYER)]
    while _json_char_size(state) > _MAP_CONTEXT_MAX_TOTAL_CHARS:
        removed = False
        for target_item in list(targets.values()):
            if not isinstance(target_item, dict):
                continue
            layer_items = target_item.get("layers", {})
            if not isinstance(layer_items, dict):
                continue
            for layer_item in layer_items.values():
                if not isinstance(layer_item, dict):
                    continue
                recent = layer_item.get("recent_regions", [])
                if isinstance(recent, list) and recent:
                    recent.pop(0)
                    removed = True
                    break
            if removed:
                break
        if not removed:
            break


def _remember_latest_map_revision(
    session: Session,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录最近一次地图工具返回的 revision/layer，供下一次写入补齐 stale 入参。"""
    if not isinstance(result, dict):
        return
    revision = _map_revision_from_result(result)
    prefer_layers = bool(tool_args.get("__auto_map_state_read")) and not any(
        key in tool_args for key in ("map_layer", "ground_map_layer")
    )
    map_layer = _map_layer_from_result(
        result,
        prefer_layers=prefer_layers,
    )
    target = _map_target_from_result(tool_args, result)
    if not target:
        return
    if revision is not None:
        previous = session.latest_map_revisions.get(target)
        if previous is None or revision > previous:
            session.latest_map_revisions[target] = revision
            logger.info(
                "Latest map revision updated session=%s target=%s previous=%s current=%s",
                session.session_id,
                target,
                previous,
                revision,
            )
    if map_layer is not None:
        previous_layer = session.latest_map_layers.get(target)
        session.latest_map_layers[target] = map_layer
        logger.info(
            "Latest map layer updated session=%s target=%s previous=%s current=%s",
            session.session_id,
            target,
            previous_layer,
            map_layer,
        )


def _map_completion_blocker(
    tool_name: str, status: str, result: Any, error_code: str | None
) -> dict[str, Any] | None:
    """从地图工具结果中提取阻断最终完成的原因。"""
    if tool_name not in _MAP_COMPLETION_TOOL_NAMES:
        return None
    result_dict = result if isinstance(result, dict) else {}
    target = str(result_dict.get("target", result_dict.get("target_path", "")))
    revision = result_dict.get("map_revision")
    revision_value = (
        revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    )
    workflow_constraints = result_dict.get("workflow_constraints", [])
    if not isinstance(workflow_constraints, list):
        workflow_constraints = []
    if status != "applied":
        return {
            "tool": tool_name,
            "reason": error_code or status,
            "issues": [str(error_code or status)],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }

    issues = result_dict.get("issues")
    if not isinstance(issues, list):
        validation = result_dict.get("validation")
        issues = validation.get("issues", []) if isinstance(validation, dict) else []
    normalized_issues = [str(issue) for issue in issues if str(issue).strip()]

    if bool(result_dict.get("blocking_completion", False)):
        return {
            "tool": tool_name,
            "reason": "blocking_completion",
            "issues": normalized_issues or ["map tool reported blocking_completion=true"],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    if result_dict.get("completion_allowed") is False:
        return {
            "tool": tool_name,
            "reason": "completion_not_allowed",
            "issues": normalized_issues or ["map tool reported completion_allowed=false"],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    if (
        tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
        and result_dict.get("completion_allowed") is not True
    ):
        return {
            "tool": tool_name,
            "reason": "map_write_requires_validation",
            "issues": [
                "map write applied but no successful same-revision validation has cleared completion"
            ],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    return None


def _same_map_target(blocker: dict[str, Any], target: str) -> bool:
    """判断阻断项是否属于同一地图目标。"""
    blocker_target = str(blocker.get("target", ""))
    return blocker_target == "" or target == "" or blocker_target == target


def _blocker_revision(blocker: dict[str, Any]) -> int | None:
    """读取阻断项要求的 map revision。"""
    value = blocker.get("required_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clear_validation_blockers(
    blockers: list[dict[str, Any]],
    target: str,
    revision: int | None,
    validator: str,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    """按同 revision 验证结果清除或缩减写后校验阻断。"""
    validation_reasons = {
        "map_write_requires_validation",
        "completion_not_allowed",
        "blocking_completion",
    }
    remaining: list[dict[str, Any]] = []
    for blocker in blockers:
        if blocker.get("reason") not in validation_reasons:
            remaining.append(blocker)
            continue
        blocker_revision = _blocker_revision(blocker)
        if _same_map_target(blocker, target) and (
            revision is None or blocker_revision is None or revision >= blocker_revision
        ):
            constraints = blocker.get("workflow_constraints", [])
            if isinstance(constraints, list) and constraints:
                remaining_constraints = [
                    constraint
                    for constraint in constraints
                    if not (
                        isinstance(constraint, dict)
                        and constraint.get("validator") == validator
                        and isinstance(constraint.get("required_args", {}), dict)
                        and all(
                            args.get(key) == value
                            for key, value in constraint.get("required_args", {}).items()
                        )
                    )
                ]
                if remaining_constraints:
                    remaining.append({**blocker, "workflow_constraints": remaining_constraints})
                    continue
            continue
        remaining.append(blocker)
    return remaining


def _has_review_blocker(blockers: list[dict[str, Any]], target: str, revision: int | None) -> bool:
    """判断是否已有同目标同版本的 reviewer 阻断。"""
    for blocker in blockers:
        if blocker.get("reason") != "map_review_required":
            continue
        if not _same_map_target(blocker, target):
            continue
        blocker_revision = _blocker_revision(blocker)
        if revision is None or blocker_revision is None or revision == blocker_revision:
            return True
    return False


def _review_required_blocker(tool_name: str, target: str, revision: int | None) -> dict[str, Any]:
    """生成验证通过后的视觉复核阻断项。"""
    return {
        "tool": tool_name,
        "reason": "map_review_required",
        "issues": ["same-revision validation passed; reviewer visual check is still required"],
        "target": target,
        "required_revision": revision,
    }


def _map_region_from_write_args(
    tool_args: dict[str, Any], result_dict: dict[str, Any]
) -> dict[str, int] | None:
    """从地图写工具参数中推导需要重读的区域。"""
    region = tool_args.get("region", tool_args.get("rect", result_dict.get("region")))
    if isinstance(region, dict):
        return {
            str(key): int(value)
            for key, value in region.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    operations = tool_args.get("operations")
    if not isinstance(operations, list):
        return None

    min_x: int | None = None
    min_y: int | None = None
    max_x: int | None = None
    max_y: int | None = None
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        x_value = operation.get("to_x", operation.get("x"))
        y_value = operation.get("to_y", operation.get("y"))
        if (
            not isinstance(x_value, int)
            or isinstance(x_value, bool)
            or not isinstance(y_value, int)
            or isinstance(y_value, bool)
        ):
            continue
        width = operation.get("width", 1)
        height = operation.get("height", 1)
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
        ):
            continue
        op_max_x = x_value + max(width, 1) - 1
        op_max_y = y_value + max(height, 1) - 1
        min_x = x_value if min_x is None else min(min_x, x_value)
        min_y = y_value if min_y is None else min(min_y, y_value)
        max_x = op_max_x if max_x is None else max(max_x, op_max_x)
        max_y = op_max_y if max_y is None else max(max_y, op_max_y)

    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    return {
        "x": min_x,
        "y": min_y,
        "width": max_x - min_x + 1,
        "height": max_y - min_y + 1,
    }


def _direct_region_from_args(tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从直接区域字段中提取地图区域。"""
    required = ("x", "y", "width", "height")
    if any(
        not isinstance(tool_args.get(key), int) or isinstance(tool_args.get(key), bool)
        for key in required
    ):
        return None
    region = {key: int(tool_args[key]) for key in required}
    if isinstance(tool_args.get("z"), int) and not isinstance(tool_args.get("z"), bool):
        region["z"] = int(tool_args["z"])
    if isinstance(tool_args.get("depth"), int) and not isinstance(tool_args.get("depth"), bool):
        region["depth"] = int(tool_args["depth"])
    return region


def _entry_sample_region_from_args(tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从平台规划 entry_sample 字段中提取真实边界采样区域。"""
    mapping = {
        "x": "entry_sample_x",
        "y": "entry_sample_y",
        "width": "entry_sample_width",
        "height": "entry_sample_height",
    }
    if any(
        not isinstance(tool_args.get(source), int) or isinstance(tool_args.get(source), bool)
        for source in mapping.values()
    ):
        return None
    return {target: int(tool_args[source]) for target, source in mapping.items()}


def _points_region(points: Any) -> dict[str, int] | None:
    """从对象/单元点列表推导最小包围区域。"""
    if not isinstance(points, list):
        return None
    min_x: int | None = None
    min_y: int | None = None
    max_x: int | None = None
    max_y: int | None = None
    for item in points:
        if not isinstance(item, dict):
            continue
        x_value = item.get("x")
        y_value = item.get("y")
        if (
            not isinstance(x_value, int)
            or isinstance(x_value, bool)
            or not isinstance(y_value, int)
            or isinstance(y_value, bool)
        ):
            continue
        min_x = x_value if min_x is None else min(min_x, x_value)
        min_y = y_value if min_y is None else min(min_y, y_value)
        max_x = x_value if max_x is None else max(max_x, x_value)
        max_y = y_value if max_y is None else max(max_y, y_value)
    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    return {"x": min_x, "y": min_y, "width": max_x - min_x + 1, "height": max_y - min_y + 1}


def _map_region_from_tool_args(tool_name: str, tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从地图工具入参推导它依赖的真实地图区域。"""
    if tool_name in {"plan_platform_level", "plan_reachable_map_growth"}:
        entry_region = _entry_sample_region_from_args(tool_args)
        if entry_region is not None:
            return entry_region
    direct_region = _direct_region_from_args(tool_args)
    if direct_region is not None:
        return direct_region
    write_region = _map_region_from_write_args(tool_args, {})
    if write_region is not None:
        return write_region
    for key in ("objects", "cells", "path_cells", "route_cells", "frontier_cells"):
        region = _points_region(tool_args.get(key))
        if region is not None:
            return region
    return None


def _map_region_read_signature(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """生成地图区域读取签名，用于约束地图工具先读区域。"""
    region = _map_region_from_tool_args(tool_name, tool_args)
    if region is None:
        return None
    target = tool_args.get("target_path", "")
    if not isinstance(target, str):
        target = ""
    map_layer = tool_args.get("map_layer", tool_args.get("ground_map_layer"))
    if not isinstance(map_layer, int) or isinstance(map_layer, bool):
        return None
    z_value = region.get("z", 0)
    depth = region.get("depth", 1)
    return "|".join(
        str(value)
        for value in (
            target,
            map_layer,
            region["x"],
            region["y"],
            z_value,
            region["width"],
            region["height"],
            depth,
        )
    )


def _remember_latest_map_region_read(
    session: Session,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录最近读过的地图区域，避免 frontier 计算在未读区域上猜 start。"""
    normalized_args = dict(tool_args)
    if isinstance(result, dict):
        target = _map_target_from_result(tool_args, result)
        layer = _map_layer_from_result(result)
        if target:
            normalized_args["target_path"] = target
        if layer is not None:
            normalized_args["map_layer"] = layer
    signature = _map_region_read_signature("describe_map_region", normalized_args)
    if signature is None:
        return
    revision = _map_revision_from_result(result) if isinstance(result, dict) else None
    if revision is None:
        return
    session.latest_map_region_reads[signature] = revision
    if isinstance(result, dict):
        session.latest_map_region_summaries[signature] = _map_result_summary(
            "describe_map_region",
            result,
            None,
        )
    while len(session.latest_map_region_reads) > 64:
        first_key = next(iter(session.latest_map_region_reads))
        del session.latest_map_region_reads[first_key]
        session.latest_map_region_summaries.pop(first_key, None)


def _latest_map_region_summary_for_call(
    session: Session,
    call: FrontToolCallDTO,
) -> dict[str, Any] | None:
    resolved_input = _resolved_map_tool_args(session, call.input)
    signature = _map_region_read_signature(call.name, resolved_input)
    if signature is None:
        return None
    summary = session.latest_map_region_summaries.get(signature)
    return summary if isinstance(summary, dict) else None


def _blocks_platform_plan_after_empty_region_read(
    session: Session,
    call: FrontToolCallDTO,
) -> bool:
    if call.name not in {"plan_platform_level", "plan_reachable_map_growth"}:
        return False
    summary = _latest_map_region_summary_for_call(session, call)
    if summary is None:
        return False
    try:
        non_empty_count = int(summary.get("non_empty_count", 0))
    except (TypeError, ValueError):
        non_empty_count = 0
    if non_empty_count > 0:
        return False
    session.pending_map_tool_after_read = None
    _append_map_state_read_error(
        session,
        call.name,
        str(call.input.get("target_path", "")),
        "non_empty_cells in the entry sample; choose the foreground map_layer or move/expand entry_sample_* before planning",
    )
    logger.info(
        "Blocked platform plan after empty region read session=%s tool=%s target=%s layer=%s",
        session.session_id,
        call.name,
        call.input.get("target_path"),
        call.input.get("map_layer"),
    )
    return True


def _map_tool_region_read_current(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图工具依赖的区域是否已按当前 revision 读取。"""
    resolved_input = _resolved_map_tool_args(session, call.input)
    if _map_tool_missing_required_context(session, call.name, resolved_input):
        return False
    signature = _map_region_read_signature(call.name, resolved_input)
    if signature is None:
        return True
    read_revision = session.latest_map_region_reads.get(signature)
    if read_revision is None:
        return False
    target = resolved_input.get("target_path")
    if not isinstance(target, str) or not target:
        return True
    latest_revision = session.latest_map_revisions.get(target)
    return latest_revision is not None and read_revision == latest_revision


def _map_region_read_call_for_tool(
    session: Session,
    call: FrontToolCallDTO,
) -> FrontToolCallDTO | None:
    """把地图工具调用转换为同一区域的 describe_map_region 调用。"""
    resolved_input = _resolved_map_tool_args(session, call.input)
    region = _map_region_from_tool_args(call.name, resolved_input)
    if region is None:
        return None
    read_input: dict[str, Any] = {
        "__auto_map_state_read": True,
        "cells_format": "non_empty_only",
        "max_returned_cells": 120,
    }
    for key in ("target_path", "map_layer", "ground_map_layer"):
        if key in resolved_input:
            read_input["map_layer" if key == "ground_map_layer" else key] = resolved_input[key]
    read_input.update(region)
    return FrontToolCallDTO(
        id=f"{call.id}__map_region_read",
        name="describe_map_region",
        input=read_input,
        needs_confirm=False,
        frame_id=call.frame_id,
        agent=call.agent,
        render_kind="json",
    )


def _defer_map_tool_for_region_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """强制依赖真实地图区域的工具在同一区域 describe_map_region 之后执行。"""
    if session.pending_map_tool_after_read is not None:
        return response
    guarded_call = next(
        (call for call in response.calls if call.name in _MAP_REGION_READ_GUARDED_TOOL_NAMES),
        None,
    )
    if guarded_call is None:
        return response
    if _map_tool_region_read_current(session, guarded_call):
        return response
    read_call = _map_region_read_call_for_tool(session, guarded_call)
    if read_call is None:
        return response
    session.pending_map_tool_after_read = {"call": guarded_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取真实地图区域，再执行地图计算/编辑工具。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
            }
        },
    )
    logger.info(
        "Deferred map tool for region read session=%s tool=%s target=%s read_call=%s",
        session.session_id,
        guarded_call.name,
        guarded_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _resume_pending_map_tool_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完地图区域后恢复此前挂起的地图工具调用。"""
    pending = session.pending_map_tool_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_tool_after_read = None
        return None
    call = FrontToolCallDTO.model_validate(raw_call)
    restored_input = _resolved_map_tool_args(session, call.input)
    target = restored_input.get("target_path")
    target_path = target if isinstance(target, str) else ""
    latest_revision = session.latest_map_revisions.get(target_path)
    latest_layer = session.latest_map_layers.get(target_path)
    if (
        call.name
        in {"plan_platform_level", "plan_reachable_map_growth", "compute_reachable_frontier"}
        and latest_layer is not None
        and "map_layer" not in restored_input
    ):
        restored_input["map_layer"] = latest_layer

    if call.name in MAP_REVISION_GUARDED_TOOL_NAMES:
        if not target_path or latest_revision is None:
            session.pending_map_tool_after_read = None
            _append_map_state_read_error(
                session,
                call.name,
                target_path,
                "expected_revision",
            )
            return None
        restored_input["expected_revision"] = latest_revision
    if "map_layer" not in restored_input and latest_layer is not None:
        restored_input["map_layer"] = latest_layer
    missing_context = _map_tool_missing_required_context(session, call.name, restored_input)
    if missing_context:
        session.pending_map_tool_after_read = None
        _append_map_state_read_error(
            session,
            call.name,
            target_path,
            missing_context,
        )
        return None
    if call.name in _MAP_VALIDATION_TOOL_NAMES and "map_layer" not in restored_input:
        session.pending_map_tool_after_read = None
        _append_map_state_read_error(session, call.name, target_path, "map_layer")
        return None

    restored_call = call.model_copy(update={"input": restored_input})
    if not _map_tool_region_read_current(session, restored_call):
        read_call = _map_region_read_call_for_tool(session, restored_call)
        if read_call is None:
            session.pending_map_tool_after_read = None
            _append_map_state_read_error(
                session,
                call.name,
                target_path,
                "target_path/map_layer/region_context",
            )
            return None
        text = "已确认地图图层，继续读取带图层的真实地图区域。"
        turn_id = session.new_turn_id()
        _append_assistant_tool_calls(session, text, [read_call])
        session.set_pending(
            turn_id,
            [read_call.id],
            {
                read_call.id: {
                    "name": read_call.name,
                    "input": read_call.input,
                    "frame_id": read_call.frame_id,
                    "agent": read_call.agent,
                }
            },
        )
        logger.info(
            "Continuing pending map tool region read session=%s tool=%s target=%s layer=%s read_call=%s",
            session.session_id,
            restored_call.name,
            target_path,
            restored_input.get("map_layer"),
            read_call.id,
        )
        return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[read_call])

    text = "已读取真实地图区域，继续执行挂起的地图工具调用。"
    if _blocks_platform_plan_after_empty_region_read(session, restored_call):
        return None

    turn_id = session.new_turn_id()
    session.pending_map_tool_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
            }
        },
    )
    logger.info(
        "Resumed pending map tool after region read session=%s tool=%s target=%s revision=%s layer=%s",
        session.session_id,
        restored_call.name,
        target_path,
        latest_revision,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


def _needs_map_state_read_before_write(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图写工具是否需要先自动读取 map_layer/map_revision。"""
    if call.name not in MAP_REVISION_GUARDED_TOOL_NAMES:
        return False
    target = call.input.get("target_path")
    if not isinstance(target, str) or not target:
        return False
    missing_revision = target not in session.latest_map_revisions
    missing_layer = "map_layer" not in call.input and target not in session.latest_map_layers
    return missing_revision or missing_layer


def _map_state_read_call_for_write(
    session: Session,
    write_call: FrontToolCallDTO,
) -> FrontToolCallDTO:
    """为挂起的地图写调用构造自动状态读取调用。"""
    target = str(write_call.input.get("target_path", ""))
    read_input: dict[str, Any] = {
        "target_path": target,
        "__auto_map_state_read": True,
    }
    latest_layer = session.latest_map_layers.get(target)
    if latest_layer is not None:
        read_input["map_layer"] = latest_layer
    region = _map_region_from_write_args(write_call.input, {})
    if region is not None:
        read_input.update(region)
    return FrontToolCallDTO(
        id=f"{write_call.id}__map_state_read",
        name="describe_map_region",
        input=read_input,
        needs_confirm=False,
        frame_id=write_call.frame_id,
        agent=write_call.agent,
        render_kind="json",
    )


def _defer_map_write_for_state_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """把缺少地图状态的写调用挂起，先返回自动 describe_map_region。"""
    if session.pending_map_write_after_read is not None:
        return response
    write_call = next(
        (call for call in response.calls if _needs_map_state_read_before_write(session, call)),
        None,
    )
    if write_call is None:
        return response
    read_call = _map_state_read_call_for_write(session, write_call)
    session.pending_map_write_after_read = {"call": write_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取地图当前状态，再恢复挂起的地图写入。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
            }
        },
    )
    logger.info(
        "Deferred map write for state read session=%s write_tool=%s target=%s read_call=%s",
        session.session_id,
        write_call.name,
        write_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _resume_pending_map_write_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完 map state 后恢复此前挂起的地图写调用。"""
    pending = session.pending_map_write_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_write_after_read = None
        return None
    write_call = FrontToolCallDTO.model_validate(raw_call)
    target = write_call.input.get("target_path")
    if not isinstance(target, str) or not target:
        session.pending_map_write_after_read = None
        return None
    latest_revision = session.latest_map_revisions.get(target)
    latest_layer = session.latest_map_layers.get(target)
    if latest_revision is None or ("map_layer" not in write_call.input and latest_layer is None):
        missing = []
        if latest_revision is None:
            missing.append("expected_revision")
        if "map_layer" not in write_call.input and latest_layer is None:
            missing.append("map_layer")
        session.pending_map_write_after_read = None
        _append_map_state_read_error(session, write_call.name, target, "/".join(missing))
        return None
    restored_input = dict(write_call.input)
    restored_input["expected_revision"] = latest_revision
    if "map_layer" not in restored_input and latest_layer is not None:
        restored_input["map_layer"] = latest_layer
    restored_call = write_call.model_copy(update={"input": restored_input})
    text = "已读取地图当前状态，继续执行挂起的地图写入。"
    turn_id = session.new_turn_id()
    session.pending_map_write_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
            }
        },
    )
    logger.info(
        "Resumed pending map write after state read session=%s tool=%s target=%s revision=%s layer=%s",
        session.session_id,
        restored_call.name,
        target,
        latest_revision,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


def _needs_map_state_read_before_validation(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图校验工具是否需要先自动读取 map_layer。"""
    if call.name not in _MAP_VALIDATION_TOOL_NAMES:
        return False
    target = call.input.get("target_path")
    return (
        isinstance(target, str)
        and bool(target)
        and "map_layer" not in call.input
        and target not in session.latest_map_layers
    )


def _defer_map_validation_for_state_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """把缺少图层的地图校验调用挂起，先返回自动 describe_map_region。"""
    if (
        session.pending_map_write_after_read is not None
        or session.pending_map_validation_after_read is not None
    ):
        return response
    validation_call = next(
        (call for call in response.calls if _needs_map_state_read_before_validation(session, call)),
        None,
    )
    if validation_call is None:
        return response
    read_call = _map_state_read_call_for_write(session, validation_call)
    session.pending_map_validation_after_read = {"call": validation_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取地图图层状态，再恢复挂起的地图校验。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
            }
        },
    )
    logger.info(
        "Deferred map validation for state read session=%s validation_tool=%s target=%s read_call=%s",
        session.session_id,
        validation_call.name,
        validation_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _resume_pending_map_validation_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完 map layer 后恢复此前挂起的地图校验调用。"""
    pending = session.pending_map_validation_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_validation_after_read = None
        return None
    validation_call = FrontToolCallDTO.model_validate(raw_call)
    target = validation_call.input.get("target_path")
    if not isinstance(target, str) or not target:
        session.pending_map_validation_after_read = None
        return None
    latest_layer = session.latest_map_layers.get(target)
    if latest_layer is None:
        session.pending_map_validation_after_read = None
        _append_map_state_read_error(session, validation_call.name, target, "map_layer")
        return None
    restored_input = dict(validation_call.input)
    restored_input.setdefault("map_layer", latest_layer)
    restored_call = validation_call.model_copy(update={"input": restored_input})
    text = "已读取地图图层状态，继续执行挂起的地图校验。"
    turn_id = session.new_turn_id()
    session.pending_map_validation_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
            }
        },
    )
    logger.info(
        "Resumed pending map validation after state read session=%s tool=%s target=%s layer=%s",
        session.session_id,
        restored_call.name,
        target,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


def _schedule_revision_conflict_reader(
    session: Session,
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """在 revision 冲突后自动压入 map-reader-agent 重读帧。"""
    result_dict = result if isinstance(result, dict) else {}
    target = str(tool_args.get("target_path", result_dict.get("target_path", "")))
    region = _map_region_from_write_args(tool_args, result_dict)
    task_payload = {
        "reason": "map_revision_conflict",
        "failed_tool": tool_name,
        "target_path": target,
        "region": region,
        "expected_revision": result_dict.get("expected_revision"),
        "actual_revision": result_dict.get("actual_revision"),
        "next_expected_revision": result_dict.get("actual_revision"),
        "instruction": (
            "重读冲突区域并只输出 map_worker_result_v1 JSON；"
            "不要写入，next_stage 设为 planner 或 validator。"
        ),
    }
    try:
        reader = get_agent("map-reader-agent", set(REGISTRY))
    except KeyError:
        frame.messages.append(
            {
                "role": "user",
                "content": "map_revision_conflict：map-reader-agent 未注册，请先手动重读冲突区域。",
            }
        )
        return
    task_text = json.dumps(task_payload, ensure_ascii=False)
    child = Frame(
        id=session.new_frame_id(),
        agent=reader,
        messages=[
            {"role": "system", "content": reader.prompt},
            {"role": "user", "content": task_text},
        ],
        parent_id=frame.id,
        depth=frame.depth + 1,
        history_anchor_frame_id=frame.history_anchor_frame_id or frame.id,
        history_anchor_message_index=(
            frame.history_anchor_message_index
            if frame.history_anchor_message_index is not None
            else len(frame.messages)
        ),
    )
    session.agent_stack.append(child)


def _pop_last_assistant_final(session: Session) -> None:
    """移除刚被完成门拦截的 assistant final，避免错误完成陈述进入后续上下文。"""
    frame = session.top_frame()
    if frame is None or not frame.messages:
        return
    last = frame.messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        frame.messages.pop()


def _schedule_map_reviewer_if_required(session: Session) -> bool:
    """把 map_review_required 阻断转换为 reviewer 子帧继续执行。"""
    frame = session.top_frame()
    if frame is None or frame.agent.name == "map-reviewer-agent":
        return False
    blocker = next(
        (
            item
            for item in session.map_completion_blockers
            if item.get("reason") == "map_review_required"
        ),
        None,
    )
    if blocker is None:
        return False
    try:
        reviewer = get_agent("map-reviewer-agent", set(REGISTRY))
    except KeyError:
        return False
    _pop_last_assistant_final(session)
    task_payload = {
        "reason": "map_review_required",
        "target_path": str(blocker.get("target", "")),
        "required_revision": blocker.get("required_revision"),
        "instruction": (
            "继续完成地图视觉复核，不要结束任务。使用 capture_viewport_screenshot "
            "检查当前地图；必要时用 describe_map_region/validate_map_region 复核。"
            "只输出 map_worker_result_v1 JSON，stage='reviewer'，"
            "并在 validation.completion_allowed 中给出是否允许完成。"
        ),
    }
    child = Frame(
        id=session.new_frame_id(),
        agent=reviewer,
        messages=[
            {"role": "system", "content": reviewer.prompt},
            {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
        ],
        parent_id=frame.id,
        depth=frame.depth + 1,
        history_anchor_frame_id=frame.history_anchor_frame_id or frame.id,
        history_anchor_message_index=(
            frame.history_anchor_message_index
            if frame.history_anchor_message_index is not None
            else len(frame.messages)
        ),
    )
    session.agent_stack.append(child)
    return True


def _has_map_review_required(blockers: list[dict[str, Any]]) -> bool:
    """判断完成门阻断里是否包含待视觉复核项。"""
    return any(item.get("reason") == "map_review_required" for item in blockers)


def _has_only_map_review_required(blockers: list[dict[str, Any]]) -> bool:
    """Return true when visual review is the only remaining map completion blocker."""
    return bool(blockers) and all(item.get("reason") == "map_review_required" for item in blockers)


def _format_map_completion_blockers_for_prompt(blockers: list[dict[str, Any]]) -> str:
    """Build a compact, model-facing blocker list for a continuation turn."""
    lines: list[str] = []
    for index, blocker in enumerate(blockers[:5], start=1):
        tool = str(blocker.get("tool", "map tool"))
        reason = str(blocker.get("reason", "blocked"))
        target = str(blocker.get("target", ""))
        revision = blocker.get("required_revision")
        issues = blocker.get("issues", [])
        issue_text = ""
        if isinstance(issues, list) and issues:
            issue_text = "; ".join(str(issue) for issue in issues[:4] if str(issue).strip())
        parts = [f"{index}. tool={tool}", f"reason={reason}"]
        if target:
            parts.append(f"target={target}")
        if isinstance(revision, int) and not isinstance(revision, bool):
            parts.append(f"map_revision={revision}")
        if issue_text:
            parts.append(f"issues={issue_text}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _schedule_map_completion_continuation(session: Session) -> bool:
    """Turn a blocked final answer into another root-agent work step."""
    frame = session.top_frame()
    if frame is None:
        return False
    _pop_last_assistant_final(session)
    blocker_text = _format_map_completion_blockers_for_prompt(session.map_completion_blockers)
    frame.messages.append(
        {
            "role": "system",
            "internal": True,
            "content": (
                "MAP_COMPLETION_GATE_BLOCKED\n"
                "Your previous response attempted to finish the map task, but the service "
                "completion gate is still blocked. Do not summarize or answer final yet.\n\n"
                f"Current blockers:\n{blocker_text}\n\n"
                "Continue the task now. Pick the next concrete repair/verification action and "
                "call the appropriate tool. If the blocker came from validate_map_region, fix "
                "the reported region with small map edits or object changes, then run "
                "validate_map_region again. If visual review is still needed, capture or inspect "
                "the map and then validate again. Only final-answer after a same-revision result "
                "has completion_allowed=true and no blocking_completion blockers remain."
            ),
        }
    )
    return True


def _map_completion_gate_text(blockers: list[dict[str, Any]]) -> str:
    """生成地图完成门拦截后的最终回复文本。"""
    issue_lines: list[str] = []
    for blocker in blockers[:3]:
        tool = str(blocker.get("tool", "map tool"))
        reason = str(blocker.get("reason", "blocked"))
        issues = blocker.get("issues", [])
        if isinstance(issues, list) and issues:
            issue_lines.append(f"- {tool}: {reason}; {str(issues[0])}")
        else:
            issue_lines.append(f"- {tool}: {reason}")
    details = "\n".join(issue_lines)
    return (
        "地图任务还不能标记为完成。\n\n"
        f"{details}\n\n"
        "需要继续按小批编辑、分段 validate_map_region、截图复核的流程修完；"
        "在 completion_allowed=true 前，最终回复已被服务层拦截。"
    )


def _replace_last_assistant_final(session: Session, text: str) -> None:
    """用服务层拦截文本替换最近一条无工具调用 assistant 回复。"""
    frame = session.top_frame()
    if frame is None or not frame.messages:
        return
    last = frame.messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        last["content"] = text


def _unknown_tool_result_summary(payload: dict[str, Any], inner: dict[str, Any]) -> str:
    """为缺少 tool call 元数据的旧历史结果生成保守摘要。"""
    status = str(payload.get("status", "")).strip()
    for key in ("message", "error", "error_code"):
        value = inner.get(key, payload.get(key))
        if status in {"error", "rejected"} and value not in (None, ""):
            return f"Tool {status}: {value}"
    path = str(inner.get("path", "")).strip()
    root_name = str(inner.get("root_name", "")).strip()
    root_type = str(inner.get("root_type", "")).strip()
    lines = ["Tool result"]
    if status:
        lines[0] = f"Tool {status}"
    if path:
        lines.append(f"Done: `{path}`")
    if root_name or root_type:
        lines.append(f"Root: {root_name} ({root_type})".strip())
    return "\n".join(lines)


def _front_tool_summary(name: str, input_args: dict[str, Any], result: dict[str, Any]) -> str:
    """为前端工具生成可读摘要，并保留返回的节点与状态。"""
    title = name.replace("_", " ").capitalize()
    if name == "read_scene_tree":
        node_name = str(result.get("name", result.get("path", "Scene"))).strip()
        node_type = str(result.get("type", "Node")).strip()
        children = result.get("children", [])
        child_count = len(children) if isinstance(children, list) else 0
        suffix = f": {node_name} ({node_type})" if node_name else ""
        return f"Read scene tree{suffix}\n{child_count} top-level child node(s)"
    if name == "read_runtime_state":
        edited_scene = result.get("edited_scene", {})
        scene_name = (
            str(edited_scene.get("name", edited_scene.get("path", "Scene"))).strip()
            if isinstance(edited_scene, dict)
            else "Scene"
        )
        selected = result.get("selected_nodes", [])
        selected_count = len(selected) if isinstance(selected, list) else 0
        return f"Read runtime state: {scene_name}\n{selected_count} selected node(s)"
    if name == "read_image_metadata":
        path = str(result.get("path", input_args.get("path", ""))).strip()
        width = result.get("width")
        height = result.get("height")
        colors = result.get("dominant_colors", [])
        color_values = [
            str(item.get("hex", "")).strip()
            for item in colors
            if isinstance(item, dict) and str(item.get("hex", "")).strip()
        ][:5]
        lines = [f"Read image metadata: {path}" if path else "Read image metadata"]
        if width is not None and height is not None:
            lines.append(f"{width}x{height}")
        if color_values:
            lines.append("Dominant colors: " + ", ".join(color_values))
        return "\n".join(lines)
    if name == "capture_viewport_screenshot":
        path = str(result.get("path", result.get("absolute_path", ""))).strip()
        width = result.get("width")
        height = result.get("height")
        lines = [f"Capture viewport screenshot: {path}" if path else "Capture viewport screenshot"]
        if width is not None and height is not None:
            lines.append(f"{width}x{height}")
        return "\n".join(lines)
    if name == "read_class_docs":
        cls = input_args.get("class_name", "")
        title = f"Read class docs: {cls}" if cls else "Read class docs"
    elif name == "add_node":
        node_type = input_args.get("type", "")
        node_name = input_args.get("name", "")
        parent = input_args.get("parent_path", ".")
        title = f"Add {node_type} '{node_name}' under '{parent}'"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        path = str(result.get("path", "")).strip()
        lines = [title]
        if path:
            lines.append(f"Done: `{path}`")
        return "\n".join(lines)
    elif name == "set_node_property":
        path = input_args.get("path", "")
        prop = input_args.get("property", "")
        value = input_args.get("value", "")
        title = f"Set {path}.{prop} = {value}"
    elif name == "instance_scene":
        scene_path = input_args.get("scene_path", "")
        parent = input_args.get("parent_path", ".")
        title = f"Instance {scene_path} under '{parent}'"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        path = str(result.get("path", "")).strip()
        position = result.get("position", {})
        lines = [title]
        if path:
            lines.append(f"Done: `{path}`")
        if isinstance(position, dict) and ("x" in position or "y" in position):
            lines.append(f"Position: ({position.get('x', '?')}, {position.get('y', '?')})")
        return "\n".join(lines)
    elif name == "duplicate_node":
        path = input_args.get("path", "")
        title = f"Duplicate node {path}"
    elif name == "open_scene":
        path = input_args.get("path", "")
        title = f"Open scene {path}"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        opened_path = str(result.get("path", path)).strip()
        root_name = str(result.get("root_name", "")).strip()
        root_type = str(result.get("root_type", "")).strip()
        lines = [title]
        if opened_path:
            lines.append(f"Done: `{opened_path}`")
        if root_name or root_type:
            lines.append(f"Root: {root_name} ({root_type})".strip())
        return "\n".join(lines)
    elif name == "save_scene":
        title = "Save current scene"
    elif name == "delete_node":
        path = input_args.get("path", "")
        title = f"Delete node {path}"
    elif name == "reparent_node":
        path = input_args.get("path", "")
        new_parent = input_args.get("new_parent_path", "")
        title = f"Reparent {path} under '{new_parent}'"
    elif name == "rename_node":
        path = input_args.get("path", "")
        new_name = input_args.get("name", "")
        title = f"Rename {path} to '{new_name}'"
    elif name == "connect_signal":
        path = input_args.get("path", "")
        signal = input_args.get("signal", "")
        target = input_args.get("target_path", "")
        method = input_args.get("method", "")
        title = f"Connect {path}.{signal} -> {target}.{method}"
    elif name == "disconnect_signal":
        path = input_args.get("path", "")
        signal = input_args.get("signal", "")
        target = input_args.get("target_path", "")
        method = input_args.get("method", "")
        title = f"Disconnect {path}.{signal} -> {target}.{method}"
    elif name == "add_to_group":
        path = input_args.get("path", "")
        group = input_args.get("group", "")
        title = f"Add {path} to group '{group}'"
    elif name == "remove_from_group":
        path = input_args.get("path", "")
        group = input_args.get("group", "")
        title = f"Remove {path} from group '{group}'"
    elif name == "bake_navigation_mesh":
        path = input_args.get("path", "")
        title = f"Bake navigation mesh for {path}"
    elif name == "create_animation_track":
        player = input_args.get("player_path", "")
        animation = input_args.get("animation", "")
        track_path = input_args.get("track_path", "")
        title = f"Set animation track {animation}@{player} ({track_path})"
    elif name == "run_tests":
        kind = input_args.get("kind", "")
        title = f"Run tests ({kind})" if kind else "Run tests"
    elif name == "run_headless_self_test":
        title = "Run headless self-test"
    elif name == "describe_map_context":
        error = _front_tool_error_message(result)
        if error:
            return f"Describe map context\nError: {error}"
        scene = str(result.get("scene", "")).strip()
        revision = result.get("map_revision")
        maps = result.get("maps", []) if isinstance(result.get("maps"), list) else []
        map_count = len(maps)
        total_layers = sum(
            len(m.get("layers", [])) if isinstance(m.get("layers"), list) else 0
            for m in maps
            if isinstance(m, dict)
        )
        total_cells = 0
        for m in maps:
            if isinstance(m, dict):
                for layer in (m.get("layers", []) if isinstance(m.get("layers"), list) else []):
                    if isinstance(layer, dict):
                        total_cells += int(layer.get("cell_count", 0) or 0)
        lines: list[str] = ["Describe map context"]
        if scene:
            lines.append(f"Scene: `{scene}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        lines.append(f"{map_count} map(s), {total_layers} layer(s), {total_cells} cell(s)")
        notes = result.get("notes", []) if isinstance(result.get("notes"), list) else []
        for note in notes[:3]:
            if isinstance(note, str):
                lines.append(f"  • {note}")
        return "\n".join(lines)
    elif name == "edit_map":
        error = _front_tool_error_message(result)
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        cells = result.get("cells")
        ops = result.get("operations")
        mode = str(result.get("mode", "")).strip()
        lines = ["Edit map"]
        if target:
            lines.append(f"Target: `{target}`")
        if error:
            lines.append(f"Error: {error}")
            if revision is not None:
                lines.append(f"Revision: {revision}")
            return "\n".join(lines)
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if ops is not None:
            lines.append(f"Operations: {ops}")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        if mode:
            lines.append(f"Mode: {mode}")
        validation = result.get("validation")
        if isinstance(validation, dict):
            v_passed = validation.get("passed")
            v_issues = (
                validation.get("issues", []) if isinstance(validation.get("issues"), list) else []
            )
            if v_passed is True:
                lines.append("Validation: Passed ✓")
            elif v_passed is False:
                lines.append("Validation: Failed ✗")
                for issue in v_issues[:3]:
                    if isinstance(issue, str):
                        lines.append(f"  • {issue}")
        gap = result.get("coverage_gap_warning")
        if gap and isinstance(gap, str):
            lines.append(f"Coverage gap: {gap}")
        return "\n".join(lines)
    elif name == "repair_layer_coverage":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair layer coverage\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        repaired = result.get("repaired", False)
        cells = result.get("cells")
        lines = ["Repair layer coverage"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if repaired:
            lines.append("Repaired ✓")
        else:
            lines.append("No repair needed")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        return "\n".join(lines)
    elif name == "repair_placements":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair placements\nError: {error}"
        target = str(result.get("target", "")).strip()
        repaired = result.get("repaired_count", result.get("repaired"))
        lines = ["Repair placements"]
        if target:
            lines.append(f"Target: `{target}`")
        if repaired is not None:
            lines.append(f"Repaired: {repaired}")
        return "\n".join(lines)
    elif name == "repair_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair map region\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        lines = ["Repair map region"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        return "\n".join(lines)
    elif name == "compact_spatial_index":
        error = _front_tool_error_message(result)
        if error:
            return f"Compact spatial index\nError: {error}"
        entries = result.get("entries_total", result.get("entries"))
        lines = ["Compact spatial index: Done"]
        if entries is not None:
            lines.append(f"Entries: {entries}")
        return "\n".join(lines)
    elif name == "write_resource_registry":
        error = _front_tool_error_message(result)
        if error:
            return f"Write resource registry\nError: {error}"
        count = result.get("resource_count", result.get("count"))
        lines = ["Write resource registry: Done"]
        if count is not None:
            lines.append(f"Resources: {count}")
        return "\n".join(lines)
    elif name == "save_map_blueprint":
        error = _front_tool_error_message(result)
        if error:
            return f"Save map blueprint\nError: {error}"
        path = str(result.get("path", "")).strip()
        lines = ["Save map blueprint"]
        if path:
            lines.append(f"Saved: `{path}`")
        return "\n".join(lines)
    elif name == "apply_map_blueprint":
        error = _front_tool_error_message(result)
        if error:
            return f"Apply map blueprint\nError: {error}"
        target = str(result.get("target", "")).strip()
        lines = ["Apply map blueprint: Done"]
        if target:
            lines.append(f"Target: `{target}`")
        return "\n".join(lines)
    elif name == "ensure_standard_map_layers":
        error = _front_tool_error_message(result)
        if error:
            return f"Ensure standard map layers\nError: {error}"
        target = str(result.get("target", "")).strip()
        created = result.get("created_count", result.get("created"))
        lines = ["Ensure standard map layers"]
        if target:
            lines.append(f"Target: `{target}`")
        if created is not None:
            lines.append(f"Created: {created} layer(s)")
        return "\n".join(lines)
    elif name == "paint_terrain_connect":
        error = _front_tool_error_message(result)
        if error:
            return f"Paint terrain connect\nError: {error}"
        target = str(result.get("target", "")).strip()
        cells = result.get("cells")
        lines = ["Paint terrain connect"]
        if target:
            lines.append(f"Target: `{target}`")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        return "\n".join(lines)
    elif name == "place_map_objects":
        error = _front_tool_error_message(result)
        if error:
            return f"Place map objects\nError: {error}"
        placed = result.get("placed_count", result.get("placed"))
        lines = ["Place map objects"]
        if placed is not None:
            lines.append(f"Placed: {placed} object(s)")
        return "\n".join(lines)
    elif name == "describe_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Describe map region\nError: {error}"
        lines: list[str] = ["Describe map region"]

        # Check if this is a cell-focused result (has cells_format)
        cells_format = result.get("cells_format")
        if cells_format:
            cells_total = result.get("cells_total", 0)
            cells_returned = result.get("cells_returned", 0)
            non_empty_count = result.get("non_empty_count", 0)
            artifact_ref = result.get("artifact_ref", "")
            lines.append(
                f"Cells: {cells_total} total, {cells_returned} returned, {non_empty_count} non-empty"
            )
            if artifact_ref:
                lines.append(f"Artifact: `{artifact_ref}`")
        else:
            # Map overview result
            scene = str(result.get("scene", "")).strip()
            revision = result.get("map_revision")
            maps = result.get("maps", []) if isinstance(result.get("maps"), list) else []
            map_count = len(maps)
            total_layers = sum(
                len(m.get("layers", [])) if isinstance(m.get("layers"), list) else 0
                for m in maps
                if isinstance(m, dict)
            )
            total_cells = 0
            for m in maps:
                if isinstance(m, dict):
                    for layer in (m.get("layers", []) if isinstance(m.get("layers"), list) else []):
                        if isinstance(layer, dict):
                            total_cells += int(layer.get("cell_count", 0) or 0)
            if scene:
                lines.append(f"Scene: `{scene}`")
            if revision is not None:
                lines.append(f"Revision: {revision}")
            lines.append(f"{map_count} map(s), {total_layers} layer(s), {total_cells} cell(s)")

        notes = result.get("notes", []) if isinstance(result.get("notes"), list) else []
        for note in notes[:3]:
            if isinstance(note, str):
                lines.append(f"  • {note}")
        return "\n".join(lines)
    elif name == "validate_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate map region\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        passed = result.get("passed", False)
        region = result.get("region", {}) if isinstance(result.get("region"), dict) else {}
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate map region"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if isinstance(region, dict) and region:
            x = region.get("x", "?")
            y = region.get("y", "?")
            w = region.get("width", "?")
            h = region.get("height", "?")
            lines.append(f"Region: ({x}, {y}) {w}×{h}")
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "validate_layer_coverage":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate layer coverage\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate layer coverage"]
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "validate_object_placements":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate object placements\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        checked = result.get("checked_count", result.get("total_checked"))
        lines = ["Validate object placements"]
        if checked is not None:
            lines.append(f"Checked: {checked} object(s)")
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "query_spatial_index":
        error = _front_tool_error_message(result)
        if error:
            return f"Query spatial index\nError: {error}"
        count = result.get("count", result.get("entries_count"))
        lines = ["Query spatial index"]
        if count is not None:
            lines.append(f"{count} entries")
        return "\n".join(lines)
    elif name == "find_placement_anchors":
        error = _front_tool_error_message(result)
        if error:
            return f"Find placement anchors\nError: {error}"
        anchors = result.get("anchors", []) if isinstance(result.get("anchors"), list) else []
        lines = ["Find placement anchors"]
        lines.append(f"{len(anchors)} anchor(s) found")
        return "\n".join(lines)
    elif name == "sample_noise_grid":
        error = _front_tool_error_message(result)
        if error:
            return f"Sample noise grid\nError: {error}"
        count = result.get("sample_count", result.get("count"))
        lines = ["Sample noise grid"]
        if count is not None:
            lines.append(f"{count} samples")
        return "\n".join(lines)
    elif name == "sample_poisson_points":
        error = _front_tool_error_message(result)
        if error:
            return f"Sample Poisson points\nError: {error}"
        count = result.get("point_count", result.get("count"))
        lines = ["Sample Poisson points"]
        if count is not None:
            lines.append(f"{count} points")
        return "\n".join(lines)
    elif name == "compose_map_blueprint_grammar":
        error = _front_tool_error_message(result)
        if error:
            return f"Compose map blueprint grammar\nError: {error}"
        lines = ["Compose map blueprint grammar: Done"]
        return "\n".join(lines)
    elif name == "validate_scene_state":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate scene state\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate scene state"]
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "run_system_command":
        shell = str(result.get("shell", input_args.get("shell", "auto")))
        status = str(result.get("status", "unknown"))
        exit_code = result.get("exit_code")
        command = str(input_args.get("command", "")).strip()
        summary = f"Shell {command}" if command else "Run system command"
        detail = f"{status} (shell={shell}"
        if exit_code is not None:
            detail += f", exit={exit_code}"
        detail += ")"
        output = str(result.get("output", "")).strip()
        lines = [summary, detail]
        if output:
            lines.extend(["```", _truncate_text(output, 4000), "```"])
        return "\n".join(lines)
    error = _front_tool_error_message(result)
    if error:
        return f"{title}\nError: {error}"
    # Compact shallow fallback: only top-level scalar keys, never recurse into nested dicts/lists
    lines = [f"{title}:"]
    _SHALLOW_SKIP_KEYS = frozenset({"error", "errors", "detail", "details", "traceback"})
    for key, val in list(result.items())[:12]:
        if key in _SHALLOW_SKIP_KEYS:
            continue
        if isinstance(val, (dict, list)):
            if isinstance(val, list):
                lines.append(f"{key}: {len(val)} item(s)")
            else:
                lines.append(f"{key}: {len(val)} field(s)")
        elif val not in (None, ""):
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _display_tool_content(content: str) -> str:
    """Pretty-print JSON tool content when possible，并截断过长内容。"""
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, _HISTORY_PREVIEW_LIMIT)
    text = json.dumps(parsed, ensure_ascii=False, indent=2)
    return "```json\n" + _truncate_text(text, _HISTORY_PREVIEW_LIMIT) + "\n```"


def _compact_tool_summary(name: str, inner: dict[str, Any], input_args: dict[str, Any]) -> str:
    """Generate concise tool result summary instead of full JSON dump.

    For tools not in specific categories (read/edit/grep), create a short
    key-value style summary showing only important fields, similar to the
    frontend EventFormatter display.

    Args:
        name: Tool name
        inner: Parsed tool result dictionary
        input_args: Tool input arguments

    Returns:
        Compact summary string, e.g., "Validate map region:\n• passed: True\n• issues_count: 0"
    """
    # Extract important status/result fields
    summary_parts = []

    # Common status fields
    if "ok" in inner:
        summary_parts.append(f"ok: {inner['ok']}")
    if "passed" in inner:
        summary_parts.append(f"passed: {inner['passed']}")
    if "success" in inner:
        summary_parts.append(f"success: {inner['success']}")
    if "status" in inner:
        summary_parts.append(f"status: {inner['status']}")

    # Common result fields
    if "message" in inner:
        msg = str(inner["message"])
        if len(msg) > 100:
            msg = msg[:100] + "..."
        summary_parts.append(f"message: {msg}")
    if "result" in inner and not isinstance(inner["result"], (dict, list)):
        summary_parts.append(f"result: {inner['result']}")
    if "count" in inner:
        summary_parts.append(f"count: {inner['count']}")
    if "issues_count" in inner:
        summary_parts.append(f"issues_count: {inner['issues_count']}")
    if "issues" in inner and isinstance(inner["issues"], list):
        count = len(inner["issues"])
        if count > 0:
            summary_parts.append(f"issues: {count} item(s)")

    # Path/file related fields
    if "path" in inner:
        summary_parts.append(f"path: {inner['path']}")
    if "file_path" in inner:
        summary_parts.append(f"file_path: {inner['file_path']}")

    # If no important fields found, show a minimal summary
    if not summary_parts:
        # Show a few generic fields if present
        for key in ["target", "region", "data", "output"]:
            if key in inner:
                val = inner[key]
                if isinstance(val, dict):
                    summary_parts.append(f"{key}: {len(val)} field(s)")
                elif isinstance(val, list):
                    summary_parts.append(f"{key}: {len(val)} item(s)")
                elif isinstance(val, str) and len(val) <= 100:
                    summary_parts.append(f"{key}: {val}")
                else:
                    summary_parts.append(f"{key}: {type(val).__name__}")

    # Format as bullet list
    if summary_parts:
        display_name = name.replace("_", " ").title()
        return f"{display_name}:\n" + "\n".join(f"• {part}" for part in summary_parts)
    else:
        # Fallback: just show tool name with success status
        display_name = name.replace("_", " ").title()
        return f"{display_name}: completed"


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
            "出错：自动 describe_map_region 请求超过 1600 cells",
        )
    )


def _history_items_for_frame(
    frame: Frame, *, include_system_prompt: bool = False
) -> list[SessionHistoryItemDTO]:
    """Convert stored LLM messages into chat-panel friendly history items."""
    items: list[SessionHistoryItemDTO] = []
    if not include_system_prompt and frame.compact_snapshot is not None:
        items.append(
            SessionHistoryItemDTO(
                role="system",
                text=frame.compact_snapshot.summary,
                frame_id=frame.id,
                agent=frame.agent.name,
            )
        )
    tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, message in enumerate(frame.messages):
        if _is_internal_history_message(message):
            continue
        role = str(message.get("role", "system"))
        content = message.get("content", "")
        text = (
            ""
            if content is None
            else flatten_message_text(content) if isinstance(content, list) else str(content)
        )

        if role == "system":
            if index == 0 and not include_system_prompt:
                continue
            if not text.strip():
                continue
            items.append(
                SessionHistoryItemDTO(
                    role="system", text=text, frame_id=frame.id, agent=frame.agent.name
                )
            )
            continue

        if role == "user":
            text = _display_user_content(text)
            if text.strip():
                items.append(
                    SessionHistoryItemDTO(
                        role="user", text=text, frame_id=frame.id, agent=frame.agent.name
                    )
                )
            continue

        if role == "assistant":
            if text.strip():
                items.append(
                    SessionHistoryItemDTO(
                        role="assistant", text=text, frame_id=frame.id, agent=frame.agent.name
                    )
                )
            tool_calls = message.get("tool_calls", [])
            if isinstance(tool_calls, list) and tool_calls:
                lines = ["Tool calls"]
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function", {})
                    name = "unknown"
                    arguments: dict[str, Any] = {}
                    if isinstance(function, dict):
                        name = str(function.get("name", "unknown"))
                        arguments = _parse_tool_call_arguments(function.get("arguments"))
                    call_id = str(call.get("id", ""))
                    if call_id:
                        tool_calls_by_id[call_id] = (name, arguments)
                    lines.append(f"- `{name}`")
                items.append(
                    SessionHistoryItemDTO(
                        role="system",
                        text="\n".join(lines),
                        frame_id=frame.id,
                        agent=frame.agent.name,
                    )
                )
            continue

        if role == "tool":
            tool_call_id = str(message.get("tool_call_id", ""))
            tool_name, tool_args = tool_calls_by_id.get(tool_call_id, ("", {}))
            items.append(
                SessionHistoryItemDTO(
                    role="system",
                    text=_format_tool_result_summary(tool_name, tool_args, text),
                    frame_id=frame.id,
                    agent=frame.agent.name,
                )
            )
            continue

        if text.strip():
            items.append(
                SessionHistoryItemDTO(
                    role="system", text=text, frame_id=frame.id, agent=frame.agent.name
                )
            )
    return items


def _history_text_fingerprint(text: str) -> str:
    """生成用于历史条目去重的稳定文本指纹。"""
    return " ".join(text.split())


def _append_history_item_if_new(
    items: list[SessionHistoryItemDTO],
    seen: set[str],
    item: SessionHistoryItemDTO | None,
) -> None:
    """将非空且未重复的历史条目追加到目标列表。"""
    if item is None:
        return
    fingerprint = _history_text_fingerprint(item.text)
    if not fingerprint or fingerprint in seen:
        return
    seen.add(fingerprint)
    items.append(item)


def _history_title_with_body(title: str, body: str) -> str:
    """组合 workflow 历史条目的标题行与 Markdown 正文。"""
    stripped = body.strip()
    if not stripped:
        return title
    return f"{title}\n{stripped}"


def _format_plan_created_history_event(payload: dict[str, Any]) -> str:
    """把 `plan_created` 事件转换成前端可渲染的 Markdown 历史文本。"""
    summary = str(payload.get("summary", "")).strip()
    steps = payload.get("steps", [])
    body_lines: list[str] = []
    if summary:
        body_lines.append(summary)
    if isinstance(steps, list) and steps:
        if body_lines:
            body_lines.append("")
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                continue
            index = int(raw_step.get("index", 0))
            title = str(raw_step.get("title", "")).strip()
            agent = str(raw_step.get("agent", "")).strip()
            task = str(raw_step.get("task", "")).strip()
            label = title or task or "Untitled step"
            suffix = f" ({agent})" if agent else ""
            body_lines.append(f"{index}. {label}{suffix}")
            if task and task != title:
                body_lines.append(f"   - {task}")
    return _history_title_with_body("Plan created:", "\n".join(body_lines))


def _format_plan_step_started_history_event(payload: dict[str, Any]) -> str:
    """把 `plan_step_started` 事件转换成前端可渲染的历史文本。"""
    index = int(payload.get("step_index", 0))
    total = int(payload.get("total_steps", 0))
    title = str(payload.get("title", "")).strip()
    agent = str(payload.get("agent", "")).strip()
    suffix = f" ({agent})" if agent else ""
    return _history_title_with_body(f"Step {index}/{total} started:", f"{title}{suffix}".strip())


def _format_plan_step_completed_history_event(payload: dict[str, Any]) -> str:
    """把 `plan_step_completed` 事件转换成前端可渲染的历史文本。"""
    index = int(payload.get("step_index", 0))
    total = int(payload.get("total_steps", 0))
    summary = str(payload.get("summary", "")).strip()
    return _history_title_with_body(f"Step {index}/{total} completed:", summary)


def _format_verify_started_history_event(payload: dict[str, Any]) -> str:
    """把 `verify_started` 事件转换成前端可渲染的历史文本。"""
    file_path = str(payload.get("file_path", "")).strip()
    phase = str(payload.get("phase", "")).strip()
    suffix = f" ({phase})" if phase else ""
    return _history_title_with_body("Verify started:", f"{file_path}{suffix}".strip())


def _format_verify_completed_history_event(payload: dict[str, Any]) -> str:
    """把 `verify_completed` 事件转换成前端可渲染的历史文本。"""
    summary = str(payload.get("summary", "")).strip()
    if bool(payload.get("passed", False)):
        return _history_title_with_body("Verify passed:", summary)
    issues_count = int(payload.get("issues_count", 0))
    return _history_title_with_body(f"Verify found {issues_count} issue(s):", summary)


def _history_item_for_event(event: Event) -> SessionHistoryItemDTO | None:
    """把可回放的 workflow 事件转换成单条历史条目。"""
    payload = event.payload
    match event.type:
        case "plan_created":
            return SessionHistoryItemDTO(
                role="system", text=_format_plan_created_history_event(payload)
            )
        case "plan_step_started":
            return SessionHistoryItemDTO(
                role="system", text=_format_plan_step_started_history_event(payload)
            )
        case "plan_step_completed":
            return SessionHistoryItemDTO(
                role="system", text=_format_plan_step_completed_history_event(payload)
            )
        case "verify_started":
            return SessionHistoryItemDTO(
                role="system", text=_format_verify_started_history_event(payload)
            )
        case "verify_completed":
            return SessionHistoryItemDTO(
                role="system", text=_format_verify_completed_history_event(payload)
            )
        case _:
            return None


def _history_item_for_stream_event(event: Event) -> SessionHistoryItemDTO | None:
    """把最后一次流式增量转换成历史条目，保留中间 Thought/子 agent 输出。"""
    text = str(event.payload.get("text", "")).strip()
    if not text:
        return None
    frame_id = str(event.payload.get("frame_id", "")) or None
    if event.type == "agent_reasoning_delta":
        text = f"Thought for 0.00s\n{text}"
    return SessionHistoryItemDTO(role="assistant", text=text, frame_id=frame_id)


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


def _history_items_for_events(events: list[Event], seen: set[str]) -> list[SessionHistoryItemDTO]:
    """从事件日志中恢复不在 frame messages 里的 workflow 历史条目。"""
    items: list[SessionHistoryItemDTO] = []
    current_stream_events: list[Event] = []
    current_stream_key: tuple[str, str, str] | None = None

    def flush_stream() -> None:
        nonlocal current_stream_events, current_stream_key
        current_stream = _merged_stream_event(current_stream_events)
        stream_item = _history_item_for_stream_event(current_stream) if current_stream else None
        _append_history_item_if_new(items, seen, stream_item)
        current_stream_events = []
        current_stream_key = None

    for event in events:
        if event.type in {"agent_text_delta", "agent_reasoning_delta"}:
            payload = event.payload
            stream_key = (
                event.type,
                str(payload.get("frame_id", "")),
                str(payload.get("message_index", payload.get("loop", ""))),
            )
            if current_stream_key is not None and stream_key != current_stream_key:
                flush_stream()
            current_stream_events.append(event)
            current_stream_key = stream_key
            continue

        flush_stream()
        _append_history_item_if_new(items, seen, _history_item_for_event(event))

    flush_stream()
    return items


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


def _tool_history_blocks(
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


def _system_history_blocks(frame: Frame, text: str) -> list[SessionHistoryBlock]:
    inner = _json_object(text)
    verify = inner.get("verify")
    if not isinstance(verify, dict):
        return []
    origin = _history_origin(frame)
    file_path = str(verify.get("file_path", ""))
    summary = str(verify.get("summary", ""))
    if bool(verify.get("passed", False)):
        return [VerifyPassedHistoryBlock(file_path=file_path, summary=summary, **origin)]
    issues = verify.get("issues", [])
    issues_count = len(issues) if isinstance(issues, list) else 0
    return [
        VerifyFailedHistoryBlock(
            file_path=file_path,
            issues_count=issues_count,
            summary=summary,
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
    if role == "user":
        if frame.parent_id is not None and message_index == 1:
            return []
        displayed = _display_user_content(text).strip()
        return [UserHistoryBlock(text=displayed, **origin)] if displayed else []
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
        return _assistant_history_blocks(
            frame,
            text,
            has_tool_calls=bool(calls),
            include_thought_summary=include_thought_summary,
        )
    if role == "tool":
        call_id = str(message.get("tool_call_id", ""))
        name, input_args = tool_calls_by_id.get(call_id, ("", {}))
        return _tool_history_blocks(frame, name, input_args, text)
    if role == "system":
        if is_initial_system or not text.strip():
            return []
        return _system_history_blocks(frame, text)
    return [SystemTextHistoryBlock(text=text, **origin)] if text.strip() else []


def _event_history_blocks(event: Event) -> list[SessionHistoryBlock]:
    payload = event.payload
    frame_id = str(payload.get("frame_id", "")) or None
    agent = str(payload.get("agent", "")) or None
    origin = {"frame_id": frame_id, "agent": agent}
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
        if bool(payload.get("passed", False)):
            return [
                VerifyPassedHistoryBlock(
                    file_path=str(payload.get("file_path", "")),
                    summary=str(payload.get("summary", "")),
                    **origin,
                )
            ]
        return [
            VerifyFailedHistoryBlock(
                file_path=str(payload.get("file_path", "")),
                issues_count=int(payload.get("issues_count", 0)),
                summary=str(payload.get("summary", "")),
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


def _block_fingerprint(block: SessionHistoryBlock) -> str:
    data = block.model_dump(exclude={"frame_id", "agent"})
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _structured_history_for_frame(frame: Frame, events: list[Event]) -> list[SessionHistoryBlock]:
    """Interleave frame messages with events anchored to their upcoming message index."""
    assistant_indexes = [
        index
        for index, message in enumerate(frame.messages)
        if str(message.get("role", "")) == "assistant"
    ]
    anchored: dict[int, list[SessionHistoryBlock]] = {}
    trailing: list[SessionHistoryBlock] = []
    legacy_stream_anchor = 0
    legacy_stream_key: tuple[str, str] | None = None
    stream_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
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
        elif event.type in {"agent_reasoning_delta", "agent_text_delta"} and assistant_indexes:
            key = (str(event.payload.get("loop", "")), str(event.payload.get("frame_id", "")))
            if legacy_stream_key is not None and key != legacy_stream_key:
                legacy_stream_anchor += 1
            legacy_stream_key = key
            message_index = assistant_indexes[min(legacy_stream_anchor, len(assistant_indexes) - 1)]
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
    seen: set[str] = set()

    def append_unique(block: SessionHistoryBlock) -> None:
        fingerprint = _block_fingerprint(block)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        result.append(block)

    tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, message in enumerate(frame.messages):
        event_blocks = anchored.get(index, [])
        has_reasoning_event = any(isinstance(block, ThoughtHistoryBlock) for block in event_blocks)
        for block in event_blocks:
            append_unique(block)
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
            append_unique(block)
    for message_index in sorted(index for index in anchored if index >= len(frame.messages)):
        for block in anchored[message_index]:
            append_unique(block)
    for block in trailing:
        append_unique(block)
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
