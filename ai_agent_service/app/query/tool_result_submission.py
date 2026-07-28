"""前端工具结果批次的纯预检与类型化输出。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.api.schemas import ToolResult
from app.sessions.store import Session
from app.tools.registry import ToolDef

_VALID_RESULT_STATUSES = frozenset({"applied", "rejected", "error"})


class ToolResultBatchValidationError(ValueError):
    """表示工具结果批次在任何状态变更之前未通过协议预检。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与面向调用方的错误消息。"""
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedToolResult:
    """保存一条已与 pending 元数据、Frame 和工具合同绑定的结果。"""

    result: ToolResult
    metadata: Mapping[str, Any]
    tool: ToolDef


@dataclass(frozen=True)
class ValidatedToolResultBatch:
    """保存整批预检成功后的不可变执行输入。"""

    turn_id: str
    items: tuple[ValidatedToolResult, ...]


def validate_tool_result_batch(
    session: Session,
    results: Sequence[ToolResult],
    tools: Mapping[str, ToolDef],
) -> ValidatedToolResultBatch:
    """纯校验完整工具结果批次，不写入 Session、缓存、消息或外部存储。"""
    if session.pending_turn_id is None:
        raise ToolResultBatchValidationError(
            "no_pending_turn",
            "当前会话没有等待回传的工具调用",
        )
    if not results:
        raise ToolResultBatchValidationError(
            "empty_batch",
            "tool_results 不能为空",
        )

    ids = [result.tool_use_id for result in results]
    if len(set(ids)) != len(ids):
        raise ToolResultBatchValidationError(
            "duplicate_tool_use_id",
            "tool_results 不能包含重复的 tool_use_id",
        )
    if set(ids) != session.pending_tool_call_ids:
        expected = ", ".join(sorted(session.pending_tool_call_ids))
        actual = ", ".join(sorted(ids))
        raise ToolResultBatchValidationError(
            "pending_id_mismatch",
            f"tool_results 与 pending 工具调用不匹配：expected={expected}; actual={actual}",
        )

    frames = {frame.id: frame for frame in session.agent_stack}
    validated: list[ValidatedToolResult] = []
    for result in results:
        if result.turn_id != session.pending_turn_id:
            raise ToolResultBatchValidationError(
                "turn_id_mismatch",
                "tool_results.turn_id 与当前 pending_turn_id 不匹配",
            )
        if result.status not in _VALID_RESULT_STATUSES:
            raise ToolResultBatchValidationError(
                "invalid_result_status",
                f"未知 tool result status：{result.status}",
            )
        metadata = session.pending_tool_calls.get(result.tool_use_id)
        if not isinstance(metadata, dict):
            raise ToolResultBatchValidationError(
                "missing_pending_metadata",
                f"tool result 缺少 pending 元数据：{result.tool_use_id}",
            )
        expected_frame_id = str(metadata.get("frame_id", ""))
        if not expected_frame_id or result.frame_id != expected_frame_id:
            raise ToolResultBatchValidationError(
                "frame_id_mismatch",
                "tool_results.frame_id 与 pending 工具调用不匹配："
                f"tool_use_id={result.tool_use_id}; "
                f"expected={expected_frame_id}; actual={result.frame_id}",
            )
        if expected_frame_id not in frames:
            raise ToolResultBatchValidationError(
                "pending_frame_missing",
                f"未知 frame_id：{expected_frame_id}",
            )
        frame = frames[expected_frame_id]
        pending_lineage_id = str(metadata.get("request_lineage_id", ""))
        pending_map_task_id = str(metadata.get("map_task_id", ""))
        if (
            pending_lineage_id
            and frame.map_request_lineage_id
            and pending_lineage_id != frame.map_request_lineage_id
        ):
            raise ToolResultBatchValidationError(
                "request_lineage_mismatch",
                "tool result 的请求 lineage 与所属 Frame 不匹配",
            )
        if (
            pending_map_task_id
            and frame.map_task_id
            and pending_map_task_id != frame.map_task_id
        ):
            raise ToolResultBatchValidationError(
                "map_task_lineage_mismatch",
                "tool result 的地图任务 id 与所属 Frame 不匹配",
            )
        active_scope = session.map_request_scope
        if (
            pending_lineage_id
            and active_scope.lineage_id
            and pending_lineage_id != active_scope.lineage_id
        ):
            raise ToolResultBatchValidationError(
                "inactive_request_lineage",
                "tool result 不属于当前活跃请求 lineage",
            )
        tool_name = str(metadata.get("name", ""))
        tool = tools.get(tool_name)
        if tool is None or tool.side != "front":
            raise ToolResultBatchValidationError(
                "pending_tool_unavailable",
                f"pending 工具不可用于前端结果回填：{tool_name or result.tool_use_id}",
            )
        if metadata.get("authorization") == "deny":
            raise ToolResultBatchValidationError(
                "pending_tool_unauthorized",
                f"pending 工具没有执行授权：{tool_name}",
            )
        if result.grant_session_allow and (
            result.status != "applied"
            or not tool.mutating
            or metadata.get("needs_confirm") is not True
        ):
            raise ToolResultBatchValidationError(
                "invalid_session_grant",
                "grant_session_allow 仅允许用于已确认并成功执行的改动型工具："
                f"{result.tool_use_id}",
            )
        validated.append(
            ValidatedToolResult(
                result=result.model_copy(deep=True),
                metadata=MappingProxyType(deepcopy(metadata)),
                tool=tool,
            )
        )

    return ValidatedToolResultBatch(
        turn_id=session.pending_turn_id,
        items=tuple(validated),
    )
