"""地图域 owner/lineage/publication reducer 状态的持久化与恢复测试。"""

from __future__ import annotations

from app.orchestrator.map_planning_contexts import (
    MapPlanningContextBundle,
    MapPlanningContextEntry,
)
from app.orchestrator.map_progress import (
    MapTaskState,
    record_map_approval_identity,
    record_map_child_lineage,
    record_map_owner_link,
    record_map_owner_publication,
    record_planning_context_refresh,
)
from app.orchestrator.map_workflow import replace_map_state_field


def _fresh() -> MapTaskState:
    """构造一个结构 revision 已设置的 MapTaskState。"""
    state = MapTaskState()
    replace_map_state_field(state, "structure_revision", 3)
    return state


class TestMapOwnerStateRoundTrip:
    """owner/macro/lineage/publication 链接字段的持久化与恢复。"""

    def test_link_fields_round_trip(self) -> None:
        """链接字段经 to_dict/from_dict 往返后保持。"""
        state = _fresh()
        record_map_owner_link(
            state,
            macro_step_id="expand",
            owner_frame_id="f1",
            domain_task_id="expand:1",
            target="Map/Main",
            revision=3,
        )
        record_map_child_lineage(
            state, child_frame_id="fc", child_stage="planner", target="Map/Main", revision=3
        )
        record_map_owner_publication(
            state,
            publication={"status": "awaiting_confirmation"},
            target="Map/Main",
            revision=3,
        )
        record_map_approval_identity(
            state,
            approval_identity={"candidate_ref": "art://c/1"},
            target="Map/Main",
            revision=3,
        )
        hydrated = MapTaskState.from_dict(state.to_dict())
        assert hydrated.macro_step_id == "expand"
        assert hydrated.owner_frame_id == "f1"
        assert hydrated.domain_task_id == "expand:1"
        assert hydrated.child_lineage == [{"child_frame_id": "fc", "child_stage": "planner"}]
        assert hydrated.owner_publication == {"status": "awaiting_confirmation"}
        assert hydrated.approval_identity == {"candidate_ref": "art://c/1"}

    def test_link_fields_emit_events(self) -> None:
        """记录链接字段时派发 reducer 事件。"""
        state = _fresh()
        before = len(state.workflow_events)
        record_map_owner_link(
            state,
            macro_step_id="expand",
            owner_frame_id="f1",
            domain_task_id="expand:1",
            target="Map/Main",
            revision=3,
        )
        record_map_child_lineage(
            state, child_frame_id="fc", child_stage="planner", target="Map/Main", revision=3
        )
        after = len(state.workflow_events)
        assert after - before == 2
        assert state.workflow_events[-2]["event_type"] == "map_owner_linked"
        assert state.workflow_events[-1]["event_type"] == "map_child_started"

    def test_task_epoch_reset_clears_link_fields(self) -> None:
        """新任务 epoch 按 lifecycle 元数据重置链接字段。"""
        state = _fresh()
        record_map_owner_link(
            state,
            macro_step_id="expand",
            owner_frame_id="f1",
            domain_task_id="expand:1",
            target="Map/Main",
            revision=3,
        )
        reset = state.task_epoch_reset_values()
        # 链接字段属于 task scope，应在 reset 默认值中
        assert reset["macro_step_id"] == ""
        assert reset["owner_frame_id"] == ""
        assert reset["domain_task_id"] == ""
        assert reset["child_lineage"] == []
        assert reset["owner_publication"] is None
        assert reset["approval_identity"] is None

    def test_legacy_dict_without_link_fields_hydrates(self) -> None:
        """旧持久化记录（无链接字段）能安全 hydrate 为默认值。"""
        legacy = {"task_id": "t", "stage": "read", "structure_revision": 1}
        hydrated = MapTaskState.from_dict(legacy)
        assert hydrated.task_id == "t"
        assert hydrated.macro_step_id == ""
        assert hydrated.child_lineage == []
        assert hydrated.owner_publication is None


class TestPlanningContextRefresh:
    """task 9.3：独立键控规划上下文注册表，刷新一个条目保留所有不相关上下文。"""

    def _fresh(self) -> MapTaskState:
        state = MapTaskState()
        replace_map_state_field(state, "structure_revision", 3)
        return state

    def _mid_entry(self) -> MapPlanningContextEntry:
        return MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/1",
            digest="sha256:mid",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=7,
            fact_fields=("occupancy",),
        )

    def _bg_entry(self) -> MapPlanningContextEntry:
        return MapPlanningContextEntry(
            context_id="bg-ctx",
            semantic_role="background",
            artifact_ref="art://bg/1",
            digest="sha256:bg",
            provenance={"kind": "snapshot"},
            target_path="Map/Background",
            map_layer=0,
            source_revision=3,
            fact_fields=("coverage",),
        )

    def test_refresh_emits_event(self) -> None:
        state = self._fresh()
        before = len(state.workflow_events)
        record_planning_context_refresh(
            state,
            context_entry=self._mid_entry(),
            target="Map/Main",
            revision=7,
        )
        after = len(state.workflow_events)
        assert after - before == 1
        assert state.workflow_events[-1]["event_type"] == "planning_context_refreshed"
        assert state.workflow_events[-1]["payload"]["context_id"] == "mid-ctx"

    def test_refresh_upserts_context_entry(self) -> None:
        state = self._fresh()
        record_planning_context_refresh(
            state,
            context_entry=self._mid_entry(),
            target="Map/Main",
            revision=7,
        )
        assert "mid-ctx" in state.planning_contexts
        hydrated = MapPlanningContextEntry.from_dict(state.planning_contexts["mid-ctx"])
        assert hydrated.source_revision == 7
        assert hydrated.semantic_role == "mid"

    def test_refresh_one_preserves_unrelated(self) -> None:
        """刷新一个 gameplay 或 background 条目保留所有不相关上下文。"""
        state = self._fresh()
        # 先注册两个上下文
        replace_map_state_field(
            state,
            "planning_contexts",
            {
                "mid-ctx": self._mid_entry().to_dict(),
                "bg-ctx": self._bg_entry().to_dict(),
            },
        )
        # 刷新 mid 上下文
        refreshed_mid = MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/2",
            digest="sha256:mid-v2",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=8,
            fact_fields=("occupancy", "traversal"),
        )
        record_planning_context_refresh(
            state,
            context_entry=refreshed_mid,
            target="Map/Main",
            revision=8,
        )
        # mid 已更新
        assert state.planning_contexts["mid-ctx"]["source_revision"] == 8
        assert state.planning_contexts["mid-ctx"]["digest"] == "sha256:mid-v2"
        # bg 未受影响
        assert "bg-ctx" in state.planning_contexts
        assert state.planning_contexts["bg-ctx"]["source_revision"] == 3
        assert state.planning_contexts["bg-ctx"]["digest"] == "sha256:bg"

    def test_refresh_rebuilds_bundle(self) -> None:
        """刷新后自动重建规划上下文集合。"""
        state = self._fresh()
        replace_map_state_field(
            state,
            "planning_contexts",
            {
                "mid-ctx": self._mid_entry().to_dict(),
                "bg-ctx": self._bg_entry().to_dict(),
            },
        )
        refreshed_mid = MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/2",
            digest="sha256:mid-v2",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=8,
            fact_fields=("occupancy",),
        )
        record_planning_context_refresh(
            state,
            context_entry=refreshed_mid,
            target="Map/Main",
            revision=8,
        )
        assert len(state.planning_context_bundles) >= 1
        bundle_dict = next(iter(state.planning_context_bundles.values()))
        bundle = MapPlanningContextBundle.from_dict(bundle_dict)
        assert len(bundle.contexts) == 2
        context_ids = {entry.context_id for entry in bundle.contexts}
        assert context_ids == {"mid-ctx", "bg-ctx"}

    def test_refresh_round_trip_survives_hydration(self) -> None:
        """刷新后经 to_dict/from_dict 往返，上下文注册表保持完整。"""
        state = self._fresh()
        record_planning_context_refresh(
            state,
            context_entry=self._mid_entry(),
            target="Map/Main",
            revision=7,
        )
        record_planning_context_refresh(
            state,
            context_entry=self._bg_entry(),
            target="Map/Background",
            revision=3,
        )
        hydrated = MapTaskState.from_dict(state.to_dict())
        assert "mid-ctx" in hydrated.planning_contexts
        assert "bg-ctx" in hydrated.planning_contexts
        assert hydrated.planning_contexts["mid-ctx"]["source_revision"] == 7
        assert hydrated.planning_contexts["bg-ctx"]["source_revision"] == 3

    def test_refresh_only_events_preserve_unrelated(self) -> None:
        """通过 reducer 事件回放验证：刷新一个上下文只改变对应条目。"""
        state = self._fresh()
        record_planning_context_refresh(
            state,
            context_entry=self._mid_entry(),
            target="Map/Main",
            revision=7,
        )
        record_planning_context_refresh(
            state,
            context_entry=self._bg_entry(),
            target="Map/Background",
            revision=3,
        )
        # 从事件回放：清空状态并通过 reduce 重建
        from app.orchestrator.map_workflow import (
            reduce_map_workflow,
            reducer_write_scope,
        )
        from app.orchestrator.runtime_contracts import MapWorkflowEvent

        clean = MapTaskState()
        with reducer_write_scope():
            replace_map_state_field(clean, "structure_revision", 3)
        for event_dict in state.workflow_events:
            event = MapWorkflowEvent(
                event_id=event_dict["event_id"],
                event_type=event_dict["event_type"],
                target=event_dict["target"],
                revision=event_dict["revision"],
                payload=event_dict.get("payload", {}),
            )
            with reducer_write_scope():
                clean = reduce_map_workflow(clean, event)
        assert "mid-ctx" in clean.planning_contexts
        assert "bg-ctx" in clean.planning_contexts
        assert clean.planning_contexts["mid-ctx"]["source_revision"] == 7
        assert clean.planning_contexts["bg-ctx"]["source_revision"] == 3
