"""`read_delegate_artifact`：按会话安全读取地图子 Agent 完整结果。"""

from __future__ import annotations

from typing import Any

from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

READ_DELEGATE_ARTIFACT_SCHEMA: dict[str, Any] = {
    "name": "read_delegate_artifact",
    "description": (
        "读取当前会话地图子 Agent 的完整 artifact。省略 field 时只返回可用字段；"
        "读取 proposed_batches/write_results 等数组时使用 offset/limit 分页。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "artifact_ref": {"type": "string"},
            "field": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["artifact_ref"],
    },
}


async def read_delegate_artifact_handler(
    args: dict[str, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """读取当前 session 的委派 artifact，拒绝跨目录和跨会话引用。"""
    artifact_ref = args.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref:
        raise ValueError("artifact_ref 不能为空")
    field = args.get("field", "")
    if not isinstance(field, str):
        raise ValueError("field 必须是字符串")
    offset = args.get("offset", 0)
    limit = args.get("limit", 20)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("offset 必须是整数")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是整数")
    store = DelegateArtifactStore(ctx.security.project_root, ctx.session_id)
    return store.read_page(
        artifact_ref,
        field=field,
        offset=offset,
        limit=limit,
    )


def register_read_delegate_artifact_tool() -> None:
    """注册只读地图委派 artifact 工具。"""
    register(
        ToolDef(
            name="read_delegate_artifact",
            domain="map",
            side="server",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="读取地图子 Agent 完整委派结果或批次",
            schema=READ_DELEGATE_ARTIFACT_SCHEMA,
            handler=read_delegate_artifact_handler,
        )
    )
