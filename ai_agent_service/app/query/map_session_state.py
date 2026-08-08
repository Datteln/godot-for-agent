"""会话级地图状态：修订、区域缓存、验证记录与 blocker。"""

from __future__ import annotations

import hashlib
import json
from ._map_derivation import (
    _MAP_COMPLETION_TOOL_NAMES,
    _MAP_CONTEXT_MAX_REGIONS_PER_LAYER,
    _MAP_CONTEXT_MAX_SUMMARY_CHARS,
    _MAP_CONTEXT_MAX_TARGETS,
    _MAP_CONTEXT_MAX_TOTAL_CHARS,
    _MAP_MATCH_SUMMARY_LIMIT,
    _map_layer_from_result,
    _map_revision_from_result,
    _map_target_from_result,
    _parsed_map_region_read_signature,
)
from ._text_utils import _truncate_text, logger
from .tool_summary import _map_result_summary
from app.history_bounds import json_char_size as _json_char_size
from app.orchestrator.map_context import map_revision_scope_key
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES, MAP_VALIDATION_TOOL_NAMES
from app.orchestrator.map_workflow import increment_map_counter, record_map_revision, record_map_validation, replace_map_state_field
from app.sessions.store import Session
from typing import Any
def _trim_text_fields(value: Any, max_chars: int = _MAP_CONTEXT_MAX_SUMMARY_CHARS) -> Any:
    """递归截断 map_context_state 摘要中的长字符串字段。"""
    if isinstance(value, str):
        return _truncate_text(value, max_chars)
    if isinstance(value, dict):
        return {str(key): _trim_text_fields(item, max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim_text_fields(item, max_chars) for item in value[:_MAP_MATCH_SUMMARY_LIMIT]]
    return value


def _update_map_context_state(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
    artifact_ref: str | None,
    artifact_locator: dict[str, str] | None = None,
) -> None:
    """维护每 session 地图小索引；只保存摘要和 artifact_ref。"""
    if tool_name != "describe_map_region" or not isinstance(result, dict):
        return
    target = _map_target_from_result(tool_args, result)
    if not target:
        return
    layer = _map_layer_from_result(result, prefer_layers=True)
    layer_key = str(layer if layer is not None else tool_args.get("map_layer", "default"))
    state = session.map_task_state.context_state
    targets = state.setdefault("targets", {})
    if not isinstance(targets, dict):
        targets = {}
        state["targets"] = targets
    if target not in targets and len(targets) >= _MAP_CONTEXT_MAX_TARGETS:
        targets.pop(next(iter(targets)))
    target_state = targets.setdefault(target, {"layers": {}})
    if not isinstance(target_state, dict):
        target_state = {"layers": {}}
        targets[target] = target_state
    revision = _map_revision_from_result(result)
    if revision is not None:
        target_state["latest_revision"] = revision
    layers = target_state.setdefault("layers", {})
    if not isinstance(layers, dict):
        layers = {}
        target_state["layers"] = layers
    layer_state = layers.setdefault(layer_key, {"recent_regions": []})
    if not isinstance(layer_state, dict):
        layer_state = {"recent_regions": []}
        layers[layer_key] = layer_state
    if "used_bounds" in result:
        layer_state["used_bounds"] = result.get("used_bounds")
    entry = _trim_text_fields(
        _map_result_summary(
            "describe_map_region",
            result,
            artifact_ref,
            artifact_locator,
        ),
        _MAP_CONTEXT_MAX_SUMMARY_CHARS,
    )
    regions = layer_state.setdefault("recent_regions", [])
    if not isinstance(regions, list):
        regions = []
        layer_state["recent_regions"] = regions
    regions.append(entry)
    del regions[: max(0, len(regions) - _MAP_CONTEXT_MAX_REGIONS_PER_LAYER)]
    while _json_char_size(state) > _MAP_CONTEXT_MAX_TOTAL_CHARS:
        removed = False
        for target_item in list(targets.values()):
            if not isinstance(target_item, dict):
                continue
            layer_items = target_item.get("layers", {})
            if not isinstance(layer_items, dict):
                continue
            for layer_item in layer_items.values():
                if not isinstance(layer_item, dict):
                    continue
                recent = layer_item.get("recent_regions", [])
                if isinstance(recent, list) and recent:
                    recent.pop(0)
                    removed = True
                    break
            if removed:
                break
        if not removed:
            break


def _invalidate_stale_map_revision_state(
    session: Session,
    target: str,
    previous_revision: int,
    current_revision: int,
) -> None:
    """在前端 revision 重新计数时废弃旧规划、验证和写入状态。"""
    scope_prefix = f"{target}::"
    state = session.map_task_state
    # 清理所有以 target 或 "target::map_layer=N" 为 key 的派生状态，
    # 包括验证契约、验证工作流、无进展计数、规划尝试、已批准规划等，
    # 确保 revision 重置后不会残留旧数据导致误判。
    for field_name in (
        "validation_contracts",
        "validation_workflows",
        "no_progress_streaks",
        "planning_attempts",
        "authoritative_snapshots",
        "approved_platform_plans",
    ):
        mapping = getattr(state, field_name)
        replace_map_state_field(
            state,
            field_name,
            {
                key: value
                for key, value in mapping.items()
                if key != target and not key.startswith(scope_prefix)
            },
            target=target,
            revision=current_revision,
        )
    replace_map_state_field(
        state,
        "planning_fingerprints",
        {
            key: value
            for key, value in state.planning_fingerprints.items()
            if not key.startswith(scope_prefix)
        },
        target=target,
        revision=current_revision,
    )
    for field_name, empty_value in (
        ("pending_batches", []),
        ("executed_batches", []),
        ("validation_cache", {}),
        ("failure_frontier", None),
        ("unresolved_issues", []),
        ("region_reads", {}),
        ("region_summaries", {}),
    ):
        replace_map_state_field(
            state,
            field_name,
            empty_value,
            target=target,
            revision=current_revision,
        )
    # 使用 transition_stage() 而非直接赋值 stage，确保阶段转换钩子（如日志/事件）正常触发
    state.transition_stage("read")
    replace_map_state_field(
        state,
        "plan_version",
        0,
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        state,
        "latest_validations",
        {key: value for key, value in state.latest_validations.items() if key != target},
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        state,
        "completion_blockers",
        [
            blocker
            for blocker in state.completion_blockers
            if blocker.get("target") not in ("", target)
        ],
        target=target,
        revision=current_revision,
    )
    logger.warning(
        "Map revision epoch reset detected session=%s target=%s previous=%s current=%s; "
        "invalidated stale plans and validation workflows",
        session.session_id,
        target,
        previous_revision,
        current_revision,
    )


def _map_revision_identity_is_authoritative(
    tool_args: dict[str, Any],
    result: dict[str, Any],
    target: str,
) -> bool:
    """判断 revision 是否明确属于解析后的目标地图节点。

    改动说明：旧实现只比较 revision_key == target，忽略了多图层场景。
    现在通过 map_revision_scope_key(target, map_layer) 生成带图层的 scope key，
    同时兼容 "纯 target" 和 "target::map_layer=N" 两种匹配形式。
    当 revision_key 缺失时，要求 explicit_target 精确匹配且无 map_layer，
    避免在多图层情况下错误地将结果归属到其他图层的 revision。
    """
    # 优先从 result 提取 map_layer，回退到 tool_args 中的显式传参
    map_layer = _map_layer_from_result(result)
    if map_layer is None:
        raw_layer = tool_args.get("map_layer")
        if isinstance(raw_layer, int) and not isinstance(raw_layer, bool):
            map_layer = raw_layer
    expected_key = map_revision_scope_key(target, map_layer)
    revision_key = result.get("revision_key")
    # revision_key 存在时，允许匹配纯 target 或带图层的 scope key
    if isinstance(revision_key, str) and revision_key.strip():
        return revision_key.strip() in {target, expected_key}
    # revision_key 缺失时，仅在无图层维度时按 target_path 匹配
    explicit_target = tool_args.get("target_path")
    return (
        isinstance(explicit_target, str) and explicit_target.strip() == target and map_layer is None
    )


def _remember_latest_map_revision(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录最近一次地图工具返回的 revision/layer，供下一次写入补齐 stale 入参。"""
    if not isinstance(result, dict):
        return
    revision = _map_revision_from_result(result)
    prefer_layers = bool(tool_args.get("__auto_map_state_read")) and not any(
        key in tool_args for key in ("map_layer", "ground_map_layer")
    )
    map_layer = _map_layer_from_result(
        result,
        prefer_layers=prefer_layers,
    )
    target = _map_target_from_result(tool_args, result)
    if not target:
        return
    if revision is not None:
        # 优先使用 result 中携带的 revision_key，否则根据 target + map_layer 构造 scope key。
        # 这样可以在多图层场景下正确区分同一 target 的不同图层 revision。
        revision_key_value = result.get("revision_key")
        revision_key = (
            revision_key_value.strip()
            if isinstance(revision_key_value, str) and revision_key_value.strip()
            else map_revision_scope_key(target, map_layer)
        )
        # 注意：latest_revisions 现在同时以 revision_key 和纯 target 两种 key 存储，
        # revision_key 用于精确的图层级查询，纯 target 用于向后兼容无图层的查询路径。
        previous = session.map_task_state.latest_revisions.get(revision_key)
        if previous is not None and revision < previous and tool_name == "describe_map_region":
            if _map_revision_identity_is_authoritative(tool_args, result, target):
                _invalidate_stale_map_revision_state(
                    session,
                    target,
                    previous,
                    revision,
                )
                revisions = dict(session.map_task_state.latest_revisions)
                revisions[revision_key] = revision
                revisions[target] = revision
                replace_map_state_field(
                    session.map_task_state,
                    "latest_revisions",
                    revisions,
                    target=target,
                    revision=revision,
                )
                record_map_revision(session.map_task_state, target, revision)
                previous = revision
            else:
                logger.warning(
                    "Ignored ambiguous lower map revision session=%s target=%s "
                    "previous=%s candidate=%s revision_key=%s",
                    session.session_id,
                    target,
                    previous,
                    revision,
                    result.get("revision_key"),
                )
                revision = None
        if revision is not None and (previous is None or revision > previous):
            if str(result.get("error_code", "")) == "map_revision_conflict":
                _invalidate_stale_map_revision_state(
                    session,
                    target,
                    previous if isinstance(previous, int) else revision,
                    revision,
                )
            touched_region = _map_write_touched_region(tool_args, result)
            if (
                previous is not None
                and tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                and isinstance(touched_region, dict)
            ):
                _promote_unaffected_map_region_cache(
                    session,
                    target,
                    previous,
                    revision,
                    touched_region,
                    map_layer,
                )
            # 双 key 写入：revision_key（精确到图层）+ target（向后兼容），
            # 保证 latest_map_revision() 无论按哪种 key 查询都能拿到最新值。
            revisions = dict(session.map_task_state.latest_revisions)
            revisions[revision_key] = revision
            revisions[target] = revision
            replace_map_state_field(
                session.map_task_state,
                "latest_revisions",
                revisions,
                target=target,
                revision=revision,
            )
            record_map_revision(session.map_task_state, target, revision)
            streaks = dict(session.map_task_state.no_progress_streaks)
            streaks[target] = 0
            replace_map_state_field(
                session.map_task_state,
                "no_progress_streaks",
                streaks,
                target=target,
                revision=revision,
            )
            increment_map_counter(
                session.map_task_state,
                "revision_advances",
                target=target,
                revision=revision,
            )
            logger.info(
                "Latest map revision updated session=%s target=%s previous=%s current=%s",
                session.session_id,
                target,
                previous,
                revision,
            )
    if map_layer is not None:
        previous_layer = session.map_task_state.latest_layers.get(target)
        latest_layers = dict(session.map_task_state.latest_layers)
        latest_layers[target] = map_layer
        replace_map_state_field(
            session.map_task_state,
            "latest_layers",
            latest_layers,
            target=target,
            revision=revision if isinstance(revision, int) else None,
        )
        logger.info(
            "Latest map layer updated session=%s target=%s previous=%s current=%s",
            session.session_id,
            target,
            previous_layer,
            map_layer,
        )


def _map_write_touched_region(
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """从写结果或矩形入参提取精确失效区域。"""
    for value in (result.get("touched_region"), result.get("region"), tool_args.get("region")):
        if isinstance(value, dict):
            return value
    if all(key in tool_args for key in ("x", "y", "width", "height")):
        return {
            "x": tool_args.get("x", 0),
            "y": tool_args.get("y", 0),
            "z": tool_args.get("z", 0),
            "width": tool_args.get("width", 1),
            "height": tool_args.get("height", 1),
            "depth": tool_args.get("depth", 1),
        }
    return None


def _regions_intersect(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判断两个二维或三维整数区域是否相交。"""
    try:
        for axis, size in (("x", "width"), ("y", "height"), ("z", "depth")):
            left_start = int(left.get(axis, 0))
            right_start = int(right.get(axis, 0))
            left_end = left_start + int(left.get(size, 1))
            right_end = right_start + int(right.get(size, 1))
            if left_end <= right_start or right_end <= left_start:
                return False
        return True
    except (TypeError, ValueError):
        return True


def _promote_unaffected_map_region_cache(
    session: Session,
    target: str,
    previous_revision: int,
    current_revision: int,
    touched_region: dict[str, Any],
    touched_layer: int | None,
) -> None:
    """写入后仅失效相交区域，并把未相交缓存提升到新 revision。"""
    region_reads = dict(session.map_task_state.region_reads)
    region_summaries = dict(session.map_task_state.region_summaries)
    for signature, cached_revision in list(region_reads.items()):
        parsed = _parsed_map_region_read_signature(signature)
        if parsed is None or cached_revision != previous_revision or parsed[0] != target:
            continue
        _, layer, x, y, z, width, height, depth = parsed
        region = {
            "x": x,
            "y": y,
            "z": z,
            "width": width,
            "height": height,
            "depth": depth,
        }
        intersects = (touched_layer is None or layer == touched_layer) and _regions_intersect(
            region, touched_region
        )
        if intersects:
            region_reads.pop(signature, None)
            region_summaries.pop(signature, None)
        else:
            region_reads[signature] = current_revision
    replace_map_state_field(
        session.map_task_state,
        "region_reads",
        region_reads,
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        session.map_task_state,
        "region_summaries",
        region_summaries,
        target=target,
        revision=current_revision,
    )

    targets = session.map_task_state.context_state.get("targets", {})
    target_state = targets.get(target, {}) if isinstance(targets, dict) else {}
    layers = target_state.get("layers", {}) if isinstance(target_state, dict) else {}
    if not isinstance(layers, dict):
        return
    for layer_key, layer_state in layers.items():
        regions = layer_state.get("recent_regions", []) if isinstance(layer_state, dict) else []
        if not isinstance(regions, list):
            continue
        layer: int | None
        try:
            layer = int(layer_key)
        except (TypeError, ValueError):
            layer = None
        kept: list[Any] = []
        for entry in regions:
            if not isinstance(entry, dict) or entry.get("map_revision") != previous_revision:
                kept.append(entry)
                continue
            region = entry.get("region", {})
            intersects = (
                (touched_layer is None or layer == touched_layer)
                and isinstance(region, dict)
                and _regions_intersect(region, touched_region)
            )
            if not intersects:
                entry["map_revision"] = current_revision
                kept.append(entry)
        layer_state["recent_regions"] = kept


def _map_validation_is_successful(result: dict[str, Any]) -> bool:
    """判断一次地图校验是否真的允许完成，不采信规划器的口头结论。"""
    passed = result.get("passed")
    passed_ok = passed is True if isinstance(passed, bool) else False
    return passed_ok and result.get("blocking_completion") is not True


def _map_validation_fingerprint(
    tool_name: str,
    result: dict[str, Any],
    target: str,
    revision: int | None,
) -> str:
    """为同一目标、版本、区域和问题生成稳定的校验失败指纹。"""
    region = result.get("region")
    issues = result.get("issues")
    structured_issues = result.get("structured_issues")
    payload = {
        "tool": tool_name,
        "target": target,
        "revision": revision,
        "region": region if isinstance(region, dict) else region,
        "issues": issues if isinstance(issues, list) else [],
        "structured_issues": (structured_issues if isinstance(structured_issues, list) else []),
        "passed": result.get("passed"),
        "blocking_completion": result.get("blocking_completion"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _remember_map_validation(
    session: Session,
    tool_name: str,
    result: dict[str, Any],
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    """持久化真实校验状态，并统计无新写入的重复失败次数。"""
    target = str(result.get("target", result.get("target_path", tool_args.get("target_path", ""))))
    revision = result.get("map_revision")
    revision_value = (
        revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    )
    raw_issues = result.get("issues")
    raw_structured_issues = result.get("structured_issues")
    normalized_issues = list(raw_issues) if isinstance(raw_issues, list) else []
    normalized_structured_issues = (
        list(raw_structured_issues) if isinstance(raw_structured_issues, list) else []
    )
    collection_error: str | None = None
    if raw_issues is not None and not isinstance(raw_issues, list):
        collection_error = "validation_issues_malformed"
    elif raw_structured_issues is not None and not isinstance(raw_structured_issues, list):
        collection_error = "validation_structured_issues_malformed"
    scope_error: str | None = None
    if not target:
        scope_error = "validation_target_missing"
    elif revision_value is None:
        scope_error = "validation_revision_missing"
    contract_error = scope_error or collection_error
    if contract_error is not None:
        normalized_issues.append(contract_error)
        normalized_structured_issues.append(
            {
                "code": contract_error,
                "message": (
                    "validation result lacks an exact target/revision scope"
                    if scope_error is not None
                    else "validation result issues fields violate their collection contract"
                ),
            }
        )
    normalized_result = {
        **result,
        "issues": normalized_issues,
        "structured_issues": normalized_structured_issues,
        "passed": result.get("passed") is True and contract_error is None,
        "blocking_completion": (
            result.get("blocking_completion") is True or contract_error is not None
        ),
    }
    fingerprint = _map_validation_fingerprint(
        tool_name,
        normalized_result,
        target,
        revision_value,
    )
    previous = session.map_task_state.latest_validations.get(target)
    previous_fingerprint = previous.get("fingerprint") if isinstance(previous, dict) else None
    previous_revision = previous.get("map_revision") if isinstance(previous, dict) else None
    count = session.map_task_state.validation_failure_counts.get(fingerprint, 0)
    if not _map_validation_is_successful(normalized_result):
        count = (
            count + 1
            if previous_fingerprint == fingerprint and previous_revision == revision_value
            else 1
        )
        failure_counts = dict(session.map_task_state.validation_failure_counts)
        failure_counts[fingerprint] = count
        replace_map_state_field(
            session.map_task_state,
            "validation_failure_counts",
            failure_counts,
            target=target or "__validation__",
            revision=revision_value or 0,
        )
    else:
        count = 0
    state = {
        **normalized_result,
        "target": target,
        "map_revision": revision_value,
        "region": normalized_result.get("region", {}),
        "passed": normalized_result.get("passed") is True,
        "blocking_completion": normalized_result.get("blocking_completion") is True,
        "issues": normalized_issues,
        "structured_issues": normalized_structured_issues,
        "fingerprint": fingerprint,
        "repeat_count": count,
        "next_stage": (
            "reviewer" if _map_validation_is_successful(normalized_result) else "planner"
        ),
        "scope_error": scope_error,
        "contract_error": contract_error,
    }
    record_map_validation(
        session.map_task_state,
        target or "__validation__",
        revision_value or 0,
        state,
    )
    return state


def _map_completion_blocker(
    tool_name: str, status: str, result: Any, error_code: str | None
) -> dict[str, Any] | None:
    """从地图工具结果中提取阻断最终完成的原因。"""
    if tool_name not in _MAP_COMPLETION_TOOL_NAMES:
        return None
    result_dict = result if isinstance(result, dict) else {}
    target = str(result_dict.get("target", result_dict.get("target_path", "")))
    revision = result_dict.get("map_revision")
    revision_value = (
        revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    )
    workflow_constraints = result_dict.get("workflow_constraints", [])
    if not isinstance(workflow_constraints, list):
        workflow_constraints = []
    if status != "applied":
        return {
            "tool": tool_name,
            "reason": error_code or status,
            "issues": [str(error_code or status)],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }

    issues = result_dict.get("issues")
    if not isinstance(issues, list):
        validation = result_dict.get("validation")
        issues = validation.get("issues", []) if isinstance(validation, dict) else []
    normalized_issues = [str(issue) for issue in issues if str(issue).strip()]

    if bool(result_dict.get("blocking_completion", False)):
        return {
            "tool": tool_name,
            "reason": "blocking_completion",
            "issues": normalized_issues or ["map tool reported blocking_completion=true"],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    if (
        tool_name not in MAP_VALIDATION_TOOL_NAMES
        and result_dict.get("completion_allowed") is False
    ):
        return {
            "tool": tool_name,
            "reason": "completion_not_allowed",
            "issues": normalized_issues or ["map tool reported completion_allowed=false"],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    if (
        tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
        and result_dict.get("completion_allowed") is not True
    ):
        return {
            "tool": tool_name,
            "reason": "map_write_requires_validation",
            "issues": [
                "map write applied but no successful same-revision validation has cleared completion"
            ],
            "target": target,
            "required_revision": revision_value,
            "workflow_constraints": workflow_constraints,
        }
    return None


def _same_map_target(blocker: dict[str, Any], target: str) -> bool:
    """判断阻断项是否属于同一地图目标。"""
    blocker_target = str(blocker.get("target", ""))
    return blocker_target == "" or target == "" or blocker_target == target


def _blocker_revision(blocker: dict[str, Any]) -> int | None:
    """读取阻断项要求的 map revision。"""
    value = blocker.get("required_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clear_validation_blockers(
    blockers: list[dict[str, Any]],
    target: str,
    revision: int | None,
    validator: str,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    """按同 revision 验证结果清除或缩减写后校验阻断。"""
    validation_reasons = {
        "map_write_requires_validation",
        "completion_not_allowed",
        "blocking_completion",
    }
    remaining: list[dict[str, Any]] = []
    for blocker in blockers:
        if blocker.get("reason") not in validation_reasons:
            remaining.append(blocker)
            continue
        blocker_revision = _blocker_revision(blocker)
        if _same_map_target(blocker, target) and (
            revision is None or blocker_revision is None or revision >= blocker_revision
        ):
            constraints = blocker.get("workflow_constraints", [])
            if isinstance(constraints, list) and constraints:
                remaining_constraints = [
                    constraint
                    for constraint in constraints
                    if not (
                        isinstance(constraint, dict)
                        and constraint.get("validator") == validator
                        and isinstance(constraint.get("required_args", {}), dict)
                        and all(
                            args.get(key) == value
                            for key, value in constraint.get("required_args", {}).items()
                        )
                    )
                ]
                if remaining_constraints:
                    remaining.append({**blocker, "workflow_constraints": remaining_constraints})
                    continue
            continue
        remaining.append(blocker)
    return remaining


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
