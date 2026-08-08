"""执行 Map 工具、计划与委派协议守卫。"""

from __future__ import annotations

from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.orchestrator.frame_contract_types import (
    DomainOwnerContract,
    FrameContractTypeError,
    MapWorkerStageContract,
)
from app.orchestrator.map_contracts import (
    MAP_WORKER_RESULT_SCHEMA,
    MAP_WORKER_STAGES,
)
from app.orchestrator.map_context import latest_map_revision
from app.orchestrator.map_platform_planning import map_platform_plan_call_error
from app.orchestrator.map_validation import validation_call_error

from app.orchestrator.map_routing import assess_map_task
from app.orchestrator.map_turn.contracts import (
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.frame_info import _find_frame
from app.orchestrator.map_turn.tool_arguments import _load_tool_args
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    MAP_PLANNER_PIPELINE_SKILLS,
    MAP_VALIDATION_TOOL_NAMES,
    MAP_WRITE_TOOL_NAMES,
    is_map_write_tool,
    validate_map_write_args,
)
from app.orchestrator.turn.contracts import (
    ErrorTurnOutcome,
)
from app.sessions.store import Session
from app.tools.registry import REGISTRY


def _planner_route_guard(
    frame: Frame,
    session: Session | None = None,
) -> ErrorTurnOutcome | None:
    """验证 owner/worker Frame 合同矩阵，并在 provider 调用前失败关闭。"""
    is_owner = frame.agent.role == "map_orchestrator" and frame.agent.map_stage == "orchestrator"
    has_worker_fields = any(
        (
            frame.map_stage_contract,
            frame.worker_instance_id,
            frame.result_schema,
            frame.allowed_next_stages,
        )
    )
    if is_owner:
        try:
            owner_contract = DomainOwnerContract.from_dict(frame.domain_owner_contract)
        except FrameContractTypeError:
            owner_contract = None
        if (
            owner_contract is not None
            and owner_contract.owner_frame_id == frame.id
            and not has_worker_fields
        ):
            return None
        return _map_route_contract_error("地图 owner 的角色、owner 合同或 worker-only 字段不一致")

    is_map_specialist = (
        frame.agent.pipeline_kind == "map" and frame.agent.map_stage in MAP_WORKER_STAGES
    )
    if not is_map_specialist:
        if has_worker_fields:
            return _map_route_contract_error("非地图 specialist Frame 携带了 worker 合同或 schema")
        return None
    try:
        worker_contract = MapWorkerStageContract.from_dict(frame.map_stage_contract)
    except FrameContractTypeError:
        return _map_route_contract_error("地图 specialist 缺少合法 worker 合同")
    if (
        worker_contract.stage != frame.agent.map_stage
        or worker_contract.contract_id != frame.contract_id
        or worker_contract.worker_instance_id != frame.worker_instance_id
        or worker_contract.result_schema != frame.result_schema
        or worker_contract.allowed_next_stages != frame.allowed_next_stages
        or frame.result_schema != MAP_WORKER_RESULT_SCHEMA
    ):
        return _map_route_contract_error("地图 specialist 的角色、阶段或身份不匹配")
    if session is not None and any(
        (
            session.map_task_state.owner_frame_id,
            session.map_task_state.task_id,
            session.map_task_state.task_lineage_id,
        )
    ):
        parent = _find_frame(session, frame.parent_id or "")
        if (
            parent is None
            or parent.id != session.map_task_state.owner_frame_id
            or frame.map_request_lineage_id != session.map_task_state.task_lineage_id
            or frame.map_task_id != session.map_task_state.task_id
        ):
            return _map_route_contract_error("地图 worker 属于 sibling 或过期 lineage")
    if worker_contract.stage == "planner":
        bundle = worker_contract.planning_context_bundle
        if bundle is None or not bundle.contexts:
            return _map_route_contract_error("planner 缺少规划上下文集合")
        if not frame.agent.skills:
            return _map_route_contract_error("planner 缺少声明的规划 Skill")
        if frame.agent.worker_mode in {
            "propose_only",
            "repair_propose",
        } and not MAP_PLANNER_PIPELINE_SKILLS.intersection(frame.agent.skills):
            return _map_route_contract_error("planner 缺少后端解析的规划 Skill")
        if session is not None:
            active_contexts = session.map_task_state.planning_contexts
            for entry in bundle.contexts:
                active = active_contexts.get(entry.context_id)
                if not isinstance(active, dict):
                    active = next(
                        (
                            snapshot
                            for snapshot in session.map_task_state.authoritative_snapshots.values()
                            if isinstance(snapshot, dict)
                            and snapshot.get("snapshot_id") == entry.context_id
                            and snapshot.get("digest") == entry.digest
                        ),
                        None,
                    )
                if (
                    not isinstance(active, dict)
                    or active.get("digest") != entry.digest
                    or active.get("semantic_role", "map_reference") != entry.semantic_role
                ):
                    return _map_route_contract_error(
                        f"planner 规划上下文缺失或已替换：{entry.context_id}"
                    )
                if not entry.fresh:
                    return _map_route_contract_error(
                        f"planner 规划上下文已标记过期：{entry.context_id}"
                    )
                if (
                    entry.target_path is not None
                    and entry.map_layer is not None
                    and entry.source_revision is not None
                    and latest_map_revision(session, entry.target_path, entry.map_layer)
                    != entry.source_revision
                ):
                    return _map_route_contract_error(
                        f"planner 规划上下文 revision 已过期：{entry.context_id}"
                    )
    return None


def _map_route_contract_error(reason: str) -> ErrorTurnOutcome:
    """构造不包含用户补救指令的后端路由错误。"""
    return ErrorTurnOutcome(
        text=(
            "map_route_contract_violation：后端地图 Frame 路由合同不一致；"
            f"{reason}。运行时将恢复 owner checkpoint 或重建 typed child。"
        ),
        error_code="map_route_contract_violation",
    )


def _append_delegate_protocol_errors(frame: Frame, calls: list[Any]) -> None:
    """当 `delegate` 与其他 tool call 并列时，给本轮所有调用补错误结果。"""
    logger.warning(
        "Delegate protocol violation frame=%s agent=%s tool_calls=%d",
        frame.id,
        frame.agent.name,
        len(calls),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                "`delegate` 必须是本轮唯一的 tool call；本轮所有工具均未执行，请重试",
                is_error=True,
            )
        )


def _append_create_plan_protocol_errors(frame: Frame, calls: list[Any]) -> None:
    """当 `create_plan` 与其他 tool call 并列时，给本轮所有调用补错误结果。"""
    logger.warning(
        "Create_plan protocol violation frame=%s agent=%s tool_calls=%d",
        frame.id,
        frame.agent.name,
        len(calls),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                "`create_plan` 必须是本轮唯一的 tool call；本轮所有工具均未执行，请重试",
                is_error=True,
            )
        )


def _agent_name_has_role(agent_name: Any, role: str) -> bool:
    """判断内置 Agent 名是否解析为指定稳定角色。

    本轮整改新增：将 agent 名称解析为 AgentDefinition 后比对 role 字段，
    替代直接硬编码 agent name 字符串，使 capability contract 派生的
    动态 agent 也能被正确识别。
    """
    if not isinstance(agent_name, str) or not agent_name:
        return False
    try:
        return get_agent(agent_name, set(REGISTRY)).role == role
    except KeyError:
        return False


def _map_agent_targets_from_delegate_call(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return map-agent task texts from delegate/delegate_many args."""
    if tool_name == "delegate":
        # 本轮整改：用 role=="map_orchestrator" 代替硬编码 "map-agent"
        if _agent_name_has_role(args.get("agent"), "map_orchestrator") and isinstance(
            args.get("task"), str
        ):
            return [str(args["task"])]
        return []
    if tool_name != "delegate_many":
        return []
    raw_tasks = args.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[str] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        if _agent_name_has_role(item.get("agent"), "map_orchestrator") and isinstance(
            item.get("task"), str
        ):
            tasks.append(str(item["task"]))
    return tasks


def _requires_create_plan_before_map_delegate(
    session: Session,
    frame: Frame,
    tool_name: str,
    args: dict[str, Any],
) -> bool:
    """Only proven atomic edits and read-only tasks may skip a visible plan."""
    # 本轮整改：用 role 代替 name=="coordinator"，支持 capability contract 派生
    if frame.agent.role != "coordinator" or session.pending_plan is not None:
        return False
    return any(
        assess_map_task(task).requires_visible_plan
        for task in _map_agent_targets_from_delegate_call(tool_name, args)
    )


def _append_map_write_protocol_errors(frame: Frame, calls: list[Any]) -> bool:
    """校验地图写工具单轮协议，失败时补工具错误并要求模型重试。"""
    write_calls = [call for call in calls if is_map_write_tool(call.name)]
    # 本轮整改：用 role/map_stage 代替 name in {"coordinator","map-agent"}，
    # 确保 capability contract 派生的 agent 也受此约束
    if write_calls and (
        frame.agent.role == "coordinator" or frame.agent.map_stage == "orchestrator"
    ):
        message = (
            "地图总控不得直接调用写工具或临时拼接地图块；请把已确认的规划批次委派给 "
            "write_one_batch worker。平台路线只能执行 validate_platform_level_plan "
            "校验通过后返回的 edit_map_batches。"
        )
        logger.warning(
            "Blocked coordinator direct map write frame=%s agent=%s tools=%s",
            frame.id,
            frame.agent.name,
            [call.name for call in write_calls],
        )
        for call in calls:
            frame.messages.append(_tool_message(call.id, message, is_error=True))
        return True
    if len(write_calls) > 1 and len(write_calls) != len(calls):
        logger.warning(
            "Map write protocol violation frame=%s agent=%s write_calls=%d",
            frame.id,
            frame.agent.name,
            len(write_calls),
        )
        for call in calls:
            frame.messages.append(
                _tool_message(
                    call.id,
                    "确定性地图批次不能与读取、验证或服务端工具混在同一轮；请只提交有序写批次",
                    is_error=True,
                )
            )
        return True

    for call in write_calls:
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return True
        assert args is not None
        error = validate_map_write_args(call.name, args)
        if error is not None:
            frame.messages.append(_tool_message(call.id, error, is_error=True))
            return True
    return False


def _append_map_plan_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """在前端执行前拒绝重复或超限的平台规划方案。"""
    for call in calls:
        if call.name not in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
            continue
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return True
        assert args is not None
        error = map_platform_plan_call_error(session, call.name, args)
        if error is None:
            continue
        logger.warning(
            "Map platform plan protocol violation session=%s frame=%s tool=%s error=%s",
            session.session_id,
            frame.id,
            call.name,
            error,
        )
        frame.messages.append(_tool_message(call.id, error, is_error=True))
        return True
    return False


def _append_reader_fallback_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """reader 耗尽后阻止总控绕过专职 agent 重复读取同一批事实。"""
    # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
    if frame.agent.map_stage != "orchestrator":
        return False
    if session.map_task_state.context_state.get("reader_exhausted") is not True:
        return False
    blocked_names = {
        "describe_map_context",
        "describe_map_region",
        "read_scene_tree",
        "read_file",
        "query_spatial_index",
    }
    blocked_calls = [call for call in calls if call.name in blocked_names]
    if not blocked_calls:
        return False
    message = (
        "map-reader-agent 已达到轮次上限；总控不得绕过职责直接重复读取。"
        "请根据 reader 的部分结构化结果缩小 missing_inputs 后重新委派一次，"
        "或向用户返回明确缺失事实。"
    )
    for call in calls:
        frame.messages.append(_tool_message(call.id, message, is_error=True))
    logger.warning(
        "Blocked map-agent direct read after reader exhaustion session=%s frame=%s tools=%s",
        session.session_id,
        frame.id,
        [call.name for call in blocked_calls],
    )
    return True


_MAP_FOLLOWUP_AGENT_ROLES = frozenset({"map_validator", "map_reviewer"})


def _has_pending_map_write_validation(session: Session) -> bool:
    """判断当前会话是否有写后必须验证的地图阻断。"""
    # 本轮整改：blockers 从 session 顶层迁移到 map_task_state 内部
    for blocker in session.map_task_state.completion_blockers:
        reason = blocker.get("reason")
        if reason in {"map_write_requires_validation", "map_review_required"}:
            return True
        if (
            reason in {"blocking_completion", "completion_not_allowed"}
            and blocker.get("tool") in MAP_WRITE_TOOL_NAMES
        ):
            return True
    return False


def _map_validation_arg_error(session: Session, tool_name: str, args: dict[str, Any]) -> str | None:
    """按写入时声明的通用约束检查验证工具参数。"""
    progress_error = validation_call_error(session, tool_name, args)
    if progress_error is not None:
        return progress_error
    # 本轮整改：blockers 从 session 顶层迁移到 map_task_state 内部
    for blocker in session.map_task_state.completion_blockers:
        raw_constraints = blocker.get("workflow_constraints", [])
        if not isinstance(raw_constraints, list):
            continue
        for constraint in raw_constraints:
            if not isinstance(constraint, dict) or constraint.get("validator") != tool_name:
                continue
            required_args = constraint.get("required_args", {})
            if not isinstance(required_args, dict):
                continue
            for key, value in required_args.items():
                if args.get(key) != value:
                    return f"{tool_name} 必须传 {key}={value!r} 以满足当前地图约束"
    return None


def _is_delegate_map_followup(tool_name: str, args: dict[str, Any]) -> bool:
    """判断委派调用是否只进入地图验证或复核阶段。"""
    task_items: list[dict[str, Any]]
    if tool_name == "delegate":
        task_items = [args]
    elif tool_name == "delegate_many":
        raw_tasks = args.get("tasks")
        task_items = (
            [item for item in raw_tasks if isinstance(item, dict)]
            if isinstance(raw_tasks, list)
            else []
        )
    else:
        return False
    if not task_items:
        return False
    for item in task_items:
        worker_spec = item.get("worker_spec")
        if isinstance(worker_spec, dict):
            # 本轮整改：简化 worker_spec 判断——只有 review_only 模式才属于 followup
            if worker_spec.get("mode") == "review_only":
                continue
            return False
        # 本轮整改：用 _agent_name_has_role 解析 role 代替硬编码 agent name
        agent_name = item.get("agent")
        if not any(_agent_name_has_role(agent_name, role) for role in _MAP_FOLLOWUP_AGENT_ROLES):
            return False
    return True


def _append_map_write_followup_protocol_errors(
    session: Session,
    frame: Frame,
    calls: list[Any],
) -> bool:
    """强制地图写入后的下一阶段必须是验证或复核。"""
    if not _has_pending_map_write_validation(session):
        return False
    for call in calls:
        if call.name in MAP_VALIDATION_TOOL_NAMES:
            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                return True
            assert args is not None
            arg_error = _map_validation_arg_error(session, call.name, args)
            if arg_error is not None:
                frame.messages.append(_tool_message(call.id, arg_error, is_error=True))
                return True
            continue
        if call.name in {"delegate", "delegate_many"}:
            args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
            if parse_error is not None:
                frame.messages.append(parse_error)
                return True
            assert args is not None
            if _is_delegate_map_followup(call.name, args):
                continue
        logger.warning(
            "Map write followup violation session=%s frame=%s agent=%s tool=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
            call.name,
        )
        allowed_tools = sorted(set(frame.agent.effective_tools) & set(MAP_VALIDATION_TOOL_NAMES))
        blocker = (
            session.map_task_state.completion_blockers[0]
            if session.map_task_state.completion_blockers
            else {}
        )
        target = str(blocker.get("target", ""))
        revision = blocker.get("required_revision")
        next_action = (
            f"请调用 {allowed_tools[0]}"
            if allowed_tools
            else "请单独 delegate 给 map-validator-agent 或 map-reviewer-agent"
        )
        context_hint = (
            f"；target_path={target}, expected_revision={revision}"
            if target and revision is not None
            else ""
        )
        for pending_call in calls:
            frame.messages.append(
                _tool_message(
                    pending_call.id,
                    (
                        "地图写入后下一阶段必须先执行 validator/reviewer 或验证工具；"
                        f"本轮工具未执行。{next_action}{context_hint}。"
                        f"当前允许的验证工具：{allowed_tools or ['delegate(map-validator-agent)']}"
                    ),
                    is_error=True,
                )
            )
        return True
    return False
