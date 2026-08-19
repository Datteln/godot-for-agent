"""分类一次 Map 模型响应并路由领域转换。"""

from __future__ import annotations

from app.orchestrator.map_contracts import (
    arm_map_worker_structured_completion,
)
from app.orchestrator.map_turn.contracts import (
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.delegation import _start_delegate_frame
from app.orchestrator.map_turn.delegation_group import _start_delegate_group
from app.orchestrator.map_turn.events import (
    _emit_cache_hit_event,
    _emit_context_usage_event,
    _emit_orchestration_event,
    _event_tool_args,
    _history_timeline_payload,
    _record_cache_metrics,
)
from app.orchestrator.map_turn.frame_info import (
    _map_output_schema_for_frame,
)
from app.orchestrator.map_turn.frame_lifecycle import (
    _finish_frame,
    _handle_frame_turns_exhausted,
)
from app.orchestrator.map_turn.planning import _handle_create_plan
from app.orchestrator.map_turn.runtime import (
    MapModelStep,
    MapToolStep,
    MapTurnContext,
)
from app.orchestrator.map_turn.structured_contracts import MAP_OUTPUT_SCHEMA_V1
from app.orchestrator.map_turn.tool_arguments import _load_tool_args
from app.orchestrator.map_turn.tool_dispatch import (
    _route_unvalidated_platform_writes_to_validator,
)
from app.orchestrator.map_turn.tool_guards import (
    _append_create_plan_protocol_errors,
    _append_delegate_protocol_errors,
    _append_map_plan_protocol_errors,
    _append_map_write_followup_protocol_errors,
    _append_map_write_protocol_errors,
    _append_reader_fallback_protocol_errors,
    _requires_create_plan_before_map_delegate,
)
from app.orchestrator.turn.contracts import (
    ContinueModel,
    ErrorTurnOutcome,
    TurnOutcome,
)
from app.permissions.engine import PermissionContext, check
from app.tools.registry import REGISTRY


async def route_model_response(
    context: MapTurnContext,
    step: MapModelStep,
) -> ContinueModel | TurnOutcome | MapToolStep:
    """分类一次 Map 模型响应并路由领域转换。"""
    session = context.runtime.session
    delegate_artifact_store = context.runtime.delegate_artifact_store
    frame_turns = context.runtime.frame_turns
    frame_edit_map_turns = context.runtime.frame_edit_map_turns
    security = context.services.security
    tool_ctx = context.services.tool_context
    session_allow = context.services.session_allow
    agent_prompt_factory = context.services.prompt_factory
    event_callback = context.services.event_callback
    cache_metrics = context.services.cache_metrics
    context_token_limit = context.options.context_token_limit
    map_worker_structured_output_enabled = context.options.structured_output_enabled
    map_worker_response_contract_mode = context.options.response_contract_mode
    map_worker_structured_correction_limit = context.options.structured_correction_limit
    frame = step.frame
    turn = step.turn
    visible_effective_tools = step.visible_effective_tools
    persistent_map_budget = step.persistent_map_budget
    final_structured_turn = step.final_structured_turn
    resolved_model = step.resolved_model
    effective_thinking_budget = step.effective_thinking_budget
    cache_decision = step.cache_decision
    loop_number = step.loop_number
    frame.messages.append(turn.raw_message)
    if final_structured_turn:
        frame.structured_response_model = turn.model or resolved_model
        frame.structured_finish_reason = turn.finish_reason
        frame.structured_thinking_budget = effective_thinking_budget
        if turn.response_mode is not None:
            frame.response_contract_mode = turn.response_mode
    _record_cache_metrics(cache_metrics, cache_decision, turn)
    _emit_context_usage_event(event_callback, frame, loop_number, turn, context_token_limit)
    _emit_cache_hit_event(event_callback, frame, loop_number, turn)

    if not turn.tool_calls:
        execution = session.map_task_state.codeact_execution
        if frame.agent.name == "map-agent" and execution:
            execution_status = str(execution.get("execution_status", ""))
            if execution_status == "failed_validation":
                return ErrorTurnOutcome(
                    error_code="failed_validation",
                    text="地图 CodeAct 校验未通过，已保留当前 diff，不能报告任务成功。",
                    retryable=False,
                    details={
                        "task_execution_id": execution.get("task_execution_id"),
                        "diff_artifact": execution.get("diff_artifact"),
                        "validation": execution.get("validation"),
                    },
                )
            if execution_status != "validated":
                frame.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "地图 CodeAct 尚未通过最终校验，禁止输出成功结果。"
                            "继续消费 repair_context 并调用统一 CodeAct 工具完成修复与校验。"
                        ),
                    }
                )
                return ContinueModel(reason="map_codeact_validation_required")
        if (
            _map_output_schema_for_frame(frame) == MAP_OUTPUT_SCHEMA_V1
            and not frame.force_text_only
        ):
            arm_map_worker_structured_completion(
                frame,
                mode=map_worker_response_contract_mode,
                correction_limit=(
                    map_worker_structured_correction_limit
                    if map_worker_structured_output_enabled
                    else 0
                ),
            )
            _emit_orchestration_event(
                event_callback,
                "map_structured_final_turn_armed",
                {
                    "frame_id": frame.id,
                    "schema_version": MAP_OUTPUT_SCHEMA_V1,
                    "response_mode": frame.response_contract_mode,
                },
            )
            return ContinueModel(reason="transition_continues")
        finish_result = await _finish_frame(
            session,
            turn.content or "",
            agent_prompt_factory,
            event_callback,
            delegate_artifact_store,
        )
        if finish_result is not None:
            logger.info(
                "Agent run_turn final session=%s loop=%d", session.session_id, loop_number
            )
            return finish_result
        return ContinueModel(reason="transition_continues")

    tool_names = [call.name for call in turn.tool_calls]
    logger.info(
        "Agent requested tools session=%s frame=%s agent=%s names=%s",
        session.session_id,
        frame.id,
        frame.agent.name,
        tool_names,
    )
    _emit_orchestration_event(
        event_callback,
        "agent_tool_calls",
        {
            "frame_id": frame.id,
            "agent": frame.agent.name,
            "tools": tool_names,
        },
    )
    if _append_map_write_protocol_errors(frame, turn.tool_calls):
        return ContinueModel(reason="transition_continues")
    if _append_map_plan_protocol_errors(session, frame, turn.tool_calls):
        return ContinueModel(reason="transition_continues")
    if _append_reader_fallback_protocol_errors(session, frame, turn.tool_calls):
        return ContinueModel(reason="transition_continues")
    if _append_map_write_followup_protocol_errors(session, frame, turn.tool_calls):
        return ContinueModel(reason="transition_continues")

    # edit_map 调用按 edit_map_max_turns 单独计算预算，不挤占该 agent 处理其他
    # 工具（read_scene_tree/截图/规划等）的常规 max_turns 配额；反之亦然。
    is_edit_map_turn = bool(tool_names) and all(name == "edit_map" for name in tool_names)
    if is_edit_map_turn and frame.agent.edit_map_max_turns is not None:
        edit_map_used = (
            frame.persistent_edit_map_turn_count
            if persistent_map_budget
            else frame_edit_map_turns.get(frame.id, 0)
        ) + 1
        if persistent_map_budget:
            frame.persistent_edit_map_turn_count = edit_map_used
        else:
            frame_edit_map_turns[frame.id] = edit_map_used
        if edit_map_used > frame.agent.edit_map_max_turns:
            result = await _handle_frame_turns_exhausted(
                session,
                frame,
                "edit_map 调用次数",
                frame.agent.edit_map_max_turns,
                agent_prompt_factory,
                event_callback,
                delegate_artifact_store,
            )
            if result is not None:
                return result
            return ContinueModel(reason="transition_continues")
    else:
        total_used = (
            frame.persistent_turn_count
            if persistent_map_budget
            else frame_turns.get(frame.id, 0)
        )
        edit_used = (
            frame.persistent_edit_map_turn_count
            if persistent_map_budget
            else frame_edit_map_turns.get(frame.id, 0)
        )
        general_used = total_used - edit_used
        if general_used > frame.agent.max_turns:
            result = await _handle_frame_turns_exhausted(
                session,
                frame,
                "常规轮数",
                frame.agent.max_turns,
                agent_prompt_factory,
                event_callback,
                delegate_artifact_store,
            )
            if result is not None:
                return result
            return ContinueModel(reason="transition_continues")

    permission_ctx = PermissionContext(
        security=security,
        effective_tools=frozenset(visible_effective_tools),
        deny_rules=security.deny_rules,
        allow_rules=security.allow_rules,
        session_allow=session_allow or set(),
    )
    delegate_calls = [
        call for call in turn.tool_calls if call.name in {"delegate", "delegate_many"}
    ]
    if delegate_calls:
        if len(turn.tool_calls) != 1:
            _append_delegate_protocol_errors(frame, turn.tool_calls)
            return ContinueModel(reason="transition_continues")

        call = delegate_calls[0]
        tool = REGISTRY.get(call.name)
        if tool is None:
            logger.warning(
                "Delegate tool missing from registry session=%s tool=%s",
                session.session_id,
                call.name,
            )
            frame.messages.append(
                _tool_message(call.id, f"{call.name} 工具未注册", is_error=True)
            )
            return ContinueModel(reason="transition_continues")

        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return ContinueModel(reason="transition_continues")
        assert args is not None

        if _requires_create_plan_before_map_delegate(session, frame, call.name, args):
            logger.warning(
                "Delegate rejected: complex map task requires create_plan first session=%s frame=%s agent=%s tool=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
                call.name,
            )
            frame.messages.append(
                _tool_message(
                    call.id,
                    "复杂地图任务必须先调用 create_plan 生成用户可见计划；"
                    "本轮委派未执行。请下一轮只调用 create_plan，"
                    "计划步骤应包含读取地图上下文、规划可达路线、预览/确认、小批写入、验证和截图复核。",
                    is_error=True,
                )
            )
            return ContinueModel(reason="transition_continues")

        decision = check(tool, args, permission_ctx)
        if decision == "deny":
            logger.warning(
                "Delegate denied session=%s frame=%s tool=%s agent=%s",
                session.session_id,
                frame.id,
                tool.name,
                frame.agent.name,
            )
            frame.messages.append(
                _tool_message(
                    call.id, "被拒绝：当前 agent/权限模式不允许 delegate", is_error=True
                )
            )
            return ContinueModel(reason="transition_continues")

        _emit_orchestration_event(
            event_callback,
            "delegate_start",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "tool": call.name,
                "args": _event_tool_args(args),
                **_history_timeline_payload(frame),
            },
        )
        if call.name == "delegate_many":
            await _start_delegate_group(
                session=session,
                frame=frame,
                call_id=call.id,
                args=args,
                prompt_factory=agent_prompt_factory,
                event_callback=event_callback,
            )
        else:
            await _start_delegate_frame(
                session=session,
                frame=frame,
                call_id=call.id,
                args=args,
                prompt_factory=agent_prompt_factory,
                event_callback=event_callback,
            )
        return ContinueModel(reason="transition_continues")

    plan_calls = [call for call in turn.tool_calls if call.name == "create_plan"]
    if plan_calls:
        if len(turn.tool_calls) != 1:
            _append_create_plan_protocol_errors(frame, turn.tool_calls)
            return ContinueModel(reason="transition_continues")

        call = plan_calls[0]
        tool = REGISTRY.get(call.name)
        if tool is None:
            logger.warning(
                "Create_plan tool missing from registry session=%s", session.session_id
            )
            frame.messages.append(
                _tool_message(call.id, "create_plan 工具未注册", is_error=True)
            )
            return ContinueModel(reason="transition_continues")

        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            frame.messages.append(parse_error)
            return ContinueModel(reason="transition_continues")
        assert args is not None

        decision = check(tool, args, permission_ctx)
        if decision == "deny":
            logger.warning(
                "Create_plan denied session=%s frame=%s agent=%s",
                session.session_id,
                frame.id,
                frame.agent.name,
            )
            frame.messages.append(
                _tool_message(
                    call.id, "被拒绝：当前 agent/权限模式不允许 create_plan", is_error=True
                )
            )
            return ContinueModel(reason="transition_continues")

        _handle_create_plan(
            session=session,
            frame=frame,
            call_id=call.id,
            args=args,
            event_callback=event_callback,
        )
        return ContinueModel(reason="transition_continues")

    platform_route_handled, platform_validation_response = (
        _route_unvalidated_platform_writes_to_validator(
            session=session,
            frame=frame,
            calls=turn.tool_calls,
            project_root=tool_ctx.security.project_root,
            event_callback=event_callback,
        )
    )
    if platform_validation_response is not None:
        return platform_validation_response
    if platform_route_handled:
        return ContinueModel(reason="transition_continues")
    return MapToolStep(
        frame=frame,
        turn=turn,
        visible_effective_tools=visible_effective_tools,
        persistent_map_budget=persistent_map_budget,
        permission_context=permission_ctx,
    )
