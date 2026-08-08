"""地图验证合同：合同提取、指纹、缓存与验收后阶段推进。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_workflow import (
    increment_map_counter,
    replace_map_state_field,
)
from .map_context import _revision, _target
from .map_failure_guard import record_no_progress
from .map_state import ValidationMode
if TYPE_CHECKING:
    from app.sessions.store import Session
_CONTRACT_KEYS = (
    "target_path",
    "map_layer",
    "start",
    "goal",
    "waypoints",
    "entrances",
    "exits",
    "movement_model",
    "cell_occupancy",
    "requires_support",
    "support_occupancy",
    "max_horizontal_gap",
    "max_rise",
    "max_fall",
    "max_step",
    "gravity_axis",
    "gravity_sign",
    "path_algorithm",
    "check_platform_design",
    "min_finish_buffer_width",
)


def has_completion_route(tool_args: dict[str, Any]) -> bool:
    """判断验证参数是否包含可冻结的真实路线约束。"""
    start = tool_args.get("start")
    goal = tool_args.get("goal")
    if isinstance(start, dict) and isinstance(goal, dict):
        return True

    entrances = tool_args.get("entrances")
    exits = tool_args.get("exits")
    if isinstance(entrances, list) and entrances and isinstance(exits, list) and exits:
        return True

    waypoints = tool_args.get("waypoints")
    return isinstance(waypoints, list) and len(waypoints) >= 2


def validation_mode(tool_args: dict[str, Any]) -> ValidationMode:
    """读取验证模式，并将无路线的旧调用安全降级为 diagnostic。"""
    requested_mode = tool_args.get("validation_mode")
    if requested_mode == "diagnostic":
        return "diagnostic"
    if requested_mode == "completion":
        return "completion"
    return "completion" if has_completion_route(tool_args) else "diagnostic"


def validation_contract(tool_args: dict[str, Any]) -> dict[str, Any]:
    """提取不可由模型在重试时漂移的 completion 验收字段。"""
    return {key: tool_args[key] for key in _CONTRACT_KEYS if key in tool_args}


def validation_contract_hash(tool_args: dict[str, Any]) -> str:
    """生成 completion 验收合同的稳定短指纹。"""
    encoded = json.dumps(
        validation_contract(tool_args), ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def validation_request_fingerprint(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """生成可在前端执行前命中的验证请求指纹。"""
    payload = {
        "tool": tool_name,
        "target": _target(tool_args),
        "revision": _revision(session, tool_args),
        "args": tool_args,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def cached_validation_result(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any] | None:
    """返回完全相同 revision 与参数对应的验证缓存。"""
    if tool_name != "validate_map_region":
        return None
    fingerprint = validation_request_fingerprint(session, tool_name, tool_args)
    cached = session.map_task_state.validation_cache.get(fingerprint)
    if not isinstance(cached, dict):
        return None
    increment_map_counter(session.map_task_state, "validation_cache_hits")
    return {
        **cached,
        "cache_hit": True,
        "cache_reason": "same_revision_validation_fingerprint",
    }


def remember_validation_cache(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """保存一次真实 validate_map_region 结果供确定性复用。"""
    if tool_name != "validate_map_region":
        return
    fingerprint = validation_request_fingerprint(session, tool_name, tool_args)
    cache = dict(session.map_task_state.validation_cache)
    cache[fingerprint] = dict(result)
    while len(cache) > 64:
        cache.pop(next(iter(cache)))
    replace_map_state_field(
        session.map_task_state,
        "validation_cache",
        cache,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )


def _validation_scope(tool_args: dict[str, Any]) -> str:
    """返回隔离 TileMap 图层的验证状态键。"""
    layer = tool_args.get("map_layer", 0)
    layer_value = layer if isinstance(layer, int) and not isinstance(layer, bool) else 0
    return f"{_target(tool_args)}::map_layer={layer_value}"


def validation_call_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """拒绝同 revision 的重复 completion、重复 diagnostic 与验收条件漂移。"""
    if tool_name != "validate_map_region":
        return None
    scope = _validation_scope(tool_args)
    revision = _revision(session, tool_args)
    mode = validation_mode(tool_args)
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    same_revision = workflow.get("map_revision") == revision

    if mode == "completion":
        if not has_completion_route(tool_args):
            record_no_progress(session, scope, "completion_route_missing")
            return (
                "completion 验证必须提供 start+goal、非空 entrances+exits，或至少两个 waypoints；"
                "无路线的图层/区域检查请使用 validation_mode='diagnostic'。"
            )
        contract_hash = validation_contract_hash(tool_args)
        frozen = session.map_task_state.validation_contracts.get(scope)
        if isinstance(frozen, dict) and frozen.get("hash") not in (None, contract_hash):
            record_no_progress(session, scope, "completion_contract_drift")
            return (
                "completion 验收合同已冻结；禁止修改 start/goal/waypoints/移动参数来绕过失败。"
                "请修改地图，或由用户明确提交新的验收目标。"
            )
        if same_revision and workflow.get("completion_attempted") is True:
            record_no_progress(session, scope, "completion_repeated_without_revision")
            next_stage = str(workflow.get("next_stage", "planner"))
            return (
                f"map revision {revision} 已执行过 completion 验证；确定性结果不会因重试改变。"
                f"下一阶段必须是 {next_stage}，产生新 revision 后才能再次 completion。"
            )
        return None

    if same_revision and workflow.get("diagnostic_attempted") is True:
        record_no_progress(session, scope, "diagnostic_repeated_without_revision")
        return (
            f"map revision {revision} 已完成 diagnostic；下一阶段必须是 planner，"
            "不得继续更换局部 goal 反复验证。"
        )
    if same_revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, scope, "validation_repeated_before_planning")
        return f"map revision {revision} 已要求进入 planner；写入新 revision 前禁止继续验证。"
    return None


def remember_validation_progress(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    successful: bool,
) -> None:
    """记录一次真实验证完成后的强制下一阶段。"""
    if tool_name != "validate_map_region":
        return
    target_value = result.get("target", result.get("target_path", _target(tool_args)))
    target = target_value if isinstance(target_value, str) else ""
    scope_args = {**tool_args, "target_path": target}
    result_layer = result.get("map_layer")
    if (
        "map_layer" not in scope_args
        and isinstance(result_layer, int)
        and not isinstance(result_layer, bool)
    ):
        scope_args["map_layer"] = result_layer
    scope = _validation_scope(scope_args)
    revision_value = result.get("map_revision")
    revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else _revision(session, tool_args)
    )
    mode = validation_mode(tool_args)
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    if workflow.get("map_revision") != revision:
        workflow = {"map_revision": revision}

    if mode == "completion":
        contract = validation_contract(scope_args)
        contracts = dict(session.map_task_state.validation_contracts)
        contracts.setdefault(
            scope,
            {"hash": validation_contract_hash(scope_args), "contract": contract},
        )
        replace_map_state_field(
            session.map_task_state,
            "validation_contracts",
            contracts,
            target=target,
            revision=revision,
        )
        workflow["completion_attempted"] = True
        workflow["next_stage"] = "reviewer" if successful else "diagnostic"
        session.map_task_state.transition_stage("review" if successful else "diagnostic")
        replace_map_state_field(
            session.map_task_state,
            "unresolved_issues",
            list(result.get("issues", [])),
            target=target,
            revision=revision,
        )
        if successful:
            replace_map_state_field(
                session.map_task_state,
                "completed_goals",
                [*session.map_task_state.completed_goals, contract],
                target=target,
                revision=revision,
            )
    else:
        workflow["diagnostic_attempted"] = True
        workflow["next_stage"] = "planner"
        session.map_task_state.transition_stage("plan")
        replace_map_state_field(
            session.map_task_state,
            "failure_frontier",
            {
                "region": result.get("region", {}),
                "issues": result.get("issues", []),
                "structured_issues": result.get("structured_issues", []),
            },
            target=target,
            revision=revision,
        )
    workflow["issues"] = result.get("issues", [])
    workflows = dict(session.map_task_state.validation_workflows)
    workflows[scope] = workflow
    replace_map_state_field(
        session.map_task_state,
        "validation_workflows",
        workflows,
        target=target,
        revision=revision,
    )
    increment_map_counter(
        session.map_task_state,
        "validations",
        target=target,
        revision=revision,
    )
    streaks = dict(session.map_task_state.no_progress_streaks)
    streaks[scope] = 0
    replace_map_state_field(
        session.map_task_state,
        "no_progress_streaks",
        streaks,
        target=target,
        revision=revision,
    )
