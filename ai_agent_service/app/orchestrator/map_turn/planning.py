"""处理 Map 计划创建、步骤启动与完成投影。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.orchestrator.macro_contracts import (
    MACRO_FORBIDDEN_FIELDS,
    MacroPlan,
    MacroPlanError,
    MacroPlanState,
    derive_macro_step_status_from_child,
)
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    record_plan_attempt,
    record_semantic_retry,
    retry_pause_report,
)
from app.orchestrator.map_turn.contracts import (
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.events import (
    _emit_orchestration_event,
    _history_timeline_payload,
)
from app.orchestrator.map_workflow import replace_map_state_field
from app.orchestrator.plan_scheduler import PlanGraph, PlanGraphError
from app.orchestrator.runtime_contracts import PlanStepResult
from app.sessions.store import Session
from app.tools.registry import REGISTRY


def _with_plan_runtime_metadata(
    payload: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Preserve request lineage fields omitted by ``PlanGraph.to_dict``."""
    for key in (
        "owner_frame_id",
        "request_lineage_id",
        "map_task_id",
        "macro_plan",
        "macro_plan_state",
        "semantic_attempt_key",
    ):
        if key in source:
            payload[key] = source.get(key)
    return payload


def _plan_step_started(
    session: Session,
    child: Frame,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    step_id: str | None = None,
) -> None:
    """若当前会话有活跃 `create_plan` 计划，记录新子帧对应的步骤并发出 `plan_step_started`。

    Args:
        session: 当前会话。
        child: 刚创建并压栈的子 agent 帧。
        event_callback: 编排事件回调；为 None 时不产生事件。
    """
    plan = session.pending_plan
    if plan is None:
        return
    try:
        graph = PlanGraph.from_dict(plan)
    except PlanGraphError as exc:
        logger.error("Invalid pending plan session=%s error=%s", session.session_id, exc)
        return
    runnable = graph.runnable_steps()
    selected_id = step_id or (runnable[0].step_id if runnable else None)
    if selected_id is None:
        return
    try:
        graph = graph.start(selected_id, child.id)
        step = graph.step(selected_id)
    except PlanGraphError as exc:
        logger.error(
            "Plan step start rejected session=%s frame=%s step=%s error=%s",
            session.session_id,
            child.id,
            selected_id,
            exc,
        )
        return
    session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
    _emit_orchestration_event(
        event_callback,
        "plan_step_started",
        {
            "frame_id": child.id,
            "message_index": len(child.messages),
            **_history_timeline_payload(child),
            "step_id": step.step_id,
            "step_index": step.order + 1,
            "total_steps": len(graph.steps),
            "agent": step.agent,
            "title": step.title,
        },
    )


def _plan_step_completed(
    session: Session,
    done: Frame,
    result_payload: dict[str, Any],
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """若已完成的子帧对应某个计划步骤，发出 `plan_step_completed`。

    Args:
        session: 当前会话。
        done: 刚结束并弹栈的子 agent 帧。
        text: 该子帧本轮产出的最终文本，用作步骤结果摘要。
        event_callback: 编排事件回调；为 None 时不产生事件。
    """
    plan = session.pending_plan
    if plan is None:
        return
    macro_state_dict = plan.get("macro_plan_state")
    if isinstance(macro_state_dict, dict):
        # macro_v2：宏观步骤终态只由 owner 发布驱动，子帧完成不直接完成宏观步骤。
        # 内部子阶段完成（reader/planner 等）通过 owner 发布上升为宏观状态。
        try:
            macro_state = MacroPlanState.from_dict(macro_state_dict)
        except MacroPlanError as exc:
            logger.debug(
                "macro state invalid on child complete session=%s error=%s",
                session.session_id,
                exc,
            )
            return
        running = next(
            (
                step
                for step in macro_state.plan.steps
                if step.owner_frame_id == done.id and step.status == "running"
            ),
            None,
        )
        if running is None:
            return
        output = (
            dict(result_payload.get("result", {}))
            if isinstance(result_payload.get("result"), dict)
            else {"summary": str(result_payload.get("summary", ""))}
        )
        updated = derive_macro_step_status_from_child(macro_state, running.step_id, output)
        if updated.to_dict() != macro_state_dict:
            plan["macro_plan_state"] = updated.to_dict()
            _emit_orchestration_event(
                event_callback,
                "macro_owner_step_updated",
                {
                    "step_id": running.step_id,
                    "frame_id": done.id,
                    "status": updated.step(running.step_id).status,
                },
            )
        return
    try:
        graph = PlanGraph.from_dict(plan)
    except PlanGraphError as exc:
        logger.error("Invalid pending plan session=%s error=%s", session.session_id, exc)
        return
    running = next(
        (step for step in graph.steps if step.frame_id == done.id and step.status == "running"),
        None,
    )
    if running is None:
        return
    output = (
        dict(result_payload.get("result", {}))
        if isinstance(result_payload.get("result"), dict)
        else {"summary": str(result_payload.get("summary", ""))}
    )
    artifact_ref = result_payload.get("artifact_ref")
    stage = str(output.get("stage", done.agent.map_stage or ""))
    missing_inputs = output.get("missing_inputs")
    is_reader_recovery = "__reader__attempt_" in running.step_id
    if isinstance(missing_inputs, list) and missing_inputs and stage in {"planner", "validator"}:
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
            stage=stage,
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
            try:
                graph = graph.inject_reader_recovery(
                    running.step_id,
                    missing_inputs=missing_inputs,
                    target=target,
                    revision=revision,
                )
            except PlanGraphError as exc:
                logger.error(
                    "Reader recovery injection failed session=%s step=%s error=%s",
                    session.session_id,
                    running.step_id,
                    exc,
                )
            else:
                session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
                _emit_orchestration_event(
                    event_callback,
                    "plan_reader_recovery_scheduled",
                    {
                        "step_id": running.step_id,
                        "missing_inputs": missing_inputs,
                        "target": target,
                        "revision": revision,
                        "attempt": retry["attempt"],
                        "retry_key": retry["retry_key"],
                    },
                )
                return
        report = retry_pause_report(
            session.map_task_state,
            stage=stage,
            target=target,
            revision=revision,
            last_attempt=retry,
        )
        output["error"] = "map_retry_exhausted"
        output["retry_result"] = report
        if session.map_task_state.status == "running":
            session.map_task_state.make_checkpoint(
                "map_retry_exhausted",
                report,
                pause_kind="no_progress_exhausted",
            )
        else:
            session.map_task_state.pause_kind = "no_progress_exhausted"
            session.map_task_state.pause_reason = "map_retry_exhausted"
            replace_map_state_field(
                session.map_task_state,
                "pause_report",
                report,
                target=target or None,
                revision=revision,
            )
    failed = result_payload.get("error") is True or "error" in output
    repair = output.get("repair")
    if isinstance(repair, dict) and repair.get("status") == "exhausted":
        failed = True
        output["error"] = "structured_output_repair_exhausted"
    if is_reader_recovery:
        reader_missing = (
            list(missing_inputs) if isinstance(missing_inputs, list) else ["missing_inputs"]
        )
        has_typed_facts = isinstance(artifact_ref, str) or bool(output.get("facts"))
        if reader_missing or not has_typed_facts:
            failed = True
            output["error"] = "reader_recovery_incomplete"
            output["reader_recovery_blocked"] = {
                "missing_inputs": reader_missing,
                "artifact_ref_present": isinstance(artifact_ref, str),
                "facts_present": bool(output.get("facts")),
            }
    error_code = str(output.get("error", "")) or None
    if not failed and isinstance(running.expected_result_schema, dict):
        required = running.expected_result_schema.get("required", [])
        if isinstance(required, list):
            missing = [
                str(field) for field in required if isinstance(field, str) and field not in output
            ]
            if missing:
                failed = True
                error_code = "result_schema_mismatch"
                output["missing_required_fields"] = missing
    result = PlanStepResult(
        status="failed" if failed else "succeeded",
        output=output,
        artifact_refs=(str(artifact_ref),) if isinstance(artifact_ref, str) else (),
        error_code=error_code,
    )
    try:
        graph = graph.finish(running.step_id, result)
    except PlanGraphError as exc:
        logger.error(
            "Plan step finish rejected session=%s frame=%s error=%s",
            session.session_id,
            done.id,
            exc,
        )
        return
    session.pending_plan = _with_plan_runtime_metadata(graph.to_dict(), plan)
    full_summary = str(result_payload.get("summary", "")).strip()
    summary = " ".join(full_summary.split())
    if len(summary) > 240:
        summary = summary[:240] + "..."
    _emit_orchestration_event(
        event_callback,
        "plan_step_completed",
        {
            "frame_id": done.id,
            "message_index": len(done.messages),
            **_history_timeline_payload(done),
            "step_id": running.step_id,
            "step_index": running.order + 1,
            "total_steps": len(graph.steps),
            "status": result.status,
            "summary": summary,
            "full_summary": full_summary,
        },
    )


_PLAN_COMPLEXITY_LEVELS = {"low", "medium", "high"}


def _normalize_plan_steps(raw_steps: Any) -> list[dict[str, Any]] | str:
    """校验并规范化 `create_plan.steps` 入参。

    Args:
        raw_steps: `create_plan` 工具调用入参里的 `steps` 原始值。

    Returns:
        校验通过时返回规范化后的步骤字典列表；校验失败时返回中文错误提示字符串。
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        return "create_plan.steps 不能为空"
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            return "create_plan.steps 的每一项必须是 object"
        forbidden = MACRO_FORBIDDEN_FIELDS & set(raw.keys())
        if forbidden:
            return (
                "create_plan.steps[] 禁止携带 specialist 内部构造字段（如 worker_spec、"
                "stage、mode 等），请改为一个领域 owner 步骤加可选展示里程碑："
                f"{sorted(forbidden)}"
            )
        title = raw.get("title")
        agent_name = raw.get("agent")
        task = raw.get("task")
        if not isinstance(title, str) or not title.strip():
            return "create_plan.steps[].title 不能为空"
        if not isinstance(agent_name, str) or not agent_name.strip():
            return "create_plan.steps[].agent 不能为空"
        if not isinstance(task, str) or not task.strip():
            return "create_plan.steps[].task 不能为空"
        try:
            get_agent(agent_name, set(REGISTRY))
        except KeyError:
            return f"未知子 agent：{agent_name}"
        complexity = raw.get("estimated_complexity")
        if complexity is not None and complexity not in _PLAN_COMPLEXITY_LEVELS:
            return "create_plan.steps[].estimated_complexity 取值必须是 low/medium/high"
        normalized.append(
            {
                "id": str(raw.get("id", raw.get("step_id", f"step-{index + 1}"))).strip(),
                "title": title.strip(),
                "agent": agent_name.strip(),
                "task": task.strip(),
                "depends_on": [
                    str(item).strip()
                    for item in raw.get("depends_on", [])
                    if isinstance(item, str) and item.strip()
                ],
                "input_bindings": [
                    dict(item) for item in raw.get("input_bindings", []) if isinstance(item, dict)
                ],
                "expected_result_schema": (
                    dict(raw["expected_result_schema"])
                    if isinstance(raw.get("expected_result_schema"), dict)
                    else None
                ),
                "owner_agent": (
                    str(raw["owner_agent"]).strip()
                    if isinstance(raw.get("owner_agent"), str)
                    else None
                ),
                "domain": (
                    str(raw["domain"]).strip() if isinstance(raw.get("domain"), str) else None
                ),
                "objective": (
                    str(raw["objective"]).strip() if isinstance(raw.get("objective"), str) else None
                ),
                "acceptance_criteria": [
                    str(item).strip()
                    for item in raw.get("acceptance_criteria", [])
                    if isinstance(item, str) and item.strip()
                ],
                "predecessor_bindings": [
                    dict(item)
                    for item in raw.get("predecessor_bindings", [])
                    if isinstance(item, dict)
                ],
                "display_milestones": [
                    dict(item)
                    for item in raw.get("display_milestones", [])
                    if isinstance(item, dict)
                ],
                "estimated_complexity": complexity,
            }
        )
    map_owner_count = sum(
        1
        for step in normalized
        if str(step.get("owner_agent") or step.get("agent") or "") == "map-agent"
        or str(step.get("domain") or "") == "map"
    )
    if map_owner_count > 1:
        return (
            "create_plan 拒绝为同一地图任务创建多个 sibling map-agent owner 步骤；"
            "请合并为一个领域 owner 成果，内部 read/plan/preview/write/verify 阶段"
            "用展示里程碑表达。"
        )
    try:
        PlanGraph.from_dict({"summary": "", "steps": normalized})
    except PlanGraphError as exc:
        return f"create_plan.steps 依赖图不合法：{exc}"
    # 保留原始规范化字典（含 owner/domain/objective/display_milestones 等
    # macro 字段），不经过 PlanStep.to_dict 再序列化——后者会丢弃 macro 字段，
    # 使 MacroPlan 构建拿不到领域信息。PlanGraph 校验仅用于依赖/DAG 校验。
    return normalized


def _handle_create_plan(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> None:
    """处理 `create_plan` 工具调用：校验入参、记录计划、发出通知事件并回填工具结果。

    `create_plan` 不挂起轮次：校验通过后立即把 `steps` 转换为 `delegate_many.tasks`
    形状，作为成功结果回填本次调用，引导 LLM 在下一轮自行调用 `delegate_many`
    开始执行（§2.4.2）。

    Args:
        session: 当前会话。
        frame: 发起 `create_plan` 调用的帧（必须是允许委派的 agent）。
        call_id: 本次 `create_plan` 调用的 tool_call id。
        args: 已解析的入参（`summary`/`steps`）。
        event_callback: 编排事件回调，用于发出 `plan_created`。
    """
    if not frame.agent.can_delegate:
        logger.warning(
            "Create_plan rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent，不能创建计划", is_error=True)
        )
        return

    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        frame.messages.append(_tool_message(call_id, "create_plan.summary 不能为空", is_error=True))
        return

    steps = _normalize_plan_steps(args.get("steps"))
    if isinstance(steps, str):
        frame.messages.append(_tool_message(call_id, steps, is_error=True))
        return

    try:
        graph = PlanGraph.from_dict(
            {
                "summary": summary.strip(),
                "steps": steps,
            }
        )
    except PlanGraphError as exc:
        frame.messages.append(
            _tool_message(call_id, f"create_plan 依赖图不合法：{exc}", is_error=True)
        )
        return
    state = session.map_task_state
    frontier = state.failure_frontier if isinstance(state.failure_frontier, dict) else {}
    target = str(frontier.get("target", "")).strip()
    if not target and state.latest_revisions:
        target = sorted(state.latest_revisions)[0].split("::", 1)[0]
    revision = max(state.latest_revisions.values(), default=0)
    root_error_code = str(
        frontier.get("error_code") or frontier.get("blocked_reason") or "planning"
    )
    attempt: dict[str, Any] | None = None
    if state.status == "running":
        attempt = record_plan_attempt(
            state,
            stage=state.stage,
            target=target or "__workflow__",
            revision=revision,
            operation={"summary": summary.strip(), "steps": steps},
            root_error_code=root_error_code,
        )
        if bool(attempt.get("exhausted", False)):
            report = {
                "type": "map_task_convergence_exhausted",
                "exact_attempt": attempt["exact"],
                "task_convergence": attempt["convergence"],
                "root_error_code": root_error_code,
                "recovery_guidance": (
                    "Inspect the first root cause or explicitly start a distinct "
                    "task epoch; sub-step revision progress does not reset this count."
                ),
            }
            state.make_checkpoint(
                "create_plan convergence circuit breaker exhausted",
                report,
                pause_kind="no_progress_exhausted",
            )
            frame.messages.append(
                _tool_message(
                    call_id,
                    {
                        "ok": False,
                        "error_code": "map_task_convergence_exhausted",
                        "report": report,
                    },
                    is_error=True,
                )
            )
            return
    exact_key = str(attempt["exact"].get("key", "")) if isinstance(attempt, dict) else ""
    if (
        exact_key
        and isinstance(session.pending_plan, dict)
        and session.pending_plan.get("semantic_attempt_key") == exact_key
    ):
        existing_graph = PlanGraph.from_dict(session.pending_plan)
        frame.messages.append(
            _tool_message(
                call_id,
                {
                    "ok": True,
                    "idempotent_replay": True,
                    "tasks": [
                        existing_graph.task_payload(step.step_id) for step in existing_graph.steps
                    ],
                    "note": "相同语义计划已存在；保留当前 running/terminal 步骤结果。",
                },
            )
        )
        return
    session.pending_plan = graph.to_dict()
    try:
        macro_plan = MacroPlan.from_dict({"summary": summary.strip(), "steps": steps})
        macro_state = MacroPlanState.from_plan(macro_plan)
        session.pending_plan["macro_plan"] = macro_plan.to_dict()
        session.pending_plan["macro_plan_state"] = macro_state.to_dict()
    except MacroPlanError as macro_exc:
        logger.debug(
            "Macro plan not stashable for create_plan session=%s frame=%s error=%s",
            session.session_id,
            frame.id,
            macro_exc,
        )
    if exact_key:
        session.pending_plan["semantic_attempt_key"] = exact_key
    session.pending_plan["owner_frame_id"] = frame.id
    session.pending_plan["request_lineage_id"] = frame.map_request_lineage_id or ""
    session.pending_plan["map_task_id"] = frame.map_task_id or ""
    logger.info(
        "Plan created session=%s frame=%s steps=%d",
        session.session_id,
        frame.id,
        len(steps),
    )
    macro_views: dict[str, dict[str, Any]] = {}
    macro_state_dict = session.pending_plan.get("macro_plan_state")
    if isinstance(macro_state_dict, dict):
        try:
            macro_state = MacroPlanState.from_dict(macro_state_dict)
        except MacroPlanError:
            macro_state = None
        if macro_state is not None:
            for macro_step in macro_state.plan.steps:
                macro_views[macro_step.step_id] = {
                    "owner_agent": macro_step.owner_agent,
                    "domain": macro_step.domain,
                    "objective": macro_step.objective,
                    "display_milestones": [
                        milestone.to_dict() for milestone in macro_step.display_milestones
                    ],
                }
    _emit_orchestration_event(
        event_callback,
        "plan_created",
        {
            "frame_id": frame.id,
            "agent": frame.agent.name,
            "message_index": len(frame.messages),
            **_history_timeline_payload(frame),
            "summary": session.pending_plan["summary"],
            "steps": [
                {
                    "id": step.step_id,
                    "index": step.order + 1,
                    "title": step.title,
                    "agent": step.agent,
                    "task": step.task,
                    "depends_on": list(step.depends_on),
                    "estimated_complexity": step.estimated_complexity,
                    **macro_views.get(step.step_id, {}),
                }
                for step in graph.steps
            ],
        },
    )
    tasks = [graph.task_payload(step.step_id) for step in graph.steps]
    frame.messages.append(
        _tool_message(
            call_id,
            {
                "ok": True,
                "tasks": tasks,
                "note": "计划已记录并通知用户。请立即调用 delegate_many，把上面的 tasks 原样作为参数传入以开始执行。",
            },
        )
    )
