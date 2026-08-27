"""压缩边界上的语义合并（任务 4.2）。

自动/手动压缩只在"整体上下文预算越界"或用户显式 /compact 时调用模型：

- **机械合并**（确定性回退，永不调用模型）由
  `app.context.memory.mechanical_merge_removed_messages` 提供：被收拢
  的旧消息转写为有界 Markdown 事实，不保留原始消息预览或工具 JSON；
- **语义合并**：仅当配置启用且处于压缩边界时，调用 quick 模型把记忆
  融合成连贯的 Markdown 段落；失败/空结果/禁用时自动回退机械合并。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.context.memory import mechanical_merge_removed_messages
from app.context.models import ContextMemoryState

__all__ = [
    "mechanical_merge_removed_messages",
    "render_memory_for_summary",
    "semantic_consolidate",
]

logger = logging.getLogger(__name__)


def render_memory_for_summary(state: ContextMemoryState) -> str:
    """把记忆状态渲染为语义合并的输入源（Markdown，不含原始 JSON）。"""
    lines: list[str] = []
    sections: list[tuple[str, list[str]]] = [
        ("目标", state.goals),
        ("约束", state.constraints),
        ("决定", state.decisions),
        ("已验证事实", state.facts),
        ("已完成工作", state.completed_work),
        ("待办工作", state.pending_work),
        ("助手承诺/说明", state.assistant_facts),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"## {title}")
        lines.extend(f"- {item}" for item in items)
    if state.editor_facts:
        lines.append("## 当前编辑器状态")
        lines.extend(
            f"- {fact.identity}: {fact.summary}" for fact in state.editor_facts.values()
        )
    if state.tool_records:
        lines.append("## 工具记忆")
        for record in state.tool_records:
            lines.append(f"### {record.tool_name} → {record.target}（{record.freshness}）")
            lines.append(record.markdown)
    return "\n".join(lines)


class _ConsolidationLLM(Protocol):
    """语义合并所需的最小 LLM 接口（与 LLMProvider.chat 兼容）。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        thinking_budget: int = 0,
    ) -> Any:
        """发起一次无工具的模型调用。"""
        ...


_CONSOLIDATE_INSTRUCTIONS = (
    "你是会话上下文压缩器。请把下面的 Markdown 记忆合并成简洁、忠实的中文 Markdown 记忆，"
    "严格按这些二级标题组织：## 目标 / ## 约束 / ## 决定 / ## 已验证事实 / ## 已完成工作 / "
    "## 待办工作。同一目标/事实只保留最新状态，删除已被取代的条目；不要编造，"
    "不要输出记忆之外的内容，不要输出任何解释。"
)


async def semantic_consolidate(
    state: ContextMemoryState,
    llm: _ConsolidationLLM,
    *,
    model: str | None,
) -> bool:
    """用 quick 模型把记忆语义融合成连贯段落（仅压缩边界调用）。

    成功时把模型输出按标题回填到会话事实区；失败/空输出时保持原状，由
    调用方继续使用机械合并结果。

    Returns:
        是否成功完成语义合并。
    """
    source = render_memory_for_summary(state)
    if not source.strip():
        return False
    try:
        turn = await llm.chat(
            messages=[
                {"role": "system", "content": _CONSOLIDATE_INSTRUCTIONS},
                {"role": "user", "content": source},
            ],
            tools=[],
            model=model,
            temperature=0.0,
            thinking_budget=0,
        )
    except Exception as exc:  # noqa: BLE001 - 合并失败必须回退机械路径，绝不中断请求
        logger.warning("Semantic consolidation failed, keeping mechanical merge: %s", exc)
        return False
    text = str(getattr(turn, "content", "") or "").strip()
    if not text:
        logger.warning("Semantic consolidation returned empty content; keeping mechanical merge")
        return False

    section_map: dict[str, list[str]] = {
        "目标": [],
        "约束": [],
        "决定": [],
        "已验证事实": [],
        "已完成工作": [],
        "待办工作": [],
    }
    current: list[str] | None = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if line.startswith("## "):
            title = line[3:].strip()
            current = section_map.get(title)
            continue
        if current is not None and line.startswith("- "):
            current.append(line[2:].strip())
    if not any(section_map.values()):
        logger.warning(
            "Semantic consolidation output had no parsable sections; keeping mechanical"
        )
        return False
    state.goals = section_map["目标"]
    state.constraints = section_map["约束"]
    state.decisions = section_map["决定"]
    state.facts = section_map["已验证事实"]
    state.completed_work = section_map["已完成工作"]
    state.pending_work = section_map["待办工作"]
    state.revision += 1
    return True
