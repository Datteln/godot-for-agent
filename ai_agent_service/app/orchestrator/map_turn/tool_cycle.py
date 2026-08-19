"""执行 Map 工具守卫、服务端调用与前端挂起转换。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from app.codeact.identity import codeact_call_id, task_execution_id
from app.orchestrator.map_contracts import (
    MAP_WORKER_TO_RUNTIME_STAGE,
    arm_map_worker_structured_completion,
)
from app.orchestrator.map_failure_guard import remember_map_tool_failure, repeated_map_tool_failure_error
from app.orchestrator.map_platform_planning import bind_authoritative_snapshot
from app.orchestrator.map_validation import cached_validation_result
from app.orchestrator.map_write_authorization import map_write_stage_error

from app.orchestrator.map_resources import normalize_edit_map_resources
from app.orchestrator.map_turn.budgets import (
    _uses_persistent_map_budget,
)
from app.orchestrator.map_turn.contracts import (
    NOOP_SEARCH_TOOLS_HINT_THRESHOLD,
    FrontToolCall,
    _PendingItem,
    _PendingServerCall,
    _PendingToolMessage,
    _queued_front_call,
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.events import (
    _emit_orchestration_event,
    _event_result_count,
    _event_result_summary,
    _event_tool_args,
    _history_timeline_payload,
)
from app.orchestrator.map_turn.frame_info import (
    _frame_in_active_map_edit,
)
from app.orchestrator.map_turn.runtime import (
    MapToolStep,
    MapTurnContext,
)
from app.orchestrator.map_turn.tool_arguments import _load_tool_args
from app.orchestrator.map_turn.tool_dispatch import (
    _cached_map_region_summary,
    _resumed_full_map_read_error,
    _with_map_write_metadata,
)
from app.orchestrator.map_turn.tool_guards import (
    _map_validation_arg_error,
)
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    MAP_VALIDATION_TOOL_NAMES,
    MAP_WRITE_TOOL_NAMES,
)
from app.orchestrator.map_workflow import replace_map_state_field
from app.orchestrator.turn.contracts import (
    ContinueModel,
    ToolCallsTurnOutcome,
    TurnOutcome,
)
from app.orchestrator.turn.tool_execution import (
    ServerToolCall,
    execute_server_tools,
)
from app.permissions.engine import check, explicit_approval_granted
from app.tools.registry import REGISTRY


async def execute_tool_cycle(
    context: MapTurnContext,
    step: MapToolStep,
) -> ContinueModel | TurnOutcome:
    """执行 Map 工具守卫、服务端调用与前端挂起转换。"""
    session = context.runtime.session
    tool_ctx = context.services.tool_context
    event_callback = context.services.event_callback
    map_worker_structured_output_enabled = context.options.structured_output_enabled
    map_worker_response_contract_mode = context.options.response_contract_mode
    map_worker_structured_correction_limit = context.options.structured_correction_limit
    frame = step.frame
    turn = step.turn
    visible_effective_tools = step.visible_effective_tools
    permission_ctx = step.permission_context
    front_calls: list[FrontToolCall] = []
    pending_items: list[_PendingItem] = []
    turn_id = session.new_turn_id()
    execution_id = task_execution_id(
        session.session_id,
        session.session_epoch,
        frame.id,
    )
    approved_codeact_call_ids: set[str] = set()

    # 第一遍：分类每个 tool call，不执行 server handler（同步、保留顺序）。
    for call in turn.tool_calls:
        tool = REGISTRY.get(call.name)
        if tool is None:
            logger.warning(
                "Unknown tool requested session=%s frame=%s tool=%s",
                session.session_id,
                frame.id,
                call.name,
            )
            pending_items.append(
                _PendingToolMessage(
                    _tool_message(call.id, f"未知工具：{call.name}", is_error=True)
                )
            )
            continue

        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None:
            pending_items.append(_PendingToolMessage(parse_error))
            continue
        assert args is not None
        if tool.name in MAP_WRITE_TOOL_NAMES:
            repeated_error = repeated_map_tool_failure_error(
                session,
                tool.name,
                args,
            )
            if repeated_error is not None:
                logger.warning(
                    "Repeated map tool failure blocked session=%s frame=%s tool=%s",
                    session.session_id,
                    frame.id,
                    tool.name,
                )
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, repeated_error, is_error=True))
                )
                continue
        if tool.name == "edit_map":
            normalization = normalize_edit_map_resources(
                tool_ctx.security.project_root,
                args,
            )
            if normalization.error_code is not None:
                error_message = normalization.error_message or (
                    "地图资源规范化失败，edit_map 未下发。"
                )
                remember_map_tool_failure(
                    session,
                    tool.name,
                    args,
                    normalization.error_code,
                    error_message,
                )
                logger.warning(
                    "Map resource normalization blocked session=%s frame=%s " "error_code=%s",
                    session.session_id,
                    frame.id,
                    normalization.error_code,
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_resource_normalization_blocked",
                    {
                        "frame_id": frame.id,
                        "tool": tool.name,
                        "error_code": normalization.error_code,
                    },
                )
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, error_message, is_error=True))
                )
                continue
            args = normalization.args
            repeated_error = repeated_map_tool_failure_error(
                session,
                tool.name,
                args,
            )
            if repeated_error is not None:
                logger.warning(
                    "Normalized repeated map tool failure blocked "
                    "session=%s frame=%s tool=%s",
                    session.session_id,
                    frame.id,
                    tool.name,
                )
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, repeated_error, is_error=True))
                )
                continue
            if normalization.rewritten_operations:
                logger.info(
                    "Map resources normalized session=%s frame=%s operations=%d",
                    session.session_id,
                    frame.id,
                    normalization.rewritten_operations,
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_resources_normalized",
                    {
                        "frame_id": frame.id,
                        "tool": tool.name,
                        "rewritten_operations": normalization.rewritten_operations,
                    },
                )
        args = _with_map_write_metadata(
            session=session,
            frame=frame,
            call_id=call.id,
            tool_name=tool.name,
            args=args,
        )
        if tool.name in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
            snapshot_error = bind_authoritative_snapshot(
                session,
                tool.name,
                args,
                tool_ctx.security.project_root,
            )
            if snapshot_error is not None:
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, snapshot_error, is_error=True))
                )
                continue
        if tool.name == "describe_map_region":
            cached_region = _cached_map_region_summary(session, args)
            if cached_region is not None:
                logger.info(
                    "Map region read served from cache session=%s frame=%s target=%s layer=%s",
                    session.session_id,
                    frame.id,
                    args.get("target_path"),
                    args.get("map_layer"),
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_cache_hit",
                    {"kind": "region", "target": args.get("target_path")},
                )
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(
                            call.id,
                            {"status": "applied", "result": cached_region},
                        )
                    )
                )
                continue
            resumed_read_error = _resumed_full_map_read_error(session, args)
            if resumed_read_error is not None:
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(call.id, resumed_read_error, is_error=True)
                    )
                )
                continue
        if tool.name in MAP_VALIDATION_TOOL_NAMES:
            cached_validation = cached_validation_result(session, tool.name, args)
            if cached_validation is not None:
                logger.info(
                    "Map validation served from cache session=%s frame=%s target=%s",
                    session.session_id,
                    frame.id,
                    args.get("target_path"),
                )
                _emit_orchestration_event(
                    event_callback,
                    "map_cache_hit",
                    {"kind": "validation", "target": args.get("target_path")},
                )
                pending_items.append(
                    _PendingToolMessage(
                        _tool_message(
                            call.id,
                            {"status": "applied", "result": cached_validation},
                        )
                    )
                )
                continue
            validation_error = _map_validation_arg_error(session, tool.name, args)
            if validation_error is not None:
                logger.warning(
                    "Map validation blocked by progress policy session=%s frame=%s tool=%s error=%s",
                    session.session_id,
                    frame.id,
                    tool.name,
                    validation_error,
                )
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, validation_error, is_error=True))
                )
                continue
        if tool.name in MAP_WRITE_TOOL_NAMES:
            stage_error = (
                None
                if _frame_in_active_map_edit(session, frame)
                else "当前用户请求未显式授权地图内容编辑，地图写工具已拒绝"
            )
            if stage_error is None:
                stage_error = map_write_stage_error(
                    session,
                    tool.name,
                    args,
                    tool_ctx.security.project_root,
                )
            if stage_error is not None:
                logger.warning(
                    "Map write blocked by progress stage session=%s frame=%s tool=%s error=%s",
                    session.session_id,
                    frame.id,
                    tool.name,
                    stage_error,
                )
                pending_items.append(
                    _PendingToolMessage(_tool_message(call.id, stage_error, is_error=True))
                )
                continue

        decision = check(tool, args, permission_ctx)
        logger.info(
            "Tool permission decision session=%s frame=%s tool=%s side=%s decision=%s",
            session.session_id,
            frame.id,
            tool.name,
            tool.side,
            decision,
        )
        if decision == "deny":
            denial_message = (
                f"当前地图工作流处于 {session.map_task_state.stage} 阶段，"
                f"不允许调用 {tool.name}"
                if tool.name not in permission_ctx.effective_tools
                else f"被拒绝：当前权限模式/安全边界不允许调用 {tool.name}"
            )
            pending_items.append(
                _PendingToolMessage(
                    _tool_message(
                        call.id,
                        denial_message,
                        is_error=True,
                    )
                )
            )
            continue

        if tool.side == "server":
            pending_items.append(_PendingServerCall(call_id=call.id, tool=tool, args=args))
            if explicit_approval_granted(tool, args, permission_ctx):
                approved_codeact_call_ids.add(codeact_call_id(execution_id, call.id))
        else:
            front_calls.append(
                FrontToolCall(
                    id=call.id,
                    name=tool.name,
                    input=args,
                    needs_confirm=decision == "ask",
                    frame_id=frame.id,
                    agent=frame.agent.name,
                    render_kind=tool.render_kind,
                )
            )

    # 第二遍：执行 server 工具——`is_concurrency_safe` 的一组用
    # `asyncio.gather` 并发执行，其余按原始顺序串行执行。
    call_ctx = replace(
        tool_ctx,
        effective_tools=frozenset(visible_effective_tools),
        agent_effective_tools=frozenset(frame.agent.effective_tools),
        workflow_stage=(
            MAP_WORKER_TO_RUNTIME_STAGE.get(str(frame.agent.map_stage))
            or (session.map_task_state.stage if _uses_persistent_map_budget(frame) else None)
        ),
        agent_role=frame.agent.role,
        worker_mode=frame.agent.worker_mode,
        task_execution_id=execution_id,
        approved_codeact_call_ids=frozenset(
            set(tool_ctx.approved_codeact_call_ids) | approved_codeact_call_ids
        ),
    )
    server_calls = [item for item in pending_items if isinstance(item, _PendingServerCall)]
    invocations = [
        ServerToolCall(
            call_id=item.call_id,
            tool=item.tool,
            arguments=item.args,
        )
        for item in server_calls
    ]

    def on_server_tool_start(item: ServerToolCall, concurrent: bool) -> None:
        """Project shared tool execution start onto orchestration events."""
        _emit_orchestration_event(
            event_callback,
            "server_tool_start",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "tool": item.tool.name,
                "args": _event_tool_args(item.arguments),
                "concurrent": concurrent,
                **_history_timeline_payload(frame),
            },
        )

    def on_server_tool_result(
        item: ServerToolCall,
        outcome: tuple[Any, bool],
    ) -> None:
        """Project shared tool results without leaking Map behavior into the core."""
        _emit_orchestration_event(
            event_callback,
            "server_tool_result",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "tool": item.tool.name,
                "args": _event_tool_args(item.arguments),
                "is_error": outcome[1],
                "result_count": _event_result_count(*outcome),
                "result_summary": _event_result_summary(item.tool.name, *outcome),
                **_history_timeline_payload(frame),
            },
        )

    results = await execute_server_tools(
        invocations,
        call_ctx,
        on_start=on_server_tool_start,
        on_result=on_server_tool_result,
    )

    if server_calls and event_callback is not None:
        # ponytail: sync event stores do not need flushing; this yields for async transports.
        await asyncio.sleep(0)

    # 第三遍：按 `tool_calls` 原始顺序把结果 append 回 `frame.messages`。
    for item in pending_items:
        if isinstance(item, _PendingToolMessage):
            frame.messages.append(item.message)
            continue

        result, is_error = results[item.call_id]
        if not is_error and item.tool.name == "search_tools":
            activated = {
                str(name)
                for name in result.get("activated_tools", [])
                if name in frame.agent.effective_tools
                and name in REGISTRY
                and REGISTRY[str(name)].deferred
            }
            frame.active_deferred_tools.update(activated)
            result["activated_tools"] = sorted(activated)
            if activated:
                frame.search_tools_noop_count = 0
            else:
                frame.search_tools_noop_count += 1
                if frame.search_tools_noop_count >= NOOP_SEARCH_TOOLS_HINT_THRESHOLD:
                    result["no_more_tools_hint"] = (
                        "search_tools 连续没有激活新工具；若已有足够事实，请输出结果，"
                        "缺失内容写入 missing_inputs。"
                    )
            logger.info(
                "Deferred tools activated session=%s frame=%s tools=%s",
                session.session_id,
                frame.id,
                sorted(activated),
            )
        frame.messages.append(_tool_message(item.call_id, result, is_error=is_error))

    if (
        # 本轮整改：用 map_stage=="reader" 代替 name=="map-reader-agent"
        frame.agent.map_stage == "reader"
        and frame.map_reader_detailed_region_ready
        and not frame.force_text_only
    ):
        artifact_read_completed = any(
            item.tool.name == "read_file"
            and not results[item.call_id][1]
            and ".ai_agent_service/artifacts/"
            in str(item.args.get("path", "")).replace("\\", "/")
            for item in server_calls
        )
        if artifact_read_completed:
            arm_map_worker_structured_completion(
                frame,
                mode=map_worker_response_contract_mode,
                correction_limit=(
                    map_worker_structured_correction_limit
                    if map_worker_structured_output_enabled
                    else 0
                ),
            )
            logger.info(
                "Map reader armed for text-only completion session=%s frame=%s",
                session.session_id,
                frame.id,
            )

    if front_calls:
        if len(front_calls) > 1 and all(
            call.name in MAP_WRITE_TOOL_NAMES for call in front_calls
        ):
            state = session.map_task_state
            replace_map_state_field(
                state,
                "plan_version",
                max(1, state.plan_version),
            )
            for batch_index, call in enumerate(front_calls):
                call.input.setdefault("plan_version", state.plan_version)
                call.input.setdefault("batch_index", batch_index)
            replace_map_state_field(
                state,
                "pending_batches",
                [_queued_front_call(call) for call in front_calls[1:]],
            )
            front_calls = front_calls[:1]
            assistant_message = frame.messages[-1] if frame.messages else {}
            raw_tool_calls = assistant_message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                assistant_message["tool_calls"] = [
                    item
                    for item in raw_tool_calls
                    if isinstance(item, dict) and item.get("id") == front_calls[0].id
                ]
            logger.info(
                "Map batch queue created session=%s plan_version=%d pending=%d",
                session.session_id,
                state.plan_version,
                len(state.pending_batches),
            )
            _emit_orchestration_event(
                event_callback,
                "map_batch_queue_created",
                {
                    "plan_version": state.plan_version,
                    "batch_count": len(state.pending_batches) + 1,
                },
            )
        session.set_pending(
            turn_id,
            [c.id for c in front_calls],
            {
                c.id: {
                    "name": c.name,
                    "input": c.input,
                    "frame_id": c.frame_id,
                    "agent": c.agent,
                    # 本轮整改：持久化 needs_confirm 标志，
                    # 前端恢复时可据此区分自动执行与需用户确认的调用
                    "needs_confirm": c.needs_confirm,
                }
                for c in front_calls
            },
        )
        logger.info(
            "Front tool calls pending session=%s turn_id=%s count=%d needs_confirm=%d",
            session.session_id,
            turn_id,
            len(front_calls),
            sum(1 for call in front_calls if call.needs_confirm),
        )
        return ToolCallsTurnOutcome(
            turn_id=turn_id,
            text=turn.content,
            calls=tuple(_queued_front_call(call) for call in front_calls),
        )
    return ContinueModel(reason="server_tools_completed")
