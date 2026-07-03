"""地图动态 worker 的最小服务层约束。"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
PipelineTemplate = Literal[
    "read_only_diagnosis",
    "platformer_extend",
    "background_fill",
    "object_placement",
    "repair_existing_map",
    "single_point_edit",
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
PIPELINE_TEMPLATE_IDS = frozenset(
    {
        "read_only_diagnosis",
        "platformer_extend",
        "background_fill",
        "object_placement",
        "repair_existing_map",
        "single_point_edit",
    }
)


@dataclass(frozen=True)
class PipelineStageSpec:
    """描述地图流水线模板中的一个受控阶段。"""

    stage: str
    preferred_agent: str
    worker_mode: str
    required_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineTemplateSpec:
    """描述地图流水线模板的服务层约束。"""

    template_id: str
    description: str
    stages: tuple[PipelineStageSpec, ...]
    required_parameters: tuple[str, ...] = ()


PIPELINE_TEMPLATES: dict[str, PipelineTemplateSpec] = {
    "read_only_diagnosis": PipelineTemplateSpec(
        template_id="read_only_diagnosis",
        description="只读诊断：读取地图、定位问题、输出可执行建议，不写入。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
    ),
    "platformer_extend": PipelineTemplateSpec(
        template_id="platformer_extend",
        description="横版平台扩图：读取边界、规划可达区域、单批写入、leap 验证、复核。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("planner", "map-planner-agent", "propose_only"),
            PipelineStageSpec("writer", "dynamic-map-worker", "write_one_batch"),
            PipelineStageSpec(
                "validator",
                "map-validator-agent",
                "review_only",
                ("validate_map_region",),
            ),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
        required_parameters=("movement_model=leap",),
    ),
    "background_fill": PipelineTemplateSpec(
        template_id="background_fill",
        description="背景补齐：读取缺口、规划覆盖、单批补齐、覆盖校验、复核。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("planner", "map-planner-agent", "propose_only"),
            PipelineStageSpec("writer", "dynamic-map-worker", "write_one_batch"),
            PipelineStageSpec(
                "validator",
                "map-validator-agent",
                "review_only",
                ("validate_layer_coverage",),
            ),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
    ),
    "object_placement": PipelineTemplateSpec(
        template_id="object_placement",
        description="对象放置：读取锚点、规划对象、单批放置、对象校验、复核。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("planner", "map-planner-agent", "propose_only"),
            PipelineStageSpec("writer", "dynamic-map-worker", "write_one_batch"),
            PipelineStageSpec(
                "validator",
                "map-validator-agent",
                "review_only",
                ("validate_object_placements",),
            ),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
    ),
    "repair_existing_map": PipelineTemplateSpec(
        template_id="repair_existing_map",
        description="修复已有地图：读取问题、提出修复、单批修复写入、验证、复核。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("planner", "map-planner-agent", "repair_propose"),
            PipelineStageSpec("repairer", "dynamic-map-worker", "repair_write_one_batch"),
            PipelineStageSpec("validator", "map-validator-agent", "review_only"),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
    ),
    "single_point_edit": PipelineTemplateSpec(
        template_id="single_point_edit",
        description="小范围单点编辑：读取目标、单批小改、验证、复核。",
        stages=(
            PipelineStageSpec("reader", "map-reader-agent", "read_only"),
            PipelineStageSpec("writer", "dynamic-map-worker", "write_one_batch"),
            PipelineStageSpec("validator", "map-validator-agent", "review_only"),
            PipelineStageSpec("reviewer", "map-reviewer-agent", "review_only"),
        ),
    ),
}


def is_map_write_tool(name: str) -> bool:
    """判断工具名是否属于地图写入工具。"""
    return name in MAP_WRITE_TOOL_NAMES


def is_map_worker_write_mode(mode: Any) -> bool:
    """判断 worker mode 是否属于地图写入 mode。"""
    return mode in MAP_WORKER_WRITE_MODES


def pipeline_template_ids() -> tuple[str, ...]:
    """返回当前支持的地图流水线模板 id。"""
    return tuple(sorted(PIPELINE_TEMPLATES))


def select_pipeline_template(objective: str, requested: Any = None) -> str:
    """按显式请求或目标文本选择地图流水线模板。"""
    if requested in PIPELINE_TEMPLATE_IDS:
        return str(requested)
    text = objective.lower()
    if any(word in text for word in ("platform", "platformer", "leap", "跳跃", "横版", "平台")):
        return "platformer_extend"
    if any(word in text for word in ("background", "water", "sky", "背景", "水面", "天空")):
        return "background_fill"
    if any(word in text for word in ("object", "placement", "anchor", "对象", "放置", "锚点")):
        return "object_placement"
    if any(word in text for word in ("repair", "fix", "修复", "报错", "不可达")):
        return "repair_existing_map"
    if any(
        word in text
        for word in (
            "single",
            "point",
            "tile",
            "edit",
            "write",
            "paint",
            "fill",
            "单点",
            "小范围",
            "单格",
            "编辑",
            "写入",
            "绘制",
            "填充",
        )
    ):
        return "single_point_edit"
    return "read_only_diagnosis"


def pipeline_required_parameters(template_id: str) -> tuple[str, ...]:
    """返回指定流水线模板要求的关键参数约束。"""
    template = PIPELINE_TEMPLATES.get(template_id)
    return template.required_parameters if template is not None else ()


def validate_map_write_args(args: dict[str, Any]) -> str | None:
    """校验地图写工具必需的批次与版本字段。"""
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
    output_schema = spec.get("output_schema")
    pipeline_template = spec.get("pipeline_template")
    stage_id = spec.get("stage_id")
    if not isinstance(name, str) or not name.strip():
        return "worker_spec.name 不能为空"
    if not isinstance(objective, str) or not objective.strip():
        return "worker_spec.objective 不能为空"
    if mode not in MAP_WORKER_MODES:
        return "worker_spec.mode 必须是受控地图 worker mode"
    if not isinstance(allowed_tools, list) or not allowed_tools:
        return "worker_spec.allowed_tools 不能为空"
    if output_schema != "map_worker_result_v1":
        return "worker_spec.output_schema 必须是 map_worker_result_v1"
    if pipeline_template is not None and pipeline_template not in PIPELINE_TEMPLATE_IDS:
        return "worker_spec.pipeline_template 必须是受支持的地图流水线模板"
    pipeline_template = select_pipeline_template(str(objective), pipeline_template)
    spec["pipeline_template"] = pipeline_template
    if stage_id is not None and (not isinstance(stage_id, str) or not stage_id.strip()):
        return "worker_spec.stage_id 必须是非空字符串"
    if not any(stage.worker_mode == mode for stage in PIPELINE_TEMPLATES[pipeline_template].stages):
        return "worker_spec.mode 与 pipeline_template 阶段不匹配"

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

    prompt = _dynamic_map_worker_prompt(spec, sorted(effective))
    worker = AgentDefinition(
        name=name.strip(),
        source="project",
        description=(
            "一次性地图动态 worker "
            f"pipeline_template={pipeline_template} stage_id={stage_id or ''}"
        ),
        prompt=prompt,
        tools=sorted(effective),
        model=parent.model,
        effort=parent.effort,
        max_turns=max_turns,
        edit_map_max_turns=1 if is_map_worker_write_mode(mode) else None,
        can_delegate=False,
    )
    return resolve_effective_tools(worker, set(REGISTRY))


def _dynamic_map_worker_prompt(spec: dict[str, Any], tools: list[str]) -> str:
    """生成动态地图 worker 的系统提示词。"""
    return (
        "你是一次性 Godot 地图 worker，只为当前委派帧存在。\n\n"
        f"worker_spec:\n{spec}\n\n"
        f"可用流水线模板：{', '.join(pipeline_template_ids())}。\n"
        f"服务层已裁剪工具：{', '.join(tools)}。\n"
        "规则：\n"
        "- 严格执行 objective，不扩大范围。\n"
        "- can_delegate=false；禁止调用 delegate、delegate_many、create_plan。\n"
        "- 非写入 mode 禁止写地图；写入 mode 同一轮只能调用一个地图写工具。\n"
        "- 地图写工具必须携带 expected_revision；write_batch_id/worker/frame/mode 由服务层补齐。\n"
        "- 写入后必须把 next_stage 设为 validator 或 reviewer，不得直接宣布完成。\n"
        "- 最终只输出 JSON，schema 为 map_worker_result_v1。\n"
        "- JSON 至少包含 stage、worker、mode、objective、target_path、map_layer、"
        "map_revision、region、summary、facts、proposed_batches、write_results、"
        "validation、missing_inputs、risks、next_stage。"
    )


def restore_project_agent(data: dict[str, Any], available_tools: set[str]) -> AgentDefinition:
    """从会话持久化数据恢复一次性 project agent。"""
    agent = AgentDefinition(
        name=str(data.get("name", "dynamic-map-worker")),
        source="project",
        description=str(data.get("description", "")),
        prompt=str(data.get("prompt", "")),
        tools=[str(tool) for tool in data.get("tools", []) if isinstance(tool, str)],
        model=str(data.get("model", "inherit")),
        effort=data.get("effort", "standard"),
        max_turns=int(data.get("max_turns", 6)),
        edit_map_max_turns=data.get("edit_map_max_turns"),
        can_delegate=False,
    )
    return resolve_effective_tools(replace(agent, can_delegate=False), available_tools)
