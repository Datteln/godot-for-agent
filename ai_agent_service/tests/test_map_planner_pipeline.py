"""权威地图规划快照、三次校验与最终发布回归测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.agents.types import AgentDefinition
from app.orchestrator.map_contracts import MAP_WORKER_RESULT_SCHEMA
from app.orchestrator.map_planning_snapshots import (
    AuthoritativeMapSnapshot,
    PlanningSnapshotStore,
    build_region_snapshot,
    merge_frontier_snapshot,
    planning_snapshot_scope,
)
from app.orchestrator.map_context import build_map_progress_digest
from app.orchestrator.map_plan_progress import remember_map_plan_progress
from app.orchestrator.map_platform_planning import map_platform_plan_attempt_count, map_platform_plan_call_error
from app.orchestrator.map_state import MapTaskState

from app.orchestrator.map_resources import normalize_edit_map_resources
from app.orchestrator.map_workers import build_dynamic_map_worker
from app.orchestrator.map_workflow import replace_map_state_field
from app.orchestrator.plan_scheduler import PlanGraph
from app.orchestrator.runtime_contracts import PlanStepResult
from app.sessions.store import Session, session_from_dict, session_to_dict
from app.tools.front_tools import register_front_tools
from app.tools.server_tools import register_server_tools


def _region_result(*, dimension: int = 2) -> dict[str, Any]:
    """构造覆盖完整且包含精确资源身份的 canonical region 结果。"""
    cells: list[dict[str, Any]]
    if dimension == 3:
        cells = [{"coords": {"x": 1, "y": 0, "z": 2}, "item": 7, "orientation": 3}]
    else:
        cells = [
            {
                "coords": {"x": 1, "y": 2},
                "source_id": 4,
                "atlas_coords": {"x": 6, "y": 8},
                "alternative_tile": 2,
                "semantic_layer": "ground",
                "tags": ["ground"],
            }
        ]
    return {
        "ok": True,
        "target": "Map/Main",
        "map_layer": 2,
        "map_revision": 7,
        "dimension": dimension,
        "cells_format": "non_empty_only",
        "cells_total": 24,
        "cells_returned": 1,
        "cells_omitted": 0,
        "non_empty_count": 1,
        "cells": cells,
        "used_bounds": {"min_x": 1, "max_x": 1, "min_y": 2, "max_y": 2},
        "collision_support": {
            "source": "canonical_editor_cells",
            "complete": True,
            "filled_cells": 1,
        },
        "object_occupancy": {
            "source": "live_scene_query",
            "freshness": "current_revision",
            "complete": True,
            "occupied": [],
        },
        "resource_bindings": {
            "ground": {
                "source_id": 4,
                "atlas_coords": {"x": 6, "y": 8},
                "tags": ["ground"],
            }
        },
    }


def _complete_snapshot(*, dimension: int = 2) -> AuthoritativeMapSnapshot:
    """构造同时满足 coverage、traversal 与 frontier 的完整快照。"""
    tool_args = {
        "target_path": "Map/Main",
        "map_layer": 2,
        "x": 0,
        "y": 0,
        "width": 6,
        "height": 4,
        **({"z": 0, "depth": 1} if dimension == 3 else {}),
    }
    base = build_region_snapshot(tool_args, _region_result(dimension=dimension))
    frontier_result = {
        "ok": True,
        "target": "Map/Main",
        "map_layer": 2,
        "map_revision": 7,
        "start_anchor": {"x": 1, "y": 1},
        "reachable_frontier": {"x": 5, "y": 1},
        "frontier_candidates": [{"x": 5, "y": 1}],
        "reachable_count": 8,
        "planning_contract": {
            "region": deepcopy(tool_args),
            "traversal": {
                "movement_model": "leap",
                "cell_occupancy": "empty",
                "requires_support": True,
                "support_occupancy": "filled",
                "max_horizontal_gap": 4,
                "max_rise": 2,
                "max_fall": 6,
            },
        },
    }
    return merge_frontier_snapshot(base, tool_args, frontier_result)


def _install_snapshot(session: Session, snapshot: AuthoritativeMapSnapshot) -> None:
    """把测试快照按 reducer 合同安装到活动 session。"""
    state = session.map_task_state
    state.start_new_task("task-1", lineage_id="lineage-1")
    session.map_task_lineage = {"lineage_id": "lineage-1"}
    replace_map_state_field(
        state,
        "latest_revisions",
        {"Map/Main::map_layer=2": snapshot.map_revision},
        target=snapshot.target_path,
        revision=snapshot.map_revision,
    )
    locator = {
        "artifact_kind": snapshot.schema,
        "artifact_ref": "fixture/planning-snapshot.json",
        "snapshot_id": snapshot.snapshot_id,
        "digest": snapshot.digest,
        "target_path": snapshot.target_path,
        "map_layer": snapshot.map_layer,
        "map_revision": snapshot.map_revision,
        "completeness": deepcopy(snapshot.completeness),
        "execution_eligible": snapshot.execution_eligible,
        "planner_projection": snapshot.planner_projection(),
    }
    replace_map_state_field(
        state,
        "authoritative_snapshots",
        {planning_snapshot_scope(snapshot.target_path, snapshot.map_layer): locator},
        target=snapshot.target_path,
        revision=snapshot.map_revision,
    )


def _candidate(ordinal: int) -> dict[str, Any]:
    """生成语义不同的平台候选，确保每次修复都有稳定新指纹。"""
    end_x = 2 + ordinal
    return {
        "target_path": "Map/Main",
        "map_layer": 2,
        "platforms": [
            {
                "id": "p0",
                "x": 1,
                "y": 2,
                "width": 2 + ordinal,
                "role": "finish",
                "resource": "ground",
            }
        ],
        "segments": [
            {
                "index": 0,
                "type": "walk",
                "start": {"x": 1, "y": 1},
                "end": {"x": end_x, "y": 1},
            }
        ],
        "semantic_resources": ["ground"],
        "rationale": f"repair attempt {ordinal}",
    }


def _failed_result(ordinal: int) -> dict[str, Any]:
    """生成带结构化问题与 repair plan 的确定性失败结果。"""
    return {
        "ok": False,
        "target": "Map/Main",
        "map_layer": 2,
        "error_code": f"route_issue_{ordinal}",
        "blocked_reason": "platform_plan_failed",
        "issues": [{"path": "platforms[0].width", "attempt": ordinal}],
        "repair_plan": [
            {
                "path": "platforms[0].width",
                "action": "increase_verified_landing_width",
                "attempt": ordinal,
            }
        ],
        "edit_map_batches": [],
    }


def _passed_result() -> dict[str, Any]:
    """生成 validator/compiler 已解析语义资源的成功结果。"""
    return {
        "ok": True,
        "target": "Map/Main",
        "map_layer": 2,
        "edit_map_batches": [
            {
                "tool": "edit_map",
                "operations": [
                    {
                        "action": "fill",
                        "x": 2,
                        "y": 2,
                        "width": 2,
                        "height": 1,
                        "resource": "ground",
                        "source_id": 4,
                        "atlas_x": 6,
                        "atlas_y": 8,
                    }
                ],
            }
        ],
    }


def _planner_writer_graph() -> PlanGraph:
    """构造最终规划发布与 writer 执行分离的最小 DAG。"""
    return PlanGraph.from_dict(
        {
            "summary": "plan then conditionally write",
            "steps": [
                {
                    "id": "planner",
                    "title": "Plan route",
                    "agent": "map-planner-agent",
                    "task": "plan",
                },
                {
                    "id": "writer",
                    "title": "Write approved route",
                    "agent": "map-worker",
                    "task": "write",
                    "depends_on": ["planner"],
                    "worker_spec": {"mode": "write_one_batch"},
                },
            ],
        }
    )


def test_snapshot_completeness_occupancy_sources_and_projection_redaction() -> None:
    """验证完整快照保留 occupancy 来源且 planner 投影剔除写入身份。"""
    snapshot = _complete_snapshot()

    assert snapshot.execution_eligible is True
    assert all(snapshot.completeness.values())
    assert snapshot.object_occupancy["source"] == "live_scene_query"
    assert snapshot.traversal_profile["source"] == "canonical_frontier_contract"
    assert set(snapshot.traversal_profile["source_fields"]) >= {
        "cell_occupancy",
        "support_occupancy",
        "requires_support",
    }
    projection = snapshot.planner_projection()
    assert projection["semantic_resources"] == ["ground"]
    assert projection["occupied_cells"][0]["occupied"] is True
    assert not (
        {"source_id", "atlas_coords", "alternative_tile"} & projection["occupied_cells"][0].keys()
    )

    projection_3d = _complete_snapshot(dimension=3).planner_projection()
    assert not ({"item", "orientation"} & projection_3d["occupied_cells"][0].keys())


def test_incomplete_snapshot_never_authorizes_execution() -> None:
    """验证截断覆盖和未知对象 freshness 会明确阻止 execution eligibility。"""
    result = _region_result()
    result.update(
        {
            "cells_format": "full",
            "cells_returned": 1,
            "cells_omitted": 23,
            "object_occupancy": {
                "source": "unavailable",
                "freshness": "unknown",
                "complete": False,
            },
        }
    )
    snapshot = build_region_snapshot(
        {"target_path": "Map/Main", "map_layer": 2, "x": 0, "y": 0, "width": 6, "height": 4},
        result,
    )

    assert snapshot.execution_eligible is False
    assert snapshot.completeness["coverage"] is False
    assert snapshot.completeness["object_occupancy"] is False


def test_snapshot_store_detects_digest_mismatch(tmp_path: Path) -> None:
    """验证 artifact 内容遭修改后不会被当成同一权威快照读取。"""
    snapshot = _complete_snapshot()
    store = PlanningSnapshotStore(tmp_path, "session-1", "epoch-1")
    locator = store.store(snapshot)
    path = tmp_path / str(locator["artifact_ref"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["snapshot"]["canonical_cells"][0]["source_id"] = 999
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        store.read(str(locator["artifact_ref"]))


def test_skill_and_runtime_worker_snapshot_contract_remain_aligned() -> None:
    """验证 skill 文档要求、worker 输入与运行时工具权限保持一致。"""
    register_front_tools()
    register_server_tools()
    skill_path = (
        Path(__file__).parents[1] / "app" / "skills" / "bundled" / "map-area-expansion" / "SKILL.md"
    )
    skill_text = skill_path.read_text(encoding="utf-8")
    for required in (
        "authoritative_map_snapshot_v1",
        "traversal profile",
        "entry",
        "frontier",
        "semantic resource",
        "source_id",
        "atlas_coords",
    ):
        assert required in skill_text

    parent = AgentDefinition(
        name="map-agent",
        source="bundled",
        description="map parent",
        prompt="coordinate map work",
    )
    snapshot = _complete_snapshot()
    locator = {
        "artifact_ref": "snapshots/map.json",
        "snapshot_id": snapshot.snapshot_id,
        "digest": snapshot.digest,
        "target_path": snapshot.target_path,
        "map_layer": snapshot.map_layer,
        "map_revision": snapshot.map_revision,
        "execution_eligible": True,
    }
    worker = build_dynamic_map_worker(
        parent,
        {
            "name": "planner-fixture",
            "objective": "extend platform route",
            "mode": "propose_only",
            "skills": ["map-area-expansion"],
            "operations": ["validate_platform_level_plan"],
            "constraints": [],
            "output_schema": MAP_WORKER_RESULT_SCHEMA,
            "authoritative_snapshot": locator,
        },
    )

    assert isinstance(worker, AgentDefinition)
    assert "read_planning_snapshot" in worker.tools
    assert "edit_map" not in worker.tools
    assert snapshot.snapshot_id in worker.prompt
    assert "不要索取或输出逐格 atlas" in worker.prompt
    # change 9.4：单权威快照被迁移为 one-entry planning-context bundle，
    # planner 的事实源是运行时冻结的 bundle，而非裸快照。
    assert "map-context-bundle:" in worker.prompt

    missing = build_dynamic_map_worker(
        parent,
        {
            "name": "planner-without-snapshot",
            "objective": "extend platform route",
            "mode": "propose_only",
            "skills": ["map-area-expansion"],
            "operations": ["validate_platform_level_plan"],
            "constraints": [],
            "output_schema": MAP_WORKER_RESULT_SCHEMA,
        },
    )
    assert isinstance(missing, str)
    # change 9.2：planner 缺失权威事实源时 fail closed，错误指明新的
    # planning_context_bundle 合同（单快照兼容路径见上方 worker 构建）。
    assert "planning_context_bundle" in missing


def test_raw_atlas_and_gridmap_items_are_rejected_instead_of_rewritten(tmp_path: Path) -> None:
    """验证删除 legacy raw-batch 改写后，裸 2D/3D 身份都 fail closed。"""
    raw_2d = normalize_edit_map_resources(
        tmp_path,
        {"operations": [{"action": "fill", "source_id": 4, "atlas_x": 6, "atlas_y": 8}]},
    )
    raw_3d = normalize_edit_map_resources(
        tmp_path,
        {"operations": [{"action": "fill", "item": 7, "orientation": 3}]},
    )
    semantic = normalize_edit_map_resources(
        tmp_path,
        {"operations": [{"action": "fill", "resource": "ground"}]},
    )

    assert raw_2d.error_code == "planner_raw_map_resource_rejected"
    assert raw_3d.error_code == "planner_raw_map_resource_rejected"
    assert semantic.error_code is None
    assert semantic.args["operations"][0] == {"action": "fill", "resource": "ground"}
    assert semantic.rewritten_operations == 0


@pytest.mark.parametrize("passing_attempt", [1, 2, 3])
def test_candidate_can_pass_on_any_of_three_attempts(passing_attempt: int) -> None:
    """验证第 1、2 或 3 次通过都会发布批准结果并停止继续规划。"""
    session = Session(session_id=f"pass-{passing_attempt}")
    _install_snapshot(session, _complete_snapshot())

    for ordinal in range(1, passing_attempt + 1):
        args = _candidate(ordinal)
        assert map_platform_plan_call_error(session, "validate_platform_level_plan", args) is None
        if ordinal == passing_attempt:
            remember_map_plan_progress(
                session,
                "validate_platform_level_plan",
                args,
                _passed_result(),
            )
        else:
            remember_map_plan_progress(
                session,
                "validate_platform_level_plan",
                args,
                _failed_result(ordinal),
            )

    assert map_platform_plan_attempt_count(session, args) == passing_attempt
    publication = next(iter(session.map_task_state.planning_publications.values()))
    assert publication["planning_status"] == "delivered"
    assert publication["execution_status"] == "approved"
    assert publication["approved_batches"]
    history = next(iter(session.map_task_state.planning_attempt_history.values()))
    assert len(history) == passing_attempt
    assert history[-1]["passed"] is True


def test_three_failures_publish_final_plan_and_refuse_fourth_attempt() -> None:
    """验证三次失败仍交付最终方案、保持 writer 阻断并拒绝第四次。"""
    session = Session(session_id="three-failures")
    _install_snapshot(session, _complete_snapshot())

    last_result: dict[str, Any] = {}
    for ordinal in range(1, 4):
        args = _candidate(ordinal)
        assert map_platform_plan_call_error(session, "validate_platform_level_plan", args) is None
        last_result = _failed_result(ordinal)
        retry = remember_map_plan_progress(
            session,
            "validate_platform_level_plan",
            args,
            last_result,
        )
        assert retry is not None
        assert retry["attempt_count"] == ordinal

    publication = next(iter(session.map_task_state.planning_publications.values()))
    assert publication["planning_status"] == "delivered"
    assert publication["execution_status"] == "blocked_by_validation"
    assert publication["semantic_plan"] == {
        "platforms": args["platforms"],
        "segments": args["segments"],
        "semantic_resources": ["ground"],
        "reference_cells": [],
        "rationale": "repair attempt 3",
    }
    assert publication["approved_batches"] == []
    assert len(publication["validation_history"]) == 3
    assert last_result["edit_map_batches"] == []
    assert last_result["planning_status"] == "delivered"
    assert last_result["execution_status"] == "blocked_by_validation"
    fourth_error = map_platform_plan_call_error(
        session,
        "validate_platform_level_plan",
        _candidate(4),
    )
    assert fourth_error is not None
    assert "planning_attempts_exhausted" in fourth_error


def test_unchanged_candidate_is_rejected_without_consuming_another_attempt() -> None:
    """验证同一语义指纹不会静默消耗第二次确定性校验。"""
    session = Session(session_id="unchanged")
    _install_snapshot(session, _complete_snapshot())
    args = _candidate(1)
    assert map_platform_plan_call_error(session, "validate_platform_level_plan", args) is None
    remember_map_plan_progress(
        session,
        "validate_platform_level_plan",
        args,
        _failed_result(1),
    )

    repeated_args = _candidate(1)
    error = map_platform_plan_call_error(
        session,
        "validate_platform_level_plan",
        repeated_args,
    )
    assert error is not None
    assert "unchanged_plan_attempt" in error
    assert map_platform_plan_attempt_count(session, repeated_args) == 1


def test_blocked_publication_never_schedules_writer() -> None:
    """验证最终规划发布成功也不会把 blocked execution 当成 writer 前驱成功。"""
    graph = _planner_writer_graph().start("planner", "frame-planner")
    finished = graph.finish(
        "planner",
        PlanStepResult(
            status="succeeded",
            output={
                "planning_status": "delivered",
                "execution_status": "blocked_by_validation",
                "semantic_plan": {"platforms": [{"id": "p3"}]},
                "unresolved_issues": [{"error_code": "route_issue_3"}],
                "approved_batches": [],
            },
        ),
    )

    assert finished.step("planner__publication").status == "succeeded"
    writer = finished.step("writer")
    assert writer.status == "blocked"
    assert writer.result is not None
    assert writer.result.error_code == "execution_not_approved"
    assert "writer" not in {step.step_id for step in finished.runnable_steps()}


def test_approved_publication_keeps_writer_runnable() -> None:
    """验证 approved execution 独立发布后仍可正常解锁 writer。"""
    graph = _planner_writer_graph().start("planner", "frame-planner")
    finished = graph.finish(
        "planner",
        PlanStepResult(
            status="succeeded",
            output={
                "planning_status": "delivered",
                "execution_status": "approved",
                "approved_batches": [{"batch_id": "approved-1"}],
            },
        ),
    )

    assert finished.step("planner__publication").status == "succeeded"
    assert finished.step("writer").status == "pending"
    assert "writer" in {step.step_id for step in finished.runnable_steps()}


def test_compaction_checkpoint_and_restart_rehydrate_final_planning_state(
    tmp_path: Path,
) -> None:
    """验证快照、frontier、三次尝试、repair 和最终规划跨压缩及重启存活。"""
    session = Session(session_id="restart", session_epoch="epoch-1")
    snapshot = _complete_snapshot()
    _install_snapshot(session, snapshot)
    store = PlanningSnapshotStore(tmp_path, session.session_id, session.session_epoch)
    stored_locator = store.store(snapshot)
    scope = planning_snapshot_scope(snapshot.target_path, snapshot.map_layer)
    locator = {
        **stored_locator,
        "planner_projection": snapshot.planner_projection(),
    }
    replace_map_state_field(
        session.map_task_state,
        "authoritative_snapshots",
        {scope: locator},
        target=snapshot.target_path,
        revision=snapshot.map_revision,
    )

    for ordinal in range(1, 4):
        args = _candidate(ordinal)
        assert map_platform_plan_call_error(session, "validate_platform_level_plan", args) is None
        remember_map_plan_progress(
            session,
            "validate_platform_level_plan",
            args,
            _failed_result(ordinal),
            project_root=tmp_path,
        )

    digest = build_map_progress_digest(session, project_root=tmp_path)
    assert snapshot.snapshot_id in digest
    assert "planning_attempts=" in digest
    assert "planning_publication=" in digest
    assert "blocked_by_validation" in digest
    assert "source_id" not in digest
    history = next(iter(session.map_task_state.planning_attempt_history.values()))
    assert all((tmp_path / item["repair_artifact_ref"]).is_file() for item in history)

    checkpoint = session.map_task_state.make_checkpoint("restart fixture")
    assert checkpoint["authoritative_snapshots"]
    assert checkpoint["planning_attempt_history"]
    assert checkpoint["planning_publications"]
    restored = deepcopy(session)
    restored.map_task_state = MapTaskState.from_dict(session.map_task_state.to_dict())

    restored_snapshot = PlanningSnapshotStore(
        tmp_path,
        restored.session_id,
        restored.session_epoch,
    ).read(str(locator["artifact_ref"]))
    assert restored_snapshot.route_facts["reachable_frontier"] == {"x": 5, "y": 1}
    assert restored.map_task_state.planning_attempt_history == (
        session.map_task_state.planning_attempt_history
    )
    assert restored.map_task_state.planning_publications == (
        session.map_task_state.planning_publications
    )
    assert restored.map_task_state.checkpoint == session.map_task_state.checkpoint
    fourth_error = map_platform_plan_call_error(
        restored,
        "validate_platform_level_plan",
        _candidate(4),
    )
    assert fourth_error is not None
    assert "planning_attempts_exhausted" in fourth_error
