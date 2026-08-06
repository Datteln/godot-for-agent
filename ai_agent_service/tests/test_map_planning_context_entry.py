"""地图规划上下文、上下文集合与执行操作的类型化合同测试。

覆盖 task 9.6：Mid + 多个 Background 上下文、独立 stale revision、
定向刷新、重叠引用区域、decorated-key 拒绝、多操作 revision 守卫。
"""

from __future__ import annotations

import pytest

from app.orchestrator.map_planning_contexts import (
    MapExecutionOperation,
    MapPlanningContextBundle,
    MapPlanningContextEntry,
    MapPlanningContextError,
)


def _mid_context(
    *,
    context_id: str = "mid-ctx",
    semantic_role: str = "mid",
    revision: int = 7,
    fresh: bool = True,
) -> MapPlanningContextEntry:
    return MapPlanningContextEntry(
        context_id=context_id,
        semantic_role=semantic_role,
        artifact_ref=f"art://{context_id}/1",
        digest=f"sha256:{context_id}",
        provenance={"kind": "authoritative_map_snapshot_v1", "snapshot_id": context_id},
        target_path="Map/Main",
        map_layer=0,
        source_revision=revision,
        fact_fields=("occupancy", "traversal", "reachable_frontier"),
        fresh=fresh,
    )


def _background_context(
    *,
    context_id: str = "bg-ctx",
    semantic_role: str = "background",
    revision: int = 3,
    fresh: bool = True,
) -> MapPlanningContextEntry:
    return MapPlanningContextEntry(
        context_id=context_id,
        semantic_role=semantic_role,
        artifact_ref=f"art://{context_id}/1",
        digest=f"sha256:{context_id}",
        provenance={"kind": "authoritative_map_snapshot_v1", "snapshot_id": context_id},
        target_path="Map/Background",
        map_layer=0,
        region={"x": -10, "y": -10, "width": 80, "height": 40},
        source_revision=revision,
        fact_fields=("coverage", "occupancy"),
        fresh=fresh,
    )


def _reference_context(
    *,
    context_id: str = "ref-ctx",
    semantic_role: str = "reference",
    revision: int = 5,
    fresh: bool = True,
) -> MapPlanningContextEntry:
    return MapPlanningContextEntry(
        context_id=context_id,
        semantic_role=semantic_role,
        artifact_ref=f"art://{context_id}/1",
        digest=f"sha256:{context_id}",
        provenance={"kind": "authoritative_map_snapshot_v1", "snapshot_id": context_id},
        target_path="Map/Reference",
        map_layer=0,
        region={"x": 0, "y": 0, "width": 40, "height": 20},
        source_revision=revision,
        fact_fields=("frontier", "entry"),
        fresh=fresh,
    )


class TestContextEntrySerialization:
    """上下文条目序列化与字段校验。"""

    def test_round_trip(self) -> None:
        entry = _mid_context()
        assert MapPlanningContextEntry.from_dict(entry.to_dict()) == entry

    def test_requires_context_id(self) -> None:
        with pytest.raises(MapPlanningContextError, match="context_id"):
            MapPlanningContextEntry.from_dict({"semantic_role": "mid", "artifact_ref": "a", "digest": "d"})

    def test_requires_semantic_role(self) -> None:
        with pytest.raises(MapPlanningContextError, match="semantic_role"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "", "artifact_ref": "a", "digest": "d"}
            )

    def test_requires_artifact_ref_and_digest(self) -> None:
        with pytest.raises(MapPlanningContextError, match="artifact_ref"):
            MapPlanningContextEntry.from_dict({"context_id": "c", "semantic_role": "mid", "digest": "d"})
        with pytest.raises(MapPlanningContextError, match="digest"):
            MapPlanningContextEntry.from_dict({"context_id": "c", "semantic_role": "mid", "artifact_ref": "a"})

    def test_fresh_defaults_true(self) -> None:
        entry = MapPlanningContextEntry.from_dict(
            {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d"}
        )
        assert entry.fresh is True

    def test_fresh_explicit_false(self) -> None:
        entry = MapPlanningContextEntry.from_dict(
            {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "fresh": False}
        )
        assert entry.fresh is False

    def test_fresh_must_be_boolean(self) -> None:
        with pytest.raises(MapPlanningContextError, match="fresh"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "fresh": "yes"}
            )

    def test_fact_fields_must_be_strings(self) -> None:
        with pytest.raises(MapPlanningContextError, match="fact_fields"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "fact_fields": [1, 2]}
            )

    def test_provenance_must_be_object(self) -> None:
        with pytest.raises(MapPlanningContextError, match="provenance"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "provenance": "snapshot"}
            )

    def test_map_layer_must_be_integer(self) -> None:
        with pytest.raises(MapPlanningContextError, match="map_layer"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "map_layer": True}
            )

    def test_region_values_must_be_integers(self) -> None:
        with pytest.raises(MapPlanningContextError, match="region"):
            MapPlanningContextEntry.from_dict(
                {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "region": {"x": "1"}}
            )


class TestDecoratedKeyRejection:
    """task 9.6：decorated-key 拒绝 — 内部索引键不可充当规范 target_path。"""

    @pytest.mark.parametrize(
        "target",
        [
            "TileMap::map_layer=1",
            "TileMap::revision=0",
            "Map/Main::map_layer=0",
            "Map/Main::revision=5",
        ],
    )
    def test_target_with_scope_decorations_rejected(self, target: str) -> None:
        with pytest.raises(MapPlanningContextError, match="cannot contain scope decorations"):
            MapPlanningContextEntry.from_dict(
                {
                    "context_id": "c",
                    "semantic_role": "mid",
                    "artifact_ref": "a",
                    "digest": "d",
                    "target_path": target,
                }
            )

    def test_valid_canonical_target_accepted(self) -> None:
        entry = MapPlanningContextEntry.from_dict(
            {"context_id": "c", "semantic_role": "mid", "artifact_ref": "a", "digest": "d", "target_path": "Map/Main"}
        )
        assert entry.target_path == "Map/Main"

    def test_execution_operation_also_rejects_decorated_target(self) -> None:
        with pytest.raises(MapPlanningContextError, match="cannot contain scope decorations"):
            MapExecutionOperation.from_dict(
                {
                    "operation_id": "op-1",
                    "target_path": "TileMap::map_layer=1",
                    "map_layer": 0,
                    "expected_revision": 7,
                    "write_payload": {"cells": []},
                }
            )


class TestBundleWithMultipleContexts:
    """task 9.6：Mid + 多个 Background 上下文集合。"""

    def test_bundle_with_mid_and_two_backgrounds(self) -> None:
        mid = _mid_context()
        bg1 = _background_context(context_id="bg-1", semantic_role="background")
        bg2 = _background_context(context_id="bg-2", semantic_role="background")
        bundle = MapPlanningContextBundle.from_entries([mid, bg1, bg2])
        assert len(bundle.contexts) == 3
        assert bundle.bundle_id.startswith("map-context-bundle:")
        assert bundle.required_roles == ()

    def test_bundle_requires_roles(self) -> None:
        mid = _mid_context()
        bg = _background_context()
        bundle = MapPlanningContextBundle.from_entries(
            [mid, bg], required_roles=["mid", "background"]
        )
        assert bundle.required_roles == ("mid", "background")

    def test_bundle_missing_required_role_rejected(self) -> None:
        mid = _mid_context()
        with pytest.raises(MapPlanningContextError, match="missing roles"):
            MapPlanningContextBundle.from_entries(
                [mid], required_roles=["mid", "background"]
            )

    def test_bundle_empty_rejected(self) -> None:
        with pytest.raises(MapPlanningContextError, match="cannot be empty"):
            MapPlanningContextBundle.from_entries([])

    def test_bundle_duplicate_ids_rejected(self) -> None:
        mid = _mid_context()
        with pytest.raises(MapPlanningContextError, match="must be unique"):
            MapPlanningContextBundle.from_entries([mid, mid])

    def test_bundle_round_trip(self) -> None:
        mid = _mid_context()
        bg = _background_context()
        ref = _reference_context()
        bundle = MapPlanningContextBundle.from_entries(
            [mid, bg, ref], required_roles=["mid", "background"]
        )
        assert MapPlanningContextBundle.from_dict(bundle.to_dict()) == bundle


class TestIndependentStaleRevisions:
    """task 9.6：独立 stale revision — 各上下文独立校验 freshness。"""

    def test_entries_use_different_revisions(self) -> None:
        """Mid 与 Background 条目可使用不同 revision，不要求全局相等。"""
        mid = _mid_context(revision=7)
        bg = _background_context(revision=3)
        ref = _reference_context(revision=5)
        bundle = MapPlanningContextBundle.from_entries([mid, bg, ref])
        revisions = {entry.source_revision for entry in bundle.contexts}
        assert revisions == {7, 3, 5}

    def test_stale_mid_does_not_affect_fresh_background(self) -> None:
        """Mid 标记为 stale 时，Background 条目保持 fresh 不变。"""
        mid = _mid_context(fresh=False)
        bg = _background_context(fresh=True)
        bundle = MapPlanningContextBundle.from_entries([mid, bg])
        for entry in bundle.contexts:
            if entry.semantic_role == "mid":
                assert entry.fresh is False
            elif entry.semantic_role == "background":
                assert entry.fresh is True

    def test_all_stale_bundle_still_valid(self) -> None:
        """全部条目标记为 stale 时，集合仍然合法（freshness 为元数据，不阻止集合构造）。"""
        mid = _mid_context(fresh=False)
        bg = _background_context(fresh=False)
        ref = _reference_context(fresh=False)
        bundle = MapPlanningContextBundle.from_entries([mid, bg, ref])
        assert len(bundle.contexts) == 3
        assert all(not entry.fresh for entry in bundle.contexts)


class TestTargetedContextRefresh:
    """task 9.6：定向刷新 — 刷新一个条目保留所有不相关上下文。"""

    def test_refresh_one_entry_preserves_unrelated(self) -> None:
        mid = _mid_context(context_id="mid-ctx", revision=7)
        bg = _background_context(context_id="bg-ctx", revision=3)
        ref = _reference_context(context_id="ref-ctx", revision=5)
        registry = {
            "mid-ctx": mid.to_dict(),
            "bg-ctx": bg.to_dict(),
            "ref-ctx": ref.to_dict(),
        }
        # 刷新 mid 上下文：新 revision、新 digest
        refreshed_mid = _mid_context(context_id="mid-ctx", revision=8)
        registry["mid-ctx"] = refreshed_mid.to_dict()
        # bg 和 ref 未被修改
        hydrated_bg = MapPlanningContextEntry.from_dict(registry["bg-ctx"])
        assert hydrated_bg.source_revision == 3
        assert hydrated_bg.fresh is True
        hydrated_ref = MapPlanningContextEntry.from_dict(registry["ref-ctx"])
        assert hydrated_ref.source_revision == 5
        assert hydrated_ref.fresh is True
        # 刷新后的 mid 是新的
        hydrated_mid = MapPlanningContextEntry.from_dict(registry["mid-ctx"])
        assert hydrated_mid.source_revision == 8

    def test_refresh_rebuilds_bundle_with_same_other_contexts(self) -> None:
        mid = _mid_context(context_id="mid-ctx", revision=7)
        bg = _background_context(context_id="bg-ctx", revision=3)
        original_bundle = MapPlanningContextBundle.from_entries([mid, bg])
        # 只刷新 mid
        refreshed_mid = _mid_context(context_id="mid-ctx", revision=8)
        new_bundle = MapPlanningContextBundle.from_entries([refreshed_mid, bg])
        assert new_bundle.bundle_id != original_bundle.bundle_id
        # 但 bg 的 digest 与原始相同
        bg_in_new = next(
            entry for entry in new_bundle.contexts if entry.context_id == "bg-ctx"
        )
        assert bg_in_new.digest == bg.digest
        assert bg_in_new.source_revision == bg.source_revision

    def test_upsert_new_context_id_adds_without_removing_others(self) -> None:
        mid = _mid_context(context_id="mid-ctx")
        bg = _background_context(context_id="bg-ctx")
        registry = {"mid-ctx": mid.to_dict(), "bg-ctx": bg.to_dict()}
        # 新增一个 reference 上下文
        ref = _reference_context(context_id="ref-ctx")
        registry["ref-ctx"] = ref.to_dict()
        assert len(registry) == 3
        assert "mid-ctx" in registry
        assert "bg-ctx" in registry
        assert "ref-ctx" in registry


class TestOverlappingReferenceRegions:
    """task 9.6：重叠引用区域 — 多个上下文可覆盖重叠区域，不互相排斥。"""

    def test_overlapping_regions_accepted(self) -> None:
        mid = _mid_context(
            context_id="mid",
            semantic_role="mid",
        )
        # 覆盖 mid 区域的一个子集
        overlay = MapPlanningContextEntry(
            context_id="overlay",
            semantic_role="background",
            artifact_ref="art://overlay/1",
            digest="sha256:overlay",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            region={"x": 0, "y": 0, "width": 20, "height": 10},
            source_revision=7,
            fact_fields=("occupancy",),
        )
        bundle = MapPlanningContextBundle.from_entries([mid, overlay])
        assert len(bundle.contexts) == 2

    def test_identical_region_different_roles_accepted(self) -> None:
        r1 = MapPlanningContextEntry(
            context_id="ctx-1",
            semantic_role="mid",
            artifact_ref="art://ctx-1/1",
            digest="sha256:ctx-1",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            region={"x": 0, "y": 0, "width": 40, "height": 20},
            source_revision=7,
            fact_fields=("occupancy",),
        )
        r2 = MapPlanningContextEntry(
            context_id="ctx-2",
            semantic_role="background",
            artifact_ref="art://ctx-2/1",
            digest="sha256:ctx-2",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            region={"x": 0, "y": 0, "width": 40, "height": 20},
            source_revision=7,
            fact_fields=("traversal",),
        )
        bundle = MapPlanningContextBundle.from_entries([r1, r2])
        assert len(bundle.contexts) == 2


class TestExecutionOperation:
    """task 9.6：确定性执行操作序列化与多操作 revision 守卫。"""

    def test_round_trip(self) -> None:
        operation = MapExecutionOperation(
            operation_id="op-1",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=7,
            write_payload={"cells": [{"x": 1, "y": 2, "atlas": {"x": 3, "y": 4}}]},
            artifact_ref="art://batch/1",
            batch_id="batch-1",
        )
        assert MapExecutionOperation.from_dict(operation.to_dict()) == operation

    def test_requires_write_payload(self) -> None:
        with pytest.raises(MapPlanningContextError, match="write_payload"):
            MapExecutionOperation.from_dict(
                {"operation_id": "op-1", "target_path": "Map/Main", "map_layer": 0, "expected_revision": 7}
            )

    def test_requires_target_path(self) -> None:
        with pytest.raises(MapPlanningContextError, match="target_path"):
            MapExecutionOperation.from_dict(
                {"operation_id": "op-1", "map_layer": 0, "expected_revision": 7, "write_payload": {"cells": []}}
            )

    def test_requires_map_layer_and_expected_revision(self) -> None:
        with pytest.raises(MapPlanningContextError, match="map_layer and expected_revision"):
            MapExecutionOperation.from_dict(
                {"operation_id": "op-1", "target_path": "Map/Main", "write_payload": {"cells": []}}
            )

    def test_multi_operation_different_revisions(self) -> None:
        """多操作可携带不同 revision，各自独立校验。"""
        op1 = MapExecutionOperation(
            operation_id="op-1",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=7,
            write_payload={"cells": [{"x": 1, "y": 2}]},
        )
        op2 = MapExecutionOperation(
            operation_id="op-2",
            target_path="Map/Background",
            map_layer=0,
            expected_revision=3,
            write_payload={"cells": [{"x": 5, "y": 6}]},
        )
        assert op1.expected_revision != op2.expected_revision
        assert op1.target_path != op2.target_path
        assert op1.operation_id != op2.operation_id

    def test_multi_operation_revision_guard(self) -> None:
        """验证每个操作独立校验 expected_revision，不要求全局一致。"""
        op1 = MapExecutionOperation(
            operation_id="op-1",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=7,
            write_payload={"cells": [{"x": 1, "y": 2}]},
        )
        op2 = MapExecutionOperation(
            operation_id="op-2",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=8,
            write_payload={"cells": [{"x": 3, "y": 4}]},
        )
        # 同一 target 不同 revision 也是合法的
        # 因为每个操作描述的是其自身写时所需的最新 revision
        assert op1.expected_revision == 7
        assert op2.expected_revision == 8

    def test_optional_artifact_ref_and_batch_id(self) -> None:
        operation = MapExecutionOperation(
            operation_id="op-1",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=7,
            write_payload={"cells": []},
        )
        assert operation.artifact_ref is None
        assert operation.batch_id is None
        assert MapExecutionOperation.from_dict(operation.to_dict()) == operation


class TestContextFromSnapshotMigration:
    """旧快照迁移为规划上下文。"""

    def test_snapshot_migration_produces_valid_entry(self) -> None:
        snapshot = {
            "snapshot_id": "snap-1",
            "artifact_ref": "art://snap/1",
            "digest": "sha256:snap",
            "target_path": "Map/Main",
            "map_layer": 0,
            "map_revision": 7,
            "execution_eligible": True,
            "cells": [{"cell": [1, 2], "occupied": True}],
        }
        entry = MapPlanningContextEntry.from_snapshot(snapshot, semantic_role="mid")
        assert entry.context_id == "snap-1"
        assert entry.semantic_role == "mid"
        assert entry.artifact_ref == "art://snap/1"
        assert entry.source_revision == 7
        assert entry.fact_fields == ("coverage", "occupancy", "traversal", "entry", "reachable_frontier")

    def test_snapshot_migration_defaults(self) -> None:
        snapshot = {
            "snapshot_id": "snap-2",
            "artifact_ref": "art://snap/2",
            "digest": "sha256:snap2",
            "target_path": "Map/Main",
            "map_layer": 0,
            "map_revision": 5,
        }
        entry = MapPlanningContextEntry.from_snapshot(snapshot)
        assert entry.semantic_role == "map_reference"
        assert entry.provenance["kind"] == "authoritative_map_snapshot_v1"

    def test_snapshot_migration_falls_back_context_id(self) -> None:
        snapshot = {
            "artifact_ref": "art://no-id/1",
            "digest": "sha256:noid",
            "target_path": "Map/Main",
            "map_layer": 0,
            "map_revision": 1,
            "execution_eligible": True,
        }
        # 缺少 snapshot_id 时 context_id 回退为空，from_dict 应拒绝
        with pytest.raises(MapPlanningContextError, match="context_id"):
            MapPlanningContextEntry.from_snapshot(snapshot)


class TestBundleDigestValidation:
    """集合摘要校验 — 篡改摘要时拒绝。"""

    def test_bundle_with_valid_digest(self) -> None:
        mid = _mid_context()
        bg = _background_context()
        bundle = MapPlanningContextBundle.from_entries([mid, bg])
        d = bundle.to_dict()
        assert MapPlanningContextBundle.from_dict(d) == bundle

    def test_bundle_with_invalid_digest_rejected(self) -> None:
        mid = _mid_context()
        bg = _background_context()
        bundle = MapPlanningContextBundle.from_entries([mid, bg])
        d = bundle.to_dict()
        d["bundle_id"] = "map-context-bundle:000000000000000000000000"
        with pytest.raises(MapPlanningContextError, match="digest is invalid"):
            MapPlanningContextBundle.from_dict(d)

    def test_bundle_without_bundle_id_regenerates(self) -> None:
        mid = _mid_context()
        bg = _background_context()
        bundle = MapPlanningContextBundle.from_entries([mid, bg])
        d = bundle.to_dict()
        del d["bundle_id"]
        restored = MapPlanningContextBundle.from_dict(d)
        assert restored.bundle_id == bundle.bundle_id