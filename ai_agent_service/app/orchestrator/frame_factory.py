"""统一创建委派和自动恢复使用的子 Agent Frame。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from app.agents.types import AgentDefinition, Frame
from app.orchestrator.frame_contract_types import (
    FRAME_CONTRACT_VERSION,
    MAP_WORKER_STAGE_CONTRACT_KIND,
    DomainOwnerContract,
    FrameContractTypeError,
    MapWorkerStageContract,
)
from app.orchestrator.map_contracts import (
    MAP_WORKER_NEXT_STAGES,
    MAP_WORKER_RESULT_SCHEMA,
)

if TYPE_CHECKING:
    from app.sessions.store import Session


def typed_child_task_text(
    objective: str,
    inputs: dict[str, Any],
    artifact_refs: list[str] | None = None,
) -> str:
    """生成只含目标、类型化输入和 artifact 引用的自动子任务载荷。"""
    return json.dumps(
        {
            "objective": objective,
            "inputs": inputs,
            "artifact_refs": artifact_refs or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def create_child_frame(
    *,
    session: Session,
    parent: Frame,
    agent: AgentDefinition,
    task_text: str,
    depth: int,
    pending_delegate_call_id: str | None = None,
    pending_delegate_group_id: str | None = None,
    map_stage_contract: dict[str, Any] | None = None,
) -> Frame:
    """按统一消息、历史锚点和生命周期字段创建子 Frame。"""
    history_anchor_frame_id = parent.id
    history_anchor_message_index: int | None = len(parent.messages)
    if parent.history_anchor_frame_id is not None:
        history_anchor_frame_id = parent.history_anchor_frame_id
        history_anchor_message_index = parent.history_anchor_message_index
    frame_id = session.new_frame_id()
    raw_contract = dict(map_stage_contract or {})
    worker_contract: MapWorkerStageContract | None = None
    if raw_contract:
        try:
            worker_contract = MapWorkerStageContract.from_dict(raw_contract)
        except FrameContractTypeError as exc:
            raise ValueError(f"invalid map worker stage contract: {exc}") from exc
    worker_instance_id: str | None = None
    contract_id: str | None = None
    result_schema: str | None = None
    allowed_next_stages: tuple[str, ...] = ()
    if worker_contract is not None:
        worker_instance_id = f"worker-instance:{session.session_id}:{frame_id}"
        identity_payload = {
            "contract_kind": MAP_WORKER_STAGE_CONTRACT_KIND,
            "contract_version": FRAME_CONTRACT_VERSION,
            "worker_instance_id": worker_instance_id,
            "stage": worker_contract.stage,
            "target_path": worker_contract.target_path,
            "map_revision": worker_contract.map_revision,
            "snapshot_id": (
                worker_contract.authoritative_snapshot.get("snapshot_id")
                if worker_contract.authoritative_snapshot is not None
                else None
            ),
            "snapshot_digest": (
                worker_contract.authoritative_snapshot.get("digest")
                if worker_contract.authoritative_snapshot is not None
                else None
            ),
            "planning_context_bundle_id": (
                worker_contract.planning_context_bundle.bundle_id
                if worker_contract.planning_context_bundle is not None
                else None
            ),
            "planning_context_digests": (
                [entry.digest for entry in worker_contract.planning_context_bundle.contexts]
                if worker_contract.planning_context_bundle is not None
                else []
            ),
            "execution_operation_ids": [
                operation.operation_id for operation in worker_contract.execution_operations
            ],
            "result_schema": MAP_WORKER_RESULT_SCHEMA,
            "allowed_next_stages": sorted(MAP_WORKER_NEXT_STAGES[worker_contract.stage]),
        }
        contract_id = (
            "frame-contract:"
            + hashlib.sha256(
                json.dumps(
                    identity_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        worker_contract = worker_contract.bind_runtime_identity(
            contract_id=contract_id,
            worker_instance_id=worker_instance_id,
        )
        result_schema = MAP_WORKER_RESULT_SCHEMA
        allowed_next_stages = worker_contract.allowed_next_stages
    contract = worker_contract.to_dict() if worker_contract is not None else {}
    owner_contract = (
        DomainOwnerContract(
            domain="map",
            owner_frame_id=frame_id,
            parent_frame_id=parent.id,
            durable_task_id=parent.map_task_id,
            request_lineage_id=parent.map_request_lineage_id,
        ).to_dict()
        if agent.role == "map_orchestrator" and agent.map_stage == "orchestrator"
        else {}
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": agent.prompt}]
    if contract:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Runtime Map Stage Contract（运行时唯一合同，优先于一般性说明）：\n"
                    + json.dumps(contract, ensure_ascii=False, sort_keys=True)
                ),
            }
        )
    messages.append({"role": "user", "content": task_text})
    return Frame(
        id=frame_id,
        agent=agent,
        messages=messages,
        parent_id=parent.id,
        pending_delegate_call_id=pending_delegate_call_id,
        pending_delegate_group_id=pending_delegate_group_id,
        depth=depth,
        history_anchor_frame_id=history_anchor_frame_id,
        history_anchor_message_index=history_anchor_message_index,
        map_stage_contract=contract,
        domain_owner_contract=owner_contract,
        map_request_lineage_id=parent.map_request_lineage_id,
        map_task_id=parent.map_task_id,
        contract_id=contract_id,
        worker_instance_id=worker_instance_id,
        result_schema=result_schema,
        allowed_next_stages=allowed_next_stages,
    )
