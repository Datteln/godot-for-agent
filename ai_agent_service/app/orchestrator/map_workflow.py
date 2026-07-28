"""事件拥有的地图工作流状态、作用域迁移与直写守卫。"""

from __future__ import annotations

import ast
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from app.orchestrator.map_contracts import MAP_RUNTIME_STAGE_TRANSITIONS
from app.orchestrator.runtime_contracts import MapWorkflowEvent

REDUCER_OWNED_FIELDS = frozenset(
    {
        "stage",
        "failure_frontier",
        "unresolved_issues",
        "completed_goals",
        "pending_batches",
        "executed_batches",
        "validation_cache",
        "validation_contracts",
        "validation_workflows",
        "no_progress_streaks",
        "latest_validations",
        "validation_failure_counts",
        "planning_attempts",
        "planning_fingerprints",
        "tool_failure_fingerprints",
        "approved_platform_plans",
        "latest_revisions",
        "region_reads",
        "region_summaries",
        "completion_blockers",
        "checkpoint",
        "pause_report",
        "evidence_registry",
        "retry_registry",
        "transaction_journals",
        "workflow_scopes",
        "workflow_events",
    }
)

_REDUCER_WRITE_DEPTH: ContextVar[int] = ContextVar(
    "map_workflow_reducer_write_depth",
    default=0,
)
def map_workflow_scope_key(target: str, revision: int) -> str:
    """生成所有 blocker、批次、验证、证据和重试共享的规范作用域键。"""
    normalized_target = target.strip()
    if not normalized_target:
        raise ValueError("map workflow target cannot be empty")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("map workflow revision must be a non-negative integer")
    return f"{normalized_target}::revision={revision}"


def assert_map_workflow_write_allowed(field_name: str) -> None:
    """拒绝 reducer 上下文外对 reducer-owned 字段的替换。"""
    if (
        field_name in REDUCER_OWNED_FIELDS
        and _REDUCER_WRITE_DEPTH.get() <= 0
    ):
        raise RuntimeError(
            f"direct write to reducer-owned MapTaskState field: {field_name}"
        )


@contextmanager
def reducer_write_scope() -> Iterator[None]:
    """标记当前调用栈为合法 reducer 写入区域。"""
    token = _REDUCER_WRITE_DEPTH.set(_REDUCER_WRITE_DEPTH.get() + 1)
    try:
        yield
    finally:
        _REDUCER_WRITE_DEPTH.reset(token)


def make_map_workflow_event(
    state: Any,
    event_type: str,
    target: str,
    revision: int,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    turn_id: str | None = None,
) -> MapWorkflowEvent:
    """基于当前事件序号和内容生成可重放的稳定事件 id。"""
    body = {
        "index": len(getattr(state, "workflow_events", [])),
        "type": event_type,
        "target": target,
        "revision": revision,
        "payload": payload or {},
        "request_id": request_id,
        "turn_id": turn_id,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:20]
    return MapWorkflowEvent(
        event_id=f"mwe-{digest}",
        event_type=event_type,
        target=target,
        revision=revision,
        payload=deepcopy(payload or {}),
        request_id=request_id,
        turn_id=turn_id,
    )


def reduce_map_workflow(state: Any, event: MapWorkflowEvent) -> Any:
    """纯归约单个事件并返回深拷贝的新状态。"""
    reduced = deepcopy(state)
    scope_key = map_workflow_scope_key(event.target, event.revision)
    scopes = reduced.workflow_scopes
    scope = deepcopy(scopes.get(scope_key, {}))
    scope["target"] = event.target
    scope["revision"] = event.revision
    payload = deepcopy(event.payload)

    if event.event_type == "stage_transition":
        next_stage = str(payload.get("stage", ""))
        allowed = MAP_RUNTIME_STAGE_TRANSITIONS.get(reduced.stage, frozenset())
        if next_stage not in allowed:
            raise ValueError(
                f"illegal map stage transition: {reduced.stage} -> {next_stage}"
            )
        reduced.stage = next_stage
        scope["stage"] = next_stage
    elif event.event_type == "blockers_replaced":
        blockers = payload.get("blockers", [])
        if not isinstance(blockers, list):
            raise ValueError("blockers_replaced requires blockers array")
        reduced.completion_blockers = deepcopy(blockers)
        scope["blockers"] = deepcopy(blockers)
    elif event.event_type == "checkpoint_replaced":
        checkpoint = payload.get("checkpoint")
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("checkpoint_replaced requires object or null")
        reduced.checkpoint = deepcopy(checkpoint)
        scope["checkpoint"] = deepcopy(checkpoint)
    elif event.event_type == "pending_batches_replaced":
        batches = payload.get("batches", [])
        if not isinstance(batches, list):
            raise ValueError("pending_batches_replaced requires batches array")
        reduced.pending_batches = deepcopy(batches)
        scope["pending_batches"] = deepcopy(batches)
    elif event.event_type == "batch_executed":
        batch = payload.get("batch")
        if not isinstance(batch, dict):
            raise ValueError("batch_executed requires batch object")
        reduced.executed_batches.append(deepcopy(batch))
        scope.setdefault("executed_batches", []).append(deepcopy(batch))
    elif event.event_type == "validation_recorded":
        validation = payload.get("validation")
        if not isinstance(validation, dict):
            raise ValueError("validation_recorded requires validation object")
        reduced.latest_validations[event.target] = deepcopy(validation)
        scope["validation"] = deepcopy(validation)
    elif event.event_type == "evidence_recorded":
        evidence_id = str(payload.get("evidence_id", "")).strip()
        evidence = payload.get("evidence")
        if not evidence_id or not isinstance(evidence, dict):
            raise ValueError("evidence_recorded requires evidence_id and evidence")
        reduced.evidence_registry[evidence_id] = deepcopy(evidence)
        scope.setdefault("evidence_ids", []).append(evidence_id)
    elif event.event_type == "retry_recorded":
        retry_key = str(payload.get("retry_key", "")).strip()
        retry = payload.get("retry")
        if not retry_key or not isinstance(retry, dict):
            raise ValueError("retry_recorded requires retry_key and retry")
        reduced.retry_registry[retry_key] = deepcopy(retry)
        scope.setdefault("retry_keys", []).append(retry_key)
    elif event.event_type == "progress_recorded":
        category = str(payload.get("category", "unknown"))
        count = int(payload.get("count", 0))
        progress = scope.setdefault("progress", {})
        progress[category] = count
        reduced.no_progress_streaks[scope_key] = count
    elif event.event_type == "revision_recorded":
        reduced.latest_revisions[scope_key] = event.revision
        reduced.latest_revisions[event.target] = event.revision
        stale_keys = [
            key
            for key, value in reduced.workflow_scopes.items()
            if isinstance(value, dict)
            and value.get("target") == event.target
            and value.get("revision") != event.revision
        ]
        for stale_key in stale_keys:
            reduced.workflow_scopes.pop(stale_key, None)
    elif event.event_type == "scope_patch":
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            raise ValueError("scope_patch requires patch object")
        scope.update(deepcopy(patch))
    elif event.event_type == "owned_field_replaced":
        field_name = str(payload.get("field", ""))
        if field_name not in REDUCER_OWNED_FIELDS or field_name in {
            "workflow_events",
            "workflow_scopes",
        }:
            raise ValueError(f"field is not replaceable by workflow reducer: {field_name}")
        value = deepcopy(payload.get("value"))
        setattr(reduced, field_name, value)
        scope[field_name] = deepcopy(value)
    else:
        raise ValueError(f"unknown map workflow event type: {event.event_type}")

    reduced.workflow_scopes[scope_key] = scope
    reduced.workflow_events.append(event.to_dict())
    if len(reduced.workflow_events) > 512:
        reduced.workflow_events = reduced.workflow_events[-512:]
    return reduced


def dispatch_map_workflow_event(state: Any, event: MapWorkflowEvent) -> None:
    """归约事件后原子替换现有 MapTaskState 的字段集合。"""
    with reducer_write_scope():
        reduced = reduce_map_workflow(state, event)
        state.__dict__.clear()
        state.__dict__.update(deepcopy(reduced.__dict__))


def replace_map_blockers(
    state: Any,
    blockers: list[dict[str, Any]],
    target: str,
    revision: int,
) -> None:
    """通过事件替换同一目标/revision 的完成阻断项。"""
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "blockers_replaced",
            target,
            revision,
            {"blockers": blockers},
        ),
    )


def workflow_scope_identity(
    state: Any,
    target: str | None = None,
    revision: int | None = None,
) -> tuple[str, int]:
    """从显式值或现有状态解析事件使用的目标与 revision。"""
    normalized_target = (target or "").strip()
    if not normalized_target:
        targets = state.context_state.get("targets", {})
        if isinstance(targets, dict) and targets:
            normalized_target = str(next(iter(targets)))
    if not normalized_target:
        normalized_target = "__workflow__"
    resolved_revision = revision
    if isinstance(resolved_revision, bool) or not isinstance(resolved_revision, int):
        candidate = state.latest_revisions.get(normalized_target)
        resolved_revision = (
            candidate
            if isinstance(candidate, int) and not isinstance(candidate, bool)
            else state.structure_revision
        )
    return normalized_target, max(0, resolved_revision)


def replace_map_state_field(
    state: Any,
    field_name: str,
    value: Any,
    *,
    target: str | None = None,
    revision: int | None = None,
) -> None:
    """通过通用类型化事件替换一个 reducer-owned 兼容投影字段。"""
    scope_target, scope_revision = workflow_scope_identity(
        state,
        target,
        revision,
    )
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "owned_field_replaced",
            scope_target,
            scope_revision,
            {"field": field_name, "value": deepcopy(value)},
        ),
    )


def record_map_revision(state: Any, target: str, revision: int) -> None:
    """通过事件记录 revision 并失效同目标旧 revision 作用域。"""
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "revision_recorded",
            target,
            revision,
        ),
    )


def record_map_validation(
    state: Any,
    target: str,
    revision: int,
    validation: dict[str, Any],
) -> None:
    """通过事件记录同目标/revision 的验证观察。"""
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "validation_recorded",
            target,
            revision,
            {"validation": validation},
        ),
    )


def migrate_legacy_workflow_scopes(state: Any) -> None:
    """把旧的未统一字段投影到 target/revision scope，并丢弃陈旧 gate。"""
    if state.workflow_scopes:
        return
    targets = set(state.latest_revisions) | set(state.latest_validations)
    for target in sorted(targets):
        if "::" in target:
            continue
        revision = state.latest_revisions.get(target)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            continue
        scope_key = map_workflow_scope_key(target, revision)
        scope: dict[str, Any] = {
            "target": target,
            "revision": revision,
            "stage": state.stage,
        }
        validation = state.latest_validations.get(target)
        if isinstance(validation, dict):
            validation_revision = validation.get("map_revision")
            if validation_revision in {None, revision}:
                scope["validation"] = deepcopy(validation)
        blockers = [
            deepcopy(item)
            for item in state.completion_blockers
            if isinstance(item, dict)
            and str(item.get("target", target)) == target
            and item.get("required_revision") in {None, revision}
        ]
        if blockers:
            scope["blockers"] = blockers
        state.workflow_scopes[scope_key] = scope


def find_direct_map_state_writes(paths: list[Path]) -> list[str]:
    """静态扫描显式 `map_task_state.<owned_field> =` 直写位置。"""
    findings: list[str] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if isinstance(node, ast.Assign):
                    targets.extend(node.targets)
                else:
                    targets.append(node.target)
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in REDUCER_OWNED_FIELDS
                    and _looks_like_map_state_expression(target.value)
                ):
                    findings.append(
                        f"{path}:{getattr(node, 'lineno', 0)}:{target.attr}"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"append", "clear", "extend", "insert", "pop", "remove", "setdefault", "update"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in REDUCER_OWNED_FIELDS
                and _looks_like_map_state_expression(node.func.value.value)
            ):
                findings.append(
                    f"{path}:{getattr(node, 'lineno', 0)}:"
                    f"{node.func.value.attr}.{node.func.attr}"
                )
    return findings


def _looks_like_map_state_expression(value: ast.expr) -> bool:
    """判断 AST 表达式是否显式引用 `state` 或 `.map_task_state`。"""
    return (
        isinstance(value, ast.Name)
        and value.id in {"state", "map_task_state"}
    ) or (
        isinstance(value, ast.Attribute)
        and value.attr == "map_task_state"
    )
