"""Final-response verification and deterministic planner projection."""

from __future__ import annotations

import json
from typing import Any

from app.agents.types import Frame
from app.api.schemas import ChatErrorResponse, ChatFinalResponse, ChatResponse
from app.orchestrator.map_progress import parse_map_plan_outcome
from app.sessions.store import Session


def _apply_verification_policy(session: Session, response: ChatResponse) -> ChatResponse:
    """Block required unavailable verification and label advisory continuation."""
    if not isinstance(response, ChatFinalResponse):
        return response
    unverified = [
        (path, item)
        for path, item in session.verify_state.items()
        if isinstance(item, dict) and item.get("status") in {"failed", "unavailable"}
    ]
    required = [(path, item) for path, item in unverified if item.get("policy") == "required"]
    if required:
        path, item = required[0]
        actions = [
            str(action.get("action", ""))
            for action in item.get("recovery_actions", [])
            if isinstance(action, dict)
        ]
        return ChatErrorResponse(
            text=(
                f"必需校验不可用，工作流已停在未验证检查点：{path}；"
                f"原因={item.get('reason_code', 'unknown')}；"
                f"可选动作={', '.join(actions) or 'pause_unverified'}"
            ),
            error_code=(
                "verification_unavailable"
                if item.get("status") == "unavailable"
                else "verification_failed"
            ),
            disposition="pause_for_user",
            retryable=False,
            side_effect_state="committed",
            next_action={
                "action": "select_one_verify_recovery",
                "owner": "user",
                "target": path,
                "permitted_actions": actions,
            },
        )
    if unverified and "[UNVERIFIED]" not in response.text:
        evidence = "; ".join(
            f"{path}: {item.get('reason_code', 'unknown')}" for path, item in unverified
        )
        return ChatFinalResponse(
            text=f"{response.text}\n\n[UNVERIFIED] Advisory verification unavailable: {evidence}"
        )
    return response

def _planner_completion_text(
    frame: Frame,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> str:
    """把确定性平台校验结果转换为 planner 的结构化阶段输出。"""
    outcome = parse_map_plan_outcome(tool_name, result)
    profile_plan_value = result.get("profile_plan")
    profile_plan = profile_plan_value if isinstance(profile_plan_value, dict) else {}
    batches_value = result.get("edit_map_batches")
    if batches_value is None:
        batches_value = profile_plan.get("edit_map_batches")
    proposed_batches = (
        batches_value if outcome.executable and isinstance(batches_value, list) else []
    )
    issues_value = (
        result.get("issues")
        or result.get("repair_plan")
        or profile_plan.get("issues")
        or profile_plan.get("repair_plan")
    )
    issues = issues_value if isinstance(issues_value, list) else []
    target_value = result.get("target_path", result.get("target", tool_args.get("target_path", "")))
    target_path = target_value if isinstance(target_value, str) else ""
    revision_value = result.get("map_revision")
    map_revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else None
    )
    region = {
        key: tool_args[key]
        for key in ("x", "y", "z", "width", "height", "depth")
        if key in tool_args
    }
    publication_value = result.get("_planning_publication")
    publication = publication_value if isinstance(publication_value, dict) else {}
    summary = (
        "LLM 显式平台规划已通过确定性校验，规划阶段由服务端自动结束。"
        if outcome.executable
        else "第三次确定性校验仍未通过；最新规划已交付，但执行明确阻断且不会调度 writer。"
    )
    payload = {
        "stage": "planner",
        "worker": frame.agent.name,
        "mode": "propose_only",
        "objective": frame.agent.description or frame.agent.name,
        "target_path": target_path,
        "map_layer": tool_args.get("map_layer"),
        "map_revision": map_revision,
        "region": region,
        "summary": summary,
        "facts": [
            {
                "kind": "llm_platform_plan",
                "tool": tool_name,
                "platforms": tool_args.get("platforms", []),
                "segments": tool_args.get("segments", []),
            }
        ],
        "proposed_batches": proposed_batches,
        "planning_status": publication.get("planning_status", "delivered"),
        "execution_status": publication.get(
            "execution_status",
            "approved" if outcome.executable else "blocked_by_validation",
        ),
        "authoritative_snapshot": publication.get(
            "authoritative_snapshot",
            {
                "snapshot_id": tool_args.get("authoritative_snapshot_id"),
                "digest": tool_args.get("authoritative_snapshot_digest"),
            },
        ),
        "semantic_plan": publication.get(
            "semantic_plan",
            {
                "platforms": tool_args.get("platforms", []),
                "segments": tool_args.get("segments", []),
                "semantic_resources": tool_args.get("semantic_resources", []),
                "reference_cells": tool_args.get("reference_cells", []),
                "rationale": tool_args.get("rationale", ""),
            },
        ),
        "approved_batches": publication.get("approved_batches", []),
        "write_results": [],
        "validation": {
            "passed": outcome.executable,
            "completion_allowed": False,
            "issues": issues,
            "structured_issues": (
                []
                if outcome.executable
                else [
                    {
                        "code": outcome.error_code
                        or outcome.blocked_reason
                        or "platform_plan_not_executable",
                        "blocked_reason": outcome.blocked_reason,
                    }
                ]
            ),
        },
        "missing_inputs": [],
        "risks": [] if outcome.executable else ["平台路线尚不可执行，禁止进入写入阶段。"],
        "next_stage": "writer" if outcome.executable else "complete",
    }
    return json.dumps(payload, ensure_ascii=False)
