"""统一创建委派和自动恢复使用的子 Agent Frame。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from app.agents.types import AgentDefinition, Frame
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
    contract = dict(map_stage_contract or {})
    worker_instance_id = f"worker-instance:{session.session_id}:{frame_id}"
    result_schema = MAP_WORKER_RESULT_SCHEMA if contract else None
    stage = str(contract.get("stage", ""))
    allowed_next_stages = tuple(sorted(MAP_WORKER_NEXT_STAGES.get(stage, frozenset())))
    contract_payload = {
        "worker_instance_id": worker_instance_id,
        "stage": stage,
        "target_path": contract.get("target_path"),
        "map_revision": contract.get("map_revision"),
        "result_schema": result_schema,
        "allowed_next_stages": allowed_next_stages,
    }
    contract_id = (
        "frame-contract:"
        + hashlib.sha256(
            json.dumps(
                contract_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:24]
        if contract
        else None
    )
    if contract:
        contract.update(contract_payload)
        contract["contract_id"] = contract_id
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
        map_request_lineage_id=parent.map_request_lineage_id,
        map_task_id=parent.map_task_id,
        contract_id=contract_id,
        worker_instance_id=worker_instance_id if contract else None,
        result_schema=result_schema,
        allowed_next_stages=allowed_next_stages,
    )
