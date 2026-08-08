"""规划进度记录：成功/失败结果落账、批次批准与规划发布。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_planning_contexts import MapExecutionOperation
from app.orchestrator.map_planning_snapshots import (
    ApprovedBatchStore,
    PlanningRepairStore,
)
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    record_semantic_retry,
)
from app.orchestrator.map_workers import (
    MAP_PLAN_TOOL_NAMES,
    PLATFORM_PLAN_TOOL_NAMES,
)
from app.orchestrator.map_workflow import replace_map_state_field
from .map_context import _target, latest_map_revision
from .map_platform_planning import _remember_platform_plan_attempt, parse_map_plan_outcome
from .map_state import MapTaskState
from .map_validation import _validation_scope
from .map_write_authorization import _platform_batch_fingerprint
if TYPE_CHECKING:
    from app.sessions.store import Session
def remember_map_plan_progress(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """有效规划完成后允许执行阶段写入，但仍要求新 revision 后再 completion。

    失败的平台规划会记入通用 no-progress 语义重试并返回该重试条目（含 exhausted 标志），
    供 planner 循环据此触发确定性收尾；成功或非平台规划工具返回 None。
    """
    if tool_name not in MAP_PLAN_TOOL_NAMES:
        return None
    attempt_scope, attempt_count, candidate_fingerprint = _remember_platform_plan_attempt(
        session, tool_name, tool_args
    )
    outcome = parse_map_plan_outcome(tool_name, result)
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
    layer = scope_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    current_revision = latest_map_revision(session, target, map_layer)
    snapshot_revision_value = tool_args.get("authoritative_snapshot_revision", 0)
    retry_revision = (
        current_revision
        if isinstance(current_revision, int) and not isinstance(current_revision, bool)
        else (
            snapshot_revision_value
            if isinstance(snapshot_revision_value, int)
            and not isinstance(snapshot_revision_value, bool)
            else 0
        )
    )
    snapshot_id = str(tool_args.get("authoritative_snapshot_id", ""))
    snapshot_digest = str(tool_args.get("authoritative_snapshot_digest", ""))

    if tool_name in PLATFORM_PLAN_TOOL_NAMES:
        issues_value = result.get("issues", result.get("repair_plan", []))
        issues = deepcopy(issues_value)[:12] if isinstance(issues_value, list) else []
        repair_artifact: dict[str, str] = {}
        if not outcome.executable and project_root is not None:
            repair_artifact = PlanningRepairStore(
                project_root,
                session.session_id,
                session.session_epoch,
            ).store(
                {
                    "attempt_scope": attempt_scope,
                    "attempt": attempt_count,
                    "candidate_fingerprint": candidate_fingerprint,
                    "snapshot_id": snapshot_id,
                    "snapshot_digest": snapshot_digest,
                    "issues": issues,
                    "repair_plan": deepcopy(result.get("repair_plan", [])),
                }
            )
        histories = dict(session.map_task_state.planning_attempt_history)
        history = list(histories.get(attempt_scope, []))
        history.append(
            {
                "attempt": attempt_count,
                "candidate_fingerprint": candidate_fingerprint,
                "snapshot_id": snapshot_id,
                "snapshot_digest": snapshot_digest,
                "map_revision": current_revision,
                "passed": outcome.executable,
                "error_code": outcome.error_code,
                "blocked_reason": outcome.blocked_reason,
                "issues": issues,
                **repair_artifact,
            }
        )
        histories[attempt_scope] = history[-3:]
        replace_map_state_field(
            session.map_task_state,
            "planning_attempt_history",
            histories,
            target=target,
            revision=retry_revision,
        )

    if not outcome.executable:
        # 规划工具可能以成功响应承载诊断结果；任何失败都必须留在规划阶段恢复。
        state = session.map_task_state
        state.transition_stage("plan")
        approvals = dict(state.approved_platform_plans)
        approvals.pop(scope, None)
        replace_map_state_field(
            state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=current_revision,
        )
        repair_plan_value = result.get("repair_plan") or result.get("issues")
        repair_plan_list = repair_plan_value if isinstance(repair_plan_value, list) else []
        replace_map_state_field(
            state,
            "failure_frontier",
            {
                "tool": tool_name,
                "blocked_reason": outcome.blocked_reason,
                "error_code": outcome.error_code,
                "suggested_foothold": outcome.suggested_foothold,
                "repair_plan": repair_plan_list[:6],
            },
            target=target,
            revision=current_revision,
        )
        replace_map_state_field(
            state,
            "unresolved_issues",
            [
                {
                    "kind": "map_plan_not_executable",
                    "tool": tool_name,
                    "blocked_reason": outcome.blocked_reason,
                    "error_code": outcome.error_code,
                }
            ],
            target=target,
            revision=current_revision,
        )
        if tool_name in PLATFORM_PLAN_TOOL_NAMES:
            workflow = session.map_task_state.validation_workflows.get(scope, {})
            workflow["map_revision"] = current_revision
            workflow["next_stage"] = "planner"
            workflow["plan_tool"] = tool_name
            workflow["plan_error_code"] = outcome.error_code or outcome.blocked_reason
            workflows = dict(session.map_task_state.validation_workflows)
            workflows[scope] = workflow
            replace_map_state_field(
                session.map_task_state,
                "validation_workflows",
                workflows,
                target=target,
                revision=current_revision,
            )
        else:
            return None
        # 失败的平台规划记入通用 no-progress 语义重试（替代 plan-specific 计数上限）；
        # operation 用 scope 身份，使同一 scope 下相同 error_code 反复出现时累积 streak，
        # 不同 error_code 视为进展（各自独立 streak），与 no-progress 语义一致。
        retry_entry: dict[str, Any] = record_semantic_retry(
            session.map_task_state,
            category="validation_failure",
            error_category=str(
                outcome.error_code or outcome.blocked_reason or "platform_plan_failed"
            ),
            root_cause=str(outcome.blocked_reason or outcome.error_code or "platform_plan_failed"),
            stage="planner",
            target=target,
            revision=retry_revision,
            operation={
                "tool": tool_name,
                "target_path": target,
                "map_layer": scope_args.get("map_layer"),
            },
            threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
        )
        retry_entry["attempt_count"] = attempt_count
        retry_entry["attempt_limit"] = 3
        retry_entry["exhausted"] = attempt_count >= 3
        if attempt_count >= 3:
            publication = {
                "planning_status": "delivered",
                "execution_status": "blocked_by_validation",
                "target_path": target,
                "map_layer": map_layer,
                "map_revision": current_revision,
                "authoritative_snapshot": {
                    "snapshot_id": snapshot_id,
                    "digest": snapshot_digest,
                },
                "semantic_plan": _semantic_plan(tool_args),
                "unresolved_issues": repair_plan_list[:12],
                "validation_history": deepcopy(
                    session.map_task_state.planning_attempt_history.get(
                        attempt_scope,
                        [],
                    )
                ),
                "approved_batches": [],
            }
            _record_planning_publication(
                state,
                attempt_scope,
                publication,
                target=target,
                revision=current_revision,
            )
            result["_planning_publication"] = deepcopy(publication)
            result["planning_status"] = "delivered"
            result["execution_status"] = "blocked_by_validation"
            result["edit_map_batches"] = []
        return retry_entry

    active_workflow = session.map_task_state.validation_workflows.get(scope)
    if isinstance(active_workflow, dict) and active_workflow.get("next_stage") == "planner":
        if active_workflow.get("map_revision") != current_revision:
            return None
        active_workflow["next_stage"] = "write"
        active_workflow["plan_tool"] = tool_name
        workflows = dict(session.map_task_state.validation_workflows)
        workflows[scope] = active_workflow
        replace_map_state_field(
            session.map_task_state,
            "validation_workflows",
            workflows,
            target=target,
            revision=current_revision,
        )
    state = session.map_task_state
    state.transition_stage("write")
    next_plan_version = state.plan_version + 1
    replace_map_state_field(
        state,
        "plan_version",
        next_plan_version,
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        state,
        "failure_frontier",
        None,
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        state,
        "unresolved_issues",
        [],
        target=target,
        revision=current_revision,
    )
    streaks = dict(state.no_progress_streaks)
    streaks[scope] = 0
    replace_map_state_field(
        state,
        "no_progress_streaks",
        streaks,
        target=target,
        revision=current_revision,
    )
    if tool_name in PLATFORM_PLAN_TOOL_NAMES:
        batches = _platform_edit_batches(result)
        records: list[dict[str, Any]] = []
        if not isinstance(current_revision, int) or isinstance(current_revision, bool):
            replace_map_state_field(
                state,
                "unresolved_issues",
                ["platform_approval_revision_missing"],
                target=target,
                revision=None,
            )
            state.transition_stage("plan")
            return None
        for index, batch in enumerate(batches):
            batch["batch_index"] = index
            batch_tool = str(batch.get("tool", "edit_map"))
            expected_revision = current_revision + index
            fingerprint = _platform_batch_fingerprint(
                batch_tool,
                batch,
                target,
                expected_revision,
                snapshot_id,
                snapshot_digest,
                map_layer or 0,
            )
            records.append(
                {
                    "approval_id": hashlib.sha256(
                        (
                            f"{target}:{next_plan_version}:" f"{expected_revision}:{fingerprint}"
                        ).encode()
                    ).hexdigest()[:32],
                    "target": target,
                    "map_layer": map_layer or 0,
                    "expected_revision": expected_revision,
                    "snapshot_id": snapshot_id,
                    "snapshot_digest": snapshot_digest,
                    "batch_fingerprint": fingerprint,
                    "plan_version": next_plan_version,
                    "batch": deepcopy(batch),
                }
            )
            operation = MapExecutionOperation(
                operation_id=f"map-operation:{records[-1]['approval_id']}",
                target_path=target,
                map_layer=map_layer or 0,
                expected_revision=expected_revision,
                write_payload={"tool": batch_tool, "args": deepcopy(batch)},
            )
            records[-1]["execution_operation"] = operation.to_dict()
            if project_root is not None:
                locator = ApprovedBatchStore(
                    project_root,
                    session.session_id,
                    session.session_epoch,
                ).store(records[-1])
                records[-1].update(locator)
        execution_operations = dict(state.execution_operations)
        for record in records:
            operation_value = record.get("execution_operation")
            if not isinstance(operation_value, dict):
                continue
            operation_id = str(operation_value.get("operation_id", ""))
            if operation_id:
                execution_operations[operation_id] = deepcopy(operation_value)
        replace_map_state_field(
            state,
            "execution_operations",
            execution_operations,
            target=target,
            revision=current_revision,
        )
        approvals = dict(state.approved_platform_plans)
        approvals[scope] = {
            "tool": tool_name,
            "target": target,
            "map_layer": map_layer or 0,
            "expected_revision": current_revision,
            "map_revision": current_revision,
            "snapshot_id": snapshot_id,
            "snapshot_digest": snapshot_digest,
            "plan_version": next_plan_version,
            "records": records,
        }
        replace_map_state_field(
            state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=current_revision,
        )
        publication = {
            "planning_status": "delivered",
            "execution_status": "approved",
            "target_path": target,
            "map_layer": map_layer,
            "map_revision": current_revision,
            "authoritative_snapshot": {
                "snapshot_id": snapshot_id,
                "digest": snapshot_digest,
            },
            "semantic_plan": _semantic_plan(tool_args),
            "unresolved_issues": [],
            "validation_history": deepcopy(state.planning_attempt_history.get(attempt_scope, [])),
            "approved_batches": [
                {
                    key: record.get(key)
                    for key in (
                        "approval_id",
                        "artifact_ref",
                        "batch_id",
                        "snapshot_id",
                        "snapshot_digest",
                        "target",
                        "target_path",
                        "map_layer",
                        "expected_revision",
                        "map_revision",
                        "batch_fingerprint",
                    )
                    if record.get(key) is not None
                }
                | {"execution_operations": [deepcopy(record["execution_operation"])]}
                for record in records
            ],
        }
        _record_planning_publication(
            state,
            attempt_scope,
            publication,
            target=target,
            revision=current_revision,
        )
        result["_planning_publication"] = deepcopy(publication)
        result["planning_status"] = "delivered"
        result["execution_status"] = "approved"
    return None


def _platform_edit_batches(result: dict[str, Any]) -> list[dict[str, Any]]:
    """提取平台校验结果中的可执行地图批次。"""
    profile_value = result.get("profile_plan")
    profile = profile_value if isinstance(profile_value, dict) else {}
    batches_value = result.get("edit_map_batches")
    if batches_value is None:
        batches_value = profile.get("edit_map_batches")
    if not isinstance(batches_value, list):
        return []
    return [deepcopy(batch) for batch in batches_value if isinstance(batch, dict)]


def _semantic_plan(tool_args: dict[str, Any]) -> dict[str, Any]:
    """提取 planner 负责的语义路线，不包含任何裸 atlas 写入。"""
    return {
        "platforms": deepcopy(tool_args.get("platforms", [])),
        "segments": deepcopy(tool_args.get("segments", [])),
        "semantic_resources": deepcopy(
            tool_args.get(
                "semantic_resources",
                [tool_args.get("ground_resource", "ground")],
            )
        ),
        "reference_cells": deepcopy(
            tool_args.get(
                "reference_cells",
                (
                    [tool_args["ground_reference_cell"]]
                    if isinstance(tool_args.get("ground_reference_cell"), dict)
                    else []
                ),
            )
        ),
        "rationale": str(tool_args.get("rationale", "")),
    }


def _record_planning_publication(
    state: MapTaskState,
    attempt_scope: str,
    publication: dict[str, Any],
    *,
    target: str,
    revision: int | None,
) -> None:
    """保存独立于 writer 的最终规划发布物。"""
    publications = dict(state.planning_publications)
    publications[attempt_scope] = deepcopy(publication)
    replace_map_state_field(
        state,
        "planning_publications",
        publications,
        target=target,
        revision=revision,
    )
