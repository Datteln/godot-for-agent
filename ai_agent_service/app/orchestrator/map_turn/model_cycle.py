"""准备 Map Frame、执行一次模型调用并返回显式阶段结果。"""

from __future__ import annotations

from app.llm.cache_decision_engine import CacheDecision
from app.llm.provider import (
    LLMError,
    ResponseContract,
)
from app.orchestrator.map_contracts import (
    arm_map_worker_structured_completion,
    render_map_worker_response_guidance,
    specialized_map_worker_schema,
)
from app.orchestrator.map_progress import (
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    map_pause_message,
)
from app.orchestrator.map_turn.budgets import (
    _sync_map_progress_budget,
    _uses_persistent_map_budget,
)
from app.orchestrator.map_turn.contracts import (
    logger,
)
from app.orchestrator.map_turn.events import (
    _delta_callback,
    _emit_orchestration_event,
    _fallback_callback,
)
from app.orchestrator.map_turn.frame_info import (
    _map_output_schema_for_frame,
)
from app.orchestrator.map_turn.frame_lifecycle import (
    _finish_frame,
    _handle_frame_turns_exhausted,
)
from app.orchestrator.map_turn.runtime import (
    MapModelStep,
    MapTurnContext,
)
from app.orchestrator.map_turn.structured_contracts import MAP_OUTPUT_SCHEMA_V1
from app.orchestrator.map_turn.tool_dispatch import (
    _stage_effective_tools,
)
from app.orchestrator.map_turn.tool_guards import (
    _planner_route_guard,
)
from app.orchestrator.map_workflow import increment_map_counter
from app.orchestrator.turn.contracts import (
    ContinueModel,
    ErrorTurnOutcome,
    TurnOutcome,
)
from app.orchestrator.turn.model_invocation import ModelInvocation, invoke_model
from app.orchestrator.turn.model_policy import (
    resolve_effort as _resolve_effort,
)
from app.orchestrator.turn.model_policy import (
    resolve_request_model as _resolve_request_model,
)
from app.orchestrator.turn.model_policy import (
    resolve_temperature as _resolve_temperature,
)
from app.orchestrator.turn.model_policy import (
    resolve_thinking_budget,
)
from app.tools.registry import tools_for


async def run_model_cycle(
    context: MapTurnContext,
    loop_index: int,
) -> ContinueModel | TurnOutcome | MapModelStep:
    """准备 Map Frame、执行一次模型调用并返回显式阶段结果。"""
    session = context.runtime.session
    delegate_artifact_store = context.runtime.delegate_artifact_store
    frame_turns = context.runtime.frame_turns
    llm = context.services.llm
    tool_ctx = context.services.tool_context
    agent_prompt_factory = context.services.prompt_factory
    model_selector = context.services.model_selector
    model_override = context.services.model_override
    thinking_budget_selector = context.services.thinking_budget_selector
    event_callback = context.services.event_callback
    cache_engine = context.services.cache_engine
    map_worker_structured_output_enabled = context.options.structured_output_enabled
    map_worker_response_contract_mode = context.options.response_contract_mode
    map_worker_structured_correction_limit = context.options.structured_correction_limit
    map_worker_structured_thinking_budget = context.options.structured_thinking_budget
    frame = session.top_frame()
    if frame is None:
        logger.error("Agent TurnDriver.run failed: empty frame stack session=%s", session.session_id)
        return ErrorTurnOutcome(
            text="会话没有活跃的 agent 帧",
            error_code="missing_agent_frame",
        )

    route_violation = _planner_route_guard(frame, session)
    if route_violation is not None:
        logger.warning(
            "Map route contract violation session=%s frame=%s agent=%s map_stage=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
            frame.agent.map_stage,
        )
        _emit_orchestration_event(
            event_callback,
            "map_route_contract_violation",
            {
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "map_stage": frame.agent.map_stage,
            },
        )
        return route_violation

    if (
        frame.force_text_only
        and _map_output_schema_for_frame(frame) == MAP_OUTPUT_SCHEMA_V1
        and frame.response_contract_mode is None
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

    if frame.forced_completion_text is not None:
        forced_text = frame.forced_completion_text
        frame.forced_completion_text = None
        logger.info(
            "Finishing frame from deterministic completion gate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        forced_result = await _finish_frame(
            session,
            forced_text,
            agent_prompt_factory,
            event_callback,
            delegate_artifact_store,
        )
        if forced_result is not None:
            return forced_result
        return ContinueModel(reason="forced_frame_completed")

    persistent_map_budget = _uses_persistent_map_budget(frame)
    if persistent_map_budget:
        current_scope_owns_task = (
            session.map_request_scope.activates_map_gate
            and session.map_request_scope.map_task_id == session.map_task_state.task_id
        )
        if session.map_task_state.status == "paused" and current_scope_owns_task:
            _emit_orchestration_event(
                event_callback,
                "map_task_paused",
                {
                    "frame_id": frame.id,
                    "pause_kind": session.map_task_state.pause_kind,
                    "reason": session.map_task_state.pause_reason,
                    "pause_report": session.map_task_state.pause_report,
                    "checkpoint": session.map_task_state.checkpoint or {},
                    "counters": session.map_task_state.counters.__dict__,
                },
            )
            return ErrorTurnOutcome(
                text=map_pause_message(session.map_task_state),
                error_code="agent_turn_budget_exhausted",
            )
        _sync_map_progress_budget(session, frame)
        increment_map_counter(session.map_task_state, "llm_turns")
    used = (
        frame.persistent_turn_count if persistent_map_budget else frame_turns.get(frame.id, 0)
    )
    # 这里只做一个宽松的总量护栏（max_turns + edit_map_max_turns），防止帧无限循环；
    # 哪个预算先耗尽由下面 tool_calls 揭晓后的精确分类检查负责。
    structured_budget = (
        frame.structured_correction_limit + 1
        if frame.force_text_only
        and _map_output_schema_for_frame(frame) == MAP_OUTPUT_SCHEMA_V1
        else 0
    )
    total_budget = (
        frame.agent.max_turns + (frame.agent.edit_map_max_turns or 0) + structured_budget
    )
    if used >= total_budget:
        result = await _handle_frame_turns_exhausted(
            session,
            frame,
            "总轮数",
            total_budget,
            agent_prompt_factory,
            event_callback,
            delegate_artifact_store,
        )
        if result is not None:
            return result
        return ContinueModel(reason="frame_budget_transition")

    if persistent_map_budget:
        frame.persistent_turn_count = used + 1
    else:
        frame_turns[frame.id] = used + 1

    try:
        visible_effective_tools = _stage_effective_tools(session, frame)
        visible_tools = tools_for(visible_effective_tools, frame.active_deferred_tools)
        logger.info(
            "Agent frame step session=%s loop=%d frame=%s agent=%s depth=%d messages=%d tools=%d",
            session.session_id,
            loop_index + 1,
            frame.id,
            frame.agent.name,
            frame.depth,
            len(frame.messages),
            len(visible_tools),
        )
        _emit_orchestration_event(
            event_callback,
            "agent_step",
            {
                "loop": loop_index + 1,
                "frame_id": frame.id,
                "agent": frame.agent.name,
                "depth": frame.depth,
                "visible_tools": len(visible_tools),
            },
        )
        effort = _resolve_effort(session, frame)
        resolved_model = _resolve_request_model(
            frame.agent,
            effort,
            model_selector,
            model_override,
        )
        final_structured_turn = (
            frame.force_text_only
            and _map_output_schema_for_frame(frame) == MAP_OUTPUT_SCHEMA_V1
        )
        effective_thinking_budget = (
            map_worker_structured_thinking_budget
            if final_structured_turn and map_worker_structured_thinking_budget > 0
            else resolve_thinking_budget(effort, thinking_budget_selector)
        )
        response_contract = (
            ResponseContract(
                mode=frame.response_contract_mode or map_worker_response_contract_mode,
                schema_name=MAP_OUTPUT_SCHEMA_V1,
                schema=specialized_map_worker_schema(frame),
                fallback_guidance=render_map_worker_response_guidance(
                    frame,
                    "prompt_only",
                ),
            )
            if final_structured_turn
            else None
        )
        if event_callback is not None and resolved_model is not None:
            event_callback(
                "agent_model_selected",
                {
                    "frame_id": frame.id,
                    "loop": loop_index + 1,
                    "model": resolved_model,
                },
            )
        cache_decision: CacheDecision | None = None
        if cache_engine is not None and llm.supports_prompt_cache:
            cache_decision = await cache_engine.decide(
                session_id=session.session_id,
                frame_id=frame.id,
                messages=frame.messages,
                tools=visible_tools,
                project_root=tool_ctx.security.project_root,
                rag_index_path=tool_ctx.rag_index_path,
                compact_digest=(
                    frame.compact_snapshot.digest if frame.compact_snapshot is not None else ""
                ),
            )

        turn = await invoke_model(
            llm,
            ModelInvocation(
                messages=frame.messages,
                tools=visible_tools,
                model=resolved_model,
                temperature=(
                    0.0
                    if final_structured_turn
                    else _resolve_temperature(effort)
                ),
                thinking_budget=effective_thinking_budget,
                on_delta=_delta_callback(
                    event_callback,
                    frame.id,
                    loop_index + 1,
                    len(frame.messages),
                    frame.history_anchor_frame_id or frame.id,
                    (
                        frame.history_anchor_message_index
                        if frame.history_anchor_message_index is not None
                        else len(frame.messages)
                    ),
                ),
                on_fallback=_fallback_callback(
                    event_callback,
                    frame.id,
                    loop_index + 1,
                ),
                cache_breakpoints=(
                    cache_decision.breakpoints
                    if cache_decision is not None and cache_decision.enabled
                    else None
                ),
                response_contract=response_contract,
            ),
        )
    except LLMError as exc:
        logger.warning(
            "Agent LLM step failed session=%s frame=%s error_code=%s "
            "model=%s wire_attempts=%d",
            session.session_id,
            frame.id,
            exc.error_code,
            exc.model or resolved_model,
            exc.wire_attempt_count,
        )
        if (
            session.map_request_scope.activates_map_gate
            and session.map_request_scope.map_task_id == session.map_task_state.task_id
            and session.map_task_state.status == "running"
        ):
            session.map_task_state.make_checkpoint(
                exc.error_code,
                pause_kind="provider_exhausted",
            )
            return ErrorTurnOutcome(
                text=(
                    str(exc)
                    if exc.error_code == "partial_stream_interrupted"
                    else map_pause_message(session.map_task_state)
                ),
                error_code=exc.error_code,
            )
        return ErrorTurnOutcome(text=str(exc), error_code=exc.error_code)
    return MapModelStep(
        loop_number=loop_index + 1,
        frame=frame,
        turn=turn,
        visible_effective_tools=visible_effective_tools,
        persistent_map_budget=persistent_map_budget,
        final_structured_turn=final_structured_turn,
        resolved_model=resolved_model,
        effective_thinking_budget=effective_thinking_budget,
        cache_decision=cache_decision,
    )
