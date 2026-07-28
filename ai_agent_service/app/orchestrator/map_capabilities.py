"""集中声明地图工具类别、阶段、Worker mode 与 revision 能力合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping


@dataclass(frozen=True)
class MapToolCapability:
    """描述一个工具在地图工作流中的稳定运行时能力。"""

    category: str
    stages: frozenset[str]
    worker_modes: frozenset[str] = frozenset()
    writes_map: bool = False
    requires_target: bool = False
    requires_revision: bool = False


_ORCHESTRATOR_STAGES = frozenset({"read", "plan", "write", "validate", "review"})
_READ_MODES = frozenset({"read_only"})
_PLAN_MODES = frozenset({"propose_only", "repair_propose"})
_WRITE_MODE = frozenset({"write_one_batch"})
_REPAIR_WRITE_MODE = frozenset({"repair_write_one_batch"})
_REVIEW_MODE = frozenset({"review_only"})


def _capability(
    category: str,
    stages: set[str],
    worker_modes: frozenset[str] = frozenset(),
    *,
    writes_map: bool = False,
    requires_target: bool = False,
    requires_revision: bool = False,
) -> MapToolCapability:
    """创建不可变地图工具能力记录。"""
    return MapToolCapability(
        category=category,
        stages=frozenset(stages),
        worker_modes=worker_modes,
        writes_map=writes_map,
        requires_target=requires_target,
        requires_revision=requires_revision,
    )


MAP_TOOL_CAPABILITIES: Final[dict[str, MapToolCapability]] = {
    "delegate": _capability("orchestration", set(_ORCHESTRATOR_STAGES)),
    "delegate_many": _capability("orchestration", set(_ORCHESTRATOR_STAGES)),
    "load_skill": _capability(
        "orchestration", {"read", "plan", "write", "validate", "diagnostic", "review"}
    ),
    "search_tools": _capability("orchestration", {"read", "plan", "write", "validate"}),
    "read_delegate_artifact": _capability(
        "artifact_read",
        {"read", "plan", "write", "validate", "diagnostic", "review"},
        _READ_MODES | _PLAN_MODES | _WRITE_MODE | _REPAIR_WRITE_MODE | _REVIEW_MODE,
    ),
    "read_map_artifact": _capability(
        "artifact_read",
        {"read", "plan", "write", "validate", "diagnostic", "review"},
        _READ_MODES | _PLAN_MODES | _WRITE_MODE | _REPAIR_WRITE_MODE | _REVIEW_MODE,
    ),
    "describe_map_context": _capability("context_read", {"read"}, _READ_MODES),
    "describe_map_region": _capability(
        "context_read",
        {"read", "plan", "write", "validate", "diagnostic", "review"},
        _READ_MODES | _PLAN_MODES | _REVIEW_MODE,
    ),
    "describe_tilemap_selection": _capability("context_read", {"read"}, _READ_MODES),
    "read_scene_tree": _capability(
        "context_read", {"read", "review"}, _READ_MODES | _REVIEW_MODE
    ),
    "read_file": _capability(
        "context_read",
        {"read", "plan", "write", "validate", "diagnostic"},
        _READ_MODES | _PLAN_MODES,
    ),
    "read_class_docs": _capability(
        "context_read", {"read", "plan", "validate", "review"}, _READ_MODES | _PLAN_MODES
    ),
    "read_image_metadata": _capability(
        "context_read", {"read", "review"}, _READ_MODES | _REVIEW_MODE
    ),
    "capture_viewport_screenshot": _capability(
        "evidence", {"read", "plan", "review"}, _READ_MODES | _PLAN_MODES | _REVIEW_MODE
    ),
    "query_spatial_index": _capability(
        "context_read",
        {"read", "plan", "write", "validate", "diagnostic"},
        _READ_MODES | _PLAN_MODES | _REVIEW_MODE,
    ),
    "convert_map_coords": _capability("context_read", {"read"}, _READ_MODES),
    "find_placement_anchors": _capability(
        "context_read", {"plan", "write"}, _PLAN_MODES
    ),
    "plan_map_layout": _capability("plan", {"plan"}, _PLAN_MODES),
    "plan_map_algorithms": _capability("plan", {"plan"}, _PLAN_MODES),
    "validate_platform_level_plan": _capability("platform_plan", {"plan"}, _PLAN_MODES),
    "plan_reachable_map_growth": _capability("platform_plan", {"plan"}, _PLAN_MODES),
    "compute_reachable_frontier": _capability("plan", {"plan"}, _PLAN_MODES),
    "sample_poisson_points": _capability("plan", {"plan"}, _PLAN_MODES),
    "sample_noise_grid": _capability("plan", {"plan"}, _PLAN_MODES),
    "compose_map_blueprint_grammar": _capability("plan", {"plan"}, _PLAN_MODES),
    "validate_map_region": _capability(
        "validation", {"validate", "diagnostic", "review"}, _REVIEW_MODE
    ),
    "validate_layer_coverage": _capability(
        "validation", {"validate", "review"}, _REVIEW_MODE
    ),
    "validate_object_placements": _capability(
        "validation", {"validate", "review"}, _REVIEW_MODE
    ),
    "save_scene": _capability("scene_commit", {"review"}),
    "edit_map": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "paint_terrain_connect": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "place_map_objects": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "apply_map_blueprint": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "repair_placements": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE | _REPAIR_WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "repair_layer_coverage": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE | _REPAIR_WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "repair_map_region": _capability(
        "content_write",
        {"write"},
        _WRITE_MODE | _REPAIR_WRITE_MODE,
        writes_map=True,
        requires_target=True,
        requires_revision=True,
    ),
    "write_resource_registry": _capability("resource_write", {"write"}, _WRITE_MODE),
    "compact_spatial_index": _capability("index_write", {"write"}, _WRITE_MODE),
    "save_map_blueprint": _capability(
        "template_write", {"write"}, _WRITE_MODE, requires_target=True
    ),
    "ensure_standard_map_layers": _capability(
        "structure_write", {"write"}, _WRITE_MODE
    ),
}


def map_tools_for_stage(stage: str) -> frozenset[str]:
    """返回指定运行时阶段允许的工具名。"""
    return frozenset(
        name for name, capability in MAP_TOOL_CAPABILITIES.items() if stage in capability.stages
    )


def map_tools_for_worker_mode(mode: str) -> frozenset[str]:
    """返回指定动态 Worker mode 允许的工具名。"""
    return frozenset(
        name
        for name, capability in MAP_TOOL_CAPABILITIES.items()
        if mode in capability.worker_modes
    )


def map_tools_in_category(category: str) -> frozenset[str]:
    """返回属于指定地图能力类别的工具名。"""
    return frozenset(
        name
        for name, capability in MAP_TOOL_CAPABILITIES.items()
        if capability.category == category
    )


def validate_map_capability_contract(
    registered_tools: Mapping[str, Any],
) -> list[str]:
    """校验地图能力合同与已注册工具是否一致。

    Args:
        registered_tools: 工具名到 ToolDef 类对象的映射。

    Returns:
        不一致问题列表；空列表表示启动合同有效。
    """
    issues: list[str] = []
    missing = sorted(set(MAP_TOOL_CAPABILITIES) - set(registered_tools))
    if missing:
        issues.append("能力合同引用未注册工具：" + ", ".join(missing))
    unclassified_map_tools = sorted(
        name
        for name, tool in registered_tools.items()
        if getattr(tool, "domain", None) == "map" and name not in MAP_TOOL_CAPABILITIES
    )
    if unclassified_map_tools:
        issues.append("地图工具缺少能力合同：" + ", ".join(unclassified_map_tools))
    for name, capability in MAP_TOOL_CAPABILITIES.items():
        if capability.requires_revision and not capability.writes_map:
            issues.append(f"{name} requires_revision=true 但 writes_map=false")
        if capability.writes_map and "write" not in capability.stages:
            issues.append(f"{name} writes_map=true 但未允许 write 阶段")
    return issues
