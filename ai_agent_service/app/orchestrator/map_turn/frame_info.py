"""提供 Map Frame 的只读身份、阶段与预算描述。"""

from __future__ import annotations

import json
from typing import Any

from app.agents.types import Frame
from app.orchestrator.frame_contract_types import (
    FrameContractTypeError,
    MapWorkerStageContract,
)
from app.orchestrator.map_contracts import (
    MAP_WORKER_RESULT_SCHEMA,
    MAP_WORKER_STAGES,
)
from app.orchestrator.map_turn.structured_contracts import (
    MAP_OUTPUT_SCHEMA_V1,
    MAP_WORKER_STAGE_NAMES,
)
from app.sessions.store import Session


def _find_frame(session: Session, frame_id: str) -> Frame | None:
    """按 frame id 查找当前会话里的帧。"""
    for frame in session.agent_stack:
        if frame.id == frame_id:
            return frame
    return None


def _frame_in_active_map_edit(session: Session, frame: Frame | None) -> bool:
    """Return whether a Frame belongs to the current authorized map-edit request."""
    scope = session.map_request_scope
    return (
        frame is not None
        and scope.activates_map_gate
        and scope.map_task_id == session.map_task_state.task_id
        and frame.map_request_lineage_id == scope.lineage_id
        and frame.map_task_id == scope.map_task_id
    )


def _map_output_schema_for_frame(frame: Frame) -> str | None:
    """解析当前地图 frame 需要执行的结构化输出 schema。

    只有通过封闭 ``MapWorkerStageContract`` 构造的 worker Frame 才能获得
    worker 输出 schema。Agent 元数据或任意非空 dict 均不足以授予该权限。
    """
    try:
        contract = MapWorkerStageContract.from_dict(frame.map_stage_contract)
    except FrameContractTypeError:
        return None
    return (
        MAP_OUTPUT_SCHEMA_V1
        if contract.result_schema == MAP_WORKER_RESULT_SCHEMA
        and frame.result_schema == MAP_WORKER_RESULT_SCHEMA
        and contract.stage in MAP_WORKER_STAGES
        else None
    )


def _map_stage_for_frame(frame: Frame) -> str:
    """根据地图 agent/frame 名称推断结构化收尾阶段。

    本轮整改：优先级从「名称硬编码」改为「合同 → agent 元数据 → reader 兜底」，
    删除了对 agent name / prompt 文本的字符串匹配。
    """
    # 1. 合同优先：子帧创建时由 _map_stage_contract 注入
    contracted_stage = frame.map_stage_contract.get("stage")
    if isinstance(contracted_stage, str) and contracted_stage in MAP_WORKER_STAGE_NAMES:
        return contracted_stage
    # 2. 静态 agent 元数据兜底
    if frame.agent.map_stage in MAP_WORKER_STAGE_NAMES:
        return str(frame.agent.map_stage)
    return "reader"


def _frame_objective(frame: Frame) -> str:
    """取子帧第一条用户消息作为 objective。"""
    for message in frame.messages:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return frame.agent.description or frame.agent.name


def _frame_semantic_operation(frame: Frame) -> dict[str, Any]:
    """提取不含 Frame/调用 id 的稳定语义操作身份，同时区分并行目标。"""
    objective_text = _frame_objective(frame)
    objective: Any = objective_text
    try:
        parsed = json.loads(objective_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        objective = parsed.get("objective", objective_text)
        inputs = parsed.get("inputs")
        if isinstance(inputs, dict):
            objective = {
                "objective": objective,
                "inputs": {
                    str(key): value
                    for key, value in inputs.items()
                    if str(key)
                    not in {
                        "artifact_ref",
                        "artifact_refs",
                        "call_id",
                        "frame_id",
                        "request_id",
                        "tool_use_id",
                    }
                },
            }
    return {
        "worker_mode": frame.agent.worker_mode,
        "stage": _map_stage_for_frame(frame),
        "objective": objective,
        "output_schema": _map_output_schema_for_frame(frame),
    }


def _map_frame_exhausted_payload(frame: Frame, limit_label: str, limit: int) -> str:
    """为地图子帧预算耗尽生成合法的部分结果 JSON。"""
    issue = f"子 agent 达到自身{limit_label}上限（{limit}），已返回部分读取/执行结果。"
    revision = frame.map_stage_contract.get("map_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = 0
    allowed_next = frame.allowed_next_stages or tuple(
        frame.map_stage_contract.get("allowed_next_stages", ())
    )
    payload = {
        "contract_id": frame.contract_id or "",
        "result_schema": frame.result_schema or MAP_OUTPUT_SCHEMA_V1,
        "stage": _map_stage_for_frame(frame),
        "worker": frame.worker_instance_id or frame.agent.name,
        "mode": "partial",
        "objective": _frame_objective(frame),
        "target_path": str(frame.map_stage_contract.get("target_path") or ""),
        "map_layer": 0,
        "map_revision": revision,
        "region": {},
        "summary": issue,
        "facts": [],
        "proposed_batches": [],
        "write_results": [],
        "validation": {
            "passed": False,
            "completion_allowed": False,
            "issues": [issue],
            "structured_issues": [
                {
                    "code": "frame_turns_exhausted",
                    "limit_label": limit_label,
                    "limit": limit,
                    "agent": frame.agent.name,
                }
            ],
        },
        "missing_inputs": [
            "需要父 agent 基于已返回的工具结果继续拆分任务，或用更具体的 target_path/map_layer/region 重新委派。"
        ],
        "risks": ["本子阶段未完整收敛，不能作为完成依据。"],
        "next_stage": (
            "replan"
            if "replan" in allowed_next
            else (allowed_next[0] if allowed_next else _map_stage_for_frame(frame))
        ),
    }
    return json.dumps(payload, ensure_ascii=False)
