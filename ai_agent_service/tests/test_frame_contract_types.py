"""版本化 owner/worker Frame 合同的封闭序列化测试。"""

from __future__ import annotations

import pytest

from app.orchestrator.frame_contract_types import (
    DomainOwnerContract,
    FrameContractTypeError,
    MapWorkerStageContract,
)


def test_domain_owner_contract_round_trip() -> None:
    contract = DomainOwnerContract(
        domain="map",
        owner_frame_id="f2",
        parent_frame_id="f1",
        macro_step_id="map-step",
        domain_task_id="map-step:epoch-1",
        durable_task_id="task-1",
        request_lineage_id="lineage-1",
    )
    assert DomainOwnerContract.from_dict(contract.to_dict()) == contract


def test_map_worker_contract_round_trip() -> None:
    contract = MapWorkerStageContract(
        stage="planner",
        target_path="Map/Main",
        map_revision=7,
        authoritative_snapshot={"snapshot_id": "snapshot-1"},
    ).bind_runtime_identity(
        contract_id="contract-1",
        worker_instance_id="worker-1",
    )
    assert MapWorkerStageContract.from_dict(contract.to_dict()) == contract


def test_owner_rejects_worker_only_fields() -> None:
    payload = DomainOwnerContract(
        domain="map",
        owner_frame_id="f2",
        parent_frame_id="f1",
    ).to_dict()
    payload["result_schema"] = "map_worker_result_v1"
    with pytest.raises(FrameContractTypeError):
        DomainOwnerContract.from_dict(payload)


def test_worker_rejects_owner_only_fields_and_unknown_stage() -> None:
    with pytest.raises(FrameContractTypeError):
        MapWorkerStageContract.from_dict({"stage": "planner", "owner_frame_id": "f2"})
    with pytest.raises(FrameContractTypeError):
        MapWorkerStageContract.from_dict({"stage": "orchestrator"})
