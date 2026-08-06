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
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, Frame
from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatRequest,
    ChatResponse,
    ChatToolCallsResponse,
    FrontToolCallDTO,
    InterruptCause,
    InterruptResponse,
    ResetResponse,
    SessionHistoryResponse,
    ToolResult,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import LLMProvider
from app.orchestrator.agent import (
    AgentPromptFactory,
    ErrorResult,
    FinalResult,
    StepResult,
    ToolCallsResult,
    run_turn,
    set_macro_v2_enforced,
)
from app.orchestrator.completion_gate import (
    completion_gate_text,
    evaluate_map_completion,
    has_canonical_map_target_revision,
)
from app.orchestrator.evidence import (
    EvidenceValidationError,
    register_screenshot_evidence,
)
from app.orchestrator.map_artifacts import (
    CURRENT_MAP_ARTIFACT_TURN,
    CoordinatedCommitFailureInjector,
    MapArtifactLocator,
    MapArtifactStore,
    MapArtifactTurnConflictError,
    StagedMapArtifactTurn,
    clear_session_artifacts,
)
from app.orchestrator.map_contracts import (
    MAP_WORKER_TO_RUNTIME_STAGE,
    MapResponseMode,
    arm_map_worker_structured_completion,
)
from app.orchestrator.map_progress import (
    build_map_progress_digest,
    consume_committed_platform_approvals,
    latest_map_revision,
    map_platform_plan_attempt_count,
    parse_map_plan_outcome,
    remember_map_plan_progress,
    remember_map_tool_failure,
    remember_planning_snapshot_evidence,
    remember_validation_cache,
    remember_validation_progress,
    reset_map_task_progress,
    resume_map_task,
    validation_mode,
)
from app.orchestrator.map_request_scope import (
    MapRequestScope,
    bind_map_task,
    invalidate_completion_candidate,
    is_continuation_intent,
    mark_completion_candidate,
    new_request_scope,
)
from app.orchestrator.map_workers import (
    MAP_REVISION_GUARDED_TOOL_NAMES,
    MAP_VALIDATION_TOOL_NAMES,
    PLATFORM_PLAN_TOOL_NAMES,
)
from app.orchestrator.map_workflow import (
    consume_map_resume_authorization,
    increment_map_counter,
    replace_map_state_field,
)
from app.output_styles.catalog import OutputStyleCatalog
from app.permissions.engine import make_session_allow_grant
from app.prompt.builder import LayeredPrompt, build_system_prompt
from app.prompt.context_builder import ContextBuilder
from app.prompt.project_context import build_project_context
from app.prompt.rag_context import build_rag_context
from app.query.compactor import SessionCompactor
from app.query.helpers import *
from app.query.tool_result_submission import (
    ToolResultBatchValidationError,
    ValidatedToolResultBatch,
    validate_tool_result_batch,
)
from app.rag.asset_llm_client import AssetLLMClient, AssetLLMConfig
from app.rag.factory import create_codebase_index
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import (
    RecoveryFailureInjector,
    RecoverySupervisor,
    RecoveryTokenError,
)
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.resource_registry import BACKEND_RESET_STEPS
from app.sessions.store import Session, SessionStore, session_from_dict, session_to_dict
from app.skills.catalog import SkillCatalog
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY
from app.verify.runner import VerifyRunner

logger = logging.getLogger(__name__)

_MAP_AUTO_COMPACT_CONTEXT_TOKENS = 64_000
_MODEL_LOG_FIELDS = frozenset({"model", "primary_model", "fallback_model"})
_MAP_MAX_AUTO_ITERATIONS = 3
_COMPLETED_TOOL_TURN_CACHE_SIZE = 64
_PREVIEW_EVENT_TYPES = frozenset({"agent_text_delta", "agent_reasoning_delta"})


def _submission_event_delivery(event_type: str) -> str:
    """Classify how an event may leave an active atomic submission."""
    if event_type in _PREVIEW_EVENT_TYPES:
        return "provisional_preview"
    if event_type == "turn_progress":
        return "out_of_band_liveness"
    return "transactional"


def _rebase_artifact_turn_identity(value: Any, old_turn_id: str, new_turn_id: str) -> None:
    """原地把一次未发布提交中的 artifact/turn 身份换到更大的 turn。"""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if (
                key in {"artifact_turn_id", "turn_id", "_submission_turn_id"}
                and item == old_turn_id
            ):
                value[key] = new_turn_id
            else:
                _rebase_artifact_turn_identity(item, old_turn_id, new_turn_id)
    elif isinstance(value, list):
        for item in value:
            _rebase_artifact_turn_identity(item, old_turn_id, new_turn_id)


def _map_completion_candidate_is_current(session: Session) -> bool:
    """Return whether the current final is owned by an active map-edit lineage."""
    scope = session.map_request_scope
    frame = session.top_frame()
    return (
        scope.activates_map_gate
        and scope.completion_candidate
        and session.map_task_state.status in {"running", "completed"}
        and scope.map_task_id == session.map_task_state.task_id
        and frame is not None
        and frame.map_request_lineage_id == scope.lineage_id
        and frame.map_task_id == scope.map_task_id
        and has_canonical_map_target_revision(session.map_task_state)
    )


def _bind_request_scope_to_frames(
    session: Session,
    scope: MapRequestScope,
    *,
    all_frames: bool,
) -> None:
    """Attach the active request lineage to the root/current workflow Frames."""
    frames = session.agent_stack if all_frames else session.agent_stack[:1]
    for frame in frames:
        frame.map_request_lineage_id = scope.lineage_id
        frame.map_task_id = scope.map_task_id or None


def _retire_non_root_frames(session: Session, reason: str) -> None:
    """Close an abandoned child stack so an unrelated request can use the root."""
    if len(session.agent_stack) <= 1:
        return
    root = session.agent_stack[0]
    direct_child = next(
        (frame for frame in session.agent_stack[1:] if frame.parent_id == root.id),
        None,
    )
    call_id: str | None = None
    if direct_child is not None:
        call_id = direct_child.pending_delegate_call_id
        if call_id is None and direct_child.pending_delegate_group_id is not None:
            group = session.delegate_groups.get(direct_child.pending_delegate_group_id, {})
            value = group.get("tool_call_id")
            call_id = str(value) if value else None
    if call_id:
        root.messages.append(
            _tool_message(
                call_id,
                {
                    "error": True,
                    "reason": reason,
                    "summary": "Previous delegated map runtime was made dormant by a new request.",
                },
                is_error=True,
            )
        )
    session.agent_stack = [root]
    session.pending_plan = None
    session.delegate_groups.clear()


def _can_contextually_resume_map_task(session: Session, user_message: str) -> bool:
    """判断续作指代是否唯一指向当前仍聚焦的已授权地图任务。

    该判断只解析“任务”所指对象，不创建新权限。只有上一请求、任务 lineage、
    暂停检查点三者一致时，泛化续作文本才可继承原地图编辑范围。

    Args:
        session: 当前会话。
        user_message: 当前用户消息原文。

    Returns:
        是否可以无歧义恢复当前地图任务。
    """
    if not is_continuation_intent(user_message):
        return False
    state = session.map_task_state
    if state.status != "paused" or not state.task_id or not isinstance(state.checkpoint, dict):
        return False
    previous_scope = session.map_request_scope
    task_lineage = session.map_task_lineage
    return (
        previous_scope.intent == "map_edit"
        and previous_scope.map_task_id == state.task_id
        and previous_scope.lineage_id == str(task_lineage.get("lineage_id", ""))
        and state.task_id == str(task_lineage.get("task_id", ""))
    )


def _activate_user_request_scope(
    session: Session,
    request: ChatRequest,
    *,
    dedicated_resume_authorized: bool = False,
) -> tuple[MapRequestScope, bool]:
    """Classify a user request and explicitly start or resume a map task."""
    contextual_resume_authorized = _can_contextually_resume_map_task(
        session,
        request.user_message or "",
    )
    scope = new_request_scope(
        request_id=request.request_id,
        user_message=request.user_message or "",
        dedicated_resume_authorized=(dedicated_resume_authorized or contextual_resume_authorized),
    )
    state = session.map_task_state
    resumed_existing_task = False
    if scope.intent == "map_edit":
        can_continue = (
            scope.explicit_continuation
            and bool(state.task_id)
            and state.status in {"running", "paused"}
        )
        if can_continue:
            if state.status == "paused":
                resume_map_task(state)
            scope = bind_map_task(scope, state.task_id)
            task_lineage = session.map_task_lineage
            if str(task_lineage.get("task_id", "")) == state.task_id and str(
                task_lineage.get("lineage_id", "")
            ):
                scope = replace(
                    scope,
                    lineage_id=str(task_lineage["lineage_id"]),
                    completion_candidate=(task_lineage.get("completion_candidate") is True),
                )
            resumed_existing_task = True
            _bind_request_scope_to_frames(session, scope, all_frames=True)
        else:
            if state.status == "paused":
                state.cancel("replaced_by_explicit_map_edit")
            _retire_non_root_frames(session, "replaced_by_explicit_map_edit")
            task_digest = hashlib.sha256(scope.lineage_id.encode("utf-8")).hexdigest()[:20]
            task_id = f"map-request-{task_digest}"
            root = session.agent_stack[0] if session.agent_stack else None
            reset_map_task_progress(
                session,
                root,
                task_id=task_id,
                lineage_id=scope.lineage_id,
            )
            scope = bind_map_task(scope, task_id)
            session.map_task_lineage = {
                "task_id": task_id,
                "lineage_id": scope.lineage_id,
                "origin_request_id": scope.request_id,
                "completion_candidate": False,
            }
            _bind_request_scope_to_frames(session, scope, all_frames=False)
    else:
        _retire_non_root_frames(session, "unrelated_request")
        _bind_request_scope_to_frames(session, scope, all_frames=False)
    session.map_request_scope = scope
    return scope, resumed_existing_task


@dataclass
class _SubmissionPublicationBuffer:
    """暂存工具结果事务产生的外部 artifact 与事件。"""

    session: Session
    request_id: str | None
    turn_id: str
    map_artifact_turn: StagedMapArtifactTurn
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    previews: dict[str, dict[str, Any]] = field(default_factory=dict)
    preview_resolved: bool = False
    preview_event_count: int = 0
    first_preview_seq: int = 0


@dataclass
class _TurnProgress:
    """保存一个活跃 `/chat` 请求的非持久化存活状态。"""

    owner_id: int
    request_id: str | None
    turn_id: str | None
    phase: str
    heartbeat_seq: int = 0


_PUBLICATION_BUFFER: ContextVar[_SubmissionPublicationBuffer | None] = ContextVar(
    "tool_result_publication_buffer",
    default=None,
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


def _tool_result_batch_identity(results: list[ToolResult] | None) -> tuple[str, str] | None:
    """生成与 request_id 无关的工具结果批次身份。

    返回 ``(turn_id, sha256_fingerprint)`` 二元组，用于跨 request_id 的幂等缓存：
    即使前端因网络超时换了 request_id 重发，只要 turn_id 与工具结果内容不变，
    就能命中已消费的响应。指纹基于规范化 JSON（按 tool_use_id 排序、键排序、
    紧凑分隔），确保同一批次不同序列化顺序仍产生相同哈希。
    """
    if not results:
        return None
    # 批次内所有结果必须属于同一个 turn_id，否则无法与 pending_turn_id 对齐
    turn_ids = {result.turn_id for result in results}
    if len(turn_ids) != 1:
        return None
    # 按 tool_use_id 排序后序列化，消除前端可能传入的不同顺序
    canonical_results = sorted(
        (result.model_dump(mode="json") for result in results),
        key=lambda item: str(item.get("tool_use_id", "")),
    )
    encoded = json.dumps(
        canonical_results,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return next(iter(turn_ids)), hashlib.sha256(encoded).hexdigest()


def _remember_completed_tool_turn(
    session: Session,
    identity: tuple[str, str],
    response: ChatResponse,
) -> None:
    """保存已消费批次的第一次响应，并限制持久化缓存大小。

    以 turn_id 为键、响应快照为值写入 ``completed_tool_turn_cache``，
    供后续同一批次的重试请求做幂等命中。当缓存条目超过上限时，
    按插入顺序淘汰最早的条目（简易 LRU）。
    """
    turn_id, fingerprint = identity
    session.completed_tool_turn_cache[turn_id] = {
        "fingerprint": fingerprint,
        "response": response.model_dump(),
    }
    # 简易 LRU：dict 保持插入顺序，超限时淘汰最旧条目
    while len(session.completed_tool_turn_cache) > _COMPLETED_TOOL_TURN_CACHE_SIZE:
        oldest_turn_id = next(iter(session.completed_tool_turn_cache))
        del session.completed_tool_turn_cache[oldest_turn_id]


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
        return ChatErrorResponse(text=step.text, error_code=step.error_code)
    raise TypeError(f"未知编排结果类型：{type(step)!r}")


def _planner_completion_text(
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """把确定性平台校验结果转换为 planner 的结构化阶段输出。"""
    outcome = parse_map_plan_outcome(tool_name, result)
    profile_plan_value = result.get("profile_plan")
    profile_plan = profile_plan_value if isinstance(profile_plan_value, dict) else {}
    batches_value = result.get("edit_map_batches")
    if batches_value is None:
        batches_value = profile_plan.get("edit_map_batches")
    proposed_batches = (
        batches_value if outcome.executable and isinstance(batches_value, list) else []
    )
    issues_value = (
        result.get("issues")
        or result.get("repair_plan")
        or profile_plan.get("issues")
        or profile_plan.get("repair_plan")
    )
    issues = issues_value if isinstance(issues_value, list) else []
    target_value = result.get("target_path", result.get("target", tool_args.get("target_path", "")))
    target_path = target_value if isinstance(target_value, str) else ""
    revision_value = result.get("map_revision")
    map_revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else None
    )
    region = {
        key: tool_args[key]
        for key in ("x", "y", "z", "width", "height", "depth")
        if key in tool_args
    }
    publication_value = result.get("_planning_publication")
    publication = publication_value if isinstance(publication_value, dict) else {}
    summary = (
        "LLM 显式平台规划已通过确定性校验，规划阶段由服务端自动结束。"
        if outcome.executable
        else "第三次确定性校验仍未通过；最新规划已交付，但执行明确阻断且不会调度 writer。"
    )
    payload = {
        "stage": "planner",
        "worker": frame.agent.name,
        "mode": "propose_only",
        "objective": frame.agent.description or frame.agent.name,
        "target_path": target_path,
        "map_layer": tool_args.get("map_layer"),
        "map_revision": map_revision,
        "region": region,
        "summary": summary,
        "facts": [
            {
                "kind": "llm_platform_plan",
                "tool": tool_name,
                "platforms": tool_args.get("platforms", []),
                "segments": tool_args.get("segments", []),
            }
        ],
        "proposed_batches": proposed_batches,
        "planning_status": publication.get("planning_status", "delivered"),
        "execution_status": publication.get(
            "execution_status",
            "approved" if outcome.executable else "blocked_by_validation",
        ),
        "authoritative_snapshot": publication.get(
            "authoritative_snapshot",
            {
                "snapshot_id": tool_args.get("authoritative_snapshot_id"),
                "digest": tool_args.get("authoritative_snapshot_digest"),
            },
        ),
        "semantic_plan": publication.get(
            "semantic_plan",
            {
                "platforms": tool_args.get("platforms", []),
                "segments": tool_args.get("segments", []),
                "semantic_resources": tool_args.get("semantic_resources", []),
                "reference_cells": tool_args.get("reference_cells", []),
                "rationale": tool_args.get("rationale", ""),
            },
        ),
        "approved_batches": publication.get("approved_batches", []),
        "write_results": [],
        "validation": {
            "passed": outcome.executable,
            "completion_allowed": False,
            "issues": issues,
            "structured_issues": (
                []
                if outcome.executable
                else [
                    {
                        "code": outcome.error_code
                        or outcome.blocked_reason
                        or "platform_plan_not_executable",
                        "blocked_reason": outcome.blocked_reason,
                    }
                ]
            ),
        },
        "missing_inputs": [],
        "risks": [] if outcome.executable else ["平台路线尚不可执行，禁止进入写入阶段。"],
        "next_stage": "writer" if outcome.executable else "complete",
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_dynamic_map_writer(frame: Frame) -> bool:
    """判断当前帧是否为一次性地图写入 worker。"""
    return bool(frame.agent.workflow_operations) and frame.agent.edit_map_max_turns is not None


def _writer_platform_validation_failure_text(
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """把写入前平台校验失败转换为确定性的 Writer 阶段结果。"""
    payload_value = json.loads(_planner_completion_text(frame, tool_name, tool_args, result))
    payload = payload_value if isinstance(payload_value, dict) else {}
    payload.update(
        {
            "stage": "writer",
            "worker": frame.agent.name,
            "mode": "partial",
            "summary": (
                "平台写入前校验未通过；Writer 未执行 edit_map，" "已停止当前写入帧并返回 planner。"
            ),
            "proposed_batches": [],
            "write_results": [],
            "next_stage": "planner",
        }
    )
    return json.dumps(payload, ensure_ascii=False)


def _map_reader_has_detailed_region(result: dict[str, Any]) -> bool:
    """判断 reader 是否已拿到足以进入事实汇总阶段的精确区域。"""
    if result.get("ok") is False:
        return False
    if result.get("cells_format") not in {"non_empty_only", "full"}:
        return False
    revision = result.get("map_revision")
    return (
        bool(result.get("target_path") or result.get("target"))
        and isinstance(revision, int)
        and not isinstance(revision, bool)
    )


def _arm_map_reader_text_completion(
    frame: Frame,
    *,
    mode: MapResponseMode = "prompt_only",
    correction_limit: int = 2,
) -> None:
    """把 reader 的下一轮限制为无工具的结构化事实输出。"""
    if frame.force_text_only:
        return
    arm_map_worker_structured_completion(
        frame,
        mode=mode,
        correction_limit=correction_limit,
    )


def _map_batch_postcondition_error(
    status: str,
    tool_args: dict[str, Any],
    result: Any,
) -> str | None:
    """本地检查批次结果，失败时阻止释放后续批次。"""
    if status != "applied" or not isinstance(result, dict) or result.get("ok") is False:
        return "batch tool did not apply successfully"
    batch_id = str(tool_args.get("write_batch_id", ""))
    if batch_id and result.get("write_batch_id") != batch_id:
        return "write_batch_id mismatch"
    transaction_id = str(tool_args.get("map_transaction_id", ""))
    if transaction_id and result.get("map_transaction_id") != transaction_id:
        return "map_transaction_id mismatch"
    expected_revision = tool_args.get("expected_revision")
    actual_revision = result.get("map_revision")
    if (
        isinstance(expected_revision, int)
        and not isinstance(expected_revision, bool)
        and actual_revision != expected_revision + 1
    ):
        return "map revision did not advance exactly once"
    expected_cells = tool_args.get("expected_cells")
    if (
        isinstance(expected_cells, int)
        and not isinstance(expected_cells, bool)
        and isinstance(result.get("cells"), int)
        and result.get("cells") != expected_cells
    ):
        return "expected_cells postcondition failed"
    postconditions = tool_args.get("postconditions")
    if isinstance(postconditions, dict):
        for key, expected in postconditions.items():
            if result.get(key) != expected:
                return f"postcondition {key}={expected!r} failed"
    return None


def _remember_map_batch_result(
    session: Session,
    tool_name: str,
    status: str,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录确定性批次结果，并在局部校验失败时清空后续队列。"""
    if tool_name not in MAP_REVISION_GUARDED_TOOL_NAMES or "plan_version" not in tool_args:
        return
    state = session.map_task_state
    error = _map_batch_postcondition_error(status, tool_args, result)
    entry = {
        "plan_version": tool_args.get("plan_version"),
        "batch_index": tool_args.get("batch_index"),
        "write_batch_id": tool_args.get("write_batch_id"),
        "tool": tool_name,
        "result": result if isinstance(result, dict) else {},
        "postconditions_passed": error is None,
        "error": error,
        "map_transaction_id": tool_args.get("map_transaction_id"),
    }
    replace_map_state_field(
        state,
        "executed_batches",
        [*state.executed_batches, entry],
        target=str(tool_args.get("target_path", "")) or None,
        revision=(
            result.get("map_revision")
            if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
            else None
        ),
    )
    increment_map_counter(state, "executed_batches")
    transaction_id = str(tool_args.get("map_transaction_id", "")).strip()
    if transaction_id:
        journals = list(state.transaction_journals)
        matched = next(
            (item for item in journals if item.get("transaction_id") == transaction_id),
            None,
        )
        transaction_entry = (
            dict(matched)
            if isinstance(matched, dict)
            else {
                "transaction_id": transaction_id,
                "target": str(tool_args.get("target_path", "")),
                "base_revision": tool_args.get("map_transaction_base_revision"),
                "operation_ids": [],
            }
        )
        operation_ids = list(transaction_entry.get("operation_ids", []))
        batch_id = str(tool_args.get("write_batch_id", ""))
        if batch_id and batch_id not in operation_ids:
            operation_ids.append(batch_id)
        approval_records = list(transaction_entry.get("approval_records", []))
        approval_id = str(tool_args.get("approval_id", "")).strip()
        approval_fingerprint = str(tool_args.get("approval_batch_fingerprint", "")).strip()
        approval_expected_revision = tool_args.get("approval_expected_revision")
        if (
            approval_id
            and approval_fingerprint
            and isinstance(approval_expected_revision, int)
            and not isinstance(approval_expected_revision, bool)
            and not any(
                record.get("approval_id") == approval_id
                for record in approval_records
                if isinstance(record, dict)
            )
        ):
            approval_records.append(
                {
                    "approval_id": approval_id,
                    "batch_fingerprint": approval_fingerprint,
                    "expected_revision": approval_expected_revision,
                }
            )
        transaction_entry.update(
            {
                "operation_ids": operation_ids,
                "approval_records": approval_records,
                "final_revision": (
                    result.get("map_revision") if isinstance(result, dict) else None
                ),
                "status": "prepared" if error is None else "rolled_back",
                "error": error,
            }
        )
        journals = [item for item in journals if item.get("transaction_id") != transaction_id]
        journals.append(transaction_entry)
        replace_map_state_field(
            state,
            "transaction_journals",
            journals,
            target=str(tool_args.get("target_path", "")) or None,
            revision=(
                result.get("map_revision")
                if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
                else None
            ),
        )
    if error is None:
        if state.pending_batches:
            first = state.pending_batches[0]
            first_input = first.get("input", {}) if isinstance(first, dict) else {}
            if isinstance(first_input, dict) and first_input.get("write_batch_id") == tool_args.get(
                "write_batch_id"
            ):
                replace_map_state_field(
                    state,
                    "pending_batches",
                    state.pending_batches[1:],
                    target=str(tool_args.get("target_path", "")) or None,
                    revision=(
                        result.get("map_revision")
                        if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
                        else None
                    ),
                )
        increment_map_counter(state, "writes")
        # 通过 transition_stage 推进阶段，内部会触发派生状态（如缓存）的失效
        state.transition_stage("validate" if not state.pending_batches else "write")
        if not state.pending_batches and session.latest_context_used_tokens >= 32_000:
            session.force_compact_next_turn = True
        return
    increment_map_counter(state, "failed_batches")
    approval_expected_revision = tool_args.get("approval_expected_revision")
    current_revision = latest_map_revision(
        session,
        str(tool_args.get("target_path", "")),
        (
            tool_args.get("map_layer")
            if isinstance(tool_args.get("map_layer"), int)
            and not isinstance(tool_args.get("map_layer"), bool)
            else None
        ),
    )
    if (
        not str(tool_args.get("approval_id", "")).strip()
        or approval_expected_revision != current_revision
    ):
        replace_map_state_field(state, "pending_batches", [])
    # 批次失败时回退到 plan 阶段，重新规划
    state.transition_stage("plan")
    replace_map_state_field(state, "unresolved_issues", [error])


def _remember_map_transaction_validation(
    session: Session,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    successful: bool,
) -> None:
    """把 Godot 端验证后的 write-group 终态镜像到 Session。"""
    transaction_id = str(tool_args.get("map_transaction_id", "")).strip()
    if not transaction_id:
        return
    journals = list(session.map_task_state.transaction_journals)
    matched = next(
        (item for item in journals if item.get("transaction_id") == transaction_id),
        None,
    )
    if not isinstance(matched, dict):
        return
    updated = dict(matched)
    frontend_status = str(result.get("map_transaction_status", "")).strip()
    updated["status"] = (
        frontend_status
        if frontend_status in {"committed", "rolled_back", "failed"}
        else ("committed" if successful else "rolled_back")
    )
    updated["final_revision"] = result.get("map_revision", updated.get("final_revision"))
    updated["validation_tool"] = result.get("validation_tool")
    updated["error"] = None if successful else str(result.get("message", "map validation failed"))
    if successful and updated["status"] == "committed":
        consume_committed_platform_approvals(session, result, updated)
    journals = [item for item in journals if item.get("transaction_id") != transaction_id]
    journals.append(updated)
    replace_map_state_field(
        session.map_task_state,
        "transaction_journals",
        journals,
        target=str(updated.get("target", "")) or None,
        revision=(
            updated.get("final_revision")
            if isinstance(updated.get("final_revision"), int)
            else None
        ),
    )


def _resume_map_batch_queue(session: Session) -> ChatToolCallsResponse | None:
    """在上一批成功后直接下发下一批，不再唤醒 LLM。"""
    state = session.map_task_state
    if not state.pending_batches or state.unresolved_issues:
        return None
    raw = state.pending_batches[0]
    input_args = raw.get("input", {})
    if not isinstance(input_args, dict):
        replace_map_state_field(state, "pending_batches", [])
        return None
    target = str(input_args.get("target_path", ""))
    # 根据 map_layer 查询最新修订号，确保批次下发时携带正确的 expected_revision
    layer = input_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    latest_revision = latest_map_revision(session, target, map_layer)
    if latest_revision is not None:
        input_args["expected_revision"] = latest_revision
    call = FrontToolCallDTO(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        input=input_args,
        needs_confirm=bool(raw.get("needs_confirm", False)),
        frame_id=str(raw.get("frame_id", "")),
        agent=str(raw.get("agent", "map-agent")),
        render_kind=(str(raw["render_kind"]) if raw.get("render_kind") is not None else None),
    )
    frame = next((item for item in session.agent_stack if item.id == call.frame_id), None)
    if frame is None:
        replace_map_state_field(state, "pending_batches", [])
        return None
    frame.messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    turn_id = session.new_turn_id()
    session.set_pending(
        turn_id,
        [call.id],
        {
            call.id: {
                "name": call.name,
                "input": call.input,
                "frame_id": call.frame_id,
                "agent": call.agent,
                "needs_confirm": call.needs_confirm,
            }
        },
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=None, calls=[call])


from app.query.helpers import (
    _MAP_VALIDATION_REPEAT_LIMIT,
    _PERSISTED_HISTORY_EVENT_TYPES,
    _abort_pending_map_region_read_on_size_error,
    _append_platform_planning_failure_hint,
    _bind_map_validation_to_pending_write,
    _build_user_content,
    _clear_validation_blockers,
    _defer_map_tool_for_region_read,
    _defer_map_validation_for_state_read,
    _defer_map_write_for_state_read,
    _has_only_map_review_required,
    _has_review_blocker,
    _history_context_used_tokens,
    _history_payload_for_front_tool,
    _json_char_size,
    _map_completion_blocker,
    _map_region_from_write_args,
    _map_validation_is_successful,
    _persisted_history_events,
    _remember_latest_map_region_read,
    _remember_latest_map_revision,
    _remember_map_validation,
    _replace_last_assistant_final,
    _resume_pending_map_tool_after_read,
    _resume_pending_map_validation_after_read,
    _resume_pending_map_write_after_read,
    _review_required_blocker,
    _schedule_map_completion_continuation,
    _schedule_map_reviewer_if_required,
    _schedule_revision_conflict_reader,
    _structured_session_history,
    _tool_message,
    _update_map_context_state,
)
from app.query.history_to_events import blocks_to_pseudo_events


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
        coordinated_commit_failure_injector: CoordinatedCommitFailureInjector | None = None,
        recovery_failure_injector: RecoveryFailureInjector | None = None,
    ) -> None:
        """构造 QueryEngine。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
            cache_engine: 上下文缓存决策引擎（§16.1）；缺省时构造新实例。
            cache_metrics: 缓存命中率观测聚合器；缺省时构造新实例。
            coordinated_commit_failure_injector: 仅测试组合传入的命名故障依赖；
                生产默认关闭，且不从请求或持久化载荷读取。
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
        self._coordinated_commit_failure_injector = coordinated_commit_failure_injector
        self._recovery_supervisor = RecoverySupervisor(
            recovery_failure_injector,
            self._store.save_task_run,
        )
        self._verify_runner = VerifyRunner(
            settings,
            llm,
            self._emit,
            self._model_for_effort,
            self._thinking_budget_for_effort,
        )
        self._compactor = SessionCompactor(
            settings,
            session_store,
            llm,
            self._cache_engine,
            self._emit,
            lambda: self.available_tools,
            self._model_for_effort,
        )
        # session_id -> 该会话当前所有"正在处理 /chat 请求"的任务集合（通常只有
        # 一个，但用户可能在前一个请求仍卡在 per-session 锁等待时就发出下一条
        # 消息/中断，short-lived 地出现多个；用 set 而不是单个槎位，避免新任务
        # 覆盖掉真正持有锁、仍在运行的旧任务引用，导致 interrupt() 取消错对象。
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._turn_progress: dict[str, _TurnProgress] = {}
        self._history_blocks_cache: dict[
            tuple[str, str],
            tuple[tuple[int, int, int], list[Any]],
        ] = {}
        self._resume_incomplete_resets()

    def _complete_reset_cleanup(self, record: dict[str, Any]) -> int:
        """完成 epoch barrier 之后的幂等物理清理并返回 reset 边界序号。"""
        session_id = str(record["session_id"])
        new_epoch = str(record["new_epoch"])
        persisted_highwater = int(record.get("last_event_seq", 0) or 0)
        last_seq = persisted_highwater
        handlers: dict[str, Callable[[], None]] = {
            "event_content": lambda: self._reset_event_content(
                record,
                session_id,
                new_epoch,
                persisted_highwater,
            ),
            "in_memory_session": lambda: self._store.remove_session_payload(session_id),
            "session_document": lambda: self._store.remove_session_payload(session_id),
            "task_run_journal": lambda: self._store.remove_session_payload(session_id),
            "map_artifacts": lambda: clear_session_artifacts(
                self._settings.project_root,
                session_id,
            ),
            "delegate_artifacts": lambda: clear_session_artifacts(
                self._settings.project_root,
                session_id,
            ),
            "recovery_pointer": lambda: (
                self._recovery.clear(session_id) if self._recovery is not None else None
            ),
            "history_projection_cache": lambda: self._history_blocks_cache.pop(
                (session_id, str(record.get("old_epoch", ""))),
                None,
            ),
            "turn_progress": lambda: self._turn_progress.pop(session_id, None),
        }
        if set(handlers) != set(BACKEND_RESET_STEPS):
            raise ValueError("reset handler registry does not match resource contracts")
        self._store.checkpoint_reset(record, "cleaning")
        for resource_id in BACKEND_RESET_STEPS:
            if resource_id != "event_content" and self._store.reset_step_completed(
                record, resource_id
            ):
                continue
            self._store.hit_reset_failpoint(f"cleanup_before_{resource_id}")
            handlers[resource_id]()
            self._store.complete_reset_step(record, resource_id)
            self._store.hit_reset_failpoint(f"cleanup_after_{resource_id}")
        if self._events is not None:
            last_seq = max(
                int(record.get("last_event_seq", 0) or 0),
                self._events.last_seq(session_id),
            )
        record["last_event_seq"] = last_seq
        self._store.finish_reset(record)
        return last_seq

    def _reset_event_content(
        self,
        record: dict[str, Any],
        session_id: str,
        new_epoch: str,
        persisted_highwater: int,
    ) -> None:
        """切换 EventStore epoch 并把 reset 边界序号写回 reset 记录。"""
        if self._events is None:
            return
        if self._events.current_epoch(session_id) != new_epoch:
            self._events.ensure_sequence(
                session_id,
                persisted_highwater,
                session_epoch=new_epoch,
            )
            boundary = self._events.reset(session_id, new_epoch)
            record["last_event_seq"] = boundary.seq
        else:
            record["last_event_seq"] = self._events.last_seq(session_id)

    def _resume_incomplete_resets(self) -> None:
        """服务启动时继续 epoch 已切换但尚未完成的 reset 清理。"""
        for record in self._store.pending_reset_records():
            try:
                current_epoch = self._store.current_epoch(
                    str(record["session_id"]),
                    create=False,
                )
                if current_epoch != record.get("new_epoch"):
                    self._store.abandon_reset(
                        record,
                        (
                            "epoch_barrier_not_established"
                            if current_epoch == record.get("old_epoch")
                            else "reset_record_lost_epoch_ownership"
                        ),
                    )
                    continue
                self._complete_reset_cleanup(record)
            except (OSError, TypeError, ValueError):
                logger.exception(
                    "Incomplete reset cleanup remains pending session=%s reset_id=%s",
                    record.get("session_id"),
                    record.get("reset_id"),
                )

    async def resume_pending_recoveries(self) -> int:
        """启动时重建未终态 TaskRun 的监督状态并发布可观察恢复事件。"""
        resumed = 0
        for session_id in self._store.task_run_session_ids():
            async with self._store.lock_for(session_id):
                session = self._store.get_or_create(
                    session_id,
                    self.available_tools,
                )
                run = self._recovery_supervisor.resume_after_restart(session)
                if run is None:
                    continue
                self._store.save_task_run(session)
                self._emit(
                    session_id,
                    "recovery_resumed",
                    {
                        "task_id": run.get("task_id"),
                        "attempt_id": run.get("current_attempt_id"),
                        "checkpoint_id": run.get("checkpoint_id"),
                        "disposition": run.get("active_disposition"),
                        "side_effect_state": run.get("side_effect_state"),
                        "next_action": run.get("next_action"),
                        "status": run.get("status"),
                    },
                )
                resumed += 1
        return resumed

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    def turn_progress(self, session_id: str) -> dict[str, Any] | None:
        """返回活跃请求的事务外存活快照，并推进临时心跳序号。

        该状态只存在于进程内，不写 Session、历史、artifact 或恢复指针。

        Args:
            session_id: 查询的会话标识。

        Returns:
            活跃请求的存活信息；无活跃请求时返回 None。
        """
        progress = self._turn_progress.get(session_id)
        if progress is None:
            return None
        progress.heartbeat_seq += 1
        return {
            "type": "turn_progress",
            "session_id": session_id,
            "request_id": progress.request_id,
            "turn_id": progress.turn_id,
            "phase": progress.phase,
            "heartbeat_seq": progress.heartbeat_seq,
        }

    def _set_turn_progress(
        self,
        session_id: str,
        *,
        owner_id: int,
        request_id: str | None,
        turn_id: str | None,
        phase: str,
    ) -> None:
        """更新活跃请求的临时阶段，不触碰可恢复业务状态。"""
        current = self._turn_progress.get(session_id)
        heartbeat_seq = (
            current.heartbeat_seq if current is not None and current.owner_id == owner_id else 0
        )
        self._turn_progress[session_id] = _TurnProgress(
            owner_id=owner_id,
            request_id=request_id,
            turn_id=turn_id,
            phase=phase,
            heartbeat_seq=heartbeat_seq,
        )

    def _clear_turn_progress(self, session_id: str, owner_id: int) -> None:
        """仅清除属于指定请求的临时存活状态。"""
        current = self._turn_progress.get(session_id)
        if current is not None and current.owner_id == owner_id:
            del self._turn_progress[session_id]

    def _store_map_artifact(
        self,
        session_id: str,
        turn_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
    ) -> MapArtifactLocator | None:
        """把大型地图工具结果加入当前事务的会话级聚合 turn。"""
        if tool_name not in {
            "describe_map_region",
            "compute_reachable_frontier",
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
        if tool_name in MAP_VALIDATION_TOOL_NAMES and _json_char_size(result) < 8_000:
            return None
        session = self._store.get_or_create(session_id, self.available_tools)
        store = MapArtifactStore(
            self._settings.project_root,
            session_id,
            session_epoch=session.session_epoch,
        )
        publication_buffer = _PUBLICATION_BUFFER.get()
        if publication_buffer is not None:
            publication_buffer.map_artifact_turn.add_entry(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                tool_args=tool_args,
                result=result,
            )
            entry = publication_buffer.map_artifact_turn.entries[tool_use_id]
            return store.locator(
                publication_buffer.turn_id,
                tool_use_id,
                str(entry.get("fingerprint", "")),
            )
        staged = StagedMapArtifactTurn(
            session_id=session_id,
            turn_id=turn_id,
            request_id=None,
            session_epoch=session.session_epoch,
        )
        staged.add_entry(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
        )
        try:
            store.merge_turn(staged)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to write map artifact session=%s tool=%s turn=%s error=%s",
                session_id,
                tool_name,
                turn_id,
                exc,
            )
            return None
        entry = staged.entries[tool_use_id]
        return store.locator(
            turn_id,
            tool_use_id,
            str(entry.get("fingerprint", "")),
        )

    def session_history(
        self, session_id: str, limit: int = 200, before: int = 0
    ) -> SessionHistoryResponse:
        """Return frontend-renderable history for a persisted session."""
        session = self._store.get_or_create(session_id, self.available_tools)
        if self._events is not None:
            self._events.ensure_sequence(
                session_id,
                session.history_event_counter,
                session_epoch=session.session_epoch,
            )
        events = _persisted_history_events(session)
        if not events and self._events is not None:
            events = self._events.list_after(session_id, 0)
        # 下面的逐 frame/event 转换是 O(frames + events) 的纯 Python 工作；长期
        # 使用的会话（大量 delegate_many 子 agent frame + 持续累积的事件日志）
        # 不加界会让这一步随历史总量无限增长，最终触发前端 30s 看门狗超时、把
        # 本来该串行复用的请求队列卡死。既然最终只展示最近 `limit` 条，这里先
        # 把输入收窄到最近窗口再转换，而不是转换全量历史后再丢弃大半。
        omitted_inputs = False
        if limit > 0 and before <= 0:
            target_blocks = limit + max(before, 0)
            input_window = max(target_blocks, 1)
            while True:
                recent_frames = session.agent_stack[-input_window:]
                recent_events = events[-(input_window * 8) :]
                omitted_inputs = len(recent_frames) < len(session.agent_stack) or len(
                    recent_events
                ) < len(events)
                blocks = _structured_session_history(recent_frames, recent_events)
                if not omitted_inputs or len(blocks) >= target_blocks:
                    break
                input_window *= 2
        else:
            # 局部窗口会把较早的 frame 误判成历史末尾，提前返回
            # history_has_more=false。仅在真正向上翻页时构建完整时间线，
            # 并缓存结果，避免每页重复做 O(frames + events) 的转换。
            recent_frames = session.agent_stack
            recent_events = events
            cache_key = (
                len(recent_frames),
                len(recent_events),
                recent_events[-1].seq if recent_events else 0,
            )
            history_cache_key = (session.session_id, session.session_epoch)
            cached = self._history_blocks_cache.get(history_cache_key)
            if cached is not None and cached[0] == cache_key:
                blocks = cached[1]
            else:
                blocks = _structured_session_history(recent_frames, recent_events)
                self._history_blocks_cache[history_cache_key] = (cache_key, blocks)
        offset = min(max(before, 0), len(blocks))
        end = len(blocks) - offset
        start = max(0, end - limit) if limit > 0 else 0
        page = blocks[start:end]
        page_pseudo_events = blocks_to_pseudo_events(page)
        logger.info(
            "Session history requested session=%s frames=%d/%d blocks=%d events=%d pending=%s",
            session_id,
            len(recent_frames),
            len(session.agent_stack),
            len(page),
            len(page_pseudo_events),
            session.pending_turn_id is not None,
        )
        return SessionHistoryResponse(
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            last_event_seq=self._events.last_seq(session_id) if self._events is not None else 0,
            pending_turn_id=session.pending_turn_id,
            context_used_tokens=_history_context_used_tokens(session, events),
            context_token_limit=self._settings.auto_compact_token_threshold,
            map_worker_structured_output_enabled=(
                self._settings.map_worker_structured_output_enabled
            ),
            map_worker_response_contract_mode=(self._settings.map_worker_response_contract_mode),
            map_worker_structured_correction_limit=(
                self._settings.map_worker_structured_correction_limit
            ),
            map_worker_structured_thinking_budget=(
                self._settings.map_worker_structured_thinking_budget
            ),
            history_before=offset + len(page),
            history_has_more=start > 0 or omitted_inputs,
            pseudo_events=page_pseudo_events,
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
        progress_owner = id(task) if task is not None else id(request)
        if task is not None:
            self._active_tasks.setdefault(request.session_id, set()).add(task)
        self._set_turn_progress(
            request.session_id,
            owner_id=progress_owner,
            request_id=request.request_id,
            turn_id=None,
            phase="queued",
        )
        try:
            async with self._store.lock_for(request.session_id):
                self._set_turn_progress(
                    request.session_id,
                    owner_id=progress_owner,
                    request_id=request.request_id,
                    turn_id=None,
                    phase="accepted",
                )
                session = self._store.get_or_create(request.session_id, self.available_tools)
                resumed_run = self._recovery_supervisor.resume_after_restart(session)
                if resumed_run is not None:
                    self._store.save_task_run(session)
                if (
                    request.session_epoch is not None
                    and request.session_epoch != session.session_epoch
                ):
                    return ChatErrorResponse(
                        text="请求属于已重置的旧会话生命周期，请刷新会话状态后重试",
                        error_code="stale_session_epoch",
                        disposition="wait_frontend",
                        retryable=True,
                        side_effect_state="none",
                        next_action={
                            "action": "adopt_session_epoch",
                            "session_epoch": session.session_epoch,
                        },
                    )
                try:
                    MapArtifactStore(
                        self._settings.project_root,
                        session.session_id,
                        self._coordinated_commit_failure_injector,
                        session.session_epoch,
                    ).reconcile_with_session(session_to_dict(session))
                except (OSError, TypeError, ValueError):
                    logger.exception(
                        "Map artifact startup reconciliation failed session=%s",
                        session.session_id,
                    )
                # 确保事件存储的序列号与会话历史计数器对齐，
                # 防止因崩溃恢复或跨进程导致的序列偏移
                if self._events is not None:
                    self._events.ensure_sequence(
                        session.session_id,
                        session.history_event_counter,
                        session_epoch=session.session_epoch,
                    )
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

                # ---- 工具结果批次幂等缓存 ----
                # 前端可能因超时/断连重发同一批 tool_results（request_id 可能不同），
                # 此处基于 turn_id + 内容指纹做二次幂等保护：
                # 1) 指纹匹配 → 直接返回缓存的响应，避免重复执行
                # 2) 指纹不匹配 → 同一 turn_id 内容不同属于协议违规，拒绝请求
                tool_batch_identity = _tool_result_batch_identity(request.tool_results)
                if tool_batch_identity is not None:
                    completed_turn = session.completed_tool_turn_cache.get(tool_batch_identity[0])
                    if isinstance(completed_turn, dict):
                        if completed_turn.get("fingerprint") != tool_batch_identity[1]:
                            self._recovery_supervisor.begin_attempt(session, request)
                            problem = self._recovery_supervisor.problem(
                                session,
                                error_code="tool_result_batch_mismatch",
                                text="同一 turn_id 已处理，但重试的 tool_results 内容不同",
                            )
                            self._store.save_task_run(session)
                            return ChatErrorResponse(**problem)
                        cached_response = completed_turn.get("response")
                        if isinstance(cached_response, dict):
                            logger.info(
                                "Tool result batch idempotency hit session=%s turn_id=%s",
                                request.session_id,
                                tool_batch_identity[0],
                            )
                            return _response_from_dict(cached_response)

                try:
                    self._recovery_supervisor.begin_attempt(session, request)
                    self._store.save_task_run(session)
                except RecoveryTokenError as exc:
                    return ChatErrorResponse(
                        text=str(exc),
                        error_code="invalid_recovery_token",
                        disposition="pause_for_user",
                        retryable=False,
                        side_effect_state="none",
                    )

                validated_tool_batch: ValidatedToolResultBatch | None = None
                if request.tool_results is not None:
                    try:
                        validated_tool_batch = validate_tool_result_batch(
                            session,
                            request.tool_results,
                            REGISTRY,
                        )
                    except ToolResultBatchValidationError as exc:
                        logger.warning(
                            "Tool result preflight rejected session=%s code=%s reason=%s",
                            request.session_id,
                            exc.code,
                            exc.message,
                        )
                        problem = self._recovery_supervisor.problem(
                            session,
                            error_code="tool_result_preflight_failed",
                            text=exc.message,
                        )
                        self._store.save_task_run(session)
                        return ChatErrorResponse(**problem)
                progress_turn_id = (
                    validated_tool_batch.turn_id if validated_tool_batch is not None else None
                )
                self._set_turn_progress(
                    request.session_id,
                    owner_id=progress_owner,
                    request_id=request.request_id,
                    turn_id=progress_turn_id,
                    phase=(
                        "tool_result_transaction"
                        if validated_tool_batch is not None
                        else "agent_turn"
                    ),
                )

                # 取消保护快照：本轮可能在追加 assistant 的 tool_calls 后、写入对应
                # tool result 之前被 interrupt 取消。若让这半截历史留在内存里，下一次
                # 请求发给 OpenAI 兼容端点会因 tool_call 缺少 tool result 而 400。取消
                # 时回滚到本轮开始前的内存快照（本轮尚未 save()，磁盘仍是旧版本）。
                snapshot = copy.deepcopy(session)
                working_session = (
                    copy.deepcopy(session) if validated_tool_batch is not None else session
                )
                publication_buffer: _SubmissionPublicationBuffer | None = None
                publication_token: Token[_SubmissionPublicationBuffer | None] | None = None
                map_artifact_token: Token[StagedMapArtifactTurn | None] | None = None
                if validated_tool_batch is not None:
                    staged_map_turn = StagedMapArtifactTurn(
                        session_id=working_session.session_id,
                        turn_id=validated_tool_batch.turn_id,
                        request_id=request.request_id,
                        session_epoch=working_session.session_epoch,
                    )
                    publication_buffer = _SubmissionPublicationBuffer(
                        session=working_session,
                        request_id=request.request_id,
                        turn_id=validated_tool_batch.turn_id,
                        map_artifact_turn=staged_map_turn,
                    )
                    publication_token = _PUBLICATION_BUFFER.set(publication_buffer)
                    map_artifact_token = CURRENT_MAP_ARTIFACT_TURN.set(staged_map_turn)
                try:
                    response, working_session = await self._submit_with_backend_recovery(
                        working_session,
                        request,
                        validated_tool_batch,
                        snapshot=snapshot,
                        publication_buffer=publication_buffer,
                    )
                    if publication_buffer is not None:
                        publication_buffer.session = working_session
                except asyncio.CancelledError:
                    if publication_buffer is not None:
                        self._resolve_submission_previews(
                            publication_buffer,
                            committed=False,
                            reason="cancelled",
                        )
                    if isinstance(snapshot.task_run, dict):
                        try:
                            problem = self._recovery_supervisor.problem(
                                snapshot,
                                error_code="response_transport_lost",
                                text="请求传输已中断；任务检查点和 attempt 身份已保留",
                                side_effect_state="ambiguous",
                                next_action={
                                    "action": "reconnect_and_observe_attempt",
                                },
                            )
                            snapshot.task_run["last_problem"] = problem
                            self._store.save_task_run(snapshot)
                        except (OSError, TypeError, ValueError):
                            logger.exception(
                                "Failed to persist cancelled transport state session=%s",
                                request.session_id,
                            )
                    self._store.replace_in_memory(request.session_id, snapshot)
                    raise
                except Exception:
                    if publication_buffer is not None:
                        self._resolve_submission_previews(
                            publication_buffer,
                            committed=False,
                            reason="submission_failed",
                        )
                    problem = self._recovery_supervisor.problem(
                        snapshot,
                        error_code="submission_internal_error",
                        text=(
                            "处理工具结果时发生内部错误；会话状态已回滚，"
                            "后端已保留同一 attempt 的安全恢复检查点"
                        ),
                    )
                    self._store.replace_in_memory(request.session_id, snapshot)
                    try:
                        self._store.save_task_run(snapshot)
                    except (OSError, TypeError, ValueError):
                        logger.exception(
                            "Failed to persist recovery problem session=%s",
                            request.session_id,
                        )
                    logger.exception(
                        "Chat request failed; restored session snapshot session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return ChatErrorResponse(**problem)
                finally:
                    if map_artifact_token is not None:
                        CURRENT_MAP_ARTIFACT_TURN.reset(map_artifact_token)
                    if publication_token is not None:
                        _PUBLICATION_BUFFER.reset(publication_token)

                if isinstance(response, ChatErrorResponse) and response.attempt_id is None:
                    problem = self._recovery_supervisor.problem(
                        working_session,
                        error_code=response.error_code or "internal_error",
                        text=response.text,
                        side_effect_state=(
                            response.side_effect_state
                            if response.side_effect_state != "none"
                            else None
                        ),
                        next_action=response.next_action,
                    )
                    response = ChatErrorResponse(**problem)
                    if publication_buffer is not None:
                        problem_fields = response.model_dump(
                            exclude={"type", "text"},
                            exclude_none=True,
                        )
                        for _, event_type, event_payload in publication_buffer.events:
                            if event_type == "error":
                                event_payload.update(problem_fields)
                else:
                    if not isinstance(response, ChatErrorResponse):
                        self._recovery_supervisor.complete_attempt(
                            working_session,
                            waiting_frontend=isinstance(response, ChatToolCallsResponse),
                        )
                        if working_session.map_task_state.status == "completed":
                            self._recovery_supervisor.mark_terminal(
                                working_session,
                                outcome="completed",
                                authorized_by="completion_gate",
                            )
                self._store.save_task_run(working_session)

                if request.request_id is not None:
                    working_session.request_id_cache[request.request_id] = response.model_dump()
                # 若本轮已完整消费且 pending_turn_id 已推进（说明不是同一轮的重试），
                # 将响应写入幂等缓存，供后续相同批次的重放请求使用
                if (
                    tool_batch_identity is not None
                    and working_session.pending_turn_id != tool_batch_identity[0]
                ):
                    _remember_completed_tool_turn(
                        working_session,
                        tool_batch_identity,
                        response,
                    )
                artifact_store: MapArtifactStore | None = None
                artifact_prepared = False
                try:
                    if (
                        publication_buffer is not None
                        and publication_buffer.map_artifact_turn.entries
                    ):
                        artifact_store = MapArtifactStore(
                            self._settings.project_root,
                            working_session.session_id,
                            self._coordinated_commit_failure_injector,
                            working_session.session_epoch,
                        )
                        artifact_prepared = artifact_store.prepare_turn(
                            publication_buffer.map_artifact_turn
                        )
                    self._set_turn_progress(
                        request.session_id,
                        owner_id=progress_owner,
                        request_id=request.request_id,
                        turn_id=progress_turn_id,
                        phase="committing",
                    )
                    if self._coordinated_commit_failure_injector is not None:
                        self._coordinated_commit_failure_injector.hit(
                            "session_publish_before_write"
                        )
                    self._store.save(working_session)
                    if self._coordinated_commit_failure_injector is not None:
                        self._coordinated_commit_failure_injector.hit("session_publish_after_write")
                except MapArtifactTurnConflictError as exc:
                    try:
                        if publication_buffer is None or artifact_store is None:
                            raise ValueError("turn conflict has no recoverable publication")
                        old_turn_id = exc.turn_id
                        self._recovery_supervisor.hit_failpoint("fresh_turn_before_allocate")
                        fresh_turn_id = working_session.new_turn_id()
                        self._recovery_supervisor.hit_failpoint("fresh_turn_after_allocate")
                        conflict_problem = self._recovery_supervisor.problem(
                            working_session,
                            error_code=exc.error_code,
                            text=(
                                "工具结果 turn_id 与已提交内容冲突；原提交已保留，"
                                "后端正在新的更大 turn_id 下恢复"
                            ),
                            side_effect_state="committed",
                            next_action={
                                "action": "backend_rebase_and_commit",
                                "turn_id": fresh_turn_id,
                            },
                        )
                        recovery_token = conflict_problem.get("retry_token")
                        if not isinstance(recovery_token, str) or not recovery_token:
                            raise ValueError("turn conflict did not issue a recovery token")
                        recovery_request = request.model_copy(
                            update={"recovery_token": recovery_token}
                        )
                        self._recovery_supervisor.begin_attempt(
                            working_session,
                            recovery_request,
                        )

                        rebased_payload = session_to_dict(working_session)
                        _rebase_artifact_turn_identity(
                            rebased_payload,
                            old_turn_id,
                            fresh_turn_id,
                        )
                        recovered_session = session_from_dict(
                            rebased_payload,
                            self.available_tools,
                        )
                        recovered_session.turn_counter = max(
                            recovered_session.turn_counter,
                            working_session.turn_counter,
                        )
                        recovered_response_payload = response.model_dump()
                        _rebase_artifact_turn_identity(
                            recovered_response_payload,
                            old_turn_id,
                            fresh_turn_id,
                        )
                        recovered_response = _response_from_dict(recovered_response_payload)
                        publication_buffer.session = recovered_session
                        publication_buffer.turn_id = fresh_turn_id
                        publication_buffer.map_artifact_turn.turn_id = fresh_turn_id
                        for _, _, event_payload in publication_buffer.events:
                            _rebase_artifact_turn_identity(
                                event_payload,
                                old_turn_id,
                                fresh_turn_id,
                            )
                        artifact_prepared = artifact_store.prepare_turn(
                            publication_buffer.map_artifact_turn
                        )
                        self._recovery_supervisor.complete_attempt(
                            recovered_session,
                            waiting_frontend=isinstance(
                                recovered_response,
                                ChatToolCallsResponse,
                            ),
                        )
                        self._store.save_task_run(recovered_session)
                        self._store.save(recovered_session)
                        if artifact_prepared:
                            artifact_store.commit_prepared_turn(
                                publication_buffer.map_artifact_turn
                            )
                        self._flush_submission_publications(publication_buffer)
                        self._resolve_submission_previews(
                            publication_buffer,
                            committed=True,
                        )
                        self._record_recovery(
                            recovered_session,
                            recovered_response,
                        )
                        logger.warning(
                            "Map artifact turn conflict recovered session=%s "
                            "old_turn=%s fresh_turn=%s",
                            request.session_id,
                            old_turn_id,
                            fresh_turn_id,
                        )
                        return recovered_response
                    except (OSError, TypeError, ValueError, RecoveryTokenError):
                        if (
                            artifact_prepared
                            and artifact_store is not None
                            and publication_buffer is not None
                        ):
                            try:
                                artifact_store.discard_prepared_turn(
                                    publication_buffer.map_artifact_turn
                                )
                            except (OSError, TypeError, ValueError):
                                logger.exception(
                                    "Failed to discard rebased artifact session=%s",
                                    request.session_id,
                                )
                        if publication_buffer is not None:
                            self._resolve_submission_previews(
                                publication_buffer,
                                committed=False,
                                reason="turn_identity_recovery_failed",
                            )
                        snapshot.turn_counter = max(
                            snapshot.turn_counter,
                            working_session.turn_counter,
                        )
                        fresh_turn_id = snapshot.new_turn_id()
                        if snapshot.pending_turn_id is not None:
                            snapshot.pending_turn_id = fresh_turn_id
                        problem = self._recovery_supervisor.problem(
                            snapshot,
                            error_code=exc.error_code,
                            text=(
                                "工具结果 turn_id 与已提交内容冲突；原提交已保留，"
                                "自动恢复失败，已保留新的 turn 检查点"
                            ),
                            side_effect_state="committed",
                            next_action={
                                "action": "resubmit_tool_results",
                                "turn_id": fresh_turn_id,
                            },
                        )
                        self._store.replace_in_memory(
                            request.session_id,
                            snapshot,
                        )
                        self._store.save_task_run(snapshot)
                        self._store.save(snapshot)
                        logger.exception(
                            "Map artifact turn identity recovery failed " "session=%s turn=%s",
                            request.session_id,
                            exc.turn_id,
                        )
                        return ChatErrorResponse(**problem)
                except (OSError, TypeError, ValueError):
                    if (
                        artifact_prepared
                        and artifact_store is not None
                        and publication_buffer is not None
                    ):
                        try:
                            artifact_store.discard_prepared_turn(
                                publication_buffer.map_artifact_turn
                            )
                        except (OSError, TypeError, ValueError):
                            logger.exception(
                                "Failed to discard unreferenced prepared map artifact "
                                "session=%s turn=%s",
                                request.session_id,
                                publication_buffer.turn_id,
                            )
                    if publication_buffer is not None:
                        self._resolve_submission_previews(
                            publication_buffer,
                            committed=False,
                            reason="session_persistence_failed",
                        )
                    problem = self._recovery_supervisor.problem(
                        snapshot,
                        error_code="session_persistence_failed",
                        text="会话持久化失败；工具结果未提交，后端将从原检查点恢复",
                        side_effect_state="rolled_back",
                    )
                    self._store.replace_in_memory(request.session_id, snapshot)
                    try:
                        self._store.save_task_run(snapshot)
                    except (OSError, TypeError, ValueError):
                        logger.exception(
                            "Failed to persist persistence-failure recovery state " "session=%s",
                            request.session_id,
                        )
                    logger.exception(
                        "Session commit failed; original session retained session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return ChatErrorResponse(**problem)
                session = working_session
                if publication_buffer is not None:
                    if artifact_prepared and artifact_store is not None:
                        try:
                            artifact_store.commit_prepared_turn(
                                publication_buffer.map_artifact_turn
                            )
                        except (OSError, TypeError, ValueError):
                            logger.exception(
                                "Prepared map artifact finalization failed; "
                                "attempting reconciliation session=%s turn=%s",
                                request.session_id,
                                publication_buffer.turn_id,
                            )
                            try:
                                artifact_store.reconcile_with_session(
                                    session_to_dict(working_session)
                                )
                            except (OSError, TypeError, ValueError):
                                logger.exception(
                                    "Map artifact reconciliation remains pending "
                                    "session=%s turn=%s",
                                    request.session_id,
                                    publication_buffer.turn_id,
                                )
                    self._flush_submission_publications(publication_buffer)
                    self._resolve_submission_previews(
                        publication_buffer,
                        committed=True,
                    )
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
                    json.dumps(response.model_dump(), ensure_ascii=False, default=str),
                )
                return response
        finally:
            self._clear_turn_progress(request.session_id, progress_owner)
            if task is not None:
                tasks = self._active_tasks.get(request.session_id)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        del self._active_tasks[request.session_id]

    async def _submit_with_backend_recovery(
        self,
        session: Session,
        request: ChatRequest,
        validated_tool_batch: ValidatedToolResultBatch | None,
        *,
        snapshot: Session,
        publication_buffer: _SubmissionPublicationBuffer | None,
    ) -> tuple[ChatResponse, Session]:
        """在已证明回滚的边界内由后端执行有界的新 Attempt 重试。"""
        active = session
        while True:
            try:
                response = await self._submit_locked(
                    active,
                    request,
                    validated_tool_batch,
                )
                return response, active
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Chat submission attempt failed before backend recovery "
                    "session=%s request_id=%s",
                    request.session_id,
                    request.request_id,
                )
                retry_state = copy.deepcopy(snapshot)
                retry_state.task_run = copy.deepcopy(active.task_run)
                problem = self._recovery_supervisor.problem(
                    retry_state,
                    error_code="submission_internal_error",
                    text=(
                        "处理请求时发生内部错误；副作用已回滚，"
                        "后端正在从持久检查点启动新的 attempt"
                    ),
                    side_effect_state="rolled_back",
                )
                has_visible_or_staged_publication = publication_buffer is not None and bool(
                    publication_buffer.previews
                    or publication_buffer.events
                    or publication_buffer.map_artifact_turn.entries
                )
                if has_visible_or_staged_publication:
                    assert publication_buffer is not None
                    self._resolve_submission_previews(
                        publication_buffer,
                        committed=False,
                        reason="submission_failed",
                    )
                    publication_buffer.events.clear()
                    publication_buffer.previews.clear()
                    publication_buffer.map_artifact_turn.entries.clear()
                    problem = self._recovery_supervisor.force_pause(
                        retry_state,
                        problem,
                        action="resume_from_clean_submission_checkpoint",
                        reason="provisional_or_transactional_publication_was_discarded",
                    )
                self._store.replace_in_memory(request.session_id, retry_state)
                self._store.save_task_run(retry_state)
                next_action = problem.get("next_action")
                owner = str(next_action.get("owner", "")) if isinstance(next_action, dict) else ""
                token = problem.get("retry_token")
                if (
                    has_visible_or_staged_publication
                    or problem.get("disposition") != "retry_new_attempt"
                    or owner != "backend"
                    or not isinstance(token, str)
                    or not token
                ):
                    logger.exception(
                        "Chat request recovery paused session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return ChatErrorResponse(**problem), retry_state
                if publication_buffer is not None:
                    self._resolve_submission_previews(
                        publication_buffer,
                        committed=False,
                        reason="backend_retry_new_attempt",
                    )
                    publication_buffer.events.clear()
                    publication_buffer.previews.clear()
                    publication_buffer.map_artifact_turn.entries.clear()
                    publication_buffer.session = retry_state
                recovery_request = request.model_copy(update={"recovery_token": token})
                self._recovery_supervisor.begin_attempt(
                    retry_state,
                    recovery_request,
                )
                self._store.save_task_run(retry_state)
                delay_ms = (
                    int(next_action.get("backoff_ms", 0) or 0)
                    if isinstance(next_action, dict)
                    else 0
                )
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
                active = retry_state

    async def _submit_locked(
        self,
        session: Session,
        request: ChatRequest,
        validated_tool_batch: ValidatedToolResultBatch | None = None,
    ) -> ChatResponse:
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
            return ChatErrorResponse(
                text="user_message 与 tool_results 必须二选一",
                error_code="invalid_request_shape",
            )

        dedicated_resume_authorized = False
        if has_user:
            state = session.map_task_state
            task_lineage = session.map_task_lineage
            lineage_id = (
                str(task_lineage.get("lineage_id", ""))
                or str(session.map_request_scope.lineage_id)
                or state.task_id
            )
            dedicated_resume_authorized = consume_map_resume_authorization(
                state,
                task_id=state.task_id,
                lineage_id=lineage_id,
            )

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
            dynamic_context=(session.rag_context or "")
            + build_map_progress_digest(session, project_root=security.project_root),
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

        async def build_child_agent_prompt(agent: AgentDefinition, task: str) -> str:
            """为委派或自动恢复子 Agent 构造按任务检索的分层 system prompt。

            在构造 prompt 前先校验 Skill 绑定完整性：若 Agent 声明了所需 skill
            但目录未配置（missing）或版本不兼容（incompatible），立即抛出异常，
            避免子 Agent 在缺少关键能力的情况下被静默启动。
            """
            # Skill 绑定校验：确保子 Agent 所需的 skill 均已正确绑定
            if self._skill_catalog is None:
                if agent.skills:
                    raise ValueError("Skill 绑定失败：当前 QueryEngine 未配置 SkillCatalog")
            else:
                worker_binding_stage = (
                    MAP_WORKER_TO_RUNTIME_STAGE.get(str(agent.map_stage))
                    if agent.pipeline_kind == "map"
                    else None
                )
                binding = self._skill_catalog.binding_status(
                    agent.skills,
                    set(agent.effective_tools),
                    workflow_stage=(
                        worker_binding_stage
                        if worker_binding_stage is not None
                        else (
                            session.map_task_state.stage if agent.pipeline_kind == "map" else None
                        )
                    ),
                    worker_mode=agent.worker_mode,
                    agent_role=agent.role,
                    permitted_tools=set(agent.effective_tools),
                )
                if binding["missing"] or binding["incompatible"]:
                    raise ValueError(
                        "Skill 绑定失败："
                        + json.dumps(
                            {
                                "missing": binding["missing"],
                                "incompatible": binding["incompatible"],
                            },
                            ensure_ascii=False,
                        )
                    )
            # 按任务文本检索 RAG 上下文，与主 Agent 共享同一套检索管线
            task_rag_context = await self._retrieve_rag_context(security, task)
            child_context = ContextBuilder().build(
                stable_prefix=build_system_prompt(
                    agent,
                    self._skill_catalog,
                    self._output_styles,
                    session.output_style,
                ),
                structure_context=project_context,
                dynamic_context=(task_rag_context or "") + build_map_progress_digest(session),
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
                session,
                request.tool_results or [],
                security,
                build_child_agent_prompt,
                validated_tool_batch,
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
            resumed = self._resume_pending_map_tool_calls(session)
            if resumed is not None:
                return resumed
        else:
            if session.pending_turn_id is not None:
                logger.warning(
                    "User message rejected because tools are pending session=%s pending_turn=%s",
                    session.session_id,
                    session.pending_turn_id,
                )
                return ChatErrorResponse(
                    text="当前会话仍有待回传的工具结果，不能开始新的用户消息",
                    error_code="pending_tool_results",
                )
            request_scope, resumed_existing_map_task = _activate_user_request_scope(
                session,
                request,
                dedicated_resume_authorized=dedicated_resume_authorized,
            )
            frame = session.top_frame()
            if frame is None:
                logger.error(
                    "User message rejected because session has no active frame session=%s",
                    session.session_id,
                )
                return ChatErrorResponse(
                    text="会话没有活跃的 agent 帧",
                    error_code="missing_agent_frame",
                )
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            session.pending_verify_candidates.clear()
            if (
                # 显式自然语言续接或 resume_map_task 命令后的首次用户消息：
                # 仅属于该 map-edit lineage 的请求才恢复检查点与批次。
                resumed_existing_map_task
                and session.map_task_state.checkpoint is not None
            ):
                frame.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Resume the explicitly reactivated map task from its checkpoint. "
                            "Reuse cached facts and executed batches."
                        ),
                    }
                )
                resumed_batch = _resume_map_batch_queue(session)
                if resumed_batch is not None:
                    self._emit_tool_call_response(
                        session,
                        resumed_batch,
                        "Resumed map batch from explicit command session=%s turn_id=%s count=%d",
                    )
                    return resumed_batch
            self._emit(
                session.session_id,
                "user_submitted",
                {
                    "has_context": request.context is not None,
                    "request_intent": request_scope.intent,
                    "request_lineage_id": request_scope.lineage_id,
                    "map_task_id": request_scope.map_task_id,
                },
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
            await self._compact_locked_async(
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

        def emit_verify_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            self._emit(session.session_id, event_type, payload)

        step = await self._run_agent_turn(
            session,
            security,
            model_override,
            build_child_agent_prompt,
            emit_turn_event,
        )
        response = _step_to_response(step)
        response = self._defer_map_tool_calls_if_needed(session, response)
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
                step = await self._run_agent_turn(
                    session,
                    security,
                    model_override,
                    build_child_agent_prompt,
                    emit_verify_turn_event,
                )
                response = _step_to_response(step)
                response = self._defer_map_tool_calls_if_needed(session, response)
        map_gate_continuations = 0
        # ---- 地图完成门控循环 ----
        # 当 agent 产出 final 响应但仍有 completion_blockers 时，
        # 自动调度 reviewer 或 repair continuation，最多迭代 _MAP_MAX_AUTO_ITERATIONS 次
        while (
            isinstance(response, ChatFinalResponse)
            and _map_completion_candidate_is_current(session)
            and session.map_task_state.completion_blockers
            and session.map_task_state.auto_iterations < _MAP_MAX_AUTO_ITERATIONS
        ):
            scheduled = False
            # 优先尝试调度 reviewer 子 Agent（需要 build_child_agent_prompt 构造独立 prompt）
            if _has_only_map_review_required(session.map_task_state.completion_blockers):
                scheduled = await _schedule_map_reviewer_if_required(
                    session,
                    build_child_agent_prompt,
                )
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
                        len(session.map_task_state.completion_blockers),
                    )
            if not scheduled:
                break
            map_gate_continuations += 1
            replace_map_state_field(
                session.map_task_state,
                "auto_iterations",
                session.map_task_state.auto_iterations + 1,
            )
            step = await self._run_agent_turn(
                session,
                security,
                model_override,
                build_child_agent_prompt,
                emit_verify_turn_event,
            )
            response = _step_to_response(step)
            response = self._defer_map_tool_calls_if_needed(session, response)
        if isinstance(response, ChatToolCallsResponse):
            self._emit_tool_call_response(
                session,
                response,
                "Chat produced front tool calls session=%s turn_id=%s count=%d",
            )
        elif isinstance(response, ChatFinalResponse):
            if _map_completion_candidate_is_current(session):
                gate = evaluate_map_completion(session.map_task_state)
                if not gate.allowed:
                    gated_text = completion_gate_text(gate)
                    _replace_last_assistant_final(session, gated_text)
                    response = ChatFinalResponse(text=gated_text)
                elif session.map_task_state.status == "running":
                    session.map_task_state.complete()
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

    async def _run_agent_turn(
        self,
        session: Session,
        security: SecuritySettings,
        model_override: str | None,
        agent_prompt_factory: AgentPromptFactory,
        event_callback: Callable[[str, dict[str, Any]], None],
    ) -> StepResult:
        """用当前 QueryEngine 依赖运行一轮 agent 编排。"""
        set_macro_v2_enforced(self._settings.macro_v2_enforced)
        return await run_turn(
            session=session,
            llm=self._llm,
            security=security,
            tool_ctx=ToolContext(
                security=security,
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                skill_catalog=self._skill_catalog,
                rag_index_path=self._settings.resolved_rag_index_path(),
            ),
            max_turns=self._settings.max_turns,
            session_allow=session.session_allow,
            agent_prompt_factory=agent_prompt_factory,
            model_selector=self._model_for_effort,
            model_override=model_override,
            thinking_budget_selector=self._thinking_budget_for_effort,
            event_callback=event_callback,
            cache_engine=self._cache_engine,
            cache_metrics=self._cache_metrics,
            context_token_limit=self._settings.auto_compact_token_threshold,
            map_worker_structured_output_enabled=(
                self._settings.map_worker_structured_output_enabled
            ),
            map_worker_response_contract_mode=(self._settings.map_worker_response_contract_mode),
            map_worker_structured_correction_limit=(
                self._settings.map_worker_structured_correction_limit
            ),
            map_worker_structured_thinking_budget=(
                self._settings.map_worker_structured_thinking_budget
            ),
        )

    def _defer_map_tool_calls_if_needed(
        self,
        session: Session,
        response: ChatResponse,
    ) -> ChatResponse:
        """按既有顺序挂起需要先读取地图状态的工具调用。"""
        if not isinstance(response, ChatToolCallsResponse):
            return response
        response = _bind_map_validation_to_pending_write(session, response)
        response = _defer_map_write_for_state_read(session, response)
        response = _defer_map_validation_for_state_read(session, response)
        return _defer_map_tool_for_region_read(session, response)

    def _resume_pending_map_tool_calls(self, session: Session) -> ChatToolCallsResponse | None:
        """按既有优先级恢复自动读取后挂起的地图工具调用。"""
        resume_steps: tuple[
            tuple[Callable[[Session], ChatToolCallsResponse | None], str],
            ...,
        ] = (
            (
                _resume_map_batch_queue,
                "Resumed deterministic map batch session=%s turn_id=%s count=%d",
            ),
            (
                _resume_pending_map_tool_after_read,
                "Resumed pending map tool after region read session=%s turn_id=%s count=%d",
            ),
            (
                _resume_pending_map_write_after_read,
                "Resumed pending map write after state read session=%s turn_id=%s count=%d",
            ),
            (
                _resume_pending_map_validation_after_read,
                "Resumed pending map validation after state read session=%s turn_id=%s count=%d",
            ),
        )
        for resume, log_template in resume_steps:
            response = resume(session)
            if response is None:
                continue
            self._emit_tool_call_response(session, response, log_template)
            return response
        return None

    def _emit_tool_call_response(
        self,
        session: Session,
        response: ChatToolCallsResponse,
        log_template: str,
    ) -> None:
        """发送 tool_calls 事件并写入对应日志。"""
        self._emit(
            session.session_id,
            "tool_calls",
            {
                "turn_id": response.turn_id,
                "text": response.text,
                "calls": [call.model_dump(mode="json") for call in response.calls],
                "count": len(response.calls),
            },
        )
        logger.info(
            log_template,
            session.session_id,
            response.turn_id,
            len(response.calls),
        )

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
        self,
        tool_name: str,
        result: dict[str, Any],
        security: SecuritySettings,
        tool_args: dict[str, Any],
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
            "authority": "visual_only",
            "exact_fact_tools": ["describe_map_context", "describe_map_region"],
        }
        raw_question = tool_args.get("question") if tool_name == "read_image_metadata" else None
        question = raw_question.strip()[:2000] if isinstance(raw_question, str) else ""
        if question:
            semantic["question"] = question
        if not client.available:
            semantic["skipped"] = "asset_understanding_not_configured"
            enriched["semantic"] = semantic
            return enriched
        image_path = self._resolve_front_image_path(enriched, security)
        if image_path is None:
            semantic["skipped"] = "image_path_not_readable_by_service"
            enriched["semantic"] = semantic
            return enriched
        description = await asyncio.to_thread(
            client.describe,
            image_path,
            "image",
            question or None,
        )
        semantic["source_path"] = str(image_path)
        semantic["description"] = description
        semantic["answer"] = description
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
        self,
        session: Session,
        results: list[ToolResult],
        security: SecuritySettings,
        prompt_factory: AgentPromptFactory | None = None,
        validated_batch: ValidatedToolResultBatch | None = None,
    ) -> tuple[ChatErrorResponse | None, list[dict[str, Any]]]:
        """校验并把前端工具结果追加到对应 agent 帧。

        Returns:
            `(error, verify_candidates)`：`error` 非 None 时本次回传被拒绝，
            `verify_candidates` 此时必为空列表；否则 `verify_candidates` 收集
            本次落地、且命中 `verify_trigger_tools` 的编辑类工具调用，供调用方
            驱动 Verify 两阶段校验（§3.3）。
        """
        try:
            batch = validated_batch or validate_tool_result_batch(
                session,
                results,
                REGISTRY,
            )
        except ToolResultBatchValidationError as exc:
            logger.warning(
                "Tool result preflight rejected session=%s code=%s reason=%s",
                session.session_id,
                exc.code,
                exc.message,
            )
            return (
                ChatErrorResponse(
                    text=exc.message,
                    error_code="tool_result_preflight_failed",
                ),
                [],
            )

        results = [item.result for item in batch.items]
        frames = {frame.id: frame for frame in session.agent_stack}

        verify_candidates: list[dict[str, Any]] = []
        for result in results:
            frame = frames[result.frame_id]
            is_error = result.status in {"rejected", "error"}
            metadata = session.pending_tool_calls.get(result.tool_use_id, {})
            tool_name = str(metadata.get("name", ""))
            tool_args = metadata.get("input", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool = REGISTRY.get(tool_name)
            payload: Any
            map_artifact_locator: MapArtifactLocator | None = None
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
                        tool_name,
                        applied_result,
                        security,
                        tool_args,
                    )
                    map_artifact_locator = self._store_map_artifact(
                        session.session_id,
                        batch.turn_id,
                        result.tool_use_id,
                        tool_name,
                        tool_args,
                        applied_result,
                    )
                    _update_map_context_state(
                        session,
                        tool_name,
                        tool_args,
                        applied_result,
                        (
                            map_artifact_locator.artifact_ref
                            if map_artifact_locator is not None
                            else None
                        ),
                        (
                            map_artifact_locator.as_dict()
                            if map_artifact_locator is not None
                            else None
                        ),
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
                if map_artifact_locator is not None:
                    artifact_refs.append(map_artifact_locator.artifact_ref)
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
            # ---- structure_revision 推进 ----
            # ensure_standard_map_layers 实际创建了/删除了图层时推进结构版本号，
            # 使下游依赖结构信息的派生状态（缓存、校验结果等）自动失效；
            # 未发生变更时仍写入当前版本号，保持下游可追溯。
            if (
                tool_name == "ensure_standard_map_layers"
                and result.status == "applied"
                and isinstance(result_for_gate, dict)
            ):
                if result_for_gate.get("changed") is True:
                    result_for_gate["structure_revision"] = (
                        session.map_task_state.record_structure_change()
                    )
                else:
                    result_for_gate["structure_revision"] = (
                        session.map_task_state.structure_revision
                    )
            # ---- 截图证据记录 ----
            # capture_viewport_screenshot 成功后将截图元信息追加到 frame.map_evidence，
            # 供后续的地图完整性校验使用（关联 target_path、revision、focus_region 等）
            if (
                tool_name == "capture_viewport_screenshot"
                and result.status == "applied"
                and isinstance(result_for_gate, dict)
            ):
                screenshot_target = str(
                    result_for_gate.get("target_path", tool_args.get("target_path", ""))
                )
                screenshot_revision = result_for_gate.get(
                    "map_revision",
                    latest_map_revision(
                        session,
                        screenshot_target,
                        session.map_task_state.latest_layers.get(screenshot_target),
                    ),
                )
                evidence_result = {
                    **result_for_gate,
                    "target_path": screenshot_target,
                    "map_revision": screenshot_revision,
                    "region": (
                        dict(tool_args["focus_region"])
                        if isinstance(tool_args.get("focus_region"), dict)
                        else {}
                    ),
                }
                try:
                    evidence = register_screenshot_evidence(
                        session.map_task_state,
                        frame,
                        tool_use_id=result.tool_use_id,
                        result=evidence_result,
                        artifact_refs=list(payload.get("artifact_refs", [])),
                        project_root=self._settings.project_root,
                    )
                except EvidenceValidationError as exc:
                    logger.warning(
                        "Screenshot evidence rejected session=%s frame=%s "
                        "tool_use_id=%s error=%s",
                        session.session_id,
                        frame.id,
                        result.tool_use_id,
                        exc,
                    )
                else:
                    frame.map_evidence.append(
                        {
                            "tool_use_id": result.tool_use_id,
                            "kind": "viewport_screenshot",
                            "target_path": screenshot_target,
                            "map_revision": screenshot_revision,
                            "region": evidence.metadata.get("region", {}),
                            "artifact_refs": [evidence.artifact_ref],
                            "evidence_id": evidence.evidence_id,
                            "contract_id": evidence.contract_id,
                        }
                    )
            if result.status == "error" and tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                result_error = result.result if isinstance(result.result, dict) else {}
                error_code = str(
                    result.error_code or result_error.get("error_code") or "map_tool_error"
                )
                error_message = str(
                    result_error.get("message") or payload.get("message", "") or error_code
                )
                remember_map_tool_failure(
                    session,
                    tool_name,
                    tool_args,
                    error_code,
                    error_message,
                )
            _remember_map_batch_result(
                session,
                tool_name,
                result.status,
                tool_args,
                result_for_gate,
            )
            if tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                transaction_status = (
                    str(result_for_gate.get("map_transaction_status", ""))
                    if isinstance(result_for_gate, dict)
                    else ""
                )
                if transaction_status == "committed":
                    self._emit(
                        session.session_id,
                        "write_committed",
                        {
                            "tool": tool_name,
                            "target_path": tool_args.get("target_path"),
                            "map_revision": (
                                result_for_gate.get("map_revision")
                                if isinstance(result_for_gate, dict)
                                else None
                            ),
                            "approval_id": tool_args.get("approval_id"),
                            "snapshot_id": tool_args.get("approval_snapshot_id"),
                        },
                    )
                elif result.status in {"error", "rejected"} or transaction_status in {
                    "failed",
                    "rolled_back",
                }:
                    self._emit(
                        session.session_id,
                        "map_edit_incomplete",
                        {
                            "tool": tool_name,
                            "target_path": tool_args.get("target_path"),
                            "error_code": result.error_code,
                            "map_transaction_status": transaction_status,
                        },
                    )
            if tool_name in MAP_REVISION_GUARDED_TOOL_NAMES and "plan_version" in tool_args:
                batch_entry = (
                    session.map_task_state.executed_batches[-1]
                    if session.map_task_state.executed_batches
                    else {}
                )
                self._emit(
                    session.session_id,
                    "map_batch_result",
                    {
                        "plan_version": tool_args.get("plan_version"),
                        "batch_index": tool_args.get("batch_index"),
                        "write_batch_id": tool_args.get("write_batch_id"),
                        "map_transaction_id": tool_args.get("map_transaction_id"),
                        "map_transaction_status": (
                            result_for_gate.get("map_transaction_status")
                            if isinstance(result_for_gate, dict)
                            else None
                        ),
                        "postconditions_passed": batch_entry.get("postconditions_passed", False),
                        "remaining_batches": len(session.map_task_state.pending_batches),
                    },
                )
            if isinstance(result_for_gate, dict) and "workflow_constraints" in tool_args:
                result_for_gate.setdefault(
                    "workflow_constraints", tool_args["workflow_constraints"]
                )
            result_target = (
                str(result_for_gate.get("target_path", ""))
                if isinstance(result_for_gate, dict)
                else ""
            )
            trusted_error_revision = (
                result.status == "error"
                and str(result.error_code) == "map_revision_conflict"
                and bool(str(tool_args.get("target_path", "")).strip())
                and result_target == str(tool_args.get("target_path", "")).strip()
            )
            if result.status == "applied" or trusted_error_revision:
                _remember_latest_map_revision(session, tool_name, tool_args, result_for_gate)
            if result.status == "applied" and tool_name in MAP_REVISION_GUARDED_TOOL_NAMES:
                session.map_request_scope = mark_completion_candidate(
                    session.map_request_scope,
                    lineage_id=str(metadata.get("request_lineage_id", "")),
                    map_task_id=str(metadata.get("map_task_id", "")),
                )
                if session.map_request_scope.completion_candidate:
                    session.map_task_lineage = {
                        **session.map_task_lineage,
                        "task_id": session.map_request_scope.map_task_id,
                        "lineage_id": session.map_request_scope.lineage_id,
                        "origin_request_id": (
                            session.map_task_lineage.get("origin_request_id")
                            or session.map_request_scope.request_id
                        ),
                        "completion_candidate": True,
                    }
            if isinstance(result_for_gate, dict):
                if result.status == "applied":
                    remember_planning_snapshot_evidence(
                        session,
                        tool_name,
                        tool_args,
                        result_for_gate,
                        self._settings.project_root,
                        (
                            map_artifact_locator.as_dict()
                            if map_artifact_locator is not None
                            else None
                        ),
                    )
                plan_progress = remember_map_plan_progress(
                    session,
                    tool_name,
                    tool_args,
                    result_for_gate,
                    self._settings.project_root,
                )
                if frame.agent.map_stage == "planner" and tool_name in PLATFORM_PLAN_TOOL_NAMES:
                    plan_outcome = parse_map_plan_outcome(tool_name, result_for_gate)
                    attempt_count = map_platform_plan_attempt_count(
                        session,
                        tool_args,
                        tool_name,
                    )
                    retry_exhausted = (
                        bool(plan_progress.get("exhausted"))
                        if isinstance(plan_progress, dict)
                        else False
                    )
                    if plan_outcome.executable or retry_exhausted:
                        frame.forced_completion_text = _planner_completion_text(
                            frame,
                            tool_name,
                            tool_args,
                            result_for_gate,
                        )
                        logger.info(
                            "Scheduled deterministic planner completion session=%s frame=%s "
                            "tool=%s executable=%s attempts=%d retry_exhausted=%s",
                            session.session_id,
                            frame.id,
                            tool_name,
                            plan_outcome.executable,
                            attempt_count,
                            retry_exhausted,
                        )
                        publication = result_for_gate.get("_planning_publication", {})
                        if isinstance(publication, dict):
                            event_name = (
                                "execution_approved"
                                if publication.get("execution_status") == "approved"
                                else "execution_blocked"
                            )
                            self._emit(session.session_id, "planning_delivered", publication)
                            self._emit(session.session_id, event_name, publication)
                elif (
                    _is_dynamic_map_writer(frame)
                    and tool_name in PLATFORM_PLAN_TOOL_NAMES
                    and not parse_map_plan_outcome(tool_name, result_for_gate).executable
                ):
                    frame.forced_completion_text = _writer_platform_validation_failure_text(
                        frame,
                        tool_name,
                        tool_args,
                        result_for_gate,
                    )
                    logger.info(
                        "Scheduled deterministic writer stop after platform validation "
                        "failure session=%s frame=%s tool=%s",
                        session.session_id,
                        frame.id,
                        tool_name,
                    )
                remember_validation_cache(
                    session,
                    tool_name,
                    tool_args,
                    result_for_gate,
                )
                if tool_name == "describe_map_region" and result.status == "applied":
                    increment_map_counter(session.map_task_state, "reads")
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    streaks = dict(session.map_task_state.no_progress_streaks)
                    streaks[target] = 0
                    replace_map_state_field(
                        session.map_task_state,
                        "no_progress_streaks",
                        streaks,
                        target=target,
                    )
                if (
                    tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                    and result.status == "applied"
                    and "plan_version" not in tool_args
                ):
                    increment_map_counter(session.map_task_state, "writes")
                    session.map_task_state.transition_stage("validate")
                if session.latest_context_used_tokens >= 32_000 and tool_name in {
                    "plan_map_layout",
                    "plan_map_algorithms",
                    "validate_platform_level_plan",
                    "plan_reachable_map_growth",
                    "validate_map_region",
                    *MAP_REVISION_GUARDED_TOOL_NAMES,
                }:
                    session.force_compact_next_turn = True
            if tool_name == "describe_map_region":
                _remember_latest_map_region_read(session, tool_args, result_for_gate)
                _abort_pending_map_region_read_on_size_error(
                    session,
                    tool_args,
                    result.error_code,
                    result_for_gate,
                )
                if (
                    frame.agent.map_stage == "reader"
                    and result.status == "applied"
                    and isinstance(result_for_gate, dict)
                    and _map_reader_has_detailed_region(result_for_gate)
                ):
                    frame.map_reader_detailed_region_ready = True
                    logger.info(
                        "Map reader detailed region ready session=%s frame=%s artifact=%s",
                        session.session_id,
                        frame.id,
                        map_artifact_locator is not None,
                    )
            blocker = _map_completion_blocker(
                tool_name, result.status, result_for_gate, result.error_code
            )
            if blocker is not None:
                map_layer = tool_args.get("map_layer")
                if isinstance(map_layer, int) and not isinstance(map_layer, bool):
                    blocker["map_layer"] = map_layer
            if tool_name in MAP_VALIDATION_TOOL_NAMES and isinstance(result_for_gate, dict):
                validation_state = _remember_map_validation(
                    session, tool_name, result_for_gate, tool_args
                )
                validation_success = _map_validation_is_successful(validation_state)
                mode = validation_mode(tool_args)
                remember_validation_progress(
                    session,
                    tool_name,
                    tool_args,
                    validation_state,
                    validation_success,
                )
                _remember_map_transaction_validation(
                    session,
                    tool_args,
                    validation_state,
                    validation_success,
                )
                if tool_name == "validate_map_region" and mode == "diagnostic":
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        [
                            {
                                "tool": tool_name,
                                "reason": "map_diagnostic_complete",
                                "issues": validation_state.get("issues", [])
                                or ["diagnostic finished; planner must produce a changed map plan"],
                                "target": validation_state["target"],
                                "required_revision": validation_state["map_revision"],
                                "next_stage": "planner",
                            }
                        ],
                        target=str(validation_state["target"]),
                        revision=validation_state["map_revision"],
                    )
                elif validation_success:
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    revision = result_for_gate.get("map_revision")
                    revision_value = (
                        revision
                        if isinstance(revision, int) and not isinstance(revision, bool)
                        else None
                    )
                    blockers = _clear_validation_blockers(
                        session.map_task_state.completion_blockers,
                        target,
                        revision_value,
                        tool_name,
                        tool_args,
                    )
                    if not _has_review_blocker(
                        blockers,
                        target,
                        revision_value,
                    ):
                        blockers.append(
                            _review_required_blocker(
                                tool_name,
                                target,
                                revision_value,
                                _map_region_from_write_args(tool_args, result_for_gate),
                            )
                        )
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        blockers,
                        target=target,
                        revision=revision_value,
                    )
                else:
                    validation_blocker = blocker or {
                        "tool": tool_name,
                        "reason": "validator_failed",
                        "issues": ["map validation did not pass"],
                        "target": validation_state["target"],
                        "required_revision": validation_state["map_revision"],
                    }
                    validation_blocker["validation_fingerprint"] = validation_state["fingerprint"]
                    validation_blocker["repeat_count"] = validation_state["repeat_count"]
                    validation_blocker["next_stage"] = "diagnostic"
                    if validation_state["repeat_count"] >= _MAP_VALIDATION_REPEAT_LIMIT:
                        validation_blocker["reason"] = "map_validation_repeat_limit"
                        validation_blocker["next_stage"] = "planner"
                        validation_blocker["issues"] = [
                            *validation_blocker.get("issues", []),
                            "same validation failure repeated without a new map revision; automatic retry stopped",
                        ]
                    replace_map_state_field(
                        session.map_task_state,
                        "completion_blockers",
                        [validation_blocker],
                        target=str(validation_state["target"]),
                        revision=validation_state["map_revision"],
                    )
            elif blocker is not None:
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    [blocker],
                    target=str(blocker.get("target", "")) or None,
                    revision=blocker.get("required_revision"),
                )
            history_payload = (
                _history_payload_for_front_tool(
                    tool_name,
                    payload,
                    (
                        map_artifact_locator.artifact_ref
                        if map_artifact_locator is not None
                        else None
                    ),
                    (map_artifact_locator.as_dict() if map_artifact_locator is not None else None),
                    frozenset(frame.agent.effective_tools),
                )
                if isinstance(payload, dict)
                else payload
            )
            frame.messages.append(
                _tool_message(result.tool_use_id, history_payload, is_error=is_error)
            )
            if (
                frame.agent.map_stage == "reader"
                and tool_name == "describe_map_region"
                and frame.map_reader_detailed_region_ready
                and map_artifact_locator is None
            ):
                _arm_map_reader_text_completion(
                    frame,
                    mode=self._settings.map_worker_response_contract_mode,
                    correction_limit=(
                        self._settings.map_worker_structured_correction_limit
                        if self._settings.map_worker_structured_output_enabled
                        else 0
                    ),
                )
            if isinstance(result_for_gate, dict):
                _append_platform_planning_failure_hint(session, tool_name, result_for_gate)
            if (
                tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                and str(result.error_code) == "map_revision_conflict"
                and trusted_error_revision
            ):
                await _schedule_revision_conflict_reader(
                    session,
                    frame,
                    tool_name,
                    tool_args,
                    result_for_gate,
                    prompt_factory,
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
                frame.messages.append({"role": "user", "content": hint})
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
        """对本轮所有命中校验条件的编辑结果运行 VerifyRunner。"""
        await self._verify_runner.run(session, security, candidates, model_override)

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

    async def reset(self, session_id: str) -> ResetResponse:
        """清空指定会话。

        先取消该会话仍在运行的 `/chat` 任务并等待其退出，再在持锁状态下清空
        会话；否则旧 turn 返回后的 `save()` 会把已重置的会话重新写回磁盘。
        """
        await self._cancel_active_tasks(session_id)
        async with self._store.lock_for(session_id):
            old_session = self._store.get_or_create(session_id, self.available_tools)
            event_highwater = max(
                old_session.history_event_counter,
                self._events.last_seq(session_id) if self._events is not None else 0,
            )
            try:
                record = self._store.begin_reset(
                    session_id,
                    last_event_seq=event_highwater,
                )
            except (OSError, TypeError, ValueError) as exc:
                logger.exception("Session reset epoch barrier failed session=%s", session_id)
                return ResetResponse(
                    ok=False,
                    session_id=session_id,
                    session_epoch=old_session.session_epoch,
                    last_event_seq=event_highwater,
                    error_code="reset_epoch_barrier_failed",
                    text=f"无法建立会话重置隔离屏障：{exc}",
                )
            try:
                last_seq = self._complete_reset_cleanup(record)
            except (OSError, TypeError, ValueError):
                logger.exception(
                    "Session reset cleanup pending session=%s reset_id=%s",
                    session_id,
                    record.get("reset_id"),
                )
                current_epoch = str(record["new_epoch"])
                last_seq = (
                    self._events.last_seq(session_id)
                    if self._events is not None
                    else event_highwater
                )
                return ResetResponse(
                    ok=True,
                    session_id=session_id,
                    session_epoch=current_epoch,
                    last_event_seq=last_seq,
                    cleanup_pending=True,
                    text="会话已隔离；后台清理将在重启或下次重试时继续",
                )
        logger.info(
            "Session reset through QueryEngine session=%s epoch=%s last_seq=%d",
            session_id,
            record["new_epoch"],
            last_seq,
        )
        return ResetResponse(
            ok=True,
            session_id=session_id,
            session_epoch=str(record["new_epoch"]),
            last_event_seq=last_seq,
        )

    async def interrupt(
        self,
        session_id: str,
        *,
        cause: InterruptCause = "user_interrupted",
    ) -> InterruptResponse:
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
        Args:
            session_id: 需要中断的会话标识。
            cause: 用户主动停止或客户端等待超时。

        Returns:
            取消结果与最新事件游标。
        """
        cancelled = await self._cancel_active_tasks(session_id)

        discarded = 0
        map_checkpoint_created = False
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            if session.map_task_state.status == "running":
                session.map_task_state.make_checkpoint(
                    cause,
                    pause_kind=cause,
                )
                map_checkpoint_created = True
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
                            tool_use_id,
                            (
                                "客户端等待超时并中断了当前请求，该工具调用结果未回传。"
                                if cause == "client_timeout"
                                else "用户中断了当前请求，该工具调用结果未回传。"
                            ),
                            is_error=True,
                        )
                    )
                    discarded += 1
                session.clear_pending()
                self._store.save(session)
                if self._recovery is not None and not map_checkpoint_created:
                    self._recovery.clear(session_id)
            elif had_pending_plan or map_checkpoint_created:
                self._store.save(session)
            if isinstance(session.task_run, dict):
                try:
                    problem = self._recovery_supervisor.problem(
                        session,
                        error_code=(
                            "response_transport_lost" if cause == "client_timeout" else "user_stop"
                        ),
                        text=(
                            "客户端连接中断；任务检查点已保留"
                            if cause == "client_timeout"
                            else "用户已停止当前执行；任务检查点已保留，可显式恢复"
                        ),
                        side_effect_state="none",
                    )
                    session.task_run["last_problem"] = problem
                    self._store.save_task_run(session)
                except ValueError:
                    logger.exception(
                        "Unable to record interrupted TaskRun session=%s",
                        session_id,
                    )

        self._emit(
            session_id,
            "turn_interrupted",
            {
                "cancelled": cancelled,
                "pending_discarded": discarded,
                "cause": cause,
            },
        )
        last_seq = self._events.last_seq(session_id) if self._events is not None else 0
        if self._recovery is not None and map_checkpoint_created:
            self._recovery.write(
                session_id,
                None,
                last_seq,
                session.map_task_state.checkpoint,
                session_epoch=session.session_epoch,
            )
        logger.info(
            "Turn interrupted session=%s cause=%s cancelled=%s pending_discarded=%d last_seq=%d",
            session_id,
            cause,
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
                return ChatErrorResponse(
                    text="当前会话没有等待回传的工具调用",
                    error_code="pending_tool_results",
                    disposition="wait_frontend",
                    retryable=True,
                )

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

    async def resume_paused_map_task(self, session_id: str) -> dict[str, Any]:
        """显式恢复暂停的地图任务，但不在命令请求内静默执行写批次。

        仅在任务处于 ``paused`` 状态且无 pending 前端工具调用时允许恢复。
        恢复操作本身只修改状态并持久化，实际的批次执行由下一轮 chat 请求
        驱动（通过一次性 resume authorization 路径），避免在此处隐式触发
        长时间运行的写操作。

        Args:
            session_id: 需要恢复的会话标识。

        Returns:
            恢复后的地图任务状态和检查点摘要。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            state = session.map_task_state
            # 前置条件：任务必须处于暂停状态且有可用检查点
            if state.status != "paused" or state.checkpoint is None:
                return {
                    "resumed": False,
                    "status": state.status,
                    "reason": "map_task_not_paused",
                }
            # 前置条件：不能有尚未回传的前端工具调用
            if session.pending_turn_id is not None:
                return {
                    "resumed": False,
                    "status": state.status,
                    "reason": "front_tools_still_pending",
                }
            task_lineage = session.map_task_lineage
            lineage_id = (
                str(task_lineage.get("lineage_id", ""))
                or str(session.map_request_scope.lineage_id)
                or state.task_id
            )
            resume_map_task(state, lineage_id=lineage_id)
            if isinstance(session.task_run, dict):
                session.task_run["status"] = "recovering"
                session.task_run["active_disposition"] = "retry_new_attempt"
                session.task_run["next_action"] = {
                    "action": "resume_from_checkpoint",
                    "owner": "backend",
                }
                session.task_run["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._store.save_task_run(session)
            self._store.save(session)
            result = {
                "resumed": True,
                "status": state.status,
                "task_id": state.task_id,
                "checkpoint": state.checkpoint,
            }
        self._emit(session_id, "map_task_resumed", {"task_id": result["task_id"]})
        logger.info("Paused map task explicitly resumed session=%s", session_id)
        return result

    async def cancel_map_task(self, session_id: str) -> dict[str, Any]:
        """显式取消地图任务并清除尚未执行的地图批次。

        适用于用户主动中止正在运行或暂停的地图任务。仅当任务处于
        ``running`` 或 ``paused`` 状态时才允许取消，且要求当前无 pending
        前端工具调用，防止取消操作与正在处理的工具结果产生竞态。

        Args:
            session_id: 需要取消地图任务的会话标识。

        Returns:
            是否取消成功及取消后的任务状态。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            state = session.map_task_state
            # 前置条件：只有运行中或暂停中的任务才能被取消
            if state.status not in {"running", "paused"}:
                return {
                    "cancelled": False,
                    "status": state.status,
                    "reason": "map_task_not_active",
                }
            # 前置条件：不能有尚未回传的前端工具调用
            if session.pending_turn_id is not None:
                return {
                    "cancelled": False,
                    "status": state.status,
                    "reason": "front_tools_still_pending",
                }
            task_id = state.task_id
            state.cancel("cancelled_by_user")
            session.map_request_scope = invalidate_completion_candidate(session.map_request_scope)
            session.map_task_lineage = {
                **session.map_task_lineage,
                "completion_candidate": False,
            }
            if isinstance(session.task_run, dict):
                self._recovery_supervisor.mark_terminal(
                    session,
                    outcome="cancelled",
                    authorized_by="explicit_cancel",
                )
                self._store.save_task_run(session)
            self._store.save(session)
            result = {
                "cancelled": True,
                "status": state.status,
                "task_id": task_id,
            }
        self._emit(session_id, "map_task_cancelled", {"task_id": task_id})
        logger.info("Map task explicitly cancelled session=%s task_id=%s", session_id, task_id)
        return result

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
        """判断当前会话是否需要自动压缩。"""
        return self._compactor.needs_auto_compact(session)

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
            return await self._compact_locked_async(session_id, keep_recent, triggered_by, use_llm)

    def _compact_locked(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """同步兼容入口；异步路径请调用 `_compact_locked_async`。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            effective_use_llm = False if use_llm is None else use_llm
            return asyncio.run(
                self._compact_locked_async(session_id, keep_recent, triggered_by, effective_use_llm)
            )
        raise RuntimeError("_compact_locked() cannot run inside an active event loop")

    async def _compact_locked_async(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """在已持有会话锁时执行压缩；不要在未持锁路径直接调用。"""
        return await self._compactor.compact_locked(session_id, keep_recent, triggered_by, use_llm)

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
        publication_buffer = _PUBLICATION_BUFFER.get()
        if publication_buffer is not None:
            staged_payload = dict(payload)
            staged_payload.setdefault(
                "_submission_request_id",
                publication_buffer.request_id,
            )
            staged_payload.setdefault(
                "_submission_turn_id",
                publication_buffer.turn_id,
            )
            staged_payload.setdefault("request_id", publication_buffer.request_id)
            staged_payload.setdefault("turn_id", publication_buffer.turn_id)
            delivery = _submission_event_delivery(event_type)
            if delivery == "transactional":
                staged_payload.setdefault("delivery", delivery)
            if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
                self._record_persisted_history_event(
                    publication_buffer.session,
                    event_type,
                    staged_payload,
                )
            if _submission_event_delivery(event_type) == "provisional_preview":
                frame_id = str(staged_payload.get("frame_id") or "")
                message_index = str(staged_payload.get("message_index") or "")
                message_id = str(staged_payload.get("message_id") or f"{frame_id}:{message_index}")
                preview_id = str(
                    staged_payload.get("preview_id")
                    or (
                        f"{publication_buffer.request_id or publication_buffer.turn_id}:"
                        f"{publication_buffer.turn_id}:{event_type}:{message_id}"
                    )
                )
                staged_payload.update(
                    {
                        "delivery": "provisional_preview",
                        "provisional": True,
                        "preview_id": preview_id,
                        "message_id": message_id,
                    }
                )
                publication_buffer.previews.setdefault(
                    preview_id,
                    {
                        "preview_id": preview_id,
                        "event_type": event_type,
                        "frame_id": frame_id,
                        "message_id": message_id,
                    },
                )
                if self._events is None:
                    return 0
                event = self._events.append(
                    session_id,
                    event_type,
                    staged_payload,
                    session_epoch=publication_buffer.session.session_epoch,
                )
                publication_buffer.preview_event_count += 1
                if publication_buffer.first_preview_seq == 0:
                    publication_buffer.first_preview_seq = event.seq
                    logger.info(
                        "First submission preview published session=%s request_id=%s "
                        "turn_id=%s preview_id=%s seq=%d provider_first_chunk=%s",
                        session_id,
                        publication_buffer.request_id,
                        publication_buffer.turn_id,
                        preview_id,
                        event.seq,
                        bool(staged_payload.get("provider_first_chunk", False)),
                    )
                else:
                    logger.debug(
                        "Submission preview fragment published session=%s request_id=%s "
                        "turn_id=%s preview_id=%s seq=%d fragment=%d",
                        session_id,
                        publication_buffer.request_id,
                        publication_buffer.turn_id,
                        preview_id,
                        event.seq,
                        publication_buffer.preview_event_count,
                    )
                return event.seq
            publication_buffer.events.append((session_id, event_type, staged_payload))
            return 0
        log_payload = _event_payload_for_log(payload)
        logger.debug(
            "Event emitted session=%s type=%s payload=%s",
            session_id,
            event_type,
            json.dumps(log_payload, ensure_ascii=False, default=str),
        )
        session: Session | None = None
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            session = self._store.get_or_create(session_id, self.available_tools)
            self._record_persisted_history_event(session, event_type, payload)
        if self._events is None:
            return 0
        epoch = (
            session.session_epoch
            if session is not None
            else self._store.current_epoch(session_id, create=False)
        )
        event = self._events.append(
            session_id,
            event_type,
            payload,
            session_epoch=epoch,
        )
        logger.debug("Event persisted session=%s seq=%d type=%s", session_id, event.seq, event_type)
        return event.seq

    def _resolve_submission_previews(
        self,
        publication_buffer: _SubmissionPublicationBuffer,
        *,
        committed: bool,
        reason: str | None = None,
    ) -> None:
        """Resolve already-visible previews exactly once without publishing staged facts."""
        if publication_buffer.preview_resolved or not publication_buffer.previews:
            return
        publication_buffer.preview_resolved = True
        event_type = "submission_preview_committed" if committed else "submission_preview_discarded"
        payload: dict[str, Any] = {
            "delivery": "provisional_preview",
            "provisional": False,
            "request_id": publication_buffer.request_id,
            "turn_id": publication_buffer.turn_id,
            "preview_ids": list(publication_buffer.previews),
            "previews": list(publication_buffer.previews.values()),
        }
        if reason is not None:
            payload["reason"] = reason
        if self._events is not None:
            event = self._events.append(
                publication_buffer.session.session_id,
                event_type,
                payload,
                session_epoch=publication_buffer.session.session_epoch,
            )
            seq = event.seq
        else:
            seq = 0
        logger.info(
            "Submission previews resolved session=%s request_id=%s turn_id=%s "
            "resolution=%s preview_streams=%d preview_events=%d "
            "first_preview_seq=%d boundary_seq=%d reason=%s",
            publication_buffer.session.session_id,
            publication_buffer.request_id,
            publication_buffer.turn_id,
            "committed" if committed else "discarded",
            len(publication_buffer.previews),
            publication_buffer.preview_event_count,
            publication_buffer.first_preview_seq,
            seq,
            reason,
        )

    def _record_persisted_history_event(
        self,
        session: Session,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """把需恢复的事件写入指定 Session，不触碰外部 EventStore。"""
        if event_type == "context_usage":
            try:
                used_tokens = int(payload.get("used_tokens", 0))
            except (TypeError, ValueError):
                used_tokens = 0
            if used_tokens > 0:
                session.latest_context_used_tokens = used_tokens
                frame = session.top_frame()
                is_map_frame = frame is not None and (
                    frame.agent.pipeline_kind == "map" or bool(frame.agent.workflow_operations)
                )
                threshold = (
                    min(
                        self._settings.auto_compact_token_threshold,
                        _MAP_AUTO_COMPACT_CONTEXT_TOKENS,
                    )
                    if is_map_frame
                    else self._settings.auto_compact_token_threshold
                )
                if used_tokens >= threshold:
                    session.force_compact_next_turn = True
        session.record_history_event(event_type, payload)

    def _flush_submission_publications(
        self,
        publication_buffer: _SubmissionPublicationBuffer,
    ) -> None:
        """在 Session 成功提交后按 artifact、事件顺序发布暂存副作用。"""
        if self._events is None:
            return
        for event_index, (session_id, event_type, payload) in enumerate(publication_buffer.events):
            try:
                payload.setdefault(
                    "_delivery_id",
                    hashlib.sha256(
                        (
                            f"{session_id}\0"
                            f"{publication_buffer.session.session_epoch}\0"
                            f"{publication_buffer.request_id or ''}\0"
                            f"{publication_buffer.turn_id}\0"
                            f"{event_index}\0{event_type}"
                        ).encode()
                    ).hexdigest(),
                )
                self._recovery_supervisor.hit_failpoint("event_delivery_before_publish")
                event = self._events.append(
                    session_id,
                    event_type,
                    payload,
                    session_epoch=publication_buffer.session.session_epoch,
                )
                self._recovery_supervisor.hit_failpoint("event_delivery_after_publish")
            except (OSError, ValueError) as exc:
                self._recovery_supervisor.record_transport_loss(
                    publication_buffer.session,
                    transport="event",
                )
                try:
                    self._store.save_task_run(publication_buffer.session)
                except (OSError, TypeError, ValueError):
                    logger.exception(
                        "Failed to persist event delivery transport state session=%s",
                        session_id,
                    )
                logger.error(
                    "Committed session event publication failed session=%s " "type=%s error=%s",
                    session_id,
                    event_type,
                    exc,
                )
                continue
            logger.debug(
                "Deferred event persisted session=%s seq=%d type=%s",
                session_id,
                event.seq,
                event_type,
            )

    def _record_recovery(self, session: Session, response: ChatResponse) -> None:
        """根据最新响应写入或清理最小恢复指针。"""
        if self._recovery is None:
            return
        if session.map_task_state.status == "paused":
            last_seq = self._events.last_seq(session.session_id) if self._events is not None else 0
            self._recovery.write(
                session.session_id,
                session.pending_turn_id,
                last_seq,
                session.map_task_state.checkpoint,
                session_epoch=session.session_epoch,
            )
            return
        if isinstance(response, ChatToolCallsResponse):
            last_seq = self._events.last_seq(session.session_id) if self._events is not None else 0
            self._recovery.write(
                session_id=session.session_id,
                pending_turn_id=response.turn_id,
                last_event_seq=last_seq,
                session_epoch=session.session_epoch,
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
