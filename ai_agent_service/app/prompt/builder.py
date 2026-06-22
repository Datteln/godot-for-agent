"""System prompt 分层组装（§6/§11/§13）。

M0/M1 先实现最小 PromptBuilder：保留 AgentDefinition 正文作为核心规则，
再追加稳定的 Skill 摘要列表。Skill 全文仍只能通过 `load_skill` 按需加载。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.types import AgentDefinition
from app.output_styles.catalog import OutputStyleCatalog
from app.skills.catalog import SkillCatalog

_TOOL_REJECTION_POLICY = (
    "工具调用被拒绝时的处理：当某个前端工具结果 `status` 为 `rejected`"
    "（用户在预览里拒绝了你提议的编辑/操作）时，绝不能就此终止对话或返回"
    "空响应——必须立即给出一条友好、建设性的正式回复，主动提出可行的替代"
    "路径，例如：手动修改步骤说明、改为只读分析/解释、或提议风险更低的"
    "降级方案；让交互流程继续顺畅，而不是卡住或晾着用户。"
)


def build_system_prompt(
    agent: AgentDefinition,
    skill_catalog: SkillCatalog | None = None,
    output_style_catalog: OutputStyleCatalog | None = None,
    output_style_id: str | None = None,
) -> str:
    """构造当前 agent 帧的 system prompt。"""
    parts = [agent.prompt.strip(), _TOOL_REJECTION_POLICY]

    if agent.hooks is not None:
        on_start = agent.hooks.get("on_start")
        if on_start:
            parts.append(on_start.strip())

    if output_style_catalog is not None:
        style = output_style_catalog.get(output_style_id)
        if style is not None and style.enabled:
            parts.append("当前 OutputStyle " + style.qualified_name + ":\n" + style.body)

    if skill_catalog is not None:
        preloaded: list[str] = []
        for name in agent.skills:
            skill = skill_catalog.get(name)
            if skill is not None and skill.enabled:
                preloaded.append("预加载 Skill " + skill.qualified_name + ":\n" + skill.body)
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


@dataclass(frozen=True)
class LayeredPrompt:
    """分层 system prompt（§16.1 / 文档 3.1 四层结构）。

    把 system 前缀拆成稳定性递减的若干层，使每层都能成为独立的缓存断点边界
    （见 `message_transformer.build_stable_prefix`）：

    Attributes:
        core: L0 核心层——agent 人格/规则 + 策略 + Skill 摘要 + OutputStyle，
            本帧存活期间几乎不变，缓存收益最高。
        project_context: L2 项目上下文层——repo 摘要/工程文档，跨会话稳定，
            支撑项目级缓存复用；为空时不产出该层。
        compact_context: 持久化的会话压缩快照，位于项目上下文之后、动态 RAG
            之前，使压缩后的历史成为可版本化的稳定缓存前缀。
        rag_context: L3 临时上下文层——RAG 检索结果/当前文件等，最易变；为空时
            不产出该层（当前实现默认为空，检索仍走工具，但该层与其独立缓存
            边界已就位）。

    说明：工具 schema（文档里的 L1）在 OpenAI 兼容接口中通过 `tools` 参数传递，
    已天然参与端点的缓存前缀计算，无需再占用一条 system 消息，故不单列为一层。
    """

    core: str
    project_context: str = ""
    structure_context: str = ""
    compact_context: str = ""
    rag_context: str = ""

    def layers(self) -> list[str]:
        """返回非空层的有序列表（核心 -> 项目 -> 压缩快照 -> RAG）。"""
        return [
            layer
            for layer in (
                self.core,
                self.project_context,
                self.structure_context,
                self.compact_context,
                self.rag_context,
            )
            if layer.strip()
        ]

    def to_text(self) -> str:
        """把各层拼成单一字符串（供 `agent.prompt` 等需要纯文本的场景）。"""
        return "\n\n".join(self.layers())

    def to_content_blocks(self) -> list[dict[str, Any]]:
        """把各层转成 OpenAI content-block 数组，每层一个文本块。

        放进 system 消息的 `content` 后，`message_transformer` 可为每个块末尾
        独立标记 `cache_control`，实现多断点分层缓存。
        """
        return [{"type": "text", "text": layer} for layer in self.layers()]


def build_layered_system_prompt(
    agent: AgentDefinition,
    skill_catalog: SkillCatalog | None = None,
    output_style_catalog: OutputStyleCatalog | None = None,
    output_style_id: str | None = None,
    project_context: str = "",
    structure_context: str = "",
    compact_context: str = "",
    rag_context: str = "",
) -> LayeredPrompt:
    """构造分层 system prompt：L0 复用 `build_system_prompt`，再叠加 L2/L3。

    Args:
        agent: 当前帧绑定的 agent 定义。
        skill_catalog: Skill 目录索引。
        output_style_catalog: OutputStyle 目录索引。
        output_style_id: 当前会话选定的 OutputStyle。
        project_context: L2 项目上下文文本；为空时不产出该层。
        compact_context: 当前帧的持久化压缩摘要；为空时不产出该层。
        rag_context: L3 临时上下文文本；为空时不产出该层。

    Returns:
        分层 prompt；`to_content_blocks()` 用于写入 system 消息以启用多断点缓存。
    """
    core = build_system_prompt(agent, skill_catalog, output_style_catalog, output_style_id)
    return LayeredPrompt(
        core=core,
        project_context=project_context.strip(),
        structure_context=structure_context.strip(),
        compact_context=compact_context.strip(),
        rag_context=rag_context.strip(),
    )
