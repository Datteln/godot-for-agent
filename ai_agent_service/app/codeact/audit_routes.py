"""提供只读且受主服务认证保护的 CodeAct 审计查询。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.codeact.gateway import ExecutionGateway

_EXECUTION_ID = re.compile(r"^exec-[0-9a-f]{32}$")


class CodeActAuditTimelineResponse(BaseModel):
    """返回一个后端派生执行标识对应的有界审计时间线。"""

    model_config = ConfigDict(extra="forbid")
    task_execution_id: str
    events: list[dict[str, Any]]


def add_codeact_audit_routes(router: APIRouter, gateway: ExecutionGateway) -> None:
    """附加不会产生执行副作用的审计时间线查询路由。"""

    @router.get(
        "/codeact/audit/{task_execution_id}",
        response_model=CodeActAuditTimelineResponse,
    )
    async def codeact_audit_timeline(task_execution_id: str) -> CodeActAuditTimelineResponse:
        """读取一个可信格式执行标识的持久化审计证据。"""
        if _EXECUTION_ID.fullmatch(task_execution_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid task execution id")
        return CodeActAuditTimelineResponse(
            task_execution_id=task_execution_id,
            events=await gateway.audit_timeline(task_execution_id),
        )
