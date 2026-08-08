"""Evidence projection for applied front-tool results."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.types import Frame
from app.api.schemas import ToolResult
from app.config import AppSettings
from app.orchestrator.evidence import EvidenceValidationError, register_screenshot_evidence
from app.orchestrator.map_context import latest_map_revision

from app.sessions.store import Session

logger = logging.getLogger(__name__)


def append_cell_count_recovery_hint(
    frame: Frame,
    error_code: str | None,
    result_payload: Any,
) -> None:
    """Tell the model why the shape failed and how to change its next attempt."""
    if error_code != "cell_count_mismatch":
        return
    actual_cells = (
        result_payload.get("actual_cells")
        if isinstance(result_payload, dict)
        else None
    )
    hint = (
        "【cell_count_mismatch 恢复指引】\n"
        "- 计算公式：x=A..B 的列数 = (B - A + 1)，不是 (B - A)\n"
        "- 示例：x=64..86 是 23 列，y=21..23 是 3 行，总计 23×3=69 格\n"
    )
    if actual_cells is not None:
        hint += f"- 重试时必须把 expected_cells 设为 {actual_cells}\n"
    hint += "- 禁止用相同参数重试第 3 次，必须切换策略或提前终止\n"
    frame.messages.append({"role": "user", "content": hint})


def record_screenshot_evidence(
    *,
    settings: AppSettings,
    session: Session,
    frame: Frame,
    result: ToolResult,
    tool_name: str,
    tool_args: dict[str, Any],
    result_payload: Any,
    response_payload: dict[str, Any],
) -> None:
    """Record a valid screenshot as revision-bound map evidence."""
    if (
        tool_name != "capture_viewport_screenshot"
        or result.status != "applied"
        or not isinstance(result_payload, dict)
    ):
        return
    screenshot_target = str(
        result_payload.get("target_path", tool_args.get("target_path", ""))
    )
    screenshot_revision = result_payload.get(
        "map_revision",
        latest_map_revision(
            session,
            screenshot_target,
            session.map_task_state.latest_layers.get(screenshot_target),
        ),
    )
    evidence_result = {
        **result_payload,
        "target_path": screenshot_target,
        "map_revision": screenshot_revision,
        "region": (
            dict(tool_args["focus_region"])
            if isinstance(tool_args.get("focus_region"), dict)
            else {}
        ),
    }
    try:
        evidence = register_screenshot_evidence(
            session.map_task_state,
            frame,
            tool_use_id=result.tool_use_id,
            result=evidence_result,
            artifact_refs=list(response_payload.get("artifact_refs", [])),
            project_root=settings.project_root,
        )
    except EvidenceValidationError as exc:
        logger.warning(
            "Screenshot evidence rejected session=%s frame=%s tool_use_id=%s error=%s",
            session.session_id,
            frame.id,
            result.tool_use_id,
            exc,
        )
        return
    frame.map_evidence.append(
        {
            "tool_use_id": result.tool_use_id,
            "kind": "viewport_screenshot",
            "target_path": screenshot_target,
            "map_revision": screenshot_revision,
            "region": evidence.metadata.get("region", {}),
            "artifact_refs": [evidence.artifact_ref],
            "evidence_id": evidence.evidence_id,
            "contract_id": evidence.contract_id,
        }
    )
