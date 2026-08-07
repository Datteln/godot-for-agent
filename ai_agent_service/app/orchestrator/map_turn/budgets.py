"""维护 Map turn 的持久预算与耗尽结果。"""

from __future__ import annotations

from app.agents.types import Frame
from app.orchestrator.map_progress import (
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    map_pause_message,
)
from app.orchestrator.map_turn.contracts import logger
from app.orchestrator.turn.contracts import (
    ErrorTurnOutcome,
)
from app.sessions.store import Session


def _uses_persistent_map_budget(frame: Frame) -> bool:
    """判断帧是否属于需要跨 HTTP 累计预算的地图工作流。"""
    # 本轮整改：改用 pipeline_kind=="map" 声明式元数据，
    # 替代 name.startswith("map-") 和 workflow_operations 的隐式判断
    return frame.agent.pipeline_kind == "map"


def _latest_map_progress_revision(session: Session) -> int | None:
    """返回会话当前已知的最高地图 revision。"""
    # 本轮整改：revisions 从 session.latest_map_revisions 迁移到
    # map_task_state.latest_revisions，统一管理地图进度状态
    return max(session.map_task_state.latest_revisions.values(), default=None)


def _sync_map_progress_budget(session: Session, frame: Frame) -> None:
    """Track revision progress without resetting task-level convergence budgets."""
    revision = _latest_map_progress_revision(session)
    if revision == frame.map_progress_revision:
        return
    frame.map_progress_revision = revision


def _map_turn_exhausted(session: Session, max_turns: int) -> ErrorTurnOutcome:
    """Create the terminal outcome when TurnDriver consumes its transition budget."""
    logger.warning(
        "Agent TurnDriver.run reached max turns session=%s max_turns=%d",
        session.session_id,
        max_turns,
    )
    if (
        session.map_request_scope.activates_map_gate
        and session.map_request_scope.map_task_id == session.map_task_state.task_id
        and session.map_task_state.status == "running"
    ):
        session.map_task_state.make_checkpoint(
            "budget_exhausted",
            pause_kind="budget_exhausted",
        )
        return ErrorTurnOutcome(
            text=map_pause_message(session.map_task_state),
            error_code="agent_turn_budget_exhausted",
        )
    return ErrorTurnOutcome(
        text="已达到本轮最大循环次数，请精简任务或拆分请求后重试",
        error_code="agent_turn_budget_exhausted",
    )
