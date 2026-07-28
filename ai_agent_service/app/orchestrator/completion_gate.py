"""地图任务唯一 Completion Gate。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.orchestrator.evidence import scoped_evidence
from app.orchestrator.map_workflow import map_workflow_scope_key


@dataclass(frozen=True)
class CompletionGateDecision:
    """保存 Completion Gate 的唯一允许结论与结构化阻断原因。"""

    allowed: bool
    blockers: tuple[dict[str, Any], ...]
    scopes: tuple[str, ...]


def evaluate_map_completion(state: Any) -> CompletionGateDecision:
    """综合验证、reviewer issues、证据、blocker 和工作流状态计算完成结论。"""
    blockers: list[dict[str, Any]] = []
    scopes: list[str] = []
    targets = _completion_targets(state)
    if not targets:
        blockers.append(
            {
                "reason": "completion_target_missing",
                "issues": ["no canonical map target/revision is known"],
            }
        )
    for target in targets:
        revision = state.latest_revisions.get(target)
        if isinstance(revision, bool) or not isinstance(revision, int):
            blockers.append(
                {
                    "target": target,
                    "reason": "completion_revision_missing",
                    "issues": ["target has no authoritative map revision"],
                }
            )
            continue
        scope_key = map_workflow_scope_key(target, revision)
        scopes.append(scope_key)
        scope = state.workflow_scopes.get(scope_key, {})
        validation = scope.get("validation")
        if not isinstance(validation, dict):
            validation = state.latest_validations.get(target)
        if not isinstance(validation, dict) or validation.get("map_revision") not in {
            None,
            revision,
        }:
            blockers.append(
                {
                    "target": target,
                    "required_revision": revision,
                    "reason": "same_revision_validation_missing",
                    "issues": ["same-revision validator observation is missing"],
                }
            )
        elif validation.get("passed") is not True or validation.get(
            "blocking_completion"
        ) is True:
            blockers.append(
                {
                    "target": target,
                    "required_revision": revision,
                    "reason": "validation_failed",
                    "issues": list(validation.get("issues", [])),
                }
            )
        evidence = scoped_evidence(
            state,
            target,
            revision,
            "viewport_screenshot",
        )
        if not evidence:
            blockers.append(
                {
                    "target": target,
                    "required_revision": revision,
                    "reason": "scoped_screenshot_missing",
                    "issues": ["no readable same-target/revision reviewer screenshot is registered"],
                }
            )

    for blocker in state.completion_blockers:
        if not isinstance(blocker, dict):
            continue
        target = str(blocker.get("target", ""))
        required_revision = blocker.get("required_revision")
        current_revision = state.latest_revisions.get(target)
        if target and isinstance(required_revision, int) and current_revision != required_revision:
            continue
        blockers.append(dict(blocker))
    if state.status == "paused":
        blockers.append(
            {
                "reason": "workflow_paused",
                "issues": [state.pause_reason or "map workflow is paused"],
            }
        )
    return CompletionGateDecision(
        allowed=not blockers,
        blockers=tuple(blockers),
        scopes=tuple(scopes),
    )


def completion_gate_text(decision: CompletionGateDecision) -> str:
    """把结构化 Gate 阻断原因转换为稳定的用户可见文本。"""
    lines = ["地图任务尚未通过 Completion Gate："]
    for blocker in decision.blockers:
        target = str(blocker.get("target", "")).strip()
        reason = str(blocker.get("reason", "completion_blocked"))
        issues = blocker.get("issues", [])
        issue_text = "；".join(str(item) for item in issues) if isinstance(issues, list) else ""
        scope = f"[{target}] " if target else ""
        lines.append(f"- {scope}{reason}" + (f"：{issue_text}" if issue_text else ""))
    return "\n".join(lines)


def has_canonical_map_target_revision(state: Any) -> bool:
    """Return whether completion has at least one authoritative target/revision."""
    targets = _completion_targets(state)
    return bool(targets) and all(
        isinstance(state.latest_revisions.get(target), int)
        and not isinstance(state.latest_revisions.get(target), bool)
        for target in targets
    )


def _completion_targets(state: Any) -> list[str]:
    """按上下文优先、revision 兜底返回规范目标列表。"""
    targets_value = state.context_state.get("targets", {})
    targets = (
        [str(item) for item in targets_value]
        if isinstance(targets_value, dict)
        else []
    )
    if not targets:
        targets = [
            str(key)
            for key in state.latest_revisions
            if "::" not in str(key)
        ]
    return list(dict.fromkeys(target for target in targets if target))
