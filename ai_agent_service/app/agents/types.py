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
        max_turns: 单次循环允许的最大轮数；不属于 `edit_map_max_turns` 覆盖范围的
            轮次（即本轮 tool_calls 不是单一 `edit_map` 调用）都计入这个预算。
        edit_map_max_turns: 单次循环中允许调用 `edit_map` 的轮数上限，与
            `max_turns` 分开计算；为 `None` 时 `edit_map` 调用同样计入
            `max_turns`（向后兼容旧行为）。用于地图编辑类 agent 把大范围地形
            拆成多批 `edit_map` 调用时，不被其余规划/校验轮次挤占预算。
        can_delegate: 是否拥有 `delegate` 编排能力；仅 coordinator 可为 True，
            且需要 `delegate` 工具与子 agent 注册表均已就位（M2+）。
        hooks: 声明式 Hook；当前支持 `on_start`——帧创建时追加到
            system prompt 末尾的提醒文本，由 `prompt/builder.py` 注入。
        effective_tools: 解析后的工具交集，由 `resolve_effective_tools` 填充。
        warnings: 解析过程中产生的告警（如声明了不存在的工具）。
        pipeline_kind: 稳定的工作流类别；权限和预算不得从展示名称推断。
        role: 稳定的编排角色；运行时权限不得从可改名的展示名称推断。
        map_stage: 地图流水线角色；非地图 Agent 为 None。
        worker_mode: 动态地图 Worker 的受控 mode；永久 Agent 为 None。
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
    edit_map_max_turns: int | None = None
    can_delegate: bool = False
    hooks: dict[str, str] | None = None
    workflow_operations: list[str] = field(default_factory=list)
    workflow_constraints: list[dict[str, Any]] = field(default_factory=list)
    # 稳定的编排元数据字段：
    # - pipeline_kind: 工作流类别（"general" / "map"），权限和预算不依赖展示名称
    # - role: 编排角色（"specialist" / "map_worker" 等），运行时权限判断用此字段
    # - map_stage: 地图流水线阶段（reader/planner/writer/reviewer/repairer），非地图 Agent 为 None
    # - worker_mode: 动态地图 Worker 的受控 mode（read_only/write_one_batch 等），永久 Agent 为 None
    pipeline_kind: str = "general"
    role: str = "specialist"
    map_stage: str | None = None
    worker_mode: str | None = None
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
class CompactSnapshot:
    """保存单个 Agent 帧的持久化会话压缩快照。

    Attributes:
        revision: 当前帧单调递增的压缩版本。
        digest: 规范化摘要文本的 SHA-256 指纹。
        summary: 不含 system content-block 包装的压缩摘要正文。
        created_at: UTC ISO-8601 创建时间。
        source_message_count: 本次摘要合并的旧消息数量。
        removed_message_count: 本次从活跃历史移除的消息数量。
        keep_recent: 压缩后保留的最近消息数量。
        estimated_tokens_before: 压缩前的预估 token 数。
        estimated_tokens_after: 压缩后的预估 token 数。
        triggered_by: 压缩来源，通常为 ``manual`` 或 ``auto``。
    """

    revision: int
    digest: str
    summary: str
    created_at: str
    source_message_count: int
    removed_message_count: int
    keep_recent: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    triggered_by: Literal["manual", "auto"]


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
        search_tools_noop_count: 本帧连续未激活新工具的 `search_tools` 次数。
        persistent_turn_count: 地图帧在当前 map revision 内累计的 LLM 轮数；
            前端工具回传造成的新 `run_turn` 调用不会清零。
        persistent_edit_map_turn_count: 当前 map revision 内累计的纯 `edit_map`
            轮数，与常规轮次预算分开计算。
        map_progress_revision: 上述预算对应的 map revision；真实写入推进 revision
            后预算自动进入新进展周期。
        forced_completion_text: 编排器已经确定当前子阶段应结束时保存的结构化
            收尾文本；`run_turn` 会在下一次调用 LLM 前消费它。
        force_text_only: 当前阶段事实已经齐全、下一轮只能输出最终文本时为 True。
        structured_attempt_count: 当前 Frame 已消费的结构化纠错次数。
        structured_correction_limit: 当前 Frame 允许的同 Frame 纠错上限。
        response_contract_mode: 最终回合使用的显式 provider 响应模式。
        response_contract_schema_digest: 当前专用 Schema 的安全稳定摘要。
        map_reader_detailed_region_ready: reader 已获得精确地图区域、只需完成一次
           必要 artifact 读取即可收尾时为 True。
        map_stage_contract: 地图子 Frame 的可信阶段、目标与 revision 合同；为空表示
            该 Frame 不参与结构化地图流水线。
        domain_owner_contract: 领域 owner 的宏观任务和 lineage 合同；不得包含地图
            worker schema、worker identity 或 specialist next stages。
        map_request_lineage_id: 创建该 Frame 的当前用户请求 lineage；用于防止
            历史地图任务的 Frame 或工具结果污染无关请求。
        map_task_id: 该 Frame 绑定的地图任务 id；非地图编辑请求为空。
        map_evidence: 当前 Frame 中由运行时记录的地图工具证据。
        compact_snapshot: 当前帧最近一次有效压缩的持久化快照；未压缩时为 None。
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
    search_tools_noop_count: int = 0
    history_anchor_frame_id: str | None = None
    history_anchor_message_index: int | None = None
    persistent_turn_count: int = 0
    persistent_edit_map_turn_count: int = 0
    map_progress_revision: int | None = None
    forced_completion_text: str | None = None
    force_text_only: bool = False
    structured_attempt_count: int = 0
    structured_correction_limit: int = 0
    response_contract_mode: Literal["json_schema", "json_object", "prompt_only"] | None = None
    response_contract_schema_digest: str | None = None
    structured_response_model: str | None = None
    structured_finish_reason: str | None = None
    structured_thinking_budget: int = 0
    structured_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    map_reader_detailed_region_ready: bool = False
    # 地图子 Frame 的可信阶段合同：包含阶段、目标路径、revision 等运行时可信信息，
    # 为空 dict 表示该 Frame 不参与结构化地图流水线
    map_stage_contract: dict[str, Any] = field(default_factory=dict)
    domain_owner_contract: dict[str, Any] = field(default_factory=dict)
    map_request_lineage_id: str | None = None
    map_task_id: str | None = None
    contract_id: str | None = None
    worker_instance_id: str | None = None
    result_schema: str | None = None
    allowed_next_stages: tuple[str, ...] = ()
    # 当前 Frame 中由运行时记录的地图工具证据（如截图引用、validation 结果等）
    map_evidence: list[dict[str, Any]] = field(default_factory=list)
    compact_snapshot: CompactSnapshot | None = None
