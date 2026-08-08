"""消息构造、verify 解析与提示词辅助。"""

from __future__ import annotations

import json
from ._text_utils import logger
from app.agents.types import AgentDefinition
from app.api.schemas import ChatRequest, FrontToolCallDTO
from app.history_bounds import bounded_tool_message_body as _bounded_tool_message_body
from app.llm.message_transformer import flatten_message_text
from app.orchestrator.map_platform_planning import parse_map_plan_outcome
from app.sessions.store import Session
from app.verify.contracts import UnsupportedVerifySchemaError, VerifyOutcome, VerifyRecoveryAction
from collections.abc import Awaitable, Callable
from typing import Any
# 用于向调度函数注入 prompt 生成回调：接收 AgentDefinition + task_text，返回最终 prompt。
# 调用方可以传入异步函数以支持动态 prompt 拼装（例如注入上下文快照）。
AgentPromptFactory = Callable[[AgentDefinition, str], Awaitable[str]]


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
                    "出错：自动 describe_map_region 请求超过单轴读取上限，已取消原请求，"
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
    """平台方案未通过校验时追加恢复指引，避免继续执行空批次。"""
    if tool_name not in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
        return
    outcome = parse_map_plan_outcome(tool_name, result)
    if outcome.executable:
        return
    frame = session.top_frame()
    if frame is None:
        return
    reason = outcome.error_code or outcome.blocked_reason or "unknown"
    profile_plan_value = result.get("profile_plan")
    profile_plan = profile_plan_value if isinstance(profile_plan_value, dict) else {}
    details_value = (
        result.get("repair_plan")
        or result.get("issues")
        or profile_plan.get("repair_plan")
        or profile_plan.get("issues")
    )
    if not isinstance(details_value, list) or not details_value:
        # 压缩后工具结果消息可能已丢失；从 failure_frontier(state) 回读持久化的 repair_plan。
        frontier = (
            session.map_task_state.failure_frontier
            if isinstance(session.map_task_state.failure_frontier, dict)
            else {}
        )
        frontier_repair = frontier.get("repair_plan")
        if isinstance(frontier_repair, list) and frontier_repair:
            details_value = frontier_repair
    details = details_value if isinstance(details_value, list) else []
    if reason == "start_not_standable" and outcome.suggested_foothold is not None:
        recovery = (
            f"先用 suggested_foothold={json.dumps(outcome.suggested_foothold, ensure_ascii=False)} "
            "小范围读取并核实下方支撑，再以该点作为 start 重新调用原校验工具。"
        )
    elif reason == "entry_anchor_not_found":
        recovery = (
            "先 describe_map_region 读取正确 target_path/map_layer 的连接边界并重新选择入口；"
            "若确实没有入口，仅可委派带有受限 repair_plan 的 repair_write_one_batch worker。"
        )
    elif reason in {
        "explicit_platform_plan_required",
        "explicit_route_segments_required",
        "invalid_explicit_plan",
        "platform_transition_unreachable",
        "finish_buffer_too_short",
        "route_too_short",
        "platform_too_wide",
        "challenge_roles_repeated",
        "score_issues",
        "score_failed",
        "jump_graph_failed",
    }:
        recovery = (
            "修改并重新提交显式 platforms/segments 中指出的字段；禁止通过更换 seed、"
            "扩大区域或重复相同参数来碰运气。"
            f" repair_plan={json.dumps(details[:6], ensure_ascii=False)}"
        )
    else:
        recovery = (
            "根据结构化失败原因修改显式 platforms/segments 后重新校验；"
            f" repair_plan={json.dumps(details[:6], ensure_ascii=False)}"
        )
    frame.messages.append(
        {
            "role": "user",
            "history_role": "error",
            "content": (
                "出错：LLM 提交的平台扩图方案未通过校验，禁止执行空 edit_map_batches。"
                f"reason={reason}。{recovery}"
                "当前必须由 LLM 修订方案；不要搜索写入工具，也不要要求用户批准工具。"
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
    "只返回当前 VerifyOutcome JSON，不要任何额外文字、不要 markdown 代码块标记。"
    "字段必须且只能包含 schema_version=1、status(passed|failed|unavailable)、"
    "phase=semantic、reason_code、summary、issues、attempt、max_attempts、retryable、"
    "recovery_actions。禁止返回 passed 布尔字段。成功使用 reason_code=verified；"
    "发现问题使用 reason_code=semantic_issue；校验器自身不可用时使用对应的"
    " unavailable reason，绝不能伪装成功。"
)


def _parse_verify_response(
    text: str,
    *,
    attempt: int = 1,
    max_attempts: int = 1,
) -> VerifyOutcome:
    """严格解析唯一 VerifyOutcome；失败时返回 unavailable，绝不伪造通过。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析得到的 canonical `VerifyOutcome`，或带确切原因的 unavailable outcome。
    """
    cleaned = text.strip()
    malformed_actions = (
        (
            VerifyRecoveryAction(action="retry_verifier", target="semantic"),
            VerifyRecoveryAction(action="pause_unverified", target="semantic"),
        )
        if attempt < max_attempts
        else (VerifyRecoveryAction(action="pause_unverified", target="semantic"),)
    )
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Verify response is not valid JSON: %s", cleaned[:200])
        return VerifyOutcome.unavailable(
            phase="semantic",
            reason_code="response_malformed",
            summary="语义校验器返回了无法解析的响应。",
            attempt=attempt,
            max_attempts=max_attempts,
            recovery_actions=malformed_actions,
        )
    if not isinstance(data, dict):
        return VerifyOutcome.unavailable(
            phase="semantic",
            reason_code="response_malformed",
            summary="语义校验器响应不是对象。",
            attempt=attempt,
            max_attempts=max_attempts,
            recovery_actions=malformed_actions,
        )
    try:
        return VerifyOutcome.from_payload(data)
    except UnsupportedVerifySchemaError as exc:
        logger.warning("Verify response failed validation: %s", exc)
        reason = (
            "unsupported_verify_schema"
            if "passed" in data
            else "response_malformed"
        )
        return VerifyOutcome.unavailable(
            phase="semantic",
            reason_code=reason,
            summary=f"语义校验响应不符合当前协议：{reason}",
            attempt=attempt,
            max_attempts=max_attempts,
            recovery_actions=malformed_actions,
        )


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


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
