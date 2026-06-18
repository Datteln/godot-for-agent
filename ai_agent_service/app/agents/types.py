"""Agent 数据模型：`AgentDefinition` 与 `Frame`（§6.4 / 详设 A §2.2）。

`AgentDefinition` 是 Claude Code 同构的 markdown frontmatter + body 模型，
由 `app/agents/loader.py::load_agent_file` 从 `app/agents/agent_defs/*.md`
解析得到，再经 `app/agents/bundled.py` 注册为内置 agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

EffortLevel = Literal["quick", "standard", "deep", "verify", "advisor"]

# 与 `EffortLevel` 字面量保持一致，供运行时校验（如 `agents/loader.py`）使用。
EFFORT_LEVELS: tuple[EffortLevel, ...] = ("quick", "standard", "deep", "verify", "advisor")


@dataclass(frozen=True)
class AgentDefinition:
    """单个 agent 的定义：元数据 + system prompt + 工具裁剪结果。

    Attributes:
        name: agent 名，kebab-case，例如 `coordinator`、`programming-agent`。
        source: 来源层级，决定信任与覆盖规则。
        description: coordinator 决定是否委派时参考的简述。
        prompt: markdown body，作为该 agent 的 system prompt。
        tools: 声明的工具白名单；`None` 或 `["*"]` 表示"当前上下文可见工具"。
        disallowed_tools: 额外 denylist，优先级高于 `tools`。
        skills: agent 启动时预加载的 Skill 名称（M1+ 生效）。
        model: 模型档位；`"inherit"` 表示沿用会话默认模型。
        effort: 任务档位（§6.5），决定 `run_turn` 传给 `LLMProvider.chat()`
            的 `temperature`（见 `orchestrator/agent.py::EFFORT_TEMPERATURE`）。
            根帧可被 `Session.effort` 覆盖，委派子帧始终使用各自的声明值。
        max_turns: 单次循环允许的最大轮数。
        can_delegate: 是否拥有 `delegate` 编排能力；仅 coordinator 可为 True，
            且需要 `delegate` 工具与子 agent 注册表均已就位（M2+）。
        hooks: 声明式 Hook；当前支持 `on_start`——帧创建时追加到
            system prompt 末尾的提醒文本，由 `prompt/builder.py` 注入。
        effective_tools: 解析后的工具交集，由 `resolve_effective_tools` 填充。
        warnings: 解析过程中产生的告警（如声明了不存在的工具）。
    """

    name: str
    source: Literal["bundled", "user", "project", "plugin"]
    description: str
    prompt: str
    tools: list[str] | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    model: str | None = "inherit"
    effort: EffortLevel = "standard"
    max_turns: int = 12
    can_delegate: bool = False
    hooks: dict[str, str] | None = None
    effective_tools: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_effective_tools(agent: AgentDefinition, available_tools: set[str]) -> AgentDefinition:
    """把 `tools`/`disallowed_tools` 与当前入口可见工具集合求交集。

    `tools` 为 `None` 或 `["*"]` 时表示"当前上下文可见工具"，不代表全局注册表；
    `disallowed_tools` 始终优先剔除。声明了但当前不可见的工具只记录告警，
    不视为错误（§6.4 落地规则）。

    Args:
        agent: 原始 agent 定义（`effective_tools`/`warnings` 通常为空）。
        available_tools: 当前入口/权限模式下实际可见的工具名集合。

    Returns:
        填充了 `effective_tools`（按名称排序）与 `warnings` 的新
        `AgentDefinition`（原对象不可变，返回副本）。
    """
    if agent.tools is None or agent.tools == ["*"]:
        base = set(available_tools)
    else:
        base = set(agent.tools) & available_tools

    effective = sorted(base - set(agent.disallowed_tools))

    warnings: list[str] = []
    if agent.tools is not None and agent.tools != ["*"]:
        missing = sorted(set(agent.tools) - available_tools)
        if missing:
            warnings.append(f"以下声明的工具在当前入口不可见，已忽略：{', '.join(missing)}")

    return replace(agent, effective_tools=effective, warnings=warnings)


@dataclass
class Frame:
    """Agent 帧：会话内 `agent_stack` 的一个元素（§6.2 / 详设 A §2.2）。

    Attributes:
        id: 帧 id，例如 `"f1"`，前端工具调用以此标注来源帧。
        agent: 该帧绑定的 agent 定义（含已解析的 `effective_tools`）。
        messages: 该帧独立维护的对话上下文（OpenAI message dict 列表）。
        parent_id: 父帧 id；根帧（coordinator）为 None。
        pending_delegate_call_id: 若该帧是被 `delegate` 创建的子帧，记录父帧
            那条 `delegate` tool_call 的 id，子帧结束后用它回填父帧。
        pending_delegate_group_id: 若该帧属于 `delegate_many` 顺序子任务组，
            记录组 id；组内所有子帧结束后统一回填父帧。
        status: 帧状态：运行中/挂起等待前端/已结束。
        depth: 帧深度，根帧为 0，供 `MAX_DEPTH` 防御性约束使用（M2+）。
        active_deferred_tools: 本帧通过 `search_tools` 激活的 deferred 工具名；
            只在本帧内生效，不提升权限、不跨 agent 继承。
    """

    id: str
    agent: AgentDefinition
    messages: list[dict[str, Any]]
    parent_id: str | None = None
    pending_delegate_call_id: str | None = None
    pending_delegate_group_id: str | None = None
    status: Literal["running", "suspended", "done"] = "running"
    depth: int = 0
    active_deferred_tools: set[str] = field(default_factory=set)
    history_anchor_frame_id: str | None = None
    history_anchor_message_index: int | None = None
