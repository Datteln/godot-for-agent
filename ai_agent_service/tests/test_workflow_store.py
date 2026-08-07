"""当前版 manifest 工作流持久化与回放完整性测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.orchestrator.map_progress import MapTaskState
from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
)
from app.workflow.contracts import WorkflowIntegrityError
from app.workflow.store import WorkflowStore


def _store(
    root: Path,
    *,
    event_threshold: int = 10_000,
    byte_threshold: int = 100_000_000,
) -> WorkflowStore:
    """创建具有稳定 Session 身份的测试工作流 store。"""
    return WorkflowStore(
        root,
        "session-1",
        "epoch-1",
        snapshot_event_threshold=event_threshold,
        snapshot_byte_threshold=byte_threshold,
    )


def _append_progress(state: MapTaskState, count: int) -> None:
    """追加指定数量的合法进度事件。"""
    for index in range(count):
        dispatch_map_workflow_event(
            state,
            make_map_workflow_event(
                state,
                "progress_recorded",
                "Map/Ground",
                index % 3,
                {"category": f"batch-{index}", "count": index},
            ),
        )


class WorkflowStoreTests(unittest.TestCase):
    """验证工作流的唯一持久 authority 与失败闭合行为。"""

    def test_more_than_512_events_replay_and_next_sequence_survives_restart(self) -> None:
        """超过旧上限的所有事件均可回放，重启后继续分配更大序号。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            _append_progress(state, 520)

            publication = store.publish(state, lineage="lineage-1", commit_id="commit-1")
            restored = store.load(lineage="lineage-1")

            self.assertEqual(publication.high_water_seq, 520)
            self.assertEqual(restored.workflow_high_water_seq, 520)
            self.assertEqual(restored.pending_workflow_events, [])
            next_event = make_map_workflow_event(
                restored,
                "progress_recorded",
                "Map/Ground",
                3,
                {"category": "after-restart", "count": 521},
            )
            self.assertEqual(next_event.event_seq, 521)

    def test_manifest_ignores_unreferenced_prepared_segment(self) -> None:
        """未被 manifest 引用的准备文件不参与正常回放。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            _append_progress(state, 2)
            store.publish(state, lineage="lineage-1")
            orphan = store.manifest_path.parent / "segments" / "events-orphan.json"
            orphan.write_text('{"prepared":true}', encoding="utf-8")

            restored = store.load(lineage="lineage-1")

            self.assertEqual(restored.workflow_high_water_seq, 2)

    def test_digest_corruption_fails_closed_and_preserves_files(self) -> None:
        """当前 manifest 引用内容损坏时只读报错且保留诊断文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            _append_progress(state, 2)
            store.publish(state, lineage="lineage-1")
            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            digest = manifest["segment_digests"][0]
            segment = next(
                (store.manifest_path.parent / "segments").glob(f"*-{digest[:20]}.json")
            )
            payload = json.loads(segment.read_text(encoding="utf-8"))
            payload["events"][0]["payload"]["count"] = 999
            segment.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(WorkflowIntegrityError):
                store.load(lineage="lineage-1")

            self.assertTrue(segment.exists())

    def test_missing_selected_segment_fails_closed(self) -> None:
        """manifest 所选事件段缺失时不得回退到快照或其他文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            _append_progress(state, 1)
            store.publish(state, lineage="lineage-1")
            segment = next((store.manifest_path.parent / "segments").glob("*.json"))
            segment.unlink()

            with self.assertRaises(WorkflowIntegrityError):
                store.load(lineage="lineage-1")

    def test_compaction_switches_verified_snapshot_before_segment_cleanup(self) -> None:
        """达到阈值后 manifest 选择完整快照且回放状态不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root, event_threshold=3)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            _append_progress(state, 3)

            publication = store.publish(state, lineage="lineage-1")
            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            restored = store.load(lineage="lineage-1")

            self.assertTrue(publication.compacted)
            self.assertEqual(manifest["segment_digests"], [])
            self.assertEqual(manifest["high_water_seq"], 3)
            self.assertEqual(restored.to_dict(), state.to_dict())

    def test_target_and_revision_scopes_remain_independent(self) -> None:
        """不同目标与 revision 的 reducer 作用域在重启后互不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            state = MapTaskState()
            store.initialize(state, lineage="lineage-1")
            dispatch_map_workflow_event(
                state,
                make_map_workflow_event(
                    state,
                    "progress_recorded",
                    "Map/Ground",
                    1,
                    {"category": "ground", "count": 1},
                ),
            )
            dispatch_map_workflow_event(
                state,
                make_map_workflow_event(
                    state,
                    "progress_recorded",
                    "Map/Background",
                    7,
                    {"category": "background", "count": 2},
                ),
            )
            store.publish(state, lineage="lineage-1")

            restored = store.load(lineage="lineage-1")

            self.assertIn("Map/Ground::revision=1", restored.workflow_scopes)
            self.assertIn("Map/Background::revision=7", restored.workflow_scopes)

    def test_embedded_serialization_contains_no_event_tail(self) -> None:
        """MapTaskState 序列化不包含历史尾或事务内待发布事件。"""
        state = MapTaskState()
        _append_progress(state, 2)

        payload = state.to_dict()

        self.assertNotIn("workflow_events", payload)
        self.assertNotIn("pending_workflow_events", payload)
        self.assertEqual(payload["workflow_high_water_seq"], 2)


if __name__ == "__main__":
    unittest.main()
