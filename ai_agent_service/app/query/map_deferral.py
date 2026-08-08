"""地图工具延迟/恢复：读先于写/验证、完成闸门与子 Frame 调度。"""

from __future__ import annotations

from ._map_derivation import (
    _MAP_REGION_READ_GUARDED_TOOL_NAMES,
    _MAP_VALIDATION_REPEAT_LIMIT,
    _current_map_region_signature,
    _map_layer_from_result,
    _map_region_from_tool_args,
    _map_region_from_write_args,
    _map_region_read_signature,
    _map_revision_from_result,
    _map_target_from_result,
    _map_tool_missing_required_context,
    _resolved_map_tool_args,
)
from ._text_utils import logger
from .map_session_state import _blocker_revision, _same_map_target
from .message_utils import AgentPromptFactory, _append_assistant_tool_calls, _append_map_state_read_error, _replace_last_assistant_tool_calls
from .tool_summary import _map_result_summary
from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatToolCallsResponse, FrontToolCallDTO
from app.orchestrator.frame_factory import create_child_frame, typed_child_task_text
from app.orchestrator.map_context import latest_map_revision, record_map_child_lineage
from app.orchestrator.map_contracts import MAP_RUNTIME_STAGE_TRANSITIONS, MAP_WORKER_TO_RUNTIME_STAGE
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES, MAP_VALIDATION_TOOL_NAMES
from app.orchestrator.map_workflow import replace_map_state_field
from app.sessions.store import Session
from app.tools.registry import REGISTRY
from dataclasses import replace
from typing import Any
def _has_review_blocker(blockers: list[dict[str, Any]], target: str, revision: int | None) -> bool:
    """判断是否已有同目标同版本的 reviewer 阻断。"""
    for blocker in blockers:
        if blocker.get("reason") != "map_review_required":
            continue
        if not _same_map_target(blocker, target):
            continue
        blocker_revision = _blocker_revision(blocker)
        if revision is None or blocker_revision is None or revision == blocker_revision:
            return True
    return False


def _review_required_blocker(
    tool_name: str,
    target: str,
    revision: int | None,
    region: dict[str, int] | None = None,
) -> dict[str, Any]:
    """生成验证通过后的视觉复核阻断项。

    region 参数用于携带触发复核的具体区域坐标，供 reviewer 子帧
    在 map_stage_contract 中获取需要截图检查的精确范围。
    """
    return {
        "tool": tool_name,
        "reason": "map_review_required",
        "issues": ["same-revision validation passed; reviewer visual check is still required"],
        "target": target,
        "required_revision": revision,
        "region": dict(region or {}),
    }


def _remember_latest_map_region_read(
    session: Session,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录最近读过的地图区域，避免 frontier 计算在未读区域上猜 start。"""
    normalized_args = dict(tool_args)
    if isinstance(result, dict):
        target = _map_target_from_result(tool_args, result)
        layer = _map_layer_from_result(result)
        if target:
            normalized_args["target_path"] = target
        if layer is not None:
            normalized_args["map_layer"] = layer
    signatures: list[str] = []
    requested_signature = _map_region_read_signature("describe_map_region", tool_args)
    if requested_signature is not None:
        signatures.append(requested_signature)
    normalized_signature = _map_region_read_signature("describe_map_region", normalized_args)
    if normalized_signature is not None and normalized_signature not in signatures:
        signatures.append(normalized_signature)
    if not signatures:
        return
    revision = _map_revision_from_result(result) if isinstance(result, dict) else None
    if revision is None:
        return
    summary = (
        _map_result_summary("describe_map_region", result, None)
        if isinstance(result, dict)
        else None
    )
    region_reads = dict(session.map_task_state.region_reads)
    region_summaries = dict(session.map_task_state.region_summaries)
    for signature in signatures:
        region_reads[signature] = revision
        if summary is not None:
            region_summaries[signature] = summary
    while len(region_reads) > 64:
        first_key = next(iter(region_reads))
        del region_reads[first_key]
        region_summaries.pop(first_key, None)
    target = str(tool_args.get("target_path", ""))
    replace_map_state_field(
        session.map_task_state,
        "region_reads",
        region_reads,
        target=target or None,
        revision=revision,
    )
    replace_map_state_field(
        session.map_task_state,
        "region_summaries",
        region_summaries,
        target=target or None,
        revision=revision,
    )


def _latest_map_region_summary_for_call(
    session: Session,
    call: FrontToolCallDTO,
) -> dict[str, Any] | None:
    resolved_input = _resolved_map_tool_args(session, call.input)
    signature = _map_region_read_signature(call.name, resolved_input)
    if signature is None:
        return None
    target = resolved_input.get("target_path")
    if not isinstance(target, str):
        target = ""
    cached_signature = _current_map_region_signature(session, signature, target)
    if cached_signature is None:
        return None
    summary = session.map_task_state.region_summaries.get(cached_signature)
    return summary if isinstance(summary, dict) else None


def _blocks_platform_plan_after_empty_region_read(
    session: Session,
    call: FrontToolCallDTO,
) -> bool:
    if call.name not in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
        return False
    summary = _latest_map_region_summary_for_call(session, call)
    if summary is None:
        return False
    try:
        non_empty_count = int(summary.get("non_empty_count", 0))
    except (TypeError, ValueError):
        non_empty_count = 0
    if non_empty_count > 0:
        return False
    session.pending_map_tool_after_read = None
    _append_map_state_read_error(
        session,
        call.name,
        str(call.input.get("target_path", "")),
        "non_empty_cells in the entry sample; choose the foreground map_layer or move/expand entry_sample_* before planning",
    )
    logger.info(
        "Blocked platform plan after empty region read session=%s tool=%s target=%s layer=%s",
        session.session_id,
        call.name,
        call.input.get("target_path"),
        call.input.get("map_layer"),
    )
    return True


def _map_tool_region_read_current(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图工具依赖的区域是否已按当前 revision 读取。"""
    resolved_input = _resolved_map_tool_args(session, call.input)
    if _map_tool_missing_required_context(session, call.name, resolved_input):
        return False
    signature = _map_region_read_signature(call.name, resolved_input)
    if signature is None:
        return True
    target = resolved_input.get("target_path")
    if not isinstance(target, str) or not target:
        return signature in session.map_task_state.region_reads
    return _current_map_region_signature(session, signature, target) is not None


def _map_region_read_call_for_tool(
    session: Session,
    call: FrontToolCallDTO,
) -> FrontToolCallDTO | None:
    """把地图工具调用转换为同一区域的 describe_map_region 调用。"""
    resolved_input = _resolved_map_tool_args(session, call.input)
    region = _map_region_from_tool_args(call.name, resolved_input)
    if region is None:
        return None
    read_input: dict[str, Any] = {
        "__auto_map_state_read": True,
        "cells_format": "non_empty_only",
        "max_returned_cells": 120,
    }
    for key in ("target_path", "map_layer", "ground_map_layer"):
        if key in resolved_input:
            read_input["map_layer" if key == "ground_map_layer" else key] = resolved_input[key]
    read_input.update(region)
    return FrontToolCallDTO(
        id=f"{call.id}__map_region_read",
        name="describe_map_region",
        input=read_input,
        needs_confirm=False,
        frame_id=call.frame_id,
        agent=call.agent,
        render_kind="json",
    )


def _defer_map_tool_for_region_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """强制依赖真实地图区域的工具在同一区域 describe_map_region 之后执行。"""
    if session.pending_map_tool_after_read is not None:
        return response
    guarded_call = next(
        (call for call in response.calls if call.name in _MAP_REGION_READ_GUARDED_TOOL_NAMES),
        None,
    )
    if guarded_call is None:
        return response
    if _map_tool_region_read_current(session, guarded_call):
        return response
    read_call = _map_region_read_call_for_tool(session, guarded_call)
    if read_call is None:
        return response
    session.pending_map_tool_after_read = {"call": guarded_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取真实地图区域，再执行地图计算/编辑工具。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
                "needs_confirm": False,
            }
        },
    )
    logger.info(
        "Deferred map tool for region read session=%s tool=%s target=%s read_call=%s",
        session.session_id,
        guarded_call.name,
        guarded_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _resume_pending_map_tool_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完地图区域后恢复此前挂起的地图工具调用。"""
    pending = session.pending_map_tool_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_tool_after_read = None
        return None
    call = FrontToolCallDTO.model_validate(raw_call)
    restored_input = _resolved_map_tool_args(session, call.input)
    target = restored_input.get("target_path")
    target_path = target if isinstance(target, str) else ""
    # 恢复时优先使用调用自身携带的 map_layer，回退到会话中记录的最近图层，
    # 再据此查询图层感知的最新 revision，保证多图层场景下恢复正确的上下文。
    latest_layer = session.map_task_state.latest_layers.get(target_path)
    input_layer = restored_input.get("map_layer", latest_layer)
    scoped_layer = (
        input_layer if isinstance(input_layer, int) and not isinstance(input_layer, bool) else None
    )
    latest_revision = latest_map_revision(session, target_path, scoped_layer)
    if (
        call.name
        in {
            "validate_platform_level_plan",
            "plan_reachable_map_growth",
            "compute_reachable_frontier",
        }
        and latest_layer is not None
        and "map_layer" not in restored_input
    ):
        restored_input["map_layer"] = latest_layer

    if call.name in MAP_REVISION_GUARDED_TOOL_NAMES:
        if not target_path or latest_revision is None:
            session.pending_map_tool_after_read = None
            _append_map_state_read_error(
                session,
                call.name,
                target_path,
                "expected_revision",
            )
            return None
        restored_input["expected_revision"] = latest_revision
    if "map_layer" not in restored_input and latest_layer is not None:
        restored_input["map_layer"] = latest_layer
    missing_context = _map_tool_missing_required_context(session, call.name, restored_input)
    if missing_context:
        session.pending_map_tool_after_read = None
        _append_map_state_read_error(
            session,
            call.name,
            target_path,
            missing_context,
        )
        return None
    if call.name in MAP_VALIDATION_TOOL_NAMES and "map_layer" not in restored_input:
        session.pending_map_tool_after_read = None
        _append_map_state_read_error(session, call.name, target_path, "map_layer")
        return None

    restored_call = call.model_copy(update={"input": restored_input})
    if not _map_tool_region_read_current(session, restored_call):
        read_call = _map_region_read_call_for_tool(session, restored_call)
        if read_call is None:
            session.pending_map_tool_after_read = None
            _append_map_state_read_error(
                session,
                call.name,
                target_path,
                "target_path/map_layer/region_context",
            )
            return None
        text = "已确认地图图层，继续读取带图层的真实地图区域。"
        turn_id = session.new_turn_id()
        _append_assistant_tool_calls(session, text, [read_call])
        session.set_pending(
            turn_id,
            [read_call.id],
            {
                read_call.id: {
                    "name": read_call.name,
                    "input": read_call.input,
                    "frame_id": read_call.frame_id,
                    "agent": read_call.agent,
                    "needs_confirm": False,
                }
            },
        )
        logger.info(
            "Continuing pending map tool region read session=%s tool=%s target=%s layer=%s read_call=%s",
            session.session_id,
            restored_call.name,
            target_path,
            restored_input.get("map_layer"),
            read_call.id,
        )
        return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[read_call])

    text = "已读取真实地图区域，继续执行挂起的地图工具调用。"
    if _blocks_platform_plan_after_empty_region_read(session, restored_call):
        return None

    turn_id = session.new_turn_id()
    session.pending_map_tool_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
                "needs_confirm": restored_call.needs_confirm,
            }
        },
    )
    logger.info(
        "Resumed pending map tool after region read session=%s tool=%s target=%s revision=%s layer=%s",
        session.session_id,
        restored_call.name,
        target_path,
        latest_revision,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


def _needs_map_state_read_before_write(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图写工具是否需要先自动读取 map_layer/map_revision。

    改动说明：revision 查询改为图层感知——优先使用 call.input 中显式传入的 map_layer，
    回退到 session 中记录的 latest_layers，再通过 latest_map_revision() 按 scope key 查询，
    确保多图层场景下不会误判 revision 缺失。
    """
    if call.name not in MAP_REVISION_GUARDED_TOOL_NAMES:
        return False
    target = call.input.get("target_path")
    if not isinstance(target, str) or not target:
        return False
    # 优先取显式 map_layer，回退到会话记录的最新图层
    input_layer = call.input.get("map_layer")
    map_layer = (
        input_layer
        if isinstance(input_layer, int) and not isinstance(input_layer, bool)
        else session.map_task_state.latest_layers.get(target)
    )
    missing_revision = latest_map_revision(session, target, map_layer) is None
    missing_layer = (
        "map_layer" not in call.input and target not in session.map_task_state.latest_layers
    )
    return missing_revision or missing_layer


def _map_state_read_call_for_write(
    session: Session,
    write_call: FrontToolCallDTO,
) -> FrontToolCallDTO:
    """为挂起的地图写调用构造自动状态读取调用。"""
    target = str(write_call.input.get("target_path", ""))
    read_input: dict[str, Any] = {
        "target_path": target,
        "__auto_map_state_read": True,
    }
    latest_layer = session.map_task_state.latest_layers.get(target)
    if latest_layer is not None:
        read_input["map_layer"] = latest_layer
    region = _map_region_from_write_args(write_call.input, {})
    if region is not None:
        read_input.update(region)
    return FrontToolCallDTO(
        id=f"{write_call.id}__map_state_read",
        name="describe_map_region",
        input=read_input,
        needs_confirm=False,
        frame_id=write_call.frame_id,
        agent=write_call.agent,
        render_kind="json",
    )


def _defer_map_write_for_state_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """把缺少地图状态的写调用挂起，先返回自动 describe_map_region。"""
    if session.pending_map_write_after_read is not None:
        return response
    write_call = next(
        (call for call in response.calls if _needs_map_state_read_before_write(session, call)),
        None,
    )
    if write_call is None:
        return response
    read_call = _map_state_read_call_for_write(session, write_call)
    session.pending_map_write_after_read = {"call": write_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取地图当前状态，再恢复挂起的地图写入。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
                "needs_confirm": False,
            }
        },
    )
    logger.info(
        "Deferred map write for state read session=%s write_tool=%s target=%s read_call=%s",
        session.session_id,
        write_call.name,
        write_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _resume_pending_map_write_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完 map state 后恢复此前挂起的地图写调用。"""
    pending = session.pending_map_write_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_write_after_read = None
        return None
    write_call = FrontToolCallDTO.model_validate(raw_call)
    target = write_call.input.get("target_path")
    if not isinstance(target, str) or not target:
        session.pending_map_write_after_read = None
        return None
    # 与 _resume_pending_map_tool_after_read 相同逻辑：
    # 根据写调用自身的 map_layer 或会话记录的最新图层，查询图层感知的 revision。
    latest_layer = session.map_task_state.latest_layers.get(target)
    input_layer = write_call.input.get("map_layer", latest_layer)
    map_layer = (
        input_layer if isinstance(input_layer, int) and not isinstance(input_layer, bool) else None
    )
    latest_revision = latest_map_revision(session, target, map_layer)
    if latest_revision is None or ("map_layer" not in write_call.input and latest_layer is None):
        missing = []
        if latest_revision is None:
            missing.append("expected_revision")
        if "map_layer" not in write_call.input and latest_layer is None:
            missing.append("map_layer")
        session.pending_map_write_after_read = None
        _append_map_state_read_error(session, write_call.name, target, "/".join(missing))
        return None
    restored_input = dict(write_call.input)
    restored_input["expected_revision"] = latest_revision
    if "map_layer" not in restored_input and latest_layer is not None:
        restored_input["map_layer"] = latest_layer
    restored_call = write_call.model_copy(update={"input": restored_input})
    text = "已读取地图当前状态，继续执行挂起的地图写入。"
    turn_id = session.new_turn_id()
    session.pending_map_write_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
                "needs_confirm": restored_call.needs_confirm,
            }
        },
    )
    logger.info(
        "Resumed pending map write after state read session=%s tool=%s target=%s revision=%s layer=%s",
        session.session_id,
        restored_call.name,
        target,
        latest_revision,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


def _needs_map_state_read_before_validation(session: Session, call: FrontToolCallDTO) -> bool:
    """判断地图校验工具是否需要先自动读取 map_layer。"""
    if call.name not in MAP_VALIDATION_TOOL_NAMES:
        return False
    target = call.input.get("target_path")
    return (
        isinstance(target, str)
        and bool(target)
        and "map_layer" not in call.input
        and target not in session.map_task_state.latest_layers
    )


def _defer_map_validation_for_state_read(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """把缺少图层的地图校验调用挂起，先返回自动 describe_map_region。"""
    if (
        session.pending_map_write_after_read is not None
        or session.pending_map_validation_after_read is not None
    ):
        return response
    validation_call = next(
        (call for call in response.calls if _needs_map_state_read_before_validation(session, call)),
        None,
    )
    if validation_call is None:
        return response
    read_call = _map_state_read_call_for_write(session, validation_call)
    session.pending_map_validation_after_read = {"call": validation_call.model_dump()}
    replacement = ChatToolCallsResponse(
        turn_id=response.turn_id,
        text="先读取地图图层状态，再恢复挂起的地图校验。",
        calls=[read_call],
    )
    _replace_last_assistant_tool_calls(session, replacement.text, replacement.calls)
    session.set_pending(
        replacement.turn_id,
        [read_call.id],
        {
            read_call.id: {
                "name": read_call.name,
                "input": read_call.input,
                "frame_id": read_call.frame_id,
                "agent": read_call.agent,
                "needs_confirm": False,
            }
        },
    )
    logger.info(
        "Deferred map validation for state read session=%s validation_tool=%s target=%s read_call=%s",
        session.session_id,
        validation_call.name,
        validation_call.input.get("target_path"),
        read_call.id,
    )
    return replacement


def _bind_map_validation_to_pending_write(
    session: Session,
    response: ChatToolCallsResponse,
) -> ChatToolCallsResponse:
    """将写后校验绑定到同一地图目标与图层，避免错误路径触发自动重读。"""
    pending = next(
        (
            blocker
            for blocker in session.map_task_state.completion_blockers
            if blocker.get("reason") == "map_write_requires_validation"
            and isinstance(blocker.get("target"), str)
            and blocker["target"]
        ),
        None,
    )
    if pending is None:
        return response

    target_path = str(pending["target"])
    map_layer = pending.get("map_layer")
    calls: list[FrontToolCallDTO] = []
    changed = False
    for call in response.calls:
        if call.name not in MAP_VALIDATION_TOOL_NAMES:
            calls.append(call)
            continue
        input_data = dict(call.input)
        if input_data.get("target_path") != target_path:
            input_data["target_path"] = target_path
            changed = True
        if isinstance(map_layer, int) and not isinstance(map_layer, bool):
            if input_data.get("map_layer") != map_layer:
                input_data["map_layer"] = map_layer
                changed = True
        calls.append(call.model_copy(update={"input": input_data}))

    if not changed:
        return response
    bound = response.model_copy(update={"calls": calls})
    _replace_last_assistant_tool_calls(session, bound.text, bound.calls)
    logger.info(
        "Bound map validation to pending write session=%s target=%s layer=%s",
        session.session_id,
        target_path,
        map_layer,
    )
    return bound


def _resume_pending_map_validation_after_read(session: Session) -> ChatToolCallsResponse | None:
    """自动读完 map layer 后恢复此前挂起的地图校验调用。"""
    pending = session.pending_map_validation_after_read
    if not isinstance(pending, dict):
        return None
    raw_call = pending.get("call")
    if not isinstance(raw_call, dict):
        session.pending_map_validation_after_read = None
        return None
    validation_call = FrontToolCallDTO.model_validate(raw_call)
    target = validation_call.input.get("target_path")
    if not isinstance(target, str) or not target:
        session.pending_map_validation_after_read = None
        return None
    latest_layer = session.map_task_state.latest_layers.get(target)
    if latest_layer is None:
        session.pending_map_validation_after_read = None
        _append_map_state_read_error(session, validation_call.name, target, "map_layer")
        return None
    restored_input = dict(validation_call.input)
    restored_input.setdefault("map_layer", latest_layer)
    restored_call = validation_call.model_copy(update={"input": restored_input})
    text = "已读取地图图层状态，继续执行挂起的地图校验。"
    turn_id = session.new_turn_id()
    session.pending_map_validation_after_read = None
    _append_assistant_tool_calls(session, text, [restored_call])
    session.set_pending(
        turn_id,
        [restored_call.id],
        {
            restored_call.id: {
                "name": restored_call.name,
                "input": restored_call.input,
                "frame_id": restored_call.frame_id,
                "agent": restored_call.agent,
                "needs_confirm": restored_call.needs_confirm,
            }
        },
    )
    logger.info(
        "Resumed pending map validation after state read session=%s tool=%s target=%s layer=%s",
        session.session_id,
        restored_call.name,
        target,
        restored_input.get("map_layer"),
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=text, calls=[restored_call])


async def _schedule_revision_conflict_reader(
    session: Session,
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
    prompt_factory: AgentPromptFactory | None = None,
) -> None:
    """在 revision 冲突后自动压入 map-reader-agent 重读帧。

    改动说明：
    - 改为 async 以支持 prompt_factory 异步生成 prompt。
    - 子帧创建统一走 frame_factory.create_child_frame，不再手动拼装 Frame，
      确保 history_anchor、parent_id 等字段由工厂统一管理。
    - 任务描述从硬编码的 instruction 字段改为 objective 字段，
      具体 prompt 拼装由 prompt_factory（运行时注入）负责，实现调度与提示词解耦。
    - 通过 map_stage_contract 将 stage/target/revision 等上下文传递给子帧，
      由运行时统一消费，不再依赖 prompt 文本中的隐式指令。
    """
    result_dict = result if isinstance(result, dict) else {}
    target = str(tool_args.get("target_path", result_dict.get("target_path", "")))
    region = _map_region_from_write_args(tool_args, result_dict)
    try:
        reader = get_agent("map-reader-agent", set(REGISTRY))
    except KeyError:
        frame.messages.append(
            {
                "role": "user",
                "content": "map_revision_conflict：map-reader-agent 未注册，请先手动重读冲突区域。",
            }
        )
        return
    expected_task_stage = session.map_task_state.stage
    reader_task_stage = MAP_WORKER_TO_RUNTIME_STAGE["reader"]
    if reader_task_stage not in MAP_RUNTIME_STAGE_TRANSITIONS.get(expected_task_stage, frozenset()):
        frame.messages.append(
            {
                "role": "system",
                "internal": True,
                "content": (
                    "自动 revision reader 阶段预检失败："
                    f"{expected_task_stage} -> {reader_task_stage}"
                ),
            }
        )
        return
    task_text = typed_child_task_text(
        "重新读取 revision 冲突影响的地图区域。",
        {
            "reason": "map_revision_conflict",
            "failed_tool": tool_name,
            "target_path": target,
            "region": region,
            "expected_revision": result_dict.get("expected_revision"),
            "actual_revision": result_dict.get("actual_revision"),
        },
    )
    # 通过 prompt_factory 动态生成 reader prompt；若未提供则回退到 agent 默认 prompt。
    # ValueError 表示 prompt 构建失败（如缺少必要上下文），此时写入错误消息并中止调度。
    try:
        prompt = (
            await prompt_factory(reader, task_text) if prompt_factory is not None else reader.prompt
        )
    except ValueError as exc:
        frame.messages.append(
            {
                "role": "system",
                "internal": True,
                "content": f"自动 revision reader 创建失败：{exc}",
            }
        )
        return
    reader = replace(reader, prompt=prompt)
    # 使用 frame_factory 创建子帧，自动继承父帧的 history_anchor 等上下文
    child = create_child_frame(
        session=session,
        parent=frame,
        agent=reader,
        task_text=task_text,
        depth=frame.depth + 1,
        map_stage_contract={
            "stage": "reader",
            "target_path": target,
            "map_revision": result_dict.get("actual_revision"),
        },
    )
    actual_revision = result_dict.get("actual_revision")
    record_map_child_lineage(
        session.map_task_state,
        child_frame_id=child.id,
        child_stage="reader",
        task_stage=reader_task_stage,
        expected_task_stage=expected_task_stage,
        target=target,
        revision=(
            actual_revision
            if isinstance(actual_revision, int) and not isinstance(actual_revision, bool)
            else None
        ),
    )
    session.agent_stack.append(child)


def _pop_last_assistant_final(session: Session) -> None:
    """移除刚被完成门拦截的 assistant final，避免错误完成陈述进入后续上下文。"""
    frame = session.top_frame()
    if frame is None or not frame.messages:
        return
    last = frame.messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        frame.messages.pop()


async def _schedule_map_reviewer_if_required(
    session: Session,
    prompt_factory: AgentPromptFactory | None = None,
) -> bool:
    """把 map_review_required 阻断转换为 reviewer 子帧继续执行。

    改动说明：与 _schedule_revision_conflict_reader 相同的模式——
    改 async、走 frame_factory、通过 prompt_factory 注入 prompt、
    用 map_stage_contract 传递结构化上下文，不再依赖 prompt 中的硬编码指令。
    通过 frame.agent.map_stage（而非 agent.name）判断是否已在 reviewer 帧中，
    避免对 agent 名称的硬编码依赖。
    """
    frame = session.top_frame()
    # 使用 map_stage 而非 agent.name 判断，更健壮地防止 reviewer 帧内重复调度
    if frame is None or frame.agent.map_stage == "reviewer":
        return False
    blocker = next(
        (
            item
            for item in session.map_task_state.completion_blockers
            if item.get("reason") == "map_review_required"
        ),
        None,
    )
    if blocker is None:
        return False
    try:
        reviewer = get_agent("map-reviewer-agent", set(REGISTRY))
    except KeyError:
        return False
    expected_task_stage = session.map_task_state.stage
    reviewer_task_stage = MAP_WORKER_TO_RUNTIME_STAGE["reviewer"]
    if reviewer_task_stage not in MAP_RUNTIME_STAGE_TRANSITIONS.get(
        expected_task_stage, frozenset()
    ):
        return False
    _pop_last_assistant_final(session)
    # 任务载荷只携带结构化字段（reason/target/revision/region/objective），
    # 具体的 prompt 文本由 prompt_factory 负责拼装，实现调度逻辑与提示词解耦。
    task_text = typed_child_task_text(
        "复核当前 revision 的地图视觉结果并提交证据。",
        {
            "reason": "map_review_required",
            "target_path": str(blocker.get("target", "")),
            "required_revision": blocker.get("required_revision"),
            "region": blocker.get("region", {}),
        },
    )
    try:
        prompt = (
            await prompt_factory(reviewer, task_text)
            if prompt_factory is not None
            else reviewer.prompt
        )
    except ValueError as exc:
        frame.messages.append(
            {
                "role": "system",
                "internal": True,
                "content": f"自动 reviewer 创建失败：{exc}",
            }
        )
        return False
    reviewer = replace(reviewer, prompt=prompt)
    child = create_child_frame(
        session=session,
        parent=frame,
        agent=reviewer,
        task_text=task_text,
        depth=frame.depth + 1,
        map_stage_contract={
            "stage": "reviewer",
            "target_path": str(blocker.get("target", "")),
            "map_revision": blocker.get("required_revision"),
            "region": blocker.get("region", {}),
        },
    )
    required_revision = blocker.get("required_revision")
    record_map_child_lineage(
        session.map_task_state,
        child_frame_id=child.id,
        child_stage="reviewer",
        task_stage=reviewer_task_stage,
        expected_task_stage=expected_task_stage,
        target=str(blocker.get("target", "")),
        revision=(
            required_revision
            if isinstance(required_revision, int) and not isinstance(required_revision, bool)
            else None
        ),
    )
    session.agent_stack.append(child)
    return True


def _has_map_review_required(blockers: list[dict[str, Any]]) -> bool:
    """判断完成门阻断里是否包含待视觉复核项。"""
    return any(item.get("reason") == "map_review_required" for item in blockers)


def _has_only_map_review_required(blockers: list[dict[str, Any]]) -> bool:
    """Return true when visual review is the only remaining map completion blocker."""
    return bool(blockers) and all(item.get("reason") == "map_review_required" for item in blockers)


def _format_map_completion_blockers_for_prompt(blockers: list[dict[str, Any]]) -> str:
    """Build a compact, model-facing blocker list for a continuation turn."""
    lines: list[str] = []
    for index, blocker in enumerate(blockers[:5], start=1):
        tool = str(blocker.get("tool", "map tool"))
        reason = str(blocker.get("reason", "blocked"))
        target = str(blocker.get("target", ""))
        revision = blocker.get("required_revision")
        next_stage = str(blocker.get("next_stage", ""))
        issues = blocker.get("issues", [])
        issue_text = ""
        if isinstance(issues, list) and issues:
            issue_text = "; ".join(str(issue) for issue in issues[:4] if str(issue).strip())
        parts = [f"{index}. tool={tool}", f"reason={reason}"]
        if target:
            parts.append(f"target={target}")
        if isinstance(revision, int) and not isinstance(revision, bool):
            parts.append(f"map_revision={revision}")
        if next_stage:
            parts.append(f"next_stage={next_stage}")
        if issue_text:
            parts.append(f"issues={issue_text}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _schedule_map_completion_continuation(session: Session, *, discard_final: bool = True) -> bool:
    """把地图校验失败原因交给根 agent，并安排下一轮修复。"""
    frame = session.top_frame()
    if frame is None:
        return False
    if any(
        blocker.get("reason") == "map_validation_repeat_limit"
        or (
            isinstance(blocker.get("repeat_count"), int)
            and blocker.get("repeat_count", 0) >= _MAP_VALIDATION_REPEAT_LIMIT
        )
        for blocker in session.map_task_state.completion_blockers
    ):
        logger.warning(
            "Stopped repeated map validation continuation session=%s blockers=%s",
            session.session_id,
            session.map_task_state.completion_blockers,
        )
        return False
    if discard_final:
        _pop_last_assistant_final(session)
    blocker_text = _format_map_completion_blockers_for_prompt(
        session.map_task_state.completion_blockers
    )
    frame.messages.append(
        {
            "role": "user",
            "internal": True,
            "content": (
                "MAP_COMPLETION_GATE_BLOCKED\n"
                "The latest map validation did not permit completion. Do not summarize or "
                "answer final yet.\n\n"
                f"Current blockers:\n{blocker_text}\n\n"
                "Continue only through each blocker's explicit next_stage. A completion failure may "
                "run one validation_mode=diagnostic call to locate the failure frontier; after that, "
                "return to the map planner and produce a changed plan before writing. Do not repeat "
                "completion at the same revision, change start/goal to evade the frozen contract, or "
                "route a goal buffer/design failure through repair_map_region. The actual validator "
                "result is authoritative, and the planner must not claim validation passed without it. "
                "Only final-answer after the runtime Completion Gate has accepted "
                "same-revision validation, reviewer observations, scoped evidence, "
                "blockers, and workflow state."
            ),
        }
    )
    return True


def _map_completion_gate_text(blockers: list[dict[str, Any]]) -> str:
    """生成地图完成门拦截后的最终回复文本。"""
    issue_lines: list[str] = []
    for blocker in blockers[:3]:
        tool = str(blocker.get("tool", "map tool"))
        reason = str(blocker.get("reason", "blocked"))
        issues = blocker.get("issues", [])
        if isinstance(issues, list) and issues:
            issue_lines.append(f"- {tool}: {reason}; {str(issues[0])}")
        else:
            issue_lines.append(f"- {tool}: {reason}")
    details = "\n".join(issue_lines)
    return (
        "地图任务还不能标记为完成。\n\n"
        f"{details}\n\n"
        "需要继续按小批编辑、分段 validate_map_region、截图复核的流程修完；"
        "在运行时 Completion Gate 接受同 revision 证据前，最终回复已被服务层拦截。"
    )


def _replace_last_assistant_final(session: Session, text: str) -> None:
    """用服务层拦截文本替换最近一条无工具调用 assistant 回复。"""
    frame = session.top_frame()
    if frame is None or not frame.messages:
        return
    last = frame.messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        last["content"] = text


def _unknown_tool_result_summary(payload: dict[str, Any], inner: dict[str, Any]) -> str:
    """为缺少 tool call 元数据的旧历史结果生成保守摘要。"""
    status = str(payload.get("status", "")).strip()
    for key in ("message", "error", "error_code"):
        value = inner.get(key, payload.get(key))
        if status in {"error", "rejected"} and value not in (None, ""):
            return f"Tool {status}: {value}"
    path = str(inner.get("path", "")).strip()
    root_name = str(inner.get("root_name", "")).strip()
    root_type = str(inner.get("root_type", "")).strip()
    lines = ["Tool result"]
    if status:
        lines[0] = f"Tool {status}"
    if path:
        lines.append(f"Done: `{path}`")
    if root_name or root_type:
        lines.append(f"Root: {root_name} ({root_type})".strip())
    return "\n".join(lines)


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
