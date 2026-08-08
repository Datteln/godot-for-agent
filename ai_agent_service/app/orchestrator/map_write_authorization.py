"""平台写授权：已批准批次校验、写阶段门禁与提交后消费。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_planning_snapshots import ApprovedBatchStore
from app.orchestrator.map_workflow import replace_map_state_field
from .map_context import _target, active_planning_snapshot, latest_map_revision
from .map_failure_guard import record_no_progress
from .map_validation import _validation_scope
if TYPE_CHECKING:
    from app.sessions.store import Session
def map_write_stage_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    project_root: Path | None = None,
) -> str | None:
    """只允许平台写入执行同作用域内校验通过的编译批次。

    所有 validation_workflows 和 revision 查询均通过 map_task_state 统一访问，
    revision 查询使用 latest_map_revision 以支持图层感知。
    """
    target = _target(tool_args)
    scope = _validation_scope(tool_args)
    # 统一通过 map_task_state 访问验证工作流状态
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    # 图层感知的 revision 查询
    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    revision = latest_map_revision(session, target, map_layer)
    if workflow.get("map_revision") == revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, scope, "write_attempted_before_planning")
        frontier = session.map_task_state.failure_frontier or {}
        reason = str(
            frontier.get("error_code") or frontier.get("blocked_reason") or "platform_plan_required"
        )
        if reason == "entry_anchor_not_found":
            recovery = (
                "上次 validate_platform_level_plan 失败：entry_anchor_not_found。"
                "请先 describe_map_region 读取相同 target_path/map_layer 的现有连接边界，"
                "由 LLM 修订 entry_anchor/frontier.cell、首个平台和首段路线，再重新提交校验。"
            )
        else:
            recovery = (
                f"上次平台方案校验失败：{reason}。请由 LLM 根据 issues/repair_plan "
                "修改显式 platforms/segments，并重新提交 validate_platform_level_plan。"
            )
        return (
            f"map revision {revision} 的当前图层尚无可执行平台方案。{recovery}"
            f"在校验通过前禁止调用 {tool_name}；该工具只能执行校验器返回的 edit_map_batches。"
        )

    approval = session.map_task_state.approved_platform_plans.get(scope)
    if not isinstance(approval, dict):
        if _looks_like_platform_route_write(tool_name, tool_args):
            record_no_progress(session, scope, "platform_write_without_validated_plan")
            return (
                "拒绝平台路线写入：带有 platform/ground 语义的可站立瓦片必须来自 "
                "validate_platform_level_plan 校验通过后返回的 edit_map_batches。"
                "请勿让 writer 自行拼接 fill。"
            )
        return None
    approval_snapshot_id = str(approval.get("snapshot_id", ""))
    approval_snapshot_digest = str(approval.get("snapshot_digest", ""))
    if not approval_snapshot_id or not approval_snapshot_digest:
        return (
            "legacy_platform_approval_requires_replan：旧批准记录缺少 snapshot id/digest，"
            "不能迁移为新的写入授权。请读取权威快照并重新规划、编译。"
        )
    active_snapshot = active_planning_snapshot(
        session,
        target,
        map_layer if map_layer is not None else 0,
    )
    if (
        active_snapshot is None
        or active_snapshot.get("snapshot_id") != approval_snapshot_id
        or active_snapshot.get("digest") != approval_snapshot_digest
    ):
        approvals = dict(session.map_task_state.approved_platform_plans)
        approvals.pop(scope, None)
        replace_map_state_field(
            session.map_task_state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=revision,
        )
        return (
            "platform_approval_snapshot_stale：批准批次的快照身份不再是当前权威事实；"
            "旧批准已失效，必须刷新事实并重新规划。"
        )
    approval_revision = approval.get(
        "expected_revision",
        approval.get("map_revision"),
    )
    if approval_revision != revision:
        approvals = dict(session.map_task_state.approved_platform_plans)
        approvals.pop(scope, None)
        replace_map_state_field(
            session.map_task_state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=revision,
        )
        return (
            f"平台方案基于 map revision {approval_revision}，当前 revision 为 "
            f"{revision}；旧编译批次已失效，请重新读取边界并提交 "
            "validate_platform_level_plan。"
        )
    records = _platform_approval_records(approval, target)
    matched_index = next(
        (
            index
            for index, record in enumerate(records)
            if record.get("expected_revision") == revision
            and record.get("snapshot_id") == approval_snapshot_id
            and record.get("snapshot_digest") == approval_snapshot_digest
            and record.get("batch_fingerprint")
            == _platform_batch_fingerprint(
                str(record.get("batch", {}).get("tool", "edit_map")),
                record.get("batch", {}),
                target,
                revision if isinstance(revision, int) else -1,
                approval_snapshot_id,
                approval_snapshot_digest,
                map_layer if map_layer is not None else 0,
            )
            and _compiled_batch_matches(
                tool_name,
                tool_args,
                record.get("batch"),
            )
        ),
        None,
    )
    if matched_index is None:
        record_no_progress(session, scope, "write_not_from_validated_platform_plan")
        return (
            "拒绝平台地图写入：当前调用不是 validate_platform_level_plan 校验通过后"
            "编译出的剩余 edit_map_batches。禁止 coordinator/writer 临时拼接连续实心 "
            "fill、修改批次 operations，或执行未获批准的可站立路线。"
        )
    record = records[matched_index]
    if project_root is not None:
        artifact_ref = str(record.get("artifact_ref", "")).strip()
        if not artifact_ref:
            return (
                "approved_batch_artifact_required：批准记录缺少不可变 artifact；"
                "恢复后不能据此创建写事务，请重新编译规划。"
            )
        try:
            persisted_record = ApprovedBatchStore(
                project_root,
                session.session_id,
                session.session_epoch,
            ).read(artifact_ref)
        except (OSError, TypeError, ValueError):
            return (
                "approved_batch_artifact_invalid：批准 artifact 无法通过会话或完整性校验；"
                "禁止写入并要求重新编译。"
            )
        for identity_field in (
            "approval_id",
            "snapshot_id",
            "snapshot_digest",
            "target",
            "map_layer",
            "expected_revision",
            "batch_fingerprint",
            "batch",
        ):
            if persisted_record.get(identity_field) != record.get(identity_field):
                return (
                    "approved_batch_artifact_mismatch：恢复状态与批准 artifact 不一致；"
                    "禁止写入并要求重新编译。"
                )
    approved_batch = record.get("batch", {})
    tool_args["plan_version"] = record.get(
        "plan_version",
        approval.get("plan_version"),
    )
    tool_args["batch_index"] = approved_batch.get("batch_index", matched_index)
    tool_args["validated_platform_batch"] = True
    tool_args["approval_id"] = record.get("approval_id")
    tool_args["approval_batch_fingerprint"] = record.get("batch_fingerprint")
    tool_args["approval_expected_revision"] = record.get("expected_revision")
    tool_args["approval_snapshot_id"] = record.get("snapshot_id")
    tool_args["approval_snapshot_digest"] = record.get("snapshot_digest")
    tool_args["approval_target_path"] = record.get("target")
    tool_args["approval_map_layer"] = record.get("map_layer")
    # Preflight is deliberately non-consuming. The matching record is removed
    # only after Godot returns a durable committed transaction result.
    return None


def _compiled_batch_matches(
    tool_name: str,
    tool_args: dict[str, Any],
    batch: Any,
) -> bool:
    """判断实际写入是否逐字段对应一个校验器编译批次。"""
    if not isinstance(batch, dict) or batch.get("tool") != tool_name:
        return False
    expected_operations = batch.get("operations")
    actual_operations = tool_args.get("operations")
    if expected_operations != actual_operations:
        return False
    expected_cells = batch.get("expected_cells")
    return expected_cells is None or tool_args.get("expected_cells") == expected_cells


def _platform_batch_fingerprint(
    tool_name: str,
    batch: dict[str, Any],
    target: str,
    expected_revision: int,
    snapshot_id: str = "",
    snapshot_digest: str = "",
    map_layer: int = 0,
) -> str:
    """Return the canonical immutable identity of one approved batch."""
    payload = {
        "tool": tool_name,
        "target": target,
        "map_layer": map_layer,
        "expected_revision": expected_revision,
        "snapshot_id": snapshot_id,
        "snapshot_digest": snapshot_digest,
        "operations": batch.get("operations"),
        "expected_cells": batch.get("expected_cells"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _platform_approval_records(
    approval: dict[str, Any],
    target: str,
) -> list[dict[str, Any]]:
    """Normalize persisted approval data without mutating the stored record."""
    records_value = approval.get("records")
    if isinstance(records_value, list):
        return [
            deepcopy(record)
            for record in records_value
            if isinstance(record, dict) and isinstance(record.get("batch"), dict)
        ]
    # Compatibility for schema versions that stored a mutable remaining queue.
    batches_value = approval.get("remaining_batches")
    batches = batches_value if isinstance(batches_value, list) else []
    base_revision = approval.get("map_revision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        return []
    plan_version = approval.get("plan_version")
    records: list[dict[str, Any]] = []
    for index, batch_value in enumerate(batches):
        if not isinstance(batch_value, dict):
            continue
        batch = deepcopy(batch_value)
        tool_name = str(batch.get("tool", "edit_map"))
        expected_revision = base_revision + index
        fingerprint = _platform_batch_fingerprint(
            tool_name,
            batch,
            target,
            expected_revision,
        )
        records.append(
            {
                "approval_id": hashlib.sha256(
                    f"{target}:{plan_version}:{fingerprint}".encode()
                ).hexdigest()[:32],
                "target": target,
                "expected_revision": expected_revision,
                "batch_fingerprint": fingerprint,
                "plan_version": plan_version,
                "batch": batch,
            }
        )
    return records


def _looks_like_platform_route_write(
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """识别声明为平台或可站立地面的直接瓦片写入。"""
    if tool_name != "edit_map":
        return False
    operations = tool_args.get("operations")
    if not isinstance(operations, list):
        return False
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        semantic = str(operation.get("semantic_layer", "")).strip().lower()
        tags_value = operation.get("tags")
        tags = (
            {str(tag).strip().lower() for tag in tags_value}
            if isinstance(tags_value, list)
            else set()
        )
        if semantic == "ground" or "platform" in tags:
            return True
    return False


def platform_write_requires_validation(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """判断平台写入是否必须先执行或重新执行平台方案校验。

    使用 latest_map_revision 进行图层感知查询，确保不同图层的 approval
    和 revision 比较在各自作用域内进行。

    Args:
        session: 当前地图任务会话。
        tool_name: 待执行的地图写工具名。
        tool_args: 待执行工具的结构化参数。

    Returns:
        当前写入属于未批准的平台路线时返回 True，否则返回 False。
    """
    if not _looks_like_platform_route_write(tool_name, tool_args):
        return False

    target = _target(tool_args)
    scope = _validation_scope(tool_args)
    approval = session.map_task_state.approved_platform_plans.get(scope)
    if not isinstance(approval, dict):
        return True

    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    revision = latest_map_revision(session, target, map_layer)
    if (
        approval.get(
            "expected_revision",
            approval.get("map_revision"),
        )
        != revision
    ):
        return True

    records = _platform_approval_records(approval, target)
    return not any(
        record.get("expected_revision") == revision
        and _compiled_batch_matches(tool_name, tool_args, record.get("batch"))
        for record in records
    )


def consume_committed_platform_approvals(
    session: Session,
    result: dict[str, Any],
    transaction_entry: dict[str, Any],
) -> bool:
    """Consume immutable approvals only from a matching durable commit result."""
    if result.get("map_transaction_status") != "committed":
        return False
    committed_revision = result.get("committed_revision", result.get("map_revision"))
    if not isinstance(committed_revision, int) or isinstance(committed_revision, bool):
        return False
    claimed_value = result.get(
        "approval_records",
        transaction_entry.get("approval_records"),
    )
    if not isinstance(claimed_value, list) or not claimed_value:
        return False
    claimed = {
        str(record.get("approval_id", "")): str(record.get("batch_fingerprint", ""))
        for record in claimed_value
        if isinstance(record, dict)
        and str(record.get("approval_id", "")).strip()
        and str(record.get("batch_fingerprint", "")).strip()
    }
    if not claimed:
        return False

    state = session.map_task_state
    approvals = dict(state.approved_platform_plans)
    workflows = dict(state.validation_workflows)
    consumed_any = False
    for scope, approval_value in list(approvals.items()):
        approval = deepcopy(approval_value)
        target = str(approval.get("target", scope.split("::", 1)[0]))
        records = _platform_approval_records(approval, target)
        matched = [
            record
            for record in records
            if claimed.get(str(record.get("approval_id", "")))
            == str(record.get("batch_fingerprint", ""))
        ]
        if not matched:
            continue
        expected_committed_revision = (
            max(int(record.get("expected_revision", -1)) for record in matched) + 1
        )
        if expected_committed_revision != committed_revision:
            continue
        consumed_ids = {str(record.get("approval_id", "")) for record in matched}
        remaining = [
            record for record in records if str(record.get("approval_id", "")) not in consumed_ids
        ]
        if remaining:
            approval["records"] = remaining
            approval["expected_revision"] = min(
                int(record["expected_revision"]) for record in remaining
            )
            approval["map_revision"] = approval["expected_revision"]
            approvals[scope] = approval
        else:
            approvals.pop(scope, None)
        workflow_value = workflows.get(scope)
        if isinstance(workflow_value, dict):
            workflow = dict(workflow_value)
            workflow["map_revision"] = committed_revision
            workflow["next_stage"] = "write" if remaining else "validator"
            workflows[scope] = workflow
        consumed_any = True
    if not consumed_any:
        return False
    replace_map_state_field(
        state,
        "approved_platform_plans",
        approvals,
        revision=committed_revision,
    )
    replace_map_state_field(
        state,
        "validation_workflows",
        workflows,
        revision=committed_revision,
    )
    return True
