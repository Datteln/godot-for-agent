"""Map tool-result projection and deterministic batch continuation."""

from __future__ import annotations

import json
from typing import Any

from app.agents.types import Frame
from app.api.schemas import ChatToolCallsResponse, FrontToolCallDTO
from app.application.response_policy import _planner_completion_text
from app.orchestrator.map_contracts import MapResponseMode, arm_map_worker_structured_completion
from app.orchestrator.map_progress import (
    consume_committed_platform_approvals,
    latest_map_revision,
)
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES
from app.orchestrator.map_workflow import increment_map_counter, replace_map_state_field
from app.sessions.store import Session


def _is_dynamic_map_writer(frame: Frame) -> bool:
    """判断当前帧是否为一次性地图写入 worker。"""
    return bool(frame.agent.workflow_operations) and frame.agent.edit_map_max_turns is not None

def _writer_platform_validation_failure_text(
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """把写入前平台校验失败转换为确定性的 Writer 阶段结果。"""
    payload_value = json.loads(_planner_completion_text(frame, tool_name, tool_args, result))
    payload = payload_value if isinstance(payload_value, dict) else {}
    payload.update(
        {
            "stage": "writer",
            "worker": frame.agent.name,
            "mode": "partial",
            "summary": (
                "平台写入前校验未通过；Writer 未执行 edit_map，" "已停止当前写入帧并返回 planner。"
            ),
            "proposed_batches": [],
            "write_results": [],
            "next_stage": "planner",
        }
    )
    return json.dumps(payload, ensure_ascii=False)

def _map_reader_has_detailed_region(result: dict[str, Any]) -> bool:
    """判断 reader 是否已拿到足以进入事实汇总阶段的精确区域。"""
    if result.get("ok") is False:
        return False
    if result.get("cells_format") not in {"non_empty_only", "full"}:
        return False
    revision = result.get("map_revision")
    return (
        bool(result.get("target_path") or result.get("target"))
        and isinstance(revision, int)
        and not isinstance(revision, bool)
    )

def _arm_map_reader_text_completion(
    frame: Frame,
    *,
    mode: MapResponseMode = "prompt_only",
    correction_limit: int = 2,
) -> None:
    """把 reader 的下一轮限制为无工具的结构化事实输出。"""
    if frame.force_text_only:
        return
    arm_map_worker_structured_completion(
        frame,
        mode=mode,
        correction_limit=correction_limit,
    )

def _map_batch_postcondition_error(
    status: str,
    tool_args: dict[str, Any],
    result: Any,
) -> str | None:
    """本地检查批次结果，失败时阻止释放后续批次。"""
    if status != "applied" or not isinstance(result, dict) or result.get("ok") is False:
        return "batch tool did not apply successfully"
    batch_id = str(tool_args.get("write_batch_id", ""))
    if batch_id and result.get("write_batch_id") != batch_id:
        return "write_batch_id mismatch"
    transaction_id = str(tool_args.get("map_transaction_id", ""))
    if transaction_id and result.get("map_transaction_id") != transaction_id:
        return "map_transaction_id mismatch"
    expected_revision = tool_args.get("expected_revision")
    actual_revision = result.get("map_revision")
    if (
        isinstance(expected_revision, int)
        and not isinstance(expected_revision, bool)
        and actual_revision != expected_revision + 1
    ):
        return "map revision did not advance exactly once"
    expected_cells = tool_args.get("expected_cells")
    if (
        isinstance(expected_cells, int)
        and not isinstance(expected_cells, bool)
        and isinstance(result.get("cells"), int)
        and result.get("cells") != expected_cells
    ):
        return "expected_cells postcondition failed"
    postconditions = tool_args.get("postconditions")
    if isinstance(postconditions, dict):
        for key, expected in postconditions.items():
            if result.get(key) != expected:
                return f"postcondition {key}={expected!r} failed"
    return None

def _remember_map_batch_result(
    session: Session,
    tool_name: str,
    status: str,
    tool_args: dict[str, Any],
    result: Any,
) -> None:
    """记录确定性批次结果，并在局部校验失败时清空后续队列。"""
    if tool_name not in MAP_REVISION_GUARDED_TOOL_NAMES or "plan_version" not in tool_args:
        return
    state = session.map_task_state
    error = _map_batch_postcondition_error(status, tool_args, result)
    entry = {
        "plan_version": tool_args.get("plan_version"),
        "batch_index": tool_args.get("batch_index"),
        "write_batch_id": tool_args.get("write_batch_id"),
        "tool": tool_name,
        "result": result if isinstance(result, dict) else {},
        "postconditions_passed": error is None,
        "error": error,
        "map_transaction_id": tool_args.get("map_transaction_id"),
    }
    replace_map_state_field(
        state,
        "executed_batches",
        [*state.executed_batches, entry],
        target=str(tool_args.get("target_path", "")) or None,
        revision=(
            result.get("map_revision")
            if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
            else None
        ),
    )
    increment_map_counter(state, "executed_batches")
    transaction_id = str(tool_args.get("map_transaction_id", "")).strip()
    if transaction_id:
        journals = list(state.transaction_journals)
        matched = next(
            (item for item in journals if item.get("transaction_id") == transaction_id),
            None,
        )
        transaction_entry = (
            dict(matched)
            if isinstance(matched, dict)
            else {
                "transaction_id": transaction_id,
                "target": str(tool_args.get("target_path", "")),
                "base_revision": tool_args.get("map_transaction_base_revision"),
                "operation_ids": [],
            }
        )
        operation_ids = list(transaction_entry.get("operation_ids", []))
        batch_id = str(tool_args.get("write_batch_id", ""))
        if batch_id and batch_id not in operation_ids:
            operation_ids.append(batch_id)
        approval_records = list(transaction_entry.get("approval_records", []))
        approval_id = str(tool_args.get("approval_id", "")).strip()
        approval_fingerprint = str(tool_args.get("approval_batch_fingerprint", "")).strip()
        approval_expected_revision = tool_args.get("approval_expected_revision")
        if (
            approval_id
            and approval_fingerprint
            and isinstance(approval_expected_revision, int)
            and not isinstance(approval_expected_revision, bool)
            and not any(
                record.get("approval_id") == approval_id
                for record in approval_records
                if isinstance(record, dict)
            )
        ):
            approval_records.append(
                {
                    "approval_id": approval_id,
                    "batch_fingerprint": approval_fingerprint,
                    "expected_revision": approval_expected_revision,
                }
            )
        transaction_entry.update(
            {
                "operation_ids": operation_ids,
                "approval_records": approval_records,
                "final_revision": (
                    result.get("map_revision") if isinstance(result, dict) else None
                ),
                "status": "prepared" if error is None else "rolled_back",
                "error": error,
            }
        )
        journals = [item for item in journals if item.get("transaction_id") != transaction_id]
        journals.append(transaction_entry)
        replace_map_state_field(
            state,
            "transaction_journals",
            journals,
            target=str(tool_args.get("target_path", "")) or None,
            revision=(
                result.get("map_revision")
                if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
                else None
            ),
        )
    if error is None:
        if state.pending_batches:
            first = state.pending_batches[0]
            first_input = first.get("input", {}) if isinstance(first, dict) else {}
            if isinstance(first_input, dict) and first_input.get("write_batch_id") == tool_args.get(
                "write_batch_id"
            ):
                replace_map_state_field(
                    state,
                    "pending_batches",
                    state.pending_batches[1:],
                    target=str(tool_args.get("target_path", "")) or None,
                    revision=(
                        result.get("map_revision")
                        if isinstance(result, dict) and isinstance(result.get("map_revision"), int)
                        else None
                    ),
                )
        increment_map_counter(state, "writes")
        # 通过 transition_stage 推进阶段，内部会触发派生状态（如缓存）的失效
        state.transition_stage("validate" if not state.pending_batches else "write")
        if not state.pending_batches and session.latest_context_used_tokens >= 32_000:
            session.force_compact_next_turn = True
        return
    increment_map_counter(state, "failed_batches")
    approval_expected_revision = tool_args.get("approval_expected_revision")
    current_revision = latest_map_revision(
        session,
        str(tool_args.get("target_path", "")),
        (
            tool_args.get("map_layer")
            if isinstance(tool_args.get("map_layer"), int)
            and not isinstance(tool_args.get("map_layer"), bool)
            else None
        ),
    )
    if (
        not str(tool_args.get("approval_id", "")).strip()
        or approval_expected_revision != current_revision
    ):
        replace_map_state_field(state, "pending_batches", [])
    # 批次失败时回退到 plan 阶段，重新规划
    state.transition_stage("plan")
    replace_map_state_field(state, "unresolved_issues", [error])

def _remember_map_transaction_validation(
    session: Session,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    successful: bool,
) -> None:
    """把 Godot 端验证后的 write-group 终态镜像到 Session。"""
    transaction_id = str(tool_args.get("map_transaction_id", "")).strip()
    if not transaction_id:
        return
    journals = list(session.map_task_state.transaction_journals)
    matched = next(
        (item for item in journals if item.get("transaction_id") == transaction_id),
        None,
    )
    if not isinstance(matched, dict):
        return
    updated = dict(matched)
    frontend_status = str(result.get("map_transaction_status", "")).strip()
    updated["status"] = (
        frontend_status
        if frontend_status in {"committed", "rolled_back", "failed"}
        else ("committed" if successful else "rolled_back")
    )
    updated["final_revision"] = result.get("map_revision", updated.get("final_revision"))
    updated["validation_tool"] = result.get("validation_tool")
    updated["error"] = None if successful else str(result.get("message", "map validation failed"))
    if successful and updated["status"] == "committed":
        consume_committed_platform_approvals(session, result, updated)
    journals = [item for item in journals if item.get("transaction_id") != transaction_id]
    journals.append(updated)
    replace_map_state_field(
        session.map_task_state,
        "transaction_journals",
        journals,
        target=str(updated.get("target", "")) or None,
        revision=(
            updated.get("final_revision")
            if isinstance(updated.get("final_revision"), int)
            else None
        ),
    )

def _resume_map_batch_queue(session: Session) -> ChatToolCallsResponse | None:
    """在上一批成功后直接下发下一批，不再唤醒 LLM。"""
    state = session.map_task_state
    if not state.pending_batches or state.unresolved_issues:
        return None
    raw = state.pending_batches[0]
    input_args = raw.get("input", {})
    if not isinstance(input_args, dict):
        replace_map_state_field(state, "pending_batches", [])
        return None
    target = str(input_args.get("target_path", ""))
    # 根据 map_layer 查询最新修订号，确保批次下发时携带正确的 expected_revision
    layer = input_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    latest_revision = latest_map_revision(session, target, map_layer)
    if latest_revision is not None:
        input_args["expected_revision"] = latest_revision
    call = FrontToolCallDTO(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        input=input_args,
        needs_confirm=bool(raw.get("needs_confirm", False)),
        frame_id=str(raw.get("frame_id", "")),
        agent=str(raw.get("agent", "map-agent")),
        render_kind=(str(raw["render_kind"]) if raw.get("render_kind") is not None else None),
    )
    frame = next((item for item in session.agent_stack if item.id == call.frame_id), None)
    if frame is None:
        replace_map_state_field(state, "pending_batches", [])
        return None
    frame.messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    turn_id = session.new_turn_id()
    session.set_pending(
        turn_id,
        [call.id],
        {
            call.id: {
                "name": call.name,
                "input": call.input,
                "frame_id": call.frame_id,
                "agent": call.agent,
                "needs_confirm": call.needs_confirm,
            }
        },
    )
    return ChatToolCallsResponse(turn_id=turn_id, text=None, calls=[call])
