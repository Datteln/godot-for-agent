"""子 Frame 冻结合同的统一结果校验。"""

from __future__ import annotations

from typing import Any

from app.agents.types import Frame
from app.orchestrator.runtime_contracts import FrameContractViolation

def validate_frame_result(
    frame: Frame,
    payload: dict[str, Any],
) -> tuple[FrameContractViolation, ...]:
    """按创建时冻结的合同校验一个子 Frame 结构化结果。"""
    contract = frame.map_stage_contract
    if not contract:
        return ()
    violations: list[FrameContractViolation] = []

    def add(code: str, message: str, expected: Any, actual: Any) -> None:
        """追加一个字段级结构化违规。"""
        violations.append(
            FrameContractViolation(
                frame_id=frame.id,
                code=code,
                message=message,
                expected={"value": expected},
                actual={"value": actual},
            )
        )

    expected_contract_id = frame.contract_id or contract.get("contract_id")
    actual_contract_id = payload.get("contract_id")
    if actual_contract_id != expected_contract_id:
        add(
            "contract_id_mismatch",
            "result contract_id does not match the frozen Frame contract",
            expected_contract_id,
            actual_contract_id,
        )

    expected_worker = frame.worker_instance_id or contract.get("worker_instance_id")
    actual_worker = payload.get("worker")
    if actual_worker != expected_worker:
        add(
            "worker_instance_mismatch",
            "result worker does not identify the current Frame instance",
            expected_worker,
            actual_worker,
        )

    expected_schema = frame.result_schema or contract.get("result_schema")
    actual_schema = payload.get("result_schema")
    if actual_schema != expected_schema:
        add(
            "result_schema_mismatch",
            "result schema does not match the frozen Frame schema",
            expected_schema,
            actual_schema,
        )

    expected_stage = contract.get("stage")
    if payload.get("stage") != expected_stage:
        add(
            "stage_mismatch",
            "result stage does not match the frozen Frame stage",
            expected_stage,
            payload.get("stage"),
        )
    if payload.get("mode") != "partial":
        expected_target = contract.get("target_path")
        if expected_target and payload.get("target_path") != expected_target:
            add(
                "target_mismatch",
                "result target does not match the frozen Frame target",
                expected_target,
                payload.get("target_path"),
            )
        expected_revision = contract.get("map_revision")
        if (
            isinstance(expected_revision, int)
            and not isinstance(expected_revision, bool)
            and payload.get("map_revision") != expected_revision
        ):
            add(
                "revision_mismatch",
                "result revision does not match the frozen Frame revision",
                expected_revision,
                payload.get("map_revision"),
            )
    allowed_next = set(
        frame.allowed_next_stages
        or tuple(contract.get("allowed_next_stages", ()))
    )
    if payload.get("next_stage") not in allowed_next:
        add(
            "illegal_next_stage",
            "result next_stage is not allowed by the frozen Frame contract",
            sorted(allowed_next),
            payload.get("next_stage"),
        )
    return tuple(violations)
