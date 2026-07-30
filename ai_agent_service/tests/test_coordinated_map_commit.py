from __future__ import annotations

import copy
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.api.schemas import ChatRequest
from app.orchestrator.map_artifacts import (
    MAP_COORDINATED_COMMIT_FAILPOINTS,
    MapArtifactStore,
    MapArtifactTurnConflictError,
    StagedMapArtifactTurn,
)
from app.orchestrator.map_progress import (
    MapTaskState,
    consume_committed_platform_approvals,
)
from app.query.engine import QueryEngine
from app.sessions.store import Session, SessionStore, session_to_dict
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


class _InjectedProcessExit(BaseException):
    """表示测试进程在一个已命名的 durable boundary 退出。"""


@dataclass
class _NamedExitInjector:
    """在指定边界恰好触发一次模拟进程退出。"""

    failpoint: str
    triggered: bool = False

    def hit(self, name: str) -> None:
        """命中目标边界时终止当前测试提交序列。"""
        if name == self.failpoint and not self.triggered:
            self.triggered = True
            raise _InjectedProcessExit(name)


def _staged_turn(
    *,
    session_id: str = "session-1",
    turn_id: str = "t1",
    value: int = 1,
) -> StagedMapArtifactTurn:
    """构造含一个稳定地图结果的 staged artifact turn。"""
    staged = StagedMapArtifactTurn(
        session_id=session_id,
        turn_id=turn_id,
        request_id="request-1",
    )
    staged.add_entry(
        tool_use_id="tool-1",
        tool_name="describe_map_region",
        tool_args={"target_path": "Map/Main"},
        result={"map_revision": value, "cells": [{"x": value}]},
    )
    return staged


def _session_with_locator(
    artifact_store: MapArtifactStore,
    staged: StagedMapArtifactTurn,
) -> Session:
    """构造只引用 staged turn 精确指纹的待发布 Session。"""
    entry = staged.entries["tool-1"]
    locator = artifact_store.locator(
        staged.turn_id,
        "tool-1",
        str(entry["fingerprint"]),
    ).as_dict()
    return Session(
        session_id=staged.session_id,
        turn_counter=int(staged.turn_id.removeprefix("t")),
        pending_tool_calls={"tool-1": {"map_artifact": locator}},
        history_events=[
            {
                "seq": 1,
                "event_type": "tool_result",
                "payload": {"tool_use_id": "tool-1"},
            }
        ],
        history_event_counter=1,
        session_allow={("describe_map_region", "map", "fingerprint", "")},
    )


def _execute_until_exit(
    project_root: Path,
    sessions_root: Path,
    failpoint: str,
) -> tuple[StagedMapArtifactTurn, bool]:
    """按 artifact-first 顺序执行，直到命名边界模拟进程退出。"""
    injector = _NamedExitInjector(failpoint)
    staged = _staged_turn()
    artifact_store = MapArtifactStore(
        project_root,
        staged.session_id,
        injector,
    )
    session = _session_with_locator(artifact_store, staged)
    session_store = SessionStore(sessions_root, project_root=project_root)
    try:
        artifact_store.prepare_turn(staged)
        injector.hit("session_publish_before_write")
        session_store.save(session)
        injector.hit("session_publish_after_write")
        artifact_store.commit_prepared_turn(staged)
    except _InjectedProcessExit:
        pass
    return staged, injector.triggered


@pytest.mark.parametrize("failpoint", sorted(MAP_COORDINATED_COMMIT_FAILPOINTS))
def test_every_coordinated_commit_boundary_reconciles_without_dangling_locator(
    tmp_path: Path,
    failpoint: str,
) -> None:
    """验证每个提交边界退出后都恢复为全不可见或联合可见。"""
    sessions_root = tmp_path / ".ai_agent_service" / "sessions"
    staged, triggered = _execute_until_exit(tmp_path, sessions_root, failpoint)
    assert triggered is True

    restarted_store = SessionStore(sessions_root, project_root=tmp_path)
    restored = restarted_store.get_or_create(staged.session_id, set())
    artifact_store = MapArtifactStore(tmp_path, staged.session_id)
    artifact_store.reconcile_with_session(session_to_dict(restored))

    locator_value = restored.pending_tool_calls.get("tool-1", {}).get(
        "map_artifact"
    )
    if isinstance(locator_value, dict):
        page = artifact_store.read_page(
            str(locator_value["artifact_ref"]),
            turn_id=str(locator_value["artifact_turn_id"]),
            entry_id=str(locator_value["artifact_entry_id"]),
            fingerprint=str(locator_value["artifact_fingerprint"]),
            field="cells",
        )
        assert page["total"] == 1
        assert len(restored.history_events) == 1
        assert len(restored.session_allow) == 1
    elif artifact_store.path.exists():
        document = json.loads(artifact_store.path.read_text(encoding="utf-8"))
        assert staged.turn_id not in document["turns"]

    if artifact_store.path.exists():
        document = json.loads(artifact_store.path.read_text(encoding="utf-8"))
        assert document["coordinated_commits"] == {}
        assert all(
            turn.get("publication_state") == "committed"
            for turn in document["turns"].values()
        )


def test_production_composition_has_no_enabled_failpoint() -> None:
    """验证 production 默认构造不携带任何 failpoint 实现。"""
    store = MapArtifactStore(Path.cwd(), "session-1")
    parameter = inspect.signature(QueryEngine.__init__).parameters[
        "coordinated_commit_failure_injector"
    ]

    assert store.failure_injector is None
    assert parameter.default is None


def test_requests_and_map_tool_schemas_cannot_configure_failpoints() -> None:
    """验证 API 与地图工具载荷均没有故障注入控制字段。"""
    assert not any(
        "failpoint" in field_name.lower()
        for field_name in ChatRequest.model_fields
    )
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        serialized = json.dumps(
            {
                name: tool.schema
                for name, tool in REGISTRY.items()
                if tool.domain == "map"
            },
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        assert "failpoint" not in serialized
        assert "failure_injector" not in serialized
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_turn_counter_stays_monotonic_after_snapshot_rollback_and_restart(
    tmp_path: Path,
) -> None:
    """验证失败请求的 snapshot rollback 永不重新分配已占用 turn。"""
    sessions_root = tmp_path / ".ai_agent_service" / "sessions"
    store = SessionStore(sessions_root, project_root=tmp_path)
    session = store.get_or_create("session-1", set())
    snapshot = copy.deepcopy(session)

    assert session.new_turn_id() == "t1"
    store.replace_in_memory("session-1", snapshot)
    rolled_back = store.get_or_create("session-1", set())
    store.save(rolled_back)

    restarted = SessionStore(
        sessions_root,
        project_root=tmp_path,
    ).get_or_create("session-1", set())
    assert restarted.turn_counter == 1
    assert restarted.new_turn_id() == "t2"


def test_restart_advances_counter_past_every_reserved_artifact_turn(
    tmp_path: Path,
) -> None:
    """验证重启会把 Session 计数器提升到 artifact 已占用 turn 之后。"""
    sessions_root = tmp_path / ".ai_agent_service" / "sessions"
    artifact_store = MapArtifactStore(tmp_path, "session-1")
    staged = _staged_turn(turn_id="t5")
    artifact_store.prepare_turn(staged)
    low_session = _session_with_locator(artifact_store, staged)
    low_session.turn_counter = 2
    SessionStore(sessions_root, project_root=tmp_path).save(low_session)
    artifact_store.commit_prepared_turn(staged)

    restarted = SessionStore(
        sessions_root,
        project_root=tmp_path,
    ).get_or_create("session-1", set())

    assert restarted.turn_counter == 5
    assert restarted.new_turn_id() == "t6"


def test_conflicting_turn_returns_typed_failure_and_retry_uses_fresh_turn(
    tmp_path: Path,
) -> None:
    """验证异指纹冲突保留原提交，并允许更大 turn id 的干净重试。"""
    sessions_root = tmp_path / ".ai_agent_service" / "sessions"
    artifact_store = MapArtifactStore(tmp_path, "session-1")
    committed = _staged_turn(turn_id="t3", value=3)
    artifact_store.prepare_turn(committed)
    session = _session_with_locator(artifact_store, committed)
    SessionStore(sessions_root, project_root=tmp_path).save(session)
    artifact_store.commit_prepared_turn(committed)
    conflicting = _staged_turn(turn_id="t3", value=99)

    with pytest.raises(MapArtifactTurnConflictError) as captured:
        artifact_store.prepare_turn(conflicting)

    assert captured.value.error_code == "map_artifact_turn_identity_conflict"
    original_locator = session.pending_tool_calls["tool-1"]["map_artifact"]
    original = artifact_store.read_page(
        str(original_locator["artifact_ref"]),
        turn_id="t3",
        entry_id="tool-1",
        fingerprint=str(original_locator["artifact_fingerprint"]),
        field="map_revision",
    )
    assert original["value"] == 3

    restarted = SessionStore(
        sessions_root,
        project_root=tmp_path,
    ).get_or_create("session-1", set())
    assert restarted.new_turn_id() == "t4"
    assert len(restarted.history_events) == 1


def test_committed_approval_consumption_is_idempotent() -> None:
    """验证 committed-result 重放不会二次消费批准或推进工作流。"""
    approval_record = {
        "approval_id": "approval-1",
        "target": "Map/Main",
        "expected_revision": 5,
        "batch_fingerprint": "fingerprint-1",
        "batch": {"tool": "edit_map", "operations": [{"action": "fill"}]},
    }
    session = Session(
        session_id="session-1",
        map_task_state=MapTaskState(
            approved_platform_plans={
                "Map/Main": {
                    "target": "Map/Main",
                    "records": [approval_record],
                }
            }
        ),
    )
    transaction = {"approval_records": [approval_record]}
    result = {
        "map_transaction_status": "committed",
        "committed_revision": 6,
        "approval_records": [approval_record],
    }

    assert consume_committed_platform_approvals(session, result, transaction)
    after_first = copy.deepcopy(session.map_task_state.to_dict())
    assert not consume_committed_platform_approvals(session, result, transaction)
    assert session.map_task_state.to_dict() == after_first
