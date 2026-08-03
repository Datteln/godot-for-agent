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
        "counters",
        "structure_revision",
        "plan_version",
        "auto_iterations",
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
        "authoritative_snapshots",
        "planning_attempt_history",
        "planning_publications",
        "tool_failure_fingerprints",
        "approved_platform_plans",
        "latest_revisions",
        "latest_layers",
        "region_reads",
        "region_summaries",
        "context_state",
        "completion_blockers",
        "checkpoint",
        "resume_authorization",
        "pause_report",
        "evidence_registry",
        "retry_registry",
        "plan_attempt_registry",
        "task_convergence_registry",
        "transaction_journals",
        "workflow_scopes",
        "workflow_events",
    }
)
DIRECT_WRITE_HYDRATION_ALLOWLIST = frozenset(
    {
        ("map_progress.py", "MapTaskState.from_dict"),
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
    if field_name in REDUCER_OWNED_FIELDS and _REDUCER_WRITE_DEPTH.get() <= 0:
        raise RuntimeError(f"direct write to reducer-owned MapTaskState field: {field_name}")


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

    if event.event_type == "task_epoch_started":
        task_id = str(payload.get("task_id", "")).strip()
        lineage_id = str(payload.get("lineage_id", "")).strip()
        if not task_id:
            raise ValueError("task_epoch_started requires task_id")
        for field_name, default_value in reduced.task_epoch_reset_values().items():
            setattr(reduced, field_name, deepcopy(default_value))
        reduced.task_id = task_id
        reduced.task_lineage_id = lineage_id
        reduced.status = "running"
        reduced.stage = "read"
        scopes = reduced.workflow_scopes
        scope = {
            "target": event.target,
            "revision": event.revision,
            "stage": "read",
            "task_id": task_id,
            "lineage_id": lineage_id,
        }
    elif event.event_type == "stage_transition":
        next_stage = str(payload.get("stage", ""))
        allowed = MAP_RUNTIME_STAGE_TRANSITIONS.get(reduced.stage, frozenset())
        if next_stage not in allowed:
            raise ValueError(f"illegal map stage transition: {reduced.stage} -> {next_stage}")
        reduced.stage = next_stage
        scope["stage"] = next_stage
    elif event.event_type == "blockers_replaced":
        blockers = payload.get("blockers", [])
        if not isinstance(blockers, list):
            raise ValueError("blockers_replaced requires blockers array")
        reduced.completion_blockers = deepcopy(blockers)
        scope["blockers"] = deepcopy(blockers)
    elif event.event_type == "blocker_upserted":
        blocker_key = str(payload.get("blocker_key", "")).strip()
        blocker = payload.get("blocker")
        if not blocker_key or not isinstance(blocker, dict):
            raise ValueError("blocker_upserted requires blocker_key and blocker")
        reduced.completion_blockers = [
            item for item in reduced.completion_blockers if item.get("blocker_key") != blocker_key
        ]
        reduced.completion_blockers.append(deepcopy(blocker))
        scope["blockers"] = deepcopy(reduced.completion_blockers)
    elif event.event_type == "blockers_removed":
        blocker_keys = payload.get("blocker_keys")
        if not isinstance(blocker_keys, list):
            raise ValueError("blockers_removed requires blocker_keys array")
        key_set = {str(item) for item in blocker_keys}
        reduced.completion_blockers = [
            item
            for item in reduced.completion_blockers
            if str(item.get("blocker_key", "")) not in key_set
        ]
        scope["blockers"] = deepcopy(reduced.completion_blockers)
    elif event.event_type == "checkpoint_replaced":
        checkpoint = payload.get("checkpoint")
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("checkpoint_replaced requires object or null")
        reduced.checkpoint = deepcopy(checkpoint)
        scope["checkpoint"] = deepcopy(checkpoint)
    elif event.event_type == "resume_authorization_replaced":
        authorization = payload.get("authorization")
        if authorization is not None and not isinstance(authorization, dict):
            raise ValueError("resume_authorization_replaced requires object or null")
        reduced.resume_authorization = deepcopy(authorization)
        scope["resume_authorization"] = deepcopy(authorization)
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
    elif event.event_type == "plan_attempt_recorded":
        exact_key = str(payload.get("exact_key", "")).strip()
        convergence_key = str(payload.get("convergence_key", "")).strip()
        exact_attempt = payload.get("exact_attempt")
        convergence = payload.get("convergence")
        if (
            not exact_key
            or not convergence_key
            or not isinstance(exact_attempt, dict)
            or not isinstance(convergence, dict)
        ):
            raise ValueError("plan_attempt_recorded requires exact and convergence records")
        reduced.plan_attempt_registry[exact_key] = deepcopy(exact_attempt)
        reduced.task_convergence_registry[convergence_key] = deepcopy(convergence)
        scope.setdefault("plan_attempt_keys", []).append(exact_key)
        scope["task_convergence_key"] = convergence_key
    elif event.event_type == "progress_recorded":
        category = str(payload.get("category", "unknown"))
        count = int(payload.get("count", 0))
        progress = scope.setdefault("progress", {})
        progress[category] = count
        reduced.no_progress_streaks[scope_key] = count
    elif event.event_type == "counter_incremented":
        counter_name = str(payload.get("counter", "")).strip()
        amount = payload.get("amount", 1)
        if (
            counter_name not in vars(reduced.counters)
            or isinstance(amount, bool)
            or not isinstance(amount, int)
        ):
            raise ValueError("counter_incremented requires a known counter and integer amount")
        setattr(
            reduced.counters,
            counter_name,
            int(getattr(reduced.counters, counter_name)) + amount,
        )
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


def consume_map_resume_authorization(
    state: Any,
    *,
    task_id: str,
    lineage_id: str,
) -> bool:
    """原子消费并校验一次性地图恢复授权。"""
    authorization = getattr(state, "resume_authorization", None)
    if not isinstance(authorization, dict):
        return False
    valid = (
        state.status == "running"
        and bool(task_id)
        and bool(lineage_id)
        and str(authorization.get("task_id", "")) == task_id
        and str(authorization.get("lineage_id", "")) == lineage_id
    )
    target, revision = workflow_scope_identity(state)
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "resume_authorization_replaced",
            target,
            revision,
            {"authorization": None},
        ),
    )
    return valid


def increment_map_counter(
    state: Any,
    counter_name: str,
    amount: int = 1,
    *,
    target: str | None = None,
    revision: int | None = None,
) -> None:
    """通过 reducer 事件递增一个已声明的地图任务计数器。"""
    scope_target, scope_revision = workflow_scope_identity(
        state,
        target,
        revision,
    )
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "counter_incremented",
            scope_target,
            scope_revision,
            {"counter": counter_name, "amount": amount},
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


def completion_blocker_key(state: Any, blocker: dict[str, Any]) -> str:
    """Return the stable task/target/revision/source/issue blocker identity."""
    issue_payload = blocker.get(
        "structured_issue",
        blocker.get("structured_issues", blocker.get("issues", [])),
    )
    issue_encoded = json.dumps(
        issue_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    issue_identity = (
        str(blocker.get("issue_id", "")).strip()
        or hashlib.sha256(issue_encoded.encode("utf-8")).hexdigest()[:24]
    )
    parts = {
        "task": str(getattr(state, "task_id", "")),
        "target": str(blocker.get("target", "")),
        "revision": blocker.get("required_revision"),
        "source": str(blocker.get("source", blocker.get("tool", blocker.get("reason", "")))),
        "issue": issue_identity,
    }
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upsert_completion_blocker(state: Any, blocker: dict[str, Any]) -> str:
    """Upsert one blocker through the reducer and return its stable key."""
    blocker_key = completion_blocker_key(state, blocker)
    normalized = {
        **deepcopy(blocker),
        "task_id": str(getattr(state, "task_id", "")),
        "blocker_key": blocker_key,
    }
    target = str(normalized.get("target", "")).strip() or "__workflow__"
    revision_value = normalized.get("required_revision")
    revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else getattr(state, "structure_revision", 0)
    )
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "blocker_upserted",
            target,
            revision,
            {"blocker_key": blocker_key, "blocker": normalized},
        ),
    )
    return blocker_key


def remove_completion_blockers(state: Any, blocker_keys: list[str]) -> None:
    """Remove exact blocker identities through one reducer event."""
    if not blocker_keys:
        return
    target, revision = workflow_scope_identity(state)
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "blockers_removed",
            target,
            revision,
            {"blocker_keys": list(dict.fromkeys(blocker_keys))},
        ),
    )


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
                    if _direct_write_is_allowlisted(path, node, tree):
                        continue
                    findings.append(f"{path}:{getattr(node, 'lineno', 0)}:{target.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"append", "clear", "extend", "insert", "pop", "remove", "setdefault", "update"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in REDUCER_OWNED_FIELDS
                and _looks_like_map_state_expression(node.func.value.value)
            ):
                if _direct_write_is_allowlisted(path, node, tree):
                    continue
                findings.append(
                    f"{path}:{getattr(node, 'lineno', 0)}:"
                    f"{node.func.value.attr}.{node.func.attr}"
                )
    return findings


def _direct_write_is_allowlisted(
    path: Path,
    node: ast.AST,
    tree: ast.Module,
) -> bool:
    """Allow only the exact pre-construction hydration method."""
    line = int(getattr(node, "lineno", 0))
    for class_node in (item for item in tree.body if isinstance(item, ast.ClassDef)):
        for function_node in (
            item
            for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            identity = (
                path.name,
                f"{class_node.name}.{function_node.name}",
            )
            end_line = int(getattr(function_node, "end_lineno", function_node.lineno))
            if (
                identity in DIRECT_WRITE_HYDRATION_ALLOWLIST
                and function_node.lineno <= line <= end_line
            ):
                return True
    return False


def check_repository_map_state_writes() -> int:
    """Run the repository direct-state-write guard as a console check."""
    app_root = Path(__file__).resolve().parents[1]
    paths = [
        *app_root.joinpath("orchestrator").rglob("*.py"),
        *app_root.joinpath("query").rglob("*.py"),
    ]
    findings = find_direct_map_state_writes(paths)
    if findings:
        raise RuntimeError(
            "direct reducer-owned MapTaskState writes found:\n" + "\n".join(findings)
        )
    return 0


def _looks_like_map_state_expression(value: ast.expr) -> bool:
    """判断 AST 表达式是否显式引用 `state` 或 `.map_task_state`。"""
    return (isinstance(value, ast.Name) and value.id in {"state", "map_task_state"}) or (
        isinstance(value, ast.Attribute) and value.attr == "map_task_state"
    )
