"""User-request identity and Map lineage activation."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from app.api.schemas import ChatRequest
from app.orchestrator.completion_gate import has_canonical_map_target_revision
from app.orchestrator.map_state import reset_map_task_progress, resume_map_task

from app.orchestrator.map_request_scope import (
    MapRequestScope,
    bind_map_task,
    is_continuation_intent,
    new_request_scope,
)
from app.orchestrator.map_turn.contracts import _tool_message
from app.sessions.store import Session


def _map_completion_candidate_is_current(session: Session) -> bool:
    """Return whether the current final is owned by an active map-edit lineage."""
    scope = session.map_request_scope
    frame = session.top_frame()
    return (
        scope.activates_map_gate
        and scope.completion_candidate
        and session.map_task_state.status in {"running", "completed"}
        and scope.map_task_id == session.map_task_state.task_id
        and frame is not None
        and frame.map_request_lineage_id == scope.lineage_id
        and frame.map_task_id == scope.map_task_id
        and has_canonical_map_target_revision(session.map_task_state)
    )

def _bind_request_scope_to_frames(
    session: Session,
    scope: MapRequestScope,
    *,
    all_frames: bool,
) -> None:
    """Attach the active request lineage to the root/current workflow Frames."""
    frames = session.agent_stack if all_frames else session.agent_stack[:1]
    for frame in frames:
        frame.map_request_lineage_id = scope.lineage_id
        frame.map_task_id = scope.map_task_id or None

def _retire_non_root_frames(session: Session, reason: str) -> None:
    """Close an abandoned child stack so an unrelated request can use the root."""
    if len(session.agent_stack) <= 1:
        return
    root = session.agent_stack[0]
    direct_child = next(
        (frame for frame in session.agent_stack[1:] if frame.parent_id == root.id),
        None,
    )
    call_id: str | None = None
    if direct_child is not None:
        call_id = direct_child.pending_delegate_call_id
        if call_id is None and direct_child.pending_delegate_group_id is not None:
            group = session.delegate_groups.get(direct_child.pending_delegate_group_id, {})
            value = group.get("tool_call_id")
            call_id = str(value) if value else None
    if call_id:
        root.messages.append(
            _tool_message(
                call_id,
                {
                    "error": True,
                    "reason": reason,
                    "summary": "Previous delegated map runtime was made dormant by a new request.",
                },
                is_error=True,
            )
        )
    session.agent_stack = [root]
    session.pending_plan = None
    session.delegate_groups.clear()

def _can_contextually_resume_map_task(session: Session, user_message: str) -> bool:
    """判断续作指代是否唯一指向当前仍聚焦的已授权地图任务。

    该判断只解析“任务”所指对象，不创建新权限。只有上一请求、任务 lineage、
    暂停检查点三者一致时，泛化续作文本才可继承原地图编辑范围。

    Args:
        session: 当前会话。
        user_message: 当前用户消息原文。

    Returns:
        是否可以无歧义恢复当前地图任务。
    """
    if not is_continuation_intent(user_message):
        return False
    state = session.map_task_state
    if state.status != "paused" or not state.task_id or not isinstance(state.checkpoint, dict):
        return False
    previous_scope = session.map_request_scope
    task_lineage = session.map_task_lineage
    return (
        previous_scope.intent == "map_edit"
        and previous_scope.map_task_id == state.task_id
        and previous_scope.lineage_id == str(task_lineage.get("lineage_id", ""))
        and state.task_id == str(task_lineage.get("task_id", ""))
    )

def _activate_user_request_scope(
    session: Session,
    request: ChatRequest,
    *,
    dedicated_resume_authorized: bool = False,
) -> tuple[MapRequestScope, bool]:
    """Classify a user request and explicitly start or resume a map task."""
    contextual_resume_authorized = _can_contextually_resume_map_task(
        session,
        request.user_message or "",
    )
    scope = new_request_scope(
        request_id=request.request_id,
        user_message=request.user_message or "",
        dedicated_resume_authorized=(dedicated_resume_authorized or contextual_resume_authorized),
    )
    state = session.map_task_state
    resumed_existing_task = False
    if scope.intent == "map_edit":
        can_continue = (
            scope.explicit_continuation
            and bool(state.task_id)
            and state.status in {"running", "paused"}
        )
        if can_continue:
            if state.status == "paused":
                resume_map_task(state)
            scope = bind_map_task(scope, state.task_id)
            task_lineage = session.map_task_lineage
            if str(task_lineage.get("task_id", "")) == state.task_id and str(
                task_lineage.get("lineage_id", "")
            ):
                scope = replace(
                    scope,
                    lineage_id=str(task_lineage["lineage_id"]),
                    completion_candidate=(task_lineage.get("completion_candidate") is True),
                )
            resumed_existing_task = True
            _bind_request_scope_to_frames(session, scope, all_frames=True)
        else:
            if state.status == "paused":
                state.cancel("replaced_by_explicit_map_edit")
            _retire_non_root_frames(session, "replaced_by_explicit_map_edit")
            task_digest = hashlib.sha256(scope.lineage_id.encode("utf-8")).hexdigest()[:20]
            task_id = f"map-request-{task_digest}"
            root = session.agent_stack[0] if session.agent_stack else None
            reset_map_task_progress(
                session,
                root,
                task_id=task_id,
                lineage_id=scope.lineage_id,
            )
            scope = bind_map_task(scope, task_id)
            session.map_task_lineage = {
                "task_id": task_id,
                "lineage_id": scope.lineage_id,
                "origin_request_id": scope.request_id,
                "completion_candidate": False,
            }
            _bind_request_scope_to_frames(session, scope, all_frames=False)
    else:
        _retire_non_root_frames(session, "unrelated_request")
        _bind_request_scope_to_frames(session, scope, all_frames=False)
    session.map_request_scope = scope
    return scope, resumed_existing_task
