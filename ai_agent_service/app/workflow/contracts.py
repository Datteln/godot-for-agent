"""Canonical current-epoch workflow manifest, snapshot, segment, and event models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Mapping

WORKFLOW_SCHEMA_VERSION: Final = 1
WORKFLOW_EVENT_SCHEMA_VERSION: Final = 1
WORKFLOW_SCHEMA_EPOCH: Final = "workflow-manifest-v1"


class WorkflowIntegrityError(ValueError):
    """Current-schema workflow bytes do not satisfy the declared integrity contract."""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a JSON object deterministically for content addressing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    event_seq: int
    event_type: str
    target: str
    revision: int
    payload: Mapping[str, Any]
    request_id: str | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_seq < 1:
            raise WorkflowIntegrityError("workflow event_seq must be positive")
        if not self.event_type.strip() or not self.target.strip() or self.revision < 0:
            raise WorkflowIntegrityError("workflow event identity is invalid")

    @property
    def event_id(self) -> str:
        """返回由完整规范事件内容派生的稳定身份。"""
        return f"mwe-{content_digest(self.to_payload())[:20]}"

    def to_payload(self) -> dict[str, Any]:
        """返回用于持久化与内容寻址的规范载荷。"""
        return {
            "event_seq": self.event_seq,
            "event_type": self.event_type,
            "target": self.target,
            "revision": self.revision,
            "payload": dict(self.payload),
            "request_id": self.request_id,
            "turn_id": self.turn_id,
        }

    def to_dict(self) -> dict[str, Any]:
        """返回规范事件字典。"""
        return self.to_payload()


@dataclass(frozen=True, slots=True)
class WorkflowSegment:
    schema_version: int
    event_schema_version: int
    schema_epoch: str
    session_epoch: str
    lineage: str
    first_seq: int
    last_seq: int
    predecessor_digest: str | None
    events: tuple[WorkflowEvent, ...]
    digest: str

    @classmethod
    def create(
        cls,
        *,
        session_epoch: str,
        lineage: str,
        predecessor_digest: str | None,
        events: tuple[WorkflowEvent, ...],
    ) -> "WorkflowSegment":
        if not events:
            raise WorkflowIntegrityError("workflow segment cannot be empty")
        expected = list(range(events[0].event_seq, events[-1].event_seq + 1))
        if [item.event_seq for item in events] != expected:
            raise WorkflowIntegrityError("workflow segment sequence must be contiguous")
        unsigned = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
            "schema_epoch": WORKFLOW_SCHEMA_EPOCH,
            "session_epoch": session_epoch,
            "lineage": lineage,
            "first_seq": events[0].event_seq,
            "last_seq": events[-1].event_seq,
            "predecessor_digest": predecessor_digest,
            "events": [item.to_payload() for item in events],
        }
        return cls(
            schema_version=WORKFLOW_SCHEMA_VERSION,
            event_schema_version=WORKFLOW_EVENT_SCHEMA_VERSION,
            schema_epoch=WORKFLOW_SCHEMA_EPOCH,
            session_epoch=session_epoch,
            lineage=lineage,
            first_seq=events[0].event_seq,
            last_seq=events[-1].event_seq,
            predecessor_digest=predecessor_digest,
            events=events,
            digest=content_digest(unsigned),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_schema_version": self.event_schema_version,
            "schema_epoch": self.schema_epoch,
            "session_epoch": self.session_epoch,
            "lineage": self.lineage,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "predecessor_digest": self.predecessor_digest,
            "events": [item.to_payload() for item in self.events],
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    schema_version: int
    event_schema_version: int
    schema_epoch: str
    session_epoch: str
    lineage: str
    snapshot_seq: int
    state: Mapping[str, Any]
    state_digest: str
    digest: str

    @classmethod
    def create(
        cls,
        *,
        session_epoch: str,
        lineage: str,
        snapshot_seq: int,
        state: Mapping[str, Any],
    ) -> "WorkflowSnapshot":
        if snapshot_seq < 0:
            raise WorkflowIntegrityError("snapshot_seq must be non-negative")
        state_copy = dict(state)
        state_digest = content_digest(state_copy)
        unsigned = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
            "schema_epoch": WORKFLOW_SCHEMA_EPOCH,
            "session_epoch": session_epoch,
            "lineage": lineage,
            "snapshot_seq": snapshot_seq,
            "state": state_copy,
            "state_digest": state_digest,
        }
        return cls(
            schema_version=WORKFLOW_SCHEMA_VERSION,
            event_schema_version=WORKFLOW_EVENT_SCHEMA_VERSION,
            schema_epoch=WORKFLOW_SCHEMA_EPOCH,
            session_epoch=session_epoch,
            lineage=lineage,
            snapshot_seq=snapshot_seq,
            state=state_copy,
            state_digest=state_digest,
            digest=content_digest(unsigned),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_schema_version": self.event_schema_version,
            "schema_epoch": self.schema_epoch,
            "session_epoch": self.session_epoch,
            "lineage": self.lineage,
            "snapshot_seq": self.snapshot_seq,
            "state": dict(self.state),
            "state_digest": self.state_digest,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class WorkflowManifest:
    schema_version: int
    schema_epoch: str
    session_epoch: str
    lineage: str
    high_water_seq: int
    snapshot_digest: str
    segment_digests: tuple[str, ...]
    generation: int
    digest: str

    @classmethod
    def create(
        cls,
        *,
        session_epoch: str,
        lineage: str,
        high_water_seq: int,
        snapshot_digest: str,
        segment_digests: tuple[str, ...],
        generation: int,
    ) -> "WorkflowManifest":
        if high_water_seq < 0 or generation < 1:
            raise WorkflowIntegrityError("workflow manifest counters are invalid")
        if not session_epoch or not lineage or not snapshot_digest:
            raise WorkflowIntegrityError("workflow manifest identity is incomplete")
        unsigned = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "schema_epoch": WORKFLOW_SCHEMA_EPOCH,
            "session_epoch": session_epoch,
            "lineage": lineage,
            "high_water_seq": high_water_seq,
            "snapshot_digest": snapshot_digest,
            "segment_digests": list(segment_digests),
            "generation": generation,
        }
        return cls(
            schema_version=WORKFLOW_SCHEMA_VERSION,
            schema_epoch=WORKFLOW_SCHEMA_EPOCH,
            session_epoch=session_epoch,
            lineage=lineage,
            high_water_seq=high_water_seq,
            snapshot_digest=snapshot_digest,
            segment_digests=segment_digests,
            generation=generation,
            digest=content_digest(unsigned),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "schema_epoch": self.schema_epoch,
            "session_epoch": self.session_epoch,
            "lineage": self.lineage,
            "high_water_seq": self.high_water_seq,
            "snapshot_digest": self.snapshot_digest,
            "segment_digests": list(self.segment_digests),
            "generation": self.generation,
            "digest": self.digest,
        }
