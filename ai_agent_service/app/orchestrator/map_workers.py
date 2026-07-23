"""地图动态 worker 的最小服务层约束。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from app.agents.types import AgentDefinition, resolve_effective_tools
from app.tools.registry import REGISTRY

MapWorkerMode = Literal[
    "read_only",
    "propose_only",
    "write_one_batch",
    "review_only",
    "repair_propose",
    "repair_write_one_batch",
]
MAP_WRITE_TOOL_NAMES = frozenset(
    {
        "edit_map",
        "paint_terrain_connect",
        "place_map_objects",
        "repair_placements",
        "repair_layer_coverage",
        "repair_map_region",
        "compact_spatial_index",
        "write_resource_registry",
        "save_map_blueprint",
        "apply_map_blueprint",
        "ensure_standard_map_layers",
        "fill_rect",
        "paint_from_image_grid",
    }
)
MAP_REVISION_GUARDED_TOOL_NAMES = MAP_WRITE_TOOL_NAMES - frozenset({"write_resource_registry"})
MAP_TARGET_REQUIRED_TOOL_NAMES = frozenset(
    {
        "edit_map",
        "paint_terrain_connect",
        "place_map_objects",
        "repair_placements",
        "repair_layer_coverage",
        "repair_map_region",
        "save_map_blueprint",
        "apply_map_blueprint",
        "fill_rect",
        "paint_from_image_grid",
    }
)
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


def is_map_write_tool(name: str) -> bool:
    """判断工具名是否属于地图写入工具。"""
    return name in MAP_WRITE_TOOL_NAMES


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
    allowed_tools = spec.get("allowed_tools")
    requested_skills = spec.get("skills", [])
    operations = _workflow_strings(spec.get("operations"), "operations")
    constraints = _workflow_constraints(spec.get("constraints", []))
    output_schema = spec.get("output_schema")
    stage_id = spec.get("stage_id")
    if not isinstance(name, str) or not name.strip():
        return "worker_spec.name 不能为空"
    if not isinstance(objective, str) or not objective.strip():
        return "worker_spec.objective 不能为空"
    if mode not in MAP_WORKER_MODES:
        return "worker_spec.mode 必须是受控地图 worker mode"
    if not isinstance(allowed_tools, list) or not allowed_tools:
        return "worker_spec.allowed_tools 不能为空"
    if isinstance(operations, str):
        return operations
    if isinstance(constraints, str):
        return constraints
    if not isinstance(requested_skills, list) or not all(
        isinstance(skill, str) for skill in requested_skills
    ):
        return "worker_spec.skills 必须是字符串数组"
    if output_schema != "map_worker_result_v1":
        return "worker_spec.output_schema 必须是 map_worker_result_v1"
    skills = tuple(dict.fromkeys(requested_skills))
    if stage_id is not None and (not isinstance(stage_id, str) or not stage_id.strip()):
        return "worker_spec.stage_id 必须是非空字符串"

    parent_tools = set(parent.effective_tools)
    requested_tools = {str(tool) for tool in allowed_tools if isinstance(tool, str)}
    if not requested_tools:
        return "worker_spec.allowed_tools 必须包含工具名字符串"

    denied = requested_tools - parent_tools
    if denied:
        return f"动态 worker 工具不能超过父 agent 权限：{', '.join(sorted(denied))}"

    effective = requested_tools & set(REGISTRY)
    if not is_map_worker_write_mode(mode):
        effective -= MAP_WRITE_TOOL_NAMES
    else:
        effective = {
            tool_name
            for tool_name in effective
            if tool_name in MAP_WRITE_TOOL_NAMES or not REGISTRY[tool_name].mutating
        }
    effective -= {"delegate", "delegate_many", "create_plan"}
    if not effective:
        return "worker_spec.allowed_tools 经 mode 裁剪后为空"

    max_turns = spec.get("max_turns", 6)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int):
        max_turns = 6
    max_turns = max(1, min(max_turns, 12))

    prompt = _dynamic_map_worker_prompt(spec, sorted(effective), skills, operations, constraints)
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
    )
    return resolve_effective_tools(worker, set(REGISTRY))


def _dynamic_map_worker_prompt(
    spec: dict[str, Any],
    tools: list[str],
    skills: tuple[str, ...],
    operations: list[str],
    constraints: list[dict[str, Any]],
) -> str:
    """生成动态地图 worker 的系统提示词。"""
    return (
        "你是一次性 Godot 地图 worker，只为当前委派帧存在。\n\n"
        f"worker_spec:\n{spec}\n\n"
        f"已绑定 Skill：{', '.join(skills)}。这些 Skill 会在 worker prompt 中预加载。\n"
        f"本阶段操作：{', '.join(operations)}。\n"
        f"写后约束：{constraints}。\n"
        f"服务层已裁剪工具：{', '.join(tools)}。\n"
        "规则：\n"
        "- 严格执行 objective，不扩大范围。\n"
        "- can_delegate=false；禁止调用 delegate、delegate_many、create_plan。\n"
        "- 非写入 mode 禁止写地图；写入 mode 可在同一轮提交多个有序地图写工具，"
        "但本轮不得混入读取、验证或服务端工具；服务层会逐批执行并在失败时停止队列。\n"
        "- 改地图区域的写工具必须携带 expected_revision；write_batch_id/worker/frame/mode 由服务层补齐。\n"
        "- 写入后必须把 next_stage 设为 validator 或 reviewer，不得直接宣布完成。\n"
        "- 最终只输出 JSON，schema 为 map_worker_result_v1。\n"
        "- JSON 至少包含 stage、worker、mode、objective、target_path、map_layer、"
        "map_revision、region、summary、facts、proposed_batches、write_results、"
        "validation、missing_inputs、risks、next_stage。\n\n"
        "- map_layer 必须是单个整数索引，或读取多层时使用非空整数数组；"
        "禁止输出图层名称或说明文字。\n\n"
        "【错误恢复规则】\n"
        "1. map_revision_conflict（地图已变更）：\n"
        "   - 服务层会自动触发 map-reader-agent 重读冲突区域\n"
        "   - 重读结果会包含 next_expected_revision 字段\n"
        "   - 你必须从重读结果中提取 next_expected_revision 值，作为下一次写入的 expected_revision\n"
        "   - 禁止使用旧的 expected_revision 值重试\n"
        "2. cell_count_mismatch（预期格数不符）：\n"
        "   - 错误信息会告诉你实际应该写多少格（actual_cells）\n"
        "   - 计算公式：x=A..B 的列数是 (B - A + 1)，不是 (B - A)\n"
        "   - 示例：x=64..86 是 23 列，y=21..23 是 3 行，总计 23×3=69 格\n"
        "   - 重试时必须把 expected_cells 设为错误信息中的 actual_cells 值\n"
        "3. 连续失败处理：\n"
        "   - 如果同一类型错误连续出现 2 次，必须切换策略或提前终止\n"
        "   - 禁止用相同参数盲目重试第 3 次\n"
    )


def restore_project_agent(data: dict[str, Any], available_tools: set[str]) -> AgentDefinition:
    """从会话持久化数据恢复一次性 project agent。"""
    agent = AgentDefinition(
        name=str(data.get("name", "dynamic-map-worker")),
        source="project",
        description=str(data.get("description", "")),
        prompt=str(data.get("prompt", "")),
        tools=[str(tool) for tool in data.get("tools", []) if isinstance(tool, str)],
        skills=[str(skill) for skill in data.get("skills", []) if isinstance(skill, str)],
        workflow_operations=[
            str(operation)
            for operation in data.get("workflow_operations", [])
            if isinstance(operation, str)
        ],
        workflow_constraints=[
            dict(constraint)
            for constraint in data.get("workflow_constraints", [])
            if isinstance(constraint, dict)
        ],
        model=str(data.get("model", "inherit")),
        effort=data.get("effort", "standard"),
        max_turns=int(data.get("max_turns", 6)),
        edit_map_max_turns=data.get("edit_map_max_turns"),
        can_delegate=False,
    )
    return resolve_effective_tools(replace(agent, can_delegate=False), available_tools)
