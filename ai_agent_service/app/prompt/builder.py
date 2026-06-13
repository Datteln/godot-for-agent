"""System prompt 分层组装（§6/§11/§13）。

M0/M1 先实现最小 PromptBuilder：保留 AgentDefinition 正文作为核心规则，
再追加稳定的 Skill 摘要列表。Skill 全文仍只能通过 `load_skill` 按需加载。
"""

from __future__ import annotations

from app.agents.types import AgentDefinition
from app.output_styles.catalog import OutputStyleCatalog
from app.skills.catalog import SkillCatalog


def build_system_prompt(
    agent: AgentDefinition,
    skill_catalog: SkillCatalog | None = None,
    output_style_catalog: OutputStyleCatalog | None = None,
    output_style_id: str | None = None,
) -> str:
    """构造当前 agent 帧的 system prompt。"""
    parts = [agent.prompt.strip()]

    if agent.hooks is not None:
        on_start = agent.hooks.get("on_start")
        if on_start:
            parts.append(on_start.strip())

    if output_style_catalog is not None:
        style = output_style_catalog.get(output_style_id)
        if style is not None and style.enabled:
            parts.append(
                "当前 OutputStyle "
                + style.qualified_name
                + ":\n"
                + style.body
            )

    if skill_catalog is not None:
        preloaded: list[str] = []
        for name in agent.skills:
            skill = skill_catalog.get(name)
            if skill is not None and skill.enabled:
                preloaded.append(
                    "预加载 Skill "
                    + skill.qualified_name
                    + ":\n"
                    + skill.body
                )
        if preloaded:
            parts.append("\n\n".join(preloaded))

        summaries = [summary for summary in skill_catalog.summaries() if summary.enabled]
        if summaries:
            lines = [
                "可用 Skill（只显示摘要；需要全文时调用 load_skill(name)）：",
            ]
            for summary in summaries:
                lines.append(
                    "- "
                    + summary.qualified_name
                    + ": "
                    + summary.description
                    + "；适用场景："
                    + summary.when_to_use
                )
            parts.append("\n".join(lines))

    return "\n\n".join(part for part in parts if part)
