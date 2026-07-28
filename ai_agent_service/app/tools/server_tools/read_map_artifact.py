"""`read_map_artifact`：读取会话级地图工具完整结果。"""

from __future__ import annotations

from typing import Any

from app.orchestrator.map_artifacts import (
    CURRENT_MAP_ARTIFACT_TURN,
    MapArtifactStore,
)
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register
from app.tools.server_tools.artifact_errors import (
    artifact_read_error,
    image_artifact_mismatch,
)

READ_MAP_ARTIFACT_SCHEMA: dict[str, Any] = {
    "name": "read_map_artifact",
    "description": (
        "按 artifact_turn_id 和 artifact_entry_id 读取当前会话的完整地图工具结果；"
        "支持读取本次提交尚未落盘的结果。读取 cells/matches 等数组时使用 field、offset、limit 分页。"
        "截图不是地图 artifact；查看截图必须使用 read_image_metadata。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "artifact_ref": {"type": "string"},
            "artifact_turn_id": {"type": "string"},
            "artifact_entry_id": {"type": "string"},
            "field": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["artifact_ref"],
    },
}


async def read_map_artifact_handler(
    args: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """安全读取当前 session 的地图结果，优先命中事务内暂存块。"""
    artifact_ref = args.get("artifact_ref")
    turn_id = args.get("artifact_turn_id")
    entry_id = args.get("artifact_entry_id")
    if not isinstance(artifact_ref, str) or not artifact_ref:
        return artifact_read_error(
            str(artifact_ref or ""),
            "map_tool_result",
            ValueError("artifact_ref 不能为空"),
        )
    mismatch = image_artifact_mismatch(artifact_ref, "map_tool_result")
    if mismatch is not None:
        return mismatch
    if turn_id is None:
        turn_id = ""
    if entry_id is None:
        entry_id = ""
    if not isinstance(turn_id, str):
        return artifact_read_error(
            artifact_ref,
            "map_tool_result",
            ValueError("artifact_turn_id 必须是字符串"),
        )
    if not isinstance(entry_id, str):
        return artifact_read_error(
            artifact_ref,
            "map_tool_result",
            ValueError("artifact_entry_id 必须是字符串"),
        )
    field = args.get("field", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", 50)
    if not isinstance(field, str):
        return artifact_read_error(
            artifact_ref,
            "map_tool_result",
            ValueError("field 必须是字符串"),
        )
    if isinstance(offset, bool) or not isinstance(offset, int):
        return artifact_read_error(
            artifact_ref,
            "map_tool_result",
            ValueError("offset 必须是整数"),
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        return artifact_read_error(
            artifact_ref,
            "map_tool_result",
            ValueError("limit 必须是整数"),
        )
    store = MapArtifactStore(ctx.security.project_root, ctx.session_id)
    try:
        return store.read_page(
            artifact_ref,
            turn_id=turn_id,
            entry_id=entry_id,
            field=field,
            offset=offset,
            limit=limit,
            staged=CURRENT_MAP_ARTIFACT_TURN.get(),
        )
    except (OSError, TypeError, ValueError) as error:
        return artifact_read_error(artifact_ref, "map_tool_result", error)


def register_read_map_artifact_tool() -> None:
    """注册地图工具结果专用只读工具。"""
    register(
        ToolDef(
            name="read_map_artifact",
            domain="map",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="读取地图区域、空间索引或校验工具的完整 artifact 结果",
            schema=READ_MAP_ARTIFACT_SCHEMA,
            handler=read_map_artifact_handler,
        )
    )
