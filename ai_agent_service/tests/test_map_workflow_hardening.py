from __future__ import annotations

from dataclasses import fields
from typing import cast

import pytest

from app.agents.types import AgentDefinition, Frame
from app.orchestrator.completion_gate import evaluate_map_completion
from app.orchestrator.map_contracts import MAP_WORKER_RESULT_SCHEMA
from app.orchestrator.map_state import MAP_TASK_FIELD_LIFECYCLE, MapTaskState, MapTaskStatus, resume_map_task

from app.orchestrator.map_request_scope import MapRequestScope
from app.orchestrator.map_workflow import (
    consume_map_resume_authorization,
    replace_map_state_field,
    upsert_completion_blocker,
)
from app.application.request_scope import _map_completion_candidate_is_current
from app.query.helpers import _remember_map_validation
from app.sessions.schema import UnsupportedSessionSchemaError, validate_session_payload
from app.sessions.store import Session


def _completion_ready_state(*, status: str = "running") -> MapTaskState:
    """构造具备同目标、同 revision 验证与截图证据的 Gate 状态。"""
    target = "Map/Main"
    revision = 7
    return MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status=cast(MapTaskStatus, status),
        context_state={"targets": {target: {}}},
        latest_revisions={target: revision},
        latest_validations={
            target: {
                "target": target,
                "map_revision": revision,
                "passed": True,
                "blocking_completion": False,
                "issues": [],
            }
        },
        evidence_registry={
            "shot-1": {
                "evidence_type": "viewport_screenshot",
                "target": target,
                "revision": revision,
                "metadata": {"status": "applied"},
            }
        },
    )


def _candidate_session(status: str) -> Session:
    """构造绑定同一任务 lineage 的完成候选会话。"""
    agent = AgentDefinition(
        name="map-agent",
        source="bundled",
        description="",
        prompt="",
        role="map_orchestrator",
    )
    frame = Frame(
        id="f1",
        agent=agent,
        messages=[],
        map_request_lineage_id="lineage-1",
        map_task_id="task-1",
    )
    return Session(
        session_id="session-1",
        agent_stack=[frame],
        map_task_state=_completion_ready_state(status=status),
        map_request_scope=MapRequestScope(
            request_id="request-1",
            lineage_id="lineage-1",
            intent="map_edit",
            map_task_id="task-1",
            completion_candidate=True,
        ),
    )


def test_lifecycle_metadata_classifies_every_state_field() -> None:
    """保证新增状态字段无法绕过 task epoch 生命周期分类。"""
    assert set(MAP_TASK_FIELD_LIFECYCLE) == {item.name for item in fields(MapTaskState)}
    assert all(
        lifecycle.scope in {"task", "revision", "context", "operation", "session"}
        and lifecycle.reset_policy == "dataclass_default"
        and lifecycle.resume_policy == "preserve"
        for lifecycle in MAP_TASK_FIELD_LIFECYCLE.values()
    )


def test_legacy_schema_is_rejected_without_migration() -> None:
    """旧嵌入状态及旧恢复标记只能得到 unsupported schema。"""
    legacy = {
        "session_id": "session-1",
        "map_task_state": {
            "task_id": "task-1",
            "status": "running",
            "resumed_from_checkpoint": True,
        },
    }

    with pytest.raises(UnsupportedSessionSchemaError):
        validate_session_payload(legacy)

    assert legacy["map_task_state"]["resumed_from_checkpoint"] is True


def test_distinct_task_resets_task_and_revision_fields_from_metadata() -> None:
    """验证新 task epoch 重置所有非 session 字段并保留事件历史。"""
    state = MapTaskState(
        task_id="old-task",
        task_lineage_id="old-lineage",
        status="completed",
        stage="review",
        auto_iterations=3,
        pending_batches=[{"batch": 1}],
        latest_revisions={"Map/Main": 8},
        completion_blockers=[{"reason": "old"}],
        task_convergence_registry={"old": {"count": 2}},
        workflow_high_water_seq=7,
        workflow_schema_version=3,
    )

    state.start_new_task("new-task", lineage_id="new-lineage")

    assert state.task_id == "new-task"
    assert state.task_lineage_id == "new-lineage"
    assert state.status == "running"
    assert state.stage == "read"
    assert state.auto_iterations == 0
    assert state.pending_batches == []
    assert state.latest_revisions == {}
    assert state.completion_blockers == []
    assert state.task_convergence_registry == {}
    assert state.workflow_schema_version == 3
    assert state.workflow_high_water_seq == 8
    assert state.pending_workflow_events[-1]["event_type"] == "task_epoch_started"


def test_same_task_resume_preserves_checkpoint_state_and_consumes_once() -> None:
    """验证同任务恢复保留进度，且授权仅能被一个请求消费。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        latest_revisions={"Map/Main": 9},
        pending_batches=[{"batch": 2}],
        auto_iterations=2,
    )
    state.make_checkpoint("waiting")
    resume_map_task(state, lineage_id="lineage-1")

    assert state.status == "running"
    assert state.latest_revisions == {"Map/Main": 9}
    assert state.pending_batches == [{"batch": 2}]
    assert state.auto_iterations == 2
    assert consume_map_resume_authorization(
        state,
        task_id="task-1",
        lineage_id="lineage-1",
    )
    assert state.resume_authorization is None
    assert not consume_map_resume_authorization(
        state,
        task_id="task-1",
        lineage_id="lineage-1",
    )


def test_resume_authorization_survives_restart_until_next_request() -> None:
    """验证授权可持久化重启，但下一请求捕获后立即清除。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        resume_authorization={
            "task_id": "task-1",
            "lineage_id": "lineage-1",
        },
    )
    restored = MapTaskState.from_dict(state.to_dict())

    assert consume_map_resume_authorization(
        restored,
        task_id="task-1",
        lineage_id="lineage-1",
    )
    assert restored.resume_authorization is None


@pytest.mark.parametrize("window", ["early_return", "missing_frame", "exception"])
def test_consumed_resume_authorization_cannot_leak_from_failure_window(
    window: str,
) -> None:
    """模拟分类早退、缺 Frame 与异常窗口，证明后续普通消息不获授权。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        resume_authorization={
            "task_id": "task-1",
            "lineage_id": "lineage-1",
        },
    )

    captured = consume_map_resume_authorization(
        state,
        task_id="task-1",
        lineage_id="lineage-1",
    )
    assert captured is True, window
    assert state.resume_authorization is None
    assert not consume_map_resume_authorization(
        state,
        task_id="task-1",
        lineage_id="lineage-1",
    )


def test_mismatched_resume_request_consumes_authorization_fail_closed() -> None:
    """验证 lineage 不匹配的请求同样耗尽授权而不会留给后续消息。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        resume_authorization={
            "task_id": "task-1",
            "lineage_id": "lineage-1",
        },
    )

    assert not consume_map_resume_authorization(
        state,
        task_id="task-1",
        lineage_id="other-lineage",
    )
    assert state.resume_authorization is None


@pytest.mark.parametrize(
    ("issues", "structured_issues"),
    [(None, None), ([], [])],
)
def test_null_validation_issue_collections_normalize_to_empty(
    issues: object,
    structured_issues: object,
) -> None:
    """验证 nullable issue 集合进入 reducer 前统一为空数组。"""
    session = Session(session_id="session-1")
    normalized = _remember_map_validation(
        session,
        "validate_map_region",
        {
            "target": "Map/Main",
            "map_revision": 7,
            "passed": True,
            "issues": issues,
            "structured_issues": structured_issues,
        },
        {"target_path": "Map/Main"},
    )

    assert normalized["passed"] is True
    assert normalized["issues"] == []
    assert normalized["structured_issues"] == []


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_code"),
    [
        ("issues", "not-a-list", "validation_issues_malformed"),
        (
            "structured_issues",
            {"code": "bad"},
            "validation_structured_issues_malformed",
        ),
    ],
)
def test_malformed_validation_issue_collections_fail_closed(
    field_name: str,
    field_value: object,
    error_code: str,
) -> None:
    """验证错误 issue 类型产生 typed contract blocker 而非通过 Gate。"""
    session = Session(session_id="session-1")
    payload: dict[str, object] = {
        "target": "Map/Main",
        "map_revision": 7,
        "passed": True,
        "issues": [],
        "structured_issues": [],
    }
    payload[field_name] = field_value

    normalized = _remember_map_validation(
        session,
        "validate_map_region",
        payload,
        {"target_path": "Map/Main"},
    )

    assert normalized["passed"] is False
    assert normalized["blocking_completion"] is True
    assert normalized["contract_error"] == error_code
    assert error_code in normalized["issues"]


def test_gate_requires_exact_revision_for_each_target() -> None:
    """验证多目标 Gate 不接受缺失或陈旧 revision 的验证结果。"""
    state = _completion_ready_state()
    replace_map_state_field(
        state,
        "context_state",
        {"targets": {"Map/Main": {}, "Map/Other": {}}},
    )
    replace_map_state_field(
        state,
        "latest_revisions",
        {"Map/Main": 7, "Map/Other": 4},
    )
    replace_map_state_field(
        state,
        "latest_validations",
        {
            **state.latest_validations,
            "Map/Other": {
                "target": "Map/Other",
                "map_revision": 3,
                "passed": True,
                "issues": [],
            },
        },
    )

    decision = evaluate_map_completion(state)

    assert decision.allowed is False
    assert any(
        blocker.get("target") == "Map/Other"
        and blocker.get("reason") == "same_revision_validation_missing"
        for blocker in decision.blockers
    )


def test_gate_rejects_missing_revision_and_preserves_unrelated_blockers() -> None:
    """验证缺 revision fail closed，且 scoped upsert 不覆盖其他目标 blocker。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        context_state={"targets": {"Map/Main": {}}},
    )
    first = upsert_completion_blocker(
        state,
        {
            "target": "Map/A",
            "required_revision": 1,
            "source": "validator",
            "issues": ["a"],
        },
    )
    second = upsert_completion_blocker(
        state,
        {
            "target": "Map/B",
            "required_revision": 2,
            "source": "reviewer",
            "issues": ["b"],
        },
    )

    assert first != second
    assert {item["blocker_key"] for item in state.completion_blockers} == {
        first,
        second,
    }
    decision = evaluate_map_completion(state)
    assert decision.allowed is False
    assert any(
        blocker.get("reason") == "completion_revision_missing" for blocker in decision.blockers
    )


@pytest.mark.parametrize(
    ("status", "gate_allowed", "candidate_current"),
    [
        ("running", True, True),
        ("completed", True, True),
        ("paused", False, False),
        ("idle", False, False),
        ("cancelled", False, False),
    ],
)
def test_completion_gate_status_matrix_and_candidate_validity(
    status: str,
    gate_allowed: bool,
    candidate_current: bool,
) -> None:
    """验证每个 workflow status 都有明确 Gate 与 candidate 语义。"""
    session = _candidate_session(status)

    assert evaluate_map_completion(session.map_task_state).allowed is gate_allowed
    assert _map_completion_candidate_is_current(session) is candidate_current


def test_completed_gate_replay_has_no_duplicate_transition_effect() -> None:
    """验证已完成任务重放相同 Gate 结论时不会再次转换状态。"""
    state = _completion_ready_state(status="running")
    assert evaluate_map_completion(state).allowed is True
    state.complete()
    event_count = len(state.pending_workflow_events)

    assert evaluate_map_completion(state).allowed is True
    assert state.status == "completed"
    assert len(state.pending_workflow_events) == event_count
