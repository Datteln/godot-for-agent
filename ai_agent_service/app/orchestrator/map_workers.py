"""地图动态 worker 的最小服务层约束。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from app.agents.types import AgentDefinition, resolve_effective_tools
from app.orchestrator.map_capabilities import (
    MAP_TOOL_CAPABILITIES,
    map_tools_for_worker_mode,
    map_tools_in_category,
)
from app.orchestrator.map_contracts import MAP_WORKER_RESULT_SCHEMA
from app.tools.registry import REGISTRY

MapWorkerMode = Literal[
    "read_only",
    "propose_only",
    "write_one_batch",
    "review_only",
    "repair_propose",
    "repair_write_one_batch",
]
# 按能力类别从 MAP_TOOL_CAPABILITIES 查询得到的工具集合，
# 不再硬编码工具名，而是由 map_capabilities 统一声明、此处统一消费，
# 确保新增工具只需在能力声明里注册即可自动进入对应的权限集合。
MAP_CONTENT_WRITE_TOOL_NAMES = map_tools_in_category("content_write")
MAP_RESOURCE_WRITE_TOOL_NAMES = map_tools_in_category("resource_write")
MAP_INDEX_WRITE_TOOL_NAMES = map_tools_in_category("index_write")
MAP_TEMPLATE_WRITE_TOOL_NAMES = map_tools_in_category("template_write")
MAP_STRUCTURE_WRITE_TOOL_NAMES = map_tools_in_category("structure_write")
# 辅助写入 = 资源/索引/模板/结构四类写入的并集
MAP_AUX_WRITE_TOOL_NAMES = (
    MAP_RESOURCE_WRITE_TOOL_NAMES
    | MAP_INDEX_WRITE_TOOL_NAMES
    | MAP_TEMPLATE_WRITE_TOOL_NAMES
    | MAP_STRUCTURE_WRITE_TOOL_NAMES
)
# 全部地图写入工具 = 内容写入 + 辅助写入
MAP_WRITE_TOOL_NAMES = MAP_CONTENT_WRITE_TOOL_NAMES | MAP_AUX_WRITE_TOOL_NAMES
# 需要 revision 守卫的工具：从能力声明的 requires_revision 字段派生
MAP_REVISION_GUARDED_TOOL_NAMES = frozenset(
    name
    for name, capability in MAP_TOOL_CAPABILITIES.items()
    if capability.requires_revision
)
# 需要 target_path 的工具：从能力声明的 requires_target 字段派生
MAP_TARGET_REQUIRED_TOOL_NAMES = frozenset(
    name for name, capability in MAP_TOOL_CAPABILITIES.items() if capability.requires_target
)
# 验证类与规划类工具集合
MAP_VALIDATION_TOOL_NAMES = map_tools_in_category("validation")
MAP_PLAN_TOOL_NAMES = map_tools_in_category("plan") | map_tools_in_category(
    "platform_plan"
)
PLATFORM_PLAN_TOOL_NAMES = map_tools_in_category("platform_plan")
MAP_WORKER_MODES = frozenset(
    {
        "read_only",
        "propose_only",
        "write_one_batch",
        "review_only",
        "repair_propose",
        "repair_write_one_batch",
    }
)
MAP_WORKER_WRITE_MODES = frozenset({"write_one_batch", "repair_write_one_batch"})
# 动态 Worker mode → 地图流水线阶段的固定映射，
# 确保编排层按 mode 而非展示名称判断权限与预算。
MAP_WORKER_MODE_STAGES = {
    "read_only": "reader",
    "propose_only": "planner",
    "write_one_batch": "writer",
    "review_only": "reviewer",
    "repair_propose": "planner",
    "repair_write_one_batch": "repairer",
}


def is_map_write_tool(name: str) -> bool:
    """判断工具名是否会直接修改地图内容。"""
    return name in MAP_CONTENT_WRITE_TOOL_NAMES


def requires_map_revision(name: str) -> bool:
    """判断地图写工具是否必须携带地图版本号。"""
    return name in MAP_REVISION_GUARDED_TOOL_NAMES


def is_map_worker_write_mode(mode: Any) -> bool:
    """判断 worker mode 是否属于地图写入 mode。"""
    return mode in MAP_WORKER_WRITE_MODES


def _workflow_strings(value: Any, field_name: str) -> list[str] | str:
    """校验 workflow 中的字符串数组字段。"""
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return f"worker_spec.{field_name} 必须是非空字符串数组"
    return list(dict.fromkeys(item.strip() for item in value))


def _workflow_constraints(value: Any) -> list[dict[str, Any]] | str:
    """校验并规整写后必须满足的通用验证约束。"""
    if not isinstance(value, list):
        return "worker_spec.constraints 必须是数组"
    constraints: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return "worker_spec.constraints 的每项必须是对象"
        validator = item.get("validator")
        required_args = item.get("required_args", {})
        if not isinstance(validator, str) or not validator.strip():
            return "workflow constraint.validator 必须是非空字符串"
        if not isinstance(required_args, dict):
            return "workflow constraint.required_args 必须是对象"
        constraints.append({"validator": validator.strip(), "required_args": dict(required_args)})
    return constraints


def validate_map_write_args(name: str, args: dict[str, Any]) -> str | None:
    """校验地图写工具必需的批次与版本字段。"""
    if not requires_map_revision(name):
        return None
    if name in MAP_TARGET_REQUIRED_TOOL_NAMES:
        target_path = args.get("target_path")
        if not isinstance(target_path, str) or not target_path.strip():
            return f"{name} 必须提供非空 target_path；" "禁止对地图写入静默使用 __selected_map__"
    expected_revision = args.get("expected_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        return "地图写工具必须提供整数 expected_revision"
    return None


def build_dynamic_map_worker(
    parent: AgentDefinition,
    spec: dict[str, Any],
) -> AgentDefinition | str:
    """根据受控 worker spec 生成一次性地图 worker 定义。"""
    spec = dict(spec)
    name = spec.get("name")
    objective = spec.get("objective")
    mode = spec.get("mode")
    requested_skills = spec.get("skills", [])
    operations = _workflow_strings(spec.get("operations"), "operations")
    constraints = _workflow_constraints(spec.get("constraints", []))
    output_schema = spec.get("output_schema")
    approved_batch = spec.get("approved_batch")
    stage_id = spec.get("stage_id")
    if not isinstance(name, str) or not name.strip():
        return "worker_spec.name 不能为空"
    if not isinstance(objective, str) or not objective.strip():
        return "worker_spec.objective 不能为空"
    if mode not in MAP_WORKER_MODES:
        return "worker_spec.mode 必须是受控地图 worker mode"
    if isinstance(operations, str):
        return operations
    if isinstance(constraints, str):
        return constraints
    if not isinstance(requested_skills, list) or not all(
        isinstance(skill, str) for skill in requested_skills
    ):
        return "worker_spec.skills 必须是字符串数组"
    if output_schema != MAP_WORKER_RESULT_SCHEMA:
        return f"worker_spec.output_schema 必须是 {MAP_WORKER_RESULT_SCHEMA}"
    if mode in MAP_WORKER_WRITE_MODES:
        if not isinstance(approved_batch, dict):
            return "写入 worker 必须提供 planner/validator 的 approved_batch artifact"
        artifact_ref = approved_batch.get("artifact_ref")
        batch_id = approved_batch.get("batch_id")
        target_path = approved_batch.get("target_path")
        map_revision = approved_batch.get("map_revision")
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            return "worker_spec.approved_batch.artifact_ref 不能为空"
        if not isinstance(batch_id, str) or not batch_id.strip():
            return "worker_spec.approved_batch.batch_id 不能为空"
        if not isinstance(target_path, str) or not target_path.strip():
            return "worker_spec.approved_batch.target_path 不能为空"
        if isinstance(map_revision, bool) or not isinstance(map_revision, int):
            return "worker_spec.approved_batch.map_revision 必须是整数"
    skills = tuple(dict.fromkeys(requested_skills))
    if stage_id is not None and (not isinstance(stage_id, str) or not stage_id.strip()):
        return "worker_spec.stage_id 必须是非空字符串"

    # mode 合同本身就是 worker 的能力边界；所有动态 worker 都从该合同与注册表
    # 派生初始工具集，避免父 agent 为编排而精简的工具集误删 reader/reviewer
    # 需要的数据读取工具。编排类工具仍在下方统一剥除。
    mode_tools = set(map_tools_for_worker_mode(str(mode)))
    effective = mode_tools & set(REGISTRY)
    # 编排类工具一律剔除，worker 不可再委派或创建计划
    effective -= {"delegate", "delegate_many", "create_plan"}
    if not effective:
        return "worker_spec.mode 在当前能力合同下没有可用工具"

    max_turns = spec.get("max_turns", 6)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int):
        max_turns = 6
    max_turns = max(1, min(max_turns, 12))

    prompt = _dynamic_map_worker_prompt(spec)
    worker = AgentDefinition(
        name=name.strip(),
        source="project",
        description=("一次性地图动态 worker " f"stage_id={stage_id or ''}"),
        prompt=prompt,
        tools=sorted(effective),
        model=parent.model,
        effort=parent.effort,
        max_turns=max_turns,
        edit_map_max_turns=1 if is_map_worker_write_mode(mode) else None,
        can_delegate=False,
        skills=list(skills),
        workflow_operations=operations,
        workflow_constraints=constraints,
        # 稳定的编排元数据：权限和预算只从这些字段推断，不依赖可改名的 name
        pipeline_kind="map",
        role="map_worker",
        map_stage=MAP_WORKER_MODE_STAGES[mode],
        worker_mode=mode,
    )
    return resolve_effective_tools(worker, set(REGISTRY))


def _read_only_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图事实采集 worker。\n"
        f"任务：{objective}\n"
        "收集完成任务所需的最小、可核实地图事实，并清楚标出仍然缺失的信息。"
    )


def _propose_only_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图方案 worker。\n"
        f"任务：{objective}\n"
        "基于已提供的真实事实提出边界明确、可检查的最小修改方案。"
    )


def _write_one_batch_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图批次执行 worker。\n"
        f"任务：{objective}\n"
        "准确执行已批准的修改，并如实报告可观察结果。"
    )


def _review_only_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图视觉复核 worker。\n"
        f"任务：{objective}\n"
        "检查指定视口或区域，报告可见问题及其直接证据。"
    )


def _repair_propose_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图修复方案 worker。\n"
        f"任务：{objective}\n"
        "针对已确认的问题提出最小修复方案，不扩大修改范围。"
    )


def _repair_write_one_batch_worker_prompt(objective: str) -> str:
    return (
        "你是一次性 Godot 地图修复执行 worker。\n"
        f"任务：{objective}\n"
        "准确执行已批准的最小修复，并如实报告可观察结果。"
    )


_MAP_WORKER_PROMPT_BUILDERS = {
    "read_only": _read_only_worker_prompt,
    "propose_only": _propose_only_worker_prompt,
    "write_one_batch": _write_one_batch_worker_prompt,
    "review_only": _review_only_worker_prompt,
    "repair_propose": _repair_propose_worker_prompt,
    "repair_write_one_batch": _repair_write_one_batch_worker_prompt,
}


def _dynamic_map_worker_prompt(spec: dict[str, Any]) -> str:
    """只生成 mode 对应的任务指导；执行合同由运行时独立注入和校验。"""
    mode = str(spec.get("mode", ""))
    objective = str(spec.get("objective", "")).strip()
    return _MAP_WORKER_PROMPT_BUILDERS[mode](objective)


def restore_project_agent(data: dict[str, Any], available_tools: set[str]) -> AgentDefinition:
    """从会话持久化数据恢复一次性 project agent。"""
    workflow_operations = [
        str(operation)
        for operation in data.get("workflow_operations", [])
        if isinstance(operation, str)
    ]
    edit_map_max_turns = data.get("edit_map_max_turns")
    tools = [str(tool) for tool in data.get("tools", []) if isinstance(tool, str)]
    if workflow_operations and edit_map_max_turns is not None:
        tools = [tool for tool in tools if tool in MAP_WRITE_TOOL_NAMES]
    agent = AgentDefinition(
        name=str(data.get("name", "dynamic-map-worker")),
        source="project",
        description=str(data.get("description", "")),
        prompt=str(data.get("prompt", "")),
        tools=tools,
        skills=[str(skill) for skill in data.get("skills", []) if isinstance(skill, str)],
        workflow_operations=workflow_operations,
        workflow_constraints=[
            dict(constraint)
            for constraint in data.get("workflow_constraints", [])
            if isinstance(constraint, dict)
        ],
        # 恢复稳定的编排元数据字段；缺省值与 AgentDefinition 默认值对齐
        pipeline_kind=str(data.get("pipeline_kind", "map")),
        role=str(data.get("role", "map_worker")),
        map_stage=(str(data["map_stage"]) if data.get("map_stage") else None),
        worker_mode=(str(data["worker_mode"]) if data.get("worker_mode") else None),
        model=str(data.get("model", "inherit")),
        effort=data.get("effort", "standard"),
        max_turns=int(data.get("max_turns", 6)),
        edit_map_max_turns=edit_map_max_turns,
        can_delegate=False,
    )
    return resolve_effective_tools(replace(agent, can_delegate=False), available_tools)
