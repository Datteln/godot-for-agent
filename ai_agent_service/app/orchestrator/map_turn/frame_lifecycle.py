"""处理 Map Frame 完成与预算耗尽转换。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from app.agents.types import Frame
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_recovery import (
    STRUCTURED_REPAIR_MAX_ATTEMPTS,
    record_semantic_retry,
    retry_pause_report,
    safe_structured_diagnostic,
    structured_error_category,
    structured_repair_actions,
)
from app.orchestrator.map_turn.contracts import (
    AgentPromptFactory,
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.delegation_continuation import (
    _continue_delegate_group,
    _map_delegate_result_payload,
)
from app.orchestrator.map_turn.events import _emit_orchestration_event
from app.orchestrator.map_turn.frame_info import (
    _frame_semantic_operation,
    _map_frame_exhausted_payload,
    _map_output_schema_for_frame,
    _map_stage_for_frame,
)
from app.orchestrator.map_turn.planning import _plan_step_completed
from app.orchestrator.map_turn.structured_completion import (
    _apply_map_structured_completion_result,
    _json_object_from_text,
    _json_parse_offset,
    _map_structured_output_error,
    _repair_map_structured_output,
)
from app.orchestrator.map_turn.structured_contracts import MAP_OUTPUT_SCHEMA_V1
from app.orchestrator.map_workflow import replace_map_state_field
from app.orchestrator.turn.contracts import (
    ErrorTurnOutcome,
    FinalTurnOutcome,
)
from app.sessions.store import Session


async def _finish_frame(
    session: Session,
    text: str,
    prompt_factory: AgentPromptFactory | None = None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    artifact_store: DelegateArtifactStore | None = None,
) -> FinalTurnOutcome | None:
    """处理当前帧产出最终文本（无 `tool_calls`）的情况（§13.1）。

    根帧（`agent_stack` 长度为 1）保留在栈中以维持多轮会话历史，直接
    返回 `FinalTurnOutcome`；由 `delegate` 创建的子帧（M2+）结束时则弹栈，
    把摘要回填父帧那条 `delegate` 的工具结果，交由调用方继续驱动父帧。

    Args:
        session: 当前会话。
        text: 当前帧本轮产出的最终文本。
        prompt_factory: 子 agent 系统提示词构造函数。
        event_callback: 编排事件回调，供 `create_plan` 步骤进度事件使用。

    Returns:
        根帧结束时返回 `FinalTurnOutcome`；子帧结束时返回 None，调用方应
        继续循环（此时 `session.top_frame()` 已是父帧）。
    """
    frame = session.top_frame()
    if frame is not None:
        structured_error = _map_structured_output_error(session, frame, text)
        if structured_error is not None:
            category = structured_error_category(structured_error)
            raw_digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
            safe_diagnostic = {
                **safe_structured_diagnostic(structured_error),
                "schema_version": _map_output_schema_for_frame(frame),
                "response_mode": frame.response_contract_mode or "none",
                "model": frame.structured_response_model or "unknown",
                "temperature": 0.0,
                "thinking_budget": frame.structured_thinking_budget,
                "tools_enabled": False,
                "finish_reason": frame.structured_finish_reason or "unknown",
                "raw_chars": len(text),
                "raw_digest": raw_digest,
                "local_attempt": frame.structured_attempt_count + 1,
            }
            parse_offset = _json_parse_offset(text)
            if parse_offset is not None:
                safe_diagnostic["parse_offset"] = parse_offset
            frame.structured_diagnostics.append(safe_diagnostic)
            if (
                frame.force_text_only
                and frame.structured_attempt_count < frame.structured_correction_limit
            ):
                frame.structured_attempt_count += 1
                immutable_constraints = {
                    key: value
                    for key, value in {
                        "contract_id": frame.contract_id,
                        "result_schema": frame.result_schema,
                        "stage": frame.map_stage_contract.get("stage"),
                        "worker": frame.worker_instance_id,
                        "target_path": frame.map_stage_contract.get("target_path"),
                        "map_revision": frame.map_stage_contract.get("map_revision"),
                        "allowed_next_stages": list(frame.allowed_next_stages),
                    }.items()
                    if value is not None and value != ""
                }
                frame.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Structured result correction required. "
                            f"schema={MAP_OUTPUT_SCHEMA_V1}; "
                            f"category={category}; "
                            "invalid_fields="
                            + json.dumps(
                                safe_diagnostic.get("fields", []),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "; frozen_constraints="
                            + json.dumps(
                                immutable_constraints,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + ". 只重新输出一个完整 JSON object；不得调用工具，"
                            "不得复述上一条原始输出。"
                        ),
                    }
                )
                logger.warning(
                    "Map structured output correction scheduled session=%s frame=%s "
                    "agent=%s category=%s schema=%s response_mode=%s "
                    "local_attempt=%d/%d raw_chars=%d raw_digest=%s",
                    session.session_id,
                    frame.id,
                    frame.agent.name,
                    category,
                    MAP_OUTPUT_SCHEMA_V1,
                    frame.response_contract_mode or "none",
                    frame.structured_attempt_count,
                    frame.structured_correction_limit,
                    len(text),
                    raw_digest,
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_structured_correction_scheduled",
                    {
                        "frame_id": frame.id,
                        "schema_version": MAP_OUTPUT_SCHEMA_V1,
                        "response_mode": frame.response_contract_mode or "none",
                        "category": category,
                        "local_attempt": frame.structured_attempt_count,
                        "raw_chars": len(text),
                        "raw_digest": raw_digest,
                    },
                )
                return None
            source_payload = _json_object_from_text(text) or {}
            target = str(
                source_payload.get(
                    "target_path",
                    frame.map_stage_contract.get("target_path", ""),
                )
            )
            revision_value = source_payload.get(
                "map_revision",
                frame.map_stage_contract.get("map_revision", 0),
            )
            revision = (
                revision_value
                if isinstance(revision_value, int) and not isinstance(revision_value, bool)
                else 0
            )
            retry = record_semantic_retry(
                session.map_task_state,
                category="structured_output",
                error_category=category,
                root_cause=structured_error,
                stage=_map_stage_for_frame(frame),
                target=target,
                revision=revision,
                operation=_frame_semantic_operation(frame),
                threshold=STRUCTURED_REPAIR_MAX_ATTEMPTS,
            )
            # 本轮整改：日志新增 raw_chars + raw_digest，便于跨请求
            # 追踪同一次结构化输出拒绝/修复事件
            logger.warning(
                "Map structured output rejected session=%s frame=%s agent=%s "
                "category=%s raw_chars=%d raw_digest=%s local_attempt=%d",
                session.session_id,
                frame.id,
                frame.agent.name,
                category,
                len(text),
                raw_digest,
                frame.structured_attempt_count,
            )
            text = _repair_map_structured_output(
                frame,
                text,
                structured_error,
                category=category,
                attempt=int(retry["attempt"]),
                exhausted=bool(retry["exhausted"]),
            )
            logger.warning(
                "Map structured output repaired session=%s frame=%s agent=%s "
                "category=%s attempt=%d exhausted=%s actions=%s "
                "repaired_chars=%d repaired_digest=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
                category,
                retry["attempt"],
                retry["exhausted"],
                structured_repair_actions(category),
                len(text),
                hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
            )
            if bool(retry["exhausted"]):
                report = retry_pause_report(
                    session.map_task_state,
                    stage=_map_stage_for_frame(frame),
                    target=target,
                    revision=revision,
                    last_attempt=retry,
                )
                if session.map_task_state.status == "running":
                    session.map_task_state.make_checkpoint(
                        "structured_output_retry_exhausted",
                        report,
                        pause_kind="no_progress_exhausted",
                    )

        _apply_map_structured_completion_result(session, frame, text)
        if structured_error is None and frame.structured_attempt_count:
            logger.info(
                "Map structured output corrected session=%s frame=%s schema=%s "
                "response_mode=%s local_attempts=%d",
                session.session_id,
                frame.id,
                MAP_OUTPUT_SCHEMA_V1,
                frame.response_contract_mode or "none",
                frame.structured_attempt_count,
            )
            _emit_orchestration_event(
                event_callback,
                "map_structured_correction_succeeded",
                {
                    "frame_id": frame.id,
                    "schema_version": MAP_OUTPUT_SCHEMA_V1,
                    "response_mode": frame.response_contract_mode or "none",
                    "local_attempts": frame.structured_attempt_count,
                },
            )

    if len(session.agent_stack) <= 1:
        logger.info("Root frame finished session=%s text_length=%d", session.session_id, len(text))
        if session.pending_plan is not None:
            session.pending_plan = None
        return FinalTurnOutcome(text=text)
    done = session.agent_stack.pop()
    # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
    if done.agent.map_stage == "reader":
        context_state = dict(session.map_task_state.context_state)
        context_state.pop("reader_exhausted", None)
        replace_map_state_field(
            session.map_task_state,
            "context_state",
            context_state,
        )
    logger.info(
        "Child frame finished session=%s frame=%s agent=%s text_length=%d",
        session.session_id,
        done.id,
        done.agent.name,
        len(text),
    )
    if done.pending_delegate_group_id is not None:
        await _continue_delegate_group(
            session,
            done,
            text,
            prompt_factory,
            event_callback,
            artifact_store,
        )
        return None
    parent = session.top_frame()
    assert parent is not None
    if done.pending_delegate_call_id is not None:
        delegate_result = _map_delegate_result_payload(done, text, artifact_store)
        _plan_step_completed(session, done, delegate_result, event_callback)
        parent.messages.append(
            _tool_message(
                done.pending_delegate_call_id,
                delegate_result,
            )
        )
    elif done.parent_id is not None:
        parent.messages.append(
            {
                "role": "user",
                "content": (
                    "自动子阶段结果："
                    + json.dumps(
                        _map_delegate_result_payload(done, text, artifact_store),
                        ensure_ascii=False,
                    )
                ),
            }
        )
    return None


async def _handle_frame_turns_exhausted(
    session: Session,
    frame: Frame,
    limit_label: str,
    limit: int,
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    artifact_store: DelegateArtifactStore | None = None,
) -> ErrorTurnOutcome | None:
    """某个轮次预算（总轮数/edit_map 轮数/常规轮数）耗尽时的统一收尾。

    根帧耗尽时整轮直接报错终止；子帧耗尽时用 `_finish_frame` 收尾并把控制权
    交还父帧，让父 agent 据此判断是否要重新拆分任务。

    Returns:
        根帧耗尽时返回 `ErrorTurnOutcome`（调用方应立即 `return`）；子帧耗尽时返回
        `None`（`_finish_frame` 已处理收尾，调用方应 `continue` 外层循环）。
    """
    if len(session.agent_stack) <= 1:
        logger.warning(
            "Agent TurnDriver.run reached root frame turns limit session=%s agent=%s limit=%s=%d",
            session.session_id,
            frame.agent.name,
            limit_label,
            limit,
        )
        return ErrorTurnOutcome(
            text="已达到本轮最大循环次数，请精简任务或拆分请求后重试",
            error_code="agent_turn_budget_exhausted",
        )
    logger.warning(
        "Delegate frame reached its turns limit session=%s frame=%s agent=%s limit=%s=%d",
        session.session_id,
        frame.id,
        frame.agent.name,
        limit_label,
        limit,
    )
    text = (
        _map_frame_exhausted_payload(frame, limit_label, limit)
        if _map_output_schema_for_frame(frame) == MAP_OUTPUT_SCHEMA_V1
        else (
            f"子 agent「{frame.agent.name}」已达到自身{limit_label}上限（{limit}），"
            "任务未完成，已强制收尾。以上为已执行步骤记录，请据此判断是否需要重新拆分任务或继续委派。"
        )
    )
    await _finish_frame(
        session,
        text,
        prompt_factory,
        event_callback,
        artifact_store,
    )
    # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
    if frame.agent.map_stage == "reader":
        context_state = dict(session.map_task_state.context_state)
        context_state["reader_exhausted"] = True
        replace_map_state_field(
            session.map_task_state,
            "context_state",
            context_state,
        )
    return None
