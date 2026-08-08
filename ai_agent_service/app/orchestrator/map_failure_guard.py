"""失败防护：工具失败指纹、重复拦截与无进展保护。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    record_semantic_retry,
    retry_pause_report,
)
from app.orchestrator.map_workflow import (
    increment_map_counter,
    replace_map_state_field,
    workflow_scope_identity,
)
from .map_context import _revision, _target
from .map_state import MapTaskState, _minimal_pause_report
if TYPE_CHECKING:
    from app.sessions.store import Session
def map_pause_message(state: MapTaskState) -> str:
    """按类型化暂停原因生成真实且可恢复的用户提示。

    Args:
        state: 已暂停的地图任务状态。

    Returns:
        包含原因、非空报告和检查点的中文提示。
    """
    target, revision = workflow_scope_identity(state)
    pause_kind = state.pause_kind or "workflow_blocked"
    report = (
        deepcopy(state.pause_report)
        if state.pause_report
        else _minimal_pause_report(
            state,
            pause_kind=pause_kind,
            reason=state.pause_reason or pause_kind,
            target=target,
            revision=revision,
        )
    )
    prefix_by_kind = {
        "client_timeout": "地图任务因客户端等待超时已暂停。",
        "user_interrupted": "地图任务已按用户请求暂停。",
        "provider_exhausted": "地图任务因主模型与备用模型均不可用而暂停。",
        "budget_exhausted": "地图任务因执行预算耗尽已暂停。",
        "no_progress_exhausted": "地图任务因连续无进展已暂停。",
        "workflow_blocked": "地图任务因工作流阻塞已暂停。",
    }
    prefix = prefix_by_kind.get(pause_kind, prefix_by_kind["workflow_blocked"])
    checkpoint = state.checkpoint or {
        "task_id": state.task_id,
        "status": state.status,
        "stage": state.stage,
        "pause_kind": pause_kind,
    }
    return (
        f"{prefix}根因与恢复建议："
        f"{json.dumps(report, ensure_ascii=False, default=str)}；恢复检查点："
        f"{json.dumps(checkpoint, ensure_ascii=False, default=str)}"
    )


def record_no_progress(session: Session, target: str, reason: str) -> dict[str, Any] | None:
    """累计无进展事件，并在第三次时生成暂停检查点。

    map_task_state 已是唯一状态源，无需在生成检查点前额外同步。
    """
    state: MapTaskState = session.map_task_state
    scoped_target, revision = workflow_scope_identity(state, target=target)
    retry = record_semantic_retry(
        state,
        category="validation_failure",
        error_category=reason,
        root_cause=reason,
        stage=state.stage,
        target=scoped_target,
        revision=revision,
        operation={"reason": reason, "scope": target},
        threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
    )
    retry_key = str(retry["retry_key"])
    streak = int(retry["attempt"])
    streaks = dict(state.no_progress_streaks)
    streaks[retry_key] = streak
    replace_map_state_field(
        state,
        "no_progress_streaks",
        streaks,
        target=scoped_target,
        revision=revision,
    )
    increment_map_counter(state, "no_progress_events", target=target, revision=revision)
    if streak < SEMANTIC_RETRY_MAX_ATTEMPTS:
        return None
    report = retry_pause_report(
        state,
        stage=state.stage,
        target=scoped_target,
        revision=revision,
        last_attempt=retry,
    )
    if state.status == "running":
        return state.make_checkpoint(
            reason,
            report,
            pause_kind="no_progress_exhausted",
        )
    state.pause_kind = "no_progress_exhausted"
    state.pause_reason = reason
    replace_map_state_field(
        state,
        "pause_report",
        report,
        target=scoped_target,
        revision=revision,
    )
    checkpoint = {
        "task_id": state.task_id,
        "status": state.status,
        "stage": state.stage,
        "reason": reason,
        "pause_kind": state.pause_kind,
        "pause_report": report,
    }
    replace_map_state_field(
        state,
        "checkpoint",
        checkpoint,
        target=scoped_target,
        revision=revision,
    )
    return checkpoint


_TOOL_FAILURE_VOLATILE_KEYS = frozenset(
    {
        "batch_index",
        "frame_id",
        "mode",
        "plan_version",
        "task_summary",
        "worker",
        "workflow_constraints",
        "workflow_operations",
        "write_batch_id",
    }
)


def map_tool_call_fingerprint(
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """生成忽略编排元数据的稳定地图工具调用指纹。"""
    normalized = {
        key: value for key, value in tool_args.items() if key not in _TOOL_FAILURE_VOLATILE_KEYS
    }
    encoded = json.dumps(
        {"tool": tool_name, "args": normalized},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def remember_map_tool_failure(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    error_code: str,
    message: str,
) -> None:
    """记录一次地图工具失败，供后续相同调用在服务层直接阻断。"""
    fingerprint = map_tool_call_fingerprint(tool_name, tool_args)
    failures = dict(session.map_task_state.tool_failure_fingerprints)
    failures[fingerprint] = {
        "tool": tool_name,
        "error_code": error_code,
        "message": message,
    }
    while len(failures) > 128:
        failures.pop(next(iter(failures)))
    replace_map_state_field(
        session.map_task_state,
        "tool_failure_fingerprints",
        failures,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )


def repeated_map_tool_failure_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """在相同地图工具调用已失败时返回不重复下发的阻断消息。"""
    fingerprint = map_tool_call_fingerprint(tool_name, tool_args)
    failure = session.map_task_state.tool_failure_fingerprints.get(fingerprint)
    if not isinstance(failure, dict):
        return None
    return (
        "duplicate_tool_failure_blocked：相同参数的地图工具调用已经失败过，"
        "服务层不会再次下发。"
        f"previous_error_code={failure.get('error_code', 'unknown')}。"
        "必须修改资源键、操作参数或重新规划，禁止原样重试。"
    )
