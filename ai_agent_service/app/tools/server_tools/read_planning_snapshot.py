"""`read_planning_snapshot`：读取不含逐格 atlas 的 planner 快照投影。"""

from __future__ import annotations

from typing import Any

from app.orchestrator.map_planning_snapshots import PlanningSnapshotStore
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register
from app.tools.server_tools.artifact_errors import artifact_read_error

READ_PLANNING_SNAPSHOT_SCHEMA: dict[str, Any] = {
    "name": "read_planning_snapshot",
    "description": (
        "读取当前会话权威地图规划快照的 planner 投影。该投影包含 coverage、occupancy、"
        "traversal、entry 和 reachable frontier，但刻意不返回逐格 atlas/item 写入身份。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "artifact_ref": {"type": "string"},
            "field": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["artifact_ref"],
    },
}


async def read_planning_snapshot_handler(
    args: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """安全读取当前 session 的权威规划快照投影。

    Args:
        args: artifact 引用与可选分页参数。
        ctx: 当前工具调用的项目和会话安全上下文。

    Returns:
        快照元数据、标量字段或数组分页结果。
    """
    artifact_ref = args.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref:
        return dict(
            artifact_read_error(
                str(artifact_ref or ""),
                "authoritative_map_snapshot_v1",
                ValueError("artifact_ref 不能为空"),
            )
        )
    field = args.get("field", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", 50)
    if not isinstance(field, str):
        return dict(
            artifact_read_error(
                artifact_ref,
                "authoritative_map_snapshot_v1",
                ValueError("field 必须是字符串"),
            )
        )
    if isinstance(offset, bool) or not isinstance(offset, int):
        return dict(
            artifact_read_error(
                artifact_ref,
                "authoritative_map_snapshot_v1",
                ValueError("offset 必须是整数"),
            )
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        return dict(
            artifact_read_error(
                artifact_ref,
                "authoritative_map_snapshot_v1",
                ValueError("limit 必须是整数"),
            )
        )
    store = PlanningSnapshotStore(
        ctx.security.project_root,
        ctx.session_id,
        ctx.session_epoch,
    )
    try:
        return store.read_projection_page(
            artifact_ref,
            field=field,
            offset=offset,
            limit=limit,
        )
    except (OSError, TypeError, ValueError) as error:
        return dict(
            artifact_read_error(
                artifact_ref,
                "authoritative_map_snapshot_v1",
                error,
            )
        )


def register_read_planning_snapshot_tool() -> None:
    """注册权威地图规划快照投影读取工具。"""
    register(
        ToolDef(
            name="read_planning_snapshot",
            domain="map",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="读取权威地图规划快照、occupancy、entry 或 reachable frontier",
            schema=READ_PLANNING_SNAPSHOT_SCHEMA,
            handler=read_planning_snapshot_handler,
        )
    )
