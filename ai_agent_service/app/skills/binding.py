"""按当前 Agent、阶段、Worker mode 与权限解析 Skill 能力。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.orchestrator.map_capabilities import (
    MAP_TOOL_CAPABILITIES,
    map_tools_for_stage,
    map_tools_for_worker_mode,
)
from app.orchestrator.runtime_contracts import SkillBindingResult
from app.skills.types import SkillDefinition
from app.tools.registry import ToolDef


@dataclass(frozen=True)
class SkillBindingContext:
    """定义一次 Skill 绑定可见的 Agent 与运行时边界。"""

    agent_tools: frozenset[str]
    permitted_tools: frozenset[str]
    workflow_stage: str | None = None
    worker_mode: str | None = None
    agent_role: str | None = None


class SkillBindingResolver:
    """从多层能力合同求出 Skill 当前可调用工具并返回结构化状态。"""

    def __init__(self, tools: Mapping[str, ToolDef]) -> None:
        """保存只读工具注册表视图。"""
        self._tools = tools

    def resolve(
        self,
        requested_name: str,
        skill: SkillDefinition | None,
        context: SkillBindingContext,
    ) -> SkillBindingResult:
        """解析单个 Skill，区分缺失、禁用和各类上下文不兼容。"""
        if skill is None:
            return SkillBindingResult(
                status="missing",
                requested_name=requested_name,
                reason_codes=("skill_missing",),
            )
        if not skill.enabled:
            return SkillBindingResult(
                status="incompatible",
                requested_name=requested_name,
                qualified_name=skill.qualified_name,
                required_capabilities=tuple(skill.required_capabilities),
                reason_codes=("skill_disabled",),
            )
        if (
            skill.compatible_roles
            and context.agent_role is not None
            and context.agent_role not in skill.compatible_roles
        ):
            return self._incompatible(
                requested_name,
                skill,
                "role_incompatible",
            )
        if (
            skill.compatible_stages
            and context.workflow_stage is not None
            and context.workflow_stage not in skill.compatible_stages
        ):
            return self._incompatible(
                requested_name,
                skill,
                "stage_incompatible",
            )
        if (
            skill.compatible_modes
            and context.worker_mode is not None
            and context.worker_mode not in skill.compatible_modes
        ):
            return self._incompatible(
                requested_name,
                skill,
                "mode_incompatible",
            )

        candidates = (
            set(context.agent_tools)
            & set(context.permitted_tools)
            & set(self._tools)
        )
        if context.worker_mode is not None:
            candidates &= set(map_tools_for_worker_mode(context.worker_mode))
        elif context.workflow_stage is not None:
            map_tools = set(MAP_TOOL_CAPABILITIES)
            candidates = (candidates - map_tools) | (
                candidates & set(map_tools_for_stage(context.workflow_stage))
            )

        required = tuple(skill.required_capabilities)
        matched_by_capability: set[str] = set()
        missing_capabilities: list[str] = []
        for capability in required:
            matched = self._tools_for_capability(capability, candidates, context)
            if not matched:
                missing_capabilities.append(capability)
            matched_by_capability.update(matched)
        if missing_capabilities:
            return SkillBindingResult(
                status="incompatible",
                requested_name=requested_name,
                qualified_name=skill.qualified_name,
                required_capabilities=required,
                reason_codes=tuple(
                    f"required_capability_unavailable:{item}"
                    for item in missing_capabilities
                ),
            )

        if required:
            effective = matched_by_capability
        elif skill.capability_tags:
            effective = {
                name
                for name in candidates
                if self._tools[name].domain in skill.capability_tags
            }
        elif skill.allowed_tools and skill.allowed_tools != ["*"]:
            effective = candidates & set(skill.allowed_tools)
        else:
            effective = candidates
        if (required or skill.capability_tags or skill.allowed_tools) and not effective:
            return self._incompatible(
                requested_name,
                skill,
                "no_effective_tools",
            )
        return SkillBindingResult(
            status="resolved",
            requested_name=requested_name,
            qualified_name=skill.qualified_name,
            effective_tools=tuple(sorted(effective)),
            required_capabilities=required,
        )

    def _tools_for_capability(
        self,
        capability: str,
        candidates: set[str],
        context: SkillBindingContext,
    ) -> set[str]:
        """返回满足一个语义能力声明的候选工具。"""
        prefix, separator, value = capability.partition(":")
        if not separator or not value:
            return set()
        if prefix == "tool":
            return {value} if value in candidates else set()
        if prefix == "domain":
            return {
                name for name in candidates if self._tools[name].domain == value
            }
        if prefix == "category":
            return {
                name
                for name in candidates
                if MAP_TOOL_CAPABILITIES.get(name) is not None
                and MAP_TOOL_CAPABILITIES[name].category == value
            }
        if prefix == "stage":
            return (
                set(candidates)
                if context.workflow_stage is None or context.workflow_stage == value
                else set()
            )
        if prefix == "mode":
            return (
                set(candidates)
                if context.worker_mode is None or context.worker_mode == value
                else set()
            )
        if prefix == "role":
            return (
                set(candidates)
                if context.agent_role is None or context.agent_role == value
                else set()
            )
        return set()

    @staticmethod
    def _incompatible(
        requested_name: str,
        skill: SkillDefinition,
        reason_code: str,
    ) -> SkillBindingResult:
        """构造统一的不兼容结果。"""
        return SkillBindingResult(
            status="incompatible",
            requested_name=requested_name,
            qualified_name=skill.qualified_name,
            required_capabilities=tuple(skill.required_capabilities),
            reason_codes=(reason_code,),
        )
