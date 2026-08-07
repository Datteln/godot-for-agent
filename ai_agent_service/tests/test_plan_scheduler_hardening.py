from __future__ import annotations

from copy import deepcopy

import pytest

from app.orchestrator.map_progress import MapTaskState
from app.orchestrator.map_recovery import record_plan_attempt
from app.orchestrator.plan_scheduler import PlanGraph, PlanGraphError
from app.orchestrator.runtime_contracts import PlanStepResult
from app.sessions.schema import UnsupportedSessionSchemaError, validate_session_payload


def _two_step_graph(*, source_path: str = "output.batch") -> PlanGraph:
    """构造 reader 输出绑定到 writer 的最小计划 DAG。"""
    return PlanGraph.from_dict(
        {
            "summary": "read then write",
            "steps": [
                {
                    "id": "reader",
                    "title": "Read",
                    "agent": "map-worker",
                    "task": "read facts",
                },
                {
                    "id": "writer",
                    "title": "Write",
                    "agent": "map-worker",
                    "task": "write batch",
                    "depends_on": ["reader"],
                    "input_bindings": [
                        {
                            "name": "approved_batch",
                            "source_step_id": "reader",
                            "source_path": source_path,
                        }
                    ],
                },
            ],
        }
    )


def _succeed_reader(graph: PlanGraph) -> PlanGraph:
    """把最小 DAG 的 reader 推进到成功终态。"""
    running = graph.start("reader", "frame-reader")
    return running.finish(
        "reader",
        PlanStepResult(
            status="succeeded",
            output={"batch": {"operations": [{"action": "fill"}]}},
        ),
    )


def test_dependency_binding_success_creates_exact_writer_payload() -> None:
    """验证成功前驱的显式路径只绑定到 scheduler payload。"""
    graph = _succeed_reader(_two_step_graph())

    assert [step.step_id for step in graph.runnable_steps()] == ["writer"]
    payload = graph.task_payload("writer")
    assert payload["plan_step_id"] == "writer"
    assert payload["scheduler_inputs"] == {"approved_batch": {"operations": [{"action": "fill"}]}}


def test_missing_binding_becomes_typed_terminal_without_child_frame() -> None:
    """验证缺失绑定可归约为 typed failure，且不产生 frame id。"""
    graph = _succeed_reader(_two_step_graph(source_path="output.missing"))

    with pytest.raises(PlanGraphError, match="required input"):
        graph.task_payload("writer")
    failed = graph.fail_unstarted("writer", "dependency_binding_failed")

    step = failed.step("writer")
    assert step.status == "failed"
    assert step.frame_id is None
    assert step.result is not None
    assert step.result.error_code == "dependency_binding_failed"


def test_failed_predecessor_blocks_every_dependent_step() -> None:
    """验证失败前驱通过 graph 状态传播，而不是创建后继 Frame。"""
    graph = _two_step_graph().start("reader", "frame-reader")
    failed = graph.finish(
        "reader",
        PlanStepResult(status="failed", error_code="reader_failed"),
    )

    writer = failed.step("writer")
    assert writer.status == "blocked"
    assert writer.frame_id is None
    assert writer.result is not None
    assert writer.result.error_code == "predecessor_not_succeeded"
    assert writer.result.blocked_by == ("reader",)


def test_recoverable_attempt_keeps_successor_pending_until_true_terminal() -> None:
    """验证 attempt 恢复不会被误当成步骤终态并传播 dependency block。"""
    running = _two_step_graph().start("reader", "frame-reader")
    recovering = running.defer_attempt(
        "reader",
        disposition="refresh_and_replan",
        error_code="revision_conflict",
    )

    reader = recovering.step("reader")
    writer = recovering.step("writer")
    assert reader.status == "pending"
    assert reader.result is None
    assert reader.current_attempt_id is None
    assert reader.attempt_history[-1]["status"] == "recovering"
    assert writer.status == "pending"
    assert writer.result is None


def test_invalid_review_to_write_transition_leaves_state_unchanged() -> None:
    """验证非法 reviewer→writer 转换在 reducer 边界失败且无部分推进。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        stage="review",
    )
    before = state.to_dict()

    with pytest.raises(
        ValueError,
        match="illegal map stage transition: review -> write",
    ):
        state.transition_stage("write")

    assert state.to_dict() == before


@pytest.mark.parametrize("has_scheduler_graph", [False, True])
def test_legacy_remaining_queue_is_rejected_without_migration(
    has_scheduler_graph: bool,
) -> None:
    """旧 remaining 队列不转换为当前 scheduler 数据。"""
    group: dict[str, object] = {
        "remaining": [{"agent": "map-agent", "task": "legacy child"}],
    }
    if has_scheduler_graph:
        group["scheduler_plan"] = _two_step_graph().to_dict()
    legacy = {
        "schema_version": 6,
        "session_id": "session-1",
        "delegate_groups": {"group-1": group},
    }

    with pytest.raises(UnsupportedSessionSchemaError):
        validate_session_payload(legacy)

    assert "remaining" in group


def test_graph_is_the_only_runnable_child_source() -> None:
    """验证后续 child 始终由 DAG 的依赖与原始顺序选出。"""
    graph = PlanGraph.from_dict(
        {
            "summary": "graph only",
            "steps": [
                {
                    "id": "first",
                    "title": "First",
                    "agent": "reader",
                    "task": "read",
                },
                {
                    "id": "second",
                    "title": "Second",
                    "agent": "writer",
                    "task": "write",
                    "depends_on": ["first"],
                },
                {
                    "id": "parallel",
                    "title": "Parallel",
                    "agent": "reviewer",
                    "task": "review",
                },
            ],
        }
    )

    assert [step.step_id for step in graph.runnable_steps()] == [
        "first",
        "parallel",
    ]
    after_first = graph.start("first", "f1").finish(
        "first",
        PlanStepResult(status="succeeded", output={"ok": True}),
    )
    assert [step.step_id for step in after_first.runnable_steps()] == [
        "second",
        "parallel",
    ]


def test_repeated_identical_plan_trips_exact_attempt_breaker() -> None:
    """验证完全相同的 create_plan 在固定 revision 内按 exact key 熔断。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    attempts = [
        record_plan_attempt(
            state,
            stage="plan",
            target="Map/Main",
            revision=10,
            operation={"summary": "same", "steps": [{"id": "one"}]},
            root_error_code="binding_failed",
            threshold=3,
        )
        for _ in range(3)
    ]

    assert [item["exact"]["count"] for item in attempts] == [1, 2, 3]
    assert attempts[-1]["exact"]["exhausted"] is True
    assert attempts[-1]["exhausted"] is True


def test_revision_advance_thrash_uses_one_convergence_budget() -> None:
    """验证 N→N+1→N+2 的部分成功循环不会因 revision 变化重置。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    attempts = [
        record_plan_attempt(
            state,
            stage="plan",
            target="Map/Main",
            revision=revision,
            operation={"summary": "repair route"},
            root_error_code="route_unreachable:gap",
            threshold=3,
        )
        for revision in (10, 11, 12)
    ]

    assert [item["exact"]["count"] for item in attempts] == [1, 1, 1]
    assert [item["convergence"]["count"] for item in attempts] == [1, 2, 3]
    assert attempts[-1]["convergence"]["first_revision"] == 10
    assert attempts[-1]["convergence"]["latest_revision"] == 12
    assert attempts[-1]["exhausted"] is True
    assert len(state.plan_attempt_registry) == 3
    assert len(state.task_convergence_registry) == 1


def test_legitimate_multi_revision_work_can_reach_terminal_before_bound() -> None:
    """验证显式 terminal outcome 可结束合法的多 revision 工作并重置预算。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    for revision in (20, 21, 22):
        attempt = record_plan_attempt(
            state,
            stage="plan",
            target="Map/Main",
            revision=revision,
            operation={"summary": "three planned sections"},
            root_error_code="planning",
            threshold=4,
        )
        assert attempt["exhausted"] is False

    state.complete()

    assert state.status == "completed"
    assert state.plan_attempt_registry == {}
    assert state.task_convergence_registry == {}


def test_same_task_restart_retains_diagnostics_and_distinct_epoch_resets() -> None:
    """验证同 lineage 重启保留诊断，而新 task epoch 获得全新预算。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    record_plan_attempt(
        state,
        stage="plan",
        target="Map/Main",
        revision=30,
        operation={"summary": "blocked"},
        root_error_code="binding_failed",
        threshold=3,
    )
    restored = MapTaskState.from_dict(deepcopy(state.to_dict()))

    assert restored.plan_attempt_registry == state.plan_attempt_registry
    assert restored.task_convergence_registry == state.task_convergence_registry
    restored.start_new_task("task-2", lineage_id="lineage-2")
    assert restored.plan_attempt_registry == {}
    assert restored.task_convergence_registry == {}


def test_changed_root_error_family_gets_independent_convergence_diagnostic() -> None:
    """验证根错误语义改变后允许新收敛路径且保留旧诊断。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    first = record_plan_attempt(
        state,
        stage="plan",
        target="Map/Main",
        revision=1,
        operation={"summary": "repair"},
        root_error_code="binding_failed:path",
        threshold=3,
    )
    second = record_plan_attempt(
        state,
        stage="plan",
        target="Map/Main",
        revision=2,
        operation={"summary": "repair"},
        root_error_code="revision_conflict:stale",
        threshold=3,
    )

    assert first["convergence"]["key"] != second["convergence"]["key"]
    assert len(state.task_convergence_registry) == 2
    assert all(diagnostic["count"] == 1 for diagnostic in state.task_convergence_registry.values())
