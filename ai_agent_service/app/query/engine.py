"""QueryEngine 门面（§13）：HTTP 层与 query_loop 内核之间的会话协调层。

`QueryEngine` 负责：
- 会话锁与本地持久化；
- 用户消息、前端工具结果与 agent 帧消息的转换；
- `request_id` 幂等缓存；
- 当前请求权限模式覆盖；
- 调用 `orchestrator.agent.run_turn()` 并转换为 HTTP DTO。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, CompactSnapshot, Frame
from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatRequest,
    ChatResponse,
    ChatToolCallsResponse,
    DelegateResultDTO,
    DelegateResultHistoryBlock,
    DelegateResultsHistoryBlock,
    ErrorHistoryBlock,
    EventHistoryBlock,
    FrontToolCallDTO,
    GrepMatchDTO,
    InterruptResponse,
    LogEditHistoryBlock,
    LogGrepHistoryBlock,
    LogReadHistoryBlock,
    LogTextHistoryBlock,
    PlanCreatedHistoryBlock,
    PlanStepDTO,
    SessionHistoryBlock,
    SessionHistoryItemDTO,
    SessionHistoryResponse,
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
from app.config import AppSettings
from app.events.store import Event, EventStore
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.message_transformer import estimate_message_tokens, flatten_message_text
from app.llm.provider import LLMError, LLMProvider
from app.orchestrator.agent import (
    EFFORT_TEMPERATURE,
    ErrorResult,
    FinalResult,
    StepResult,
    ToolCallsResult,
    resolve_thinking_budget,
    run_turn,
)
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES, MAP_WRITE_TOOL_NAMES
from app.output_styles.catalog import OutputStyleCatalog
from app.permissions.engine import make_session_allow_grant
from app.prompt.builder import LayeredPrompt, build_system_prompt
from app.prompt.context_builder import ContextBuilder
from app.prompt.project_context import build_project_context
from app.prompt.rag_context import build_rag_context
from app.rag.asset_llm_client import AssetLLMClient, AssetLLMConfig
from app.rag.factory import create_codebase_index
from app.recovery.pointer import RecoveryPointerStore
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.store import Session, SessionStore
from app.skills.catalog import SkillCatalog
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY
from app.tools.server_tools.read_file import read_file_handler
from app.storage.atomic import atomic_write_json
from app.verify.syntax_check import run_syntax_check

logger = logging.getLogger(__name__)
_MODEL_LOG_FIELDS = frozenset({"model", "primary_model", "fallback_model"})
_MAP_CONTEXT_MAX_TARGETS = 8
_MAP_CONTEXT_MAX_REGIONS_PER_LAYER = 24
_MAP_CONTEXT_MAX_SUMMARY_CHARS = 2048
_MAP_CONTEXT_MAX_TOTAL_CHARS = 262_144
_MAP_ATLAS_SUMMARY_LIMIT = 12
_MAP_MATCH_SUMMARY_LIMIT = 12
_MAP_ARTIFACT_MAX_FILES_PER_SESSION = 128
_MAP_ARTIFACT_MAX_BYTES_PER_SESSION = 100 * 1024 * 1024
_HISTORY_TOOL_MAX_JSON_CHARS = 80_000
_HISTORY_TOOL_MAX_STRING_CHARS = 16_000
_HISTORY_TOOL_MAX_LIST_ITEMS = 80
_HISTORY_TOOL_MAX_DICT_ITEMS = 120
_HISTORY_TOOL_DROP_KEYS = frozenset(
    {"data_url", "base64", "image_base64", "screenshot_base64", "binary", "bytes"}
)


def _normalize_model_override(model: str | None) -> str | None:
    """清理请求级模型覆盖；空白值等同于未指定。"""
    if model is None:
        return None
    normalized = model.strip()
    return normalized or None


def _event_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """隐藏事件日志中的模型名，不影响发送给 UI 的原始事件。"""
    return {
        key: "<redacted>" if key in _MODEL_LOG_FIELDS else value for key, value in payload.items()
    }


def _response_from_dict(data: dict[str, Any]) -> ChatResponse:
    """把幂等缓存中的响应字典恢复为具体 DTO。"""
    response_type = data.get("type")
    if response_type == "tool_calls":
        return ChatToolCallsResponse.model_validate(data)
    if response_type == "final":
        return ChatFinalResponse.model_validate(data)
    return ChatErrorResponse.model_validate(data)


def _response_to_dict(response: ChatResponse) -> dict[str, Any]:
    """把三态响应序列化为幂等缓存可存的 dict。"""
    return response.model_dump()


def _step_to_response(step: StepResult) -> ChatResponse:
    """把编排内核结果转换为 `/chat` 三态响应 DTO。"""
    if isinstance(step, ToolCallsResult):
        return ChatToolCallsResponse(
            turn_id=step.turn_id,
            text=step.text,
            calls=[
                FrontToolCallDTO(
                    id=call.id,
                    name=call.name,
                    input=call.input,
                    needs_confirm=call.needs_confirm,
                    frame_id=call.frame_id,
                    agent=call.agent,
                    render_kind=call.render_kind,
                )
                for call in step.calls
            ],
        )
    if isinstance(step, FinalResult):
        return ChatFinalResponse(text=step.text)
    if isinstance(step, ErrorResult):
        return ChatErrorResponse(text=step.text)
    raise TypeError(f"未知编排结果类型：{type(step)!r}")


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
            "role": "user",
            "content": (
                "出错：自动读取没有拿到需要的 state，"
                f"无法恢复挂起的 {tool_name} 调用。"
                f"target_path={target}，缺少 {required_state}。"
                "请重新 describe_map_region 或显式指定 map_layer/expected_revision。"
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
            key=lambda item: int(item[1].get("count", item[1]))
            if isinstance(item[1], dict)
            else int(item[1]),
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
            "non_empty_count": result.get("non_empty_count")
            if "non_empty_count" in result
            else (len(cells) if isinstance(cells, list) else result.get("cells")),
            "cells_omitted": result.get("cells_omitted")
            if "cells_omitted" in result
            else (isinstance(cells, list) and bool(cells)),
            "artifact_ref": artifact_ref,
        }
        if "atlas_summary" in result:
            atlas_summary = _top_atlas_summary(result.get("atlas_summary"))
            summary["atlas_summary"] = atlas_summary
            summary["atlas_summary_top"] = atlas_summary
            summary["atlas_summary_omitted"] = True
        if artifact_ref is not None and (
            result.get("cells_omitted")
            or result.get("cells_returned") != result.get("cells_total")
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
    }:
        slim = dict(payload)
        slim["result"] = _map_result_summary(tool_name, result, artifact_ref)
        return _bounded_tool_message_body(slim)
    if tool_name in {
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
    } or _json_char_size(result) > _HISTORY_TOOL_MAX_JSON_CHARS:
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
    map_layer = _map_layer_from_result(
        result,
        prefer_layers=bool(tool_args.get("__auto_map_state_read", False)),
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
    pipeline_template = str(result_dict.get("pipeline_template", ""))
    if status != "applied":
        return {
            "tool": tool_name,
            "reason": error_code or status,
            "issues": [str(error_code or status)],
            "target": target,
            "required_revision": revision_value,
            "pipeline_template": pipeline_template,
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
            "pipeline_template": pipeline_template,
        }
    if result_dict.get("completion_allowed") is False:
        return {
            "tool": tool_name,
            "reason": "completion_not_allowed",
            "issues": normalized_issues or ["map tool reported completion_allowed=false"],
            "target": target,
            "required_revision": revision_value,
            "pipeline_template": pipeline_template,
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
            "pipeline_template": pipeline_template,
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
    blockers: list[dict[str, Any]], target: str, revision: int | None
) -> list[dict[str, Any]]:
    """清除已被同 revision validate_map_region 覆盖的写后校验阻断。"""
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
    map_layer = tool_args.get("map_layer", tool_args.get("ground_map_layer", 0))
    if not isinstance(map_layer, int) or isinstance(map_layer, bool):
        map_layer = 0
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
    signature = _map_region_read_signature("describe_map_region", tool_args)
    if signature is None:
        return
    revision = _map_revision_from_result(result) if isinstance(result, dict) else None
    if revision is None:
        return
    session.latest_map_region_reads[signature] = revision
    while len(session.latest_map_region_reads) > 64:
        first_key = next(iter(session.latest_map_region_reads))
        del session.latest_map_region_reads[first_key]


def _map_tool_region_read_current(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图工具依赖的区域是否已按当前 revision 读取。"""
    signature = _map_region_read_signature(call.name, call.input)
    if signature is None:
        return True
    read_revision = session.latest_map_region_reads.get(signature)
    if read_revision is None:
        return False
    target = call.input.get("target_path")
    if not isinstance(target, str) or not target:
        return True
    latest_revision = session.latest_map_revisions.get(target)
    return latest_revision is not None and read_revision == latest_revision


def _map_region_read_call_for_tool(call: FrontToolCallDTO) -> FrontToolCallDTO | None:
    """把地图工具调用转换为同一区域的 describe_map_region 调用。"""
    region = _map_region_from_tool_args(call.name, call.input)
    if region is None:
        return None
    read_input: dict[str, Any] = {
        "__auto_map_state_read": True,
        "cells_format": "non_empty_only",
        "max_returned_cells": 120,
    }
    for key in ("target_path", "map_layer", "ground_map_layer"):
        if key in call.input:
            read_input["map_layer" if key == "ground_map_layer" else key] = call.input[key]
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
    read_call = _map_region_read_call_for_tool(guarded_call)
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
    target = call.input.get("target_path")
    target_path = target if isinstance(target, str) else ""
    latest_revision = session.latest_map_revisions.get(target_path)
    latest_layer = session.latest_map_layers.get(target_path)
    restored_input = dict(call.input)

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
    if call.name in _MAP_VALIDATION_TOOL_NAMES and "map_layer" not in restored_input:
        session.pending_map_tool_after_read = None
        _append_map_state_read_error(session, call.name, target_path, "map_layer")
        return None

    restored_call = call.model_copy(update={"input": restored_input})
    text = "已读取真实地图区域，继续执行挂起的地图工具调用。"
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
    return bool(blockers) and all(
        item.get("reason") == "map_review_required" for item in blockers
    )


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
            "role": "user",
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
    return "\n".join([f"{title}:", *_front_result_lines(result)])


def _display_tool_content(content: str) -> str:
    """Pretty-print JSON tool content when possible，并截断过长内容。"""
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, _HISTORY_PREVIEW_LIMIT)
    text = json.dumps(parsed, ensure_ascii=False, indent=2)
    return "```json\n" + _truncate_text(text, _HISTORY_PREVIEW_LIMIT) + "\n```"


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

    return _display_tool_content(content)


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


class QueryEngine:
    """会话级 QueryEngine 门面。

    M0 中该对象可作为进程级单例：内部把不同 `session_id` 分发给
    `SessionStore`，并用 per-session lock 串行化同一会话的请求。
    """

    def __init__(
        self,
        settings: AppSettings,
        session_store: SessionStore,
        llm: LLMProvider,
        base_security: SecuritySettings | None = None,
        skill_catalog: SkillCatalog | None = None,
        output_style_catalog: OutputStyleCatalog | None = None,
        event_store: EventStore | None = None,
        recovery_store: RecoveryPointerStore | None = None,
        cache_engine: CacheDecisionEngine | None = None,
        cache_metrics: CacheMetricsCollector | None = None,
    ) -> None:
        """构造 QueryEngine。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
            cache_engine: 上下文缓存决策引擎（§16.1）；缺省时构造新实例。
            cache_metrics: 缓存命中率观测聚合器；缺省时构造新实例。
        """
        self._settings = settings
        self._store = session_store
        self._llm = llm
        self._base_security = base_security or security_settings_from_app(settings)
        self._skill_catalog = skill_catalog
        self._output_styles = output_style_catalog
        self._events = event_store
        self._recovery = recovery_store
        self._cache_engine = cache_engine or CacheDecisionEngine()
        self._cache_metrics = cache_metrics or CacheMetricsCollector()
        # session_id -> 该会话当前所有"正在处理 /chat 请求"的任务集合（通常只有
        # 一个，但用户可能在前一个请求仍卡在 per-session 锁等待时就发出下一条
        # 消息/中断，short-lived 地出现多个；用 set 而不是单个槎位，避免新任务
        # 覆盖掉真正持有锁、仍在运行的旧任务引用，导致 interrupt() 取消错对象。
        self._active_tasks: dict[str, set[asyncio.Task]] = {}

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    def _map_artifact_path(self, session_id: str, tool_name: str, result: dict[str, Any]) -> Path:
        """构造地图 raw artifact 的项目内路径。"""
        target = str(result.get("target_path", result.get("target", "map")))
        revision = result.get("map_revision", result.get("actual_revision", "unknown"))
        digest = hashlib.sha256(
            json.dumps(
                {
                    "tool": tool_name,
                    "target": target,
                    "revision": revision,
                    "region": _region_summary_from_value(result),
                    "size": _json_char_size(result),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return (
            self._settings.project_root
            / ".ai_agent_service"
            / "artifacts"
            / _safe_artifact_name(session_id)
            / f"{_safe_artifact_name(tool_name)}-{digest}.json"
        )

    def _store_map_artifact(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
    ) -> str | None:
        """把大型地图工具 raw result 写入本地 artifact，返回相对路径引用。"""
        if tool_name not in {
            "describe_map_region",
            "query_spatial_index",
            "validate_map_region",
            "validate_layer_coverage",
            "validate_object_placements",
        }:
            return None
        if not isinstance(result, dict):
            return None
        if tool_name == "describe_map_region" and not (
            isinstance(result.get("cells"), list) or "atlas_summary" in result
        ):
            return None
        if tool_name == "query_spatial_index" and not isinstance(result.get("matches"), list):
            return None
        if tool_name in _MAP_VALIDATION_TOOL_NAMES and _json_char_size(result) < 8_000:
            return None
        path = self._map_artifact_path(session_id, tool_name, result)
        try:
            atomic_write_json(
                path,
                {
                    "tool": tool_name,
                    "input": tool_args,
                    "result": result,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._cleanup_map_artifacts(path.parent)
        except OSError as exc:
            logger.warning(
                "Failed to write map artifact session=%s tool=%s path=%s error=%s",
                session_id,
                tool_name,
                path,
                exc,
            )
            return None
        try:
            return str(path.relative_to(self._settings.project_root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _cleanup_map_artifacts(self, session_dir: Path) -> None:
        """按 LRU 清理单个 session 的地图 artifact 文件。"""
        try:
            files = [path for path in session_dir.iterdir() if path.is_file()]
        except OSError:
            return
        stats: list[tuple[Path, float, int]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            stats.append((path, stat.st_mtime, stat.st_size))
        stats.sort(key=lambda item: item[1], reverse=True)
        total = 0
        for index, (path, _mtime, size) in enumerate(stats):
            total += size
            if (
                index < _MAP_ARTIFACT_MAX_FILES_PER_SESSION
                and total <= _MAP_ARTIFACT_MAX_BYTES_PER_SESSION
            ):
                continue
            try:
                path.unlink()
            except OSError:
                logger.debug("Failed to prune map artifact path=%s", path)

    def session_history(self, session_id: str, limit: int = 200) -> SessionHistoryResponse:
        """Return frontend-renderable history for a persisted session."""
        session = self._store.get_or_create(session_id, self.available_tools)
        events = _persisted_history_events(session)
        if not events and self._events is not None:
            events = self._events.list_after(session_id, 0)
        # 下面的逐 frame/event 转换是 O(frames + events) 的纯 Python 工作；长期
        # 使用的会话（大量 delegate_many 子 agent frame + 持续累积的事件日志）
        # 不加界会让这一步随历史总量无限增长，最终触发前端 30s 看门狗超时、把
        # 本来该串行复用的请求队列卡死。既然最终只展示最近 `limit` 条，这里先
        # 把输入收窄到最近窗口再转换，而不是转换全量历史后再丢弃大半。
        if limit > 0:
            recent_frames = session.agent_stack[-limit:]
            recent_events = events[-(limit * 8) :] if len(events) > limit * 8 else events
        else:
            recent_frames = session.agent_stack
            recent_events = events
        blocks = _structured_session_history(recent_frames, recent_events)
        items: list[SessionHistoryItemDTO] = []
        for frame in recent_frames:
            items.extend(_history_items_for_frame(frame))
        seen = {_history_text_fingerprint(item.text) for item in items}
        if recent_events:
            items.extend(_history_items_for_events(recent_events, seen))
        if limit > 0 and len(items) > limit:
            items = items[-limit:]
        if limit > 0 and len(blocks) > limit:
            blocks = blocks[-limit:]
        logger.info(
            "Session history requested session=%s frames=%d/%d items=%d blocks=%d pending=%s",
            session_id,
            len(recent_frames),
            len(session.agent_stack),
            len(items),
            len(blocks),
            session.pending_turn_id is not None,
        )
        return SessionHistoryResponse(
            session_id=session.session_id,
            last_event_seq=self._events.last_seq(session_id) if self._events is not None else 0,
            pending_turn_id=session.pending_turn_id,
            context_used_tokens=_history_context_used_tokens(session, events),
            context_token_limit=self._settings.auto_compact_token_threshold,
            items=items,
            blocks=blocks,
        )

    async def submit_user_turn(self, request: ChatRequest) -> ChatResponse:
        """处理一次 `/chat` 请求。

        `user_message` 发起新用户轮次；`tool_results` 回填上一轮 front 工具结果。
        两者不可同时出现，且会话有 pending 工具结果时拒绝新用户消息。

        本方法把当前 `asyncio.Task` 登记到 `_active_tasks`，使
        `interrupt()` 能在用户点击"停止"时真正取消仍在运行的 agent 循环
        （而不是仅让前端断开 HTTP 连接、后端继续跑完整个 turn）。
        """
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.setdefault(request.session_id, set()).add(task)
        try:
            async with self._store.lock_for(request.session_id):
                session = self._store.get_or_create(request.session_id, self.available_tools)
                logger.info(
                    "Chat request accepted session=%s request_id=%s has_user=%s tool_results=%d",
                    request.session_id,
                    request.request_id,
                    request.user_message is not None,
                    len(request.tool_results or []),
                )

                if (
                    request.request_id is not None
                    and request.request_id in session.request_id_cache
                ):
                    logger.info(
                        "Chat idempotency hit session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return _response_from_dict(session.request_id_cache[request.request_id])

                # 取消保护快照：本轮可能在追加 assistant 的 tool_calls 后、写入对应
                # tool result 之前被 interrupt 取消。若让这半截历史留在内存里，下一次
                # 请求发给 OpenAI 兼容端点会因 tool_call 缺少 tool result 而 400。取消
                # 时回滚到本轮开始前的内存快照（本轮尚未 save()，磁盘仍是旧版本）。
                snapshot = copy.deepcopy(session)
                try:
                    response = await self._submit_locked(session, request)
                except asyncio.CancelledError:
                    self._store.replace_in_memory(request.session_id, snapshot)
                    raise

                if request.request_id is not None:
                    session.request_id_cache[request.request_id] = _response_to_dict(response)
                self._store.save(session)
                self._record_recovery(session, response)
                logger.info(
                    "Chat request completed session=%s response_type=%s pending=%s",
                    request.session_id,
                    response.type,
                    session.pending_turn_id is not None,
                )
                logger.debug(
                    "Chat response details session=%s type=%s response=%s",
                    request.session_id,
                    response.type,
                    json.dumps(_response_to_dict(response), ensure_ascii=False, default=str),
                )
                return response
        finally:
            if task is not None:
                tasks = self._active_tasks.get(request.session_id)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        del self._active_tasks[request.session_id]

    async def _submit_locked(self, session: Session, request: ChatRequest) -> ChatResponse:
        """在持有会话锁时执行一次请求。"""
        has_user = request.user_message is not None
        has_results = request.tool_results is not None
        if has_user == has_results:
            logger.warning(
                "Invalid chat request shape session=%s has_user=%s has_results=%s",
                session.session_id,
                has_user,
                has_results,
            )
            return ChatErrorResponse(text="user_message 与 tool_results 必须二选一")

        security = self._security_for_request(request)
        model_override = _normalize_model_override(request.model)

        if request.effort is not None:
            session.effort = request.effort
            logger.info(
                "Session effort overridden session=%s effort=%s", session.session_id, request.effort
            )
        if request.output_style is not None:
            session.output_style = request.output_style
            logger.info(
                "Session output style overridden session=%s output_style=%s",
                session.session_id,
                request.output_style,
            )

        # RAG 段（L3）：用户新提问时刷新检索结果，工具结果回填等同一轮的后续
        # 请求里复用 `session.rag_context`，使该段在整轮 agent 循环内保持稳定、
        # 可被缓存（§16.1 RAG 段缓存）。
        if request.user_message is not None:
            session.rag_context = await self._retrieve_rag_context(security, request.user_message)

        project_context = build_project_context(security.project_root)
        coordinator = get_agent("coordinator", self.available_tools)
        cache_context = ContextBuilder().build(
            stable_prefix=build_system_prompt(
                coordinator,
                self._skill_catalog,
                self._output_styles,
                session.output_style,
            ),
            structure_context=project_context,
            dynamic_context=session.rag_context,
            query=request.user_message or "",
        )
        root_snapshot = session.agent_stack[0].compact_snapshot if session.agent_stack else None
        layered_prompt = LayeredPrompt(
            core=cache_context.stable_prefix,
            structure_context=cache_context.structure_context,
            compact_context=root_snapshot.summary if root_snapshot is not None else "",
            rag_context=cache_context.dynamic_context,
        )
        # `agent.prompt` 保留拼平后的纯文本（供委派子帧继承等需要字符串的场景）；
        # 根帧的 system 消息则写成分层 content-block 数组，使缓存层可为每层（L0
        # 核心 / L2 项目上下文 / L3 RAG）独立标记 `cache_control`，实现多断点缓存
        # （§16.1 / 文档 3.1）。content_blocks 不带 `cache_control`，标记在请求时
        # 由 provider 按 CacheDecisionEngine 的断点注入，不写入会话历史。
        coordinator = replace(coordinator, prompt=layered_prompt.to_text())
        session.ensure_root_frame(coordinator)
        root = session.agent_stack[0]
        root.agent = coordinator
        if root.messages and root.messages[0].get("role") == "system":
            # 只有真正分层（≥2 层）时才写成 content-block 数组以启用多断点；单层
            # （无项目文档/RAG，最常见）保持纯字符串，与改造前完全一致、零行为变化。
            layers = layered_prompt.layers()
            root.messages[0]["content"] = (
                layered_prompt.to_content_blocks() if len(layers) >= 2 else layered_prompt.to_text()
            )

        if has_results:
            self._emit(
                session.session_id,
                "tool_results_received",
                {"count": len(request.tool_results or [])},
            )
            logger.info(
                "Appending front tool results session=%s count=%d pending_turn=%s",
                session.session_id,
                len(request.tool_results or []),
                session.pending_turn_id,
            )
            result_error, verify_candidates = await self._append_tool_results(
                session, request.tool_results or [], security
            )
            if result_error is not None:
                logger.warning(
                    "Front tool result rejected session=%s reason=%s",
                    session.session_id,
                    result_error.text,
                )
                return result_error
            if verify_candidates:
                session.pending_verify_candidates.extend(verify_candidates)
            resumed_map_tool = _resume_pending_map_tool_after_read(session)
            if resumed_map_tool is not None:
                self._emit(
                    session.session_id,
                    "tool_calls",
                    {"turn_id": resumed_map_tool.turn_id, "count": len(resumed_map_tool.calls)},
                )
                logger.info(
                    "Resumed pending map tool after region read session=%s turn_id=%s count=%d",
                    session.session_id,
                    resumed_map_tool.turn_id,
                    len(resumed_map_tool.calls),
                )
                return resumed_map_tool
            resumed_write = _resume_pending_map_write_after_read(session)
            if resumed_write is not None:
                self._emit(
                    session.session_id,
                    "tool_calls",
                    {"turn_id": resumed_write.turn_id, "count": len(resumed_write.calls)},
                )
                logger.info(
                    "Resumed pending map write after state read session=%s turn_id=%s count=%d",
                    session.session_id,
                    resumed_write.turn_id,
                    len(resumed_write.calls),
                )
                return resumed_write
            resumed_validation = _resume_pending_map_validation_after_read(session)
            if resumed_validation is not None:
                self._emit(
                    session.session_id,
                    "tool_calls",
                    {
                        "turn_id": resumed_validation.turn_id,
                        "count": len(resumed_validation.calls),
                    },
                )
                logger.info(
                    "Resumed pending map validation after state read session=%s turn_id=%s count=%d",
                    session.session_id,
                    resumed_validation.turn_id,
                    len(resumed_validation.calls),
                )
                return resumed_validation
        else:
            if session.pending_turn_id is not None:
                logger.warning(
                    "User message rejected because tools are pending session=%s pending_turn=%s",
                    session.session_id,
                    session.pending_turn_id,
                )
                return ChatErrorResponse(text="当前会话仍有待回传的工具结果，不能开始新的用户消息")
            frame = session.top_frame()
            if frame is None:
                logger.error(
                    "User message rejected because session has no active frame session=%s",
                    session.session_id,
                )
                return ChatErrorResponse(text="会话没有活跃的 agent 帧")
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            session.pending_verify_candidates.clear()
            session.map_completion_blockers.clear()
            self._emit(
                session.session_id, "user_submitted", {"has_context": request.context is not None}
            )
            logger.info(
                "User turn appended session=%s has_context=%s language_hint=%s",
                session.session_id,
                request.context is not None,
                request.language_hint,
            )

        # 自动压缩（§16.1 策略 A）：新消息/工具结果已追加完毕、即将驱动 LLM 之前
        # 检查体积——这样下面 run_turn 实际发出的请求已经是压缩后的大小，而不是
        # "先发一次超大请求，下次才生效"。只在体积越界时才触发，不影响正常大小
        # 会话的行为；阈值用粗估 token 数而非精确计费值，足够判断"是否该收紧"。
        if self._settings.auto_compact_enabled and self._needs_auto_compact(session):
            logger.info(
                "Auto-compact triggered session=%s threshold=%d keep_recent=%d",
                session.session_id,
                self._settings.auto_compact_token_threshold,
                self._settings.auto_compact_keep_recent,
            )
            await self._compact_locked(
                session.session_id,
                keep_recent=self._settings.auto_compact_keep_recent,
                triggered_by="auto",
                use_llm=request.compact_summary_use_llm,
            )

        defer_verification_until_final = bool(session.pending_verify_candidates)

        def emit_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            if defer_verification_until_final and event_type in {
                "agent_text_delta",
                "agent_reasoning_delta",
            }:
                return
            self._emit(session.session_id, event_type, payload)

        async def build_child_agent_prompt(agent: AgentDefinition, task: str) -> str:
            """为委派子 agent 构造按任务检索的分层 system prompt。"""
            task_rag_context = await self._retrieve_rag_context(security, task)
            child_context = ContextBuilder().build(
                stable_prefix=build_system_prompt(
                    agent,
                    self._skill_catalog,
                    self._output_styles,
                    session.output_style,
                ),
                structure_context=project_context,
                dynamic_context=task_rag_context,
                query=task,
            )
            return cast(
                str,
                LayeredPrompt(
                    core=child_context.stable_prefix,
                    structure_context=child_context.structure_context,
                    rag_context=child_context.dynamic_context,
                ).to_text(),
            )

        def emit_verify_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            self._emit(session.session_id, event_type, payload)

        step = await run_turn(
            session=session,
            llm=self._llm,
            security=security,
            tool_ctx=ToolContext(
                security=security,
                session_id=session.session_id,
                skill_catalog=self._skill_catalog,
                rag_index_path=self._settings.resolved_rag_index_path(),
            ),
            max_turns=self._settings.max_turns,
            session_allow=session.session_allow,
            agent_prompt_factory=build_child_agent_prompt,
            model_selector=self._model_for_effort,
            model_override=model_override,
            thinking_budget_selector=self._thinking_budget_for_effort,
            event_callback=emit_turn_event,
            cache_engine=self._cache_engine,
            cache_metrics=self._cache_metrics,
            context_token_limit=self._settings.auto_compact_token_threshold,
        )
        response = _step_to_response(step)
        if isinstance(response, ChatToolCallsResponse):
            response = _defer_map_tool_for_region_read(session, response)
            response = _defer_map_write_for_state_read(session, response)
            response = _defer_map_validation_for_state_read(session, response)
        if isinstance(response, ChatFinalResponse) and session.pending_verify_candidates:
            final_frame = session.top_frame()
            if final_frame is not None and final_frame.messages:
                last_message = final_frame.messages[-1]
                if last_message.get("role") == "assistant" and not last_message.get("tool_calls"):
                    final_frame.messages.pop()
            latest_by_path: dict[str, dict[str, Any]] = {}
            for candidate in session.pending_verify_candidates:
                path = str(candidate.get("path", ""))
                if path:
                    latest_by_path[path] = candidate
            session.pending_verify_candidates.clear()
            if latest_by_path:
                await self._run_verify(
                    session, security, list(latest_by_path.values()), model_override
                )
                step = await run_turn(
                    session=session,
                    llm=self._llm,
                    security=security,
                    tool_ctx=ToolContext(
                        security=security,
                        session_id=session.session_id,
                        skill_catalog=self._skill_catalog,
                        rag_index_path=self._settings.resolved_rag_index_path(),
                    ),
                    max_turns=self._settings.max_turns,
                    session_allow=session.session_allow,
                    agent_prompt_factory=build_child_agent_prompt,
                    model_selector=self._model_for_effort,
                    model_override=model_override,
                    thinking_budget_selector=self._thinking_budget_for_effort,
                    event_callback=emit_verify_turn_event,
                    cache_engine=self._cache_engine,
                    cache_metrics=self._cache_metrics,
                    context_token_limit=self._settings.auto_compact_token_threshold,
                )
                response = _step_to_response(step)
                if isinstance(response, ChatToolCallsResponse):
                    response = _defer_map_tool_for_region_read(session, response)
                    response = _defer_map_write_for_state_read(session, response)
                    response = _defer_map_validation_for_state_read(session, response)
        map_gate_continuations = 0
        while (
            isinstance(response, ChatFinalResponse)
            and session.map_completion_blockers
            and map_gate_continuations < 3
        ):
            scheduled = False
            if _has_only_map_review_required(session.map_completion_blockers):
                scheduled = _schedule_map_reviewer_if_required(session)
                if scheduled:
                    logger.info(
                        "Map completion gate scheduled reviewer continuation session=%s",
                        session.session_id,
                    )
            if not scheduled:
                scheduled = _schedule_map_completion_continuation(session)
                if scheduled:
                    logger.info(
                        "Map completion gate scheduled repair continuation session=%s blockers=%d",
                        session.session_id,
                        len(session.map_completion_blockers),
                    )
            if not scheduled:
                break
            map_gate_continuations += 1
            step = await run_turn(
                session=session,
                llm=self._llm,
                security=security,
                tool_ctx=ToolContext(
                    security=security,
                    session_id=session.session_id,
                    skill_catalog=self._skill_catalog,
                    rag_index_path=self._settings.resolved_rag_index_path(),
                ),
                max_turns=self._settings.max_turns,
                session_allow=session.session_allow,
                agent_prompt_factory=build_child_agent_prompt,
                model_selector=self._model_for_effort,
                model_override=model_override,
                thinking_budget_selector=self._thinking_budget_for_effort,
                event_callback=emit_verify_turn_event,
                cache_engine=self._cache_engine,
                cache_metrics=self._cache_metrics,
                context_token_limit=self._settings.auto_compact_token_threshold,
            )
            response = _step_to_response(step)
            if isinstance(response, ChatToolCallsResponse):
                response = _defer_map_tool_for_region_read(session, response)
                response = _defer_map_write_for_state_read(session, response)
                response = _defer_map_validation_for_state_read(session, response)
        if isinstance(response, ChatToolCallsResponse):
            self._emit(
                session.session_id,
                "tool_calls",
                {"turn_id": response.turn_id, "count": len(response.calls)},
            )
            logger.info(
                "Chat produced front tool calls session=%s turn_id=%s count=%d",
                session.session_id,
                response.turn_id,
                len(response.calls),
            )
        elif isinstance(response, ChatFinalResponse):
            if session.map_completion_blockers:
                gated_text = _map_completion_gate_text(session.map_completion_blockers)
                _replace_last_assistant_final(session, gated_text)
                response = ChatFinalResponse(text=gated_text)
            self._emit(session.session_id, "final", {"text_length": len(response.text)})
            logger.info(
                "Chat produced final response session=%s text_length=%d",
                session.session_id,
                len(response.text),
            )
        else:
            self._emit(session.session_id, "error", {"text": response.text})
            logger.warning(
                "Chat produced error response session=%s text=%s", session.session_id, response.text
            )
        return response

    def _security_for_request(self, request: ChatRequest) -> SecuritySettings:
        """基于启动安全边界叠加单次请求的权限模式覆盖。"""
        if request.permission_mode is None:
            return self._base_security
        logger.info(
            "Permission mode overridden session=%s mode=%s",
            request.session_id,
            request.permission_mode,
        )
        return self._base_security.model_copy(update={"permission_mode": request.permission_mode})

    def _model_for_effort(self, effort: str) -> str | None:
        """Return an optional model override for the current effort."""
        value = {
            "quick": self._settings.llm_quick_model,
            "standard": self._settings.llm_standard_model,
            "deep": self._settings.llm_deep_model,
            "verify": self._settings.llm_verify_model,
            "advisor": self._settings.llm_advisor_model,
        }.get(effort)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
        return self._settings.llm_model.strip() or None

    def _thinking_budget_for_effort(self, effort: str) -> int | None:
        """Return an optional thinking budget override for the current effort."""
        return {
            "quick": self._settings.llm_thinking_budget_quick,
            "standard": self._settings.llm_thinking_budget_standard,
            "deep": self._settings.llm_thinking_budget_deep,
            "verify": self._settings.llm_thinking_budget_verify,
            "advisor": self._settings.llm_thinking_budget_advisor,
        }.get(effort)

    async def _enrich_front_image_result(
        self, tool_name: str, result: dict[str, Any], security: SecuritySettings
    ) -> dict[str, Any]:
        """为前端读图类工具结果补充多模态语义描述。"""
        if tool_name not in {"read_image_metadata", "capture_viewport_screenshot"}:
            return result
        enriched = dict(result)
        client = AssetLLMClient(
            AssetLLMConfig(
                enabled=self._settings.asset_understanding_enabled,
                model=self._settings.asset_understanding_model,
                endpoint=self._settings.asset_understanding_endpoint,
                api_key=self._settings.asset_understanding_api_key.get_secret_value(),
                timeout_s=self._settings.asset_understanding_timeout_s,
                max_tokens=self._settings.asset_understanding_max_tokens,
                concurrency=1,
            )
        )
        semantic: dict[str, Any] = {
            "enabled": client.available,
            "model": self._settings.asset_understanding_model,
        }
        if not client.available:
            semantic["skipped"] = "asset_understanding_not_configured"
            enriched["semantic"] = semantic
            return enriched
        image_path = self._resolve_front_image_path(enriched, security)
        if image_path is None:
            semantic["skipped"] = "image_path_not_readable_by_service"
            enriched["semantic"] = semantic
            return enriched
        description = await asyncio.to_thread(client.describe, image_path, "image")
        semantic["source_path"] = str(image_path)
        semantic["description"] = description
        enriched["semantic"] = semantic
        if description:
            enriched["semantic_description"] = description
        return enriched

    def _resolve_front_image_path(
        self, result: dict[str, Any], security: SecuritySettings
    ) -> Path | None:
        """把前端返回的 res/user 路径解析为服务端可读的本地图片路径。"""
        raw_path = str(result.get("path", "")).strip()
        if raw_path.startswith("res://"):
            rel = raw_path.removeprefix("res://").lstrip("/\\")
            return self._resolve_project_image_path(security.project_root / rel, security)
        if raw_path and not raw_path.startswith("user://") and not Path(raw_path).is_absolute():
            return self._resolve_project_image_path(security.project_root / raw_path, security)
        absolute = str(result.get("absolute_path", "")).strip()
        if raw_path.startswith("user://") and absolute:
            return self._resolve_existing_image_path(Path(absolute))
        return None

    def _resolve_project_image_path(
        self, candidate: Path, security: SecuritySettings
    ) -> Path | None:
        """确认项目内图片路径没有越过安全根目录且真实存在。"""
        try:
            resolved_root = security.project_root.resolve()
            resolved_candidate = candidate.resolve()
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        return self._resolve_existing_image_path(resolved_candidate)

    def _resolve_existing_image_path(self, candidate: Path) -> Path | None:
        """确认图片候选路径存在且是普通文件。"""
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            return None
        return None

    async def _append_tool_results(
        self, session: Session, results: list[ToolResult], security: SecuritySettings
    ) -> tuple[ChatErrorResponse | None, list[dict[str, Any]]]:
        """校验并把前端工具结果追加到对应 agent 帧。

        Returns:
            `(error, verify_candidates)`：`error` 非 None 时本次回传被拒绝，
            `verify_candidates` 此时必为空列表；否则 `verify_candidates` 收集
            本次落地、且命中 `verify_trigger_tools` 的编辑类工具调用，供调用方
            驱动 Verify 两阶段校验（§3.3）。
        """
        if session.pending_turn_id is None:
            logger.warning("Tool results rejected: no pending turn session=%s", session.session_id)
            return ChatErrorResponse(text="当前会话没有等待回传的工具调用"), []
        if not results:
            logger.warning("Tool results rejected: empty results session=%s", session.session_id)
            return ChatErrorResponse(text="tool_results 不能为空"), []

        ids = {result.tool_use_id for result in results}
        if ids != session.pending_tool_call_ids:
            expected = ", ".join(sorted(session.pending_tool_call_ids))
            actual = ", ".join(sorted(ids))
            logger.warning(
                "Tool results rejected: id mismatch session=%s expected=%s actual=%s",
                session.session_id,
                expected,
                actual,
            )
            return (
                ChatErrorResponse(
                    text=f"tool_results 与 pending 工具调用不匹配：expected={expected}; actual={actual}"
                ),
                [],
            )
        if any(result.turn_id != session.pending_turn_id for result in results):
            logger.warning(
                "Tool results rejected: turn mismatch session=%s pending_turn=%s",
                session.session_id,
                session.pending_turn_id,
            )
            return ChatErrorResponse(text="tool_results.turn_id 与当前 pending_turn_id 不匹配"), []

        frames = {frame.id: frame for frame in session.agent_stack}
        verify_candidates: list[dict[str, Any]] = []
        for result in results:
            frame = frames.get(result.frame_id)
            if frame is None:
                logger.warning(
                    "Tool results rejected: unknown frame session=%s frame=%s",
                    session.session_id,
                    result.frame_id,
                )
                return ChatErrorResponse(text=f"未知 frame_id：{result.frame_id}"), []
            is_error = result.status in {"rejected", "error"}
            metadata = session.pending_tool_calls.get(result.tool_use_id, {})
            tool_name = str(metadata.get("name", ""))
            tool_args = metadata.get("input", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool = REGISTRY.get(tool_name)
            payload: Any
            map_artifact_ref: str | None = None
            if result.status == "applied":
                applied_result = result.result
                if (
                    tool is not None
                    and tool.enrich is not None
                    and isinstance(applied_result, dict)
                ):
                    applied_result = tool.enrich(tool_args, applied_result)
                if isinstance(applied_result, dict):
                    applied_result = await self._enrich_front_image_result(
                        tool_name, applied_result, security
                    )
                    map_artifact_ref = self._store_map_artifact(
                        session.session_id,
                        tool_name,
                        tool_args,
                        applied_result,
                    )
                    _update_map_context_state(
                        session,
                        tool_name,
                        tool_args,
                        applied_result,
                        map_artifact_ref,
                    )
                if result.grant_session_allow and tool is not None:
                    session.session_allow.add(make_session_allow_grant(tool, tool_args))
                    logger.info(
                        "Session allow grant added session=%s tool=%s frame=%s",
                        session.session_id,
                        tool.name,
                        frame.id,
                    )
                artifact_refs = list(result.artifact_refs)
                if map_artifact_ref is not None:
                    artifact_refs.append(map_artifact_ref)
                payload = {
                    "status": result.status,
                    "result": applied_result,
                    "artifact_refs": artifact_refs,
                    "grant_session_allow": result.grant_session_allow,
                }
                if (
                    self._settings.verify_after_edit
                    and tool_name in self._settings.verify_trigger_tools
                ):
                    path = tool_args.get("path") or tool_args.get("target_path")
                    if isinstance(path, str) and path:
                        verify_candidates.append(
                            {
                                "tool_use_id": result.tool_use_id,
                                "frame_id": frame.id,
                                "tool_name": tool_name,
                                "path": path,
                                "input": tool_args,
                            }
                        )
            else:
                payload = {
                    "status": result.status,
                    "error_code": result.error_code,
                    "result": result.result,
                }
            result_for_gate = payload.get("result") if isinstance(payload, dict) else None
            _remember_latest_map_revision(session, tool_args, result_for_gate)
            if tool_name == "describe_map_region":
                _remember_latest_map_region_read(session, tool_args, result_for_gate)
            blocker = _map_completion_blocker(
                tool_name, result.status, result_for_gate, result.error_code
            )
            if tool_name in _MAP_VALIDATION_TOOL_NAMES and isinstance(result_for_gate, dict):
                if result_for_gate.get("completion_allowed") is True:
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    revision = result_for_gate.get("map_revision")
                    revision_value = (
                        revision
                        if isinstance(revision, int) and not isinstance(revision, bool)
                        else None
                    )
                    session.map_completion_blockers = _clear_validation_blockers(
                        session.map_completion_blockers,
                        target,
                        revision_value,
                    )
                    if not _has_review_blocker(
                        session.map_completion_blockers,
                        target,
                        revision_value,
                    ):
                        session.map_completion_blockers.append(
                            _review_required_blocker(tool_name, target, revision_value)
                        )
                elif blocker is not None:
                    session.map_completion_blockers = [blocker]
            elif blocker is not None:
                session.map_completion_blockers = [blocker]
            history_payload = (
                _history_payload_for_front_tool(tool_name, payload, map_artifact_ref)
                if isinstance(payload, dict)
                else payload
            )
            frame.messages.append(
                _tool_message(result.tool_use_id, history_payload, is_error=is_error)
            )
            if (
                tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                and str(result.error_code) == "map_revision_conflict"
            ):
                _schedule_revision_conflict_reader(
                    session,
                    frame,
                    tool_name,
                    tool_args,
                    result_for_gate,
                )
            # cell_count_mismatch 时自动注入恢复指引，避免 LLM 盲目重试
            if str(result.error_code) == "cell_count_mismatch":
                actual_cells = None
                if isinstance(result_for_gate, dict):
                    actual_cells = result_for_gate.get("actual_cells")
                hint = (
                    "【cell_count_mismatch 恢复指引】\n"
                    "- 计算公式：x=A..B 的列数 = (B - A + 1)，不是 (B - A)\n"
                    "- 示例：x=64..86 是 23 列，y=21..23 是 3 行，总计 23×3=69 格\n"
                )
                if actual_cells is not None:
                    hint += f"- 重试时必须把 expected_cells 设为 {actual_cells}\n"
                hint += "- 禁止用相同参数重试第 3 次，必须切换策略或提前终止\n"
                frame.messages.append(
                    {"role": "user", "content": hint}
                )
            logger.info(
                "Tool result appended session=%s turn_id=%s tool=%s status=%s frame=%s",
                session.session_id,
                result.turn_id,
                tool_name,
                result.status,
                frame.id,
            )

        session.clear_pending()
        logger.info("Tool results completed session=%s count=%d", session.session_id, len(results))
        return None, verify_candidates

    async def _run_verify(
        self,
        session: Session,
        security: SecuritySettings,
        candidates: list[dict[str, Any]],
        model_override: str | None = None,
    ) -> None:
        """对本轮所有命中校验条件的编辑结果依次跑 Verify 两阶段校验（§3.4）。

        Args:
            session: 当前会话。
            security: 当前请求的安全边界配置，决定文件读取/语法检查的工程根目录。
            candidates: `_append_tool_results()` 收集的待校验候选列表。
        """
        for candidate in candidates:
            await self._verify_one(session, security, candidate, model_override)

    async def _verify_one(
        self,
        session: Session,
        security: SecuritySettings,
        candidate: dict[str, Any],
        model_override: str | None = None,
    ) -> None:
        """对单个编辑结果跑 Phase 1 语法快检 + Phase 2 语义校验，并把结论写回对应帧。"""
        settings = self._settings
        tool_use_id = str(candidate["tool_use_id"])
        frame_id = str(candidate["frame_id"])
        tool_name = str(candidate["tool_name"])
        path = str(candidate["path"])
        frame = next((f for f in session.agent_stack if f.id == frame_id), None)
        if frame is None:
            frame = session.top_frame()
            if frame is None:
                logger.warning(
                    "Verify skipped: frame missing session=%s frame=%s",
                    session.session_id,
                    frame_id,
                )
                return

        retries = session.verify_retry_count.get(path, 0)
        if retries >= settings.verify_max_retries:
            logger.info(
                "Verify skipped: max retries reached session=%s path=%s retries=%d",
                session.session_id,
                path,
                retries,
            )
            return

        if settings.verify_syntax_enabled:
            self._emit(
                session.session_id,
                "verify_started",
                {
                    "tool_use_id": tool_use_id,
                    "file_path": path,
                    "phase": "syntax",
                    "frame_id": frame.id,
                    "message_index": len(frame.messages),
                },
            )
            outcome = await run_syntax_check(
                path=path,
                project_root=security.project_root,
                godot_path=settings.verify_godot_path,
                timeout_s=settings.verify_syntax_timeout,
            )
            if outcome is not None:
                passed, issues = outcome
                if not passed:
                    summary = issues[0].message if issues else "语法检查失败"
                    self._emit(
                        session.session_id,
                        "verify_completed",
                        {
                            "tool_use_id": tool_use_id,
                            "file_path": path,
                            "passed": False,
                            "issues_count": len(issues),
                            "summary": summary,
                            "phase": "syntax",
                            "frame_id": frame.id,
                            "message_index": len(frame.messages),
                        },
                    )
                    frame.messages.append(
                        {
                            "role": "system",
                            "content": json.dumps(
                                {
                                    "verify": {
                                        "phase": "syntax",
                                        "passed": False,
                                        "issues": [issue.model_dump() for issue in issues],
                                        "summary": summary,
                                        "file_path": path,
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    session.verify_retry_count[path] = retries + 1
                    logger.info(
                        "Verify syntax failed session=%s path=%s issues=%d retries=%d",
                        session.session_id,
                        path,
                        len(issues),
                        session.verify_retry_count[path],
                    )
                    return

        self._emit(
            session.session_id,
            "verify_started",
            {
                "tool_use_id": tool_use_id,
                "file_path": path,
                "phase": "semantic",
                "frame_id": frame.id,
                "message_index": len(frame.messages),
            },
        )
        result = await self._run_semantic_verify(
            security,
            tool_name,
            candidate.get("input", {}),
            path,
            model_override,
        )
        self._emit(
            session.session_id,
            "verify_completed",
            {
                "tool_use_id": tool_use_id,
                "file_path": path,
                "passed": result.passed,
                "issues_count": len(result.issues),
                "summary": result.summary,
                "phase": "semantic",
                "frame_id": frame.id,
                "message_index": len(frame.messages),
            },
        )
        frame.messages.append(
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "verify": {
                            "phase": "semantic",
                            "passed": result.passed,
                            "issues": [issue.model_dump() for issue in result.issues],
                            "summary": result.summary,
                            "file_path": path,
                        }
                    },
                    ensure_ascii=False,
                ),
            }
        )
        session.verify_retry_count[path] = 0 if result.passed else retries + 1
        logger.info(
            "Verify semantic finished session=%s path=%s passed=%s issues=%d",
            session.session_id,
            path,
            result.passed,
            len(result.issues),
        )

    async def _run_semantic_verify(
        self,
        security: SecuritySettings,
        tool_name: str,
        tool_input: dict[str, Any],
        path: str,
        model_override: str | None = None,
    ) -> VerifyResultDTO:
        """调用 LLM 对改动后的文件内容做语义/逻辑层面的校验（Phase 2，§3.5）。

        语法正确性已由 Phase 1 保证，这里只关注：未定义引用、编辑意图是否
        完整实现、明显的逻辑错误、信号连接、import/preload 依赖关系。
        """
        try:
            file_payload = await read_file_handler(
                {"path": path, "limit": 20000},
                ToolContext(security=security, session_id="verify"),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Verify semantic skipped: cannot read file path=%s error=%s", path, exc)
            return VerifyResultDTO(passed=True, issues=[], summary=f"无法读取文件以校验：{exc}")

        file_content = str(file_payload.get("content", ""))
        user_payload = {
            "tool_name": tool_name,
            "tool_input_path": tool_input.get("path", path),
            "file_path": path,
            "file_content": file_content,
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        try:
            turn = await self._llm.chat(
                messages,
                [],
                model=model_override or self._model_for_effort(self._settings.verify_effort),
                temperature=EFFORT_TEMPERATURE.get(self._settings.verify_effort, 0.0),
                thinking_budget=resolve_thinking_budget(
                    self._settings.verify_effort, self._thinking_budget_for_effort
                ),
            )
        except LLMError as exc:
            logger.warning("Verify semantic LLM call failed path=%s error=%s", path, exc)
            return VerifyResultDTO(passed=True, issues=[], summary="校验调用失败，已跳过")

        return _parse_verify_response(turn.content or "")

    async def _cancel_active_tasks(self, session_id: str) -> bool:
        """取消并等待该会话仍在运行的 `/chat` 任务，返回是否取消了任何任务。

        会话生命周期操作（reset/interrupt）必须先把仍在 await LLM/工具的旧
        turn 真正取消并 await 到它退出，否则旧 turn 之后的 `save(session)` 会
        把已被重置/中断的会话重新写回，造成"会话复活"（§14.2）。排除当前
        协程自身，避免自取消。
        """
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._active_tasks.get(session_id, set())
            if not task.done() and task is not current
        }
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Cancelled task raised after cancel session=%s", session_id)
        return bool(tasks)

    async def reset(self, session_id: str) -> None:
        """清空指定会话。

        先取消该会话仍在运行的 `/chat` 任务并等待其退出，再在持锁状态下清空
        会话；否则旧 turn 返回后的 `save()` 会把已重置的会话重新写回磁盘。
        """
        await self._cancel_active_tasks(session_id)
        async with self._store.lock_for(session_id):
            self._store.reset(session_id)
            if self._recovery is not None:
                self._recovery.clear(session_id)
            self._emit(session_id, "reset", {})
        logger.info("Session reset through QueryEngine session=%s", session_id)

    async def interrupt(self, session_id: str) -> InterruptResponse:
        """真正中断该会话仍在运行的 `/chat` 请求，并丢弃其后续输出。

        前端"停止"按钮此前只是断开自己的 HTTP 连接：后端的 `run_turn`
        循环（自动执行的静默工具，如 grep/read）会继续跑完整轮，并持续把
        新事件写进 `EventStore`。等用户发出下一条消息时，这些属于已停止
        旧任务的事件会被一起拉取并误渲染成新对话的内容。这里改为取消
        该会话当前登记的 `asyncio.Task`，让 `CancelledError` 在下一个
        await 点（LLM 调用/工具执行）处中断循环，并清理任何尚未回传的
        pending 工具调用占位，使会话立刻能接受新消息。

        `_active_tasks[session_id]` 是一个集合而不是单个任务：如果用户在
        前一个请求仍卡在 per-session 锁等待时就又发了一条消息（或快速点了
        多次"停止"），会话上会短暂同时存在多个 `submit_user_turn` 任务。
        只取消其中一个（尤其是若取了最新、可能只是在排队等锁的那个）会让
        真正持锁运行的旧任务永远不会被取消，导致锁一直被占用，包括这次
        interrupt 自己后面要拿的锁也会卡死。所以这里要把所有未完成的都
        取消掉。
        """
        cancelled = await self._cancel_active_tasks(session_id)

        discarded = 0
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            had_pending_plan = session.pending_plan is not None
            session.pending_plan = None
            if session.pending_turn_id is not None:
                frames = {frame.id: frame for frame in session.agent_stack}
                for tool_use_id in sorted(session.pending_tool_call_ids):
                    metadata = session.pending_tool_calls.get(tool_use_id, {})
                    frame = frames.get(str(metadata.get("frame_id", "")))
                    if frame is None:
                        continue
                    frame.messages.append(
                        _tool_message(
                            tool_use_id, "用户中断了当前请求，该工具调用结果未回传。", is_error=True
                        )
                    )
                    discarded += 1
                session.clear_pending()
                self._store.save(session)
                if self._recovery is not None:
                    self._recovery.clear(session_id)
            elif had_pending_plan:
                self._store.save(session)

        self._emit(
            session_id, "turn_interrupted", {"cancelled": cancelled, "pending_discarded": discarded}
        )
        last_seq = self._events.last_seq(session_id) if self._events is not None else 0
        logger.info(
            "Turn interrupted session=%s cancelled=%s pending_discarded=%d last_seq=%d",
            session_id,
            cancelled,
            discarded,
            last_seq,
        )
        return InterruptResponse(ok=True, cancelled=cancelled, last_event_seq=last_seq)

    async def discard_pending(self, session_id: str) -> ChatResponse:
        """放弃当前会话待回传的前端工具调用，保留其余会话历史。

        为每个待回应的 `tool_use_id` 写入一条"用户放弃"的占位 `tool` 消息，
        然后清空 `pending_turn_id`，使会话恢复到可接受新用户消息的状态。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            if session.pending_turn_id is None:
                return ChatErrorResponse(text="当前会话没有等待回传的工具调用")

            frames = {frame.id: frame for frame in session.agent_stack}
            discarded = 0
            for tool_use_id in sorted(session.pending_tool_call_ids):
                metadata = session.pending_tool_calls.get(tool_use_id, {})
                frame = frames.get(str(metadata.get("frame_id", "")))
                if frame is None:
                    continue
                frame.messages.append(
                    _tool_message(tool_use_id, "用户放弃了该工具调用的结果回传。", is_error=True)
                )
                discarded += 1

            session.clear_pending()
            self._store.save(session)
            response = ChatFinalResponse(
                text=f"已放弃 {discarded} 个待回传的工具调用，可以继续发送新消息。"
            )
            self._record_recovery(session, response)
            self._emit(session_id, "pending_discarded", {"count": discarded})
            logger.info("Pending tool calls discarded session=%s count=%d", session_id, discarded)
            return response

    async def set_effort(self, session_id: str, effort: str) -> None:
        """Set session effort without starting a model turn.

        持锁修改：否则会与正在 await LLM 的活跃 turn 抢同一个 Session，导致
        配置在一轮中途被改、响应与上下文错配（§会话锁边界）。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.effort = effort
            self._store.save(session)
        self._emit(session_id, "config_changed", {"effort": effort})
        logger.info("Session effort changed session=%s effort=%s", session_id, effort)

    async def set_output_style(self, session_id: str, output_style: str) -> None:
        """Set session output style without starting a model turn."""
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.output_style = output_style
            self._store.save(session)
        self._emit(session_id, "config_changed", {"output_style": output_style})
        logger.info(
            "Session output style changed session=%s output_style=%s", session_id, output_style
        )

    def _needs_auto_compact(self, session: Session) -> bool:
        """判断当前会话是否有任意帧的预估 token 数超过自动压缩阈值。

        Args:
            session: 当前会话（已追加本轮新消息/工具结果）。

        Returns:
            只要 `agent_stack` 中任意一帧超过 `auto_compact_token_threshold`
            即返回 True；`compact()` 本身会对所有超过 `keep_recent` 消息数的
            帧分别处理，这里只需判断"值不值得调用一次"。
        """
        threshold = self._settings.auto_compact_token_threshold
        local_estimate = max(
            (estimate_message_tokens(frame.messages) for frame in session.agent_stack),
            default=0,
        )
        effective_tokens = max(local_estimate, session.latest_context_used_tokens)
        return session.force_compact_next_turn or effective_tokens > threshold

    async def compact(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """对指定 session 执行本地 micro/full compact，保留 pending 协议完整性。

        持锁入口：手动 `/compact` 命令经此处，先获取会话锁再压缩，避免与正在
        await LLM 的活跃 turn 同时修改 `frame.messages`（§会话锁边界）。自动
        压缩发生在已持锁的 `_submit_locked` 内，必须直接调用 `_compact_locked`，
        否则同一协程再次获取非重入的 `asyncio.Lock` 会死锁。

        Args:
            session_id: 待压缩的会话 id。
            keep_recent: 每帧保留的最近消息数（不含 system prompt）。
            triggered_by: `"manual"`（`/compact` 命令）或 `"auto"`（§16.1 策略 A
                的自动触发），写入 `compact_boundary` 事件 payload，仅用于
                日志/观测区分来源，不影响压缩逻辑本身。
            use_llm: 本次压缩是否用 LLM 语义压缩摘要的 per-request 覆盖；None 时
                沿用服务端 `compact_summary_use_llm` 配置。
        """
        async with self._store.lock_for(session_id):
            return await self._compact_locked(session_id, keep_recent, triggered_by, use_llm)

    async def _build_compact_summary(
        self,
        previous: CompactSnapshot | None,
        old_messages: list[dict[str, Any]],
        *,
        use_llm: bool,
    ) -> str:
        """生成最终压缩摘要：优先 LLM 语义压缩，未启用/失败/空时回退确定性机械拼接。"""
        if use_llm and old_messages:
            body = await self._summarize_via_llm(previous, old_messages)
            if body:
                return _wrap_compact_summary(body)
        return _compact_summary_text(previous, old_messages)

    async def _summarize_via_llm(
        self, previous: CompactSnapshot | None, old_messages: list[dict[str, Any]]
    ) -> str | None:
        """调用 LLM 把旧摘要与本次移除消息综合成单一连贯摘要正文；失败返回 None。

        采用 ``temperature=0`` 与 ``thinking_budget=0``，尽量让同一输入得到稳定输出，
        从而稳定 `compact_digest`、减少远端缓存版本抖动（§9/§10）。任何 `LLMError`
        或空响应都被吞掉并返回 None，由调用方回退到机械摘要——压缩绝不因摘要失败
        而中断本轮请求（§12：失败不得停留在半压缩状态）。
        """
        source = _mechanical_summary_body(previous, old_messages)
        if not source.strip():
            return None
        instructions = (
            "你是会话历史压缩器。请把下面这段较早的对话上下文压缩成简洁、忠实的中文摘要，"
            "保留关键决策、结论、涉及的文件路径与符号、以及尚未完成的事项；不要编造，不要补充原文没有的信息。"
            "若其中已包含『较早压缩快照』，请把它与新内容融合成单一连贯摘要，不要罗列多份摘要。"
            "只输出摘要正文，不要添加任何前后缀或标记。"
        )
        model = self._settings.compact_summary_model or self._model_for_effort("quick")
        try:
            turn = await self._llm.chat(
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": source},
                ],
                tools=[],
                model=model,
                temperature=0.0,
                thinking_budget=0,
            )
        except LLMError as exc:
            logger.warning("Compact LLM summarize failed, falling back to mechanical: %s", exc)
            return None
        text = (turn.content or "").strip()
        if not text:
            logger.warning(
                "Compact LLM summarize returned empty content, falling back to mechanical"
            )
            return None
        return text

    async def _compact_locked(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """在已持有会话锁时执行压缩；不要在未持锁路径直接调用。"""
        session = self._store.get_or_create(session_id, self.available_tools)
        trigger: Literal["manual", "auto"] = "auto" if triggered_by == "auto" else "manual"
        summary_use_llm = self._settings.compact_summary_use_llm if use_llm is None else use_llm
        logger.info(
            "Compacting session session=%s keep_recent=%d triggered_by=%s",
            session_id,
            keep_recent,
            trigger,
        )
        compacted_frames = 0
        removed_messages = 0
        truncated_messages = 0
        keep = max(6, keep_recent)
        modified_frame_ids: list[str] = []
        snapshot_payloads: list[dict[str, Any]] = []
        estimated_tokens_before = 0
        estimated_tokens_after = 0
        backups = [
            (frame, copy.deepcopy(frame.messages), copy.deepcopy(frame.compact_snapshot))
            for frame in session.agent_stack
        ]

        # 压缩本身是纯本地操作（无网络/子进程 I/O），耗时通常远低于一帧；这里仍
        # 单独发一个"开始"事件（而不是只在结束时发 compact_boundary），是为了让
        # 前端能渲染出一条独立的"正在压缩会话历史…"消息块——哪怕这条消息和随后
        # 的完成事件几乎同时到达，压缩历史里也会留下"这一轮发生过压缩"的痕迹，
        # 而不是只在日志里能看到。
        self._emit(
            session_id,
            "compact_started",
            {
                "keep_recent": keep,
                "triggered_by": trigger,
                "frame_ids": [frame.id for frame in session.agent_stack],
            },
        )

        for frame in session.agent_stack:
            frame_tokens_before = estimate_message_tokens(frame.messages)
            estimated_tokens_before += frame_tokens_before
            frame_changed = False
            anchor = _pending_anchor_index(frame, session.pending_tool_call_ids)
            # 超大单条消息的截断独立于"按消息数收拢"逐帧执行：不依赖
            # `len(frame.messages) <= keep + 2` 的早退判断，否则消息数很少但单条
            # 巨大的帧会被完全跳过（见 `_truncate_oversized_message` 文档）。排除
            # index 0（system prompt）与最后一条（当前活跃/刚提交的消息，可能是
            # 用户正在询问的内容，不应被静默改写）；待回传的 pending tool_call
            # 之后的消息同样不动，与下方摘要逻辑的 `anchor` 边界保持一致。
            scan_end = len(frame.messages) - 1
            if anchor is not None:
                scan_end = min(scan_end, anchor)
            for index in range(1, scan_end):
                replacement = _truncate_oversized_message(frame.messages[index])
                if replacement is not None:
                    frame.messages[index] = replacement
                    truncated_messages += 1
                    frame_changed = True

            if len(frame.messages) <= keep + 1:
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue
            default_start = max(1, len(frame.messages) - keep)
            keep_from = min(default_start, anchor) if anchor is not None else default_start
            if keep_from <= 1:
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue

            old_messages = frame.messages[1:keep_from]
            # 自动压缩防抖（§10）：本次可收拢的旧消息太少时（pending 锚点过早、或
            # 近期消息独占体积），生成新快照只会徒增一个 compact_digest 版本、让
            # 远端缓存反复失效，token 却几乎降不下来。auto 触发下跳过该帧、不翻新
            # 快照；手动 /compact 是用户显式意图，不受此门槛限制。
            if (
                trigger == "auto"
                and len(old_messages) < self._settings.auto_compact_min_new_messages
            ):
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue
            previous = frame.compact_snapshot
            summary = await self._build_compact_summary(
                previous, old_messages, use_llm=summary_use_llm
            )
            digest = _compact_digest(summary)
            revision = (
                previous.revision
                if previous is not None and previous.digest == digest
                else previous.revision + 1 if previous is not None else 1
            )
            frame.messages = [frame.messages[0], *frame.messages[keep_from:]]
            snapshot = CompactSnapshot(
                revision=revision,
                digest=digest,
                summary=summary,
                created_at=datetime.now(timezone.utc).isoformat(),
                source_message_count=(previous.source_message_count if previous is not None else 0)
                + len(old_messages),
                removed_message_count=len(old_messages),
                keep_recent=keep,
                estimated_tokens_before=frame_tokens_before,
                estimated_tokens_after=0,
                triggered_by=trigger,
            )
            frame.compact_snapshot = snapshot
            _inject_compact_snapshot(
                frame,
                has_rag_context=(
                    frame is session.agent_stack[0] and bool(session.rag_context.strip())
                ),
            )
            frame_tokens_after = estimate_message_tokens(frame.messages)
            snapshot = replace(
                snapshot,
                estimated_tokens_after=frame_tokens_after,
            )
            frame.compact_snapshot = snapshot
            estimated_tokens_after += frame_tokens_after
            frame_changed = True
            compacted_frames += 1
            removed_messages += len(old_messages)
            modified_frame_ids.append(frame.id)
            snapshot_payloads.append(
                {
                    "frame_id": frame.id,
                    "revision": revision,
                    "digest": digest,
                    "source_message_count": snapshot.source_message_count,
                    "removed_message_count": len(old_messages),
                    "estimated_tokens_before": frame_tokens_before,
                    "estimated_tokens_after": frame_tokens_after,
                }
            )

        session.latest_context_used_tokens = estimated_tokens_after
        session.force_compact_next_turn = False
        try:
            self._store.save(session)
        except Exception:
            for frame, messages, backup_snapshot in backups:
                frame.messages = messages
                frame.compact_snapshot = backup_snapshot
            raise
        if modified_frame_ids:
            self._cache_engine.invalidate(session_id, modified_frame_ids)
        seq = self._emit(
            session_id,
            "compact_boundary",
            {
                "compacted_frames": compacted_frames,
                "removed_messages": removed_messages,
                "truncated_messages": truncated_messages,
                "keep_recent": keep,
                "pending_preserved": session.pending_turn_id is not None,
                "triggered_by": trigger,
                "estimated_tokens_before": estimated_tokens_before,
                "estimated_tokens_after": estimated_tokens_after,
                "snapshots": snapshot_payloads,
            },
        )
        logger.info(
            "Compacted session session=%s frames=%d removed_messages=%d truncated_messages=%d "
            "pending_preserved=%s triggered_by=%s",
            session_id,
            compacted_frames,
            removed_messages,
            truncated_messages,
            session.pending_turn_id is not None,
            trigger,
        )
        return {
            "session_id": session_id,
            "compacted_frames": compacted_frames,
            "removed_messages": removed_messages,
            "truncated_messages": truncated_messages,
            "estimated_tokens_before": estimated_tokens_before,
            "estimated_tokens_after": estimated_tokens_after,
            "snapshots": snapshot_payloads,
            "last_event_seq": seq,
            "pending_turn_id": session.pending_turn_id,
        }

    async def _retrieve_rag_context(self, security: SecuritySettings, user_message: str) -> str:
        """为当前用户提问检索 RAG 上下文（L3 段），在线程池里执行避免阻塞事件循环。

        Args:
            security: 当前请求的安全边界（限定检索范围与索引路径）。
            user_message: 当前用户提问原文。

        Returns:
            组装好的 L3 RAG 上下文文本；无索引/无结果/出错时为空串。
        """
        index = create_codebase_index(self._settings, security)
        return await asyncio.to_thread(build_rag_context, index, user_message)

    def _emit(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """记录内部事件；未配置事件存储时返回 0。"""
        log_payload = _event_payload_for_log(payload)
        logger.debug(
            "Event emitted session=%s type=%s payload=%s",
            session_id,
            event_type,
            json.dumps(log_payload, ensure_ascii=False, default=str),
        )
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            session = self._store.get_or_create(session_id, self.available_tools)
            if event_type == "context_usage":
                try:
                    used_tokens = int(payload.get("used_tokens", 0))
                except (TypeError, ValueError):
                    used_tokens = 0
                if used_tokens > 0:
                    session.latest_context_used_tokens = used_tokens
                    if used_tokens >= self._settings.auto_compact_token_threshold:
                        session.force_compact_next_turn = True
            session.record_history_event(event_type, payload)
        if self._events is None:
            return 0
        event = self._events.append(session_id, event_type, payload)
        logger.debug("Event persisted session=%s seq=%d type=%s", session_id, event.seq, event_type)
        return event.seq

    def _record_recovery(self, session: Session, response: ChatResponse) -> None:
        """根据最新响应写入或清理最小恢复指针。"""
        if self._recovery is None:
            return
        if isinstance(response, ChatToolCallsResponse):
            last_seq = self._events.last_seq(session.session_id) if self._events is not None else 0
            self._recovery.write(
                session_id=session.session_id,
                pending_turn_id=response.turn_id,
                last_event_seq=last_seq,
            )
            logger.info(
                "Recovery pointer written session=%s turn_id=%s last_seq=%d",
                session.session_id,
                response.turn_id,
                last_seq,
            )
        elif isinstance(response, ChatFinalResponse):
            self._recovery.clear(session.session_id)
            logger.debug("Recovery pointer cleared after final session=%s", session.session_id)
