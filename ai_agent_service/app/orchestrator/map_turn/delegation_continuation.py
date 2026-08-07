"""处理 Map 委派结果归并与父级继续执行。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.types import Frame
from app.history_bounds import (
    summarize_history_text as _summarize_history_text,
)
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    record_semantic_retry,
    retry_pause_report,
)
from app.orchestrator.map_turn.contracts import (
    AgentPromptFactory,
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.delegation import _delegate_child_frame
from app.orchestrator.map_turn.frame_info import (
    _find_frame,
    _map_output_schema_for_frame,
)
from app.orchestrator.map_turn.planning import (
    _plan_step_completed,
    _plan_step_started,
    _with_plan_runtime_metadata,
)
from app.orchestrator.map_turn.structured_completion import (
    _json_object_from_text,
    _slim_map_delegate_value,
)
from app.orchestrator.map_turn.structured_contracts import MAP_OUTPUT_SCHEMA_V1
from app.orchestrator.plan_scheduler import PlanGraph, PlanGraphError
from app.orchestrator.runtime_contracts import PlanStepResult
from app.sessions.store import Session


def _map_delegate_result_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """提取父 Frame 推进阶段所需的最小地图结果摘要。

    本轮整改新增：当 artifact_store 成功落盘后，父帧只需保留摘要字段
    和列表计数（artifact_list_counts），完整数据通过 artifact_ref 回溯，
    大幅减少父帧 context 体积。
    """
    summary_fields = (
        "stage",
        "worker",
        "mode",
        "objective",
        "target_path",
        "map_layer",
        "map_revision",
        "region",
        "summary",
        "validation",
        "missing_inputs",
        "risks",
        "next_stage",
    )
    summary = {
        key: _slim_map_delegate_value(payload[key]) for key in summary_fields if key in payload
    }
    # 只保留列表长度，父帧据此判断是否需要回查 artifact
    summary["artifact_list_counts"] = {
        key: len(payload[key])
        for key in ("facts", "proposed_batches", "write_results")
        if isinstance(payload.get(key), list)
    }
    return summary


def _map_delegate_result_payload(
    done: Frame,
    text: str,
    artifact_store: DelegateArtifactStore | None = None,
) -> dict[str, Any]:
    """把地图子 worker 结果压缩为结构化载荷，避免向父帧透传完整自然语言历史。

    本轮整改：新增 artifact_store 参数。若落盘成功则只回传最小摘要
    （_map_delegate_result_summary）和 artifact_ref；失败时回退到
    preserve_lists=True 的完整瘦身载荷，保证父帧仍能拿到 proposed_batches
    等关键列表。
    """
    output_schema = _map_output_schema_for_frame(done)
    payload = _json_object_from_text(text)
    if payload is not None and output_schema == MAP_OUTPUT_SCHEMA_V1:
        # 尝试将完整结果写入 artifact store，换取一个可回溯引用
        artifact_ref: str | None = None
        if artifact_store is not None:
            try:
                artifact_ref = artifact_store.store(
                    frame_id=done.id,
                    agent_name=done.agent.name,
                    result_schema=str(output_schema),
                    result=payload,
                )
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "Delegate artifact store failed frame=%s agent=%s error=%s",
                    done.id,
                    done.agent.name,
                    exc,
                )
        # artifact_ref 成功 → 最小摘要；失败 → 保留列表的完整瘦身
        result_payload = (
            _map_delegate_result_summary(payload)
            if artifact_ref is not None
            else _slim_map_delegate_value(payload, preserve_lists=True)
        )
        return {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": _summarize_history_text(str(payload.get("summary", "")), 4000),
            "result": result_payload,
            "artifact_ref": artifact_ref,
        }
    if output_schema == MAP_OUTPUT_SCHEMA_V1:
        return {
            "agent": done.agent.name,
            "frame_id": done.id,
            "summary": "",
            "result": {
                "error": "invalid_map_worker_result",
                "message": "child output was not valid map_worker_result_v1 JSON",
            },
        }
    return {
        "agent": done.agent.name,
        "frame_id": done.id,
        "summary": _summarize_history_text(text),
    }


async def _continue_delegate_group(
    session: Session,
    done: Frame,
    text: str,
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    artifact_store: DelegateArtifactStore | None = None,
) -> None:
    """记录一个 `delegate_many` 子任务结果，并按需启动下一个子任务。"""
    assert done.pending_delegate_group_id is not None
    group = session.delegate_groups.get(done.pending_delegate_group_id)
    if group is None:
        logger.warning(
            "Delegate group missing session=%s group_id=%s frame=%s",
            session.session_id,
            done.pending_delegate_group_id,
            done.id,
        )
        return

    delegate_result = _map_delegate_result_payload(done, text, artifact_store)
    group.setdefault("results", []).append(delegate_result)
    _plan_step_completed(session, done, delegate_result, event_callback)
    child: Any = None

    if group.get("plan_driven") is True and session.pending_plan is not None:
        try:
            graph = PlanGraph.from_dict(session.pending_plan)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "Delegate plan continuation failed session=%s group=%s error=%s",
                session.session_id,
                done.pending_delegate_group_id,
                exc,
            )
            graph = None
        runnable = graph.runnable_steps() if graph is not None else ()
        if runnable:
            assert graph is not None
            next_step = runnable[0]
            try:
                next_task = graph.task_payload(next_step.step_id)
                child = await _delegate_child_frame(
                    session=session,
                    parent_id=str(group["parent_frame_id"]),
                    call_id=None,
                    group_id=done.pending_delegate_group_id,
                    args=next_task,
                    depth=int(group["depth"]),
                    prompt_factory=prompt_factory,
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                child = {
                    "error_code": "plan_dependency_or_stage_blocked",
                    "message": str(exc),
                }
            if isinstance(child, Frame):
                session.agent_stack.append(child)
                _plan_step_started(
                    session,
                    child,
                    event_callback,
                    next_step.step_id,
                )
                logger.info(
                    "Plan delegate group continued session=%s group_id=%s "
                    "step=%s child_frame=%s",
                    session.session_id,
                    done.pending_delegate_group_id,
                    next_step.step_id,
                    child.id,
                )
                return
            failure_text = (
                child
                if isinstance(child, str)
                else (
                    str(child.get("message", ""))
                    if isinstance(child, dict)
                    else "调度器已解锁的子任务参数不合法或 agent 不存在"
                )
            )
            group["results"].append(
                {
                    "agent": next_step.agent,
                    "summary": failure_text,
                    "error": True,
                    "error_code": (
                        child.get("error_code")
                        if isinstance(child, dict)
                        else "child_frame_creation_failed"
                    ),
                }
            )
            try:
                failed_graph = graph.fail_unstarted(
                    next_step.step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                group["results"].append(
                    {
                        "agent": next_step.agent,
                        "summary": str(exc),
                        "error": True,
                        "error_code": "plan_failure_reduction_blocked",
                    }
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
    elif isinstance(group.get("scheduler_plan"), dict):
        try:
            group_graph = PlanGraph.from_dict(group["scheduler_plan"])
            running = next(
                (
                    step
                    for step in group_graph.steps
                    if step.frame_id == done.id and step.status == "running"
                ),
                None,
            )
            if running is None:
                raise PlanGraphError(f"no running step owns frame {done.id}")
            output = (
                dict(delegate_result.get("result", {}))
                if isinstance(delegate_result.get("result"), dict)
                else {"summary": str(delegate_result.get("summary", ""))}
            )
            recovery_injected = False
            missing_inputs = output.get("missing_inputs")
            output_stage = str(output.get("stage", done.agent.map_stage or ""))
            if (
                isinstance(missing_inputs, list)
                and missing_inputs
                and output_stage in {"planner", "validator"}
            ):
                target = str(
                    output.get(
                        "target_path",
                        done.map_stage_contract.get("target_path", ""),
                    )
                )
                revision_value = output.get(
                    "map_revision",
                    done.map_stage_contract.get("map_revision", 0),
                )
                revision = (
                    revision_value
                    if isinstance(revision_value, int) and not isinstance(revision_value, bool)
                    else 0
                )
                retry = record_semantic_retry(
                    session.map_task_state,
                    category="missing_input",
                    error_category="typed_missing_inputs",
                    root_cause="planner_or_validator_missing_typed_inputs",
                    stage=output_stage,
                    target=target,
                    revision=revision,
                    operation={
                        "step_id": running.step_id,
                        "task": running.task,
                        "worker_spec": running.worker_spec,
                    },
                    missing_inputs=missing_inputs,
                    threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
                )
                if not bool(retry["exhausted"]):
                    group_graph = group_graph.inject_reader_recovery(
                        running.step_id,
                        missing_inputs=missing_inputs,
                        target=target,
                        revision=revision,
                    )
                    recovery_injected = True
                else:
                    output["error"] = "map_retry_exhausted"
                    output["retry_result"] = retry_pause_report(
                        session.map_task_state,
                        stage=output_stage,
                        target=target,
                        revision=revision,
                        last_attempt=retry,
                    )
            failed = delegate_result.get("error") is True or "error" in output
            error_code = str(output.get("error", "")) or None
            if "__reader__attempt_" in running.step_id:
                reader_missing = (
                    list(missing_inputs) if isinstance(missing_inputs, list) else ["missing_inputs"]
                )
                artifact_ref_value = delegate_result.get("artifact_ref")
                if reader_missing or (
                    not isinstance(artifact_ref_value, str) and not bool(output.get("facts"))
                ):
                    failed = True
                    error_code = "reader_recovery_incomplete"
                    output["reader_recovery_blocked"] = {
                        "missing_inputs": reader_missing,
                        "artifact_ref_present": isinstance(artifact_ref_value, str),
                        "facts_present": bool(output.get("facts")),
                    }
            if not failed and isinstance(running.expected_result_schema, dict):
                required = running.expected_result_schema.get("required", [])
                if isinstance(required, list):
                    missing = [
                        str(field)
                        for field in required
                        if isinstance(field, str) and field not in output
                    ]
                    if missing:
                        failed = True
                        error_code = "result_schema_mismatch"
                        output["missing_required_fields"] = missing
            artifact_ref = delegate_result.get("artifact_ref")
            if not recovery_injected:
                group_graph = group_graph.finish(
                    running.step_id,
                    PlanStepResult(
                        status="failed" if failed else "succeeded",
                        output=output,
                        artifact_refs=(
                            (str(artifact_ref),) if isinstance(artifact_ref, str) else ()
                        ),
                        error_code=error_code,
                    ),
                )
            group["scheduler_plan"] = group_graph.to_dict()
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "Delegate group scheduler update failed session=%s group=%s error=%s",
                session.session_id,
                done.pending_delegate_group_id,
                exc,
            )
            group_graph = None
        runnable = group_graph.runnable_steps() if group_graph is not None else ()
        if runnable:
            assert group_graph is not None
            next_step = runnable[0]
            try:
                next_task = group_graph.task_payload(next_step.step_id)
                child = await _delegate_child_frame(
                    session=session,
                    parent_id=str(group["parent_frame_id"]),
                    call_id=None,
                    group_id=done.pending_delegate_group_id,
                    args=next_task,
                    depth=int(group["depth"]),
                    prompt_factory=prompt_factory,
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                child = {
                    "error_code": "plan_dependency_or_stage_blocked",
                    "message": str(exc),
                }
            if isinstance(child, Frame):
                try:
                    started_graph = group_graph.start(
                        next_step.step_id,
                        child.id,
                    )
                except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                    child = {
                        "error_code": "plan_stage_transition_blocked",
                        "message": str(exc),
                    }
                else:
                    session.agent_stack.append(child)
                    group["scheduler_plan"] = started_graph.to_dict()
                    logger.info(
                        "Delegate scheduler continued session=%s group_id=%s "
                        "step=%s child_frame=%s",
                        session.session_id,
                        done.pending_delegate_group_id,
                        next_step.step_id,
                        child.id,
                    )
                    return
            failure_text = (
                child
                if isinstance(child, str)
                else (
                    str(child.get("message", ""))
                    if isinstance(child, dict)
                    else "调度器已解锁的子任务参数不合法或 agent 不存在"
                )
            )
            group["results"].append(
                {
                    "agent": next_step.agent,
                    "summary": failure_text,
                    "error": True,
                    "error_code": (
                        child.get("error_code")
                        if isinstance(child, dict)
                        else "child_frame_creation_failed"
                    ),
                }
            )
            try:
                failed_graph = group_graph.fail_unstarted(
                    next_step.step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                group["results"].append(
                    {
                        "agent": next_step.agent,
                        "summary": str(exc),
                        "error": True,
                        "error_code": "plan_failure_reduction_blocked",
                    }
                )
            else:
                group["scheduler_plan"] = failed_graph.to_dict()

    parent = _find_frame(session, str(group["parent_frame_id"]))
    if parent is not None:
        parent.messages.append(
            _tool_message(
                str(group["tool_call_id"]),
                {"results": group.get("results", [])},
            )
        )
        logger.info(
            "Delegate group completed session=%s group_id=%s results=%d",
            session.session_id,
            done.pending_delegate_group_id,
            len(group.get("results", [])),
        )
    session.delegate_groups.pop(done.pending_delegate_group_id, None)
