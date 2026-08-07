"""项目边界内的 Map 工作流快照、事件段与 manifest 持久化。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from app.storage.atomic import atomic_write_json
from app.workflow.contracts import (
    WORKFLOW_EVENT_SCHEMA_VERSION,
    WORKFLOW_SCHEMA_EPOCH,
    WORKFLOW_SCHEMA_VERSION,
    WorkflowEvent,
    WorkflowIntegrityError,
    WorkflowManifest,
    WorkflowSegment,
    WorkflowSnapshot,
)

if TYPE_CHECKING:
    from app.orchestrator.map_progress import MapTaskState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkflowPublication:
    """描述一次已由 manifest 原子选中的工作流发布。"""

    manifest_digest: str
    generation: int
    high_water_seq: int
    segment_digest: str | None
    compacted: bool


@dataclass(frozen=True, slots=True)
class PreparedWorkflowPublication:
    """描述已准备但尚未由权威 manifest 选中的工作流发布。"""

    manifest: WorkflowManifest
    segment_digest: str | None
    prepared_manifest_path: Path | None


class WorkflowStore:
    """维护单个 Session epoch 的唯一当前工作流持久化表示。"""

    def __init__(
        self,
        project_root: Path,
        session_id: str,
        session_epoch: str,
        *,
        snapshot_event_threshold: int,
        snapshot_byte_threshold: int,
    ) -> None:
        """构造项目内存储并验证压缩阈值。"""
        if snapshot_event_threshold < 1 or snapshot_byte_threshold < 1:
            raise ValueError("workflow snapshot thresholds must be positive")
        if not session_id or not session_epoch:
            raise ValueError("workflow store requires session id and epoch")
        session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
        epoch_key = hashlib.sha256(session_epoch.encode("utf-8")).hexdigest()[:20]
        self._project_root = project_root.resolve()
        self._root = (
            self._project_root
            / ".ai_agent_service"
            / "workflows"
            / session_key
            / epoch_key
        )
        self._session_epoch = session_epoch
        self._snapshot_event_threshold = snapshot_event_threshold
        self._snapshot_byte_threshold = snapshot_byte_threshold

    @property
    def manifest_path(self) -> Path:
        """返回当前权威 manifest 路径。"""
        return self._root / "manifest.json"

    def current_manifest(self) -> WorkflowManifest:
        """返回通过摘要与 schema 验证的当前 manifest。"""
        return self._read_manifest()

    def initialize(self, state: "MapTaskState", *, lineage: str) -> WorkflowManifest:
        """为零序状态创建首个完整快照与 manifest。"""
        if self.manifest_path.exists():
            manifest = self._read_manifest()
            self._assert_identity(manifest, lineage)
            return manifest
        if state.workflow_high_water_seq != 0 or state.pending_workflow_events:
            raise WorkflowIntegrityError(
                "workflow must be initialized before the first reducer event"
            )
        snapshot = WorkflowSnapshot.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            snapshot_seq=0,
            state=state.to_dict(),
        )
        self._write_snapshot(snapshot)
        manifest = WorkflowManifest.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            high_water_seq=0,
            snapshot_digest=snapshot.digest,
            segment_digests=(),
            generation=1,
        )
        atomic_write_json(self.manifest_path, manifest.to_payload())
        return manifest

    def publish(
        self,
        state: "MapTaskState",
        *,
        lineage: str,
        commit_id: str | None = None,
    ) -> WorkflowPublication:
        """写入不可变事件段并以最后一次 manifest 替换发布它。"""
        prepared = self.prepare(state, lineage=lineage, commit_id=commit_id)
        next_manifest = self.commit_prepared(prepared, state)
        compacted = False
        if self._should_compact(next_manifest):
            next_manifest = self.compact(state, lineage=lineage)
            compacted = True
        return WorkflowPublication(
            next_manifest.digest,
            next_manifest.generation,
            next_manifest.high_water_seq,
            prepared.segment_digest,
            compacted,
        )

    def prepare(
        self,
        state: "MapTaskState",
        *,
        lineage: str,
        commit_id: str | None = None,
    ) -> PreparedWorkflowPublication:
        """准备不可变段与下一 manifest，但不改变权威选择或运行态。"""
        manifest = self.initialize(state, lineage=lineage)
        self._assert_identity(manifest, lineage)
        raw_events = list(state.pending_workflow_events)
        if not raw_events:
            if state.workflow_high_water_seq != manifest.high_water_seq:
                raise WorkflowIntegrityError(
                    "workflow state high-water advanced without staged events"
                )
            return PreparedWorkflowPublication(manifest, None, None)
        events = tuple(self._event_from_payload(item) for item in raw_events)
        if events[0].event_seq != manifest.high_water_seq + 1:
            raise WorkflowIntegrityError("staged workflow events do not follow manifest")
        if events[-1].event_seq != state.workflow_high_water_seq:
            raise WorkflowIntegrityError("staged workflow events do not reach state high-water")
        predecessor = (
            manifest.segment_digests[-1]
            if manifest.segment_digests
            else manifest.snapshot_digest
        )
        segment = WorkflowSegment.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            predecessor_digest=predecessor,
            events=events,
        )
        self._write_segment(segment, commit_id or segment.digest)
        next_manifest = WorkflowManifest.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            high_water_seq=segment.last_seq,
            snapshot_digest=manifest.snapshot_digest,
            segment_digests=(*manifest.segment_digests, segment.digest),
            generation=manifest.generation + 1,
        )
        prepared_path = (
            self._root
            / "prepared"
            / f"manifest-{next_manifest.generation}-{next_manifest.digest[:20]}.json"
        )
        self._write_immutable(prepared_path, next_manifest.to_payload())
        return PreparedWorkflowPublication(next_manifest, segment.digest, prepared_path)

    def commit_prepared(
        self,
        prepared: PreparedWorkflowPublication,
        state: "MapTaskState",
    ) -> WorkflowManifest:
        """将已准备 manifest 设为权威，并清除已发布事务事件。"""
        if state.workflow_high_water_seq != prepared.manifest.high_water_seq:
            raise WorkflowIntegrityError("prepared workflow state high-water changed")
        if prepared.prepared_manifest_path is not None:
            verified = self._manifest_from_payload(
                self._read_json(prepared.prepared_manifest_path)
            )
            if verified.digest != prepared.manifest.digest:
                raise WorkflowIntegrityError("prepared workflow manifest digest mismatch")
            atomic_write_json(self.manifest_path, verified.to_payload())
            prepared.prepared_manifest_path.unlink(missing_ok=True)
            self._clear_pending_events(state)
        return prepared.manifest

    def reconcile(self, *, manifest_digest: str, generation: int) -> WorkflowManifest:
        """按已提交 Session 引用完成一次中断的 manifest 最终切换。"""
        try:
            current = self._read_manifest()
        except WorkflowIntegrityError:
            current = None
        if (
            current is not None
            and current.digest == manifest_digest
            and current.generation == generation
        ):
            return current
        pattern = f"manifest-{generation}-{manifest_digest[:20]}.json"
        prepared_path = self._root / "prepared" / pattern
        if not prepared_path.exists():
            raise WorkflowIntegrityError(
                "Session selected workflow manifest is neither current nor prepared"
            )
        prepared = self._manifest_from_payload(self._read_json(prepared_path))
        atomic_write_json(self.manifest_path, prepared.to_payload())
        prepared_path.unlink(missing_ok=True)
        return prepared

    def load(self, *, lineage: str | None = None) -> "MapTaskState":
        """验证 manifest、快照、段链并通过 reducer 回放完整状态。"""
        from app.orchestrator.map_progress import MapTaskState
        from app.orchestrator.map_workflow import reduce_map_workflow, reducer_write_scope

        manifest = self._read_manifest()
        if lineage is not None:
            self._assert_identity(manifest, lineage)
        snapshot = self._read_snapshot(manifest.snapshot_digest)
        if (
            snapshot.session_epoch != manifest.session_epoch
            or snapshot.lineage != manifest.lineage
            or snapshot.snapshot_seq > manifest.high_water_seq
        ):
            raise WorkflowIntegrityError("workflow snapshot identity is inconsistent")
        state = MapTaskState.from_dict(dict(snapshot.state))
        if state.workflow_high_water_seq != snapshot.snapshot_seq:
            raise WorkflowIntegrityError("workflow snapshot state high-water mismatch")
        expected_seq = snapshot.snapshot_seq + 1
        predecessor = snapshot.digest
        for digest in manifest.segment_digests:
            segment = self._read_segment(digest)
            if (
                segment.session_epoch != manifest.session_epoch
                or segment.lineage != manifest.lineage
                or segment.predecessor_digest != predecessor
                or segment.first_seq != expected_seq
            ):
                raise WorkflowIntegrityError("workflow segment chain is discontinuous")
            for event in segment.events:
                with reducer_write_scope():
                    state = reduce_map_workflow(state, event, stage_event=False)
            expected_seq = segment.last_seq + 1
            predecessor = segment.digest
        if state.workflow_high_water_seq != manifest.high_water_seq:
            raise WorkflowIntegrityError("workflow replay did not reach manifest high-water")
        state.pending_workflow_events.clear()
        return state

    def compact(self, state: "MapTaskState", *, lineage: str) -> WorkflowManifest:
        """验证完整快照、切换 manifest，之后清理已覆盖事件段。"""
        manifest = self._read_manifest()
        self._assert_identity(manifest, lineage)
        if state.pending_workflow_events:
            raise WorkflowIntegrityError("cannot compact unpublished workflow events")
        if state.workflow_high_water_seq != manifest.high_water_seq:
            raise WorkflowIntegrityError("cannot compact a stale workflow state")
        snapshot = WorkflowSnapshot.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            snapshot_seq=manifest.high_water_seq,
            state=state.to_dict(),
        )
        snapshot_path = self._write_snapshot(snapshot)
        verified = self._snapshot_from_payload(self._read_json(snapshot_path))
        if verified.digest != snapshot.digest:
            raise WorkflowIntegrityError("new workflow snapshot verification failed")
        compacted = WorkflowManifest.create(
            session_epoch=self._session_epoch,
            lineage=lineage,
            high_water_seq=manifest.high_water_seq,
            snapshot_digest=snapshot.digest,
            segment_digests=(),
            generation=manifest.generation + 1,
        )
        atomic_write_json(self.manifest_path, compacted.to_payload())
        for digest in manifest.segment_digests:
            try:
                self._find_content_file("segments", digest).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Covered workflow segment cleanup deferred digest=%s",
                    digest,
                    exc_info=True,
                )
        return compacted

    def _should_compact(self, manifest: WorkflowManifest) -> bool:
        """按已提交增量事件数或规范字节数判断是否应压缩。"""
        event_count = 0
        byte_count = 0
        for digest in manifest.segment_digests:
            path = self._find_content_file("segments", digest)
            segment = self._read_segment(digest)
            event_count += len(segment.events)
            byte_count += path.stat().st_size
        return (
            event_count >= self._snapshot_event_threshold
            or byte_count >= self._snapshot_byte_threshold
        )

    def _read_manifest(self) -> WorkflowManifest:
        """读取并验证当前 manifest。"""
        if not self.manifest_path.exists():
            raise WorkflowIntegrityError("workflow manifest is missing")
        return self._manifest_from_payload(self._read_json(self.manifest_path))

    def _assert_identity(self, manifest: WorkflowManifest, lineage: str) -> None:
        """验证 manifest 属于当前 Session epoch 与 lineage。"""
        if manifest.session_epoch != self._session_epoch or manifest.lineage != lineage:
            raise WorkflowIntegrityError("workflow manifest identity mismatch")

    def _write_snapshot(self, snapshot: WorkflowSnapshot) -> Path:
        """以内容寻址文件写入不可变快照。"""
        path = (
            self._root
            / "snapshots"
            / f"snapshot-{snapshot.snapshot_seq}-{snapshot.digest[:20]}.json"
        )
        self._write_immutable(path, snapshot.to_payload())
        return path

    def _write_segment(self, segment: WorkflowSegment, commit_id: str) -> Path:
        """以序列范围、提交身份与摘要写入不可变事件段。"""
        safe_commit = hashlib.sha256(commit_id.encode("utf-8")).hexdigest()[:16]
        path = (
            self._root
            / "segments"
            / (
                f"events-{segment.first_seq}-{segment.last_seq}-"
                f"{safe_commit}-{segment.digest[:20]}.json"
            )
        )
        self._write_immutable(path, segment.to_payload())
        return path

    def _write_immutable(self, path: Path, payload: Mapping[str, Any]) -> None:
        """创建不可变内容文件，已存在时只接受完全相同的规范内容。"""
        self._assert_confined(path)
        if path.exists():
            existing = self._read_json(path)
            if existing != dict(payload):
                raise WorkflowIntegrityError("immutable workflow file content conflict")
            return
        atomic_write_json(path, dict(payload))

    def _find_content_file(self, directory: str, digest: str) -> Path:
        """按完整摘要唯一定位一个 manifest 引用的内容文件。"""
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise WorkflowIntegrityError("workflow content digest is invalid")
        folder = self._root / directory
        matches = list(folder.glob(f"*-{digest[:20]}.json")) if folder.exists() else []
        if len(matches) != 1:
            raise WorkflowIntegrityError(
                f"workflow content locator count is {len(matches)} for digest={digest}"
            )
        self._assert_confined(matches[0])
        return matches[0]

    def _read_snapshot(self, digest: str) -> WorkflowSnapshot:
        """定位并验证 manifest 选中的快照。"""
        return self._snapshot_from_payload(
            self._read_json(self._find_content_file("snapshots", digest))
        )

    def _read_segment(self, digest: str) -> WorkflowSegment:
        """定位并验证 manifest 选中的事件段。"""
        return self._segment_from_payload(
            self._read_json(self._find_content_file("segments", digest))
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        """读取项目内 JSON 对象，损坏时转换为工作流完整性错误。"""
        self._assert_confined(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowIntegrityError("workflow JSON is unreadable") from exc
        if not isinstance(value, dict):
            raise WorkflowIntegrityError("workflow JSON root must be an object")
        return value

    def _assert_confined(self, path: Path) -> None:
        """拒绝访问工作流根目录之外的路径。"""
        try:
            path.resolve().relative_to(self._root.resolve())
        except ValueError as exc:
            raise WorkflowIntegrityError("workflow path escaped its root") from exc

    @staticmethod
    def _clear_pending_events(state: "MapTaskState") -> None:
        """在 reducer 写守卫内清空已由 manifest 发布的事务事件。"""
        from app.orchestrator.map_workflow import reducer_write_scope

        with reducer_write_scope():
            state.pending_workflow_events = []

    @staticmethod
    def _event_from_payload(value: Any) -> WorkflowEvent:
        """严格解析一个当前版规范事件。"""
        if not isinstance(value, dict) or set(value) != {
            "event_seq",
            "event_type",
            "target",
            "revision",
            "payload",
            "request_id",
            "turn_id",
        }:
            raise WorkflowIntegrityError("workflow event schema is unsupported")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise WorkflowIntegrityError("workflow event payload must be an object")
        try:
            return WorkflowEvent(
                event_seq=int(value["event_seq"]),
                event_type=str(value["event_type"]),
                target=str(value["target"]),
                revision=int(value["revision"]),
                payload=dict(payload),
                request_id=(
                    str(value["request_id"])
                    if value.get("request_id") is not None
                    else None
                ),
                turn_id=(
                    str(value["turn_id"])
                    if value.get("turn_id") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowIntegrityError("workflow event fields are invalid") from exc

    @classmethod
    def _snapshot_from_payload(cls, value: dict[str, Any]) -> WorkflowSnapshot:
        """严格解析快照并重新计算状态与文档摘要。"""
        state = value.get("state")
        if not isinstance(state, dict):
            raise WorkflowIntegrityError("workflow snapshot state is invalid")
        try:
            expected = WorkflowSnapshot.create(
                session_epoch=str(value["session_epoch"]),
                lineage=str(value["lineage"]),
                snapshot_seq=int(value["snapshot_seq"]),
                state=state,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowIntegrityError("workflow snapshot fields are invalid") from exc
        if value != expected.to_payload():
            raise WorkflowIntegrityError("workflow snapshot digest or schema mismatch")
        return expected

    @classmethod
    def _segment_from_payload(cls, value: dict[str, Any]) -> WorkflowSegment:
        """严格解析事件段并重新计算链内容摘要。"""
        raw_events = value.get("events")
        if not isinstance(raw_events, list):
            raise WorkflowIntegrityError("workflow segment events are invalid")
        events = tuple(cls._event_from_payload(item) for item in raw_events)
        try:
            expected = WorkflowSegment.create(
                session_epoch=str(value["session_epoch"]),
                lineage=str(value["lineage"]),
                predecessor_digest=(
                    str(value["predecessor_digest"])
                    if value.get("predecessor_digest") is not None
                    else None
                ),
                events=events,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowIntegrityError("workflow segment fields are invalid") from exc
        if value != expected.to_payload():
            raise WorkflowIntegrityError("workflow segment digest or schema mismatch")
        return expected

    @staticmethod
    def _manifest_from_payload(value: dict[str, Any]) -> WorkflowManifest:
        """严格解析 manifest 并重新计算其摘要。"""
        raw_digests = value.get("segment_digests")
        if not isinstance(raw_digests, list) or not all(
            isinstance(item, str) for item in raw_digests
        ):
            raise WorkflowIntegrityError("workflow manifest segment digests are invalid")
        try:
            expected = WorkflowManifest.create(
                session_epoch=str(value["session_epoch"]),
                lineage=str(value["lineage"]),
                high_water_seq=int(value["high_water_seq"]),
                snapshot_digest=str(value["snapshot_digest"]),
                segment_digests=tuple(raw_digests),
                generation=int(value["generation"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowIntegrityError("workflow manifest fields are invalid") from exc
        if value != expected.to_payload():
            raise WorkflowIntegrityError("workflow manifest digest or schema mismatch")
        return expected


__all__ = [
    "WORKFLOW_EVENT_SCHEMA_VERSION",
    "WORKFLOW_SCHEMA_EPOCH",
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowIntegrityError",
    "PreparedWorkflowPublication",
    "WorkflowPublication",
    "WorkflowStore",
]
