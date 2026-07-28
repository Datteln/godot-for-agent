"""地图验收证据的所有权、可读性与作用域校验。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.agents.types import Frame
from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
)
from app.orchestrator.runtime_contracts import EvidenceReference


class EvidenceValidationError(ValueError):
    """表示证据无法成为 Completion Gate 的可信输入。"""


def register_screenshot_evidence(
    state: Any,
    frame: Frame,
    *,
    tool_use_id: str,
    result: dict[str, Any],
    artifact_refs: list[str],
    project_root: Path,
) -> EvidenceReference:
    """校验并登记当前 Frame 成功产生的截图证据。"""
    target = str(
        result.get(
            "target_path",
            frame.map_stage_contract.get("target_path", ""),
        )
    ).strip()
    revision = result.get(
        "map_revision",
        frame.map_stage_contract.get("map_revision"),
    )
    if not target:
        raise EvidenceValidationError("screenshot evidence target is missing")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise EvidenceValidationError("screenshot evidence revision is missing")
    contracted_target = frame.map_stage_contract.get("target_path")
    if contracted_target and target != contracted_target:
        raise EvidenceValidationError("screenshot evidence target does not match Frame")
    contracted_revision = frame.map_stage_contract.get("map_revision")
    if (
        isinstance(contracted_revision, int)
        and not isinstance(contracted_revision, bool)
        and revision != contracted_revision
    ):
        raise EvidenceValidationError("screenshot evidence revision does not match Frame")

    candidates = [
        *artifact_refs,
        str(result.get("path", "")),
        str(result.get("absolute_path", "")),
    ]
    readable_path: Path | None = None
    selected_ref: str | None = None
    for candidate in candidates:
        path = _resolve_evidence_path(candidate, project_root)
        if path is not None:
            readable_path = path
            selected_ref = candidate
            break
    if readable_path is None:
        raise EvidenceValidationError("screenshot artifact is not readable by the service")
    try:
        digest = hashlib.sha256(readable_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceValidationError("screenshot artifact read failed") from exc

    contract_id = frame.contract_id or str(
        frame.map_stage_contract.get("contract_id", "")
    )
    if not contract_id:
        raise EvidenceValidationError("screenshot Frame has no frozen contract id")
    evidence_id = (
        "evidence:"
        + hashlib.sha256(
            f"{contract_id}:{tool_use_id}:{target}:{revision}:{digest}".encode("utf-8")
        ).hexdigest()[:24]
    )
    evidence = EvidenceReference(
        evidence_id=evidence_id,
        evidence_type="viewport_screenshot",
        target=target,
        revision=revision,
        contract_id=contract_id,
        artifact_ref=selected_ref,
        digest=digest,
        metadata={
            "frame_id": frame.id,
            "worker_instance_id": frame.worker_instance_id,
            "tool_use_id": tool_use_id,
            "status": "applied",
            "region": (
                dict(result["region"])
                if isinstance(result.get("region"), dict)
                else {}
            ),
            "absolute_path": str(readable_path),
        },
    )
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "evidence_recorded",
            target,
            revision,
            {
                "evidence_id": evidence_id,
                "evidence": evidence.to_dict(),
            },
        ),
    )
    return evidence


def _resolve_evidence_path(value: str, project_root: Path) -> Path | None:
    """把 res/相对/绝对引用解析为存在的普通文件。"""
    candidate = value.strip()
    if not candidate or candidate.startswith("user://"):
        return None
    path = (
        project_root / candidate.removeprefix("res://").lstrip("/\\")
        if candidate.startswith("res://")
        else Path(candidate)
    )
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve()
        if not resolved.exists() or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


def scoped_evidence(
    state: Any,
    target: str,
    revision: int,
    evidence_type: str | None = None,
) -> list[dict[str, Any]]:
    """返回同目标/revision 且结构完整的登记证据。"""
    return [
        dict(item)
        for item in state.evidence_registry.values()
        if isinstance(item, dict)
        and item.get("target") == target
        and item.get("revision") == revision
        and (evidence_type is None or item.get("evidence_type") == evidence_type)
        and isinstance(item.get("metadata"), dict)
        and item["metadata"].get("status") == "applied"
    ]
