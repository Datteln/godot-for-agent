"""平台规划生命周期：结果解析、快照绑定、尝试追踪与前置门禁。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_planning_contexts import (
    MapPlanningContextBundle,
    MapPlanningContextEntry,
    MapPlanningContextError,
)
from app.orchestrator.map_planning_snapshots import (
    PlanningSnapshotStore,
    build_region_snapshot,
    merge_frontier_snapshot,
    planning_snapshot_scope,
)
from app.orchestrator.map_workers import PLATFORM_PLAN_TOOL_NAMES
from app.orchestrator.map_workflow import replace_map_state_field
from .map_context import _revision, _target, active_planning_snapshot
from .map_state import MapPlanOutcome, MapTaskState
if TYPE_CHECKING:
    from app.sessions.store import Session
def parse_map_plan_outcome(tool_name: str, result: dict[str, Any]) -> MapPlanOutcome:
    """统一解析顶层及平台子规划中的执行门信息。

    Args:
        tool_name: 返回结果的地图规划工具名。
        result: 前端规划工具返回的结构化结果。

    Returns:
        归一化后的规划结果；只有满足对应工具执行门时才标记为可执行。
    """
    profile_plan_value = result.get("profile_plan")
    profile_plan = profile_plan_value if isinstance(profile_plan_value, dict) else {}

    blocked_reason_value = result.get("blocked_reason") or profile_plan.get("blocked_reason")
    blocked_reason = (
        blocked_reason_value
        if isinstance(blocked_reason_value, str) and blocked_reason_value.strip()
        else None
    )
    error_code_value = result.get("error_code") or profile_plan.get("error_code")
    error_code = (
        error_code_value if isinstance(error_code_value, str) and error_code_value.strip() else None
    )
    suggested_foothold_value = result.get("suggested_foothold") or profile_plan.get(
        "suggested_foothold"
    )
    suggested_foothold = (
        dict(suggested_foothold_value) if isinstance(suggested_foothold_value, dict) else None
    )

    ok = result.get("ok") is not False and profile_plan.get("ok") is not False
    platform_tool = tool_name in {"validate_platform_level_plan", "plan_reachable_map_growth"}
    if platform_tool:
        defaults_value = result.get("ability_used_defaults")
        if defaults_value is None:
            defaults_value = profile_plan.get("ability_used_defaults")
        if isinstance(defaults_value, list) and defaults_value:
            blocked_reason = blocked_reason or "ability_defaults_used"
        jump_graph_value = result.get("jump_graph") or profile_plan.get("jump_graph")
        if isinstance(jump_graph_value, dict) and jump_graph_value.get("passed") is False:
            blocked_reason = blocked_reason or "jump_graph_failed"
        score_value = result.get("score") or profile_plan.get("score")
        if isinstance(score_value, dict) and score_value.get("passed") is False:
            blocked_reason = blocked_reason or "score_failed"
        edit_batches_value = result.get("edit_map_batches")
        if edit_batches_value is None:
            edit_batches_value = profile_plan.get("edit_map_batches")
        if not isinstance(edit_batches_value, list) or not edit_batches_value:
            blocked_reason = blocked_reason or "empty_edit_map_batches"

    executable = ok and blocked_reason is None and error_code is None
    return MapPlanOutcome(
        ok=ok,
        executable=executable,
        blocked_reason=blocked_reason,
        error_code=error_code,
        suggested_foothold=suggested_foothold,
    )


def _platform_plan_scope(tool_args: dict[str, Any]) -> str:
    """为平台规划事实生成目标与图层隔离的基础作用域。"""
    layer = tool_args.get("map_layer", 0)
    layer_value = layer if isinstance(layer, int) and not isinstance(layer, bool) else 0
    return f"{_target(tool_args)}::map_layer={layer_value}"


def _planning_operation(tool_name: str) -> str:
    """把兼容规划工具名规整为稳定的规划操作。"""
    if tool_name == "validate_platform_level_plan":
        return "platform_route_validation"
    return tool_name


def _planning_attempt_scope(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """生成绑定任务 lineage、地图作用域、快照和操作的尝试键。"""
    lineage = str(
        session.map_task_lineage.get("lineage_id") or session.map_task_state.task_id or "unbound"
    )
    snapshot_id = str(tool_args.get("authoritative_snapshot_id", "legacy"))
    return (
        f"lineage={lineage}::{_platform_plan_scope(tool_args)}::"
        f"snapshot={snapshot_id}::operation={_planning_operation(tool_name)}"
    )


def _platform_plan_fingerprint(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """为 LLM 显式平台方案生成稳定指纹，缺少方案字段时不参与去重。"""
    platforms = tool_args.get("platforms")
    segments = tool_args.get("segments")
    if not isinstance(platforms, list) or not platforms:
        return None
    if not isinstance(segments, list) or not segments:
        return None
    payload = {
        "tool": tool_name,
        "scope": _platform_plan_scope(tool_args),
        "platforms": platforms,
        "segments": segments,
        "start": tool_args.get("start"),
        "frontier": tool_args.get("frontier"),
        "movement": {
            key: tool_args.get(key)
            for key in (
                "movement_model",
                "max_horizontal_gap",
                "max_rise",
                "max_fall",
                "gravity_axis",
                "gravity_sign",
                "frontier_axis",
                "frontier_sign",
            )
            if key in tool_args
        },
        "authoritative_snapshot_id": tool_args.get("authoritative_snapshot_id"),
        "authoritative_snapshot_digest": tool_args.get("authoritative_snapshot_digest"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def map_platform_plan_call_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """执行前拒绝缺快照、第四次或未修订的平台规划提交。"""
    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        return None
    snapshot_error = bind_authoritative_snapshot(session, tool_name, tool_args)
    if snapshot_error is not None:
        return snapshot_error
    attempt_scope = _planning_attempt_scope(session, tool_name, tool_args)
    if session.map_task_state.planning_attempts.get(attempt_scope, 0) >= 3:
        return (
            "planning_attempts_exhausted：当前 task lineage、target、layer、snapshot "
            "和规划操作已完成三次确定性校验。规划结果已经或将被交付；禁止第四次校验，"
            "writer 必须保持阻断，直到 revision/facts 变化并产生新快照。"
        )
    fingerprint = _platform_plan_fingerprint(tool_name, tool_args)
    if fingerprint is None:
        return None
    fingerprint_key = f"{attempt_scope}::{fingerprint}"
    if session.map_task_state.planning_fingerprints.get(fingerprint_key, 0) > 0:
        return (
            "unchanged_plan_attempt：该 platforms/segments 方案已经校验过，"
            "确定性结果不会因重复提交改变；"
            "必须根据 issues/repair_plan 修改具体平台字段。"
        )
    return None


def bind_authoritative_snapshot(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    project_root: Path | None = None,
) -> str | None:
    """把当前权威快照身份绑定到规划调用，失败时返回类型化刷新指令。"""
    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        return None
    target = _target(tool_args)
    layer_value = tool_args.get("map_layer", 0)
    layer = layer_value if isinstance(layer_value, int) and not isinstance(layer_value, bool) else 0
    snapshot = active_planning_snapshot(session, target, layer)
    if snapshot is None:
        return (
            "authoritative_snapshot_required：当前平台方案没有同 target/layer/revision 的"
            "权威快照。请由 reader 以 cells_format=non_empty_only/full 读取完整覆盖区域，"
            "再按显式 traversal profile 运行 compute_reachable_frontier；禁止 planner "
            "自行读取或猜测第二份事实基线。"
        )
    if snapshot.get("execution_eligible") is not True:
        missing = [
            key
            for key, complete in dict(snapshot.get("completeness", {})).items()
            if complete is not True
        ]
        return (
            "authoritative_snapshot_incomplete：快照不能授权确定性执行；"
            f"missing_or_stale={missing}。请由 reader 定向刷新或重算 frontier。"
        )
    tool_args["authoritative_snapshot_id"] = snapshot["snapshot_id"]
    tool_args["authoritative_snapshot_digest"] = snapshot["digest"]
    tool_args["authoritative_snapshot_target"] = snapshot["target_path"]
    tool_args["authoritative_snapshot_layer"] = snapshot["map_layer"]
    tool_args["authoritative_snapshot_revision"] = snapshot["map_revision"]
    tool_args["authoritative_snapshot_coverage_complete"] = bool(
        dict(snapshot.get("completeness", {})).get("coverage", False)
    )
    tool_args["authoritative_snapshot_traversal_complete"] = bool(
        dict(snapshot.get("completeness", {})).get("traversal_profile", False)
    )
    tool_args["authoritative_snapshot_frontier_complete"] = bool(
        dict(snapshot.get("completeness", {})).get("reachable_frontier", False)
    )
    full_snapshot = None
    if project_root is not None:
        try:
            full_snapshot = PlanningSnapshotStore(
                project_root,
                session.session_id,
                session.session_epoch,
            ).read(str(snapshot["artifact_ref"]))
        except (OSError, TypeError, ValueError):
            return (
                "authoritative_snapshot_digest_mismatch：快照 artifact 无法通过身份或 digest "
                "校验，请由 reader 重新物化同 revision 快照。"
            )
    projection = (
        full_snapshot.planner_projection()
        if full_snapshot is not None
        else snapshot.get("planner_projection")
    )
    if isinstance(projection, dict):
        route_facts = projection.get("route_facts")
        if isinstance(route_facts, dict):
            entry = route_facts.get("entry_anchor")
            frontier = route_facts.get("reachable_frontier")
            if "entry_anchor" not in tool_args and isinstance(entry, dict) and entry:
                tool_args["entry_anchor"] = deepcopy(entry)
            if "frontier" not in tool_args and isinstance(frontier, dict) and frontier:
                tool_args["frontier"] = deepcopy(frontier)
    if full_snapshot is not None:
        tool_args["_authoritative_resource_bindings"] = deepcopy(full_snapshot.resource_bindings)
        tool_args["_authoritative_snapshot_digest_verified"] = True
    return None


def remember_planning_snapshot_evidence(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    project_root: Path,
    evidence_ref: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把 canonical region/frontier 结果物化为可恢复的权威规划快照。"""
    if tool_name not in {"describe_map_region", "compute_reachable_frontier"}:
        return None
    if result.get("ok") is not True:
        return None
    store = PlanningSnapshotStore(
        project_root,
        session.session_id,
        session.session_epoch,
    )
    try:
        if tool_name == "describe_map_region":
            snapshot = build_region_snapshot(
                tool_args,
                result,
                evidence_ref=evidence_ref,
            )
        else:
            target_value = result.get(
                "target",
                result.get("target_path", tool_args.get("target_path", "")),
            )
            target = target_value if isinstance(target_value, str) else ""
            layer_value = result.get("map_layer", tool_args.get("map_layer", 0))
            layer = (
                layer_value
                if isinstance(layer_value, int) and not isinstance(layer_value, bool)
                else 0
            )
            current = active_planning_snapshot(session, target, layer)
            if current is None:
                return None
            base = store.read(str(current["artifact_ref"]))
            snapshot = merge_frontier_snapshot(
                base,
                tool_args,
                result,
                evidence_ref=evidence_ref,
            )
        locator = store.store(snapshot)
    except (OSError, TypeError, ValueError):
        return None
    scope = planning_snapshot_scope(snapshot.target_path, snapshot.map_layer)
    snapshots = dict(session.map_task_state.authoritative_snapshots)
    snapshots[scope] = locator
    replace_map_state_field(
        session.map_task_state,
        "authoritative_snapshots",
        snapshots,
        target=snapshot.target_path,
        revision=snapshot.map_revision,
    )
    semantic_role_value = result.get(
        "semantic_role",
        tool_args.get("semantic_role", f"map_layer:{snapshot.map_layer}"),
    )
    semantic_role = (
        semantic_role_value.strip()
        if isinstance(semantic_role_value, str) and semantic_role_value.strip()
        else f"map_layer:{snapshot.map_layer}"
    )
    try:
        context_entry = MapPlanningContextEntry.from_snapshot(
            locator,
            semantic_role=semantic_role,
        )
        contexts = dict(session.map_task_state.planning_contexts)
        contexts[context_entry.context_id] = context_entry.to_dict()
        replace_map_state_field(
            session.map_task_state,
            "planning_contexts",
            contexts,
            target=snapshot.target_path,
            revision=snapshot.map_revision,
        )
        current_entries = [
            MapPlanningContextEntry.from_dict(item)
            for item in contexts.values()
            if isinstance(item, dict)
        ]
        bundle = MapPlanningContextBundle.from_entries(current_entries)
        bundles = dict(session.map_task_state.planning_context_bundles)
        bundles[bundle.bundle_id] = bundle.to_dict()
        replace_map_state_field(
            session.map_task_state,
            "planning_context_bundles",
            bundles,
            target=snapshot.target_path,
            revision=snapshot.map_revision,
        )
    except MapPlanningContextError:
        # 旧快照仍保持可恢复；不合法的规划投影不得污染新上下文注册表。
        pass
    return deepcopy(locator)


def map_platform_plan_attempt_count(
    session: Session,
    tool_args: dict[str, Any],
    tool_name: str = "validate_platform_level_plan",
) -> int:
    """返回当前目标和图层已经执行的平台规划次数。"""
    state: MapTaskState = session.map_task_state
    return state.planning_attempts.get(
        _planning_attempt_scope(session, tool_name, tool_args),
        0,
    )


def _remember_platform_plan_attempt(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[str, int, str | None]:
    """记录一次真实执行的平台规划及其显式方案指纹。"""
    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        return "", 0, None
    scope = _planning_attempt_scope(session, tool_name, tool_args)
    state = session.map_task_state
    attempts = dict(state.planning_attempts)
    attempts[scope] = attempts.get(scope, 0) + 1
    replace_map_state_field(
        state,
        "planning_attempts",
        attempts,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )
    fingerprint = _platform_plan_fingerprint(tool_name, tool_args)
    if fingerprint is None:
        return scope, attempts[scope], None
    fingerprint_key = f"{scope}::{fingerprint}"
    fingerprints = dict(state.planning_fingerprints)
    fingerprints[fingerprint_key] = fingerprints.get(fingerprint_key, 0) + 1
    replace_map_state_field(
        state,
        "planning_fingerprints",
        fingerprints,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )
    return scope, attempts[scope], fingerprint
