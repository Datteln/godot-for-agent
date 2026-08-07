"""准备 Map 工具可见性、缓存与写入元数据。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.types import AgentDefinition, Frame
from app.llm.provider import (
    ToolCallRequest,
)
from app.orchestrator.frame_contract_types import (
    MAP_WORKER_STAGE_CONTRACT_KIND,
)
from app.orchestrator.map_capabilities import map_tools_for_stage
from app.orchestrator.map_contracts import (
    MAP_WORKER_STAGES,
)
from app.orchestrator.map_progress import (
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    latest_map_revision,
    platform_write_requires_validation,
)
from app.orchestrator.map_turn.budgets import _uses_persistent_map_budget
from app.orchestrator.map_turn.contracts import (
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.events import _emit_orchestration_event
from app.orchestrator.map_turn.tool_arguments import _load_tool_args
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    MAP_VALIDATION_TOOL_NAMES,
    MAP_WORKER_MODE_STAGES,
    is_map_write_tool,
)
from app.orchestrator.map_workflow import increment_map_counter
from app.orchestrator.runtime_contracts import UnapprovedWriteRejection
from app.orchestrator.turn.contracts import (
    ToolCallsTurnOutcome,
)
from app.sessions.store import Session


def _map_stage_contract(
    agent: AgentDefinition,
    task_text: str,
    worker_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据可信委派元数据构造地图子 Frame 合同。

    本轮整改新增：合同在子帧创建时一次性绑定 stage / target_path /
    map_revision / region，后续 _map_structured_output_error 据此做
    一致性校验，防止 worker 越权写入非预期的图层或 revision。
    """
    # 动态 worker 以 worker_spec.mode 为准（mode→stage 映射由 map_contracts 集中维护），
    # 静态 agent 则直接读取 agent.map_stage 元数据
    stage = agent.map_stage
    if isinstance(worker_spec, dict):
        stage = MAP_WORKER_MODE_STAGES.get(str(worker_spec.get("mode", "")))
    if stage not in MAP_WORKER_STAGES:
        return {}
    # 尝试从 task_text 解析 JSON 载荷，提取 target/revision/region 等合同字段
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(task_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
    approved_batch = (
        worker_spec.get("approved_batch")
        if isinstance(worker_spec, dict) and isinstance(worker_spec.get("approved_batch"), dict)
        else {}
    )
    contract: dict[str, Any] = {
        "contract_kind": MAP_WORKER_STAGE_CONTRACT_KIND,
        "contract_version": 1,
        "stage": stage,
    }
    target = payload.get("target_path", approved_batch.get("target_path"))
    if isinstance(target, str) and target.strip():
        contract["target_path"] = target.strip()
    # 兼容 map_revision 与旧字段 required_revision
    revision = payload.get(
        "map_revision",
        payload.get("required_revision", approved_batch.get("map_revision")),
    )
    if isinstance(revision, int) and not isinstance(revision, bool):
        contract["map_revision"] = revision
    region = payload.get("region")
    if isinstance(region, dict):
        # 只保留整数坐标，过滤掉非数值脏数据
        contract["region"] = {
            str(key): value
            for key, value in region.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    if approved_batch:
        contract["approved_batch_ref"] = approved_batch.get("artifact_ref")
        contract["approved_batch_id"] = approved_batch.get("batch_id")
    authoritative_snapshot = (
        worker_spec.get("authoritative_snapshot")
        if isinstance(worker_spec, dict)
        and isinstance(worker_spec.get("authoritative_snapshot"), dict)
        else None
    )
    if authoritative_snapshot is not None:
        contract["authoritative_snapshot"] = dict(authoritative_snapshot)
    planning_context_bundle = (
        worker_spec.get("planning_context_bundle")
        if isinstance(worker_spec, dict)
        and isinstance(worker_spec.get("planning_context_bundle"), dict)
        else None
    )
    if planning_context_bundle is not None:
        contract["planning_context_bundle"] = dict(planning_context_bundle)
    raw_execution_operations = (
        approved_batch.get("execution_operations") if approved_batch else None
    )
    if isinstance(raw_execution_operations, list):
        contract["execution_operations"] = [
            dict(item) for item in raw_execution_operations if isinstance(item, dict)
        ]
    return contract


def _route_unvalidated_platform_writes_to_validator(
    *,
    session: Session,
    frame: Frame,
    calls: list[ToolCallRequest],
    project_root: Path,
    event_callback: Callable[[str, dict[str, Any]], None] | None,
) -> tuple[bool, ToolCallsTurnOutcome | None]:
    """拒绝未获 planner 批准的平台写入，不在服务层推断路线。

    本轮整改：旧实现会在服务层自动推断平台几何并下发 validate_platform_level_plan，
    但这绕过了 planner 的路线设计职责，且推断结果常与模型意图不一致。
    新实现只做「守门」：检测到缺少批准批次的平台写入就一律拒绝，
    把控制权还给 planner agent。
    """
    del project_root  # 新实现不再需要 project_root，保留签名兼容
    blocked = False
    for call in calls:
        args, parse_error = _load_tool_args(call.id, call.arguments, call.name)
        if parse_error is not None or args is None:
            continue
        blocked = blocked or platform_write_requires_validation(session, call.name, args)
    if not blocked:
        return False, None
    rejection = UnapprovedWriteRejection(
        error_code="approved_write_batch_required",
        message=(
            "平台路线写入缺少 planner 生成并由 "
            "validate_platform_level_plan 编译的批准批次。"
            "服务层不会从 edit_map 几何推断路线；请返回 planner。"
        ),
    )
    for call in calls:
        frame.messages.append(
            _tool_message(
                call.id,
                rejection.to_dict(),
                is_error=True,
            )
        )
    _emit_orchestration_event(
        event_callback,
        "map_platform_write_rejected",
        {"frame_id": frame.id, "agent": frame.agent.name},
    )
    return True, None


def _stage_effective_tools(session: Session, frame: Frame) -> list[str]:
    """按地图任务阶段裁剪工具，非地图帧保持原白名单。"""
    if frame.force_text_only:
        return []
    if not _uses_persistent_map_budget(frame):
        return list(frame.agent.effective_tools)
    stage = session.map_task_state.stage
    # 本轮整改：阶段→工具映射从内嵌 _MAP_STAGE_TOOLS 迁移到
    # map_capabilities.map_tools_for_stage()，支持动态扩展
    allowed = map_tools_for_stage(stage)
    if not allowed:
        return list(frame.agent.effective_tools)
    return [name for name in frame.agent.effective_tools if name in allowed]


def _region_contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    """判断缓存区域是否完整覆盖请求区域。"""
    try:
        for axis, size in (("x", "width"), ("y", "height"), ("z", "depth")):
            outer_start = int(outer.get(axis, 0))
            inner_start = int(inner.get(axis, 0))
            if outer_start > inner_start:
                return False
            if outer_start + int(outer.get(size, 1)) < inner_start + int(inner.get(size, 1)):
                return False
        return True
    except (TypeError, ValueError):
        return False


def _cached_map_region_summary(
    session: Session,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """返回同 revision 下覆盖请求的最近区域摘要。"""
    target = args.get("target_path")
    layer = args.get("map_layer")
    if not isinstance(target, str) or not target:
        return None
    if not isinstance(layer, int) or isinstance(layer, bool):
        return None
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    current_revision = latest_map_revision(session, target, layer)
    # 本轮整改：context_state 从 session 顶层迁移到 map_task_state
    targets = session.map_task_state.context_state.get("targets", {})
    target_state = targets.get(target, {}) if isinstance(targets, dict) else {}
    layers = target_state.get("layers", {}) if isinstance(target_state, dict) else {}
    layer_state = layers.get(str(layer), {}) if isinstance(layers, dict) else {}
    regions = layer_state.get("recent_regions", []) if isinstance(layer_state, dict) else []
    if not isinstance(regions, list):
        return None
    requested_region = {
        "x": args.get("x", 0),
        "y": args.get("y", 0),
        "z": args.get("z", 0),
        "width": args.get("width", 1),
        "height": args.get("height", 1),
        "depth": args.get("depth", 1),
    }
    format_rank = {"summary_only": 0, "non_empty_only": 1, "full": 2}
    requested_rank = format_rank.get(str(args.get("cells_format", "summary_only")), 0)
    for entry in reversed(regions):
        if not isinstance(entry, dict) or entry.get("map_revision") != current_revision:
            continue
        cached_rank = format_rank.get(str(entry.get("cells_format", "summary_only")), 0)
        if cached_rank < requested_rank:
            continue
        region = entry.get("region", {})
        if isinstance(region, dict) and _region_contains(region, requested_region):
            increment_map_counter(session.map_task_state, "read_cache_hits")
            return {**entry, "cache_hit": True, "cache_reason": "same_revision_region_covered"}
    return None


def _resumed_full_map_read_error(session: Session, args: dict[str, Any]) -> str | None:
    """恢复任务时拒绝重新读取已知整图范围。"""
    if not session.map_request_scope.explicit_continuation:
        return None
    target = args.get("target_path")
    layer = args.get("map_layer")
    if not isinstance(target, str) or not isinstance(layer, int):
        return None
    # 本轮整改：context_state 从 session 顶层迁移到 map_task_state
    targets = session.map_task_state.context_state.get("targets", {})
    target_state = targets.get(target, {}) if isinstance(targets, dict) else {}
    layers = target_state.get("layers", {}) if isinstance(target_state, dict) else {}
    layer_state = layers.get(str(layer), {}) if isinstance(layers, dict) else {}
    used_bounds = layer_state.get("used_bounds") if isinstance(layer_state, dict) else None
    requested = {
        "x": args.get("x", 0),
        "y": args.get("y", 0),
        "z": args.get("z", 0),
        "width": args.get("width", 1),
        "height": args.get("height", 1),
        "depth": args.get("depth", 1),
    }
    if isinstance(used_bounds, dict) and _region_contains(requested, used_bounds):
        return (
            "任务已从结构化检查点恢复；禁止从头读取整个地图。"
            "请复用 checkpoint/region cache，只读取 failure_frontier 或尚未缓存的小区域。"
        )
    return None


def _with_map_write_metadata(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """补充服务端掌握的地图写组与验证事务元数据。"""
    enriched = dict(args)
    if tool_name in MAP_VALIDATION_TOOL_NAMES:
        target_path = str(enriched.get("target_path", "")).strip()
        candidates = [
            item
            for item in session.map_task_state.transaction_journals
            if item.get("status") == "prepared"
            and (not target_path or str(item.get("target", "")).strip() == target_path)
        ]
        if candidates:
            active = candidates[-1]
            enriched.setdefault("map_transaction_id", str(active.get("transaction_id", "")))
            enriched.setdefault("map_transaction_revision", active.get("final_revision"))
            enriched.setdefault("map_transaction_target", str(active.get("target", "")))
        return enriched
    if not is_map_write_tool(tool_name):
        return enriched
    target_path = str(enriched.get("target_path", ""))
    # 本轮整改：revision 查询改为图层感知，传入 map_layer 避免跨图层冲突
    layer = enriched.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    latest_revision = latest_map_revision(session, target_path, map_layer)
    supplied_revision = enriched.get("expected_revision")
    supplied_revision_is_int = isinstance(supplied_revision, int) and not isinstance(
        supplied_revision, bool
    )
    if latest_revision is not None and (
        not supplied_revision_is_int or latest_revision > int(supplied_revision)
    ):
        logger.info(
            "Overriding stale map expected_revision session=%s frame=%s tool=%s target=%s supplied=%s latest=%s",
            session.session_id,
            frame.id,
            tool_name,
            target_path,
            supplied_revision,
            latest_revision,
        )
        enriched["expected_revision"] = latest_revision
    # 本轮整改：latest_layers 从 session 顶层迁移到 map_task_state
    latest_layer = session.map_task_state.latest_layers.get(target_path)
    if latest_layer is not None and "map_layer" not in enriched:
        logger.info(
            "Filling missing map_layer session=%s frame=%s tool=%s target=%s map_layer=%s",
            session.session_id,
            frame.id,
            tool_name,
            target_path,
            latest_layer,
        )
        enriched["map_layer"] = latest_layer
    enriched.setdefault("write_batch_id", f"b-{call_id}")
    if isinstance(enriched.get("plan_version"), int) and not isinstance(
        enriched.get("plan_version"), bool
    ):
        transaction_seed = ":".join(
            (
                session.session_id,
                frame.id,
                str(enriched["plan_version"]),
                target_path,
            )
        )
        enriched.setdefault(
            "map_transaction_id",
            "mtx-" + hashlib.sha256(transaction_seed.encode("utf-8")).hexdigest()[:24],
        )
        enriched.setdefault("map_transaction_mode", "approved_write_group")
        enriched.setdefault("map_transaction_base_revision", enriched.get("expected_revision"))
        enriched.setdefault("map_transaction_validator", "validate_map_region")
    else:
        enriched.setdefault("map_transaction_mode", "single_tool")
    enriched.setdefault("worker", frame.agent.name)
    enriched.setdefault("mode", "write_one_batch")
    enriched.setdefault("frame_id", frame.id)
    if frame.agent.workflow_operations:
        enriched.setdefault("workflow_operations", frame.agent.workflow_operations)
    if frame.agent.workflow_constraints:
        enriched.setdefault("workflow_constraints", frame.agent.workflow_constraints)
    if frame.pending_delegate_group_id is not None:
        enriched.setdefault("delegate_group_id", frame.pending_delegate_group_id)
    if "task_summary" not in enriched:
        enriched["task_summary"] = str(enriched.get("objective", tool_name))
    return enriched
