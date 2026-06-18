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
import json
import logging
from dataclasses import replace
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
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
from app.llm.provider import LLMError, LLMProvider
from app.orchestrator.agent import (
    EFFORT_TEMPERATURE,
    ErrorResult,
    FinalResult,
    StepResult,
    ToolCallsResult,
    run_turn,
)
from app.output_styles.catalog import OutputStyleCatalog
from app.permissions.engine import make_session_allow_grant
from app.prompt.builder import build_system_prompt
from app.recovery.pointer import RecoveryPointerStore
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.store import Session, SessionStore
from app.skills.catalog import SkillCatalog
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY
from app.tools.server_tools.read_file import read_file_handler
from app.verify.syntax_check import run_syntax_check

logger = logging.getLogger(__name__)


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


def _tool_message(tool_call_id: str, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    """构造 OpenAI `role=tool` 消息。"""
    body: Any = {"error": result} if is_error else result
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
    content = str(message.get("content", ""))
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
_PERSISTED_HISTORY_EVENT_TYPES = frozenset(
    {
        "agent_reasoning_delta",
        "agent_text_delta",
        "plan_created",
        "plan_step_started",
        "plan_step_completed",
        "verify_started",
        "verify_completed",
        "delegate_start",
        "server_tool_result",
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
    return "summary" in content and set(content.keys()).issubset({"summary", "agent", "frame_id", "error"})


def _format_delegate_history_summary(content: dict[str, Any]) -> str:
    """把单个 `delegate` 子任务结果转换成可渲染 Markdown 的历史块。"""
    agent = str(content.get("agent", "")).strip()
    summary = str(content.get("summary", "")).strip()
    title = f"Delegate result: {agent}" if agent else "Delegate result:"
    return f"{title}\n{_truncate_text(summary or 'No summary', 2000)}"


def _truncate_text(text: str, max_chars: int) -> str:
    # 按字符数截断超长文本，避免会话历史里堆入过长内容。
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


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
        line_count = _count_lines(str(inner.get("content", "")))
        return f"Read {path} (lines 1-{line_count})"

    if name in _HISTORY_EDIT_TOOLS:
        path = str(inner.get("path", input_args.get("path", input_args.get("target_path", "<unknown>"))))
        after_text = str(input_args.get("content", input_args.get("after_text", "")))
        before_text = str(input_args.get("before_text", input_args.get("before", "")))
        added = max(_count_lines(after_text) - _count_lines(before_text), 0)
        removed = max(_count_lines(before_text) - _count_lines(after_text), 0)
        return f"Edit {path}\n+{added} -{removed} lines"

    if name in _HISTORY_GREP_TOOLS:
        pattern = str(input_args.get("pattern", input_args.get("query", input_args.get("include", ""))))
        escaped_pattern = pattern.replace('"', '\\"')
        return f'Grep "{escaped_pattern}" (in project)'

    return _display_tool_content(content)


def _history_items_for_frame(frame: Frame, *, include_system_prompt: bool = False) -> list[SessionHistoryItemDTO]:
    """Convert stored LLM messages into chat-panel friendly history items."""
    items: list[SessionHistoryItemDTO] = []
    tool_calls_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, message in enumerate(frame.messages):
        role = str(message.get("role", "system"))
        content = message.get("content", "")
        text = "" if content is None else str(content)

        if role == "system":
            if index == 0 and not include_system_prompt:
                continue
            if not text.strip():
                continue
            items.append(
                SessionHistoryItemDTO(role="system", text=text, frame_id=frame.id, agent=frame.agent.name)
            )
            continue

        if role == "user":
            text = _display_user_content(text)
            if text.strip():
                items.append(
                    SessionHistoryItemDTO(role="user", text=text, frame_id=frame.id, agent=frame.agent.name)
                )
            continue

        if role == "assistant":
            if text.strip():
                items.append(
                    SessionHistoryItemDTO(role="assistant", text=text, frame_id=frame.id, agent=frame.agent.name)
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
            items.append(SessionHistoryItemDTO(role="system", text=text, frame_id=frame.id, agent=frame.agent.name))
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
            return SessionHistoryItemDTO(role="system", text=_format_plan_created_history_event(payload))
        case "plan_step_started":
            return SessionHistoryItemDTO(role="system", text=_format_plan_step_started_history_event(payload))
        case "plan_step_completed":
            return SessionHistoryItemDTO(role="system", text=_format_plan_step_completed_history_event(payload))
        case "verify_started":
            return SessionHistoryItemDTO(role="system", text=_format_verify_started_history_event(payload))
        case "verify_completed":
            return SessionHistoryItemDTO(role="system", text=_format_verify_completed_history_event(payload))
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


def _history_items_for_events(events: list[Event], seen: set[str]) -> list[SessionHistoryItemDTO]:
    """从事件日志中恢复不在 frame messages 里的 workflow 历史条目。"""
    items: list[SessionHistoryItemDTO] = []
    current_stream: Event | None = None
    current_stream_key: tuple[str, str, str] | None = None

    def flush_stream() -> None:
        nonlocal current_stream, current_stream_key
        stream_item = _history_item_for_stream_event(current_stream) if current_stream else None
        _append_history_item_if_new(items, seen, stream_item)
        current_stream = None
        current_stream_key = None

    for event in events:
        if event.type in {"agent_text_delta", "agent_reasoning_delta"}:
            payload = event.payload
            stream_key = (
                event.type,
                str(payload.get("frame_id", "")),
                str(payload.get("loop", "")),
            )
            if current_stream_key is not None and stream_key != current_stream_key:
                flush_stream()
            current_stream = event
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
    inner = _json_object(content)
    origin = _history_origin(frame)
    error_message = inner.get("error")
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

    if name in _HISTORY_READ_TOOLS:
        path = str(inner.get("path", input_args.get("path", "<unknown>")))
        line_count = max(_count_lines(str(inner.get("content", ""))), 1)
        return [LogReadHistoryBlock(path=path, line_end=line_count, **origin)]

    if name in _HISTORY_EDIT_TOOLS:
        path = str(inner.get("path", input_args.get("path", input_args.get("target_path", "<unknown>"))))
        after_text = str(input_args.get("content", input_args.get("after_text", "")))
        before_text = str(input_args.get("before_text", input_args.get("before", "")))
        return [
            LogEditHistoryBlock(
                path=path,
                added=max(_count_lines(after_text) - _count_lines(before_text), 0),
                removed=max(_count_lines(before_text) - _count_lines(after_text), 0),
                **origin,
            )
        ]

    if name in _HISTORY_GREP_TOOLS:
        matches = _grep_matches(inner)
        pattern = str(input_args.get("pattern", input_args.get("query", input_args.get("include", ""))))
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
    include_thought_summary: bool = True,
) -> list[SessionHistoryBlock]:
    role = str(message.get("role", "system"))
    raw_content = message.get("content", "")
    text = "" if raw_content is None else str(raw_content)
    origin = _history_origin(frame)
    if role == "user":
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
    if event.type == "agent_reasoning_delta":
        detail = str(payload.get("text", "")).strip()
        if not detail:
            return []
        elapsed_ms = payload.get("elapsed_ms")
        header = "Thought"
        if isinstance(elapsed_ms, int | float) and elapsed_ms > 0:
            header = f"Thought for {elapsed_ms / 1000:.2f}s"
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
            if bool(payload.get("is_error", False)):
                tool = str(payload.get("tool", "tool"))
                return [ErrorHistoryBlock(text=f"{tool} failed", **origin)]
            return []
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
                                int(match["line"])
                                if match.get("line") not in (None, "")
                                else None
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
    return []


def _block_fingerprint(block: SessionHistoryBlock) -> str:
    data = block.model_dump(exclude={"frame_id", "agent"})
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _structured_history_for_frame(frame: Frame, events: list[Event]) -> list[SessionHistoryBlock]:
    """Interleave frame messages with events anchored to their upcoming message index."""
    assistant_indexes = [
        index for index, message in enumerate(frame.messages) if str(message.get("role", "")) == "assistant"
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
        group_events = group[
            "reasoning" if event.type == "agent_reasoning_delta" else "text"
        ]
        assert isinstance(group_events, list)
        group_events.append(event)

    for group in stream_groups.values():
        reasoning_events = group["reasoning"]
        text_events = group["text"]
        assert isinstance(reasoning_events, list)
        assert isinstance(text_events, list)
        text = max(text_events, key=lambda event: event.seq) if text_events else None
        if text is not None:
            reasoning_before_text = [
                event for event in reasoning_events if event.seq < text.seq
            ]
            reasoning = (
                max(reasoning_before_text, key=lambda event: event.seq)
                if reasoning_before_text
                else None
            )
        else:
            reasoning = (
                max(reasoning_events, key=lambda event: event.seq)
                if reasoning_events
                else None
            )
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


def _structured_session_history(session_frames: list[Frame], events: list[Event]) -> list[SessionHistoryBlock]:
    blocks: list[SessionHistoryBlock] = []
    claimed_event_ids: set[int] = set()
    for frame in session_frames:
        frame_events = [
            event
            for event in events
            if str(
                event.payload.get(
                    "timeline_frame_id",
                    event.payload.get("frame_id", ""),
                )
            )
            == frame.id
        ]
        claimed_event_ids.update(id(event) for event in frame_events)
        blocks.extend(_structured_history_for_frame(frame, frame_events))
    for event in events:
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
    ) -> None:
        """构造 QueryEngine。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
        """
        self._settings = settings
        self._store = session_store
        self._llm = llm
        self._base_security = base_security or security_settings_from_app(settings)
        self._skill_catalog = skill_catalog
        self._output_styles = output_style_catalog
        self._events = event_store
        self._recovery = recovery_store
        # session_id -> 该会话当前所有"正在处理 /chat 请求"的任务集合（通常只有
        # 一个，但用户可能在前一个请求仍卡在 per-session 锁等待时就发出下一条
        # 消息/中断，short-lived 地出现多个；用 set 而不是单个槎位，避免新任务
        # 覆盖掉真正持有锁、仍在运行的旧任务引用，导致 interrupt() 取消错对象。
        self._active_tasks: dict[str, set[asyncio.Task]] = {}

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    def session_history(self, session_id: str, limit: int = 200) -> SessionHistoryResponse:
        """Return frontend-renderable history for a persisted session."""
        session = self._store.get_or_create(session_id, self.available_tools)
        events = _persisted_history_events(session)
        if not events and self._events is not None:
            events = self._events.list_after(session_id, 0)
        blocks = _structured_session_history(session.agent_stack, events)
        items: list[SessionHistoryItemDTO] = []
        for frame in session.agent_stack:
            items.extend(_history_items_for_frame(frame))
        seen = {_history_text_fingerprint(item.text) for item in items}
        if events:
            items.extend(_history_items_for_events(events, seen))
        if limit > 0 and len(items) > limit:
            items = items[-limit:]
        if limit > 0 and len(blocks) > limit:
            blocks = blocks[-limit:]
        logger.info(
            "Session history requested session=%s frames=%d items=%d blocks=%d pending=%s",
            session_id,
            len(session.agent_stack),
            len(items),
            len(blocks),
            session.pending_turn_id is not None,
        )
        return SessionHistoryResponse(
            session_id=session.session_id,
            pending_turn_id=session.pending_turn_id,
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

                if request.request_id is not None and request.request_id in session.request_id_cache:
                    logger.info(
                        "Chat idempotency hit session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return _response_from_dict(session.request_id_cache[request.request_id])

                response = await self._submit_locked(session, request)

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
        if request.effort is not None:
            session.effort = request.effort
            logger.info("Session effort overridden session=%s effort=%s", session.session_id, request.effort)
        if request.output_style is not None:
            session.output_style = request.output_style
            logger.info(
                "Session output style overridden session=%s output_style=%s",
                session.session_id,
                request.output_style,
            )

        coordinator = get_agent("coordinator", self.available_tools)
        prompt = build_system_prompt(
            coordinator,
            self._skill_catalog,
            self._output_styles,
            session.output_style,
        )
        coordinator = replace(coordinator, prompt=prompt)
        session.ensure_root_frame(coordinator)
        root = session.agent_stack[0]
        root.agent = coordinator
        if root.messages and root.messages[0].get("role") == "system":
            root.messages[0]["content"] = prompt

        if has_results:
            self._emit(session.session_id, "tool_results_received", {"count": len(request.tool_results or [])})
            logger.info(
                "Appending front tool results session=%s count=%d pending_turn=%s",
                session.session_id,
                len(request.tool_results or []),
                session.pending_turn_id,
            )
            result_error, verify_candidates = self._append_tool_results(session, request.tool_results or [])
            if result_error is not None:
                logger.warning("Front tool result rejected session=%s reason=%s", session.session_id, result_error.text)
                return result_error
            if verify_candidates:
                await self._run_verify(session, security, verify_candidates)
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
                logger.error("User message rejected because session has no active frame session=%s", session.session_id)
                return ChatErrorResponse(text="会话没有活跃的 agent 帧")
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            self._emit(session.session_id, "user_submitted", {"has_context": request.context is not None})
            logger.info(
                "User turn appended session=%s has_context=%s language_hint=%s",
                session.session_id,
                request.context is not None,
                request.language_hint,
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
            agent_prompt_factory=lambda agent: build_system_prompt(
                agent,
                self._skill_catalog,
                self._output_styles,
                session.output_style,
            ),
            model_selector=self._model_for_effort,
            event_callback=lambda event_type, payload: self._emit(session.session_id, event_type, payload),
        )
        response = _step_to_response(step)
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
            self._emit(session.session_id, "final", {"text_length": len(response.text)})
            logger.info(
                "Chat produced final response session=%s text_length=%d",
                session.session_id,
                len(response.text),
            )
        else:
            self._emit(session.session_id, "error", {"text": response.text})
            logger.warning("Chat produced error response session=%s text=%s", session.session_id, response.text)
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
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    def _append_tool_results(
        self, session: Session, results: list[ToolResult]
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
                ChatErrorResponse(text=f"tool_results 与 pending 工具调用不匹配：expected={expected}; actual={actual}"),
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
            if result.status == "applied":
                applied_result = result.result
                if tool is not None and tool.enrich is not None and isinstance(applied_result, dict):
                    applied_result = tool.enrich(tool_args, applied_result)
                if result.grant_session_allow and tool is not None and not tool.executes_process:
                    session.session_allow.add(make_session_allow_grant(tool))
                    logger.info(
                        "Session allow grant added session=%s tool=%s frame=%s",
                        session.session_id,
                        tool.name,
                        frame.id,
                    )
                payload = {
                    "status": result.status,
                    "result": applied_result,
                    "artifact_refs": result.artifact_refs,
                    "grant_session_allow": result.grant_session_allow,
                }
                if self._settings.verify_after_edit and tool_name in self._settings.verify_trigger_tools:
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
            frame.messages.append(_tool_message(result.tool_use_id, payload, is_error=is_error))
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
    ) -> None:
        """对本轮所有命中校验条件的编辑结果依次跑 Verify 两阶段校验（§3.4）。

        Args:
            session: 当前会话。
            security: 当前请求的安全边界配置，决定文件读取/语法检查的工程根目录。
            candidates: `_append_tool_results()` 收集的待校验候选列表。
        """
        for candidate in candidates:
            await self._verify_one(session, security, candidate)

    async def _verify_one(
        self,
        session: Session,
        security: SecuritySettings,
        candidate: dict[str, Any],
    ) -> None:
        """对单个编辑结果跑 Phase 1 语法快检 + Phase 2 语义校验，并把结论写回对应帧。"""
        settings = self._settings
        tool_use_id = str(candidate["tool_use_id"])
        frame_id = str(candidate["frame_id"])
        tool_name = str(candidate["tool_name"])
        path = str(candidate["path"])
        frame = next((f for f in session.agent_stack if f.id == frame_id), None)
        if frame is None:
            logger.warning("Verify skipped: frame missing session=%s frame=%s", session.session_id, frame_id)
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
        result = await self._run_semantic_verify(security, tool_name, candidate.get("input", {}), path)
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
    ) -> VerifyResultDTO:
        """调用 LLM 对改动后的文件内容做语义/逻辑层面的校验（Phase 2，§3.5）。

        语法正确性已由 Phase 1 保证，这里只关注：未定义引用、编辑意图是否
        完整实现、明显的逻辑错误、信号连接、import/preload 依赖关系。
        """
        try:
            file_payload = await read_file_handler(
                {"path": path},
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
                model=self._model_for_effort(self._settings.verify_effort),
                temperature=EFFORT_TEMPERATURE.get(self._settings.verify_effort, 0.0),
            )
        except LLMError as exc:
            logger.warning("Verify semantic LLM call failed path=%s error=%s", path, exc)
            return VerifyResultDTO(passed=True, issues=[], summary="校验调用失败，已跳过")

        return _parse_verify_response(turn.content or "")

    def reset(self, session_id: str) -> None:
        """清空指定会话。"""
        self._store.reset(session_id)
        if self._recovery is not None:
            self._recovery.clear()
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
        tasks = {task for task in self._active_tasks.get(session_id, set()) if not task.done()}
        cancelled = bool(tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Interrupted task raised after cancel session=%s", session_id)

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
                        _tool_message(tool_use_id, "用户中断了当前请求，该工具调用结果未回传。", is_error=True)
                    )
                    discarded += 1
                session.clear_pending()
                self._store.save(session)
                if self._recovery is not None:
                    self._recovery.clear()
            elif had_pending_plan:
                self._store.save(session)

        self._emit(session_id, "turn_interrupted", {"cancelled": cancelled, "pending_discarded": discarded})
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
            response = ChatFinalResponse(text=f"已放弃 {discarded} 个待回传的工具调用，可以继续发送新消息。")
            self._record_recovery(session, response)
            self._emit(session_id, "pending_discarded", {"count": discarded})
            logger.info("Pending tool calls discarded session=%s count=%d", session_id, discarded)
            return response

    def set_effort(self, session_id: str, effort: str) -> None:
        """Set session effort without starting a model turn."""
        session = self._store.get_or_create(session_id, self.available_tools)
        session.effort = effort
        self._store.save(session)
        self._emit(session_id, "config_changed", {"effort": effort})
        logger.info("Session effort changed session=%s effort=%s", session_id, effort)

    def set_output_style(self, session_id: str, output_style: str) -> None:
        """Set session output style without starting a model turn."""
        session = self._store.get_or_create(session_id, self.available_tools)
        session.output_style = output_style
        self._store.save(session)
        self._emit(session_id, "config_changed", {"output_style": output_style})
        logger.info("Session output style changed session=%s output_style=%s", session_id, output_style)

    def compact(self, session_id: str, keep_recent: int = 12) -> dict[str, Any]:
        """对指定 session 执行本地 micro/full compact，保留 pending 协议完整性。"""
        session = self._store.get_or_create(session_id, self.available_tools)
        logger.info("Compacting session session=%s keep_recent=%d", session_id, keep_recent)
        compacted_frames = 0
        removed_messages = 0
        keep = max(6, keep_recent)

        for frame in session.agent_stack:
            if len(frame.messages) <= keep + 2:
                continue
            anchor = _pending_anchor_index(frame, session.pending_tool_call_ids)
            default_start = max(1, len(frame.messages) - keep)
            keep_from = min(default_start, anchor) if anchor is not None else default_start
            if keep_from <= 1:
                continue

            old_messages = frame.messages[1:keep_from]
            summary_lines = [_brief_message(message) for message in old_messages]
            summary = (
                "[compact_summary]\n"
                "以下是较早上下文的本地摘要；写文件或执行高风险操作前仍需重新读取事实。\n"
                + "\n".join(f"- {line}" for line in summary_lines)
            )
            frame.messages = [
                frame.messages[0],
                {"role": "system", "content": summary},
                *frame.messages[keep_from:],
            ]
            compacted_frames += 1
            removed_messages += len(old_messages)

        self._store.save(session)
        seq = self._emit(
            session_id,
            "compact_boundary",
            {
                "compacted_frames": compacted_frames,
                "removed_messages": removed_messages,
                "keep_recent": keep,
                "pending_preserved": session.pending_turn_id is not None,
            },
        )
        logger.info(
            "Compacted session session=%s frames=%d removed_messages=%d pending_preserved=%s",
            session_id,
            compacted_frames,
            removed_messages,
            session.pending_turn_id is not None,
        )
        return {
            "session_id": session_id,
            "compacted_frames": compacted_frames,
            "removed_messages": removed_messages,
            "last_event_seq": seq,
            "pending_turn_id": session.pending_turn_id,
        }

    def _emit(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """记录内部事件；未配置事件存储时返回 0。"""
        logger.debug(
            "Event emitted session=%s type=%s payload=%s",
            session_id,
            event_type,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            session = self._store.get_or_create(session_id, self.available_tools)
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
            self._recovery.clear()
            logger.debug("Recovery pointer cleared after final session=%s", session.session_id)
