"""处理 Map 并行委派组的创建与所有权绑定。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from app.agents.types import Frame
from app.orchestrator.frame_contract_types import (
    DomainOwnerContract,
    FrameContractTypeError,
)
from app.orchestrator.macro_contracts import (
    MacroPlanError,
    MacroPlanState,
)
from app.orchestrator.map_context import record_map_owner_link

from app.orchestrator.map_turn.contracts import (
    MAX_AGENT_DEPTH,
    AgentPromptFactory,
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.delegation import _delegate_child_frame
from app.orchestrator.map_turn.frame_info import _find_frame
from app.orchestrator.map_turn.planning import (
    _plan_step_started,
    _with_plan_runtime_metadata,
)
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    is_map_worker_write_mode,
)
from app.orchestrator.plan_scheduler import PlanGraph, PlanGraphError
from app.sessions.store import Session


def _record_macro_owner(session: Session, step_id: str | None, owner_frame_id: str) -> None:
    """若存在 macro_v2 调度状态，记录 owner Frame 身份并持久化回 pending_plan。

    持久 owner 身份是 create-or-resume 调度与 planner 路由守卫（task 3.3）的
    前置：后续重试、审批续接与超时恢复据此 resume 同一 owner 而非新建 sibling。
    实际帧级 resume 复用引擎既有 map-task 恢复路径（resumed_existing_map_task）。
    """
    plan = session.pending_plan
    if not isinstance(plan, dict) or not step_id:
        return
    state_dict = plan.get("macro_plan_state")
    if not isinstance(state_dict, dict):
        return
    try:
        state = MacroPlanState.from_dict(state_dict)
        domain_task_id = state.step(step_id).domain_task_id or f"{step_id}:{session.session_epoch}"
        state = state.set_owner(
            step_id, owner_frame_id=owner_frame_id, domain_task_id=domain_task_id
        )
        plan["macro_plan_state"] = state.to_dict()
        owner_frame = _find_frame(session, owner_frame_id)
        if owner_frame is not None and owner_frame.domain_owner_contract:
            owner_contract = DomainOwnerContract.from_dict(
                owner_frame.domain_owner_contract
            ).with_macro_link(
                macro_step_id=step_id,
                domain_task_id=domain_task_id,
            )
            owner_frame.domain_owner_contract = owner_contract.to_dict()
        if owner_frame is not None and owner_frame.agent.role == "map_orchestrator":
            target = next(
                iter(session.map_task_state.latest_revisions),
                "__map_owner__",
            )
            revision = session.map_task_state.latest_revisions.get(target, 0)
            record_map_owner_link(
                session.map_task_state,
                macro_step_id=step_id,
                owner_frame_id=owner_frame_id,
                domain_task_id=domain_task_id,
                target=target,
                revision=(
                    revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
                ),
            )
    except (MacroPlanError, FrameContractTypeError) as macro_exc:
        logger.debug(
            "macro owner record skipped session=%s step=%s frame=%s error=%s",
            session.session_id,
            step_id,
            owner_frame_id,
            macro_exc,
        )


async def _start_delegate_group(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """启动 `delegate_many` 顺序子任务组。"""
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        logger.warning(
            "Delegate_many rejected: missing tasks session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(_tool_message(call_id, "delegate_many.tasks 不能为空", is_error=True))
        return False
    if not frame.agent.can_delegate:
        logger.warning(
            "Delegate_many rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True)
        )
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate_many rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(
            _tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True)
        )
        return False

    submitted_tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not submitted_tasks:
        logger.warning(
            "Delegate_many rejected: invalid tasks session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(call_id, "delegate_many.tasks 格式不合法", is_error=True)
        )
        return False
    plan_driven = (
        session.pending_plan is not None and session.pending_plan.get("owner_frame_id") == frame.id
    )
    plan_step_id: str | None = None
    if plan_driven:
        try:
            graph = PlanGraph.from_dict(session.pending_plan or {})
        except PlanGraphError as exc:
            frame.messages.append(_tool_message(call_id, f"当前计划无法恢复：{exc}", is_error=True))
            return False
        runnable = graph.runnable_steps()
        if not runnable:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "status": "blocked",
                        "error_code": "plan_predecessor_not_succeeded",
                        "message": "当前计划没有可运行步骤；请检查前置步骤终态",
                    },
                    is_error=True,
                )
            )
            return False
        first_step = runnable[0]
        plan_step_id = first_step.step_id
        try:
            first = graph.task_payload(first_step.step_id)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_dependency_binding_failed",
                        "step_id": first_step.step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
        contract_tasks = [
            {
                "agent": step.agent,
                "task": step.task,
                "worker_spec": step.worker_spec,
            }
            for step in graph.steps
            if step.status == "pending"
        ]
    else:
        raw_group_steps = [
            {
                "id": str(
                    task.get(
                        "plan_step_id",
                        task.get("id", f"{call_id}-step-{index + 1}"),
                    )
                ),
                "title": str(task.get("title", task.get("task", "")))[:120],
                "agent": task.get("agent"),
                "task": task.get("task"),
                "depends_on": task.get("depends_on", []),
                "input_bindings": task.get("input_bindings", []),
                "expected_result_schema": task.get("expected_result_schema"),
                "worker_spec": task.get("worker_spec"),
            }
            for index, task in enumerate(submitted_tasks)
        ]
        try:
            group_graph = PlanGraph.from_dict(
                {
                    "summary": f"delegate_many:{call_id}",
                    "steps": raw_group_steps,
                }
            )
        except PlanGraphError as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    f"delegate_many 依赖图不合法：{exc}",
                    is_error=True,
                )
            )
            return False
        runnable = group_graph.runnable_steps()
        if not runnable:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "status": "blocked",
                        "error_code": "plan_predecessor_not_succeeded",
                        "message": "delegate_many 没有可运行根步骤",
                    },
                    is_error=True,
                )
            )
            return False
        first_step = runnable[0]
        plan_step_id = first_step.step_id
        try:
            first = group_graph.task_payload(first_step.step_id)
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_dependency_binding_failed",
                        "step_id": first_step.step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
        contract_tasks = [
            {
                "agent": step.agent,
                "task": step.task,
                "worker_spec": step.worker_spec,
            }
            for step in group_graph.steps
        ]
    if any(isinstance(task.get("worker_spec"), dict) for task in contract_tasks):
        # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
        if frame.agent.map_stage != "orchestrator":
            logger.warning(
                "Delegate_many rejected: dynamic worker parent is not map-agent session=%s frame=%s agent=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
            )
            frame.messages.append(
                _tool_message(call_id, "只有 map-agent 可以创建动态地图 worker", is_error=True)
            )
            return False
        write_workers = [
            task
            for task in contract_tasks
            if isinstance(task.get("worker_spec"), dict)
            and is_map_worker_write_mode(task["worker_spec"].get("mode"))
        ]
        if len(write_workers) > 1:
            logger.warning(
                "Delegate_many rejected: multiple map write workers session=%s frame=%s count=%d",
                session.session_id,
                frame.id,
                len(write_workers),
            )
            frame.messages.append(
                _tool_message(
                    call_id,
                    "delegate_many 同一组最多只能包含一个地图写入 worker；请拆成多个阶段串行执行",
                    is_error=True,
                )
            )
            return False
    group_id = call_id
    session.delegate_groups[group_id] = {
        "parent_frame_id": frame.id,
        "tool_call_id": call_id,
        "request_lineage_id": frame.map_request_lineage_id or "",
        "map_task_id": frame.map_task_id or "",
        "results": [],
        "depth": frame.depth + 1,
        "plan_driven": plan_driven,
        "scheduler_plan": (None if plan_driven else group_graph.to_dict()),
    }
    workflow_snapshot = copy.deepcopy(session.map_task_state)
    try:
        child = await _delegate_child_frame(
            session=session,
            parent_id=frame.id,
            call_id=None,
            group_id=group_id,
            args=first,
            depth=frame.depth + 1,
            prompt_factory=prompt_factory,
        )
    except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "error_code": "plan_child_creation_blocked",
                    "step_id": plan_step_id,
                    "message": str(exc),
                },
                is_error=True,
            )
        )
        return False
    if isinstance(child, str):
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        if plan_driven and session.pending_plan is not None and plan_step_id is not None:
            try:
                active_graph = PlanGraph.from_dict(session.pending_plan)
                failed_graph = active_graph.fail_unstarted(
                    plan_step_id,
                    "child_frame_creation_failed",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                logger.error(
                    "Failed to reduce child creation outcome session=%s step=%s error=%s",
                    session.session_id,
                    plan_step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
        logger.warning(
            "Delegate_many rejected: invalid first task session=%s frame=%s error=%s",
            session.session_id,
            frame.id,
            child,
        )
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "status": "blocked",
                    "error_code": "plan_child_contract_blocked",
                    "step_id": plan_step_id,
                    "message": child,
                },
                is_error=True,
            )
        )
        return False
    if child is None:
        session.map_task_state = workflow_snapshot
        session.delegate_groups.pop(group_id, None)
        if plan_driven and session.pending_plan is not None and plan_step_id is not None:
            try:
                active_graph = PlanGraph.from_dict(session.pending_plan)
                failed_graph = active_graph.fail_unstarted(
                    plan_step_id,
                    "invalid_delegate_task",
                )
            except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
                logger.error(
                    "Failed to reduce invalid child payload session=%s step=%s error=%s",
                    session.session_id,
                    plan_step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(
                    failed_graph.to_dict(),
                    session.pending_plan,
                )
        logger.warning(
            "Delegate_many rejected: invalid first task session=%s frame=%s",
            session.session_id,
            frame.id,
        )
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": False,
                    "status": "blocked",
                    "error_code": "plan_child_payload_invalid",
                    "step_id": plan_step_id,
                    "message": "delegate_many 首个子任务不合法",
                },
                is_error=True,
            )
        )
        return False
    session.agent_stack.append(child)
    if plan_driven:
        _plan_step_started(session, child, event_callback, plan_step_id)
        _record_macro_owner(session, plan_step_id, child.id)
    else:
        group = session.delegate_groups[group_id]
        try:
            group_graph = PlanGraph.from_dict(group["scheduler_plan"])
            group["scheduler_plan"] = group_graph.start(
                str(plan_step_id),
                child.id,
            ).to_dict()
        except (PlanGraphError, KeyError, TypeError, ValueError) as exc:
            session.map_task_state = workflow_snapshot
            session.agent_stack.pop()
            session.delegate_groups.pop(group_id, None)
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "plan_stage_transition_blocked",
                        "step_id": plan_step_id,
                        "message": str(exc),
                    },
                    is_error=True,
                )
            )
            return False
    logger.info(
        "Delegate_many group started session=%s group_id=%s parent_frame=%s child_frame=%s total_tasks=%d",
        session.session_id,
        group_id,
        frame.id,
        child.id,
        len(raw_tasks),
    )
    return True
