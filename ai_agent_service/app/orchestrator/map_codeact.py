"""归约地图 CodeAct 执行证据、修复预算和失败保留语义。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.orchestrator.map_workflow import replace_map_state_field


def record_map_codeact_execution(
    state: Any,
    *,
    task_execution_id: str,
    validation: dict[str, Any],
    diff_artifact: str | None,
    retry_budget: int,
    repair_context: dict[str, Any] | None = None,
) -> None:
    """持久化地图 CodeAct 的验证结果、修复上下文和 retained-diff 状态。"""
    previous = state.codeact_execution
    same_execution = previous.get("task_execution_id") == task_execution_id
    failures = int(previous.get("validation_failures", 0)) if same_execution else 0
    validation_status = str(validation.get("status", "unavailable"))
    failed = validation_status == "failed"
    failures += int(failed)
    # `retry_budget` 表示首次失败之后可继续执行的修复次数，而非总失败次数。
    retries_remaining = max(0, retry_budget - failures + 1)
    if validation_status == "passed":
        status = "validated"
    elif failed and retries_remaining:
        status = "repair_required"
    else:
        status = "failed_validation"
    replace_map_state_field(
        state,
        "codeact_execution",
        {
            "task_execution_id": task_execution_id,
            "validation": deepcopy(validation),
            "repair_context": deepcopy(repair_context or {}),
            "retry_budget": retry_budget,
            "validation_failures": failures,
            "retries_remaining": retries_remaining,
            "diff_artifact": diff_artifact,
            "execution_status": status,
            "recovery_disposition": "retain_diff" if status == "failed_validation" else "continue",
        },
    )
